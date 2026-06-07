"""Fitness = execution accuracy on Spider. Reuses SIA's read-only scoring.

A genome is turned into per-question prompts (from its sections); the model call is
injected (`generate_fn(system, user) -> raw`) so this runs offline in tests and wires
to Claude/OpenAI later. Reads the existing SIA task data (no duplication of the DBs).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections import Counter
from pathlib import Path
from typing import Callable

from phylo.genome import Genome

# Reuse the SIA task data living one level up (E:\sia\tasks\text-to-sql).
SIA_ROOT = Path(__file__).resolve().parents[2]
PUBLIC = SIA_ROOT / "tasks" / "text-to-sql" / "data" / "public"
PRIVATE = SIA_ROOT / "tasks" / "text-to-sql" / "data" / "private"
DB_ROOT = PUBLIC / "databases"

GenerateFn = Callable[[str, str], str]  # (system, user) -> raw model text

ALLOWED = ("select", "with")
QUERY_TIMEOUT = 5.0
ROW_CAP = 100_000


# ── SQL safety + execution (copied from SIA evaluate.py) ─────────────────────

def is_read_only(sql: str) -> bool:
    if not sql or not sql.strip():
        return False
    s = sql.strip().rstrip(";").strip().lower()
    if ";" in s:
        return False
    return s.startswith(ALLOWED)


def run_sql(db_path: Path, sql: str, timeout: float = QUERY_TIMEOUT):
    if not db_path.exists():
        return None, f"db-not-found:{db_path.name}"
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=timeout)
    except sqlite3.Error as e:
        return None, f"connect-error:{e}"
    timer = threading.Timer(timeout, con.interrupt)
    try:
        timer.start()
        rows = con.execute(sql).fetchmany(ROW_CAP)
        return rows, None
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        return None, "timeout" if "interrupted" in msg.lower() else msg
    finally:
        timer.cancel()
        con.close()


def normalize(rows) -> list:
    return sorted(tuple(str(c) for c in r) for r in (rows or []))


def extract_sql(raw: str) -> str:
    """Light extraction: strip code fences / a leading 'sql' label, keep one statement."""
    if not raw:
        return ""
    t = raw.strip()
    if "```" in t:
        m = t.split("```")
        # take the longest fenced block
        blocks = [b for i, b in enumerate(m) if i % 2 == 1]
        if blocks:
            t = max(blocks, key=len)
        t = t.strip()
        if t[:3].lower() == "sql":
            t = t[3:].strip()
    return t.strip()


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def load_task():
    questions = read_jsonl(PUBLIC / "test_questions.jsonl")
    schemas = json.loads((PUBLIC / "schemas.json").read_text(encoding="utf-8"))
    gold = {r["id"]: r["query"] for r in read_jsonl(PRIVATE / "test_gold.jsonl")}
    return questions, schemas, gold


def build_user_prompt(genome: Genome, schema: str, question: str) -> str:
    return (f"{genome.meta_instructions}\n\nDatabase schema:\n{schema}\n\n"
            f"Question: {question}\n\n{genome.output_format}")


def score_one(db_path: Path, pred_sql: str, gold_sql: str):
    if not is_read_only(pred_sql):
        return False, "rejected-or-empty"
    pred_rows, err = run_sql(db_path, pred_sql)
    if err:
        return False, err
    gold_rows, _ = run_sql(db_path, gold_sql)
    return normalize(pred_rows) == normalize(gold_rows), None


def _eval_one(genome: Genome, q: dict, schemas, gold, generate_fn: GenerateFn) -> dict:
    schema = schemas.get(q["db_id"], "")
    try:
        raw = generate_fn(genome.system_prompt, build_user_prompt(genome, schema, q["question"]))
        sql = extract_sql(raw)
    except Exception as e:  # noqa: BLE001 - a model error on one question is just a miss
        sql = ""
    db_path = DB_ROOT / q["db_id"] / f"{q['db_id']}.sqlite"
    correct, err = score_one(db_path, sql, gold[q["id"]])
    return {"id": q["id"], "db_id": q["db_id"], "question": q["question"],
            "predicted_sql": sql, "error": err, "correct": bool(correct)}


def evaluate_genome(genome: Genome, questions, schemas, gold, generate_fn: GenerateFn,
                    limit: int | None = None, workers: int = 1) -> tuple[float, list[dict]]:
    """Run the genome over the questions; return (execution_accuracy, traces).

    `workers` parallelizes the (I/O-bound) model calls across threads — each SQLite
    read opens its own read-only connection, so concurrent scoring is safe.
    """
    items = questions[:limit] if limit else questions
    if workers and workers > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as ex:
            traces = list(ex.map(lambda q: _eval_one(genome, q, schemas, gold, generate_fn), items))
    else:
        traces = [_eval_one(genome, q, schemas, gold, generate_fn) for q in items]
    n_correct = sum(t["correct"] for t in traces)
    fitness = n_correct / len(items) if items else 0.0
    return fitness, traces


def failure_summary(traces: list[dict], max_samples: int = 12) -> str:
    """Compact, model-readable summary of failures to drive mutation."""
    fails = [t for t in traces if not t["correct"]]
    if not fails:
        return "No failures."
    hist = Counter(t["error"] or "wrong-result" for t in fails)
    lines = [f"{len(fails)}/{len(traces)} wrong. Error types: " + ", ".join(f"{k}:{v}" for k, v in hist.most_common())]
    for t in fails[:max_samples]:
        lines.append(f"- Q: {t['question'][:120]} | pred: {t['predicted_sql'][:160]} | err: {t['error']}")
    return "\n".join(lines)
