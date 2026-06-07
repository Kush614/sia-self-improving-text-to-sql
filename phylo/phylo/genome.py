"""Agent genome: a harness decomposed into named, recombinable sections.

Genetic operators work on this structured representation, not raw prompt strings,
so crossover/mutation preserve semantic units (see operators.py). The schema matches
SPECS.md §Data Models so a RedisStore can serialize it 1:1 when sponsor docs land.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

# The five recombinable sections of a harness. Order matters for one-point crossover.
SECTIONS = ("system_prompt", "tool_definitions", "meta_instructions", "error_handling", "output_format")


def agent_id(gen: int, pop: int, agent: int) -> str:
    return f"gen_{gen}:pop_{pop}:agent_{agent}"


@dataclass
class Genome:
    """One agent harness + its lineage metadata."""

    # ── recombinable sections ────────────────────────────────────────────────
    system_prompt: str = ""
    tool_definitions: str = "[]"          # JSON array (string)
    meta_instructions: str = ""
    error_handling: str = ""
    output_format: str = ""               # JSON schema / format spec (string)

    # ── identity + lineage ───────────────────────────────────────────────────
    gen: int = 0
    pop: int = 0
    agent: int = 0
    parent_a: str | None = None
    parent_b: str | None = None
    operator: str = "seed"                # seed | mutation | crossover | migration
    crossover_points: list[str] = field(default_factory=list)  # sections taken from parent_b
    created_at: str | None = None         # ISO string, stamped by the store
    fitness: float | None = None          # execution accuracy, filled by the evaluator

    @property
    def id(self) -> str:
        return agent_id(self.gen, self.pop, self.agent)

    def sections(self) -> dict[str, str]:
        return {s: getattr(self, s) for s in SECTIONS}

    def with_sections(self, sections: dict[str, str]) -> "Genome":
        """Return a copy with the given sections replaced (lineage fields reset by caller)."""
        data = self.sections() | {k: v for k, v in sections.items() if k in SECTIONS}
        return Genome(**data)

    def genome_text(self) -> str:
        """Canonical text of the harness (used for embeddings / similarity later)."""
        return "\n\n".join(f"## {s}\n{getattr(self, s)}" for s in SECTIONS)

    def content_hash(self) -> str:
        return hashlib.sha256(self.genome_text().encode("utf-8")).hexdigest()[:12]

    # ── serialization (flat string dict ⇄ Redis hash shape) ──────────────────
    def to_hash(self) -> dict[str, str]:
        d = asdict(self)
        d["crossover_points"] = json.dumps(self.crossover_points)
        return {k: ("" if v is None else str(v)) for k, v in d.items()}

    @classmethod
    def from_hash(cls, h: dict[str, str]) -> "Genome":
        def num(x, cast, default=None):
            try:
                return cast(x)
            except (TypeError, ValueError):
                return default
        return cls(
            system_prompt=h.get("system_prompt", ""),
            tool_definitions=h.get("tool_definitions", "[]"),
            meta_instructions=h.get("meta_instructions", ""),
            error_handling=h.get("error_handling", ""),
            output_format=h.get("output_format", ""),
            gen=num(h.get("gen"), int, 0),
            pop=num(h.get("pop"), int, 0),
            agent=num(h.get("agent"), int, 0),
            parent_a=h.get("parent_a") or None,
            parent_b=h.get("parent_b") or None,
            operator=h.get("operator", "seed"),
            crossover_points=json.loads(h["crossover_points"]) if h.get("crossover_points") else [],
            created_at=h.get("created_at") or None,
            fitness=num(h.get("fitness"), float, None),
        )

    def to_dict(self) -> dict:
        return asdict(self) | {"id": self.id, "content_hash": self.content_hash()}
