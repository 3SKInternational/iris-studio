#!/usr/bin/env python3
"""book_read_guard.py — bounded-retry readability probe for a book file.

The book-update routine reads sellable books that live under ~/Documents, a path
the Cowork sandbox VM shares into a Linux guest. While that VM holds a book open,
host-side reads intermittently fail with EDEADLK ("Resource deadlock avoided") at
byte 0. The 2026-08-25 run saw a stable 3-of-4 split for ~40 min, then it cleared
on its own — the deadlock is TRANSIENT, tied to the VM being up (A-77 diagnosis).

Instantly tallying a book `broke` on the first EDEADLK throws away a whole night
over a flap that clears in minutes. This guard retries a real read a bounded number
of times before giving up, so book-update only aborts a book when the deadlock
actually persists — and when it does persist, the abort stays honest (no torn write
to a revenue artifact, exactly the 08-25 behavior).

The retry is a script, not a prompt instruction, on purpose: a deterministic
sleep-and-reread loop can't be fumbled by an agent, and the failing reads happen
in the routine's own shell (Pass 0 find / Pass 1 backup cp) where a preflight
gate belongs.

Exit codes (the routine keys on these):
  0  readable (now, or after a retry)
  2  persistent deadlock after all attempts  -> tally `broke`, honest abort
  3  file missing
  4  other OSError (permissions, etc.)        -> tally `broke`
"""
import errno
import sys
import time

# The transient "try again shortly" class for a VM-shared file. EDEADLK is the one
# observed on 08-25; EAGAIN is its documented sibling for a busy share resource.
RETRIABLE = frozenset({errno.EDEADLK, errno.EAGAIN})

OK, DEADLOCK, MISSING, OTHER = 0, 2, 3, 4


def probe(reader, attempts=3, sleep=time.sleep, delay_seconds=10.0):
    """Call reader() until it returns, retrying only the transient-deadlock class.

    reader: () -> bytes, may raise OSError.
    Returns (exit_code, message). sleep/delay_seconds are injectable so tests
    don't wait real seconds.
    """
    for attempt in range(1, attempts + 1):  # mutequiv: attempts+1 upper bound is belt-and-suspenders — the `attempt < attempts` guard returns DEADLOCK at the last attempt, so no iteration past `attempts` is ever reached (see the `# unreachable` line below)
        try:
            data = reader()
        except FileNotFoundError:
            return MISSING, "missing: file not found"
        except OSError as e:
            if e.errno in RETRIABLE:
                if attempt < attempts:
                    sleep(delay_seconds)
                    continue
                return DEADLOCK, (
                    f"deadlock: errno {e.errno} persisted after {attempts} attempt(s)"
                )
            return OTHER, f"error: errno {e.errno} ({e.strerror})"
        return OK, f"ok: {len(data)} bytes after {attempt} attempt(s)"
    # unreachable (attempts >= 1), but fail closed rather than silently return OK
    return DEADLOCK, "deadlock: no attempt made"


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        print("usage: book_read_guard.py <file>", file=sys.stderr)
        return OTHER
    path = args[0]

    def read_file():
        with open(path, "rb") as fh:
            return fh.read()

    code, msg = probe(read_file)
    print(f"{path}: {msg}", file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":  # mutequiv: ->False is the standard entrypoint mutant — already False under import, no observable effect
    sys.exit(main(sys.argv))
