#!/usr/bin/env python
"""Verify RedisStore against the configured Redis Cloud (run when network is up).

Needs REDIS_URL in env and `pip install redis redisvl`.
Run: python tools/redis_smoke.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phylo.genome import Genome  # noqa: E402
from phylo.redis_store import RedisStore  # noqa: E402

url = os.getenv("REDIS_URL")
if not url:
    raise SystemExit("REDIS_URL not set (load .env first)")

r = RedisStore(url)
print("ping:", r.ping())
print("flushed keys:", r.flush_namespace())

# seed two agents in gen1/pop0 with fitness, then a gen2 mutation child
a0 = Genome(gen=1, pop=0, agent=0, system_prompt="A", operator="seed", fitness=0.40)
a1 = Genome(gen=1, pop=0, agent=1, system_prompt="B", operator="seed", fitness=0.75)
r.save_agent(a0); r.save_agent(a1)
child = Genome(gen=2, pop=0, agent=0, system_prompt="B+", operator="mutation", parent_a=a1.id, fitness=0.82)
r.save_agent(child)

ok = True
def chk(n, c):
    global ok; ok = ok and c; print(("  PASS " if c else "  FAIL ") + n)

pop = r.get_population(1, 0)
chk("get_population returns 2", len(pop) == 2)
chk("top_k(1,0,1) is the fitter seed", r.top_k(1, 0, 1)[0].id == a1.id)
chk("children adjacency", r.children(a1.id) == [child.id])
chk("parents adjacency", r.parents(child.id) == [a1.id])
lin = r.export_lineage()
chk("lineage nodes == 3", len(lin["nodes"]) == 3)
chk("lineage has edge a1->child", any(e["source"] == a1.id and e["target"] == child.id for e in lin["edges"]))
chk("best is the 0.82 child", lin["best"] and lin["best"]["fitness"] == 0.82)
chk("vector index (redisvl)", r.ensure_vector_index(dim=8))

r.flush_namespace()
print("\nRedisStore OK" if ok else "\nRedisStore FAILURES")
sys.exit(0 if ok else 1)
