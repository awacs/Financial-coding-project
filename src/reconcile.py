"""
reconcile.py

Full pipeline in one shot: classify → reconcile → analyze.

Usage:
    python src/reconcile.py \
        --bank  Input/Chase1600_Activity_20251011.CSV \
        --card  Input/Chase4488_Activity20250801_20251001_20251011.CSV \
        --card  Input/Chase5650_Activity20250801_20251001_20251011.CSV \
        -p src/params.yaml

Exits with code 1 if any payment rows are left unmatched.
"""

import os
import sys
import glob
import argparse
from typing import List

import pandas as pd

# Make sibling scripts importable regardless of working directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classify_transactions import classify_file  # noqa: E402
from analyze_transactions import analyze          # noqa: E402

# Order in which to look for a usable date column
DATE_COL_PRIORITY = ["posting date", "transaction date", "post date", "date"]

# Keywords in bank descriptions that indicate which card institution was paid
CARD_INSTITUTION_HINTS = {
    "American Express": ["AMERICAN EXPRESS", "AMEX"],
    "Discover":         ["DISCOVER"],
    "Chase":            ["CHASE CREDIT", "CHASE CRD"],
    "Citi":             ["CITI", "CITIBANK"],
    "Capital One":      ["CAPITAL ONE"],
    "Bank of America":  ["BANK OF AMERICA", "BANKAMERICARD"],
    "Wells Fargo":      ["WELLS FARGO"],
    "Barclays":         ["BARCLAYS"],
    "Synchrony":        ["SYNCHRONY"],
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def classified_path(raw_path: str, output_dir: str) -> str:
    """Derive Output/<stem>.classified.csv from any raw input path."""
    stem = os.path.splitext(os.path.basename(raw_path))[0]
    return os.path.join(output_dir, f"{stem}.classified.csv")


def detect_institution(description: str) -> str:
    """Return the card institution name inferred from a bank description, or ''."""
    up = description.upper()
    for institution, keywords in CARD_INSTITUTION_HINTS.items():
        if any(k in up for k in keywords):
            return institution
    return ""


def load(path: str, role: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]

    # Normalize date columns so bank ("posting date") and card ("post date")
    # merge into a single column in the reconciled report.
    if "post date" in df.columns and "posting date" not in df.columns:
        df = df.rename(columns={"post date": "posting date"})

    df["account"] = os.path.splitext(os.path.basename(path))[0]
    df["role"] = role
    df["match_id"] = ""
    df["match_status"] = "n/a"
    return df


def best_dates(df: pd.DataFrame) -> pd.Series:
    for col in DATE_COL_PRIORITY:
        if col in df.columns:
            parsed = pd.to_datetime(df[col], errors="coerce")
            if parsed.notna().any():
                return parsed
    return pd.Series([pd.NaT] * len(df), index=df.index)


def card_payment_mask(df: pd.DataFrame, amounts: pd.Series) -> pd.Series:
    if "type" in df.columns:
        is_pmt = df["type"].str.strip().str.lower() == "payment"
        if is_pmt.any():
            return is_pmt & (amounts > 0)
    return amounts > 0


def desc(row: pd.Series, df: pd.DataFrame, width: int = 55) -> str:
    for col in ["description", "memo", "raw"]:
        if col in df.columns and pd.notna(row.get(col)):
            return str(row[col])[:width]
    return ""


# ── Reconciliation ────────────────────────────────────────────────────────────

def _reconcile(bank_path: str, card_paths: List[str],
               date_tol: int, amount_tol: float,
               output_path: str) -> bool:
    """
    Matches classified bank and card CSVs, writes combined report.
    Returns True if any unmatched payments were found.
    """
    bank = load(bank_path, "bank")
    if "category" not in bank.columns:
        sys.exit(f"ERROR: 'category' column missing in {bank_path}.")

    cards = [load(p, "card") for p in card_paths]

    bank_amounts = pd.to_numeric(bank["amount"], errors="coerce")
    bank_dates   = best_dates(bank)
    card_amounts = [pd.to_numeric(c["amount"], errors="coerce") for c in cards]
    card_dates   = [best_dates(c) for c in cards]

    match_counter = 0
    used: set = set()

    bank_pmt_idx = bank.index[bank["category"] == "credit_card_payment"]

    for b_idx in bank_pmt_idx:
        b_amt  = abs(bank_amounts[b_idx])
        b_date = bank_dates[b_idx]

        if pd.isna(b_amt) or pd.isna(b_date):
            bank.at[b_idx, "match_status"] = "unmatched"
            continue

        found = False
        for ci, card in enumerate(cards):
            pmt_mask = card_payment_mask(card, card_amounts[ci])
            for c_idx in card.index[pmt_mask]:
                if (ci, c_idx) in used:
                    continue
                c_amt  = card_amounts[ci][c_idx]
                c_date = card_dates[ci][c_idx]
                if pd.isna(c_date):
                    continue
                if (abs(b_amt - c_amt) <= amount_tol
                        and abs((b_date - c_date).days) <= date_tol):
                    mid = f"M{match_counter:04d}"
                    match_counter += 1
                    bank.at[b_idx, "match_id"]          = mid
                    bank.at[b_idx, "match_status"]      = "matched"
                    cards[ci].at[c_idx, "match_id"]     = mid
                    cards[ci].at[c_idx, "match_status"] = "matched"
                    used.add((ci, c_idx))
                    found = True
                    break
            if found:
                break

        if not found:
            bank.at[b_idx, "match_status"] = "unmatched"

    for ci, card in enumerate(cards):
        pmt_mask = card_payment_mask(card, card_amounts[ci])
        for c_idx in card.index[pmt_mask]:
            if (ci, c_idx) not in used:
                cards[ci].at[c_idx, "match_status"] = "unmatched"

    combined = pd.concat([bank] + cards, ignore_index=True)
    sort_col = next((c for c in DATE_COL_PRIORITY if c in combined.columns), None)
    if sort_col:
        combined[sort_col] = pd.to_datetime(combined[sort_col], errors="coerce")
        combined = combined.sort_values([sort_col, "account"], na_position="last")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    combined.to_csv(output_path, index=False)

    # ── Summary ───────────────────────────────────────────────────────────────
    n_bank_pmts      = len(bank_pmt_idx)
    n_matched        = int((bank["match_status"] == "matched").sum())
    n_unmatched_bank = int((bank["match_status"] == "unmatched").sum())
    n_unmatched_card = int(sum((c["match_status"] == "unmatched").sum() for c in cards))

    SEP = "=" * 58
    print(f"\n{SEP}")
    print("  RECONCILIATION SUMMARY")
    print(SEP)
    print(f"  Bank credit-card payments found : {n_bank_pmts}")
    print(f"  Matched pairs                   : {n_matched}")
    print(f"  Unmatched — bank side           : {n_unmatched_bank}")
    print(f"  Unmatched — card side           : {n_unmatched_card}")
    print(SEP)

    has_warnings = False

    if n_unmatched_bank > 0:
        has_warnings = True
        print("\n[WARN] Unmatched bank payments (no corresponding card credit found):")
        ub = bank[bank["match_status"] == "unmatched"]
        missing_institutions: set = set()
        for b_idx, row in ub.iterrows():
            amt      = abs(bank_amounts[b_idx])
            date     = bank_dates[b_idx]
            date_str = str(date.date()) if pd.notna(date) else "?"
            row_desc = desc(row, bank)
            institution = detect_institution(row_desc)
            institution_tag = f"  ← no {institution} statement provided" if institution else ""
            print(f"  {date_str}  ${amt:>10,.2f}  {row_desc}{institution_tag}")
            if institution:
                missing_institutions.add(institution)
        if missing_institutions:
            print()
            for inst in sorted(missing_institutions):
                print(f"  [WARN] Bank has payments to {inst} but no {inst} card statement was provided.")

    if n_unmatched_card > 0:
        has_warnings = True
        print("\n[WARN] Unmatched card payments (no corresponding bank debit found):")
        for ci, card in enumerate(cards):
            uc = card[card["match_status"] == "unmatched"]
            for c_idx, row in uc.iterrows():
                amt      = card_amounts[ci][c_idx]
                date     = card_dates[ci][c_idx]
                date_str = str(date.date()) if pd.notna(date) else "?"
                acct     = row.get("account", card_paths[ci])
                print(f"  {date_str}  ${amt:>10,.2f}  {desc(row, card)}  [{acct}]")

    print(f"\nReconciled report -> {output_path}")
    return has_warnings


# ── Full pipeline ─────────────────────────────────────────────────────────────

def run_pipeline(bank_raw: str, cards_raw: List[str], params_path: str,
                 output_dir: str, date_tol: int, amount_tol: float,
                 flag_threshold: float, summary_file: str = None) -> None:

    if "ANTHROPIC_API_KEY" not in os.environ:
        sys.exit("Please set ANTHROPIC_API_KEY in your environment.")

    os.makedirs(output_dir, exist_ok=True)

    # Step 1: Classify
    print("\n── Step 1: Classifying ─────────────────────────────────────────────")
    bank_classified = classified_path(bank_raw, output_dir)
    print(f"\n  Bank : {os.path.basename(bank_raw)}")
    classify_file(bank_raw, bank_classified, params_path)

    cards_classified = []
    for card_raw in cards_raw:
        cp = classified_path(card_raw, output_dir)
        print(f"\n  Card : {os.path.basename(card_raw)}")
        classify_file(card_raw, cp, params_path)
        cards_classified.append(cp)

    # Step 2: Reconcile
    print("\n── Step 2: Reconciling ─────────────────────────────────────────────")
    bank_stem = os.path.splitext(os.path.basename(bank_raw))[0]
    reconcile_out = os.path.join(output_dir, f"{bank_stem}.reconciled.csv")
    has_warnings = _reconcile(bank_classified, cards_classified,
                              date_tol, amount_tol, reconcile_out)

    # Step 3: Analyze
    print("\n── Step 3: Analyzing ───────────────────────────────────────────────")
    analyze([reconcile_out], flag_threshold, output_dir, summary_file)

    if has_warnings:
        sys.exit(1)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Classify, reconcile, and analyze bank + credit card statements in one shot."
    )
    ap.add_argument("--bank", required=True,
                    help="Raw bank CSV (e.g. Input/Chase1600_Activity.CSV)")
    ap.add_argument("--card", required=True, action="append", dest="cards",
                    help="Raw credit card CSV — repeat for each card")
    ap.add_argument("-p", "--params", default="src/params.yaml",
                    help="Path to params.yaml (default: src/params.yaml)")
    ap.add_argument("--output-dir", default="Output",
                    help="Directory for all output files (default: Output)")
    ap.add_argument("--date-tolerance", type=int, default=5,
                    help="Max days between bank payment and card credit (default: 5)")
    ap.add_argument("--amount-tolerance", type=float, default=0.01,
                    help="Max dollar difference for amount match (default: 0.01)")
    ap.add_argument("--flag-threshold", type=float, default=500,
                    help="Flag debits larger than this in the analysis (default: 500)")
    ap.add_argument("--summary-file", default=None,
                    help="Output filename for the summary report (default: summary.txt). "
                         "Relative paths are joined with --output-dir; absolute paths are used as-is.")
    args = ap.parse_args()

    expanded_cards = []
    for pattern in args.cards:
        matched = glob.glob(pattern)
        expanded_cards.extend(matched if matched else [pattern])

    run_pipeline(
        args.bank,
        expanded_cards,
        args.params,
        args.output_dir,
        args.date_tolerance,
        args.amount_tolerance,
        args.flag_threshold,
        args.summary_file,
    )
