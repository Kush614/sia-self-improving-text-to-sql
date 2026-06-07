#!/usr/bin/env python
"""Run a real Phylo evolution.

Anthropic target agent (Spider execution accuracy) + diverse Claude/OpenAI mutation,
Redis store (if REDIS_URL set), Weave tracing (if WANDB_API_KEY set). Writes the
lineage JSON the 3D phylogeny frontend consumes.

Usage:  python tools/run_phylo.py --pops 2 --per-pop 3 --gens 3 --limit 50
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phylo.evolution import Config, Evolution  # noqa: E402
from phylo.models import make_cached_generate, make_diverse_mutate, make_target_generate  # noqa: E402
from phylo.store import get_store  # noqa: E402
from phylo.weave_integration import Tracer  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pops", type=int, default=2)
    ap.add_argument("--per-pop", type=int, default=3)
    ap.add_argument("--gens", type=int, default=3)
    ap.add_argument("--limit", type=int, default=50, help="questions per eval (cost/time control)")
    ap.add_argument("--workers", type=int, default=12, help="parallel model calls per eval")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--project", default="phylo")
    ap.add_argument("--no-weave", action="store_true")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "web" / "lineage.json"))
    args = ap.parse_args()

    tracer = Tracer(project=args.project, enabled=not args.no_weave)
    store = get_store()
    print(f"store={type(store).__name__}  weave={tracer.enabled} {tracer.url or ''}")
    print(f"config: {args.pops} pops x {args.per_pop} agents x {args.gens} gens, "
          f"eval_limit={args.limit}  (~{args.pops*args.per_pop*args.gens*args.limit} target calls)")

    cfg = Config(n_pops=args.pops, n_per_pop=args.per_pop, n_gens=args.gens,
                 eval_limit=args.limit, eval_workers=args.workers, seed=args.seed)
    generate = make_cached_generate(make_target_generate())  # Redis-memoized target calls
    evo = Evolution(cfg, generate, make_diverse_mutate(seed=args.seed), store=store, tracer=tracer)
    lineage = evo.run()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(lineage, ensure_ascii=False), encoding="utf-8")

    nodes = lineage["nodes"]
    by_gen: dict[int, list[float]] = {}
    for n in nodes:
        if n["fitness"] is not None:
            by_gen.setdefault(n["gen"], []).append(n["fitness"])
    print("best-per-gen:", {g: round(max(v), 3) for g, v in sorted(by_gen.items())})
    strong = sorted({round(n["fitness"], 3) for n in nodes if (n["fitness"] or 0) >= 0.85}, reverse=True)
    best = lineage["best"]
    print(f"BEST: {best['id'] if best else None} @ {round(best['fitness'],3) if best else None}")
    print(f">=0.85 distinct fitness levels: {len(strong)}  {strong}")
    print(f"lineage -> {out}")


if __name__ == "__main__":
    main()
