#!/usr/bin/env python
"""Tests for the dashboard's pure data helpers against the sample fixture.

Run: python tests/test_dashboard.py   (requires runs_sample/ — run make_sample_run.py first)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "dashboard" / "app.py"
RUNS_SAMPLE = ROOT / "runs_sample"
DB_ROOT = ROOT / "tasks" / "text-to-sql" / "data" / "public" / "databases"

spec = importlib.util.spec_from_file_location("dash_app", APP)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)  # safe: render() is guarded by __name__ == "__main__"

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  PASS  {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name}  {extra}")


def main():
    check("find_runs finds demo", "demo" in app.find_runs(RUNS_SAMPLE), str(app.find_runs(RUNS_SAMPLE)))

    gens = app.load_run(RUNS_SAMPLE, "demo")
    check("load_run returns 6 gens", len(gens) == 6, str(len(gens)))
    accs = [g["accuracy"] for g in gens]
    check("accuracy present for all gens", all(a is not None for a in accs), str(accs))
    check("accuracy is monotonically increasing", all(b >= a for a, b in zip(accs, accs[1:])), str(accs))
    check("improvement.md present for gen>=2", all(g["improvement_md"] for g in gens[1:]))

    cand = app.pick_before_after(gens)
    check("before/after candidates exist", len(cand) > 0, str(len(cand)))
    if cand:
        c = cand[0]
        check("candidate has before+after sql", bool(c["before_sql"]) and bool(c["after_sql"]))
        # The 'after' (gold) must execute; run it live read-only.
        cols, rows, err = app.run_sql_ro(app.db_path_for(c["db_id"]), c["after_sql"])
        check("after_sql executes live", err is None, str(err))

    # Read-only guard: a write must fail on the read-only connection.
    any_db = next(DB_ROOT.glob("*/*.sqlite"))
    _, _, werr = app.run_sql_ro(any_db, "CREATE TABLE hax(x)")
    check("playground runner is read-only", werr is not None, str(werr))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
