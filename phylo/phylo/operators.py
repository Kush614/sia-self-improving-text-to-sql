"""Genetic operators over agent genomes: selection, crossover, mutation, migration.

Operators are pure functions that work on Genome *sections* (semantic units), not raw
strings. The LLM call used by mutation is injected (`llm_fn`) so the engine runs and
tests offline; the real Claude/OpenAI mutation wires in once sponsor docs land.
"""
from __future__ import annotations

import json
import random
import re
from typing import Callable

from phylo.genome import SECTIONS, Genome

LLMFn = Callable[[str], str]  # prompt -> raw text


# ── selection ────────────────────────────────────────────────────────────────

def select_top(population: list[Genome], frac: float = 0.4) -> list[Genome]:
    """Top fraction of a population by fitness (at least 1). Unscored agents rank last."""
    ranked = sorted(population, key=lambda g: (g.fitness if g.fitness is not None else -1.0), reverse=True)
    k = max(1, round(len(ranked) * frac))
    return ranked[:k]


# ── crossover (one-point, on section boundaries) ─────────────────────────────

def crossover(parent_a: Genome, parent_b: Genome, rng: random.Random) -> tuple[dict[str, str], list[str]]:
    """One-point crossover on section boundaries.

    Sections before the cut come from parent_a, sections from the cut onward from
    parent_b. Returns (child_sections, sections_taken_from_b). Section-level (not
    byte-level) keeps each prompt/tool block syntactically intact.
    """
    cut = rng.randint(1, len(SECTIONS) - 1)
    child: dict[str, str] = {}
    from_b: list[str] = []
    for i, s in enumerate(SECTIONS):
        if i < cut:
            child[s] = getattr(parent_a, s)
        else:
            child[s] = getattr(parent_b, s)
            from_b.append(s)
    return child, from_b


# ── mutation (LLM-driven, SIA-style) ─────────────────────────────────────────

def build_mutation_prompt(parent: Genome, failure_summary: str) -> str:
    sec_text = "\n".join(f"### {s}\n{getattr(parent, s) or '(empty)'}" for s in SECTIONS)
    return f"""You are evolving one text-to-SQL agent's HARNESS (its prompt/scaffold) to score
higher on a held-out SQLite benchmark. The model is fixed — only the harness text changes,
so your edits to the prompt are the ONLY lever.

Study the failure summary below and find the SYSTEMATIC mistakes (not one-off questions).
Then rewrite ONE or TWO sections to prevent them. Be specific and bold — add concrete,
general SQLite/benchmark rules to the system_prompt or meta_instructions, e.g.:
- inspect the schema and use EXACT table/column names and exact string-literal casing
- put aggregate functions first in SELECT for GROUP BY queries when the benchmark expects it
- prefer INNER JOIN over LEFT JOIN unless rows without matches are needed
- avoid unnecessary CAST(); read values as stored
- interpret "any"/"all" correctly (e.g. "> any" often means "> MIN")
- think step by step about which tables/joins are needed before writing SQL
Only add rules that the failures justify. Keep the harness general (do not hardcode answers).

CURRENT HARNESS (fitness={parent.fitness}):
{sec_text}

FAILURE SUMMARY:
{failure_summary}

Return ONLY a JSON object mapping the changed section name(s) to their new FULL text,
e.g. {{"system_prompt": "...", "meta_instructions": "..."}}. Valid sections: {", ".join(SECTIONS)}."""


def _parse_sections(raw: str) -> dict[str, str]:
    """Extract a {section: text} JSON object from model output, tolerantly."""
    if not raw:
        return {}
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return {k: str(v) for k, v in obj.items() if k in SECTIONS and isinstance(v, (str, int, float))}


def mutate(parent: Genome, failure_summary: str, llm_fn: LLMFn) -> tuple[dict[str, str], list[str]]:
    """Return (child_sections, changed_section_names). Falls back to the parent unchanged
    if the model returns nothing usable (caller can treat that as a no-op)."""
    raw = llm_fn(build_mutation_prompt(parent, failure_summary))
    changed = _parse_sections(raw)
    child = parent.sections() | changed
    return child, sorted(changed.keys())


# ── migration ────────────────────────────────────────────────────────────────

def should_migrate(rng: random.Random, p: float = 0.05) -> bool:
    return rng.random() < p
