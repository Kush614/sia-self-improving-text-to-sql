#!/usr/bin/env python
"""Cold-start text-to-SQL target agent (reference seed for SIA).

DELIBERATELY MINIMAL so the SIA feedback-agent has obvious room to improve. This
seed does the naive thing and nothing more:

  - dumps the FULL schema DDL + the question into one prompt
  - calls the task model EXACTLY ONCE per question
  - extracts SQL crudely (uses the raw model text as-is)
  - no schema-linking, no few-shot from train.jsonl, no execute-and-repair,
    no self-consistency, no column/table validation

It writes predictions.jsonl and a single agent_execution.json into --working_dir.

Contract (SIA): launched as
    python target_agent.py --dataset_dir <DATASET ro> --working_dir <WORK rw>
Only the configured task model may be called.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

# Task model. SIA's meta-agent bakes the configured model in here; overridable via env.
MODEL = os.environ.get("SIA_TASK_MODEL", "claude-haiku-4-5-20251001")
MAX_TOKENS = 1024
MAX_RETRIES = 4

SYSTEM_PROMPT = "You are a text-to-SQL system. Output only a single SQLite SELECT query and nothing else."


def call_model(prompt: str) -> str:
    """Call the task model once and return its raw text. Basic retry/backoff.

    Uses the Anthropic SDK by default. The client also honors ANTHROPIC_BASE_URL,
    so an OpenAI-compatible gateway can be swapped in via env without code changes.
    """
    import anthropic

    client = anthropic.Anthropic()
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        except Exception as e:  # noqa: BLE001 - transient API errors: back off and retry
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"model call failed after {MAX_RETRIES} attempts: {last_err}")


def build_prompt(question: str, schema_ddl: str) -> str:
    """Naive prompt: entire schema + the question. (No schema-linking on purpose.)"""
    return (
        f"Database schema:\n{schema_ddl}\n\n"
        f"Question: {question}\n\n"
        "Return one SQLite SELECT query that answers the question."
    )


def extract_sql(raw: str) -> str:
    """Crude extraction: use the raw model output as-is (just trimmed)."""
    return raw.strip()


def predict_all(questions: list[dict], schemas: dict[str, str], generate=call_model) -> tuple[list[dict], list[dict]]:
    """Return (predictions, execution_log). `generate` is injectable for testing."""
    predictions: list[dict] = []
    log: list[dict] = []
    total = len(questions)
    for i, q in enumerate(questions):
        schema_ddl = schemas.get(q["db_id"], "")
        prompt = build_prompt(q["question"], schema_ddl)
        try:
            raw = generate(prompt)
            sql = extract_sql(raw)
        except Exception as e:  # noqa: BLE001
            raw, sql = f"[error] {e}", ""
        predictions.append({"id": q["id"], "predicted_sql": sql})
        log.append({
            "id": q["id"],
            "db_id": q["db_id"],
            "question": q["question"],
            "prompt_excerpt": prompt[:300],
            "raw_output": raw[:1000],
            "predicted_sql": sql,
        })
        print(f"[{i + 1}/{total}] {q['id']}: {sql[:80]}")
    return predictions, log


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset_dir", required=True)
    ap.add_argument("--working_dir", required=True)
    args = ap.parse_args()

    dataset = Path(args.dataset_dir)
    work = Path(args.working_dir)
    work.mkdir(parents=True, exist_ok=True)

    questions = read_jsonl(dataset / "test_questions.jsonl")
    schemas = json.loads((dataset / "schemas.json").read_text(encoding="utf-8"))
    print(f"Loaded {len(questions)} questions across {len(schemas)} databases. Model={MODEL}")

    predictions, log = predict_all(questions, schemas)

    with open(work / "predictions.jsonl", "w", encoding="utf-8") as f:
        for p in predictions:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    with open(work / "agent_execution.json", "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(predictions)} predictions to {work / 'predictions.jsonl'}")


if __name__ == "__main__":
    main()
