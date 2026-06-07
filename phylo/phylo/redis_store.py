"""Redis-backed Store (sponsor: Redis). Drop-in for InMemoryStore.

Implements the same interface using Redis primitives (SPECS.md §Data Models):
  - genomes        → hashes        phylo:genome:{id}
  - fitness        → sorted sets   phylo:fitness:gen_{G}:pop_{P}   (O(log N) top-K)
  - lineage        → sets          phylo:children:{id} / phylo:parents:{id}
  - new-agent feed → pub/sub       channel "phylo:events"          (→ CopilotKit AG-UI)
  - ancestry search→ RedisVL       index "phylo_genome_idx"         (genome embeddings)

`get_store()` returns this when REDIS_URL is set; otherwise InMemoryStore. Needs
`pip install redis redisvl`. Vector methods are optional (need an embedding source).
"""
from __future__ import annotations

import json
import threading
from typing import Callable

import redis  # requires: pip install redis

from phylo.genome import Genome

NS = "phylo"
EVENTS = f"{NS}:events"
AGENTS = f"{NS}:agents"
VINDEX = f"{NS}_genome_idx"


def _split_id(agent_id: str) -> tuple[int, int, int]:
    # "gen_2:pop_1:agent_3" -> (2, 1, 3)
    parts = dict(p.split("_") for p in agent_id.split(":"))
    return int(parts["gen"]), int(parts["pop"]), int(parts["agent"])


class RedisStore:
    def __init__(self, url: str):
        self.r = redis.from_url(url, decode_responses=True)
        self._embed_dim = 1536

    # ── keys ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _gkey(aid: str) -> str: return f"{NS}:genome:{aid}"
    @staticmethod
    def _fkey(gen: int, pop: int) -> str: return f"{NS}:fitness:gen_{gen}:pop_{pop}"
    @staticmethod
    def _popkey(gen: int, pop: int) -> str: return f"{NS}:pop:gen_{gen}:pop_{pop}"
    @staticmethod
    def _ckey(aid: str) -> str: return f"{NS}:children:{aid}"
    @staticmethod
    def _pkey(aid: str) -> str: return f"{NS}:parents:{aid}"

    def ping(self) -> bool:
        return bool(self.r.ping())

    # ── writes ────────────────────────────────────────────────────────────────
    def save_agent(self, g: Genome) -> Genome:
        if g.created_at is None:
            import datetime as _dt
            g.created_at = _dt.datetime.now().isoformat(timespec="seconds")
        pipe = self.r.pipeline()
        pipe.hset(self._gkey(g.id), mapping=g.to_hash())
        pipe.sadd(AGENTS, g.id)
        pipe.sadd(self._popkey(g.gen, g.pop), g.id)
        if g.fitness is not None:
            pipe.zadd(self._fkey(g.gen, g.pop), {g.id: g.fitness})
        for parent in (g.parent_a, g.parent_b):
            if parent:
                pipe.sadd(self._ckey(parent), g.id)
                pipe.sadd(self._pkey(g.id), parent)
        pipe.execute()
        self.r.publish(EVENTS, json.dumps({"type": "agent", **g.to_dict()}))
        return g

    def set_fitness(self, agent_id: str, fitness: float) -> None:
        gen, pop, _ = _split_id(agent_id)
        pipe = self.r.pipeline()
        pipe.hset(self._gkey(agent_id), "fitness", str(fitness))
        pipe.zadd(self._fkey(gen, pop), {agent_id: fitness})
        pipe.execute()
        self.r.publish(EVENTS, json.dumps({"type": "fitness", "id": agent_id, "fitness": fitness}))

    # ── reads ─────────────────────────────────────────────────────────────────
    def get_agent(self, agent_id: str) -> Genome | None:
        h = self.r.hgetall(self._gkey(agent_id))
        return Genome.from_hash(h) if h else None

    def get_population(self, gen: int, pop: int) -> list[Genome]:
        ids = self.r.smembers(self._popkey(gen, pop))
        gs = [self.get_agent(i) for i in ids]
        return sorted((g for g in gs if g), key=lambda g: g.agent)

    def top_k(self, gen: int, pop: int, k: int) -> list[Genome]:
        ids = self.r.zrevrange(self._fkey(gen, pop), 0, max(0, k) - 1)
        return [g for g in (self.get_agent(i) for i in ids) if g]

    def children(self, agent_id: str) -> list[str]:
        return sorted(self.r.smembers(self._ckey(agent_id)))

    def parents(self, agent_id: str) -> list[str]:
        return sorted(self.r.smembers(self._pkey(agent_id)))

    def all_agents(self) -> list[Genome]:
        ids = self.r.smembers(AGENTS)
        gs = [g for g in (self.get_agent(i) for i in ids) if g]
        return sorted(gs, key=lambda g: (g.gen, g.pop, g.agent))

    def export_lineage(self) -> dict:
        agents = self.all_agents()
        nodes = [g.to_dict() for g in agents]
        edges = []
        for g in agents:
            for p in self.parents(g.id):
                edges.append({"source": p, "target": g.id, "operator": g.operator})
        best = max((g for g in agents if g.fitness is not None), key=lambda g: g.fitness, default=None)
        return {
            "nodes": nodes, "edges": edges,
            "generations": sorted({g.gen for g in agents}),
            "populations": sorted({g.pop for g in agents}),
            "best": best.to_dict() if best else None,
        }

    # ── pub/sub (→ CopilotKit AG-UI live tree) ───────────────────────────────
    def subscribe(self, fn: Callable[[dict], None]) -> threading.Thread:
        pubsub = self.r.pubsub()
        pubsub.subscribe(EVENTS)

        def _loop():
            for msg in pubsub.listen():
                if msg.get("type") == "message":
                    try:
                        fn(json.loads(msg["data"]))
                    except Exception:  # noqa: BLE001
                        pass

        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        return t

    # ── vector search (RedisVL) — optional ───────────────────────────────────
    def ensure_vector_index(self, dim: int = 1536) -> bool:
        """Create the genome-embedding index if redisvl is available."""
        try:
            from redisvl.index import SearchIndex
            from redisvl.schema import IndexSchema
        except ImportError:
            return False
        self._embed_dim = dim
        schema = IndexSchema.from_dict({
            "index": {"name": VINDEX, "prefix": f"{NS}:vec"},
            "fields": [
                {"name": "agent_id", "type": "tag"},
                {"name": "generation", "type": "numeric"},
                {"name": "population", "type": "numeric"},
                {"name": "fitness", "type": "numeric"},
                {"name": "genome_vector", "type": "vector",
                 "attrs": {"dims": dim, "distance_metric": "cosine", "algorithm": "hnsw", "datatype": "float32"}},
            ],
        })
        idx = SearchIndex(schema, self.r)
        idx.create(overwrite=True)  # recreate cleanly at the requested dim
        self._vindex = idx
        return True

    def index_genome(self, g: Genome, vector: list[float]) -> None:
        if not getattr(self, "_vindex", None):
            return
        import numpy as np
        buf = np.asarray(vector, dtype=np.float32).tobytes()  # RedisVL stores vectors as float32 bytes
        self._vindex.load([{
            "id": g.id, "agent_id": g.id, "generation": g.gen, "population": g.pop,
            "fitness": g.fitness or 0.0, "genome_vector": buf,
        }], id_field="id")

    def search_similar(self, vector: list[float], k: int = 5) -> list[dict]:
        """Find genomes with similar harness text (e.g. high-fitness ancestors unseen by this lineage)."""
        if not getattr(self, "_vindex", None):
            return []
        from redisvl.query import VectorQuery
        q = VectorQuery(vector=vector, vector_field_name="genome_vector",
                        return_fields=["agent_id", "generation", "population", "fitness"], num_results=k)
        return [dict(r) for r in self._vindex.query(q)]

    # ── housekeeping ──────────────────────────────────────────────────────────
    def flush_namespace(self) -> int:
        keys = list(self.r.scan_iter(match=f"{NS}:*"))
        return self.r.delete(*keys) if keys else 0
