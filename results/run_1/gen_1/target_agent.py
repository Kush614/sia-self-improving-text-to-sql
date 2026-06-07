#!/usr/bin/env python
"""Enhanced text-to-SQL target agent with few-shot examples and execute-and-repair.

Strategy:
  - Loads training examples from train.jsonl for few-shot prompting (same db)
  - Calls claude-haiku-4-5-20251001 with schema + few-shot + question
  - Executes generated SQL; if it errors, sends the error back for a repair pass
  - Extracts SQL robustly from markdown code blocks
  - Logs each question's trajectory to agent_execution/execution_q{i}.json

Contract: launched as
    python target_agent.py --dataset_dir <DATASET ro> --working_dir <WORK rw>
Only the configured task model may be called.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from pathlib import Path

MODEL = os.environ.get("SIA_TASK_MODEL", "claude-haiku-4-5-20251001")
MAX_TOKENS = 1024
MAX_RETRIES = 4
MAX_REPAIR_ATTEMPTS = 2
MAX_FEW_SHOT = 5  # max training examples per db_id injected as few-shot

SYSTEM_PROMPT = (
    "You are an expert text-to-SQL system specialised in SQLite. "
    "Translate natural-language questions into exactly ONE correct SQLite SELECT query. "
    "Always wrap your final SQL in a ```sql ... ``` code block and output nothing else. "
    "Never use INSERT, UPDATE, DELETE, DROP, CREATE, PRAGMA or ATTACH. "
    "Do not output multiple semicolon-separated statements."
)


# ---------------------------------------------------------------------------
# Model call
# ---------------------------------------------------------------------------

def call_model(messages: list[dict]) -> tuple[str, list[dict]]:
    """Call the task model with the given message list.

    Returns (response_text, updated_messages_including_assistant_turn).
    Retries with exponential backoff on transient errors.
    """
    import anthropic

    client = anthropic.Anthropic()
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=messages,
            )
            text = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            )
            updated = messages + [{"role": "assistant", "content": text}]
            return text, updated
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(2**attempt)
    raise RuntimeError(
        f"model call failed after {MAX_RETRIES} attempts: {last_err}"
    )


# ---------------------------------------------------------------------------
# SQL extraction
# ---------------------------------------------------------------------------

def extract_sql(raw: str) -> str:
    """Extract the SQL query from model output.

    Tries (in order):
    1. ```sql ... ``` fenced block
    2. ``` ... ``` fenced block (no language tag)
    3. First line/block starting with SELECT or WITH
    4. Raw text (trimmed)
    """
    # Fenced block with optional sql tag
    m = re.search(r"```(?:sql)?\s*(.*?)\s*```", raw, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # Look for SELECT / WITH keyword at line start
    lines = raw.strip().splitlines()
    sql_lines: list[str] = []
    collecting = False
    for line in lines:
        stripped = line.strip().upper()
        if not collecting and (stripped.startswith("SELECT") or stripped.startswith("WITH")):
            collecting = True
        if collecting:
            sql_lines.append(line)
    if sql_lines:
        return "\n".join(sql_lines).strip()

    return raw.strip()


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def try_execute_sql(db_path: Path, sql: str) -> tuple[bool, str]:
    """Execute *sql* read-only against *db_path*.

    Returns (success, error_message).
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")
        cursor = conn.execute(sql)
        cursor.fetchall()
        conn.close()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def get_sample_rows(db_path: Path, schema_ddl: str, n: int = 3) -> str:
    """Return a compact string of sample rows for each table, for value-format hints."""
    samples: list[str] = []
    try:
        # Extract table names from DDL
        table_names = re.findall(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`]?(\w+)[\"'`]?",
                                 schema_ddl, re.IGNORECASE)
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        for tbl in table_names[:8]:  # limit to 8 tables max
            try:
                cur = conn.execute(f"SELECT * FROM \"{tbl}\" LIMIT {n}")
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                if rows:
                    samples.append(f"-- Sample rows from {tbl}: {cols}")
                    for row in rows:
                        samples.append(f"--   {row}")
            except Exception:  # noqa: BLE001
                pass
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(samples)


# ---------------------------------------------------------------------------
# Few-shot retrieval
# ---------------------------------------------------------------------------

def get_few_shot_examples(db_id: str, train_data: list[dict]) -> list[dict]:
    """Return up to MAX_FEW_SHOT training examples for *db_id*."""
    return [ex for ex in train_data if ex.get("db_id") == db_id][:MAX_FEW_SHOT]


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_initial_prompt(
    question: str,
    schema_ddl: str,
    sample_rows: str,
    few_shot: list[dict],
    dataset_dir: str,
    working_dir: str,
) -> str:
    parts: list[str] = [
        f"Dataset directory (READ-ONLY): {dataset_dir}",
        f"Working directory (READ-WRITE): {working_dir}",
        "",
        "=== DATABASE SCHEMA ===",
        schema_ddl,
    ]

    if sample_rows:
        parts += ["", "=== SAMPLE DATA (for value/format reference) ===", sample_rows]

    if few_shot:
        parts += ["", "=== EXAMPLE QUESTION-SQL PAIRS FOR THIS DATABASE ==="]
        for ex in few_shot:
            parts.append(f"Question: {ex['question']}")
            parts.append(f"SQL:\n```sql\n{ex['query']}\n```")
            parts.append("")

    parts += [
        "=== YOUR TASK ===",
        f"Question: {question}",
        "",
        "Write a single SQLite SELECT query that answers the question above.",
        "Wrap it in ```sql ... ``` and output nothing else.",
    ]

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Per-question prediction
# ---------------------------------------------------------------------------

def predict_one(
    q: dict,
    schema_ddl: str,
    db_path: Path,
    few_shot: list[dict],
    dataset_dir: str,
    working_dir: str,
) -> tuple[dict, list[dict]]:
    """Generate SQL for a single question with optional repair passes.

    Returns (prediction_dict, trajectory_list).
    """
    # The trajectory mirrors the sample execution format
    trajectory: list[dict] = [
        {
            "role": "system",
            "content": [{"type": "text", "text": SYSTEM_PROMPT}],
        }
    ]

    sample_rows = get_sample_rows(db_path, schema_ddl) if db_path.exists() else ""

    initial_prompt = build_initial_prompt(
        question=q["question"],
        schema_ddl=schema_ddl,
        sample_rows=sample_rows,
        few_shot=few_shot,
        dataset_dir=dataset_dir,
        working_dir=working_dir,
    )

    messages: list[dict] = [{"role": "user", "content": initial_prompt}]
    trajectory.append(
        {"role": "user", "content": [{"type": "text", "text": initial_prompt}]}
    )

    sql = ""

    try:
        raw, messages = call_model(messages)
        trajectory.append(
            {"role": "assistant", "content": [{"type": "text", "text": raw}]}
        )
        sql = extract_sql(raw)

        # Execute-and-repair loop
        for _repair in range(MAX_REPAIR_ATTEMPTS):
            if not sql:
                break
            success, error_msg = try_execute_sql(db_path, sql)
            if success:
                break  # query runs without errors

            repair_prompt = (
                f"The SQL query you provided failed with this error:\n"
                f"  {error_msg}\n\n"
                f"Please correct the query so it executes without errors. "
                f"Return only the corrected SQL wrapped in ```sql ... ```."
            )
            messages.append({"role": "user", "content": repair_prompt})
            trajectory.append(
                {"role": "user", "content": [{"type": "text", "text": repair_prompt}]}
            )

            raw, messages = call_model(messages)
            trajectory.append(
                {"role": "assistant", "content": [{"type": "text", "text": raw}]}
            )
            sql = extract_sql(raw)

    except Exception as exc:  # noqa: BLE001
        error_text = f"[error] {exc}"
        trajectory.append(
            {"role": "assistant", "content": [{"type": "text", "text": error_text}]}
        )
        sql = ""

    prediction = {"id": q["id"], "predicted_sql": sql}
    return prediction, trajectory


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def read_jsonl(path: Path) -> list[dict]:
    """Read a .jsonl file, skipping blank lines."""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Text-to-SQL agent using claude-haiku-4-5-20251001"
    )
    ap.add_argument("--dataset_dir", required=True, help="Path to read-only dataset directory")
    ap.add_argument("--working_dir", required=True, help="Path to read-write working directory")
    args = ap.parse_args()

    dataset = Path(args.dataset_dir)
    work = Path(args.working_dir)
    work.mkdir(parents=True, exist_ok=True)

    # Per-question execution logs go into agent_execution/
    exec_dir = work / "agent_execution"
    exec_dir.mkdir(exist_ok=True)

    # Load inputs
    questions = read_jsonl(dataset / "test_questions.jsonl")
    schemas: dict[str, str] = json.loads(
        (dataset / "schemas.json").read_text(encoding="utf-8")
    )

    train_data: list[dict] = []
    train_path = dataset / "train.jsonl"
    if train_path.exists():
        train_data = read_jsonl(train_path)

    print(
        f"Loaded {len(questions)} questions | "
        f"{len(schemas)} schemas | "
        f"{len(train_data)} training examples | "
        f"Model={MODEL}"
    )

    predictions: list[dict] = []
    total = len(questions)

    for i, q in enumerate(questions):
        db_id: str = q["db_id"]
        schema_ddl: str = schemas.get(db_id, "")
        db_path = dataset / "databases" / db_id / f"{db_id}.sqlite"
        few_shot = get_few_shot_examples(db_id, train_data)

        prediction, trajectory = predict_one(
            q=q,
            schema_ddl=schema_ddl,
            db_path=db_path,
            few_shot=few_shot,
            dataset_dir=args.dataset_dir,
            working_dir=args.working_dir,
        )
        predictions.append(prediction)

        # Save trajectory for this question
        exec_file = exec_dir / f"execution_q{i}.json"
        with open(exec_file, "w", encoding="utf-8") as fh:
            json.dump(trajectory, fh, indent=2, ensure_ascii=False)

        print(f"[{i + 1}/{total}] {q['id']} | {prediction['predicted_sql'][:80]}")

    # Write predictions.jsonl
    pred_path = work / "predictions.jsonl"
    with open(pred_path, "w", encoding="utf-8") as fh:
        for p in predictions:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")

    print(f"\nDone. Wrote {len(predictions)} predictions to {pred_path}")
    print(f"Execution logs saved to {exec_dir}/")


if __name__ == "__main__":
    main()
