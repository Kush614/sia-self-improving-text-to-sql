#!/usr/bin/env python
"""Snapshot/cache all Phylo results: into phylo/results/ (repo) and into the SIA
dashboard cache (dashboard/cache/phylo_data.js) for the SIA-site Phylo explainer.

Run: python tools/save_phylo_results.py
"""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phylo.seeds import seed_genome  # noqa: E402

PHYLO = Path(__file__).resolve().parents[1]
SIA = PHYLO.parent
LINEAGE = PHYLO / "web" / "lineage.json"


def main():
    if not LINEAGE.exists():
        raise SystemExit("no lineage.json — run a Phylo evolution first")
    lin = json.loads(LINEAGE.read_text(encoding="utf-8"))
    nodes, best = lin["nodes"], lin.get("best")

    bygen: dict[int, list[float]] = {}
    for n in nodes:
        if n.get("fitness") is not None:
            bygen.setdefault(n["gen"], []).append(n["fitness"])
    curve = [{"gen": g, "best": round(max(v), 4), "mean": round(sum(v) / len(v), 4)} for g, v in sorted(bygen.items())]

    # ── phylo/results/ snapshot ──────────────────────────────────────────────
    res = PHYLO / "results"; res.mkdir(exist_ok=True)
    shutil.copy2(LINEAGE, res / "lineage.json")
    (res / "best_genome.json").write_text(json.dumps(best, indent=2), encoding="utf-8")
    first = curve[0]["best"] if curve else 0
    peak = max((c["best"] for c in curve), default=0)
    lines = [
        "# Phylo results\n",
        f"**{len(nodes)} agents · {len(lin['generations'])} generations · {len(lin['populations'])} populations**  ",
        f"Best **{best['id']}** @ **{(best.get('fitness') or 0)*100:.1f}%** execution accuracy "
        f"(climb {first*100:.1f}% → {peak*100:.1f}%). Same task model fixed; only the harness evolves.\n",
        "| gen | best | mean |", "|----|------|------|",
        *[f"| {c['gen']} | {c['best']*100:.1f}% | {c['mean']*100:.1f}% |" for c in curve],
        "\nFull lineage (nodes/edges/genomes): `results/lineage.json`. Best harness: `results/best_genome.json`.",
        "Sponsors: Weave (traces) · Redis (genomes/sorted-sets/lineage/vector/pub-sub/cache) · CopilotKit (AG-UI) · OpenAI (GPT-5).",
    ]
    (res / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ── SIA dashboard cache for the Phylo explainer page ─────────────────────
    seed = seed_genome()
    data = {
        "meta": {"agents": len(nodes), "generations": len(lin["generations"]), "populations": len(lin["populations"]),
                 "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M")},
        "best": best, "curve": curve,
        "seed": {"system_prompt": seed.system_prompt, "meta_instructions": seed.meta_instructions, "output_format": seed.output_format},
    }
    cache = SIA / "dashboard" / "cache"; cache.mkdir(parents=True, exist_ok=True)
    (cache / "phylo_data.js").write_text("window.PHYLO_DATA = " + json.dumps(data, ensure_ascii=False) + ";\n", encoding="utf-8")

    print(f"snapshot -> {res}")
    print(f"curve: " + " -> ".join(f"{c['best']*100:.1f}%" for c in curve))
    print(f"SIA-page cache -> {cache / 'phylo_data.js'}")


if __name__ == "__main__":
    main()
