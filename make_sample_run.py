#!/usr/bin/env python
"""Build a LABELED dashboard test fixture under runs_sample/run_demo/.

This is NOT a real SIA run and NOT a demo deliverable. It exists so the dashboard
can be exercised offline before a real `sia run` produces runs/run_1/. The
accuracy numbers are GENUINE (each generation's predictions are scored by the real
evaluate.py); only improvement.md is placeholder narrative, clearly marked.

Each generation k uses a monotonically growing prefix of (sorted) question ids as
"correct" (predicted = gold) and renders the rest as a plausible-but-wrong query
(`SELECT * FROM <first table>`), so the before/after playground has real material.

Usage:  python make_sample_run.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
TASK_DIR = REPO / "tasks" / "text-to-sql"
EVAL_PY = TASK_DIR / "data" / "public" / "evaluate.py"
PUBLIC = TASK_DIR / "data" / "public"
GOLD = TASK_DIR / "data" / "private" / "test_gold.jsonl"
OUT = REPO / "runs_sample" / "run_demo"

RATES = [0.30, 0.42, 0.55, 0.68, 0.78, 0.85]

IMPROVEMENTS = {
    2: ("Schema linking", "Gen 1 dumped the entire schema for every database. The "
        "feedback agent observed many `no-such-column`/`no-such-table` errors and added a "
        "**schema-linking** step: select only the tables/columns relevant to the question "
        "before generating SQL."),
    3: ("Few-shot from train pool", "Persistent join mistakes. The agent began retrieving "
        "a few **same-database examples from `train.jsonl`** and putting them in the prompt as "
        "worked examples."),
    4: ("Execute-and-repair", "Some queries still errored at execution. The agent added an "
        "**execute-and-repair loop**: run the predicted SQL read-only, and if it errors, feed "
        "the error back to the model for one repair attempt."),
    5: ("Robust SQL extraction", "A chunk of failures were format-only (markdown fences, prose). "
        "The agent hardened **SQL extraction** to strip code fences and keep only the statement."),
    6: ("Self-consistency", "For ambiguous questions the agent now samples a few candidates and "
        "**votes by execution result**, keeping the majority answer."),
}


def read_jsonl(p: Path):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def first_table(ddl: str) -> str:
    m = re.search(r'CREATE TABLE\s+"?([A-Za-z_][\w]*)"?', ddl or "", re.IGNORECASE)
    return m.group(1) if m else "sqlite_master"


def main() -> None:
    questions = read_jsonl(PUBLIC / "test_questions.jsonl")
    gold = {g["id"]: g["query"] for g in read_jsonl(GOLD)}
    schemas = json.loads((PUBLIC / "schemas.json").read_text(encoding="utf-8"))
    wrong_for_db = {db: f"SELECT * FROM {first_table(ddl)}" for db, ddl in schemas.items()}

    ordered_ids = sorted(q["id"] for q in questions)
    qdb = {q["id"]: q["db_id"] for q in questions}

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT.parent / "FIXTURE.txt").write_text(
        "This directory holds a LABELED dashboard test fixture, not a real SIA run.\n"
        "Scores are genuine (scored by evaluate.py); improvement.md is placeholder narrative.\n",
        encoding="utf-8")

    for k, rate in enumerate(RATES, start=1):
        gen_dir = OUT / f"gen_{k}"
        gen_dir.mkdir(parents=True, exist_ok=True)
        n_correct = int(round(rate * len(ordered_ids)))
        correct = set(ordered_ids[:n_correct])

        preds = []
        for qid in ordered_ids:
            sql = gold[qid] if qid in correct else wrong_for_db.get(qdb[qid], "SELECT 1")
            preds.append({"id": qid, "predicted_sql": sql})
        with open(gen_dir / "predictions.jsonl", "w", encoding="utf-8") as f:
            for p in preds:
                f.write(json.dumps(p) + "\n")

        # Genuine scoring via the real verifier.
        subprocess.run([sys.executable, str(EVAL_PY), "--gen-dir", str(gen_dir)], check=True)

        # Placeholder improvement.md (clearly marked).
        if k in IMPROVEMENTS:
            title, body = IMPROVEMENTS[k]
            (gen_dir / "improvement.md").write_text(
                f"> **[SAMPLE PLACEHOLDER — not the Feedback-Agent's real output]**\n\n"
                f"# Generation {k}: {title}\n\n{body}\n", encoding="utf-8")
        (gen_dir / "target_agent.py").write_text(
            f"# [SAMPLE PLACEHOLDER] gen {k} target agent stub\n", encoding="utf-8")

    acc = []
    for k in range(1, len(RATES) + 1):
        r = json.loads((OUT / f"gen_{k}" / "results.json").read_text(encoding="utf-8"))
        acc.append((k, r["accuracy"]))
    print("Sample fixture written to", OUT)
    print("Accuracy by gen:", ", ".join(f"g{k}={a:.2f}" for k, a in acc))


if __name__ == "__main__":
    main()
