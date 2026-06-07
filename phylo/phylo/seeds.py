"""The seed genome every population starts from — deliberately minimal (headroom)."""
from __future__ import annotations

from phylo.genome import Genome


def seed_genome() -> Genome:
    return Genome(
        system_prompt="You are a text-to-SQL system. Given a SQLite database schema and a question, output one SQLite SELECT query that answers it.",
        tool_definitions="[]",
        meta_instructions="Read the schema, then write a single SELECT query that answers the question.",
        error_handling="If you are unsure, still return your single best SELECT query.",
        output_format="Output only the SQL query. No markdown, no commentary.",
        operator="seed",
    )
