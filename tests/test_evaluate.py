#!/usr/bin/env python
"""Unit + integration tests for the text-to-SQL verifier (evaluate.py).

Run: python tests/test_evaluate.py
Exits non-zero on any failure.
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
EVAL_PY = TASK_DIR / "data" / "public" / "evaluate.py"
GOLD = TASK_DIR / "data" / "private" / "test_gold.jsonl"
QUESTIONS = TASK_DIR / "data" / "public" / "test_questions.jsonl"
DB_ROOT = TASK_DIR / "data" / "public" / "databases"

# Import evaluate.py as a module for unit tests.
spec = importlib.util.spec_from_file_location("evaluate_mod", EVAL_PY)
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)

PASS, FAIL = 0, 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


def read_jsonl(p: Path):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def run_eval(gen_dir: Path) -> dict:
    r = subprocess.run([sys.executable, str(EVAL_PY), "--gen-dir", str(gen_dir)],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"evaluate.py exited {r.returncode}: {r.stderr}"
    return json.loads((gen_dir / "results.json").read_text(encoding="utf-8"))


def main() -> None:
    gold = read_jsonl(GOLD)
    qmeta = {q["id"]: q for q in read_jsonl(QUESTIONS)}

    # ── Unit: is_read_only ───────────────────────────────────────────────────
    check("is_read_only accepts SELECT", ev.is_read_only("SELECT * FROM t"))
    check("is_read_only accepts WITH", ev.is_read_only("WITH x AS (SELECT 1) SELECT * FROM x"))
    check("is_read_only trailing semicolon ok", ev.is_read_only("SELECT 1;"))
    check("is_read_only rejects DROP", not ev.is_read_only("DROP TABLE t"))
    check("is_read_only rejects UPDATE", not ev.is_read_only("UPDATE t SET a=1"))
    check("is_read_only rejects multi-stmt", not ev.is_read_only("SELECT 1; DROP TABLE t"))
    check("is_read_only rejects empty", not ev.is_read_only("   "))
    check("is_read_only rejects code-fence", not ev.is_read_only("```sql\nSELECT 1\n```"))

    # ── Unit: run_sql read-only + classify ───────────────────────────────────
    db = DB_ROOT / "concert_singer" / "concert_singer.sqlite"
    rows, err = ev.run_sql(db, "SELECT count(*) FROM singer")
    check("run_sql reads", err is None and rows is not None, str(err))
    _, err2 = ev.run_sql(db, "SELECT * FROM no_such_table_xyz")
    check("run_sql surfaces missing table", err2 is not None and "no such table" in err2.lower(), str(err2))
    check("classify no-such-table", ev.classify_error(err2) == "no-such-table", ev.classify_error(err2))

    # Read-only connection must reject a write even if it slips past the allowlist.
    _, werr = ev.run_sql(db, "CREATE TABLE hax(x)")  # not a SELECT, but prove engine is RO too
    check("run_sql cannot write (readonly db)", werr is not None, str(werr))

    # ── Unit: timeout path ───────────────────────────────────────────────────
    slow = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 100000000) "
            "SELECT count(*) FROM c")
    _, terr = ev.run_sql(db, slow, timeout=1.0)
    check("run_sql times out on heavy query", terr == "timeout", str(terr))

    # ── Integration A: perfect predictions => 100% ───────────────────────────
    with tempfile.TemporaryDirectory() as td:
        gen = Path(td)
        (gen / "predictions.jsonl").write_text(
            "\n".join(json.dumps({"id": g["id"], "predicted_sql": g["query"]}) for g in gold),
            encoding="utf-8")
        res = run_eval(gen)
        check("perfect predictions => accuracy 1.0", res["accuracy"] == 1.0,
              f"acc={res['accuracy']} errs={res.get('error_summary')}")
        check("n_total == 120", res["n_total"] == len(gold), str(res["n_total"]))
        check("details length matches", len(res["details"]) == len(gold))

    # ── Integration B: half gold, half DROP => ~0.5 + rejections, DB untouched ─
    sample_db = DB_ROOT / "pets_1" / "pets_1.sqlite"
    before = sample_db.stat().st_size, sample_db.stat().st_mtime
    with tempfile.TemporaryDirectory() as td:
        gen = Path(td)
        recs = []
        for i, g in enumerate(gold):
            if i % 2 == 0:
                recs.append({"id": g["id"], "predicted_sql": g["query"]})
            else:
                recs.append({"id": g["id"], "predicted_sql": "DROP TABLE Student"})
        (gen / "predictions.jsonl").write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")
        res = run_eval(gen)
        check("mixed => accuracy between 0.4 and 0.6", 0.4 <= res["accuracy"] <= 0.6, str(res["accuracy"]))
        check("DROP => rejected-not-select recorded", res["error_summary"].get("rejected-not-select", 0) > 0,
              str(res["error_summary"]))
    after = sample_db.stat().st_size, sample_db.stat().st_mtime
    check("DROP did not modify DB file", before == after, f"{before} != {after}")

    # ── Integration C: no submission => accuracy 0 + submission_error ─────────
    with tempfile.TemporaryDirectory() as td:
        gen = Path(td)
        res = run_eval(gen)
        check("no submission => accuracy 0.0", res["accuracy"] == 0.0, str(res["accuracy"]))
        check("no submission => submission_error present", "submission_error" in res)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
