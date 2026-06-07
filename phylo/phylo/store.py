"""Population/lineage/fitness storage behind one interface.

InMemoryStore implements the same operations a RedisStore will (sorted-set top-K for
selection, parent/child adjacency for lineage, a pub/sub hook for the live frontend),
so the Redis-backed version (sponsor docs pending) is a drop-in: implement Store, set
REDIS_URL, done. Method names mirror SPECS.md §Data Models.
"""
from __future__ import annotations

import datetime as _dt
from collections import defaultdict
from typing import Callable, Protocol

from phylo.genome import Genome


class Store(Protocol):
    def save_agent(self, g: Genome) -> Genome: ...
    def get_agent(self, agent_id: str) -> Genome | None: ...
    def get_population(self, gen: int, pop: int) -> list[Genome]: ...
    def set_fitness(self, agent_id: str, fitness: float) -> None: ...
    def top_k(self, gen: int, pop: int, k: int) -> list[Genome]: ...
    def children(self, agent_id: str) -> list[str]: ...
    def parents(self, agent_id: str) -> list[str]: ...
    def all_agents(self) -> list[Genome]: ...
    def export_lineage(self) -> dict: ...
    def subscribe(self, fn: Callable[[dict], None]) -> None: ...


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


class InMemoryStore:
    """Reference implementation. Deterministic, dependency-free, fully testable."""

    def __init__(self) -> None:
        self._genomes: dict[str, Genome] = {}
        self._fitness: dict[tuple[int, int], dict[str, float]] = defaultdict(dict)  # (gen,pop) -> id -> score
        self._children: dict[str, set[str]] = defaultdict(set)
        self._parents: dict[str, set[str]] = defaultdict(set)
        self._subscribers: list[Callable[[dict], None]] = []

    # ── writes ───────────────────────────────────────────────────────────────
    def save_agent(self, g: Genome) -> Genome:
        if g.created_at is None:
            g.created_at = _now()
        self._genomes[g.id] = g
        if g.fitness is not None:
            self._fitness[(g.gen, g.pop)][g.id] = g.fitness
        for parent in (g.parent_a, g.parent_b):
            if parent:
                self._children[parent].add(g.id)
                self._parents[g.id].add(parent)
        self._publish({"type": "agent", **g.to_dict()})
        return g

    def set_fitness(self, agent_id: str, fitness: float) -> None:
        g = self._genomes[agent_id]
        g.fitness = fitness
        self._fitness[(g.gen, g.pop)][agent_id] = fitness
        self._publish({"type": "fitness", "id": agent_id, "fitness": fitness})

    # ── reads ────────────────────────────────────────────────────────────────
    def get_agent(self, agent_id: str) -> Genome | None:
        return self._genomes.get(agent_id)

    def get_population(self, gen: int, pop: int) -> list[Genome]:
        return sorted((g for g in self._genomes.values() if g.gen == gen and g.pop == pop),
                      key=lambda g: g.agent)

    def top_k(self, gen: int, pop: int, k: int) -> list[Genome]:
        ranked = sorted(self._fitness[(gen, pop)].items(), key=lambda kv: kv[1], reverse=True)
        return [self._genomes[aid] for aid, _ in ranked[:max(0, k)]]

    def children(self, agent_id: str) -> list[str]:
        return sorted(self._children.get(agent_id, set()))

    def parents(self, agent_id: str) -> list[str]:
        return sorted(self._parents.get(agent_id, set()))

    def all_agents(self) -> list[Genome]:
        return sorted(self._genomes.values(), key=lambda g: (g.gen, g.pop, g.agent))

    # ── lineage export for the 3D phylogeny frontend ─────────────────────────
    def export_lineage(self) -> dict:
        nodes = [g.to_dict() for g in self.all_agents()]
        edges = []
        for child, ps in self._parents.items():
            for p in sorted(ps):
                op = self._genomes[child].operator if child in self._genomes else "?"
                edges.append({"source": p, "target": child, "operator": op})
        gens = sorted({g.gen for g in self._genomes.values()})
        pops = sorted({g.pop for g in self._genomes.values()})
        best = max((g for g in self._genomes.values() if g.fitness is not None),
                   key=lambda g: g.fitness, default=None)
        return {
            "nodes": nodes,
            "edges": edges,
            "generations": gens,
            "populations": pops,
            "best": best.to_dict() if best else None,
        }

    # ── pub/sub hook (Redis pub/sub → CopilotKit AG-UI later) ────────────────
    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._subscribers.append(fn)

    def _publish(self, event: dict) -> None:
        for fn in self._subscribers:
            try:
                fn(event)
            except Exception:  # noqa: BLE001 - a bad subscriber must not break evolution
                pass


def get_store() -> Store:
    """Return the configured store. RedisStore is wired once sponsor docs arrive."""
    import os
    if os.getenv("REDIS_URL"):
        try:
            from phylo.redis_store import RedisStore  # type: ignore
            return RedisStore(os.environ["REDIS_URL"])
        except Exception:  # noqa: BLE001 - fall back to in-memory until RedisStore lands
            pass
    return InMemoryStore()
