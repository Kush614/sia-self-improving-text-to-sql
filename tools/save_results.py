#!/usr/bin/env python
"""Persist a run's meaningful results into the repo (results/) + write RESULTS.md.

Copies per-generation results.json, improvement.md, and the evolved target_agent.py
into results/run_<id>/gen_<n>/ (excludes venv, logs, predictions, execution dumps),
plus context.md / profiles.json, and generates a RESULTS.md summary at the repo root.

Usage:  python tools/save_results.py --run-id 1
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "runs"

KEEP = ("results.json", "improvement.md", "target_agent.py")
HEADLINE = {  # short label per generation for the summary table
    1: "Cold start — meta-agent's initial agent (full schema + question, one call)",
    2: "Learned Spider conventions: aggregate-first ordering, exact string casing, any→MIN",
    3: "Fixed its own gen-2 over-correction; added few-shot + execute-and-repair",
    4: "Surgical per-failure fixes (column identity, INNER vs LEFT JOIN, simplification)",
    5: "Over-applied ordering rules → regressed",
    6: "Recovered; new peak",
    7: "Over-tuned again → slight regression",
    8: "Diagnosed & fixed gen-7 regressions (ANY/ALL, CAST, temporal) → final peak",
}


def h3s(md_path: Path, n=6):
    if not md_path.exists():
        return []
    out = []
    for line in md_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("### "):
            out.append(s[4:].strip())
    return out[:n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="1")
    args = ap.parse_args()
    run_dir = RUNS / f"run_{args.run_id}"
    if not run_dir.is_dir():
        raise SystemExit(f"run dir not found: {run_dir}")

    out_root = REPO / "results" / f"run_{args.run_id}"
    out_root.mkdir(parents=True, exist_ok=True)

    gen_dirs = sorted((p for p in run_dir.iterdir() if p.is_dir() and p.name.startswith("gen_")),
                      key=lambda p: int(p.name.split("_")[1]))
    rows = []
    for gd in gen_dirs:
        n = int(gd.name.split("_")[1])
        rj = gd / "results.json"
        if not rj.exists():
            continue
        res = json.loads(rj.read_text(encoding="utf-8"))
        dest = out_root / gd.name
        dest.mkdir(parents=True, exist_ok=True)
        for name in KEEP:
            src = gd / name
            if src.exists():
                shutil.copy2(src, dest / name)
        rows.append({
            "gen": n,
            "acc": res.get("accuracy"),
            "nc": res.get("n_correct"),
            "nt": res.get("n_total"),
            "errs": res.get("error_summary", {}),
            "headlines": h3s(gd / "improvement.md"),
        })

    # run-level files
    for name in ("context.md", "profiles.json"):
        src = run_dir / name
        if src.exists():
            shutil.copy2(src, out_root / name)

    # RESULTS.md
    scored = [r for r in rows if r["acc"] is not None]
    first, best = scored[0], max(scored, key=lambda r: r["acc"])
    lines = []
    lines.append(f"# Results — run_{args.run_id}\n")
    lines.append(f"**{len(scored)} generations · execution accuracy {first['acc']*100:.1f}% → "
                 f"{best['acc']*100:.1f}% (peak gen {best['gen']}, +{(best['acc']-first['acc'])*100:.1f} pts)**  ")
    lines.append("Task model held fixed (Claude Haiku) every generation; meta/feedback agent Claude Sonnet. "
                 "Verifier: execution accuracy (read-only, order-insensitive) on real Spider DBs; gold held out.\n")
    lines.append("| gen | accuracy | correct | errors left | what changed (self-edit) |")
    lines.append("|----|----------|---------|-------------|--------------------------|")
    for r in scored:
        errs = ", ".join(f"{k}:{v}" for k, v in r["errs"].items()) or "0"
        peak = " ⭐" if r["gen"] == best["gen"] else ""
        lines.append(f"| {r['gen']}{peak} | {r['acc']*100:.1f}% | {r['nc']}/{r['nt']} | {errs} | {HEADLINE.get(r['gen'],'')} |")
    lines.append("")
    lines.append("## Per-generation self-edits (from each improvement.md)\n")
    for r in scored:
        if r["headlines"]:
            lines.append(f"**Generation {r['gen']} ({r['acc']*100:.1f}%)**")
            for h in r["headlines"]:
                lines.append(f"- {h}")
            lines.append("")
    lines.append("## Notes\n")
    lines.append("- Two self-correction arcs: gen 2→3 and gen 7→8 (the agent diagnosed and fixed regressions it caused itself).")
    lines.append("- Non-monotonic (gens 5 and 7 regressed) — honest self-improvement, not a smoothed curve.")
    lines.append("- Full artifacts per generation (results.json, improvement.md, evolved target_agent.py) are in "
                 f"`results/run_{args.run_id}/gen_*/`.")
    (REPO / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"saved {len(scored)} generations to {out_root}")
    print("curve:", " -> ".join(f"{r['acc']*100:.1f}%" for r in scored))
    print("wrote", REPO / "RESULTS.md")


if __name__ == "__main__":
    main()
