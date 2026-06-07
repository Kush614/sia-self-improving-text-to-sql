#!/usr/bin/env python
"""Verify Weave/W&B connectivity with the configured key (creates a tiny test run)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phylo.weave_integration import Tracer, weave_enabled  # noqa: E402

print("weave_enabled():", weave_enabled())
t = Tracer(project="phylo-smoke")
print("tracer.enabled:", t.enabled)
print("weave url:", t.url)

def generate(system: str, user: str) -> str:
    return "SELECT count(*) FROM singer"

traced = t.op(generate, "target_agent.generate")
with t.attributes(agent_id="gen_1:pop_0:agent_0", gen=1, pop=0, operator="seed"):
    out = traced("you are a sql system", "Question: how many singers?")
print("traced call output:", out)
t.log_generation(1, {0: [0.5, 0.7, 0.3], 1: [0.6, 0.4]})
t.log_generation(2, {0: [0.8, 0.7, 0.6], 1: [0.75, 0.5]})
t.finish()
print("OK — Weave run created and fitness logged.")
