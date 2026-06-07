# Phylo results

**18 agents · 3 generations · 2 populations**  
Best **gen_2:pop_1:agent_2** @ **67.5%** execution accuracy (climb 60.0% → 67.5%). Same task model fixed; only the harness evolves.

| gen | best | mean |
|----|------|------|
| 1 | 60.0% | 60.0% |
| 2 | 67.5% | 62.1% |
| 3 | 67.5% | 65.0% |

Full lineage (nodes/edges/genomes): `results/lineage.json`. Best harness: `results/best_genome.json`.
Sponsors: Weave (traces) · Redis (genomes/sorted-sets/lineage/vector/pub-sub/cache) · CopilotKit (AG-UI) · OpenAI (GPT-5).
