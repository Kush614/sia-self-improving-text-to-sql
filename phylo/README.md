# Phylo — Evolutionary Search Over Agent Harnesses

Population-based self-improvement over agent harnesses, built on
[SIA](../README.md). Instead of one self-improving chain (SIA: 75.8% → 95.0% over 8
sequential generations), Phylo evolves a **population** of harnesses — selection,
crossover, mutation, migration — exploring many lineages in parallel and exposing the
search as a living 3D phylogeny.

## Status

**Engine core: built + tested (14/14, offline, no sponsors).** Mock evolution climbs
fitness 0.00 → 0.88 across generations with a real lineage graph.

| Component | Status |
|---|---|
| Genome model (5 recombinable sections + lineage) | ✅ `phylo/genome.py` |
| Storage interface + in-memory impl (sorted-set top-K, lineage adjacency, pub/sub hook) | ✅ `phylo/store.py` |
| Genetic operators (selection, section-crossover, LLM mutation, migration, elitism) | ✅ `phylo/operators.py` |
| Evaluator (real Spider execution accuracy, reuses SIA; model call injected) | ✅ `phylo/evaluator.py` |
| Evolution loop (evaluate → select → recombine → spawn; lineage recorded) | ✅ `phylo/evolution.py` |
| End-to-end test (mock model) | ✅ `tests/test_core.py` (14/14) |
| **RedisStore** (hashes / sorted sets / lineage sets / pub-sub / RedisVL vector index) | ✅ code `phylo/redis_store.py` · ⏳ live test pending network (`tools/redis_smoke.py`) |
| **Weave** tracing + W&B MCP | ✅ code `phylo/weave_integration.py` + MCP registered/connected · ⏳ live trace pending network (`tools/weave_smoke.py`) |
| **OpenAI** mutation diversity | ⏳ awaiting key |
| **CopilotKit AG-UI + Three.js** live phylogeny | ⏳ awaiting CopilotKit docs (will read `export_lineage()`) |
| Small **real** evolution run | ⏳ awaiting fresh Anthropic key + network |

> Sponsor creds live in the repo-root `.env` (git-ignored): `WANDB_API_KEY`, `REDIS_URL`.
> Install sponsor libs with `pip install -r requirements.txt` (needs network).

The sponsor pieces are **drop-in**: `get_store()` returns `RedisStore` when `REDIS_URL`
is set; the model call and tracing are injected seams; `store.export_lineage()` already
emits the exact node/edge JSON the 3D frontend will consume.

## Design note vs SPECS

Crossover children are pure recombination (2 parents, no extra LLM call); mutation
children are LLM-driven (1 parent). Keeping them distinct halves LLM cost and makes the
lineage graph readable (2-parent edge = crossover, 1-parent edge = mutation).

## Run the engine test

```bash
python tests/test_core.py     # offline, no API key
```
