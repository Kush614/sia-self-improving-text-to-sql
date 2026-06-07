#!/usr/bin/env python
"""One call to each model to validate keys + model ids before the real run."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phylo.models import anthropic_generate, claude_mutate, openai_mutate  # noqa: E402

print("--- anthropic target (haiku) ---")
sql = anthropic_generate("You are a text-to-SQL system. Output only SQL.",
                         "Schema: CREATE TABLE singer(id, name);\nQuestion: how many singers?")
print(repr(sql[:120]))

print("--- claude mutate (sonnet) ---")
print(repr(claude_mutate('Return JSON {"system_prompt":"x"} only.')[:160]))

print("--- openai mutate (gpt-5 / fallback) ---")
print(repr(openai_mutate('Return JSON {"system_prompt":"x"} only.')[:160]))

print("model smoke done")
