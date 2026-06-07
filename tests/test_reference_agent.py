#!/usr/bin/env python
"""Offline wiring test for the reference target agent.

No API key needed: we inject a fake model into predict_all() and drive the full
agent -> predictions.jsonl -> evaluate.py -> results.json pipeline.

Run: python tests/test_reference_agent.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASK_DIR = ROOT / "tasks" / "text-to-sql"
AGENT_PY = TASK_DIR / "reference" / "reference_target_agent.py"
EVAL_PY = TASK_DIR / "data" / "public" / "evaluate.py"
PUBLIC = TASK_DIR / "data" / "public"
GOLD = TASK_DIR / "data" / "private" / "test_gold.jsonl"

spec = importlib.util.spec_from_file_location("ref_agent", AGENT_PY)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  PASS  {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name}  {extra}")


def read_jsonl(p):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def run_eval(gen_dir):
    r = subprocess.run([sys.executable, str(EVAL_PY), "--gen-dir", str(gen_dir)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads((gen_dir / "results.json").read_text(encoding="utf-8"))


def main():
    questions = read_jsonl(PUBLIC / "test_questions.jsonl")
    schemas = json.loads((PUBLIC / "schemas.json").read_text(encoding="utf-8"))
    gold = {g["id"]: g["query"] for g in read_jsonl(GOLD)}
    q_by_text = {q["question"]: q["id"] for q in questions}

    # An "oracle" fake model: recover the question from the prompt, return its gold.
    def oracle(prompt: str) -> str:
        line = next(l for l in prompt.splitlines() if l.startswith("Question: "))
        qid = q_by_text[line[len("Question: "):]]
        return gold[qid]

    # A "naive" fake model: always returns a trivially valid but wrong query.
    def naive(prompt: str) -> str:
        return "SELECT 1"

    # ── Oracle path => predictions wired correctly => 100% through real evaluate ──
    with tempfile.TemporaryDirectory() as td:
        gen = Path(td)
        preds, log = agent.predict_all(questions, schemas, generate=oracle)
        with open(gen / "predictions.jsonl", "w", encoding="utf-8") as f:
            for p in preds:
                f.write(json.dumps(p) + "\n")
        check("oracle: 120 predictions", len(preds) == len(questions), str(len(preds)))
        check("oracle: execution log per question", len(log) == len(questions))
        res = run_eval(gen)
        check("oracle: accuracy 1.0 end-to-end", res["accuracy"] == 1.0,
              f"acc={res['accuracy']} errs={res.get('error_summary')}")

    # ── Naive path => pipeline still runs, low score ─────────────────────────
    with tempfile.TemporaryDirectory() as td:
        gen = Path(td)
        preds, _ = agent.predict_all(questions, schemas, generate=naive)
        with open(gen / "predictions.jsonl", "w", encoding="utf-8") as f:
            for p in preds:
                f.write(json.dumps(p) + "\n")
        res = run_eval(gen)
        check("naive: pipeline scores all 120", res["n_total"] == len(questions))
        check("naive: accuracy is low", res["accuracy"] < 0.2, str(res["accuracy"]))

    # ── build_prompt / extract_sql units ─────────────────────────────────────
    check("build_prompt includes schema + question",
          "Question: hi" in agent.build_prompt("hi", "CREATE TABLE t(x)"))
    check("extract_sql trims", agent.extract_sql("  SELECT 1  ") == "SELECT 1")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
