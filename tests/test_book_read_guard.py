"""Tests for book_read_guard.probe — the bounded EDEADLK retry (A-77)."""
import errno
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import book_read_guard as g  # noqa: E402


def _flaky_reader(fail_times, err=errno.EDEADLK):
    """Reader that raises OSError(err) the first `fail_times` calls, then returns bytes."""
    state = {"n": 0}

    def reader():
        if state["n"] < fail_times:
            state["n"] += 1
            raise OSError(err, os.strerror(err))
        return b"BOOK CONTENT"

    return reader


def _no_sleep(_):
    pass


class TestReadGuard(unittest.TestCase):
    def test_reads_clean_first_try(self):
        code, msg = g.probe(_flaky_reader(0), sleep=_no_sleep)
        self.assertEqual(code, g.OK, msg)
        self.assertIn("1 attempt", msg)

    def test_recovers_after_transient_deadlock(self):
        # Fails twice, succeeds on the 3rd — a transient flap the retry absorbs.
        code, msg = g.probe(_flaky_reader(2), attempts=3, sleep=_no_sleep)
        self.assertEqual(code, g.OK, msg)
        self.assertIn("3 attempt", msg)

    def test_persistent_deadlock_aborts(self):
        # Fails every attempt -> honest abort (broke), never a false OK.
        code, msg = g.probe(_flaky_reader(99), attempts=3, sleep=_no_sleep)
        self.assertEqual(code, g.DEADLOCK, msg)

    def test_eagain_is_also_retried(self):
        code, _ = g.probe(_flaky_reader(1, err=errno.EAGAIN), attempts=3, sleep=_no_sleep)
        self.assertEqual(code, g.OK)

    def test_missing_file(self):
        def reader():
            raise FileNotFoundError()

        code, _ = g.probe(reader, sleep=_no_sleep)
        self.assertEqual(code, g.MISSING)

    def test_non_retriable_oserror_does_not_loop(self):
        calls = {"n": 0}

        def reader():
            calls["n"] += 1
            raise OSError(errno.EACCES, "denied")

        code, _ = g.probe(reader, attempts=3, sleep=_no_sleep)
        self.assertEqual(code, g.OTHER)
        self.assertEqual(calls["n"], 1)  # bailed immediately, did not burn retries

    def test_delay_only_between_attempts_not_after_last(self):
        slept = []
        g.probe(_flaky_reader(99), attempts=3, sleep=slept.append, delay_seconds=10)
        self.assertEqual(slept, [10, 10])  # 3 attempts -> 2 sleeps, none trailing

    def test_exit_code_contract_is_literal(self):
        # The book-update routine keys on these LITERAL codes (Pass 0a). Pin them
        # against literals so a value drift can't ride along with a symbolic assert.
        self.assertEqual((g.OK, g.DEADLOCK, g.MISSING, g.OTHER), (0, 2, 3, 4))

    def test_default_attempts_is_three(self):
        # Default attempts=3: 3 straight fails exhaust it -> DEADLOCK. If the default
        # were 4, the 4th (would-be) read would recover. Pins the documented default.
        code, _ = g.probe(_flaky_reader(3), sleep=_no_sleep)  # no attempts arg
        self.assertEqual(code, g.DEADLOCK)
        code, _ = g.probe(_flaky_reader(2), sleep=_no_sleep)  # recovers within 3
        self.assertEqual(code, g.OK)

    def test_main_reads_real_file(self):
        import tempfile

        with tempfile.NamedTemporaryFile("wb", suffix=".md", delete=False) as f:
            f.write(b"# book\n")
            path = f.name
        try:
            self.assertEqual(g.main(["prog", path]), 0)
        finally:
            os.unlink(path)

    def test_main_missing_file_returns_missing(self):
        self.assertEqual(g.main(["prog", "/no/such/book/xyz.md"]), 3)

    def test_main_requires_exactly_one_path(self):
        self.assertEqual(g.main(["prog"]), 4)  # no path -> usage/OTHER
        self.assertEqual(g.main(["prog", "a.md", "b.md"]), 4)  # two paths -> OTHER
        # flags are stripped, so one real path plus flags is still valid arg-count
        # (a missing file, so MISSING not OTHER — proves the flag filter works)
        self.assertEqual(g.main(["prog", "--x", "/no/such/xyz.md"]), 3)

    def test_real_file_round_trip(self):
        # End-to-end through the real filesystem path, no injection.
        import tempfile

        with tempfile.NamedTemporaryFile("wb", suffix=".md", delete=False) as f:
            f.write(b"# real book\n")
            path = f.name
        try:
            code, msg = g.probe(lambda: open(path, "rb").read(), sleep=_no_sleep)
            self.assertEqual(code, g.OK, msg)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
