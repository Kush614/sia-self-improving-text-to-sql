#!/usr/bin/env python
"""SIA text-to-SQL demo dashboard.

Three panels (see specs.md §5.4 / Task 6):
  1. Accuracy-by-generation line chart (from each gen's results.json).
  2. Per-generation improvement.md — "what SIA changed" (the self-edits).
  3. Before/after query playground — a question gen-1 got wrong and the best gen
     got right, with BOTH predicted SQLs executed live (read-only) side by side.

Reads ONLY from disk (runs/, tasks/.../data, databases). No model API calls.

Run:  streamlit run dashboard/app.py
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_RUNS_DIR = REPO / "runs"
TASK_DIR = REPO / "tasks" / "text-to-sql"
DB_ROOT = TASK_DIR / "data" / "public" / "databases"


# ── Pure data helpers (no streamlit; unit-tested separately) ─────────────────

def find_runs(runs_dir: Path) -> list[str]:
    """Return sorted run ids (the part after 'run_') under runs_dir."""
    if not runs_dir.is_dir():
        return []
    runs = []
    for p in sorted(runs_dir.iterdir()):
        if p.is_dir() and p.name.startswith("run_"):
            runs.append(p.name[len("run_"):])
    return runs


def load_run(runs_dir: Path, run_id: str) -> list[dict]:
    """Load every generation of a run as a list of dicts ordered by gen number."""
    run_dir = runs_dir / f"run_{run_id}"
    gens = []
    if not run_dir.is_dir():
        return gens
    gen_dirs = sorted(
        (p for p in run_dir.iterdir() if p.is_dir() and p.name.startswith("gen_")),
        key=lambda p: int(p.name.split("_")[1]),
    )
    for gd in gen_dirs:
        n = int(gd.name.split("_")[1])
        results = None
        rj = gd / "results.json"
        if rj.exists():
            try:
                results = json.loads(rj.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                results = None
        imp = gd / "improvement.md"
        gens.append({
            "gen": n,
            "results": results,
            "accuracy": (results or {}).get("accuracy"),
            "n_correct": (results or {}).get("n_correct"),
            "n_total": (results or {}).get("n_total"),
            "details": (results or {}).get("details", []),
            "error_summary": (results or {}).get("error_summary", {}),
            "improvement_md": imp.read_text(encoding="utf-8") if imp.exists() else None,
            "target_agent": (gd / "target_agent.py").read_text(encoding="utf-8")
            if (gd / "target_agent.py").exists() else None,
        })
    return gens


def details_by_id(gen: dict) -> dict[str, dict]:
    return {d["id"]: d for d in gen.get("details", [])}


def pick_before_after(gens: list[dict]) -> list[dict]:
    """Questions wrong in the first scored gen but correct in the best gen.

    Returns [{id, db_id, question, gold_sql, before_sql, before_err, after_sql}].
    """
    scored = [g for g in gens if g.get("details")]
    if len(scored) < 2:
        return []
    first = scored[0]
    best = max(scored, key=lambda g: g.get("accuracy") or 0)
    if best["gen"] == first["gen"]:
        return []
    fb, bb = details_by_id(first), details_by_id(best)
    out = []
    for qid, fd in fb.items():
        bd = bb.get(qid)
        if bd and not fd["correct"] and bd["correct"]:
            out.append({
                "id": qid,
                "db_id": fd.get("db_id", ""),
                "question": fd.get("question", ""),
                "gold_sql": bd.get("gold_sql", fd.get("gold_sql", "")),
                "before_sql": fd.get("predicted_sql", ""),
                "before_err": fd.get("pred_error"),
                "after_sql": bd.get("predicted_sql", ""),
                "before_gen": first["gen"],
                "after_gen": best["gen"],
            })
    return out


# ── Read-only SQL runner for the live playground (copied from evaluate.py) ────

def run_sql_ro(db_path: Path, sql: str, timeout: float = 5.0):
    """Execute read-only with a hard timeout. Returns (columns, rows, error)."""
    if not sql or not sql.strip():
        return None, None, "empty"
    if not db_path.exists():
        return None, None, f"db-not-found:{db_path.name}"
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=timeout)
    except sqlite3.Error as e:
        return None, None, f"connect-error:{e}"
    timer = threading.Timer(timeout, con.interrupt)
    try:
        timer.start()
        cur = con.execute(sql)
        cols = [c[0] for c in cur.description] if cur.description else []
        rows = cur.fetchmany(1000)
        return cols, rows, None
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        return None, None, ("timeout" if "interrupted" in msg.lower() else msg)
    finally:
        timer.cancel()
        con.close()


def db_path_for(db_id: str) -> Path:
    return DB_ROOT / db_id / f"{db_id}.sqlite"


# ── Streamlit UI ─────────────────────────────────────────────────────────────

def render() -> None:
    import pandas as pd
    import streamlit as st

    st.set_page_config(page_title="SIA · Text-to-SQL self-improvement", layout="wide")
    st.title("SIA — Self-Improving Text-to-SQL Agent")
    st.caption("Same task model every generation. Every gain comes from the agent rewriting its own harness.")

    # Sidebar: choose runs dir + run id
    runs_dir = Path(st.sidebar.text_input("Runs directory", value=str(DEFAULT_RUNS_DIR)))
    run_ids = find_runs(runs_dir)
    if not run_ids:
        st.warning(
            f"No runs found under `{runs_dir}`.\n\n"
            "Run SIA first (`sia run --task_dir ./tasks/text-to-sql --max_gen 8 --run_id 1`), "
            "or point the sidebar at the bundled fixture directory `runs_sample`."
        )
        return
    run_id = st.sidebar.selectbox("Run", run_ids)
    gens = load_run(runs_dir, run_id)
    scored = [g for g in gens if g.get("accuracy") is not None]

    # Banner if this is the labeled fixture
    if (runs_dir / "FIXTURE.txt").exists():
        st.info("⚠️ Viewing the **sample fixture** (genuine scores, placeholder improvement.md). "
                "Point the sidebar at `runs/` to view a real SIA run.")

    # ── Panel 1: accuracy by generation ──────────────────────────────────────
    st.header("1 · Execution accuracy by generation")
    if scored:
        df = pd.DataFrame({
            "generation": [g["gen"] for g in scored],
            "accuracy": [g["accuracy"] for g in scored],
        }).set_index("generation")
        st.line_chart(df, y="accuracy")
        c1, c2, c3 = st.columns(3)
        first, best = scored[0], max(scored, key=lambda g: g["accuracy"])
        c1.metric(f"Gen {first['gen']} (cold start)", f"{first['accuracy']:.1%}")
        c2.metric(f"Best (gen {best['gen']})", f"{best['accuracy']:.1%}")
        c3.metric("Improvement", f"+{(best['accuracy'] - first['accuracy']) * 100:.1f} pts")
        with st.expander("Per-generation table"):
            st.dataframe(pd.DataFrame([
                {"gen": g["gen"], "accuracy": g["accuracy"],
                 "correct": g["n_correct"], "total": g["n_total"],
                 "top errors": ", ".join(f"{k}:{v}" for k, v in list(g["error_summary"].items())[:3])}
                for g in scored
            ]), hide_index=True)
    else:
        st.write("No results.json found in any generation yet.")

    # ── Panel 2: what SIA changed (improvement.md) ───────────────────────────
    st.header("2 · What SIA changed each generation (its own self-edits)")
    with_imp = [g for g in gens if g["improvement_md"]]
    if with_imp:
        labels = [f"gen {g['gen']}" for g in with_imp]
        idx = st.selectbox("Generation", range(len(with_imp)), format_func=lambda i: labels[i])
        st.markdown(with_imp[idx]["improvement_md"])
    else:
        st.write("No improvement.md found yet (the feedback agent writes these for gen ≥ 2).")

    # ── Panel 3: before/after live SQL playground ────────────────────────────
    st.header("3 · Before → after, executed live (read-only)")
    candidates = pick_before_after(gens)
    if not candidates:
        st.write("Need at least two scored generations with a question fixed between them.")
        return

    labels = [f"[{c['db_id']}] {c['question'][:80]}" for c in candidates]
    sel = st.selectbox(f"{len(candidates)} questions gen-{candidates[0]['before_gen']} got wrong "
                       f"and gen-{candidates[0]['after_gen']} got right:", range(len(candidates)),
                       format_func=lambda i: labels[i])
    c = candidates[sel]
    st.markdown(f"**Question:** {c['question']}  \n**Database:** `{c['db_id']}`")
    dbp = db_path_for(c["db_id"])

    def show(title: str, sql: str, gold: bool = False):
        st.subheader(title)
        st.code(sql or "(none)", language="sql")
        cols, rows, err = run_sql_ro(dbp, sql)
        if err:
            st.error(f"execution error: {err}")
        else:
            st.caption(f"{len(rows)} row(s)")
            st.dataframe(pd.DataFrame(rows, columns=cols or None), hide_index=True)

    left, right = st.columns(2)
    with left:
        show(f"❌ Gen {c['before_gen']} (wrong)", c["before_sql"])
        if c["before_err"]:
            st.caption(f"verifier reason: `{c['before_err']}`")
    with right:
        show(f"✅ Gen {c['after_gen']} (correct)", c["after_sql"])
    st.divider()
    show("🪙 Gold query (for reference)", c["gold_sql"], gold=True)


if __name__ == "__main__":
    render()
