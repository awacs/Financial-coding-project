# Bank Transaction Classifier

Classifies Chase bank transaction CSVs into spending categories using Claude (Anthropic LLM), then summarizes income vs. spending across all accounts.

## How it works

1. **`classify_transactions.py`** reads a transaction CSV, sends rows in batches to Claude, and writes back the original rows plus `category`, `confidence`, `rationale`, and `model` columns.
2. **`analyze_transactions.py`** reads one or more classified CSVs and prints an income-vs-spending summary to the console and writes `Output/summary.csv`.

---

## Setup

### Install dependencies

```bash
pip install anthropic pandas pyyaml
```

### Set your API key

```bash
# Linux / macOS / WSL
export ANTHROPIC_API_KEY="sk-ant-..."

# Windows PowerShell
$env:ANTHROPIC_API_KEY="sk-ant-..."
```

---

## Usage

```bash
python src/reconcile.py \
    --bank Input/Chase1600_Activity_20251011.CSV \
    --card Input/Chase4488_Activity20250801_20251001_20251011.CSV \
    --card Input/Chase5650_Activity20250801_20251001_20251011.CSV \
    -p src/params.yaml
```

This runs the full pipeline in one shot:

### Step 1 — Classify each account (automatic)

Classifies each input CSV via the Anthropic API and saves to `Output/<name>.classified.csv`.

### Step 2 — Reconcile credit cards against the bank (automatic)

Cross-references every `credit_card_payment` on the bank statement against the
corresponding payment credit on each card statement, matching by amount + date.

```bash
python src/reconcile.py \
    --bank Output/Chase1600.classified.csv \
    --card Output/Chase4488.classified.csv \
    --card Output/Chase5650.classified.csv

# Loosen the date window (default 5 days) or amount tolerance (default $0.01):
python src/reconcile.py \
    --bank Output/Chase1600.classified.csv \
    --card Output/Chase4488.classified.csv \
    --card Output/Chase5650.classified.csv \
    --date-tolerance 7 --amount-tolerance 0.05
```

Output: `Output/reconciled_report.csv` — all transactions from all accounts combined in one file. Exits with code **1** and prints `[WARN]` lines for any unmatched payments, including a note if a payment appears to belong to a card statement that wasn't provided.

### Step 3 — Analyze the reconciled report (automatic)

**CLI options:**

| Flag | Default | Description |
|---|---|---|
| `--bank` | *(required)* | Raw bank CSV |
| `--card` | *(required, repeatable)* | Raw credit card CSV(s) |
| `-p / --params` | `src/params.yaml` | Path to params.yaml |
| `--output-dir` | `Output` | Directory for all output files |
| `--date-tolerance` | `5` | Max days between bank payment and card credit |
| `--amount-tolerance` | `0.01` | Max dollar difference for amounts to be considered equal |
| `--flag-threshold` | `500` | Flag debits larger than this in the analysis |

**Why credit card payments aren't double-counted:**

Both sides of a payment are classified as `credit_card_payment`, which is a **neutral** category excluded from both income and spending totals:

| Row | Account | Amount | Category | Counted as |
|---|---|---|---|---|
| Pay card bill | Bank 1600 | -$1,829 | `credit_card_payment` | neutral — excluded |
| Payment received | Card 4488 | +$1,829 | `credit_card_payment` | neutral — excluded |
| Grocery purchase | Card 4488 | -$85 | `groceries` | spending ✓ |

Only the actual credit card spending flows into the spending total.

---

## Output files

| File | Description |
|---|---|
| `Output/<account>.classified.csv` | Original rows + `category`, `confidence`, `rationale`, `model` |
| `Output/summary.csv` | Per-category totals + `INCOME_TOTAL`, `SPENDING_TOTAL`, `NET` rollup rows |
| `Output/reconciled_report.csv` | All transactions from all accounts combined, with `account`, `role`, `match_id`, `match_status` columns |

**`match_status` values:**

| Value | Meaning |
|---|---|
| `matched` | Payment row paired with a counterpart on the other side |
| `unmatched` | Payment row with no counterpart found — investigate |
| `n/a` | Regular spending/income row, not a payment |

---

## Defining categories

All category configuration lives in **`src/params.yaml`**. There are three layers to customize:

---

### 1. The `categories` list — what categories exist

This is the master list of every category the model may assign. Add, remove, or rename entries here to match your personal finances.

```yaml
categories:
  - wage
  - reimbursement
  - groceries
  - dining
  - misc          # always keep a catch-all
```

**Rules:**
- Use `snake_case` names (the model returns these verbatim).
- Order matters for tie-breaking: the model prefers the most specific category, but if two are equally plausible it may favor whichever appears first.
- Always keep at least one catch-all (e.g. `misc`). The model is instructed to fall back to `misc` when nothing else fits.

---

### 2. `keyword_hints` — fast local pre-classification

Before each batch is sent to Claude, the script runs a cheap local keyword scan. If a transaction description matches any keyword in a category's hint list, that suggestion is passed to the model as a hint (the model still has final say).

```yaml
keyword_hints:
  wage:
    - "PAYROLL"
    - "UNIVERSITY OF CA"
  groceries:
    - "TRADER JOE"
    - "WHOLE FOODS"
    - "RALPHS"
  subscriptions:
    - "NETFLIX"
    - "SPOTIFY"
    - "GITHUB"
```

**Rules:**
- Keywords are **case-insensitive** substrings — `"payroll"` matches `"ADP PAYROLL DEPOSIT"`.
- A transaction can match multiple categories; the one with the most keyword hits wins the hint.
- Hints are informational — the LLM will override them when the hint is wrong.
- Omitting a category from `keyword_hints` is fine; those transactions simply go straight to the LLM without a pre-hint.

**When to add a keyword hint:**
- You have a recurring merchant or payee with a distinctive string in the description (e.g. your employer's ACH name).
- You want to reduce API cost by front-loading obvious classifications.

---

### 3. Income / Neutral / Spending groupings (in `analyze_transactions.py`)

The analyzer sorts every category into one of three buckets. These are **hardcoded** near the top of `analyze_transactions.py` and must be updated manually when you add new categories.

```python
INCOME_CATS  = {"wage", "reimbursement", "treasury_interest"}
NEUTRAL_CATS = {"transfer_internal", "credit_card_payment"}
# Everything else is treated as spending
```

| Bucket | Behavior |
|---|---|
| **income** | Counted toward `INCOME_TOTAL` (positive values) |
| **neutral** | Excluded from both totals — moves between your own accounts, credit card payments |
| **spending** | Counted toward `SPENDING_TOTAL` (typically negative values) |

**To add a new income category (e.g. `rental_income`):**

1. Add it to the `categories` list in `params.yaml`.
2. Optionally add keyword hints.
3. In `analyze_transactions.py`, add it to `INCOME_CATS`:
   ```python
   INCOME_CATS = {"wage", "reimbursement", "treasury_interest", "rental_income"}
   ```

---

### 4. LLM classification rules (in `classify_transactions.py`)

For unusual or ambiguous transactions, the model follows a set of explicit rules baked into its system prompt. If you add a new category that requires special disambiguation logic, add a rule to the `SYSTEM_PROMPT` string near line 87 of `classify_transactions.py`:

```python
SYSTEM_PROMPT = """...
- Rental income deposits = "rental_income".
..."""
```

---

## Full `params.yaml` reference

```yaml
# Model and batching
model: "claude-haiku-4-5-20251001"   # Anthropic model to use
batch_size: 40                        # Rows sent per API call (lower = safer, higher = faster)
max_retries: 4                        # Retry attempts on transient API errors
temperature: 0.0                      # 0.0 = deterministic; raise slightly for more varied output

# Required: list of categories the model may assign
categories:
  - wage
  - misc                              # catch-all; keep this

# Optional: local keyword hints (case-insensitive substrings)
keyword_hints:
  wage:
    - "PAYROLL"

# Column names the script will look for (normalized to lowercase)
possible_columns:
  - description
  - amount
  - type
  - transaction date
  - post date
  - balance
  - memo
```

---

## Project structure

```
Financial_coding_project/
├── Input/                        # Raw Chase CSV exports (not modified)
├── Output/                       # Classified CSVs + summary.csv
├── src/
│   ├── params.yaml               # All configuration (categories, hints, model, system prompt)
│   ├── classify_transactions.py  # Step 1: LLM classifier
│   ├── analyze_transactions.py   # Step 2: income/spending summary
│   └── reconcile.py              # Step 3: reconcile credit cards vs bank
└── README.md
```
