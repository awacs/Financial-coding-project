import os
import re
import json
import time
import argparse
from typing import List, Dict, Any, Tuple

import yaml

try:
    import pandas as pd
except ImportError:
    raise SystemExit("Please: pip install pandas pyyaml anthropic")

from anthropic import Anthropic
client = Anthropic()

# ---------- Helpers ----------

def read_params(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def smart_read_table(path: str) -> pd.DataFrame:
    """
    Reads CSV/TSV or a plain text file containing tab/space separated lines.
    If parsing fails, returns a single 'raw' column.
    Normalizes column names to lowercase + stripped for consistent matching.
    """
    for sep in [",", "\t", r"\s{2,}", r"\s+"]:
        try:
            df = pd.read_csv(path, sep=sep, engine="python", index_col=False)
            if df.shape[1] >= 2:
                # Normalize column names: lowercase and strip whitespace
                df.columns = [c.strip().lower() for c in df.columns]
                return df
        except Exception:
            pass

    # Fallback: read as text into a single column
    with open(path, "r", encoding="utf-8") as f:
        lines = [line.rstrip("\n") for line in f]
    return pd.DataFrame({"raw": lines})

def ensure_string(x) -> str:
    if pd.isna(x):
        return ""
    return str(x)

def row_to_compact_text(row: pd.Series, col_order: List[str]) -> str:
    if "raw" in row:
        return ensure_string(row["raw"])
    parts = []
    for c in col_order:
        if c in row:
            parts.append(ensure_string(row[c]))
    if not parts:
        parts = [ensure_string(v) for v in row.astype(str).tolist()]
    return " | ".join(parts)

def apply_keyword_hints(text: str, hints: Dict[str, List[str]]) -> Tuple[str, float]:
    """
    Very light local heuristic. Returns (suggested_category, confidence) or ("", 0.0).
    """
    up = text.upper()
    best_cat, best_hits = "", 0
    for cat, keys in hints.items():
        hits = sum(1 for k in keys if k.upper() in up)
        if hits > best_hits:
            best_hits = hits
            best_cat = cat
    if best_hits > 0:
        # confidence is weak; LLM will still arbitrate
        conf = min(0.60, 0.35 + 0.10 * best_hits)
        return best_cat, conf
    return "", 0.0

def chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

def backoff_sleep(i):
    time.sleep(min(30, 2 ** i + (0.2 * i)))

# ---------- LLM Prompting ----------


def make_user_prompt(items: List[Dict[str, Any]], categories: List[str]) -> str:
    lines = [f"CATEGORIES = {categories}"]
    lines.append("TRANSACTIONS = [")
    for it in items:
        line = json.dumps({"id": it["id"], "text": it["text"], "hint": it.get("hint", "")}, ensure_ascii=False)
        lines.append(f"  {line},")
    lines.append("]")
    lines.append("Return a JSON list of results aligned by order; ignore the 'id' key in output.")
    return "\n".join(lines)

def call_llm(model: str, system_prompt: str, messages: List[Dict[str, str]], temperature: float, max_retries: int) -> str:
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                messages=messages,
                temperature=temperature,
            )
            return response.content[0].text
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            print(f"  [warn] API error (attempt {attempt+1}/{max_retries}): {e}")
            backoff_sleep(attempt + 1)

def extract_json_list(raw: str) -> list:
    """Parse a JSON list from model output, with regex fallback."""
    # Strip markdown code fences if present
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            if "results" in parsed:
                return parsed["results"]
            # return first list value found
            for v in parsed.values():
                if isinstance(v, list):
                    return v
    except Exception:
        pass

    # Regex fallback: find [...] block in raw text
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if m:
        return json.loads(m.group(0))

    raise ValueError(f"Model returned non-JSON or unexpected shape:\n{raw[:500]}")

# ---------- Main pipeline ----------

def classify_file(input_path: str, output_path: str, params_path: str):
    params = read_params(params_path)

    model         = params.get("model", "claude-haiku-4-5-20251001")
    batch_size    = int(params.get("batch_size", 40))
    max_retries   = int(params.get("max_retries", 4))
    temperature   = float(params.get("temperature", 0.0))
    system_prompt = params["system_prompt"]
    categories    = params["categories"]
    hints         = params.get("keyword_hints", {})
    possible_cols = params.get("possible_columns", ["type", "date", "description", "amount", "method", "balance"])

    # Normalize possible_columns to lowercase for matching against normalized df columns
    possible_cols = [c.strip().lower() for c in possible_cols]

    df = smart_read_table(input_path).copy()
    print(f"Loaded {len(df)} rows from {input_path}")
    print(f"Columns: {list(df.columns)}")

    # Determine preferred column order if present
    present = [c for c in possible_cols if c in df.columns]
    col_order = present if present else list(df.columns)

    # Build payload rows
    payload = []
    for idx, row in df.iterrows():
        text = row_to_compact_text(row, col_order)
        hint_cat, hint_conf = apply_keyword_hints(text, hints)
        payload.append({
            "id": int(idx),
            "text": text,
            "hint": f"hint_category={hint_cat}; hint_conf={hint_conf}" if hint_cat else ""
        })

    results: Dict[int, Dict[str, Any]] = {}
    total_batches = (len(payload) + batch_size - 1) // batch_size
    for batch_num, batch in enumerate(chunked(payload, batch_size), 1):
        print(f"  Classifying batch {batch_num}/{total_batches} ({len(batch)} rows)...")
        user_prompt = make_user_prompt(batch, categories)
        messages = [{"role": "user", "content": user_prompt}]
        raw = call_llm(model, system_prompt, messages, temperature, max_retries)

        out_list = extract_json_list(raw)

        if len(out_list) != len(batch):
            raise ValueError(f"Expected {len(batch)} results but got {len(out_list)}")

        for row_id, res in zip([b["id"] for b in batch], out_list):
            cat = res.get("category", "misc")
            conf = res.get("confidence", 0.0)
            rat = res.get("rationale", "")
            results[row_id] = {"category": cat, "confidence": conf, "rationale": rat}

    # Attach to dataframe
    df["__text_for_model"] = [row_to_compact_text(r, col_order) for _, r in df.iterrows()]
    df["category"]   = df.index.map(lambda i: results[i]["category"])
    df["confidence"] = df.index.map(lambda i: results[i]["confidence"])
    df["rationale"]  = df.index.map(lambda i: results[i]["rationale"])
    df["model"]      = model

    # Save
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    df.to_csv(output_path, index=False)
    print(f"Saved classified file -> {output_path}")

# ---------- CLI ----------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Classify bank transactions with an LLM.")
    ap.add_argument("input", help="Path to CSV/TSV or text file of transactions")
    ap.add_argument("-o", "--output", default=None, help="Output CSV path (default: <input>.classified.csv)")
    ap.add_argument("-p", "--params", default="params.yaml", help="Path to params.yaml")
    args = ap.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ:
        raise SystemExit("Please set ANTHROPIC_API_KEY in your environment.")

    out = args.output or (os.path.splitext(args.input)[0] + ".classified.csv")
    classify_file(args.input, out, args.params)
