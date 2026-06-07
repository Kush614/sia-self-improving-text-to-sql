#!/usr/bin/env python
"""Offline end-to-end test of the Phylo engine (no API, no Redis, no sponsors).

Drives genome -> operators -> store -> evaluator -> evolution with a mock model, and
asserts a real lineage forms and fitness climbs across generations.

Run: python tests/test_core.py
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `phylo` importable

from phylo.evaluator import load_task
from phylo.evolution import Config, Evolution
from phylo.genome import SECTIONS, Genome
from phylo.operators import crossover, select_top
from phylo.seeds import seed_genome
from phylo.store import InMemoryStore

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  PASS  {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name}  {extra}")


# ── deterministic mock model: "skill" encoded in the system prompt ───────────
def stable_pct(q: str) -> int:
    return int(hashlib.md5(q.encode()).hexdigest(), 16) % 100


def make_generate(gold_by_qtext):
    def gen(system: str, user: str) -> str:
        m = re.search(r"SKILL=(\d+)", system)
        skill = int(m.group(1)) if m else 0
        qm = re.search(r"Question: (.+)", user)
        q = qm.group(1).strip() if qm else ""
        gold = gold_by_qtext.get(q)
        return gold if (gold and stable_pct(q) < skill) else "SELECT 1"
    return gen


def mock_mutate(prompt: str) -> str:
    m = re.search(r"SKILL=(\d+)", prompt)
    cur = int(m.group(1)) if m else 0
    return json.dumps({"system_prompt": f"text-to-SQL system. SKILL={min(100, cur + 45)}"})


def main():
    # ── genome ────────────────────────────────────────────────────────────────
    g = seed_genome(); g.gen, g.pop, g.agent = 1, 0, 2
    check("genome id", g.id == "gen_1:pop_0:agent_2", g.id)
    check("genome hash roundtrip", Genome.from_hash(g.to_hash()).genome_text() == g.genome_text())
    check("sections complete", set(g.sections()) == set(SECTIONS))

    # ── crossover keeps section boundaries ─────────────────────────────────────
    a = Genome(system_prompt="A_sys", tool_definitions="A_tools", meta_instructions="A_meta",
               error_handling="A_err", output_format="A_out")
    b = Genome(system_prompt="B_sys", tool_definitions="B_tools", meta_instructions="B_meta",
               error_handling="B_err", output_format="B_out")
    child, from_b = crossover(a, b, random.Random(1))
    check("crossover takes a prefix + b suffix",
          all(child[s] in (getattr(a, s), getattr(b, s)) for s in SECTIONS) and 0 < len(from_b) < len(SECTIONS),
          str(from_b))
    check("crossover child has all 5 sections", set(child) == set(SECTIONS))

    # ── selection ──────────────────────────────────────────────────────────────
    pop = [Genome(fitness=f, agent=i) for i, f in enumerate([0.1, 0.9, 0.5, 0.7, 0.3])]
    top = select_top(pop, 0.4)
    check("select_top returns top 40%", [round(x.fitness, 1) for x in top] == [0.9, 0.7], str([x.fitness for x in top]))

    # ── store: lineage + top_k ─────────────────────────────────────────────────
    s = InMemoryStore()
    p = Genome(gen=1, pop=0, agent=0); s.save_agent(p); s.set_fitness(p.id, 0.5)
    c = Genome(gen=2, pop=0, agent=0, parent_a=p.id, operator="mutation"); s.save_agent(c)
    check("store children/parents adjacency", s.children(p.id) == [c.id] and s.parents(c.id) == [p.id])
    check("store top_k by fitness", s.top_k(1, 0, 1)[0].id == p.id)

    # ── end-to-end evolution (mock model) ──────────────────────────────────────
    questions, schemas, gold = load_task()
    gold_by_qtext = {q["question"]: gold[q["id"]] for q in questions}
    cfg = Config(n_pops=2, n_per_pop=3, n_gens=3, eval_limit=40, seed=7)
    evo = Evolution(cfg, make_generate(gold_by_qtext), mock_mutate)
    lineage = evo.run()

    nodes = lineage["nodes"]
    check("all agents created", len(nodes) == cfg.n_pops * cfg.n_per_pop * cfg.n_gens, str(len(nodes)))
    check("lineage has edges", len(lineage["edges"]) > 0, str(len(lineage["edges"])))
    ops = {n["operator"] for n in nodes}
    check("operators include seed+mutation+elite", {"seed", "mutation", "elite"} <= ops, str(ops))

    def best_at(gen):
        fs = [n["fitness"] for n in nodes if n["gen"] == gen and n["fitness"] is not None]
        return max(fs) if fs else 0.0
    g1, g3 = best_at(1), best_at(3)
    check("fitness climbs across generations (mock)", g3 > g1, f"gen1={g1:.2f} gen3={g3:.2f}")
    check("gen 1 seed fitness is ~0 (no skill yet)", g1 == 0.0, str(g1))
    check("export has best agent", lineage["best"] is not None)

    print(f"\n{PASS} passed, {FAIL} failed  |  mock curve: gen1 best={g1:.2f} -> gen3 best={g3:.2f}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
