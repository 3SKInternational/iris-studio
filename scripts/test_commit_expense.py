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
import tempfile
from pathlib import Path

TARGET = Path(__file__).with_name("commit_expense.py")
PY = "/usr/bin/python3"


def test_selftest_passes_under_system_python():
    proc = subprocess.run([PY, str(TARGET), "--selftest"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        f"commit_expense.py --selftest failed (rc={proc.returncode}):\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    assert "commit_expense self-check: PASS" in proc.stdout, proc.stdout


def _make_book(path):
    """Minimal valid Expense_Tracker workbook so a dry-run's history load works."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Expense Log"
    for c, name in enumerate(["Date", "Category", "Vendor", "Description",
                              "Amount ($)", "Recurring?", "Paid By",
                              "Receipt Link", "Notes"], start=1):
        ws.cell(row=4, column=c, value=name)
    wb.save(path)


def test_main_reads_rows_from_file_not_stdin():
    """Wiring: main() must dispatch a --rows FILE arg to _resolve_input().read_text,
    not stdin. Kills the `args.rows == "-"` diff mutant, which the helper-only
    _selftest can't reach (it calls commit() directly, never main()). Runs against
    a non-live temp book so a failure never pings Telegram (is_live gate)."""
    with tempfile.TemporaryDirectory() as d:
        rows = Path(d) / "rows.json"
        rows.write_text('[{"date":"2026-07-15","vendor":"TestCo",'
                        '"amount":"1.00","category":"API"}]')
        book = Path(d) / "book.xlsx"
        _make_book(book)
        proc = subprocess.run(
            [PY, str(TARGET), "--rows", str(rows), "--dry-run", "--force",
             "--xlsx", str(book)],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        # Original reads the file → dry-run succeeds. The mutant reads the empty
        # DEVNULL stdin → json error → non-zero exit.
        assert proc.returncode == 0, (
            f"main() should read the --rows file (rc={proc.returncode}):\n"
            f"{proc.stdout}\n{proc.stderr}")
        assert "dry-run" in proc.stdout.lower(), proc.stdout
        # And the non-live failure path must stay silent (is_live notify gate).
        assert "notify.sh: delivered" not in (proc.stdout + proc.stderr)


if __name__ == "__main__":
    test_selftest_passes_under_system_python()
    test_main_reads_rows_from_file_not_stdin()
    print("test_commit_expense: 2/2 pass")
