#!/usr/bin/env python3
"""Wrapper so commit_expense.py's --selftest is part of the precheck.sh gate.

commit_expense.py is deliberately run under /usr/bin/python3 (the interpreter
that has openpyxl; the iris_studio venv/homebrew python3 does not — see its
own docstring). precheck.sh's generic `python3 <test>` invocation therefore
can't import it directly, and there was no scripts/test_commit_expense.py at
all — so the msg-id dedup fix (2026-07-28 two-pass audit) had a real selftest
that the mandatory pre-review gate would never have run. Fixed by shelling to
the documented interpreter explicitly, so THIS wrapper runs under any python3
while still exercising the real selftest end-to-end.

Run: python3 scripts/test_commit_expense.py   (exit 0 = pass)
"""
import subprocess
from pathlib import Path

TARGET = Path(__file__).with_name("commit_expense.py")


def test_selftest_passes_under_system_python():
    proc = subprocess.run(["/usr/bin/python3", str(TARGET), "--selftest"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        f"commit_expense.py --selftest failed (rc={proc.returncode}):\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    assert "commit_expense self-check: PASS" in proc.stdout, proc.stdout


if __name__ == "__main__":
    test_selftest_passes_under_system_python()
    print("test_commit_expense: 1/1 pass")
