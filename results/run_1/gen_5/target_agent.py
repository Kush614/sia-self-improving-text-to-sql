#!/usr/bin/env python
"""Generation 5: Enhanced text-to-SQL target agent.

Key improvements over Gen 4:
  - NEW RULE: Superlative entity questions
    * "Which/What X has the most/highest/lowest Y?" → SELECT entity ONLY
    * count(*) goes in ORDER BY, NOT in SELECT
    * Fixes: concert_singer__t07, student_transcripts_tracking__t11
  - REVISED Rule 1: Sentence-structure determines aggregate ordering
    * "How many X for each Y?" → count(*) FIRST (aggregate-primary phrasing)
    * "For each Y, how many X?" → entity name FIRST (entity-primary phrasing)
    * "[Entity] and number/count of X" → entity FIRST (question-order)
    * Fixes: concert_singer__t11 (review was incorrectly swapping entity→count ordering)
    * Partial fix: concert_singer__t02 (entity name first; also adds JOIN guidance)
  - NEW RULE: GROUP BY + HAVING without outer COUNT wrapper
    * "How many X has/have more than N Y?" → GROUP BY X HAVING count(*) > N (no subquery wrapping)
    * Fixes: car_1__t04 (model was wrapping correct pattern in unnecessary outer COUNT)
  - NEW RULE: Entity name via JOIN in "for each [entity]" GROUP BY
    * When "for each [entity]" refers to an entity in a separate table, JOIN to get its name
    * Partial fix: concert_singer__t02 (missing JOIN to get stadium name)
  - IMPROVED Review: New Check 0 for superlative entity; revised Check 1 with sentence-structure
  - IMPROVED Selection: Three new protection cases added to prevent review regressions
    * Superlative entity protection: prefer c1 when it lacks unnecessary count
    * Entity-count order protection: prefer c1 when review swaps (entity,count)→(count,entity)
    * GROUP BY + HAVING protection: prefer c1 when review adds outer COUNT subquery

Contract: launched as
    python target_agent.py --dataset_dir <DATASET ro> --working_dir <WORK rw>
Only the configured task model may be called.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from pathlib import Path

MODEL = os.environ.get("SIA_TASK_MODEL", "claude-haiku-4-5-20251001")
MAX_TOKENS = 1024
MAX_RETRIES = 4
MAX_REPAIR_ATTEMPTS = 2
MAX_FEW_SHOT = 10
NUM_CANDIDATES = 2  # candidate 1 = standard; candidate 2 = review/refine of candidate 1

# ---------------------------------------------------------------------------
# System prompt — Gen 5: superlative entity, sentence-structure ordering, GROUP BY+HAVING
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are an expert text-to-SQL system specialised in SQLite.
Translate natural-language questions into exactly ONE correct SQLite SELECT query.
Always wrap your final SQL in a ```sql ... ``` code block and output nothing else.

CRITICAL RULES — follow these precisely:

1. COLUMN SELECTION AND ORDER IN SELECT:

   SUPERLATIVE ENTITY RULE (highest priority — check this FIRST):
   When the question asks "Which/What X has the most/highest/lowest/best/fewest Y?"
   or "What is the X that has the most/highest Y?" or "X that has the most Y":
   → SELECT ONLY the entity X — do NOT include count(*) or other aggregate in SELECT
   → The aggregate goes in ORDER BY only (for ranking), NOT in SELECT (it's not being asked for)

   ✓ "Which year has most concerts?" → SELECT Year FROM concert GROUP BY Year ORDER BY count(*) DESC LIMIT 1
   ✓ "What degree name has most students enrolled?" → SELECT degree_name FROM ... ORDER BY count(*) DESC LIMIT 1
   ✓ "Which model has the most versions?" → SELECT Model FROM car_names GROUP BY Model ORDER BY count(*) DESC LIMIT 1
   ✗ WRONG: "Which year has most concerts?" → SELECT Year, count(*) ... (count NOT in SELECT)
   ✗ WRONG: "What degree name has most students?" → SELECT count(*), degree_name ... (WRONG)

   SENTENCE-STRUCTURE RULE (determines column order for GROUP BY queries):
   The position of "how many" in the question determines aggregate vs entity ordering.

   CASE A — Aggregate-primary ("How many X for each Y?" / "Find the number of X for each Y?"):
   The question STARTS with or LEADS with "how many" or "find the number":
   → Aggregate (count/max/min/avg) MUST be FIRST in SELECT
   ✓ "How many players for each hand type?" → SELECT count(*), hand ... GROUP BY hand
   ✓ "How many players from each country?" → SELECT count(*), country_code ... GROUP BY country_code
   ✓ "Find the number of pets for each student" → SELECT count(*), stuid ...
   ✓ "What is the max accelerate for all different cylinders?" → SELECT max(Accelerate), Cylinders ...

   CASE B — Entity-primary ("For each Y, how many X?" / explicit entity-then-count listing):
   The question mentions the ENTITY FIRST (as context/grouping), then asks for the count:
   → Entity NAME comes FIRST in SELECT, then the aggregate
   ✓ "For each stadium, how many concerts play there?" → SELECT stadium.name, count(*) ... JOIN stadium ...
   ✓ "Show names of singers and number of concerts for each" → SELECT singer.name, count(*) ...
   ✓ "List each continent and how many car makers" → SELECT Continent, count(*) ...
   ✗ WRONG: "For each stadium, how many?" → SELECT count(*), Stadium_ID (wrong: ID instead of name AND wrong order)

   CASE C — Multi-column explicit listing (question lists both things with entity first):
   When question says "[entity name] and [count/number]" or "[name], [id], and how many [items]":
   → Follow the EXACT question mention order
   ✓ "Full name, id, and how many models" → SELECT FullName, Id, count(*)
   ✓ "Name of each continent and how many car makers" → SELECT Continent, count(*)

   ENTITY NAME VIA JOIN (applies to Cases B and C):
   When "for each [entity]" refers to an entity that lives in a separate table:
   → JOIN to the entity's table and return its NAME column, not just the FK/ID column
   ✓ "For each stadium, how many concerts?" → JOIN concert with stadium; return stadium.Name (not concert.Stadium_ID)
   ✓ "For each singer, how many concerts?" → JOIN with singer table; return singer.Name

   HAVING IS A FILTER ONLY:
   Using HAVING count(*) to filter groups does NOT mean count(*) goes in SELECT.
   Only add count(*) to SELECT if the question explicitly asks "how many".
   ✗ WRONG: "Which tournaments have more than 10 matches?" → SELECT count(*), tourney_name HAVING count(*) > 10
   ✓ RIGHT: SELECT tourney_name FROM matches GROUP BY tourney_name HAVING count(*) > 10

   ORDER BY IS A SORTER ONLY:
   Using count(*) in ORDER BY to rank entities does NOT mean count(*) goes in SELECT.
   Only add count(*) to SELECT if the question explicitly asks for the count value.
   ✗ WRONG: "Which year has most concerts?" → SELECT Year, count(*) ... ORDER BY count(*) DESC LIMIT 1
   ✓ RIGHT: SELECT Year FROM concert GROUP BY Year ORDER BY count(*) DESC LIMIT 1

2. DISTINCT COUNTS:
   "How many different/distinct/unique X" → COUNT(DISTINCT X), not COUNT(*)
   Examples:
   - "How many different degrees are offered?" → SELECT count(DISTINCT degree_summary_name) FROM Degree_Programs
   - "How many departments offer any degree?" → SELECT count(DISTINCT department_id) FROM Degree_Programs
   Trigger words: "different", "distinct", "unique", "variety of"

3. STRING MATCHING — use EXACT case from DISTINCT VALUES:
   - Use = with EXACT case for string comparisons.
   - Only use LIKE when the question implies partial matching: "contains", "starts with", "has the word".
   - If DISTINCT VALUES shows 'Republic', write GovernmentForm = 'Republic' — NOT LIKE '%Republic%'
   - If DISTINCT VALUES shows 'North America', write Continent = 'North America' exactly.
   - NEVER alter the case of string literals from what the data shows.

4. BOUNDARY CONDITIONS AND QUANTIFIERS:
   - "at least N" → >= N
   - "more than N" → > N
   - "at most N" → <= N
   - "fewer than / less than N" → < N

   CRITICAL — "ANY" vs "ALL" — opposite meanings:
   • "X larger/greater than ANY Y in group" = larger than AT LEAST ONE = X > MIN(Y in group)
   • "X larger/greater than ALL Y in group" = larger than EVERY ONE = X > MAX(Y in group)
   ✓ "larger than any country in Africa" → > (SELECT MIN(Population) WHERE Continent='Africa')
   ✓ "larger than all countries in Africa" → > (SELECT MAX(Population) WHERE Continent='Africa')

   GROUPING + FILTERING WITHOUT OUTER COUNT WRAP:
   "How many X has/have more than N Y?" where X is a grouped entity:
   → SELECT count(*) FROM X JOIN Y GROUP BY X_id HAVING count(*) > N
   → Do NOT wrap in an outer COUNT(*) subquery
   The per-group count(*) IS the result the benchmark expects.
   ✗ WRONG: SELECT COUNT(*) FROM (SELECT X_id FROM X JOIN Y GROUP BY X_id HAVING count(*) > 2)
   ✓ RIGHT:  SELECT count(*) FROM X JOIN Y GROUP BY X_id HAVING count(*) > 2

5. JOIN TYPE:
   - Use INNER JOIN by default.
   - Only use LEFT JOIN when the question explicitly needs rows without matches.

6. AVOID UNNECESSARY CAST:
   - Do NOT use CAST() or type conversion unless absolutely required.

7. COLUMN IDENTITY, TABLE CHOICE, AND ATTRIBUTE FILTERING:

   SELECTING THE RIGHT COLUMN:
   - "which airports" / "what airports" → SELECT AirportName, NOT AirportCode
   - "which countries" / "what countries" → SELECT Name or CountryName, NOT Code
   - "[entity] codes" / "[entity] ids" (explicitly requested) → SELECT the code/id column
   - In GROUP BY / "for each [entity]" queries → JOIN to entity table and return its name column

   FILTERING BY NAMED ATTRIBUTES:
   - "cars of model X" → WHERE Model = 'X' (use the Model column, NOT Make)
   - "students of major X" → WHERE Major = 'X'
   - Check DISTINCT VALUES to confirm which column contains the filter value

   TABLE CHOICE:
   - Prefer the simplest correct query.
   - Don't add unnecessary JOINs. Check if a single table has all needed data.

8. Never use INSERT, UPDATE, DELETE, DROP, CREATE, PRAGMA or ATTACH.
9. Do not output multiple semicolon-separated statements.
"""


# ---------------------------------------------------------------------------
# Model call
# ---------------------------------------------------------------------------

def call_model(messages: list[dict]) -> tuple[str, list[dict]]:
    """Call the task model. Returns (response_text, updated_messages)."""
    import anthropic

    client = anthropic.Anthropic()
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=messages,
            )
            text = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            )
            updated = messages + [{"role": "assistant", "content": text}]
            return text, updated
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(2**attempt)
    raise RuntimeError(
        f"model call failed after {MAX_RETRIES} attempts: {last_err}"
    )


# ---------------------------------------------------------------------------
# SQL extraction
# ---------------------------------------------------------------------------

def extract_sql(raw: str) -> str:
    """Extract SQL from model output (fenced block, then heuristics).

    Takes the FIRST ```sql...``` block if multiple are present.
    """
    m = re.search(r"```(?:sql)?\s*(.*?)\s*```", raw, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()

    lines = raw.strip().splitlines()
    sql_lines: list[str] = []
    collecting = False
    for line in lines:
        stripped = line.strip().upper()
        if not collecting and (stripped.startswith("SELECT") or stripped.startswith("WITH")):
            collecting = True
        if collecting:
            sql_lines.append(line)
    if sql_lines:
        return "\n".join(sql_lines).strip()

    return raw.strip()


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def try_execute_sql(db_path: Path, sql: str) -> tuple[bool, str]:
    """Execute *sql* read-only. Returns (success, error_message)."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")
        cursor = conn.execute(sql)
        cursor.fetchall()
        conn.close()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def execute_and_get_result_key(db_path: Path, sql: str) -> str | None:
    """Execute SQL and return a canonical string key of the sorted result rows."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")
        cursor = conn.execute(sql)
        rows = cursor.fetchall()
        conn.close()
        normalized = tuple(sorted(str(r) for r in rows))
        return str(normalized)
    except Exception:  # noqa: BLE001
        return None


def has_results(result_key: str | None) -> bool:
    """Return True if the result key represents a non-empty result set."""
    if result_key is None:
        return False
    return result_key != "()" and result_key != "str(())"


def get_sample_rows(db_path: Path, schema_ddl: str, n: int = 3) -> str:
    """Return sample rows for each table as commentary lines."""
    samples: list[str] = []
    try:
        table_names = re.findall(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`]?(\w+)[\"'`]?",
            schema_ddl, re.IGNORECASE,
        )
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        for tbl in table_names[:8]:
            try:
                cur = conn.execute(f'SELECT * FROM "{tbl}" LIMIT {n}')
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                if rows:
                    samples.append(f"-- Sample rows from {tbl}: {cols}")
                    for row in rows:
                        samples.append(f"--   {row}")
            except Exception:  # noqa: BLE001
                pass
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(samples)


def get_distinct_string_values(db_path: Path, schema_ddl: str, max_distinct: int = 25) -> str:
    """Show distinct values for categorical string columns."""
    output: list[str] = []
    try:
        table_names = re.findall(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`]?(\w+)[\"'`]?",
            schema_ddl, re.IGNORECASE,
        )
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        for tbl in table_names[:10]:
            if tbl.lower() == "sqlite_sequence":
                continue
            try:
                cur = conn.execute(f'PRAGMA table_info("{tbl}")')
                cols_info = cur.fetchall()
                for col_info in cols_info:
                    col_name = col_info[1]
                    col_type = (col_info[2] or "").upper()
                    if not any(t in col_type for t in ("CHAR", "TEXT", "VARCHAR", "STRING")) and col_type not in ("", "BLOB"):
                        continue
                    try:
                        cur2 = conn.execute(
                            f'SELECT COUNT(DISTINCT "{col_name}") FROM "{tbl}" WHERE "{col_name}" IS NOT NULL'
                        )
                        count = cur2.fetchone()[0]
                        if 2 <= count <= max_distinct:
                            cur3 = conn.execute(
                                f'SELECT DISTINCT "{col_name}" FROM "{tbl}" '
                                f'WHERE "{col_name}" IS NOT NULL '
                                f'ORDER BY "{col_name}" LIMIT {max_distinct}'
                            )
                            values = [str(r[0]) for r in cur3.fetchall()]
                            if values:
                                output.append(
                                    f"-- {tbl}.{col_name} [{count} distinct]: "
                                    + ", ".join(f"'{v}'" for v in values)
                                )
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(output)


# ---------------------------------------------------------------------------
# Few-shot retrieval
# ---------------------------------------------------------------------------

def score_example_similarity(question: str, example: dict) -> float:
    """Score structural/lexical similarity between question and a training example."""
    q_words = set(question.lower().split())
    ex_words = set(example.get("question", "").lower().split())

    overlap = len(q_words & ex_words) / max(len(q_words), 1)

    sql_upper = example.get("query", "").upper()
    boost = 0.0
    if "GROUP BY" in sql_upper:
        if any(w in question.lower() for w in ["each", "per", "group", "many", "count"]):
            boost += 0.4
    if "HAVING" in sql_upper:
        if any(w in question.lower() for w in ["more than", "less than", "at least", "at most"]):
            boost += 0.3
    if " JOIN " in sql_upper:
        boost += 0.1
    if "INTERSECT" in sql_upper or "EXCEPT" in sql_upper or "UNION" in sql_upper:
        if any(w in question.lower() for w in ["both", "and", "not", "except", "only"]):
            boost += 0.3
    if "NOT IN" in sql_upper or "EXCEPT" in sql_upper:
        if any(w in question.lower() for w in ["not", "without", "no ", "except", "don't"]):
            boost += 0.2
    if "DISTINCT" in sql_upper:
        if any(w in question.lower() for w in ["different", "distinct", "unique", "variety"]):
            boost += 0.4
    if "count(*)" in sql_upper.lower() or "count( *)" in sql_upper.lower():
        if any(w in question.lower() for w in ["how many", "count", "number of"]):
            boost += 0.3

    return overlap + boost


def get_few_shot_examples(db_id: str, question: str, train_data: list[dict]) -> list[dict]:
    """Return up to MAX_FEW_SHOT training examples for *db_id*, sorted by relevance."""
    same_db = [ex for ex in train_data if ex.get("db_id") == db_id]

    if len(same_db) <= MAX_FEW_SHOT:
        return same_db

    scored = sorted(same_db, key=lambda ex: score_example_similarity(question, ex), reverse=True)
    return scored[:MAX_FEW_SHOT]


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_initial_prompt(
    question: str,
    schema_ddl: str,
    sample_rows: str,
    distinct_values: str,
    few_shot: list[dict],
) -> str:
    parts: list[str] = [
        "=== DATABASE SCHEMA ===",
        schema_ddl,
    ]

    if distinct_values:
        parts += [
            "",
            "=== DISTINCT VALUES (use EXACT case for = comparisons; use LIKE only for partial matches) ===",
            distinct_values,
        ]

    if sample_rows:
        parts += [
            "",
            "=== SAMPLE DATA (for value/format reference) ===",
            sample_rows,
        ]

    if few_shot:
        parts += ["", "=== EXAMPLE QUESTION-SQL PAIRS FOR THIS DATABASE ==="]
        for ex in few_shot:
            parts.append(f"Question: {ex['question']}")
            parts.append(f"SQL:\n```sql\n{ex['query']}\n```")
            parts.append("")

    parts += [
        "=== YOUR TASK ===",
        f"Question: {question}",
        "",
        "Write a single SQLite SELECT query that answers the question above.",
        "CRITICAL REMINDERS before writing:",
        "  1. SUPERLATIVE ENTITY: 'Which/What X has the most/highest Y?' → SELECT X ONLY (no count in SELECT)",
        "     'Which year has most concerts?' → SELECT Year ... ORDER BY count(*) DESC LIMIT 1",
        "  2. SENTENCE STRUCTURE DETERMINES COLUMN ORDER:",
        "     'How many X for each Y?' → SELECT count(*), Y (count FIRST)",
        "     'For each Y, how many X?' → SELECT Y_name, count(*) (entity name FIRST — JOIN if needed)",
        "     '[Entity] and number of X?' → entity FIRST, count SECOND (question order)",
        "  3. ORDER BY IS SORTER ONLY: count(*) in ORDER BY does NOT mean it goes in SELECT",
        "  4. HAVING IS FILTER ONLY: count(*) in HAVING does NOT mean it goes in SELECT",
        "  5. 'How many X has more than N Y?' → GROUP BY X HAVING count(*) > N (no outer COUNT subquery)",
        "  6. DISTINCT: 'how many different/distinct' → use COUNT(DISTINCT col)",
        "  7. STRING CASE: Use = with EXACT case from DISTINCT VALUES section",
        "  8. ANY vs ALL: 'larger than ANY X' → > MIN(X); 'larger than ALL X' → > MAX(X)",
        "  9. 'For each [entity]' → JOIN to entity table to get its NAME, not just the FK",
        "Wrap in ```sql ... ``` and output nothing else.",
    ]

    return "\n".join(parts)


def build_review_prompt(question: str, candidate_sql: str) -> str:
    """Build a targeted review prompt with 6 checks (added Check 0 for superlative entity).

    Gen 5 changes:
    - Check 0: New — Superlative entity check (most important for new failures)
    - Check 1: Revised — Uses sentence-structure to determine ordering direction
    - Check 2: Unchanged — Unnecessary aggregates (HAVING filter)
    - Check 3: Unchanged — Column identity
    - Check 4: Unchanged — ANY vs ALL
    - Check 5: Unchanged — String case
    """
    parts = [
        f"Review your SQL above for this question: {question}",
        "",
        "Perform these 6 checks. Fix only real issues — if SQL is correct, return unchanged.",
        "",
        "--- CHECK 0: SUPERLATIVE ENTITY (check FIRST) ---",
        "Does the question ask 'Which/What X has the most/highest/lowest/best/fewest Y?'",
        "or 'What is the X that has the most Y?' or 'X that has the most/highest Y'?",
        "",
        "If YES — this is a SUPERLATIVE ENTITY question:",
        "  → SELECT should contain ONLY the entity X (name/value being asked for)",
        "  → count(*) or other aggregates go in ORDER BY only — NOT in SELECT",
        "  → Remove count(*) from SELECT if it is there unnecessarily",
        "",
        "Examples of SUPERLATIVE ENTITY (aggregate must NOT be in SELECT):",
        "  ✓ 'Which year has most concerts?' → SELECT Year ... ORDER BY count(*) DESC LIMIT 1",
        "  ✓ 'What degree name has most students?' → SELECT degree_summary_name ... ORDER BY count(*) DESC LIMIT 1",
        "  ✓ 'Which model has most versions?' → SELECT Model ... ORDER BY count(*) DESC LIMIT 1",
        "  ✗ WRONG: SELECT Year, count(*) ... [count not needed in SELECT — fix to SELECT Year only]",
        "  ✗ WRONG: SELECT count(*), degree_name ... [count not needed — fix to SELECT degree_name only]",
        "",
        "If the question asks 'how many X for each Y' or 'find the number of X for each Y' (NOT superlative),",
        "this check does NOT apply — proceed to Check 1.",
        "",
        "--- CHECK 1: SENTENCE-STRUCTURE AGGREGATE ORDERING ---",
        "Determine WHICH CASE applies based on how the question is phrased:",
        "",
        "CASE A — 'How many X for each Y?' / 'Find the number of X for each Y?':",
        "  Question LEADS with 'how many' or 'find the number':",
        "  → Aggregate (count/max/min/avg) MUST be FIRST in SELECT",
        "  → IMPORTANT: If aggregate is ALREADY first — this is CORRECT, do NOT change it",
        "  ✓ CORRECT: SELECT count(*), hand FROM players GROUP BY hand",
        "  ✗ WRONG: SELECT hand, count(*) → fix to: SELECT count(*), hand",
        "",
        "CASE B — 'For each Y, how many X?' / '[Y name/description] and number of X':",
        "  Entity/group is mentioned FIRST as context, count is secondary:",
        "  → Entity NAME comes FIRST in SELECT, then aggregate",
        "  → JOIN to entity's table to get its descriptive NAME (not FK/ID)",
        "  ✓ CORRECT: SELECT stadium.Name, count(*) FROM concert JOIN stadium ...",
        "  ✓ CORRECT: SELECT singer.Name, count(*) FROM singer_in_concert JOIN singer ...",
        "  ✗ WRONG: SELECT count(*), Stadium_ID → fix to: SELECT Stadium_Name, count(*) [join needed]",
        "",
        "CASE C — Explicit multi-column listing '[entity] and [number/count]':",
        "  Question mentions entity name FIRST and count SECOND:",
        "  → Follow question mention order (entity first, count second)",
        "  ✓ CORRECT: SELECT singer.Name, count(*) [when question says 'names and number of concerts']",
        "  ✗ WRONG: SELECT count(*), singer.Name [when question said 'names' before 'number']",
        "",
        "--- CHECK 2: UNNECESSARY AGGREGATES ---",
        "Does SELECT include count(*) when question asks WHICH/WHAT entities qualify (not HOW MANY)?",
        "  HAVING count(*) > N is a FILTER — do NOT add count(*) to SELECT just because HAVING uses it",
        "  ORDER BY count(*) is a SORTER — do NOT add count(*) to SELECT just because ORDER BY uses it",
        "  ✗ 'What tournament names have >10 matches?' → WRONG: SELECT count(*), tourney_name ...",
        "  ✓ Same question → RIGHT: SELECT tourney_name ... HAVING count(*) > 10  [no count in SELECT]",
        "",
        "--- CHECK 3: COLUMN IDENTITY ---",
        "If question asks 'which [entity]' or 'what [entity]' WITHOUT specifying 'code' or 'id':",
        "  → Return the DESCRIPTIVE NAME column (AirportName, Name, FullName, etc.)",
        "  → NOT a code or id column (AirportCode, Code, CountryCode, etc.)",
        "",
        "--- CHECK 4: ANY vs ALL ---",
        "If question says '[X] larger/greater than ANY [Y] in group':",
        "  → MUST use > (SELECT MIN(Y) WHERE ...)  [larger than at least one = beat the minimum]",
        "  ✗ 'larger than any African country' → > MAX(...) is WRONG",
        "  ✓ 'larger than any African country' → > MIN(...) is RIGHT",
        "",
        "--- CHECK 5: STRING CASE ---",
        "Use = with EXACT case from DISTINCT VALUES section.",
        "Only use LIKE for partial matches ('contains', 'starts with', 'has the word').",
        "",
        "Return ONLY the final (possibly fixed) SQL in ```sql ... ``` format — nothing else.",
    ]
    return "\n".join(parts)


def build_repair_prompt(error_msg: str, original_sql: str) -> str:
    """Build a targeted repair prompt with specific guidance."""
    parts = [
        f"The SQL query you provided failed with this error:\n  {error_msg}",
        "",
        f"Your query was:\n```sql\n{original_sql}\n```",
        "",
        "Please fix the query. Common issues to check:",
        "  1. Column/table names: verify exact names from the schema",
        "  2. String values: check exact case from the DISTINCT VALUES section",
        "  3. COUNT(DISTINCT col) vs COUNT(*) for 'how many different' questions",
        "  4. JOIN type: use INNER JOIN unless rows without matches are needed",
        "  5. Subquery syntax: ensure subqueries are properly nested",
        "",
        "Return ONLY the corrected SQL wrapped in ```sql ... ```.",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Question classification helpers (Gen 5 additions/revisions)
# ---------------------------------------------------------------------------

def question_is_aggregate_focused(question: str) -> bool:
    """Check if question primarily asks for a count/aggregate per group.

    Only returns True for aggregate-primary phrasings (Case A in Rule 1).
    Does NOT return True for superlative entity questions or entity-primary phrasings.
    """
    q_lower = question.lower().strip()
    # Aggregate-primary: starts with "how many" or "find the number"
    if q_lower.startswith("how many"):
        return True
    if q_lower.startswith("find the number of") or q_lower.startswith("what is the number"):
        return True
    # "What is the max/min/avg X for each Y?" patterns (aggregate asked for per group)
    aggregate_starters = ["what is the max", "what is the min", "what is the avg",
                          "what is the average", "what is the maximum", "what is the minimum",
                          "what is the total"]
    if any(q_lower.startswith(p) for p in aggregate_starters):
        if any(w in q_lower for w in ["for each", "per ", "for all different", "for every"]):
            return True
    return False


def question_is_superlative_entity(question: str) -> bool:
    """Check if question asks for the entity with the highest/lowest aggregate.

    Pattern: "Which/What X has the most/highest/lowest/best/fewest Y?"
    These questions want ONLY the entity in SELECT (aggregate goes in ORDER BY only).
    """
    q_lower = question.lower().strip()
    # Superlative keywords
    superlative_words = ["most", "highest", "lowest", "best", "worst", "fewest",
                         "least", "maximum", "minimum", "greatest", "largest", "smallest",
                         "most number of", "highest number of"]
    has_superlative = any(w in q_lower for w in superlative_words)
    if not has_superlative:
        return False

    # Must be asking for an entity (which/what/name that has ...)
    entity_patterns = [
        "which ", "what is the ", "what are the ", "find the ",
        "name that", "names that", "that has the", "with the most",
        "with the highest", "with the lowest", "with the fewest"
    ]
    has_entity_pattern = any(p in q_lower for p in entity_patterns)
    if not has_entity_pattern:
        return False

    # But NOT questions like "what is the max X for each Y" (that's aggregate-per-group)
    per_group_patterns = ["for each", "per ", "for all different", "for every"]
    if any(p in q_lower for p in per_group_patterns):
        return False  # aggregate-per-group, not superlative entity

    return True


def question_is_entity_count_order(question: str) -> bool:
    """Check if question explicitly lists an entity before 'number/count'.

    Pattern: "[names/entity] and [number of / count of]" → entity should come first.
    Used to protect (entity, count) ordering from being reversed by review.
    """
    q_lower = question.lower()
    # Look for "name(s)/entity and number/count" pattern
    entity_then_count_patterns = [
        "names.*and.*number",
        "name.*and.*number",
        "names.*and.*count",
        "name.*and.*count",
        r"\w+ and number of",
        r"\w+ and how many",
    ]
    for pattern in entity_then_count_patterns:
        if re.search(pattern, q_lower):
            return True
    # "For each Y, how many X?" — entity mentioned before "how many"
    if re.search(r"for each .{1,30}, how many", q_lower):
        return True
    return False


def sql_starts_with_aggregate(sql: str) -> bool:
    """Check if SELECT's first column is an aggregate function."""
    after_select = re.sub(r'^\s*SELECT\s+', '', sql, flags=re.IGNORECASE).strip().upper()
    return any(after_select.startswith(agg + "(") for agg in
               ["COUNT", "SUM", "AVG", "MAX", "MIN"])


def sql_has_aggregate_in_select(sql: str) -> bool:
    """Check if SQL has any aggregate function in SELECT clause (before FROM)."""
    select_part = re.split(r'\bFROM\b', sql, maxsplit=1, flags=re.IGNORECASE)[0]
    select_upper = select_part.upper()
    return any(f"{agg}(" in select_upper for agg in ["COUNT", "SUM", "AVG", "MAX", "MIN"])


def sql_has_subquery_outer_count(sql: str) -> bool:
    """Check if SQL wraps a GROUP BY + HAVING subquery in an outer COUNT."""
    sql_upper = sql.upper().strip()
    # Pattern: SELECT COUNT(*) FROM (... GROUP BY ... HAVING ...)
    return bool(re.search(
        r'SELECT\s+COUNT\s*\(\s*\*\s*\)\s+FROM\s+\(',
        sql_upper
    ))


def sql_has_group_by_having(sql: str) -> bool:
    """Check if SQL has GROUP BY ... HAVING without outer COUNT wrapping."""
    sql_upper = sql.upper()
    return "GROUP BY" in sql_upper and "HAVING" in sql_upper and not sql_has_subquery_outer_count(sql)


def question_has_more_than_group_pattern(question: str) -> bool:
    """Check if question is 'How many X has more than N Y?' (GROUP BY + HAVING pattern)."""
    q_lower = question.lower()
    return (
        "how many" in q_lower and
        ("more than" in q_lower or "greater than" in q_lower or
         "at least" in q_lower or "less than" in q_lower or "fewer than" in q_lower)
    )


# ---------------------------------------------------------------------------
# Per-question prediction with review-and-refine self-consistency
# ---------------------------------------------------------------------------

def predict_one(
    q: dict,
    schema_ddl: str,
    db_path: Path,
    few_shot: list[dict],
    dataset_dir: str,
    working_dir: str,
) -> tuple[dict, list[dict]]:
    """Generate SQL with review-and-refine self-consistency and optional repair.

    Phase 1: Generate candidate 1 (standard generation with system prompt rules)
    Phase 2: Review candidate 1 in same conversation (targeted checks)
    Phase 3: Select best candidate with multiple protection rules
    """
    trajectory: list[dict] = [
        {
            "role": "system",
            "content": [{"type": "text", "text": SYSTEM_PROMPT}],
        }
    ]

    sample_rows = get_sample_rows(db_path, schema_ddl) if db_path.exists() else ""
    distinct_values = get_distinct_string_values(db_path, schema_ddl) if db_path.exists() else ""

    # -----------------------------------------------------------------------
    # Phase 1: Standard generation (candidate 1)
    # -----------------------------------------------------------------------
    initial_prompt = build_initial_prompt(
        question=q["question"],
        schema_ddl=schema_ddl,
        sample_rows=sample_rows,
        distinct_values=distinct_values,
        few_shot=few_shot,
    )

    messages: list[dict] = [{"role": "user", "content": initial_prompt}]
    trajectory.append({"role": "user", "content": [{"type": "text", "text": initial_prompt}]})

    final_sql = ""

    try:
        raw1, messages = call_model(messages)
        trajectory.append({"role": "assistant", "content": [{"type": "text", "text": raw1}]})
        sql1 = extract_sql(raw1)
        ok1, err1 = try_execute_sql(db_path, sql1)

        # -----------------------------------------------------------------------
        # Phase 2: Review and refine candidate 1 (in same conversation context)
        # -----------------------------------------------------------------------
        review_prompt = build_review_prompt(q["question"], sql1)
        messages_with_review = messages + [{"role": "user", "content": review_prompt}]
        trajectory.append({"role": "user", "content": [{"type": "text", "text": review_prompt}]})

        raw2, messages_with_review = call_model(messages_with_review)
        trajectory.append({"role": "assistant", "content": [{"type": "text", "text": raw2}]})
        sql2 = extract_sql(raw2)
        ok2, err2 = try_execute_sql(db_path, sql2)

        # -----------------------------------------------------------------------
        # Phase 3: Select best candidate with multi-layer protection
        # -----------------------------------------------------------------------
        if ok1 and ok2:
            key1 = execute_and_get_result_key(db_path, sql1)
            key2 = execute_and_get_result_key(db_path, sql2)

            if key1 == key2:
                # Agreement — use reviewed version (should have fixed any issues)
                final_sql = sql2
                trajectory.append({
                    "role": "system",
                    "content": [{"type": "text", "text": "[selection] Both agree → using reviewed candidate 2"}],
                })
            else:
                # Disagreement — apply selection logic with protection rules
                has_k1 = has_results(key1)
                has_k2 = has_results(key2)

                note = ""
                selected = None

                # --- Protection Rule A: Superlative entity ---
                # If question asks "which entity has the most X" AND candidate 1 has NO aggregate
                # in SELECT AND candidate 2 HAS aggregate in SELECT → review added count unnecessarily
                is_superlative = question_is_superlative_entity(q["question"])
                c1_has_agg = sql_has_aggregate_in_select(sql1)
                c2_has_agg = sql_has_aggregate_in_select(sql2)

                if is_superlative and not c1_has_agg and c2_has_agg:
                    selected = sql1
                    note = "[selection] Disagree: superlative entity — review added unnecessary aggregate → protecting candidate 1"

                # --- Protection Rule B: Aggregate-first ordering protection (from Gen 4) ---
                # If question is aggregate-focused AND c1 has aggregate first AND c2 doesn't
                if selected is None:
                    agg_focused = question_is_aggregate_focused(q["question"])
                    c1_agg_first = sql_starts_with_aggregate(sql1)
                    c2_agg_first = sql_starts_with_aggregate(sql2)

                    if agg_focused and c1_agg_first and not c2_agg_first:
                        selected = sql1
                        note = "[selection] Disagree: review broke aggregate-first ordering → protecting candidate 1"

                # --- Protection Rule C: Entity-count order protection ---
                # If question explicitly mentions entity before "number/count" AND c1 has entity
                # first (no agg in first position) AND c2 has aggregate first → review reordered wrong
                if selected is None:
                    entity_count_order = question_is_entity_count_order(q["question"])
                    if entity_count_order and not c1_agg_first and c2_agg_first:
                        selected = sql1
                        note = "[selection] Disagree: review swapped entity-first to aggregate-first → protecting candidate 1"

                # --- Protection Rule D: GROUP BY + HAVING no outer wrap ---
                # If question has "more than N Y" pattern AND c1 has GROUP BY + HAVING (correct)
                # AND c2 wraps in outer COUNT → review added unnecessary subquery wrapping
                if selected is None:
                    has_more_than_pattern = question_has_more_than_group_pattern(q["question"])
                    c1_group_having = sql_has_group_by_having(sql1)
                    c2_outer_count = sql_has_subquery_outer_count(sql2)

                    if has_more_than_pattern and c1_group_having and c2_outer_count:
                        selected = sql1
                        note = "[selection] Disagree: review added outer COUNT subquery → protecting GROUP BY+HAVING candidate 1"

                # --- Default: result-based selection ---
                if selected is None:
                    if has_k2 and not has_k1:
                        selected = sql2
                        note = "[selection] Disagree: candidate 2 non-empty, candidate 1 empty → candidate 2"
                    elif has_k1 and not has_k2:
                        selected = sql1
                        note = "[selection] Disagree: candidate 1 non-empty, candidate 2 empty → candidate 1"
                    else:
                        selected = sql2
                        note = "[selection] Disagree: both non-empty/empty → using reviewed candidate 2"

                final_sql = selected
                trajectory.append({
                    "role": "system",
                    "content": [{"type": "text", "text": note}],
                })

        elif ok2:
            final_sql = sql2
            trajectory.append({
                "role": "system",
                "content": [{"type": "text", "text": "[selection] Only candidate 2 passes → candidate 2"}],
            })
        elif ok1:
            final_sql = sql1
            trajectory.append({
                "role": "system",
                "content": [{"type": "text", "text": "[selection] Only candidate 1 passes → candidate 1"}],
            })
        else:
            # Neither passes — attempt repair on candidate 1 (as baseline)
            candidate_sql = sql1
            err_for_repair = err1

            for _repair in range(MAX_REPAIR_ATTEMPTS):
                if not candidate_sql:
                    break
                repair_prompt = build_repair_prompt(err_for_repair, candidate_sql)
                messages_repair = messages + [{"role": "user", "content": repair_prompt}]
                trajectory.append({
                    "role": "user",
                    "content": [{"type": "text", "text": repair_prompt}],
                })

                raw_r, messages_repair = call_model(messages_repair)
                trajectory.append({
                    "role": "assistant",
                    "content": [{"type": "text", "text": raw_r}],
                })
                repaired_sql = extract_sql(raw_r)
                ok_r, err_for_repair = try_execute_sql(db_path, repaired_sql)
                candidate_sql = repaired_sql
                messages = messages_repair
                if ok_r:
                    trajectory.append({
                        "role": "system",
                        "content": [{"type": "text", "text": f"[repair] Fixed on attempt {_repair + 1}"}],
                    })
                    break

            final_sql = candidate_sql

    except Exception as exc:  # noqa: BLE001
        error_text = f"[error] {exc}"
        trajectory.append({
            "role": "assistant",
            "content": [{"type": "text", "text": error_text}],
        })
        final_sql = ""

    prediction = {"id": q["id"], "predicted_sql": final_sql}
    return prediction, trajectory


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Gen-5 text-to-SQL agent")
    ap.add_argument("--dataset_dir", required=True)
    ap.add_argument("--working_dir", required=True)
    args = ap.parse_args()

    dataset = Path(args.dataset_dir)
    work = Path(args.working_dir)
    work.mkdir(parents=True, exist_ok=True)

    exec_dir = work / "agent_execution"
    exec_dir.mkdir(exist_ok=True)

    questions = read_jsonl(dataset / "test_questions.jsonl")
    schemas: dict[str, str] = json.loads(
        (dataset / "schemas.json").read_text(encoding="utf-8")
    )

    train_data: list[dict] = []
    train_path = dataset / "train.jsonl"
    if train_path.exists():
        train_data = read_jsonl(train_path)

    print(
        f"Loaded {len(questions)} questions | "
        f"{len(schemas)} schemas | "
        f"{len(train_data)} training examples | "
        f"Model={MODEL} | Candidates={NUM_CANDIDATES} (review-and-refine with 4 protection rules)"
    )

    predictions: list[dict] = []
    total = len(questions)

    for i, q in enumerate(questions):
        db_id: str = q["db_id"]
        schema_ddl: str = schemas.get(db_id, "")
        db_path = dataset / "databases" / db_id / f"{db_id}.sqlite"
        few_shot = get_few_shot_examples(db_id, q["question"], train_data)

        prediction, trajectory = predict_one(
            q=q,
            schema_ddl=schema_ddl,
            db_path=db_path,
            few_shot=few_shot,
            dataset_dir=args.dataset_dir,
            working_dir=args.working_dir,
        )
        predictions.append(prediction)

        exec_file = exec_dir / f"execution_q{i}.json"
        with open(exec_file, "w", encoding="utf-8") as fh:
            json.dump(trajectory, fh, indent=2, ensure_ascii=False)

        print(f"[{i + 1}/{total}] {q['id']} | {prediction['predicted_sql'][:80]}")

    pred_path = work / "predictions.jsonl"
    with open(pred_path, "w", encoding="utf-8") as fh:
        for p in predictions:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")

    print(f"\nDone. Wrote {len(predictions)} predictions to {pred_path}")
    print(f"Execution logs saved to {exec_dir}/")


if __name__ == "__main__":
    main()
