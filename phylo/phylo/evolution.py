"""The evolution loop: evaluate every agent, select, recombine, mutate, migrate, repeat.

Model calls (target eval + mutation) are injected so the engine runs offline/in tests.
Design note vs SPECS.md: crossover children are pure recombination (2 parents, no extra
LLM call) and mutation children are LLM-driven (1 parent). Keeping them distinct halves
LLM cost and makes the lineage graph readable (2-parent edges = crossover, 1-parent = mutation).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable

from phylo import operators
from phylo.evaluator import GenerateFn, evaluate_genome, failure_summary, load_task
from phylo.genome import Genome
from phylo.seeds import seed_genome
from phylo.store import InMemoryStore, Store
from phylo.weave_integration import Tracer


@dataclass
class Config:
    n_pops: int = 3
    n_per_pop: int = 5
    n_gens: int = 6
    select_frac: float = 0.4
    migration_p: float = 0.05
    crossover_p: float = 0.5      # among non-elite, non-migrant slots
    eval_limit: int | None = None  # cap questions per eval (cost control); None = all 120
    eval_workers: int = 12          # parallel model calls per genome eval
    seed: int = 42


def _child(sections: dict[str, str], gen: int, pop: int, agent: int, operator: str,
           parent_a: str | None = None, parent_b: str | None = None,
           crossover_points: list[str] | None = None) -> Genome:
    g = Genome(**sections)
    g.gen, g.pop, g.agent = gen, pop, agent
    g.operator = operator
    g.parent_a, g.parent_b = parent_a, parent_b
    g.crossover_points = crossover_points or []
    return g


class Evolution:
    def __init__(self, config: Config, generate_fn: GenerateFn, mutate_llm_fn: operators.LLMFn,
                 store: Store | None = None, tracer: Tracer | None = None):
        self.cfg = config
        self.tracer = tracer or Tracer(enabled=False)
        # Trace every target-agent call and every mutation as a Weave op.
        self.generate_fn = self.tracer.op(generate_fn, "target_agent.generate")
        self.mutate_llm_fn = self.tracer.op(mutate_llm_fn, "meta_agent.mutate")
        self.store: Store = store or InMemoryStore()
        self.rng = random.Random(config.seed)
        self._fail: dict[str, str] = {}  # agent_id -> failure summary (for mutation)
        self.questions, self.schemas, self.gold = load_task()

    # ── evaluation ────────────────────────────────────────────────────────────
    def evaluate_generation(self, gen: int) -> None:
        per_pop: dict[int, list[float]] = {}
        for pop in range(self.cfg.n_pops):
            per_pop[pop] = []
            for g in self.store.get_population(gen, pop):
                with self.tracer.attributes(agent_id=g.id, gen=gen, pop=pop, operator=g.operator):
                    fitness, traces = evaluate_genome(
                        g, self.questions, self.schemas, self.gold, self.generate_fn,
                        self.cfg.eval_limit, self.cfg.eval_workers)
                self.store.set_fitness(g.id, fitness)
                self._fail[g.id] = failure_summary(traces)
                per_pop[pop].append(fitness)
        self.tracer.log_generation(gen, per_pop)

    # ── seeding ───────────────────────────────────────────────────────────────
    def seed_generation(self) -> None:
        for pop in range(self.cfg.n_pops):
            for a in range(self.cfg.n_per_pop):
                g = seed_genome()
                g.gen, g.pop, g.agent = 1, pop, a
                self.store.save_agent(g)

    # ── spawning the next generation ───────────────────────────────────────────
    def spawn_generation(self, gen: int) -> None:
        prev = gen - 1
        for pop in range(self.cfg.n_pops):
            prev_pop = self.store.get_population(prev, pop)
            selected = operators.select_top(prev_pop, self.cfg.select_frac)
            elite = max(prev_pop, key=lambda g: (g.fitness if g.fitness is not None else -1.0))

            # slot 0: elitism (carry the best unchanged)
            self.store.save_agent(_child(elite.sections(), gen, pop, 0, "elite", parent_a=elite.id))

            for a in range(1, self.cfg.n_per_pop):
                if operators.should_migrate(self.rng, self.cfg.migration_p) and self.cfg.n_pops > 1:
                    other = self.rng.choice([p for p in range(self.cfg.n_pops) if p != pop])
                    top_other = self.store.top_k(prev, other, 1)
                    src = top_other[0] if top_other else self.rng.choice(selected)
                    self.store.save_agent(_child(src.sections(), gen, pop, a, "migration", parent_a=src.id))
                elif len(selected) >= 2 and self.rng.random() < self.cfg.crossover_p:
                    pa, pb = self.rng.sample(selected, 2)
                    sections, from_b = operators.crossover(pa, pb, self.rng)
                    self.store.save_agent(_child(sections, gen, pop, a, "crossover",
                                                 parent_a=pa.id, parent_b=pb.id, crossover_points=from_b))
                else:
                    parent = self.rng.choice(selected)
                    sections, _changed = operators.mutate(parent, self._fail.get(parent.id, ""), self.mutate_llm_fn)
                    self.store.save_agent(_child(sections, gen, pop, a, "mutation", parent_a=parent.id))

    # ── full run ────────────────────────────────────────────────────────────────
    def run(self) -> dict:
        try:
            self.seed_generation()
            self.evaluate_generation(1)
            for gen in range(2, self.cfg.n_gens + 1):
                self.spawn_generation(gen)
                self.evaluate_generation(gen)
            return self.store.export_lineage()
        finally:
            self.tracer.finish()


def run_evolution(config: Config, generate_fn: GenerateFn, mutate_llm_fn: operators.LLMFn,
                  store: Store | None = None) -> dict:
    return Evolution(config, generate_fn, mutate_llm_fn, store).run()
