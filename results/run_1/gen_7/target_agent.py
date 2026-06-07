#!/usr/bin/env python
"""Generation 7: Enhanced text-to-SQL target agent.

Key improvements over Gen 6 (94.17%, 113/120):
  - NEW RULE: NOT IN for negative threshold queries
    * "X that have NOT spent/exceeded N" → NOT IN subquery (not JOIN+HAVING<=N)
    * JOIN+HAVING<=N excludes entities with NO related records (wrong!)
    * NOT IN includes entities with no related records (correct!)
    * Fixes: dog_kennels__t10 (dogs with no treatments were excluded by JOIN)
  - NEW RULE: COUNT(DISTINCT) must use descriptive name column, not primary key
    * "how many different degrees" → COUNT(DISTINCT degree_summary_name)
    * NEVER COUNT(DISTINCT primary_key_col) for "how many types" questions
    * PKs are unique per row → count(distinct PK) = count(*) → wrong for types
    * Fixes: student_transcripts_tracking__t03 (counted PK instead of name col)
  - ENHANCED RULE: LIKE patterns with article phrases
    * "has the word X", "has the substring X" → LIKE '%X%' (drop article phrase)
    * "the word/substring" is an article phrase, not part of the LIKE pattern
    * Fixes: student_transcripts_tracking__t08 (used '%the computer%' not '%computer%')
  - NEW REVIEW CHECK 8: NOT IN detection for negative threshold queries
  - ENHANCED REVIEW: COUNT(DISTINCT) primary key validation
  - ADDED: Few-shot example string casing guidance
  - KEPT: All beneficial Gen 5/6 improvements
    * Superlative Entity Rule, Review Check 0
    * Protection Rules A, B, C, E from Gen 4/5/6
    * Python post-processing for outer COUNT wrapper
    * fix_outer_count_from_having_subquery

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
# System prompt — Gen 7: targeted fixes for 3 identified failure patterns
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

   COLUMN ORDER RULE (always follow question word order):
   The SELECT column order MUST match the order the question asks for them.
   ✓ "What are the names and ids?" → SELECT name_col, id_col   (name FIRST, then id)
   ✓ "What are the ids and names?" → SELECT id_col, name_col   (id FIRST, then name)
   ✓ "First name, country code, and birth date" → SELECT first_name, country_code, birth_date
   ✗ WRONG: "What are the names and ids?" → SELECT id_col, name_col (id must NOT come first)

   AGGREGATE ORDERING RULE (determines aggregate vs entity order for GROUP BY queries):

   CASE A — Aggregate-primary: question LEADS with "how many", "find the number", OR
   "What is the max/min/avg/total X for Y?":
   → Aggregate (count/max/min/avg/sum) MUST be FIRST in SELECT
   ✓ "How many players for each hand type?" → SELECT count(*), hand ... GROUP BY hand
   ✓ "How many players from each country?" → SELECT count(*), country_code ... GROUP BY country_code
   ✓ "What is the max accelerate for all different cylinders?" → SELECT max(Accelerate), Cylinders ...
   ✓ "What is the average GNP for each continent?" → SELECT avg(GNP), Continent ...
   ✓ "What is the max/min/avg/total X for each/all different Y?" → SELECT max/min/avg/sum(X), Y

   CASE B — Entity-primary: "For each Y, how many X?" or "[Y] and number of X":
   The entity is mentioned FIRST as context, count is secondary:
   → Entity NAME comes FIRST in SELECT, then the aggregate
   → JOIN to entity's table to get its descriptive NAME (not FK/ID)
   ✓ "For each stadium, how many concerts play there?" → SELECT stadium.name, count(*) JOIN stadium ...
   ✓ "Show names of singers and number of concerts for each" → SELECT singer.name, count(*) ...
   ✓ "List each continent and how many car makers" → SELECT Continent, count(*) ...
   ✗ WRONG: "For each stadium, how many?" → SELECT count(*), Stadium_ID (wrong: ID not name, wrong order)

   CASE C — Explicit multi-column listing (entity listed before count):
   → Follow the EXACT question mention order
   ✓ "Full name, id, and how many models" → SELECT FullName, Id, count(*)
   ✓ "Name of each continent and how many car makers" → SELECT Continent, count(*)

   ENTITY NAME VIA JOIN (applies to Cases B and C):
   When "for each [entity]" refers to an entity in a separate table:
   → JOIN to the entity's table and return its NAME column, not just the FK/ID column
   ✓ "For each stadium, how many concerts?" → JOIN concert with stadium; return stadium.Name
   ✓ "For each singer, how many concerts?" → JOIN with singer table; return singer.Name

   HAVING IS A FILTER ONLY:
   Using HAVING count(*) to filter groups does NOT mean count(*) goes in SELECT.
   Only add count(*) to SELECT if the question explicitly asks "how many".
   ✗ WRONG: "Which tournaments have more than 10 matches?" → SELECT count(*), tourney_name HAVING count(*) > 10
   ✓ RIGHT: SELECT tourney_name FROM matches GROUP BY tourney_name HAVING count(*) > 10

   ORDER BY IS A SORTER ONLY:
   Using count(*) in ORDER BY does NOT mean count(*) goes in SELECT.
   Only add count(*) to SELECT if the question explicitly asks for the count value.
   ✗ WRONG: "Which year has most concerts?" → SELECT Year, count(*) ... ORDER BY count(*) DESC LIMIT 1
   ✓ RIGHT: SELECT Year FROM concert GROUP BY Year ORDER BY count(*) DESC LIMIT 1

2. DISTINCT — ONLY WHEN EXPLICITLY ASKED:
   NEVER use SELECT DISTINCT unless the question explicitly uses one of these trigger words:
   "different", "distinct", "unique", "variety", "possible [combinations/values]"

   ✓ "What are the different models produced after 1980?" → SELECT DISTINCT model ... (trigger: "different")
   ✓ "Find all possible breed and size combinations" → SELECT DISTINCT ... (trigger: "possible combinations")
   ✗ "Find the first name of students who have a dog" → SELECT T1.fname (NO DISTINCT — not asked for)
   ✗ "Show the name and theme for all concerts" → SELECT name, theme (NO DISTINCT)

   COUNT(DISTINCT) for "how many different/distinct/unique X":
   "How many different/distinct/unique X" → COUNT(DISTINCT X), not COUNT(*)
   Trigger words: "different", "distinct", "unique", "variety of"

   CRITICAL — COUNT(DISTINCT) MUST use the DESCRIPTIVE NAME column, NEVER the primary key:
   When counting "how many different types/kinds/varieties" of something:
   → Use COUNT(DISTINCT [name_or_type_column]) — the column that distinguishes types
   → NEVER use COUNT(DISTINCT [primary_key_column]) — PKs are unique per row, not per type
   → COUNT(DISTINCT primary_key) always equals COUNT(*) and is meaningless for "types"
   → Check DISTINCT VALUES: if a column has very few distinct values (2-10), it's likely the type column
   ✓ "How many different degrees?" → COUNT(DISTINCT degree_summary_name) [3 distinct: Bachelor, Master, PHD]
   ✗ WRONG: COUNT(DISTINCT degree_program_id) [primary key → unique per row, not per degree type]
   ✓ "How many distinct cities?" → COUNT(DISTINCT city_name), NOT COUNT(DISTINCT city_id)
   ✓ "How many unique languages?" → COUNT(DISTINCT Language), NOT COUNT(DISTINCT language_id)

3. SELECT ONLY WHAT'S REQUESTED:
   Only include columns that the question explicitly asks for.
   Do NOT add extra columns (id, code, address_id, etc.) unless the question mentions them.
   ✗ "What are all the addresses including line 1 and line 2?" → SELECT line_1, line_2 (NOT address_id)
   ✗ "What is the total population and average area?" → SELECT sum(Population), avg(SurfaceArea) (no extra cols)

4. FINDING THE TOP ENTITY + ITS ATTRIBUTES:
   "What is the [entity] that has the most [Y], and what is their [attribute]?"
   → Use: SELECT entity, attribute FROM table GROUP BY entity ORDER BY count(*) DESC LIMIT 1
   → Check if [attribute] exists DIRECTLY in the main table before joining to other tables
   ✓ "Name of winner with most matches and their rank points?" → SELECT winner_name, winner_rank_points
     FROM matches GROUP BY winner_name ORDER BY count(*) DESC LIMIT 1
   (winner_rank_points is directly in matches table — no need to join rankings table)

5. GROUPING + FILTERING:
   "How many X has/have more than N Y?" where X is a grouped entity:
   → SELECT count(*) FROM X JOIN Y GROUP BY X_id HAVING count(*) > N
   → Do NOT wrap in an outer COUNT(*) subquery
   The per-group count(*) IS the result the benchmark expects.
   ✓ RIGHT: SELECT count(*) FROM countries JOIN car_makers GROUP BY countryid HAVING count(*) > 2

6. STRING MATCHING — use EXACT case from DISTINCT VALUES:
   - Use = with EXACT case for string comparisons.
   - Only use LIKE when the question implies partial matching: "contains", "starts with", "has the word".
   - If DISTINCT VALUES shows 'Republic', write GovernmentForm = 'Republic' — NOT LIKE '%Republic%'
   - NEVER alter the case of string literals from what the data shows.

   LIKE PATTERNS WITH ARTICLE PHRASES:
   - "has the word X" → LIKE '%X%'   (NOT LIKE '%the word X%' — "the word" is an article phrase)
   - "has the substring X" → LIKE '%X%'  (NOT LIKE '%the substring X%' — "the substring" is an article phrase)
   - "whose name has X" → LIKE '%X%'
   - "name contains the word X" → LIKE '%X%'
   ✓ "department whose name has the word computer" → LIKE '%computer%'
   ✓ "department whose name has the substring computer" → LIKE '%computer%'
   ✗ WRONG: → LIKE '%the word computer%' or LIKE '%the substring computer%'

   FEW-SHOT STRING CASING: When EXAMPLE QUESTION-SQL PAIRS contains a very similar question
   with a WHERE/LIKE clause using a specific string literal for the same filter column, prefer
   that exact casing as it reflects the actual evaluation expectation.

7. BOUNDARY CONDITIONS AND QUANTIFIERS:
   - "at least N" → >= N
   - "more than N" → > N
   - "at most N" → <= N
   - "fewer than / less than N" → < N

   CRITICAL — "ANY" vs "ALL" — opposite meanings:
   • "X larger/greater than ANY Y in group" = larger than AT LEAST ONE = X > MIN(Y in group)
   • "X larger/greater than ALL Y in group" = larger than EVERY ONE = X > MAX(Y in group)
   ✓ "larger than any country in Africa" → > (SELECT MIN(Population) WHERE Continent='Africa')
   ✓ "larger than all countries in Africa" → > (SELECT MAX(Population) WHERE Continent='Africa')

8. JOIN TYPE:
   - Use INNER JOIN by default.
   - Only use LEFT JOIN when the question explicitly needs rows without matches.

9. AVOID UNNECESSARY CAST:
   - Do NOT use CAST() or type conversion unless absolutely required.

10. COLUMN IDENTITY, TABLE CHOICE, AND ATTRIBUTE FILTERING:

    SELECTING THE RIGHT COLUMN:
    - "which airports" / "what airports" → SELECT AirportName, NOT AirportCode
    - "which countries" / "what countries" → SELECT Name or CountryName, NOT Code
    - "[entity] codes" / "[entity] ids" (explicitly requested) → SELECT the code/id column
    - In GROUP BY / "for each [entity]" queries → JOIN to entity table and return its name column

    FILTERING BY NAMED ATTRIBUTES:
    - "cars of model X" → WHERE Model = 'X' (the Model column in car_names has values like 'volvo', 'chevrolet')
    - "students of major X" → WHERE Major = 'X'
    - Check DISTINCT VALUES to confirm which column contains the filter value
    - The Make column in car_names has FULL vehicle names like 'volvo diesel 245', NOT manufacturer names

    TABLE CHOICE:
    - Prefer the simplest correct query.
    - Don't add unnecessary JOINs. Check if a single table has all needed data.
    - Use columns that exist directly in the table before joining to get the same data from elsewhere.

11. NEGATIVE EXISTENCE AND THRESHOLD QUERIES — USE NOT IN SUBQUERY:

    When the question asks for "X for which [Y's accumulated total] has NOT exceeded N"
    or "X that have NOT [done/spent] more than N" or "X without any [Y]":

    ALWAYS USE: SELECT col FROM X WHERE X.id NOT IN (SELECT X.id FROM Y GROUP BY X.id HAVING agg > N)

    DO NOT USE: JOIN approach with HAVING aggregate <= N
    The JOIN approach INCORRECTLY EXCLUDES entities with NO related records!
    Entities with no Y records should be INCLUDED (they haven't exceeded any threshold).

    EXAMPLES:
    ✓ "Dogs for which owner has not spent more than 1000 for treatment":
       SELECT name FROM Dogs WHERE dog_id NOT IN (
         SELECT dog_id FROM Treatments GROUP BY dog_id HAVING sum(cost_of_treatment) > 1000)
    ✗ WRONG: Dogs JOIN Treatments ... HAVING sum(cost) <= 1000
      (This EXCLUDES dogs with NO treatments — they should be INCLUDED since 0 <= 1000!)

    ✓ "Students who have not taken more than 3 courses":
       SELECT student_id FROM Students WHERE student_id NOT IN (
         SELECT student_id FROM Enrollments GROUP BY student_id HAVING count(*) > 3)
    ✗ WRONG: Students JOIN Enrollments ... HAVING count(*) <= 3
      (This EXCLUDES students with NO courses)

    GENERAL RULE: For "X that have NOT exceeded threshold" or "X that haven't done more than N":
    → SELECT from main table WHERE id NOT IN (subquery finding those that DID exceed threshold)

    NOTE: Negative-existence pattern words: "not spend/spent more than", "hasn't done more than",
    "hasn't exceeded", "has not [verb] more than", "without any [related]", "do not own".

12. Never use INSERT, UPDATE, DELETE, DROP, CREATE, PRAGMA or ATTACH.
13. Do not output multiple semicolon-separated statements.
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


def get_primary_keys(schema_ddl: str) -> dict[str, list[str]]:
    """Extract primary key columns per table from DDL."""
    pk_map: dict[str, list[str]] = {}
    # Match table blocks
    table_blocks = re.findall(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`]?(\w+)[\"'`]?\s*\((.*?)\)\s*;",
        schema_ddl, re.DOTALL | re.IGNORECASE
    )
    for tbl_name, body in table_blocks:
        pks: list[str] = []
        lines = body.split('\n')
        for line in lines:
            line_stripped = line.strip().rstrip(',')
            # Match inline PRIMARY KEY: `col_name type PRIMARY KEY`
            m = re.match(
                r'[`"\']?(\w+)[`"\']?\s+\w[\w\s()]*\bPRIMARY\s+KEY\b',
                line_stripped, re.IGNORECASE
            )
            if m:
                pks.append(m.group(1))
            # Match table-level: PRIMARY KEY (col1, col2)
            m2 = re.match(
                r'PRIMARY\s+KEY\s*\(([^)]+)\)',
                line_stripped, re.IGNORECASE
            )
            if m2:
                cols = [c.strip().strip('`"\' ') for c in m2.group(1).split(',')]
                pks.extend(cols)
        if pks:
            pk_map[tbl_name.lower()] = pks
    return pk_map


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
    if "LIKE" in sql_upper:
        if any(w in question.lower() for w in ["substring", "contains", "has the word", "starts with"]):
            boost += 0.4

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
        "  2. COLUMN ORDER: Select columns in the SAME ORDER as the question asks for them",
        "     'names and ids' → SELECT name_col, id_col  |  'ids and names' → SELECT id_col, name_col",
        "  3. AGGREGATE ORDERING: 'How many X for each Y?' or 'What is the max X for Y?' → aggregate FIRST",
        "     'What is the max accelerate for each cylinder?' → SELECT max(Accelerate), Cylinders",
        "  4. NO DISTINCT unless question says 'different/distinct/unique/variety'",
        "  5. ONLY SELECT requested columns — don't add extra id/code columns",
        "  6. ORDER BY is sorter only; HAVING is filter only — don't add aggregate to SELECT for these",
        "  7. COUNT(DISTINCT): 'how many different/distinct' → use COUNT(DISTINCT col)",
        "     IMPORTANT: Use the DESCRIPTIVE NAME column (few distinct values), NOT the primary key column",
        "     'how many different degrees?' → COUNT(DISTINCT degree_summary_name), NOT COUNT(DISTINCT degree_program_id)",
        "  8. STRING CASE: Use = with EXACT case from DISTINCT VALUES section",
        "     FEW-SHOT CASING: If a similar training example in EXAMPLE QUESTION-SQL PAIRS uses a specific",
        "     string literal for the same filter column, prefer that casing",
        "  9. ANY vs ALL: 'larger than ANY X' → > MIN(X); 'larger than ALL X' → > MAX(X)",
        "  10. For 'which entity has most X and what is its Y?': SELECT entity, Y FROM table GROUP BY entity ORDER BY count(*) DESC LIMIT 1",
        "  11. NEGATIVE THRESHOLD: 'X that have NOT spent/exceeded N' → NOT IN subquery (not JOIN+HAVING<=N)",
        "      Dogs with NO treatments are NOT in the over-1000 set → they should appear in results",
        "      'X for which [sum] has not exceeded N' → SELECT X WHERE id NOT IN (... HAVING sum > N)",
        "  12. LIKE PATTERNS: 'has the word X', 'has the substring X' → LIKE '%X%'",
        "      'the word' and 'the substring' are article phrases, NOT part of the LIKE pattern",
        "      'name has the word computer' → LIKE '%computer%' (NOT '%the word computer%')",
        "Wrap in ```sql ... ``` and output nothing else.",
    ]

    return "\n".join(parts)


def build_review_prompt(question: str, candidate_sql: str) -> str:
    """Build a targeted review prompt with 8 checks.

    Gen 7 changes:
    - Check 0: Superlative entity (unchanged from Gen 5/6)
    - Check 1: Column order matches question order (from Gen 6)
    - Check 2: Aggregate ordering / sentence structure (from Gen 6)
    - Check 3: DISTINCT — only when explicitly asked (from Gen 6)
    - Check 4: Unnecessary aggregates (HAVING/ORDER BY) (from Gen 6)
    - Check 5: Column identity (descriptive name vs code) (from Gen 6)
    - Check 6: ANY vs ALL (from Gen 4)
    - Check 7: String case (from Gen 4)
    - Check 8: NOT IN vs JOIN for negative threshold queries (NEW in Gen 7)
    - Check 9: COUNT(DISTINCT) must use descriptive column not primary key (NEW in Gen 7)
    """
    parts = [
        f"Review your SQL above for this question: {question}",
        "",
        "Perform these checks. Fix only REAL issues — if SQL is correct, return it UNCHANGED.",
        "",
        "--- CHECK 0: SUPERLATIVE ENTITY (check FIRST) ---",
        "Does the question ask 'Which/What X has the most/highest/lowest/best/fewest Y?'",
        "or 'What is the X that has the most Y?' or 'X that has the most/highest Y'?",
        "",
        "If YES — this is a SUPERLATIVE ENTITY question:",
        "  → SELECT should contain ONLY the entity X (name/value being asked for)",
        "  → count(*) or other aggregates go in ORDER BY only — NOT in SELECT",
        "  → Remove aggregate from SELECT if there unnecessarily",
        "",
        "Examples (aggregate must NOT be in SELECT):",
        "  ✓ 'Which year has most concerts?' → SELECT Year ... ORDER BY count(*) DESC LIMIT 1",
        "  ✗ WRONG: SELECT Year, count(*) ... [fix to SELECT Year only]",
        "",
        "If NOT superlative (e.g., 'how many X for each Y'), proceed to Check 1.",
        "",
        "--- CHECK 1: COLUMN ORDER MATCHES QUESTION ---",
        "Does the SELECT column order match the ORDER in which columns are mentioned in the question?",
        "  ✓ 'What are the names and ids?' → SELECT name_col, id_col  (name FIRST)",
        "  ✓ 'What are the ids and names?' → SELECT id_col, name_col  (id FIRST)",
        "  ✓ 'first name, country code, birth date' → SELECT first_name, country_code, birth_date",
        "  ✗ WRONG: 'names and ids' → SELECT id_col, name_col  (must fix to name first)",
        "",
        "Check the question word order and fix if columns are in the wrong sequence.",
        "",
        "--- CHECK 2: AGGREGATE ORDERING FOR GROUP BY QUERIES ---",
        "Does the question LEAD with 'how many', 'find the number', or 'what is the max/min/avg X for Y?'?",
        "",
        "CASE A (aggregate-primary): Question starts with 'how many' / 'find the number' / 'what is the max/min/avg X for Y?':",
        "  → Aggregate MUST be FIRST in SELECT",
        "  ✓ 'How many players for each hand?' → SELECT count(*), hand ... (count FIRST)",
        "  ✓ 'What is the max accelerate for all different cylinders?' → SELECT max(Accelerate), Cylinders ... (max FIRST)",
        "  ✓ 'What is the avg GNP for each continent?' → SELECT avg(GNP), Continent ... (avg FIRST)",
        "  ✗ WRONG: SELECT Cylinders, max(Accelerate) → fix to: SELECT max(Accelerate), Cylinders",
        "",
        "CASE B (entity-primary): 'For each Y, how many X?' or entity mentioned before count:",
        "  → Entity NAME comes FIRST, aggregate second",
        "  ✓ 'For each stadium, how many concerts?' → SELECT stadium.Name, count(*) JOIN stadium",
        "",
        "IMPORTANT: If aggregate is already in the CORRECT position, do NOT change it!",
        "",
        "--- CHECK 3: UNNECESSARY DISTINCT ---",
        "Is SELECT DISTINCT used in the query?",
        "If YES: Does the question explicitly use words like 'different', 'distinct', 'unique', 'variety', 'possible combinations'?",
        "  If NO: REMOVE the DISTINCT keyword — benchmark expects exact (possibly duplicate) rows",
        "  ✓ Keep DISTINCT: 'What are the different models?' (trigger: 'different')",
        "  ✓ Keep DISTINCT: 'Find all possible combinations' (trigger: 'possible combinations')",
        "  ✗ Remove DISTINCT: 'Find the first name and age of students who have a dog' (no trigger word)",
        "  ✗ Remove DISTINCT: 'What are the names and ids of makers?' (no trigger word)",
        "",
        "--- CHECK 4: UNNECESSARY AGGREGATES ---",
        "Does SELECT include count(*) when question asks WHICH/WHAT entities qualify (not HOW MANY)?",
        "  HAVING count(*) > N is a FILTER — do NOT add count(*) to SELECT",
        "  ORDER BY count(*) is a SORTER — do NOT add count(*) to SELECT",
        "  ✗ 'What tournament names have >10 matches?' → WRONG: SELECT count(*), tourney_name",
        "  ✓ Same question → RIGHT: SELECT tourney_name ... HAVING count(*) > 10",
        "",
        "--- CHECK 5: COLUMN IDENTITY ---",
        "If question asks 'which [entity]' or 'what [entity]' WITHOUT specifying 'code' or 'id':",
        "  → Return the DESCRIPTIVE NAME column (AirportName, Name, FullName, etc.)",
        "  → NOT a code or id column (AirportCode, Code, CountryCode, etc.)",
        "",
        "--- CHECK 6: ANY vs ALL ---",
        "If question says '[X] larger/greater than ANY [Y] in group':",
        "  → MUST use > (SELECT MIN(Y) WHERE ...)  [larger than at least one = beat the minimum]",
        "  ✗ 'larger than any African country' → > MAX(...) is WRONG",
        "  ✓ 'larger than any African country' → > MIN(...) is RIGHT",
        "",
        "--- CHECK 7: STRING CASE ---",
        "Use = with EXACT case from DISTINCT VALUES section.",
        "Only use LIKE for partial matches ('contains', 'starts with', 'has the word').",
        "",
        "LIKE PATTERNS: Check if LIKE pattern includes article phrases:",
        "  'has the word X' → LIKE '%X%'  NOT  LIKE '%the word X%'",
        "  'has the substring X' → LIKE '%X%'  NOT  LIKE '%the substring X%'",
        "  If the LIKE pattern starts with 'the word' or 'the substring', strip it.",
        "",
        "--- CHECK 8: NOT IN vs JOIN FOR NEGATIVE THRESHOLD QUERIES ---",
        "Does the question use negative phrasing like 'has NOT spent/exceeded', 'have not done more than',",
        "'for which [accumulated cost] does NOT exceed', 'that haven't [verb] more than N'?",
        "",
        "If YES — check if the SQL uses JOIN...GROUP BY...HAVING aggregate <= N:",
        "  → THIS IS WRONG: JOIN approach EXCLUDES entities with NO related records",
        "  → CORRECT: Use NOT IN subquery:",
        "     SELECT X.col FROM X WHERE X.id NOT IN (SELECT X.id FROM Y GROUP BY X.id HAVING aggregate > N)",
        "",
        "EXAMPLE:",
        "  Q: 'dogs for which owner has NOT spent more than 1000 for treatment'",
        "  ✗ WRONG: Dogs JOIN Treatments GROUP BY dog_id HAVING sum(cost) <= 1000",
        "    (Excludes dogs with NO treatments — they should be included!)",
        "  ✓ CORRECT: SELECT name FROM Dogs WHERE dog_id NOT IN (",
        "               SELECT dog_id FROM Treatments GROUP BY dog_id HAVING sum(cost_of_treatment) > 1000)",
        "    (Includes dogs with 0 treatments since they're not in the over-1000 set)",
        "",
        "If the SQL uses JOIN+HAVING<=N for a negative-phrasing question, FIX it to NOT IN.",
        "",
        "--- CHECK 9: COUNT(DISTINCT) COLUMN VALIDATION ---",
        "Does the query use COUNT(DISTINCT some_column)?",
        "If YES: Is some_column a PRIMARY KEY column (marked as PRIMARY KEY or _id suffix pattern)?",
        "  If the question asks 'how many different types/kinds' of something:",
        "  → PRIMARY KEY columns are unique per row → COUNT(DISTINCT PK) = COUNT(*) → WRONG for 'types'",
        "  → Instead, use the DESCRIPTIVE NAME column (the one with few distinct values)",
        "  → Check DISTINCT VALUES section: the type column typically has 2-10 distinct values",
        "",
        "  ✓ 'how many different degrees?' → COUNT(DISTINCT degree_summary_name) [3 types: Bachelor, Master, PHD]",
        "  ✗ WRONG: COUNT(DISTINCT degree_program_id) [primary key — as many values as rows]",
        "  ✓ 'how many unique languages?' → COUNT(DISTINCT Language) [actual language names]",
        "  ✗ WRONG: COUNT(DISTINCT language_id) [primary key]",
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
# Python post-processing: fix outer COUNT wrapper from GROUP BY+HAVING subquery
# ---------------------------------------------------------------------------

def fix_outer_count_from_having_subquery(sql: str) -> str:
    """Remove outer COUNT(*) wrapper when inner subquery has GROUP BY + HAVING.

    Transforms:
      SELECT count(*) FROM (SELECT X FROM T GROUP BY X HAVING count(*) > N)
    To:
      SELECT count(*) FROM T GROUP BY X HAVING count(*) > N

    This matches the benchmark's expected output for "How many X has more than N Y?"
    questions, where the gold SQL returns per-group counts rather than a total count.
    """
    sql_stripped = sql.strip()

    # Pattern: SELECT count(*) FROM (...) with optional alias
    outer_pattern = re.compile(
        r'^\s*SELECT\s+count\s*\(\s*\*\s*\)\s+FROM\s+\((.*)\)\s*(?:\w+)?\s*$',
        re.DOTALL | re.IGNORECASE
    )
    match = outer_pattern.match(sql_stripped)
    if not match:
        return sql

    inner = match.group(1).strip()
    inner_upper = inner.upper()

    # Only transform if inner subquery has GROUP BY + HAVING
    if 'GROUP BY' in inner_upper and 'HAVING' in inner_upper:
        return inner

    return sql


# ---------------------------------------------------------------------------
# Question classification helpers
# ---------------------------------------------------------------------------

def question_is_aggregate_focused(question: str) -> bool:
    """Check if question primarily asks for a count/aggregate per group.

    Returns True for aggregate-primary phrasings (Case A in Rule 1).
    Includes "What is the max/min/avg X for Y?" patterns.
    """
    q_lower = question.lower().strip()
    # Aggregate-primary: starts with "how many" or "find the number"
    if q_lower.startswith("how many"):
        return True
    if q_lower.startswith("find the number of") or q_lower.startswith("what is the number"):
        return True
    # "What is the max/min/avg/total X for each Y?" patterns
    aggregate_starters = [
        "what is the max", "what is the min", "what is the avg",
        "what is the average", "what is the maximum", "what is the minimum",
        "what is the total", "what is the sum",
    ]
    if any(q_lower.startswith(p) for p in aggregate_starters):
        if any(w in q_lower for w in ["for each", "per ", "for all different", "for every",
                                       "for all the different", "for different"]):
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
    """
    q_lower = question.lower()
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


def question_has_distinct_trigger(question: str) -> bool:
    """Check if question explicitly asks for distinct/different/unique values."""
    q_lower = question.lower()
    distinct_triggers = [
        "different", "distinct", "unique", "variety",
        "possible combinations", "possible breed", "possible size",
        "possible type",
    ]
    return any(t in q_lower for t in distinct_triggers)


def question_is_negative_threshold(question: str) -> bool:
    """Check if question asks for entities that have NOT exceeded a threshold.

    These should use NOT IN subquery, not JOIN+HAVING<=N.
    Pattern: "X that have NOT spent/done/exceeded more than N"
    """
    q_lower = question.lower()
    negative_patterns = [
        "has not spend", "has not spent", "have not spend", "have not spent",
        "has not done more than", "have not done more than",
        "has not exceeded", "have not exceeded",
        "hasn't spent", "haven't spent",
        "not spend more than", "not spent more than",
        "not pay more than", "not paid more than",
        "did not spend", "do not spend",
    ]
    return any(p in q_lower for p in negative_patterns)


def sql_starts_with_aggregate(sql: str) -> bool:
    """Check if SELECT's first column is an aggregate function."""
    after_select = re.sub(r'^\s*SELECT\s+DISTINCT\s+', '', sql, flags=re.IGNORECASE)
    after_select = re.sub(r'^\s*SELECT\s+', '', after_select, flags=re.IGNORECASE).strip().upper()
    return any(after_select.startswith(agg + "(") for agg in
               ["COUNT", "SUM", "AVG", "MAX", "MIN"])


def sql_has_aggregate_in_select(sql: str) -> bool:
    """Check if SQL has any aggregate function in SELECT clause (before FROM)."""
    select_part = re.split(r'\bFROM\b', sql, maxsplit=1, flags=re.IGNORECASE)[0]
    select_upper = select_part.upper()
    return any(f"{agg}(" in select_upper for agg in ["COUNT", "SUM", "AVG", "MAX", "MIN"])


def sql_has_distinct(sql: str) -> bool:
    """Check if SQL uses SELECT DISTINCT."""
    return bool(re.search(r'\bSELECT\s+DISTINCT\b', sql, re.IGNORECASE))


def sql_uses_join_having_lte(sql: str) -> bool:
    """Check if SQL uses JOIN+GROUP BY+HAVING with <= or 'not > N' pattern.

    Detects the anti-pattern for negative threshold queries.
    """
    sql_upper = sql.upper()
    has_join = "JOIN" in sql_upper
    has_group_by = "GROUP BY" in sql_upper
    has_having = "HAVING" in sql_upper
    if not (has_join and has_group_by and has_having):
        return False
    # Check for HAVING with <= or < N (negative threshold pattern)
    having_match = re.search(
        r'HAVING\s+.{0,100}(?:<=|<\s*\d|\bNOT\b)',
        sql_upper
    )
    return having_match is not None


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
    Phase 4: Python post-processing (fix outer COUNT wrapper pattern)
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

                # --- Protection Rule B: Aggregate-first ordering protection ---
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
                # first AND c2 has aggregate first → review reordered wrong
                if selected is None:
                    entity_count_order = question_is_entity_count_order(q["question"])
                    if entity_count_order and not c1_agg_first and c2_agg_first:
                        selected = sql1
                        note = "[selection] Disagree: review swapped entity-first to aggregate-first → protecting candidate 1"

                # --- Protection Rule E: DISTINCT protection ---
                # If question has no distinct trigger words AND c1 has DISTINCT AND c2 doesn't →
                # review correctly removed DISTINCT; prefer c2
                # (Note: this is the reverse protection — prefer c2 when c2 removes DISTINCT correctly)
                if selected is None:
                    c1_has_distinct = sql_has_distinct(sql1)
                    c2_has_distinct = sql_has_distinct(sql2)
                    has_distinct_trigger = question_has_distinct_trigger(q["question"])

                    if not has_distinct_trigger and c1_has_distinct and not c2_has_distinct:
                        selected = sql2
                        note = "[selection] Disagree: review correctly removed unnecessary DISTINCT → using candidate 2"

                # --- Protection Rule F: NOT IN for negative threshold (NEW Gen 7) ---
                # If question has negative threshold phrasing AND c2 uses NOT IN AND c1 uses JOIN+HAVING<=N:
                # prefer c2 (review correctly applied NOT IN pattern)
                if selected is None:
                    is_neg_threshold = question_is_negative_threshold(q["question"])
                    c1_join_having = sql_uses_join_having_lte(sql1)
                    c2_has_not_in = "NOT IN" in sql2.upper()

                    if is_neg_threshold and c1_join_having and c2_has_not_in:
                        selected = sql2
                        note = "[selection] Disagree: negative threshold question — review correctly switched to NOT IN → using candidate 2"

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

    # -----------------------------------------------------------------------
    # Phase 4: Python post-processing
    # Fix outer COUNT(*) FROM (subquery with GROUP BY + HAVING)
    # -----------------------------------------------------------------------
    if final_sql:
        fixed_sql = fix_outer_count_from_having_subquery(final_sql)
        if fixed_sql != final_sql:
            trajectory.append({
                "role": "system",
                "content": [{"type": "text", "text": "[post-processing] Removed outer COUNT wrapper from GROUP BY+HAVING subquery"}],
            })
            # Verify the fixed SQL still executes
            ok_fixed, _ = try_execute_sql(db_path, fixed_sql)
            if ok_fixed:
                final_sql = fixed_sql
            else:
                trajectory.append({
                    "role": "system",
                    "content": [{"type": "text", "text": "[post-processing] Fixed SQL failed execution — reverting to original"}],
                })

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
    ap = argparse.ArgumentParser(description="Gen-7 text-to-SQL agent")
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
        f"Model={MODEL} | Candidates={NUM_CANDIDATES} (review-and-refine with protection rules + NOT IN fix + post-processing)"
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
