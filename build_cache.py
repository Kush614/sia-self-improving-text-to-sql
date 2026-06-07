#!/usr/bin/env python
"""Snapshot a SIA run into a single self-contained file the demo frontend reads.

Produces dashboard/cache/demo_data.js -> `window.DEMO_DATA = {...}` so the dashboard
works fully offline (open index.html via file:// — no server, no model API, no live
DB needed), and keeps showing results even if the SIA run is stopped.

Captured per generation: accuracy / correct / total / error histogram, the
feedback agent's improvement.md (rendered to HTML), and agent size. Plus a
"before/after" set: questions the first generation got wrong and the best generation
got right, with each query's result table PRE-EXECUTED (read-only) so the playground
shows real wrong-table-vs-right-table without touching a model or DB at demo time.

Re-runnable: rerun after each generation to refresh the snapshot.

Usage:  python build_cache.py [--run-id 1]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import markdown as md

REPO = Path(__file__).resolve().parent
TASK_DIR = REPO / "tasks" / "text-to-sql"
DB_ROOT = TASK_DIR / "data" / "public" / "databases"
RUNS = REPO / "runs"
OUT = REPO / "dashboard" / "cache" / "demo_data.js"

ROW_CAP = 20
COL_CAP = 12


# ── read-only SQL (for pre-executing the before/after tables) ────────────────

def run_sql_ro(db_id: str, sql: str, timeout: float = 5.0):
    db_path = DB_ROOT / db_id / f"{db_id}.sqlite"
    if not sql or not sql.strip():
        return {"columns": [], "rows": [], "error": "empty", "rowcount": 0}
    if not db_path.exists():
        return {"columns": [], "rows": [], "error": f"db-not-found:{db_id}", "rowcount": 0}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=timeout)
    timer = threading.Timer(timeout, con.interrupt)
    try:
        timer.start()
        cur = con.execute(sql)
        cols = [c[0] for c in cur.description] if cur.description else []
        allrows = cur.fetchmany(1000)
        rowcount = len(allrows)
        rows = [[str(c) for c in r[:COL_CAP]] for r in allrows[:ROW_CAP]]
        return {"columns": cols[:COL_CAP], "rows": rows, "error": None, "rowcount": rowcount}
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        return {"columns": [], "rows": [], "error": ("timeout" if "interrupted" in msg.lower() else msg), "rowcount": 0}
    finally:
        timer.cancel()
        con.close()


def render_md(text: str | None) -> str | None:
    if not text:
        return None
    return md.markdown(text, extensions=["fenced_code", "tables", "sane_lists"])


def load_gens(run_dir: Path) -> list[dict]:
    gens = []
    gen_dirs = sorted(
        (p for p in run_dir.iterdir() if p.is_dir() and p.name.startswith("gen_")),
        key=lambda p: int(p.name.split("_")[1]),
    )
    for gd in gen_dirs:
        n = int(gd.name.split("_")[1])
        rj = gd / "results.json"
        if not rj.exists():
            continue  # skip generations not yet scored
        res = json.loads(rj.read_text(encoding="utf-8"))
        imp = gd / "improvement.md"
        agent = gd / "target_agent.py"
        agent_txt = agent.read_text(encoding="utf-8") if agent.exists() else ""
        imp_txt = imp.read_text(encoding="utf-8") if imp.exists() else None
        # short list of h3 headings as "headline changes"
        headlines = []
        if imp_txt:
            for line in imp_txt.splitlines():
                s = line.strip()
                if s.startswith("### "):
                    headlines.append(s[4:].strip())
        gens.append({
            "gen": n,
            "accuracy": res.get("accuracy"),
            "n_correct": res.get("n_correct"),
            "n_total": res.get("n_total"),
            "error_summary": res.get("error_summary", {}),
            "improvement_html": render_md(imp_txt),
            "improvement_md": imp_txt,
            "headlines": headlines[:8],
            "agent_lines": agent_txt.count("\n") + 1 if agent_txt else 0,
            "agent_bytes": len(agent_txt.encode("utf-8")),
            "details": res.get("details", []),
        })
    return gens


def build_before_after(gens: list[dict], limit: int = 16) -> list[dict]:
    scored = [g for g in gens if g.get("details")]
    if len(scored) < 2:
        return []
    first = scored[0]
    best = max(scored, key=lambda g: g["accuracy"] or 0)
    if best["gen"] == first["gen"]:
        return []
    fb = {d["id"]: d for d in first["details"]}
    bb = {d["id"]: d for d in best["details"]}
    out = []
    for qid, fd in fb.items():
        bd = bb.get(qid)
        if bd and not fd["correct"] and bd["correct"]:
            db_id = fd.get("db_id", "")
            before_sql = fd.get("predicted_sql", "")
            after_sql = bd.get("predicted_sql", "")
            gold_sql = bd.get("gold_sql", fd.get("gold_sql", ""))
            out.append({
                "id": qid,
                "db_id": db_id,
                "question": fd.get("question", ""),
                "before_gen": first["gen"],
                "after_gen": best["gen"],
                "before": {"sql": before_sql, "verifier_error": fd.get("pred_error"), **run_sql_ro(db_id, before_sql)},
                "after": {"sql": after_sql, **run_sql_ro(db_id, after_sql)},
                "gold": {"sql": gold_sql, **run_sql_ro(db_id, gold_sql)},
            })
    out.sort(key=lambda r: (r["db_id"], r["id"]))
    return out[:limit]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="1")
    ap.add_argument("--max-gen", type=int, default=None,
                    help="Only include generations up to this number (e.g. 4 to pin the demo).")
    args = ap.parse_args()
    run_dir = RUNS / f"run_{args.run_id}"
    if not run_dir.is_dir():
        raise SystemExit(f"run dir not found: {run_dir}")

    gens = load_gens(run_dir)
    if args.max_gen is not None:
        gens = [g for g in gens if g["gen"] <= args.max_gen]
    if not gens:
        raise SystemExit("no scored generations found yet")

    scored = [g for g in gens if g["accuracy"] is not None]
    first, best = scored[0], max(scored, key=lambda g: g["accuracy"])
    before_after = build_before_after(gens)

    # strip heavy per-question details from the per-gen payload (kept only for before/after)
    gens_public = [{k: v for k, v in g.items() if k != "details"} for g in gens]

    data = {
        "meta": {
            "run_id": str(args.run_id),
            "generations": len(gens),
            "task_model": "claude-haiku-4-5 (fixed every generation)",
            "meta_model": "claude-sonnet (feedback agent)",
            "scored_set": first["n_total"],
            "databases": len(list(DB_ROOT.glob("*/*.sqlite"))),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "verifier": "execution accuracy (read-only, order-insensitive)",
        },
        "summary": {
            "first_gen": first["gen"], "first_acc": first["accuracy"],
            "best_gen": best["gen"], "best_acc": best["accuracy"],
            "delta_pts": round((best["accuracy"] - first["accuracy"]) * 100, 1),
        },
        "gens": gens_public,
        "before_after": before_after,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = "window.DEMO_DATA = " + json.dumps(data, ensure_ascii=False) + ";\n"
    OUT.write_text(payload, encoding="utf-8")

    kb = len(payload.encode("utf-8")) / 1024
    print(f"Wrote {OUT} ({kb:.0f} KB)")
    print(f"  generations cached: {[g['gen'] for g in gens]}")
    print(f"  curve: " + " -> ".join(f"{g['accuracy']:.3f}" for g in scored))
    print(f"  best: gen {best['gen']} @ {best['accuracy']:.3f}  |  delta {data['summary']['delta_pts']} pts")
    print(f"  before/after examples: {len(before_after)}")


if __name__ == "__main__":
    main()
