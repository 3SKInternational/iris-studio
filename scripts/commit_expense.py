#!/usr/bin/env python3
"""Auto-commit structured expense rows into Expense_Tracker.xlsx — safely.

This is the write-side counterpart to export_expense_csv.py (the read-side
mirror). It is the ONLY sanctioned path that mutates the canonical book, and it
crosses the "daemon never auto-writes xlsx" guardrail deliberately, with Steve's
explicit approval, gated on:

  1. an expense-reviewer SHIP verdict   (the agent review Steve required)
  2. a mandatory timestamped backup     (02_Finance/Backups/ before any write)
  3. dedup vs the CSV mirror            (vendor+date+amount already filed → skip)
  4. formula-safety                     (append data rows only, never row >200,
                                         load data_only=False so summary formulas
                                         are preserved, not frozen)

RUN UNDER /usr/bin/python3 — that interpreter has openpyxl; the iris_studio
.venv does NOT. The shebang points there on purpose.

Input: a JSON file (or stdin) — a list of row objects, keys matching the 9
'Expense Log' columns:
    date, category, vendor, description, amount, recurring, paid_by,
    receipt_link, notes
`amount` is required and numeric; date/vendor required. The rest default to "".

Usage:
    commit_expense.py --rows rows.json --review path/to/_Review.md
    commit_expense.py --rows rows.json --review ... --dry-run
    commit_expense.py --selftest          # offline, temp workbook, no live file
"""
import argparse
import csv
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import openpyxl

VAULT = Path("/Users/steve/Documents/3SK/outputs")
DEFAULT_XLSX = VAULT / "02_Finance/Expense_Tracker.xlsx"
NOTIFY = Path("/Volumes/AI_Workspace/iris_studio/scripts/notify.sh")
SHEET = "Expense Log"
HEADER_ROW = 4              # row 4 holds the column headers
FIRST_DATA_ROW = 5
MAX_DATA_ROW = 200         # summary sheets aggregate 'Expense Log'!A5:A200 — past this, formulas miss the row
COLUMNS = ["date", "category", "vendor", "description", "amount",
           "recurring", "paid_by", "receipt_link", "notes"]


# ----------------------------------------------------------------------------- helpers
def _notify(msg: str) -> None:
    if NOTIFY.exists():
        try:
            subprocess.run([str(NOTIFY), msg], timeout=15, check=False)
        except Exception:
            pass


def _amount_key(val) -> str:
    """Normalize an amount to a stable dedup key ('40' and '40.00' collide)."""
    try:
        return f"{float(str(val).replace('$', '').replace(',', '').strip()):.2f}"
    except (TypeError, ValueError):
        return str(val).strip()


def _norm_date(val) -> str:
    """Canonicalize a date to YYYY-MM-DD so '2026-6-25' and '2026-06-25' dedup.

    openpyxl may hand back a datetime (xlsx date cell) or a string; agent input is
    a string. Unparseable values are returned stripped, unchanged (never guess)."""
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d")
    s = str(val).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


def validate_rows(rows):
    """Coerce + validate input rows. Raises ValueError on the first bad row."""
    clean = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise ValueError(f"row {i}: not an object")
        date = str(r.get("date", "")).strip()
        vendor = str(r.get("vendor", "")).strip()
        if not date:
            raise ValueError(f"row {i}: missing 'date'")
        if not vendor:
            raise ValueError(f"row {i}: missing 'vendor'")
        if "amount" not in r or str(r.get("amount")).strip() == "":
            raise ValueError(f"row {i}: missing 'amount'")
        try:
            amount = float(str(r["amount"]).replace("$", "").replace(",", "").strip())
        except (TypeError, ValueError):
            raise ValueError(f"row {i}: amount {r['amount']!r} is not numeric")
        clean.append({
            "date": _norm_date(date), "category": str(r.get("category", "")).strip(),
            "vendor": vendor, "description": str(r.get("description", "")).strip(),
            "amount": amount, "recurring": str(r.get("recurring", "")).strip(),
            "paid_by": str(r.get("paid_by", "")).strip(),
            "receipt_link": str(r.get("receipt_link", "")).strip(),
            "notes": str(r.get("notes", "")).strip(),
        })
    return clean


def _dedup_key(date, vendor, amount):
    return (_norm_date(date), str(vendor).strip().lower(), _amount_key(amount))


def load_dedup_keys(xlsx_path: Path):
    """Set of (date, vendor_lower, amount_key) already filed.

    Reads the xlsx 'Expense Log' itself (authoritative) — NOT the CSV mirror,
    which can silently lag the book if a prior CSV regen failed. Unions in the
    CSV too as belt-and-suspenders, but the xlsx is the source of truth."""
    keys = set()
    if xlsx_path.exists():
        wb = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
        if SHEET in wb.sheetnames:
            ws = wb[SHEET]
            for row in ws.iter_rows(min_row=FIRST_DATA_ROW, max_col=5, values_only=True):
                date, _cat, vendor, _desc, amount = row
                if date is None and vendor is None and amount is None:
                    continue
                keys.add(_dedup_key(date, vendor, amount))
        wb.close()
    csv_path = xlsx_path.with_suffix(".csv")
    if csv_path.exists():
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                keys.add(_dedup_key(row.get("Date", ""), row.get("Vendor", ""),
                                    row.get("Amount ($)", "")))
    return keys


def review_is_ship(review_path: Path) -> bool:
    """True only if the leading YAML frontmatter says exactly status: ship.

    Parses ONLY the first ---...--- frontmatter block (so a 'status: ship' in the
    review body prose can't flip the gate), and requires the value to equal 'ship'
    exactly — 'ship-blocked' / 'ship-with-fixes' are NOT ship (binary gate)."""
    if not review_path or not review_path.exists():
        return False
    text = review_path.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"\s*---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.DOTALL)
    if not m:
        return False
    fm = m.group(1)
    sm = re.search(r"^status:\s*([^\s#]+)\s*$", fm, re.MULTILINE)
    return bool(sm and sm.group(1).strip().lower() == "ship")


def append_rows(xlsx_path: Path, rows):
    """Append validated rows to the Expense Log, formula-safe. Returns row numbers.

    Loads data_only=False so summary-sheet formulas survive the save; finds the
    first fully-blank data row and writes there; aborts before MAX_DATA_ROW so a
    row never lands outside the summary aggregation range.
    """
    wb = openpyxl.load_workbook(xlsx_path, data_only=False)
    if SHEET not in wb.sheetnames:
        raise KeyError(f"'{SHEET}' not found in {xlsx_path}")
    ws = wb[SHEET]
    ncols = len(COLUMNS)

    r = FIRST_DATA_ROW
    while any(ws.cell(row=r, column=c).value is not None for c in range(1, ncols + 1)):
        r += 1

    if r + len(rows) - 1 > MAX_DATA_ROW:
        raise RuntimeError(
            f"append would reach row {r + len(rows) - 1} > {MAX_DATA_ROW} "
            f"(outside summary-formula range A5:A200). Extend the formulas first."
        )

    written = []
    for row in rows:
        ws.cell(row=r, column=1, value=row["date"])
        ws.cell(row=r, column=2, value=row["category"])
        ws.cell(row=r, column=3, value=row["vendor"])
        ws.cell(row=r, column=4, value=row["description"])
        ws.cell(row=r, column=5, value=row["amount"])
        ws.cell(row=r, column=6, value=row["recurring"])
        ws.cell(row=r, column=7, value=row["paid_by"])
        ws.cell(row=r, column=8, value=row["receipt_link"])
        ws.cell(row=r, column=9, value=row["notes"])
        written.append(r)
        r += 1

    # Atomic save: temp in same dir, then os.replace.
    fd, tmp = tempfile.mkstemp(dir=str(xlsx_path.parent), suffix=".xlsx.tmp")
    os.close(fd)
    try:
        wb.save(tmp)
        os.replace(tmp, xlsx_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return written


def commit(rows, xlsx_path=DEFAULT_XLSX, review_path=None, force=False, dry_run=False):
    """Full pipeline: validate → lock → dedup → review gate → backup → append → CSV → notify.

    The dedup→append→save critical section runs under an exclusive flock so two
    concurrent runs (e.g. a manual fire racing the hourly sweep) can't both read
    the same first-empty row and clobber each other's write."""
    xlsx_path = Path(xlsx_path)
    if not xlsx_path.exists():
        raise FileNotFoundError(f"tracker not found at {xlsx_path}")
    is_live = xlsx_path.resolve() == DEFAULT_XLSX.resolve()

    def notify(msg):  # only ping the real Telegram channel for the live book
        if is_live:
            _notify(msg)

    clean = validate_rows(rows)

    lock_path = xlsx_path.parent / f".{xlsx_path.name}.lock"
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)

        # Dedup vs the authoritative xlsx (not the lagging mirror).
        seen = load_dedup_keys(xlsx_path)
        fresh, dupes = [], []
        for row in clean:
            k = _dedup_key(row["date"], row["vendor"], row["amount"])
            (dupes if k in seen else fresh).append(row)
            seen.add(k)  # also dedup within this batch

        if not fresh:
            print(f"commit_expense: nothing to commit ({len(dupes)} duplicate(s) skipped).")
            return {"committed": [], "dupes": dupes, "rows": []}

        # Review gate.
        if not force and not review_is_ship(Path(review_path) if review_path else None):
            raise PermissionError(
                "expense-reviewer SHIP verdict required "
                f"(--review pointing at frontmatter status: ship), or --force. Got: {review_path}"
            )
        if force:  # an unlogged bypass of the accounting-review control is the gap, so log it
            notify(f"⚠️ commit_expense: review gate FORCED (no SHIP verdict) on {len(fresh)} row(s).")

        if dry_run:
            print(f"[dry-run] would commit {len(fresh)} row(s), skip {len(dupes)} dupe(s):")
            for row in fresh:
                print(f"  {row['date']}  {row['vendor']}  ${row['amount']:.2f}  {row['category']}")
            return {"committed": [], "dupes": dupes, "rows": fresh, "dry_run": True}

        # Mandatory backup BEFORE any write — into the book's own Backups dir.
        backup_dir = xlsx_path.parent / "Backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        backup = backup_dir / f"Expense_Tracker_pre-autocommit_{stamp}.xlsx"
        shutil.copy2(xlsx_path, backup)

        try:
            written = append_rows(xlsx_path, fresh)
        except BaseException as e:
            notify(f"commit_expense FAILED on append: {e} (backup safe at {backup.name})")
            raise

        # Regenerate the CSV mirror so dedup stays current.
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import export_expense_csv
            export_expense_csv.export(xlsx_path)
        except Exception as e:
            notify(f"commit_expense: rows written (xlsx rows {written}) but CSV regen FAILED: {e}")

    total = sum(r["amount"] for r in fresh)
    lines = "\n".join(f"  {r['date']}  {r['vendor']}  ${r['amount']:.2f}" for r in fresh)
    notify(f"💸 Expense auto-commit: {len(fresh)} row(s), ${total:.2f} → rows {written}\n{lines}\n(backup: {backup.name})")
    print(f"Committed {len(fresh)} row(s) to rows {written}; {len(dupes)} dupe(s) skipped. Backup: {backup}")
    return {"committed": written, "dupes": dupes, "rows": fresh, "backup": str(backup)}


# ----------------------------------------------------------------------------- self-test
def _selftest():
    """Offline: build a synthetic workbook with the real structure, exercise append
    + dedup + the row-ceiling guard against a TEMP file. Never touches the live book."""
    import tempfile as _tf
    with _tf.TemporaryDirectory() as d:
        xlsx = Path(d) / "Expense_Tracker.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = SHEET
        for c, name in enumerate(["Date", "Category", "Vendor", "Description",
                                  "Amount ($)", "Recurring?", "Paid By",
                                  "Receipt Link", "Notes"], start=1):
            ws.cell(row=HEADER_ROW, column=c, value=name)
        wb.save(xlsx)

        # append two rows
        rows = validate_rows([
            {"date": "2026-06-25", "vendor": "OpenAI", "amount": "40", "category": "API"},
            {"date": "2026-06-25", "vendor": "ElevenLabs", "amount": 22.0, "category": "API"},
        ])
        written = append_rows(xlsx, rows)
        assert written == [5, 6], written

        # reload, confirm values landed and the amount is a real number
        wb2 = openpyxl.load_workbook(xlsx)[SHEET]
        assert wb2.cell(row=5, column=3).value == "OpenAI"
        assert abs(wb2.cell(row=6, column=5).value - 22.0) < 1e-9

        # next append starts at row 7 (first empty)
        written2 = append_rows(xlsx, validate_rows(
            [{"date": "2026-06-26", "vendor": "X", "amount": 1}]))
        assert written2 == [7], written2

        # amount-key dedup normalizes 40 == 40.00
        assert _amount_key("40") == _amount_key(40.00) == "40.00"

        # validation rejects a non-numeric amount and a missing vendor
        for bad in ([{"date": "d", "vendor": "v", "amount": "abc"}],
                    [{"date": "d", "amount": 1}]):
            try:
                validate_rows(bad)
                assert False, "should have raised"
            except ValueError:
                pass

        # date + amount keys normalize so 40==40.00 and 2026-6-25==2026-06-25
        assert _amount_key("$1,234.50") == "1234.50"
        assert _norm_date("2026-6-25") == _norm_date("2026-06-25") == "2026-06-25"
        assert _dedup_key("2026-6-25", "OpenAI", "40") == _dedup_key("2026-06-25", "openai", 40.0)

        # review gate (binary, frontmatter-only, exact 'ship'):
        rv = Path(d) / "r.md"
        rv.write_text("---\nstatus: revise\n---\n")
        assert review_is_ship(rv) is False
        rv.write_text("---\ntype: expense-review\nstatus: ship\n---\n")
        assert review_is_ship(rv) is True
        assert review_is_ship(Path(d) / "nope.md") is False
        # 'ship-blocked' / 'ship-with-fixes' must NOT pass (C1)
        rv.write_text("---\nstatus: ship-blocked\n---\n")
        assert review_is_ship(rv) is False
        rv.write_text("---\nstatus: ship with fixes\n---\n")
        assert review_is_ship(rv) is False
        # a 'status: SHIP' only in the BODY prose must NOT pass (C2)
        rv.write_text("---\nstatus: revise\n---\nbody text\nstatus: SHIP\n")
        assert review_is_ship(rv) is False

        # row-ceiling guard: filling to the cap then one more must raise
        big = Path(d) / "big.xlsx"
        wb3 = openpyxl.Workbook(); ws3 = wb3.active; ws3.title = SHEET
        for c, name in enumerate(["Date"] * 9, start=1):
            ws3.cell(row=HEADER_ROW, column=c, value=name)
        for rr in range(FIRST_DATA_ROW, MAX_DATA_ROW + 1):
            ws3.cell(row=rr, column=1, value="x")  # fill exactly to the cap
        wb3.save(big)
        try:
            append_rows(big, validate_rows([{"date": "d", "vendor": "v", "amount": 1}]))
            assert False, "ceiling guard should have raised"
        except RuntimeError:
            pass

        # C3: dedup keys come from the xlsx itself, even with NO csv mirror present.
        keys = load_dedup_keys(xlsx)  # xlsx has OpenAI/40, ElevenLabs/22, X/1
        assert not xlsx.with_suffix(".csv").exists()  # no mirror at all
        assert _dedup_key("2026-06-25", "OpenAI", "40.00") in keys
        assert _dedup_key("2026-06-25", "ElevenLabs", 22) in keys

    print("commit_expense self-check: PASS")


# ----------------------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rows", help="JSON file of row objects (or '-' for stdin)")
    ap.add_argument("--review", help="path to the expense-reviewer SHIP verdict")
    ap.add_argument("--xlsx", default=str(DEFAULT_XLSX))
    ap.add_argument("--force", action="store_true", help="bypass the review gate (logged)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        _selftest()
        return

    if not args.rows:
        ap.error("--rows is required (JSON file or '-' for stdin)")
    try:
        raw = sys.stdin.read() if args.rows == "-" else Path(args.rows).read_text(encoding="utf-8")
        rows = json.loads(raw)
        if isinstance(rows, dict):
            rows = [rows]
        commit(rows, xlsx_path=args.xlsx, review_path=args.review,
               force=args.force, dry_run=args.dry_run)
    except Exception as e:
        _notify(f"commit_expense FAILED: {e}")
        sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    main()
