#!/usr/bin/env python
"""SIA verifier for the text-to-SQL task: execution accuracy.

Contract (see ../../INTERFACE_NOTES.md §4): SIA's orchestrator runs

    <venv-python> evaluate.py --gen-dir <runs/run_X/gen_N>

This script must (a) find the agent's submission inside --gen-dir, (b) score it
against the private gold queries, and (c) write <gen-dir>/results.json. The
orchestrator injects results.json *in full* into the feedback-agent prompt, so
this is the highest-signal channel for the agent to diagnose its own failures —
we therefore pack results.json with per-question detail + an error histogram +
a sample of concrete failures.

ANTI-GOODHART: gold lives only in data/private/, which is NEVER passed to the agent
(its --dataset_dir is data/public). This file lives in data/public/ because that is
the only place SIA's harness searches for it (run_evaluation is invoked with the
dataset dir as its search root — see INTERFACE_NOTES.md §4). task.md instructs the
agent to read only its dataset_dir; in docker sandbox mode data/private is not even
mounted, making the gold structurally unreachable.

Scoring: predicted SQL and gold SQL are executed read-only on the same SQLite DB
and their result sets compared order-insensitively (a pragmatic approximation of
Spider's official test-suite accuracy; it does not handle column-permutation
equivalence — noted as a known caveat). Anything rejected/errored/timed-out = 0.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
from collections import Counter
from pathlib import Path

# evaluate.py sits at <task_dir>/data/public/evaluate.py (where SIA's harness finds it).
PUBLIC_DIR = Path(__file__).resolve().parent
TASK_DIR = PUBLIC_DIR.parent.parent
PRIVATE_DIR = TASK_DIR / "data" / "private"
DB_ROOT = PUBLIC_DIR / "databases"

GOLD_PATH = PRIVATE_DIR / "test_gold.jsonl"
QUESTIONS_PATH = PUBLIC_DIR / "test_questions.jsonl"

SUBMISSION_CANDIDATES = ("predictions.jsonl", "predictions.json")

ALLOWED_PREFIXES = ("select", "with")
ROW_CAP = 100_000          # guard against runaway cartesian products
QUERY_TIMEOUT = 5.0        # seconds per query
MAX_FAILURE_SAMPLES = 20   # concrete failures embedded for the feedback agent


# ── SQL safety + execution ──────────────────────────────────────────────────

def is_read_only(sql: str) -> bool:
    """True only for a single SELECT/WITH statement (no mutations, no multi-stmt)."""
    if not sql or not sql.strip():
        return False
    s = sql.strip().rstrip(";").strip().lower()
    if ";" in s:  # reject anything that smells like multiple statements
        return False
    return s.startswith(ALLOWED_PREFIXES)


def run_sql(db_path: Path, sql: str, timeout: float = QUERY_TIMEOUT):
    """Execute read-only with a hard timeout. Returns (rows, error_str).

    Uses con.interrupt() fired from a timer thread so a slow/looping query is
    cancelled cleanly in the calling thread rather than abandoned.
    """
    if not db_path.exists():
        return None, f"db-not-found:{db_path.name}"
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=timeout)
    except sqlite3.Error as e:
        return None, f"connect-error:{e}"

    timer = threading.Timer(timeout, con.interrupt)
    try:
        timer.start()
        cur = con.execute(sql)
        rows = cur.fetchmany(ROW_CAP)
        return rows, None
    except Exception as e:  # noqa: BLE001 - report any execution failure verbatim
        msg = str(e)
        if "interrupted" in msg.lower():
            return None, "timeout"
        return None, msg
    finally:
        timer.cancel()
        con.close()


def normalize(rows) -> list:
    """Order-insensitive multiset; stringify cells for type robustness."""
    return sorted(tuple(str(c) for c in row) for row in (rows or []))


def classify_error(err: str | None) -> str:
    """Bucket an execution error into a category the feedback agent can act on."""
    if err is None:
        return "wrong-result"
    e = err.lower()
    if e.startswith("rejected"):
        return "rejected-not-select"
    if e == "timeout":
        return "timeout"
    if e.startswith("empty"):
        return "empty-prediction"
    if "no such column" in e:
        return "no-such-column"
    if "no such table" in e:
        return "no-such-table"
    if "no such function" in e:
        return "no-such-function"
    if "syntax error" in e or "near " in e:
        return "syntax-error"
    if "ambiguous column" in e:
        return "ambiguous-column"
    if e.startswith("db-not-found"):
        return "db-not-found"
    return "other-error"


def score_one(db_path: Path, pred_sql: str, gold_sql: str):
    """Return (correct: bool, pred_error: str | None)."""
    if not pred_sql or not pred_sql.strip():
        return False, "empty-prediction"
    if not is_read_only(pred_sql):
        return False, "rejected-not-select"
    pred_rows, pred_err = run_sql(db_path, pred_sql)
    if pred_err:
        return False, pred_err
    gold_rows, gold_err = run_sql(db_path, gold_sql)
    if gold_err:
        # Should not happen (gold is sanity-checked in prep), but surface it.
        return False, f"gold-error:{gold_err}"
    return normalize(pred_rows) == normalize(gold_rows), None


# ── IO helpers ──────────────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _find_submission(gen_dir: Path) -> Path | None:
    for name in SUBMISSION_CANDIDATES:
        p = gen_dir / name
        if p.exists():
            return p
    return None


def _load_predictions(path: Path) -> dict[str, str]:
    """Map id -> predicted_sql. Tolerant of jsonl or a single json array."""
    text = path.read_text(encoding="utf-8").strip()
    records: list[dict] = []
    if text.startswith("["):
        records = json.loads(text)
    else:
        for line in text.splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
    preds: dict[str, str] = {}
    for r in records:
        rid = str(r.get("id"))
        preds[rid] = r.get("predicted_sql") or r.get("sql") or r.get("query") or ""
    return preds


# ── Main scoring ────────────────────────────────────────────────────────────

def evaluate(submission_path: Path) -> dict:
    """Score a submission file against the private gold set. Returns the results dict."""
    gold = {str(r["id"]): r["query"] for r in _read_jsonl(GOLD_PATH)}
    qmeta = {str(r["id"]): r for r in _read_jsonl(QUESTIONS_PATH)}
    preds = _load_predictions(submission_path) if submission_path else {}

    details = []
    error_hist: Counter = Counter()
    n_correct = 0

    for rid, gold_sql in gold.items():
        meta = qmeta.get(rid, {})
        db_id = meta.get("db_id", "")
        question = meta.get("question", "")
        db_path = DB_ROOT / db_id / f"{db_id}.sqlite"
        pred_sql = preds.get(rid, "")

        correct, pred_err = score_one(db_path, pred_sql, gold_sql)
        if correct:
            n_correct += 1
        else:
            error_hist[classify_error(pred_err)] += 1

        details.append(
            {
                "id": rid,
                "db_id": db_id,
                "question": question,
                "predicted_sql": pred_sql,
                "gold_sql": gold_sql,
                "pred_error": pred_err,
                "correct": bool(correct),
            }
        )

    n_total = len(gold)
    accuracy = n_correct / n_total if n_total else 0.0

    # A compact sample of concrete failures for the feedback agent to learn from.
    failure_samples = [
        {
            "id": d["id"],
            "db_id": d["db_id"],
            "question": d["question"],
            "predicted_sql": d["predicted_sql"][:500],
            "pred_error": d["pred_error"],
            "gold_sql": d["gold_sql"][:500],
        }
        for d in details
        if not d["correct"]
    ][:MAX_FAILURE_SAMPLES]

    results = {
        "accuracy": round(accuracy, 4),
        "n_correct": n_correct,
        "n_total": n_total,
        "n_predicted": len(preds),
        "error_summary": dict(error_hist.most_common()),
        "failure_samples": failure_samples,
        "details": details,
    }
    if not preds:
        results["submission_error"] = (
            "No predictions found in the generation dir. The agent must write "
            "predictions.jsonl ({\"id\": ..., \"predicted_sql\": ...} per line) "
            "into its --working_dir."
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Execution-accuracy verifier for text-to-SQL.")
    parser.add_argument("--gen-dir", type=Path, required=True, help="Generation dir holding the submission.")
    args = parser.parse_args()

    gen_dir = args.gen_dir
    submission = _find_submission(gen_dir)
    if submission is None:
        print(f"[evaluate] No submission ({'/'.join(SUBMISSION_CANDIDATES)}) in {gen_dir}")

    results = evaluate(submission)

    results_path = gen_dir / "results.json"
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"[evaluate] accuracy={results['accuracy']:.4f} "
          f"({results['n_correct']}/{results['n_total']})  errors={results['error_summary']}")
    print(f"[evaluate] wrote {results_path}")
    # Always exit 0 so the score/diagnostics reach the feedback agent even on a
    # broken submission (orchestrator treats a non-zero exit as an eval failure).
    sys.exit(0)


if __name__ == "__main__":
    main()
