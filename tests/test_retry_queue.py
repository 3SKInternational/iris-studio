#!/usr/bin/env python3
"""Tests for the retry queue (scripts/retry_queue.sh + scripts/retry_runner.sh).

Stdlib unittest only (repo convention). Run:
    python3 tests/test_retry_queue.py

WHY THIS EXISTS (A-82, 2026-10-13). After the 09-22 → 10-09 OAuth outage every
retry marker sat at paused=1, and nothing ever un-paused them: the fleet was
healthy again but each job waited for its own next schedule, so the five monthly
jobs skipped October. rq_clear_on_success now un-pauses the other claude markers
when a claude job recovers, and the runner replays at most N per tick, oldest
first. This file also runs the older scripts/test_retry_queue.sh, which
precheck.sh never discovered (it only globs tests/test_*.py).
"""
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# RQ_UNDER_TEST points at a copy for mutation runs; live wrappers source the real one.
QUEUE = Path(os.environ.get("RQ_UNDER_TEST") or REPO / "scripts" / "retry_queue.sh")
RUNNER = Path(os.environ.get("RUNNER_UNDER_TEST") or REPO / "scripts" / "retry_runner.sh")


def write_marker(base, job, kind="claude", paused=1, attempts=48,
                 last_attempt="2026-10-01T00:00:00Z", argv0=None,
                 first_failed="2026-09-22T00:00:00Z"):
    lines = [f"job={job}", f"kind={kind}",
             *([f"first_failed={first_failed}"] if first_failed else []),
             "first_failed_epoch=1790000000",
             f"last_attempt={last_attempt}", f"attempts={attempts}",
             f"paused={paused}", "last_reason=exit code 1"]
    if argv0 is not None:
        import base64
        raw = (argv0 + "\0" + job + "\0").encode()
        lines.append("cmd_b64=" + base64.b64encode(raw).decode())
    (Path(base) / f"{job}.retry").write_text("\n".join(lines) + "\n")


def field(base, job, key):
    for line in (Path(base) / f"{job}.retry").read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    return None


def source_and_run(base, snippet):
    """Source retry_queue.sh with alerts stubbed, then run snippet."""
    script = f'. "{QUEUE}"\n_rq_alert() {{ :; }}\n{snippet}\n'
    r = subprocess.run(["/bin/bash", "-c", script], capture_output=True,
                       text=True, env={**os.environ, "RQ_BASE": str(base)})
    assert r.returncode == 0 and r.stderr == "", (r.returncode, r.stderr)
    return r


class TestUnpauseOnRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    # "nightly" is the recovering job: failing 09-22 → 10-08 (the outage).
    def recover(self, first="2026-09-22T00:00:00Z", last="2026-10-08T00:00:00Z"):
        write_marker(self.base, "nightly", paused=0, attempts=3,
                     first_failed=first, last_attempt=last)
        source_and_run(self.base, "rq_clear_on_success nightly")

    def test_claude_recovery_unpauses_other_claude_markers(self):
        write_marker(self.base, "nightly", paused=0, attempts=3,
                     last_attempt="2026-10-08T00:00:00Z")
        write_marker(self.base, "credential-check")
        write_marker(self.base, "venture-scout")
        source_and_run(self.base, "rq_clear_on_success nightly")
        self.assertFalse((Path(self.base) / "nightly.retry").exists())
        self.assertEqual(field(self.base, "credential-check", "paused"), "0")
        self.assertEqual(field(self.base, "venture-scout", "paused"), "0")
        # attempts untouched, so a still-broken job re-pauses on its next failure
        self.assertEqual(field(self.base, "credential-check", "attempts"), "48")

    def test_job_broken_before_an_unrelated_transient_stays_paused(self):
        # reviewer round 1: every routine recovery re-armed a broken job
        write_marker(self.base, "credential-check", last_attempt="2026-10-01T00:00:00Z")
        self.recover(first="2026-10-10T00:00:00Z", last="2026-10-10T01:00:00Z")
        self.assertEqual(field(self.base, "credential-check", "paused"), "1")

    def test_job_that_failed_after_the_outage_ended_stays_paused(self):
        write_marker(self.base, "credential-check", last_attempt="2026-10-12T00:00:00Z")
        self.recover()
        self.assertEqual(field(self.base, "credential-check", "paused"), "1")

    def test_window_edges_are_inclusive(self):
        write_marker(self.base, "a", last_attempt="2026-09-22T00:00:00Z")
        write_marker(self.base, "b", last_attempt="2026-10-08T00:00:00Z")
        self.recover()
        self.assertEqual(field(self.base, "a", "paused"), "0")
        self.assertEqual(field(self.base, "b", "paused"), "0")

    def test_marker_whose_job_lock_is_held_is_skipped(self):
        write_marker(self.base, "credential-check")
        lock = Path(self.base) / "locks" / "credential-check.lock"
        lock.mkdir(parents=True)
        self.recover()
        self.assertEqual(field(self.base, "credential-check", "paused"), "1")
        self.assertTrue(lock.is_dir(), "un-pause removed a lock it did not take")

    def test_unpause_releases_the_lock_it_took(self):
        write_marker(self.base, "credential-check")
        self.recover()
        self.assertFalse((Path(self.base) / "locks" / "credential-check.lock").exists())

    def test_recovering_marker_without_first_failed_unpauses_nothing(self):
        # an open-ended window would re-arm every older paused job
        write_marker(self.base, "credential-check")
        self.recover(first=None)
        self.assertEqual(field(self.base, "credential-check", "paused"), "1")

    def test_infra_markers_stay_paused(self):
        write_marker(self.base, "nightly", paused=0, attempts=3)
        write_marker(self.base, "drive-sync", kind="infra")
        source_and_run(self.base, "rq_clear_on_success nightly")
        self.assertEqual(field(self.base, "drive-sync", "paused"), "1")

    def test_infra_recovery_unpauses_nothing(self):
        write_marker(self.base, "drive-sync", kind="infra", paused=0, attempts=2)
        write_marker(self.base, "credential-check")
        source_and_run(self.base, "rq_clear_on_success drive-sync")
        self.assertEqual(field(self.base, "credential-check", "paused"), "1")

    def test_healthy_run_without_marker_unpauses_nothing(self):
        write_marker(self.base, "credential-check")
        source_and_run(self.base, "rq_clear_on_success nightly")
        self.assertEqual(field(self.base, "credential-check", "paused"), "1")

    def test_unpaused_job_that_fails_again_repauses(self):
        write_marker(self.base, "nightly", paused=0, attempts=3)
        write_marker(self.base, "credential-check")
        source_and_run(self.base, "rq_clear_on_success nightly\n"
                       "IRIS_RETRY=1 rq_record_failure credential-check claude "
                       "'exit code 1' -- /bin/true")
        self.assertEqual(field(self.base, "credential-check", "paused"), "1")
        self.assertEqual(field(self.base, "credential-check", "attempts"), "49")


class RunnerFixture(unittest.TestCase):
    """Run a copy of retry_runner.sh next to a fake run_job.sh that logs which
    job it was asked to replay and clears that marker (a successful replay)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.scripts = root / "scripts"
        self.base = root / "retry"
        self.scripts.mkdir()
        self.base.mkdir()
        self.calls = root / "calls.txt"
        (self.scripts / "retry_runner.sh").write_text(RUNNER.read_text())
        (self.scripts / "retry_queue.sh").write_text(QUEUE.read_text())
        (self.scripts / "notify.sh").write_text("#!/bin/bash\nexit 0\n")
        (self.scripts / "run_job.sh").write_text(
            f'#!/bin/bash\necho "$1" >> "{self.calls}"\n'
            f'rm -f "{self.base}/$1.retry"\n')
        for f in ("notify.sh", "run_job.sh"):
            (self.scripts / f).chmod(0o755)
        self.wrapper = str(self.scripts / "run_job.sh")

    def tearDown(self):
        self.tmp.cleanup()

    def run_runner(self, cap=None):
        env = {**os.environ, "RQ_BASE": str(self.base),
               "RETRY_RUNNER_LOG": str(Path(self.tmp.name) / "runner.log")}
        if cap is not None:
            env["RQ_REPLAYS_PER_TICK"] = str(cap)
        subprocess.run(["/bin/bash", str(self.scripts / "retry_runner.sh")],
                       env=env, capture_output=True, text=True, check=True)
        return self.calls.read_text().split() if self.calls.exists() else []


class TestRunnerCapAndOrder(RunnerFixture):
    def test_default_cap_is_two_and_oldest_goes_first(self):
        # names sort opposite to age, so alphabetical order would fail this
        write_marker(self.base, "a-newest", paused=0,
                     last_attempt="2026-10-12T00:00:00Z", argv0=self.wrapper)
        write_marker(self.base, "b-middle", paused=0,
                     last_attempt="2026-10-05T00:00:00Z", argv0=self.wrapper)
        write_marker(self.base, "c-oldest", paused=0,
                     last_attempt="2026-10-01T00:00:00Z", argv0=self.wrapper)
        self.assertEqual(self.run_runner(), ["c-oldest", "b-middle"])
        self.assertTrue((self.base / "a-newest.retry").exists())

    def test_non_numeric_cap_falls_back_to_two(self):
        for j, t in (("a", "01"), ("b", "02"), ("c", "03")):
            write_marker(self.base, j, paused=0, argv0=self.wrapper,
                         last_attempt=f"2026-10-{t}T00:00:00Z")
        self.assertEqual(self.run_runner(cap="x"), ["a", "b"])

    def test_paused_markers_do_not_use_a_slot(self):
        write_marker(self.base, "a-paused", paused=1,
                     last_attempt="2026-09-01T00:00:00Z", argv0=self.wrapper)
        write_marker(self.base, "b", paused=0, argv0=self.wrapper)
        write_marker(self.base, "c", paused=0,
                     last_attempt="2026-10-02T00:00:00Z", argv0=self.wrapper)
        self.assertEqual(self.run_runner(cap=1), ["b"])


class TestRunnerLock(RunnerFixture):
    """A-105: the sweep lock uses the A-99 job lock. A live sweep is never
    stolen (one 8 h replay used to outlast the 6 h steal), a dead one is taken
    at once, and the exit trap leaves a lock that is no longer ours."""

    def setUp(self):
        super().setUp()
        self.lock = self.base / "locks" / ".runner.lock"
        write_marker(self.base, "job", paused=0, argv0=self.wrapper)

    def make_lock(self, pid, age_min):
        self.lock.mkdir(parents=True)
        (self.lock / "pid").write_text(f"{pid}\n")
        t = time.time() - age_min * 60
        os.utime(self.lock, (t, t))

    def test_live_sweep_at_7h_is_not_stolen(self):
        self.make_lock(os.getpid(), age_min=420)
        self.assertEqual(self.run_runner(), [])
        self.assertEqual((self.lock / "pid").read_text().strip(), str(os.getpid()))
        self.assertIn("already running", (Path(self.tmp.name) / "runner.log").read_text())

    def test_dead_sweep_is_taken_and_released(self):
        p = subprocess.Popen(["/usr/bin/true"])
        p.wait()
        self.make_lock(p.pid, age_min=1)
        self.assertEqual(self.run_runner(), ["job"])
        self.assertFalse(self.lock.exists())

    def test_normal_sweep_releases_its_lock(self):
        self.assertEqual(self.run_runner(), ["job"])
        self.assertFalse(self.lock.exists())

    def test_sweep_holds_the_lock_with_its_pid_while_replaying(self):
        # The fake wrapper records who holds the sweep lock mid-replay.
        seen = Path(self.tmp.name) / "holder.txt"
        (self.scripts / "run_job.sh").write_text(
            f'#!/bin/bash\necho "$1" >> "{self.calls}"\n'
            f'cat "{self.lock}/pid" > "{seen}"; echo "$PPID" >> "{seen}"\n'
            f'rm -f "{self.base}/$1.retry"\n')
        self.assertEqual(self.run_runner(), ["job"])
        holder, parent = seen.read_text().split()
        self.assertEqual(holder, parent)

    def test_lock_mtime_is_refreshed_between_replays(self):
        # The 24 h PID-reuse ceiling must count from the last replay, or a live
        # sweep of several 8 h replays would be stolen. First replay ages the
        # lock two days; the second records its mtime.
        write_marker(self.base, "job2", paused=0, argv0=self.wrapper,
                     last_attempt="2026-10-02T00:00:00Z")
        seen = Path(self.tmp.name) / "mtime.txt"
        (self.scripts / "run_job.sh").write_text(
            f'#!/bin/bash\necho "$1" >> "{self.calls}"\n'
            f'if [ "$1" = job ]; then touch -t 202601010000 "{self.lock}"; '
            f'else stat -f %m "{self.lock}" > "{seen}"; fi\n'
            f'rm -f "{self.base}/$1.retry"\n')
        self.assertEqual(self.run_runner(), ["job", "job2"])
        self.assertLess(time.time() - int(seen.read_text()), 600)

    def test_exit_leaves_a_lock_taken_from_it(self):
        # Mid-replay another sweep takes the lock; ours must not remove it.
        (self.scripts / "run_job.sh").write_text(
            f'#!/bin/bash\necho "$1" >> "{self.calls}"\n'
            f'echo {os.getpid()} > "{self.lock}/pid"\n'
            f'rm -f "{self.base}/$1.retry"\n')
        self.assertEqual(self.run_runner(), ["job"])
        self.assertEqual((self.lock / "pid").read_text().strip(), str(os.getpid()))


class TestLockSteal(unittest.TestCase):
    """A-99: steal a job lock only when its holder PID is dead, not after 180 min.

    Jobs may now run 8 h (e5c3ccc); the old age-only steal let a replay and a
    scheduled fire of the same long job run at once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.lock = Path(self.base) / "locks" / "job.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def make_lock(self, pid=None, age_min=0):
        self.lock.mkdir(parents=True)
        if pid is not None:
            (self.lock / "pid").write_text(f"{pid}\n")
        t = time.time() - age_min * 60
        os.utime(self.lock, (t, t))

    def acquire(self):
        r = source_and_run(self.base, 'if rq_acquire_lock job; then echo GOT $$; else echo BUSY; fi')
        return r.stdout.split()

    def dead_pid(self):
        p = subprocess.Popen(["/usr/bin/true"])
        p.wait()
        return p.pid

    def test_live_holder_at_4h_is_kept(self):
        self.make_lock(os.getpid(), age_min=240)
        self.assertEqual(self.acquire(), ["BUSY"])
        self.assertEqual((self.lock / "pid").read_text().strip(), str(os.getpid()))

    def test_dead_holder_is_taken_at_once(self):
        self.make_lock(self.dead_pid(), age_min=1)
        out = self.acquire()
        self.assertEqual(out[0], "GOT")
        self.assertEqual((self.lock / "pid").read_text().strip(), out[1])

    def test_dir_without_pid_younger_than_floor_is_kept(self):
        self.make_lock(None, age_min=170)
        self.assertEqual(self.acquire(), ["BUSY"])

    def test_dir_without_pid_older_than_floor_is_taken(self):
        self.make_lock(None, age_min=190)
        self.assertEqual(self.acquire()[0], "GOT")

    def test_live_holder_just_under_24h_ceiling_is_kept(self):
        # Pins the ceiling from below: any value under ~1430 min would steal a
        # live long run again (the A-99 bug).
        self.make_lock(os.getpid(), age_min=1430)
        self.assertEqual(self.acquire(), ["BUSY"])

    def test_live_holder_past_24h_ceiling_is_taken(self):
        self.make_lock(os.getpid(), age_min=1450)
        self.assertEqual(self.acquire()[0], "GOT")

    def test_release_removes_own_lock(self):
        source_and_run(self.base, "rq_acquire_lock job && rq_release_lock")
        self.assertFalse(self.lock.exists())

    def test_release_leaves_a_lock_taken_from_us(self):
        source_and_run(self.base, f'rq_acquire_lock job; echo {os.getpid()} > "$_RQ_LOCK/pid"; rq_release_lock')
        self.assertEqual((self.lock / "pid").read_text().strip(), str(os.getpid()))

    def test_release_removes_lock_with_no_pid_file(self):
        source_and_run(self.base, 'rq_acquire_lock job; rm -f "$_RQ_LOCK/pid"; rq_release_lock')
        self.assertFalse(self.lock.exists())


class TestLegacyShellSelfcheck(unittest.TestCase):
    def test_drop_marker_selfcheck(self):
        r = subprocess.run(["/bin/bash", str(REPO / "scripts" / "test_retry_queue.sh")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
