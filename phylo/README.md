# 🧬 Phylo — Evolutionary Search Over Agent Harnesses

> **Phylo is evolutionary search over AI-agent harnesses.** Instead of improving one agent step-by-step, it evolves a **population** of agent *harnesses* (the prompt/scaffold around a **fixed** model) with genetic operators — **selection, crossover, mutation, migration** — scored by **real text-to-SQL execution accuracy**, exposing the whole search as a **live, glowing 3D phylogeny**.

Built on **[SIA](https://github.com/Kush614/sia-self-improving-text-to-sql)** (which proved *sequential* self-improvement, 75.8% → 95.0% on Spider). Phylo turns that single chain into a population.

🔭 **Sponsors used end-to-end:** Weave/W&B · Redis · CopilotKit (AG-UI) · OpenAI (GPT‑5) + Anthropic
🎛️ **Live app:** Next.js 16 + CopilotKit + Three.js (cyberpunk 3D) · **Explainer + cached demo:** on the SIA site → [`/phylo.html`](https://kush614.github.io/sia-self-improving-text-to-sql/phylo.html)

---

## What it is (one paragraph)

An "agent" here is a **fixed** language model wrapped in a *harness*: a system prompt, reasoning instructions, tools, and output rules. Phylo treats that harness as a **genome** of five recombinable sections and evolves a **population** of them across generations. Each generation it **evaluates** every harness on a held-out Spider set (the predicted SQL is executed read-only and compared to gold — the same ungameable verifier SIA uses), **selects** the top performers, **recombines** their sections (crossover), **mutates** them with Claude/GPT‑5, occasionally **migrates** a strong agent between populations, and carries the elite forward. The model never changes, so **every gain comes purely from the evolved scaffold.** Running many lineages in parallel finds several strong, distinct agents at once and resists local optima — and the entire search is rendered as a live 3D phylogeny (every node an agent, every edge a parent→child mutation/crossover), traced in **Weave**, stored in **Redis**, explored through a **CopilotKit** UI.

## Results

A representative run (2 populations × N agents × several generations, full Spider eval) climbs across generations with the **task model held fixed** — e.g. `67.5% → 75.0%` execution accuracy, with the best agent an *evolved* gen‑4 harness, not the seed. SIA's sequential baseline on the same task reaches 95.0%; Phylo's contribution is **breadth** (many strong harnesses in parallel) and **interpretable provenance** (full lineage + Weave traces). Cached results live in [`results/`](results) and the demo's `web/lineage.json`.

## How it works

```
seed population
   │  (every call traced → Weave)
   ▼
evaluate  ──►  execution-accuracy verifier (read-only SQL on Spider, gold held out)
   │
   ▼
select top-K  ──►  crossover (section-level) · mutation (Claude/GPT-5) · migration · elitism
   │
   ▼
next generation  ──►  persisted to Redis (genomes/fitness/lineage) ──► pub/sub ──► live 3D tree
```

**Genome** (`phylo/genome.py`): five sections — `system_prompt`, `tool_definitions`, `meta_instructions`, `error_handling`, `output_format`. Operators work on these semantic units, not raw strings.

## Sponsor stack — what each does

| Sponsor | How Phylo uses it |
|---|---|
| **CopilotKit (AG-UI)** | Next.js runtime + live AG-UI connection · `useCopilotReadable` exposes the live phylogeny + selected node · **generative-UI actions**: ancestor-diff and an **in-chat live harness editor** · chat sidebar to interrogate any agent's lineage |
| **Redis** | genomes → **hashes** · fitness → **sorted sets** (O(log N) top-K selection) · lineage → parent/child **sets** · **RedisVL vector search** over genome embeddings (similar harnesses) · **pub/sub → SSE** drives the live tree · **cache** memoizes identical model calls |
| **Weave / W&B** | every target + mutation call **traced** (tagged `agent/gen/pop/operator`) · per-generation fitness **dashboards** · **MCP server** for run inspection · per-node Weave deep-links in the inspector |
| **OpenAI + Anthropic** | Anthropic **Haiku** = fixed target model · **Sonnet** + OpenAI **GPT‑5** = diverse mutation · OpenAI **embeddings** feed the Redis vector index |

## Architecture

```
 ┌────────────┐   ┌────────────────┐
 │ Anthropic  │   │  OpenAI GPT-5  │     model calls (every one traced → Weave)
 │ Haiku/Sonnet│  │ mutate+embeds  │
 └─────┬──────┘   └───────┬────────┘
       └───────┬──────────┘
               ▼
   ┌──────────────────────────────────────────────┐
   │  Phylo evolution engine                        │
   │  seed → evaluate (Spider verifier) → select →  │
   │  crossover · mutation · migration · elitism    │
   └───────┬───────────────────────────┬───────────┘
   persist │                    traces  │ fitness
           ▼                            ▼
   ┌───────────────────────┐   ┌──────────────────┐
   │ Redis                 │   │ Weave / W&B      │
   │ hashes·zset·sets·     │   │ traces·dashboards│
   │ RedisVL vector·cache· │   │ ·MCP             │
   │ pub/sub               │   └──────────────────┘
   └───────┬───────────────┘
   pub/sub → Server-Sent Events
           ▼
   ┌──────────────────────────────────────────────┐
   │ CopilotKit AG-UI + Three.js (cyberpunk 3D)     │
   │ live phylogeny · inspector (vector-similar +   │
   │ Weave links) · in-chat live harness editor     │
   └───────┬───────────────────────────────────────┘
   export best genome (export_to_sia.py)
           ▼
   ┌──────────────────────────────────────────────┐
   │ SIA — runs/evolves the evolved target_agent   │
   └──────────────────────────────────────────────┘
```

## Repo structure

```
phylo/
├── phylo/                      # engine (sponsor-agnostic core + integrations)
│   ├── genome.py               # 5-section genome + serialization
│   ├── operators.py            # selection · crossover · mutation · migration
│   ├── evaluator.py            # real Spider execution-accuracy fitness (reuses SIA verifier)
│   ├── evolution.py            # the loop
│   ├── store.py / redis_store.py  # Store interface + Redis (hashes/zset/sets/vector/pubsub)
│   ├── weave_integration.py    # Weave tracing + W&B fitness dashboards
│   ├── models.py               # Anthropic target + Claude/GPT-5 mutation + Redis cache
│   └── seeds.py
├── frontend/                   # Next.js 16 + CopilotKit AG-UI + Three.js cyberpunk app
│   ├── app/                    # page.tsx, api/copilotkit, api/stream (SSE)
│   └── components/             # Phylogeny.tsx (3D), HarnessEditor.tsx (in-chat editor)
├── tools/                      # run_phylo · index_genomes · export_to_sia · save_phylo_results · smokes
├── results/                    # cached run snapshot (lineage, best genome, RESULTS.md)
├── web/lineage.json            # cached demo data
└── tests/test_core.py          # offline engine tests (14/14, no API)
```

## Run it

**Engine tests (offline, no keys):**
```bash
pip install -r requirements.txt        # or: anthropic openai redis redisvl weave wandb
python tests/test_core.py              # 14/14
```

**A real evolution run** (needs keys in env: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `WANDB_API_KEY`, `REDIS_URL`):
```bash
# Phylo reuses SIA's Spider data + verifier. Get it from the SIA repo:
#   git clone https://github.com/Kush614/sia-self-improving-text-to-sql
#   (run its prep_data.py) — Phylo's evaluator reads tasks/text-to-sql/data
python tools/run_phylo.py --pops 2 --per-pop 3 --gens 3 --limit 80 --workers 12
python tools/index_genomes.py          # embed + RedisVL vector index + nearest neighbors
python tools/save_phylo_results.py      # cache snapshot
```

**The cyberpunk app:**
```bash
cd frontend && npm install
# .env.local: OPENAI_API_KEY=... , REDIS_URL=...
npm run dev    # http://localhost:3000
```

**Export the winner back into SIA:**
```bash
python tools/export_to_sia.py          # best genome -> SIA target_agent.py + profile
```

## Integration with SIA

Phylo reuses SIA's exact Spider data + execution-accuracy verifier, so it's evolving harnesses *for the SIA text-to-SQL task*. `tools/export_to_sia.py` converts Phylo's best genome into a SIA-compatible `target_agent.py` + target profile, so **SIA seeds its next run from the evolved harness**. Phylo searches the harness space; SIA runs the winner.

## Honest limitations

- Demo runs use a small population (compute-bounded); real evolutionary search would use 100s–1000s.
- Execution accuracy is an order-insensitive result-set match (pragmatic stand-in for Spider's official test-suite evaluator).
- The standalone frontend is deployed as a static cached explainer; the live interactive app runs locally (Next.js server app — deployable to Vercel with the runtime env vars).

## Credits

Built on [SIA](https://github.com/Kush614/sia-self-improving-text-to-sql) · Spider dataset (Yu et al., Yale) · Weights & Biases (Weave) · Redis · CopilotKit · OpenAI · Anthropic. Built with [Claude Code](https://claude.com/claude-code).
