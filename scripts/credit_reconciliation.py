#!/usr/bin/env python3
"""Prepaid-credit reconciliation: purchased − consumed = balance, per provider.

This is the "consumption" half of the charges-vs-consumption split. The EXPENSE
book records the cash top-ups (a real card charge = the deductible row). The
renders/API calls that draw down that prepaid balance are NOT new expenses — they
are consumption of credit already expensed. Logging them as expense rows would
double-count and corrupt Schedule C.

So consumption lives here, as a NON-DESTRUCTIVE reconciliation view, never as
expense rows:

    purchased (sum of top-up rows in Expense_Tracker.csv)
  − consumed  (sum of cost_usd in cost_ledger.jsonl)
  = remaining prepaid balance

Read-only on both sources; writes nothing. Run anytime for the current read, or
`--json` for a machine rollup.

RUN UNDER /usr/bin/python3 (csv/json stdlib only — no openpyxl needed; reads the
CSV mirror, not the xlsx, so it never risks the book).
"""
import argparse
import csv
import json
import re
from pathlib import Path

VAULT = Path("/Users/steve/Documents/3SK/outputs")
DEFAULT_CSV = VAULT / "02_Finance/Expense_Tracker.csv"
DEFAULT_LEDGER = Path("/Volumes/AI_Workspace/iris_studio/image_factory/cost_ledger.jsonl")

# A CSV expense row is a prepaid-credit TOP-UP (not a flat subscription, not a
# refund/fee) when its description names a credit PURCHASE. Subscriptions
# ("ChatGPT Plus", "Claude Max") and incidental "credit" rows (refund/credit memo,
# "credit card fee", "credit report") deliberately don't match — a bare "credit"
# is too broad, so it's only matched when qualified (api/prepaid/prototype credit,
# or credit top-up/reload/purchase/added).
TOPUP_RE = re.compile(
    r"top-?up|auto-?reload|prepaid|recharge|refill|funds? added|add(?:ing)? credits?"
    r"|(?:api|prepaid|prototype)\s+credit|credit\s+(?:top-?up|auto-?reload|reload|purchase|added)",
    re.I)


def model_provider(model: str) -> str:
    """Map a ledger row's model to its prepaid provider."""
    m = (model or "").lower()
    if m.startswith(("gpt-image", "dall")):
        return "OpenAI"
    if "eleven" in m or m.startswith("eleven"):
        return "ElevenLabs"
    return "OpenAI"  # image_factory is OpenAI-only today; widen when a 2nd provider lands


def load_topups(csv_path: Path):
    """{provider: purchased_usd} from top-up rows in the CSV mirror."""
    out = {}
    if not csv_path.exists():
        return out
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            desc = row.get("Description", "") or ""
            if not TOPUP_RE.search(desc):
                continue
            vendor = (row.get("Vendor", "") or "").strip() or "Unknown"
            try:
                amt = float(str(row.get("Amount ($)", "")).replace("$", "").replace(",", "").strip())
            except (TypeError, ValueError):
                continue
            if amt <= 0:  # a top-up is always a positive purchase; refunds/credits never inflate it
                continue
            out[vendor] = out.get(vendor, 0.0) + amt
    return out


def load_consumption(ledger_path: Path):
    """{provider: {consumed_usd, renders, unknown_cost}} from the cost ledger.

    Tolerates torn final lines (crash mid-write) — bad JSON lines are skipped."""
    out = {}
    if not ledger_path.exists():
        return out
    for line in ledger_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn/partial line — ignore
        prov = model_provider(rec.get("model", ""))
        agg = out.setdefault(prov, {"consumed": 0.0, "renders": 0, "unknown_cost": 0})
        agg["renders"] += 1
        cost = rec.get("cost_usd")
        if cost is None:
            agg["unknown_cost"] += 1
        else:
            agg["consumed"] += float(cost)
    return out


def reconcile(csv_path=DEFAULT_CSV, ledger_path=DEFAULT_LEDGER):
    topups = load_topups(Path(csv_path))
    consumption = load_consumption(Path(ledger_path))
    providers = sorted(set(topups) | set(consumption))
    report = {}
    for p in providers:
        purchased = topups.get(p, 0.0)
        c = consumption.get(p, {"consumed": 0.0, "renders": 0, "unknown_cost": 0})
        report[p] = {
            "purchased": round(purchased, 2),
            "consumed": round(c["consumed"], 2),
            "balance": round(purchased - c["consumed"], 2),
            "renders": c["renders"],
            "unknown_cost_rows": c["unknown_cost"],
        }
    return report


def _fmt(report) -> str:
    if not report:
        return "No prepaid providers found (no top-up rows and an empty/absent ledger)."
    lines = ["Prepaid-credit reconciliation (purchased − consumed = balance):", ""]
    lines.append(f"{'Provider':<12} {'Purchased':>10} {'Consumed':>10} {'Balance':>10} {'Renders':>8}")
    lines.append("-" * 54)
    for p, r in report.items():
        warn = "  ⚠ negative — under-funded or untracked consumption" if r["balance"] < 0 else ""
        unk = f"  ({r['unknown_cost_rows']} unknown-cost)" if r["unknown_cost_rows"] else ""
        lines.append(f"{p:<12} {r['purchased']:>10.2f} {r['consumed']:>10.2f} "
                     f"{r['balance']:>10.2f} {r['renders']:>8}{warn}{unk}")
    return "\n".join(lines)


def _selftest():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        csvp = Path(d) / "t.csv"
        with open(csvp, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Date", "Category", "Vendor", "Description", "Amount ($)",
                        "Recurring?", "Paid By", "Receipt Link", "Notes"])
            w.writerow(["2026-06-15", "Tools", "OpenAI", "$5 prototype credit", "5", "", "", "", ""])
            w.writerow(["2026-06-18", "Tools", "OpenAI", "API credit auto-reload Tier 2", "40", "", "", "", ""])
            w.writerow(["2026-04-19", "Software", "OpenAI", "ChatGPT Plus", "20", "Monthly", "", "", ""])  # NOT a top-up
            w.writerow(["2026-06-20", "Refund", "OpenAI", "Refund credit issued", "-20", "", "", "", ""])  # refund, not a purchase
            w.writerow(["2026-06-20", "Fees", "Bank", "Credit card annual fee", "95", "", "", "", ""])  # 'credit' but not a top-up
        topups = load_topups(csvp)
        assert topups == {"OpenAI": 45.0}, topups  # 5+40 only; sub, refund, and card-fee all excluded

        ledp = Path(d) / "l.jsonl"
        with open(ledp, "w") as f:
            f.write(json.dumps({"model": "gpt-image-2", "cost_usd": 0.13}) + "\n")
            f.write(json.dumps({"model": "gpt-image-2", "cost_usd": 0.13}) + "\n")
            f.write(json.dumps({"model": "gpt-image-2", "cost_usd": None}) + "\n")  # unknown cost
            f.write('{"model": "gpt-image-2", "cost_usd": 0.1')  # torn line
        cons = load_consumption(ledp)
        assert cons["OpenAI"]["renders"] == 3, cons          # torn line skipped
        assert abs(cons["OpenAI"]["consumed"] - 0.26) < 1e-9, cons
        assert cons["OpenAI"]["unknown_cost"] == 1, cons

        rep = reconcile(csvp, ledp)
        assert rep["OpenAI"]["purchased"] == 45.0
        assert rep["OpenAI"]["consumed"] == 0.26
        assert rep["OpenAI"]["balance"] == 44.74, rep

        # empty everything → empty report, no crash
        assert reconcile(Path(d) / "none.csv", Path(d) / "none.jsonl") == {}
    print("credit_reconciliation self-check: PASS")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=str(DEFAULT_CSV))
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return
    report = reconcile(args.csv, args.ledger)
    print(json.dumps(report, indent=2) if args.json else _fmt(report))


if __name__ == "__main__":
    main()
