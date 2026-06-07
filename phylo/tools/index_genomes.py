#!/usr/bin/env python
"""Embed every stored genome, index it in RedisVL, and compute nearest-neighbor
"similar harnesses" via Redis Vector Search — then inject them into lineage.json.

This makes Redis vector search a real, visible feature: the inspector / copilot can
show "agents whose harness is most similar to this one" across populations/lineages.

Run (network up):  python tools/index_genomes.py
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openai import OpenAI  # noqa: E402
from phylo.redis_store import RedisStore  # noqa: E402

EMBED_MODEL = "text-embedding-3-small"  # 1536 dims
WEB = Path(__file__).resolve().parents[1] / "web" / "lineage.json"
PUBLIC = Path(__file__).resolve().parents[1] / "frontend" / "public" / "lineage.json"


def main():
    store = RedisStore(os.environ["REDIS_URL"])
    agents = store.all_agents()
    if not agents:
        raise SystemExit("no genomes in Redis")
    print(f"embedding {len(agents)} genomes with {EMBED_MODEL}…")

    client = OpenAI()
    texts = [g.genome_text() for g in agents]
    resp = client.embeddings.create(model=EMBED_MODEL, input=texts)
    vectors = {g.id: d.embedding for g, d in zip(agents, resp.data)}
    dim = len(resp.data[0].embedding)

    store.ensure_vector_index(dim=dim)
    for g in agents:
        store.index_genome(g, vectors[g.id])
    print(f"indexed {len(agents)} genome vectors (dim={dim}) in RedisVL")

    # nearest neighbors per genome (Redis Vector Search), excluding self
    neighbors = {}
    for g in agents:
        res = store.search_similar(vectors[g.id], k=5)
        sims = []
        for r in res:
            aid = r.get("agent_id")
            if not aid or aid == g.id:
                continue
            dist = float(r.get("vector_distance", r.get("distance", 0)) or 0)
            sims.append({"id": aid, "fitness": float(r.get("fitness", 0) or 0), "score": round(1 - dist, 3)})
        neighbors[g.id] = sims[:3]

    # inject into lineage.json (web + frontend/public)
    if WEB.exists():
        lin = json.loads(WEB.read_text(encoding="utf-8"))
        for n in lin["nodes"]:
            n["similar"] = neighbors.get(n["id"], [])
        WEB.write_text(json.dumps(lin, ensure_ascii=False), encoding="utf-8")
        PUBLIC.parent.mkdir(parents=True, exist_ok=True)
        PUBLIC.write_text(json.dumps(lin, ensure_ascii=False), encoding="utf-8")
        print(f"injected nearest-neighbor 'similar' into {WEB.name} (+ frontend/public)")

    # show a sample
    sample = agents[0]
    print(f"\nexample — agents most similar to {sample.id}:")
    for s in neighbors[sample.id]:
        print(f"  {s['id']}  sim={s['score']}  fitness={s['fitness']:.3f}")


if __name__ == "__main__":
    main()
