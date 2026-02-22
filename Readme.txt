# A brief idea on how to organize financials
1. Yearly spending
    a. Tax
    b. Property Tax
    c. DMV Registration fee
2. Monthly
    a. Mortgage
    b. HOA fees
    c. Daycare fees
    d. Harold income
    e. Anqi income
    f. Google fi
    g. Water/power/gas bills
    h. Digital subscription
    i. Reimbursements
    j. Treasury interest
3. Misc income/cost
    a. Investments in/out
    b. Penalties
    c. Travel
    d. Gaming
    e. Luxury
    f. Cash transfer from parents in/out

# Goals:
To see whether we're living beyond our means
Define: Income > spending
Income = 2d + 2e + 2j + 2i
Spending = 1a + 1b + 2c + 2a + 2b + 2c + 2f + 2g + 2h + 3b + 3c + 3d + 3e

# Scope of investigation
March 2023 - Feb 2024

---Code---

## Install deps

    pip install anthropic pandas pyyaml

## Set your API key

    export ANTHROPIC_API_KEY="sk-ant-..."
    # on Windows PowerShell:
    # $env:ANTHROPIC_API_KEY="sk-ant-..."

## Step 1: Classify each account

    python src/classify_transactions.py Input/Chase1600_Activity_20251011.CSV \
        -p src/params.yaml -o Output/Chase1600.classified.csv

    python src/classify_transactions.py Input/Chase4488_Activity20250801_20251001_20251011.CSV \
        -p src/params.yaml -o Output/Chase4488.classified.csv

    python src/classify_transactions.py Input/Chase5650_Activity20250801_20251001_20251011.CSV \
        -p src/params.yaml -o Output/Chase5650.classified.csv

## Step 2: Analyze (all accounts combined)

    python src/analyze_transactions.py Output/*.classified.csv

    # To flag transactions over a custom threshold (e.g. $1000):
    python src/analyze_transactions.py Output/*.classified.csv --flag-threshold 1000

## Output files

The classifier produces:
  Output/<account>.classified.csv  — original rows + category, confidence, rationale, model

The analyzer produces:
  Output/summary.csv               — per-category totals + income/spending/net summary rows

## Category reference

Income (counted toward income total):
  wage               — payroll / salary
  reimbursement      — employer or other reimbursements
  treasury_interest  — APA TREAS / treasury interest payments

Neutral (excluded from income & spending totals):
  transfer_internal  — moves between your own accounts
  credit_card_payment — payments to credit cards

Spending (all other categories):
  tuition, mortgage, rent, utilities, subscriptions, groceries, dining,
  transportation, childcare, insurance, medical, taxes, property_tax,
  hoa_dues, investment, travel, fees, misc
