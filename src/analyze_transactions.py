"""
analyze_transactions.py

Reads one or more .classified.csv files produced by classify_transactions.py,
then prints an income-vs-spending summary to the console and writes
Output/summary.txt.

Usage:
    python analyze_transactions.py Output/*.classified.csv
    python analyze_transactions.py Output/*.classified.csv --flag-threshold 500
"""

import os
import sys
import glob
import argparse

try:
    import pandas as pd
except ImportError:
    raise SystemExit("Please: pip install pandas")

# ---------------------------------------------------------------------------
# Category groupings
# ---------------------------------------------------------------------------
INCOME_CATS  = {"wage", "reimbursement", }
NEUTRAL_CATS = {"transfer_internal", "credit_card_payment", "investment","wire"}
# Everything else is treated as spending


def load_files(paths: list) -> pd.DataFrame:
    dfs = []
    for p in paths:
        if not os.path.isfile(p):
            print(f"[warn] File not found, skipping: {p}", file=sys.stderr)
            continue
        df = pd.read_csv(p)
        df.columns = [c.strip().lower() for c in df.columns]
        df["_source"] = os.path.basename(p)
        dfs.append(df)
    if not dfs:
        raise SystemExit("No valid input files found.")
    return pd.concat(dfs, ignore_index=True)


def analyze(files: list, flag_threshold: float, output_dir: str = "Output",
            summary_file: str = None):
    all_data = load_files(files)

    if "amount" not in all_data.columns:
        raise SystemExit("No 'amount' column found in classified CSVs. "
                         "Did you run classify_transactions.py first?")
    if "category" not in all_data.columns:
        raise SystemExit("No 'category' column found. "
                         "Did you run classify_transactions.py first?")

    all_data["amount"] = pd.to_numeric(all_data["amount"], errors="coerce").fillna(0.0)

    income_mask  = all_data["category"].isin(INCOME_CATS)
    neutral_mask = all_data["category"].isin(NEUTRAL_CATS)
    spending_mask = ~income_mask & ~neutral_mask

    income_total   = all_data.loc[income_mask,  "amount"].sum()
    spending_total = all_data.loc[spending_mask, "amount"].sum()  # negative values
    net            = income_total + spending_total

    # Per-category totals (sorted: largest positive first)
    cat_totals = (
        all_data.groupby("category")["amount"]
        .sum()
        .sort_values(ascending=False)
    )

    # Flagged large debits — exclude CC payments already reconciled with a bank statement,
    # but keep ones where no card statement was provided (match_status == "unmatched").
    if "match_status" in all_data.columns:
        cc_reconciled = (
            (all_data["category"] == "credit_card_payment") &
            (all_data["match_status"] == "matched")
        )
    else:
        cc_reconciled = pd.Series(False, index=all_data.index)

    flagged = all_data[(all_data["amount"] < -flag_threshold) & ~cc_reconciled].copy()
    flagged = flagged.sort_values("amount")

    # Detect description column name
    desc_col = next(
        (c for c in ["description", "desc", "memo", "raw"] if c in all_data.columns),
        None,
    )

    # -----------------------------------------------------------------------
    # Build report text
    # -----------------------------------------------------------------------
    SEP = "=" * 60
    lines = []

    lines.append(f"\n{SEP}")
    lines.append("  INCOME vs. SPENDING SUMMARY")
    lines.append(SEP)
    lines.append(f"  {'Income total:':<20} ${income_total:>12,.2f}")
    lines.append(f"  {'Spending total:':<20} ${spending_total:>12,.2f}  (negative = outflow)")
    lines.append(f"  {'Net:':<20} ${net:>12,.2f}")
    lines.append(f"{SEP}\n")

    lines.append(f"{'Category':<25} {'Total ($)':>13}  Type")
    lines.append("-" * 55)
    for cat, total in cat_totals.items():
        if cat in INCOME_CATS:
            tag = "INCOME"
        elif cat in NEUTRAL_CATS:
            tag = "NEUTRAL"
        else:
            tag = "spending"
        lines.append(f"  {cat:<23} {total:>13,.2f}  {tag}")
    lines.append("")

    if not flagged.empty:
        lines.append(f"=== Flagged Transactions  (debit > ${flag_threshold:,.0f}) ===")
        for _, row in flagged.iterrows():
            desc = str(row[desc_col])[:60] if desc_col else ""
            src  = row.get("_source", "")
            lines.append(f"  {row['category']:<23} ${row['amount']:>10,.2f}  {desc}  [{src}]")
        lines.append("")

    report = "\n".join(lines)

    # -----------------------------------------------------------------------
    # Console output
    # -----------------------------------------------------------------------
    print(report)

    # -----------------------------------------------------------------------
    # File output
    # -----------------------------------------------------------------------
    os.makedirs(output_dir, exist_ok=True)
    if summary_file is None:
        out_path = os.path.join(output_dir, "summary.txt")
    elif os.path.isabs(summary_file):
        out_path = summary_file
    else:
        out_path = os.path.join(output_dir, summary_file)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(f"Summary written -> {out_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Analyze classified bank transactions and report income vs. spending."
    )
    ap.add_argument(
        "files", nargs="+",
        help="One or more .classified.csv files (glob patterns are expanded automatically)"
    )
    ap.add_argument(
        "--flag-threshold", type=float, default=500,
        help="Flag individual debits larger than this amount (default: 500)"
    )
    ap.add_argument(
        "--output-dir", default="Output",
        help="Directory for summary output (default: Output)"
    )
    ap.add_argument(
        "--summary-file", default=None,
        help="Output filename for the summary report (default: summary.csv). "
             "Relative paths are joined with --output-dir; absolute paths are used as-is."
    )
    args = ap.parse_args()

    # Expand shell globs (needed on Windows / WSL when shell doesn't expand them)
    expanded = []
    for pattern in args.files:
        matched = glob.glob(pattern)
        expanded.extend(matched if matched else [pattern])

    analyze(expanded, args.flag_threshold, args.output_dir, args.summary_file)
