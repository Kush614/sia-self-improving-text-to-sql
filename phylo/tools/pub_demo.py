#!/usr/bin/env python
"""Publish demo agent/fitness events to the Redis pub/sub channel (drives the LIVE feed)."""
import json
import os
import sys
import time

import redis

r = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
ops = ["mutation", "crossover", "migration", "elite"]
n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
for i in range(n):
    aid = f"gen_{2 + i // 4}:pop_{i % 2}:agent_{i % 3}"
    r.publish("phylo:events", json.dumps({"type": "agent", "id": aid, "operator": ops[i % 4]}))
    time.sleep(0.4)
    r.publish("phylo:events", json.dumps({"type": "fitness", "id": aid, "fitness": 0.6 + 0.012 * i}))
    time.sleep(1.0)
print(f"published {n*2} events")
