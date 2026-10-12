"""iMessage transport for the iris.py daemon: read new texts from Messages'
chat.db, send replies through Messages via AppleScript.

Reading chat.db needs Full Disk Access on the daemon's Python; sending needs
the Automation permission (Python -> Messages). Both are one-time macOS grants.
No model logic lives here; iris.py decides what to say.
"""
import logging
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

log = logging.getLogger("iris")

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
APPLE_EPOCH = 978307200  # message.date counts nanoseconds from 2001-01-01 UTC
MAX_AGE_SECONDS = 600

# Every new row since the cursor, unfiltered, so the cursor can advance past
# rows we ignore (our own sends, SMS, group chats, other senders).
_NEW_ROWS_SQL = """
SELECT m.ROWID, h.id, m.text, m.attributedBody, m.is_from_me, m.service,
       m.cache_roomnames, m.associated_message_type, m.item_type, m.date
FROM message m LEFT JOIN handle h ON h.ROWID = m.handle_id
WHERE m.ROWID > ? ORDER BY m.ROWID
"""

# `on run argv` keeps the handle and text out of the script source, so a quote
# or backslash in a reply can't break or inject AppleScript.
_SEND_SCRIPT = """
on run argv
  tell application "Messages"
    set svc to 1st account whose service type = iMessage
    send (item 2 of argv) to participant (item 1 of argv) of svc
  end tell
end run
"""


def _connect(db_path):
    # closing(): a bare sqlite3 connection's `with` only commits, never closes.
    return closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5))  # mutequiv: lock-wait tuning value


def max_rowid(db_path=CHAT_DB) -> int:
    with _connect(db_path) as con:
        return con.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0]


def decode_attributed_body(blob) -> str:
    """Pull the plain string out of an attributedBody typedstream. Recent macOS
    leaves message.text NULL and stores the text only here. Layout after the
    'NSString' class name: a few header bytes, '+', then a length (one byte, or
    0x81 + uint16 LE, or 0x82 + uint32 LE), then UTF-8 bytes."""
    if not blob:
        return ""
    i = blob.find(b"NSString")
    if i < 0:
        return ""
    plus = blob.find(b"+", i + 8, i + 16)  # mutequiv: window is slack at both ends; i+8 is a header byte and the real "+" sits at i+12
    if plus < 0:  # mutequiv: find starts at i+8, so plus is never 0 or 1
        return ""
    p = plus + 1
    n = blob[p] if p < len(blob) else 0  # mutequiv: blob has ended, any length slices empty
    if n == 0x81:
        n, p = int.from_bytes(blob[p + 1:p + 3], "little"), p + 3
    elif n == 0x82:
        n, p = int.from_bytes(blob[p + 1:p + 5], "little"), p + 5
    else:
        p += 1
    return blob[p:p + n].decode("utf-8", "replace")


def fetch_new(db_path, after: int, allowed: set[str], now: float | None = None
              ) -> tuple[int, list[tuple[int, str, str]]]:
    """Return (new_cursor, [(rowid, handle, text), ...]) for recent incoming 1:1
    iMessages from an allowed handle with ROWID > after. allowed must be
    lowercased. If chat.db was rebuilt and its MAX(ROWID) fell below `after`,
    the cursor drops back to it so new messages are not ignored forever."""
    now = time.time() if now is None else now
    with _connect(db_path) as con:
        top = con.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0]
        if top < after:
            return top, []
        rows = con.execute(_NEW_ROWS_SQL, (after,)).fetchall()
    cursor, out = after, []
    for rowid, handle, text, body, from_me, service, room, assoc, item, date in rows:
        cursor = max(cursor, rowid)
        # service == 'iMessage' only: SMS sender ids are not Apple-authenticated.
        # assoc != 0 is a tapback/reaction row; item != 0 is a rename, member
        # change or similar. Both look like a message from Steve otherwise.
        if from_me or service != "iMessage" or room or not handle or assoc or item:
            continue
        if handle.lower() not in allowed:
            continue
        # ROWID order is not time order: an iCloud resync appends old texts.
        # Texts that sat unread through a long outage land here too; log them.
        if (date or 0) / 1e9 + APPLE_EPOCH < now - MAX_AGE_SECONDS:  # mutequiv: a NULL date as 0 or 1 ns is 2001 either way
            log.warning(f"iMessage: skipped text from {handle} older than "
                        f"{MAX_AGE_SECONDS}s (rowid {rowid})")
            continue
        text = (text or decode_attributed_body(body)).replace("\ufffc", "").strip()
        if text:
            out.append((rowid, handle, text))
    return cursor, out


def load_cursor(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):  # missing, unreadable or corrupt: start fresh
        return None


def save_cursor(path: Path, cursor: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(str(cursor))
    tmp.replace(path)


def send(handle: str, text: str) -> None:
    """Send text to handle from the Mac's signed-in iMessage account. Raises on failure."""
    r = subprocess.run(["osascript", "-e", _SEND_SCRIPT, handle, text],
                       capture_output=True, text=True, timeout=30)  # mutequiv: tuning value
    if r.returncode != 0:
        raise RuntimeError(f"osascript exit {r.returncode}: {r.stderr.strip()}")
