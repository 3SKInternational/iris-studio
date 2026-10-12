#!/usr/bin/env python3
"""Tests for imessage_bridge.py — the chat.db reader + AppleScript sender behind
the daemon's iMessage surface.

Stdlib unittest only (repo convention). Run:
    python3 tests/test_imessage_bridge.py

WHY THIS EXISTS. The reader is a trust boundary: whatever fetch_new returns goes
to the model as a message from Steve. So the sender filter (allowlist, iMessage
only, no group chats, never our own sends) and the cursor (must advance past
rows it ignores, or they get re-read forever) are pinned against a fake chat.db
with the real column names.
"""
import importlib.util
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("imessage_bridge", REPO / "imessage_bridge.py")
ib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ib)

STEVE = "+15551234567"
ALLOWED = {STEVE, "steve@example.com"}


def typedstream(text: str) -> bytes:
    """attributedBody bytes in the layout macOS writes (NSString, header, '+', length)."""
    raw = text.encode()
    n = len(raw)
    if n < 0x80:
        length = bytes([n])
    elif n < 0x10000:
        length = b"\x81" + n.to_bytes(2, "little")
    else:
        length = b"\x82" + n.to_bytes(4, "little")
    return (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString"
            b"\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
            + length + raw + b"\x86\x84\x02iI\x01\x05\x92\x84\x84\x84\x0cNSDictionary\x00")


NOW = 1_800_000_000.0
# Pinned independently of the module, so a wrong constant there moves the
# boundary tests below instead of cancelling out.
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc).timestamp()
MAX_AGE = 600  # the contract: texts older than 10 minutes are never answered


def apple_ns(unix: float) -> int:
    return int((unix - APPLE_EPOCH) * 1e9)


class FakeChatDb:
    """Real chat.db column names for every column the filter depends on. A fake
    with only the columns the code reads cannot hold the rows it forgot to
    exclude (tapbacks passed review once that way)."""

    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "chat.db"
        con = sqlite3.connect(self.path)
        con.executescript("""
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE message (ROWID INTEGER PRIMARY KEY, handle_id INTEGER,
                text TEXT, attributedBody BLOB, is_from_me INTEGER, service TEXT,
                cache_roomnames TEXT, associated_message_type INTEGER DEFAULT 0,
                item_type INTEGER DEFAULT 0, date INTEGER);
        """)
        con.commit()
        con.close()
        self.handles = {}

    def add(self, handle, text=None, body=None, from_me=0, service="iMessage", room=None,
            assoc=0, item=0, unix=NOW):
        con = sqlite3.connect(self.path)
        hid = 0
        if handle is not None:
            if handle not in self.handles:
                self.handles[handle] = con.execute(
                    "INSERT INTO handle (id) VALUES (?)", (handle,)).lastrowid
            hid = self.handles[handle]
        rowid = con.execute(
            "INSERT INTO message (handle_id, text, attributedBody, is_from_me, service, "
            "cache_roomnames, associated_message_type, item_type, date) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (hid, text, body, from_me, service, room, assoc, item,
             None if unix is None else apple_ns(unix))).lastrowid
        con.commit()
        con.close()
        return rowid


class TestFetchNew(unittest.TestCase):
    def setUp(self):
        self.db = FakeChatDb()
        self.addCleanup(self.db.dir.cleanup)

    def fetch(self, after=0):
        return ib.fetch_new(self.db.path, after, ALLOWED, now=NOW)

    def texts(self, after=0):
        return [(h, t) for _, h, t in self.fetch(after)[1]]

    def test_only_allowed_incoming_1to1_imessages(self):
        r1 = self.db.add(STEVE, "hi iris")
        self.db.add(STEVE, "my own send", from_me=1)
        self.db.add(STEVE, "sms copy", service="SMS")
        self.db.add(STEVE, "group chat", room="chat123")
        self.db.add("+19998887777", "stranger")
        self.db.add(None, "no handle")
        last = self.db.add("Steve@Example.com", "from email")
        cursor, msgs = self.fetch()
        self.assertEqual(msgs, [(r1, STEVE, "hi iris"), (last, "Steve@Example.com", "from email")])
        self.assertEqual(cursor, last)

    def test_tapbacks_and_system_rows_skipped(self):
        self.db.add(STEVE, "Loved \u201cHere is your V14 status\u201d", assoc=2000)
        self.db.add(STEVE, "Laughed at \u201cok\u201d", assoc=2003)
        self.db.add(STEVE, "Questioned \u201cok\u201d", assoc=2006)
        self.db.add(STEVE, "Removed a heart from \u201cok\u201d", assoc=3000)
        self.db.add(STEVE, "named the conversation", item=2)
        last = self.db.add(STEVE, "real one")
        self.assertEqual(self.fetch(), (last, [(last, STEVE, "real one")]))

    def test_old_rows_skipped_recent_kept(self):
        self.db.add(STEVE, "resynced from last year", unix=NOW - 365 * 86400)
        self.db.add(STEVE, "just too old", unix=NOW - MAX_AGE - 1)
        self.db.add(STEVE, "no date", unix=None)
        self.db.add(STEVE, "at the limit", unix=NOW - MAX_AGE)
        self.db.add(STEVE, "now")
        self.assertEqual([t for _, t in self.texts()], ["at the limit", "now"])

    def test_old_allowlisted_text_is_logged_stranger_is_not(self):
        r = self.db.add(STEVE, "sent during an outage", unix=NOW - 3600)
        self.db.add("+19998887777", "old stranger", unix=NOW - 3600)
        with self.assertLogs("iris", "WARNING") as logs:
            self.assertEqual(self.texts(), [])
        self.assertEqual(len(logs.output), 1)
        self.assertIn(f"{STEVE} older than 600s (rowid {r})", logs.output[0])

    def test_default_now_is_wall_clock(self):
        import time
        self.db.add(STEVE, "fresh", unix=time.time())
        self.assertEqual([t for _, _, t in ib.fetch_new(self.db.path, 0, ALLOWED)[1]], ["fresh"])

    def test_rebuilt_db_resets_cursor(self):
        r = self.db.add(STEVE, "a")
        self.assertEqual(self.fetch(after=500), (r, []))
        self.assertEqual(self.fetch(after=r), (r, []))  # equal is not "rebuilt"

    def test_cursor_advances_past_ignored_rows(self):
        self.db.add(STEVE, "old")
        r2 = self.db.add("+19998887777", "stranger")
        cursor, msgs = self.fetch(1)
        self.assertEqual((cursor, msgs), (r2, []))
        self.assertEqual(self.fetch(cursor), (r2, []))

    def test_no_new_rows_keeps_cursor(self):
        for _ in range(42):
            self.db.add(STEVE, "x", from_me=1)
        self.assertEqual(self.fetch(42), (42, []))

    def test_text_null_falls_back_to_attributed_body(self):
        self.db.add(STEVE, None, body=typedstream("from the blob"))
        self.assertEqual(self.texts(), [(STEVE, "from the blob")])

    def test_text_column_wins_over_body(self):
        self.db.add(STEVE, "column", body=typedstream("blob"))
        self.assertEqual(self.texts(), [(STEVE, "column")])

    def test_attachment_only_and_blank_messages_skipped(self):
        self.db.add(STEVE, "\ufffc")
        self.db.add(STEVE, "   ")
        self.db.add(STEVE, None, body=None)
        self.assertEqual(self.texts(), [])

    def test_attachment_placeholder_stripped_from_caption(self):
        self.db.add(STEVE, "\ufffclook at this")
        self.assertEqual(self.texts(), [(STEVE, "look at this")])

    def test_max_rowid(self):
        self.assertEqual(ib.max_rowid(self.db.path), 0)
        self.db.add(STEVE, "a")
        r = self.db.add(STEVE, "b")
        self.assertEqual(ib.max_rowid(self.db.path), r)

    def test_unreadable_db_raises(self):
        with self.assertRaises(sqlite3.Error):
            ib.fetch_new(Path(self.db.dir.name) / "missing.db", 0, ALLOWED)


class TestDecode(unittest.TestCase):
    def test_short_medium_long_and_unicode(self):
        for s in ("ok", "x" * 127, "y" * 128, "z" * 300, "é💸 " * 40, "w" * 70000):
            self.assertEqual(ib.decode_attributed_body(typedstream(s)), s)

    def test_needs_nsstring_marker(self):
        # A stray '+' with no NSString class name must not be read as text.
        self.assertEqual(ib.decode_attributed_body(b"abcdefgh+\x03xyz"), "")

    def test_nsstring_at_offset_zero(self):
        self.assertEqual(ib.decode_attributed_body(b"NSString\x01\x94\x84\x01+\x02hi"), "hi")

    def test_garbage_returns_empty(self):
        for blob in (None, b"", b"no class name here", b"NSString\x01\x94\x84\x01"):
            self.assertEqual(ib.decode_attributed_body(blob), "")


class TestCursorFile(unittest.TestCase):
    def test_roundtrip_and_missing_or_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state" / "cursor"
            self.assertIsNone(ib.load_cursor(p))
            ib.save_cursor(p, 1234)
            self.assertEqual(ib.load_cursor(p), 1234)
            p.write_text("junk")
            self.assertIsNone(ib.load_cursor(p))
            self.assertIsNone(ib.load_cursor(Path(d)))  # a directory: OSError, not a crash


class TestSend(unittest.TestCase):
    def test_handle_and_text_go_as_argv_not_script_source(self):
        evil = 'hi" & (do shell script "rm -rf ~") & "'
        with mock.patch.object(ib.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stderr="")) as run:
            ib.send(STEVE, evil)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "osascript")
        self.assertEqual(argv[-2:], [STEVE, evil])
        self.assertNotIn(evil, argv[2])

    def test_nonzero_exit_raises(self):
        with mock.patch.object(ib.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stderr="not authorized")):
            with self.assertRaisesRegex(RuntimeError, "not authorized"):
                ib.send(STEVE, "x")


if __name__ == "__main__":
    unittest.main()
