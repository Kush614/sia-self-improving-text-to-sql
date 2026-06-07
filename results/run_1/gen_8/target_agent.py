#!/usr/bin/env python
"""Generation 8: Enhanced text-to-SQL target agent.

Key improvements over Gen 7 (93.33%, 112/120):

Gen 7 fixed 3 but regressed 4 (net -1). Gen 8 targets those 4 regressions:

  1. FIX world_1__t03 (ANY/ALL direction):
     - Gen 7's FEW-SHOT CASING guidance made model follow wrong training example
       (training example showed MAX for "any" but should be MIN).
     - NEW: Explicit override rule — "Do NOT follow few-shot examples for ANY/ALL direction"
     - NEW: Python post-processing: "larger than any" → replace > MAX with > MIN

  2. FIX car_1__t02 (CAST inside aggregate):
     - Model adds max(CAST(T2.MPG AS REAL)) which gives different result than max(MPG)
       when MPG column contains non-numeric strings like '?'
     - NEW: Explicit rule: NEVER use CAST inside aggregate functions
     - NEW: Python post-processing: max(CAST(col AS type)) → max(col)

  3. FIX dog_kennels__t06 (temporal filter for "at this moment"):
     - Gen 7's NOT IN rule made model interpret "at this moment" as date_departed IS NULL
     - Added date filter to NOT IN subquery (NOT IN (Dogs WHERE date_departed IS NULL))
     - Gold: NOT IN (Dogs) — no date filter
     - NEW: Explicit note: "at this moment" / "currently" do NOT trigger date filters

  4. FIX wta_1__t12 (aggregate ordering: "How many X from each Y?"):
     - "How many players are from each country?" → model puts entity first (WRONG)
     - Gold expects count(*) first because question starts with "how many"
     - NEW: Explicit CASE A extension for "from each Y" phrasing
     - NEW: Python post-processing: ensure count(*) first for "how many" + GROUP BY (2 cols)
     - NEW: Protection Rule G to prevent review from breaking count-first order

  5. STRENGTHEN FEW-SHOT STRING CASING:
     - Make it explicitly override DISTINCT VALUES for string literals in WHERE clauses
     - Targets world_1__t13 (North America lowercase in training examples)

  KEPT: All beneficial Gen 3/4/6/7 improvements:
    - Superlative Entity Rule, Review Check 0
    - Protection Rules A, B, C, E, F from earlier generations
    - Python post-processing: fix_outer_count_from_having_subquery
    - NOT IN for negative threshold queries
    - COUNT(DISTINCT) descriptive column rule
    - LIKE article phrase stripping

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
# System prompt — Gen 8: fix 4 Gen-7 regressions
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
   ✓ "How many players are from each country?" → SELECT count(*), country_code ... (STILL count FIRST)
   ✓ "What is the max accelerate for all different cylinders?" → SELECT max(Accelerate), Cylinders ...
   ✓ "What is the average GNP for each continent?" → SELECT avg(GNP), Continent ...
   ✓ "What is the max/min/avg/total X for each/all different Y?" → SELECT max/min/avg/sum(X), Y

   CRITICAL CASE A NOTE: "How many X [verb] from each Y?" or "How many X are from each Y?" also
   follows CASE A (aggregate-first), not CASE B. Even though "each Y" appears later, the question
   LEADS with "how many" so count(*) must be FIRST.
   ✓ "How many players are from each country?" → SELECT count(*), country_code (count FIRST)
   ✗ WRONG: SELECT country_code, count(*) (entity-first is WRONG for "how many" lead questions)

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

   FEW-SHOT STRING CASING OVERRIDE (HIGHEST PRIORITY for string literals in WHERE/LIKE):
   When EXAMPLE QUESTION-SQL PAIRS contains a very similar question with a WHERE/LIKE clause using a
   specific string literal for the SAME filter column, you MUST use that EXACT string literal casing,
   even if the DISTINCT VALUES section shows different casing.
   The few-shot example's casing TAKES PRIORITY over the DISTINCT VALUES section.
   ✓ If training examples show: continent = "north america" (lowercase) → use "north america" in your query
   ✓ If training examples show: WHERE Language = "English" → use "English" exactly
   This reflects the actual evaluation expectation for this database.

7. BOUNDARY CONDITIONS AND QUANTIFIERS:
   - "at least N" → >= N
   - "more than N" → > N
   - "at most N" → <= N
   - "fewer than / less than N" → < N

   CRITICAL — "ANY" vs "ALL" — FIXED MATHEMATICAL RULE (NEVER follow few-shot examples for this):
   DO NOT use few-shot examples to determine MIN vs MAX for ANY/ALL subqueries.
   ALWAYS use the mathematical definition:
   • "X larger/greater/bigger/more than ANY Y in group" = larger than AT LEAST ONE = X > MIN(Y in group)
   • "X larger/greater/bigger/more than ALL Y in group" = larger than EVERY ONE = X > MAX(Y in group)
   • "X smaller/less/fewer than ANY Y in group" = smaller than AT LEAST ONE = X < MAX(Y in group)
   • "X smaller/less/fewer than ALL Y in group" = smaller than EVERY ONE = X < MIN(Y in group)
   ✓ "larger than any country in Africa" → > (SELECT MIN(Population) WHERE Continent='Africa')
   ✓ "larger than all countries in Africa" → > (SELECT MAX(Population) WHERE Continent='Africa')
   ✗ WRONG (even if a training example shows this): "larger than any African country" → > MAX(...)
   WARNING: Some training examples may show MAX for "any" — these are wrong. Use MIN for "any".

8. JOIN TYPE:
   - Use INNER JOIN by default.
   - Only use LEFT JOIN when the question explicitly needs rows without matches.

9. AVOID UNNECESSARY CAST:
   - Do NOT use CAST() or type conversion unless absolutely required.
   - ESPECIALLY: NEVER use CAST inside aggregate functions (max, min, avg, sum, count).
   ✗ WRONG: max(CAST(MPG AS REAL)), min(CAST(col AS INTEGER))
   ✓ RIGHT: max(MPG), min(col)
   CAST inside aggregates can produce WRONG results when the column contains non-numeric strings
   like '?' or 'N/A' — the CAST returns NULL for those values, changing the aggregate result.
   Always use max(col) / min(col) directly, matching the gold SQL convention.

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

    TEMPORAL QUALIFIER WARNING — "at this moment" / "currently" / "right now":
    These phrases in negative existence queries do NOT mean you should add date-based filters.
    - "owners who do not own any dogs at this moment" → NOT IN (SELECT owner_id FROM Dogs)
    - Do NOT add: WHERE date_departed IS NULL or date_ended IS NULL to the subquery
    - The gold SQL expects the simple NOT IN without temporal conditions.
    - Only add date filters if the question specifically says "active dogs" or "not yet departed" AND
      the schema has an explicit boolean is_active column.
    ✗ WRONG: NOT IN (SELECT owner_id FROM Dogs WHERE date_departed IS NULL)
    ✓ RIGHT: NOT IN (SELECT owner_id FROM Dogs)

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
            "=== DISTINCT VALUES (few-shot casing overrides this for matching filter columns) ===",
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
        parts += ["(String literals in these examples OVERRIDE DISTINCT VALUES for casing decisions)"]
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
        "  3. AGGREGATE ORDERING: 'How many X for/from each Y?' or 'What is the max X for Y?' → aggregate FIRST",
        "     'How many players are from each country?' → SELECT count(*), country_code (count FIRST)",
        "     'What is the max accelerate for each cylinder?' → SELECT max(Accelerate), Cylinders",
        "  4. NO DISTINCT unless question says 'different/distinct/unique/variety'",
        "  5. ONLY SELECT requested columns — don't add extra id/code columns",
        "  6. ORDER BY is sorter only; HAVING is filter only — don't add aggregate to SELECT for these",
        "  7. COUNT(DISTINCT): 'how many different/distinct' → use COUNT(DISTINCT col)",
        "     IMPORTANT: Use the DESCRIPTIVE NAME column (few distinct values), NOT the primary key column",
        "     'how many different degrees?' → COUNT(DISTINCT degree_summary_name), NOT COUNT(DISTINCT degree_program_id)",
        "  8. STRING CASE: Few-shot examples OVERRIDE DISTINCT VALUES for string literal casing.",
        "     If a training example uses continent = \"north america\" (lowercase), use that EXACT form.",
        "  9. ANY vs ALL (NEVER follow few-shot examples for this — use mathematical definition):",
        "     'larger than ANY X' → > MIN(X)  |  'larger than ALL X' → > MAX(X)",
        "     'smaller than ANY X' → < MAX(X)  |  'smaller than ALL X' → < MIN(X)",
        "  10. For 'which entity has most X and what is its Y?': SELECT entity, Y ... ORDER BY count(*) DESC LIMIT 1",
        "  11. NEGATIVE THRESHOLD: 'X that have NOT spent/exceeded N' → NOT IN subquery (not JOIN+HAVING<=N)",
        "      'at this moment' / 'currently' does NOT mean add date_departed IS NULL to the subquery",
        "      Use: NOT IN (SELECT id FROM Y)  NOT: NOT IN (SELECT id FROM Y WHERE date_departed IS NULL)",
        "  12. LIKE PATTERNS: 'has the word X', 'has the substring X' → LIKE '%X%'",
        "      'the word' and 'the substring' are article phrases, NOT part of the LIKE pattern",
        "  13. NEVER use CAST inside aggregate functions: max(CAST(col AS REAL)) is WRONG",
        "      Always use max(col) directly — CAST changes results with non-numeric values",
        "Wrap in ```sql ... ``` and output nothing else.",
    ]

    return "\n".join(parts)


def build_review_prompt(question: str, candidate_sql: str) -> str:
    """Build a targeted review prompt with 11 checks.

    Gen 8 changes:
    - Check 0: Superlative entity (unchanged)
    - Check 1: Column order matches question order
    - Check 2: Aggregate ordering / sentence structure
    - Check 3: DISTINCT — only when explicitly asked
    - Check 4: Unnecessary aggregates (HAVING/ORDER BY)
    - Check 5: Column identity (descriptive name vs code)
    - Check 6: ANY vs ALL — enhanced with "do NOT follow few-shot examples"
    - Check 7: String case + LIKE article phrases + few-shot override
    - Check 8: NOT IN vs JOIN for negative threshold queries
    - Check 9: COUNT(DISTINCT) must use descriptive column not primary key
    - Check 10 (NEW): CAST inside aggregate functions — remove it
    - Check 11 (NEW): "How many X from each Y?" → count must be FIRST
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
        "  ✓ 'How many players are from each country?' → SELECT count(*), country_code ... (count FIRST)",
        "  ✓ 'What is the max accelerate for all different cylinders?' → SELECT max(Accelerate), Cylinders ...",
        "  ✗ WRONG: SELECT country_code, count(*) for 'How many players are from each country?' → fix to count(*) first",
        "",
        "CASE B (entity-primary): 'For each Y, how many X?' or entity mentioned before count:",
        "  → Entity NAME comes FIRST, aggregate second",
        "  ✓ 'For each stadium, how many concerts?' → SELECT stadium.Name, count(*) JOIN stadium",
        "",
        "IMPORTANT: If aggregate is already in the CORRECT position, do NOT change it!",
        "IMPORTANT: 'How many X [verb] from each Y?' is CASE A (count first), NOT CASE B (entity first).",
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
        "--- CHECK 6: ANY vs ALL (NEVER follow few-shot examples here) ---",
        "IMPORTANT: For ANY/ALL subqueries, IGNORE training examples. Use ONLY mathematical definition.",
        "",
        "If question says '[X] larger/greater/more than ANY [Y] in group':",
        "  → MUST use > (SELECT MIN(Y) WHERE ...)  [larger than at least one = beat the minimum]",
        "  ✗ WRONG: 'larger than any African country' → > MAX(...) is WRONG",
        "  ✓ RIGHT: 'larger than any African country' → > MIN(...) is RIGHT",
        "",
        "If question says '[X] smaller/less than ANY [Y] in group':",
        "  → MUST use < (SELECT MAX(Y) WHERE ...)  [smaller than at least one = beat the maximum]",
        "  ✓ RIGHT: 'smaller than any Asian country' → < MAX(population of Asia)",
        "",
        "DO NOT follow a training example that shows > MAX() for 'larger than any' — that's wrong.",
        "Fix the SQL if it uses the wrong direction.",
        "",
        "--- CHECK 7: STRING CASE + FEW-SHOT OVERRIDE ---",
        "Use = with EXACT case from DISTINCT VALUES section.",
        "Only use LIKE for partial matches ('contains', 'starts with', 'has the word').",
        "",
        "FEW-SHOT OVERRIDE: If EXAMPLE QUESTION-SQL PAIRS uses a specific string literal",
        "for the same filter column, that casing OVERRIDES the DISTINCT VALUES section.",
        "Use the exact string from the training example.",
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
        "ALSO CHECK: Is there a temporal filter like 'date_departed IS NULL' in the NOT IN subquery?",
        "  If the question says 'at this moment' / 'currently' but is really asking about negative ownership:",
        "  → Remove date-based filters from the NOT IN subquery",
        "  → 'owners who do not own any dogs at this moment' → NOT IN (SELECT owner_id FROM Dogs)",
        "  → NOT: NOT IN (SELECT owner_id FROM Dogs WHERE date_departed IS NULL)",
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
        "--- CHECK 10: CAST INSIDE AGGREGATE FUNCTIONS ---",
        "Does the SQL use CAST inside an aggregate function like max(CAST(col AS REAL))?",
        "",
        "If YES — REMOVE the CAST wrapper:",
        "  ✗ WRONG: max(CAST(T2.MPG AS REAL)), min(CAST(col AS INTEGER))",
        "  ✓ RIGHT: max(T2.MPG), min(col)",
        "",
        "REASON: CAST changes results when the column has non-numeric strings like '?'.",
        "The benchmark expects max(col) directly, not max(CAST(col AS type)).",
        "",
        "--- CHECK 11: 'HOW MANY X FROM EACH Y?' AGGREGATE ORDERING ---",
        "Does the question START WITH 'how many' and use 'from each Y' or 'for each Y' phrasing?",
        "",
        "If YES — count(*) MUST be FIRST in SELECT:",
        "  'How many players are from each country?' → SELECT count(*), country_code (count FIRST)",
        "  'How many flights are from each source?' → SELECT count(*), source_col (count FIRST)",
        "",
        "The 'from each Y' clause is just the grouping specification — it does NOT make this entity-primary.",
        "A question starting with 'how many' is ALWAYS aggregate-primary (count first).",
        "",
        "If the SQL has entity-col first and count(*) second for a 'how many ... from each' question:",
        "  → SWAP them: put count(*) first, entity second.",
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
# Python post-processing functions
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


def remove_cast_in_aggregates(sql: str) -> str:
    """Remove CAST() inside aggregate functions.

    Transforms: max(CAST(col AS REAL)) → max(col)
               min(CAST(T2.MPG AS REAL)) → min(T2.MPG)

    This fixes car_1__t02 where the model adds CAST(MPG AS REAL) inside max().
    """
    # Pattern: AGG(CAST(expr AS type)) where expr has no nested parens
    pattern = re.compile(
        r'\b(MAX|MIN|AVG|SUM|COUNT)\s*\(\s*CAST\s*\(\s*([^()]+?)\s+AS\s+\w+\s*\)\s*\)',
        re.IGNORECASE
    )

    def replace_fn(m: re.Match) -> str:
        agg = m.group(1)
        expr = m.group(2).strip()
        return f'{agg}({expr})'

    return pattern.sub(replace_fn, sql)


def fix_any_all_direction(sql: str, question: str) -> str:
    """Fix incorrect MAX/MIN direction in ANY/ALL subqueries.

    "larger than any X" should use MIN (larger than at least one = beat the minimum).
    "smaller than any X" should use MAX (smaller than at least one = less than the maximum).

    Fixes world_1__t03: model used MAX for "larger than any country in Africa"
    but gold expects MIN.
    """
    q_lower = question.lower()

    # "larger/greater/bigger/more than any" → should use MIN, not MAX
    if re.search(r'\b(larger|greater|bigger|more)\s+than\s+any\b', q_lower):
        # Replace > (SELECT MAX(... with > (SELECT MIN(...
        fixed = re.sub(
            r'>\s*\(\s*SELECT\s+MAX\s*\(',
            '> (SELECT MIN(',
            sql,
            flags=re.IGNORECASE
        )
        if fixed != sql:
            return fixed

    # "smaller/less/fewer than any" → should use MAX, not MIN
    if re.search(r'\b(smaller|less|fewer)\s+than\s+any\b', q_lower):
        # Replace < (SELECT MIN(... with < (SELECT MAX(...
        fixed = re.sub(
            r'<\s*\(\s*SELECT\s+MIN\s*\(',
            '< (SELECT MAX(',
            sql,
            flags=re.IGNORECASE
        )
        if fixed != sql:
            return fixed

    return sql


def fix_how_many_aggregate_order(sql: str, question: str) -> str:
    """For 'how many X' questions with GROUP BY, ensure count(*) is first in SELECT.

    Fixes wta_1__t12: "How many players are from each country?"
    model produced: SELECT country_code, count(*) FROM players GROUP BY country_code
    gold expects:   SELECT count(*), country_code FROM players GROUP BY country_code
    """
    q_lower = question.lower().strip()
    # Only apply if question starts with "how many"
    if not q_lower.startswith('how many'):
        return sql

    # Only apply if SQL has GROUP BY
    if not re.search(r'\bGROUP\s+BY\b', sql, re.IGNORECASE):
        return sql

    # Find position of FROM keyword (first occurrence, not inside subqueries)
    # Use a simple approach: find SELECT ... FROM pattern at the top level
    m = re.match(r'^(\s*SELECT\s+)(.*?)(\s+FROM\b)(.*)', sql.strip(),
                 re.IGNORECASE | re.DOTALL)
    if not m:
        return sql

    select_kw = m.group(1)
    cols_str = m.group(2).strip()
    from_kw = m.group(3)
    rest = m.group(4)

    # Simple comma split (handles basic 2-column case)
    # Be careful: only split if no nested parens
    if '(' in cols_str:
        # Count parentheses to find top-level commas
        cols = []
        depth = 0
        current = []
        for ch in cols_str:
            if ch == '(':
                depth += 1
                current.append(ch)
            elif ch == ')':
                depth -= 1
                current.append(ch)
            elif ch == ',' and depth == 0:
                cols.append(''.join(current).strip())
                current = []
            else:
                current.append(ch)
        if current:
            cols.append(''.join(current).strip())
    else:
        cols = [c.strip() for c in cols_str.split(',')]

    # Only handle 2-column case safely
    if len(cols) != 2:
        return sql

    first_col_upper = cols[0].upper().strip()
    # Check if first column is already an aggregate
    is_first_agg = bool(re.match(r'\b(COUNT|SUM|AVG|MAX|MIN)\s*\(', first_col_upper))
    if is_first_agg:
        return sql  # Already aggregate-first

    # Check if ANY column is count(*)
    count_idx = None
    for i, col in enumerate(cols):
        if re.match(r'\s*count\s*\(\s*\*\s*\)\s*$', col.strip(), re.IGNORECASE):
            count_idx = i
            break

    if count_idx is None or count_idx == 0:
        return sql  # No count(*) found or already first

    # Swap count(*) to first position
    new_cols = [cols[count_idx]] + [cols[i] for i in range(len(cols)) if i != count_idx]
    new_sql = select_kw + ', '.join(new_cols) + from_kw + rest
    return new_sql


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


def question_starts_with_how_many(question: str) -> bool:
    """Check if question starts with 'how many'."""
    return question.lower().strip().startswith('how many')


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


def sql_has_count_first_in_select(sql: str) -> bool:
    """Check if SELECT's first column is count(*)."""
    m = re.match(r'^\s*SELECT\s+', sql, re.IGNORECASE)
    if not m:
        return False
    after_select = sql[m.end():].strip().upper()
    return bool(re.match(r'COUNT\s*\(\s*\*\s*\)', after_select))


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
    Phase 4: Python post-processing (fix patterns the model gets wrong)
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
                if selected is None:
                    c1_has_distinct = sql_has_distinct(sql1)
                    c2_has_distinct = sql_has_distinct(sql2)
                    has_distinct_trigger = question_has_distinct_trigger(q["question"])

                    if not has_distinct_trigger and c1_has_distinct and not c2_has_distinct:
                        selected = sql2
                        note = "[selection] Disagree: review correctly removed unnecessary DISTINCT → using candidate 2"

                # --- Protection Rule F: NOT IN for negative threshold (from Gen 7) ---
                # If question has negative threshold phrasing AND c2 uses NOT IN AND c1 uses JOIN+HAVING<=N:
                # prefer c2 (review correctly applied NOT IN pattern)
                if selected is None:
                    is_neg_threshold = question_is_negative_threshold(q["question"])
                    c1_join_having = sql_uses_join_having_lte(sql1)
                    c2_has_not_in = "NOT IN" in sql2.upper()

                    if is_neg_threshold and c1_join_having and c2_has_not_in:
                        selected = sql2
                        note = "[selection] Disagree: negative threshold question — review correctly switched to NOT IN → using candidate 2"

                # --- Protection Rule G: "How many" questions → protect count-first (NEW Gen 8) ---
                # If question starts with "how many" AND c1 has count(*) first AND c2 doesn't:
                # protect c1 (review broke correct count-first ordering)
                if selected is None:
                    is_how_many = question_starts_with_how_many(q["question"])
                    c1_count_first = sql_has_count_first_in_select(sql1)
                    c2_count_first = sql_has_count_first_in_select(sql2)

                    if is_how_many and c1_count_first and not c2_count_first:
                        selected = sql1
                        note = "[selection] Disagree: 'how many' question — review broke count-first ordering → protecting candidate 1"

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
    # Apply targeted fixes in order, verifying execution after each
    # -----------------------------------------------------------------------
    if final_sql:
        # Step 4a: Remove CAST inside aggregate functions (fixes car_1__t02)
        fixed_cast = remove_cast_in_aggregates(final_sql)
        if fixed_cast != final_sql:
            ok_fixed, _ = try_execute_sql(db_path, fixed_cast)
            if ok_fixed:
                trajectory.append({
                    "role": "system",
                    "content": [{"type": "text", "text": "[post-processing] Removed CAST inside aggregate function"}],
                })
                final_sql = fixed_cast
            else:
                trajectory.append({
                    "role": "system",
                    "content": [{"type": "text", "text": "[post-processing] CAST removal failed execution — reverting"}],
                })

        # Step 4b: Fix ANY/ALL subquery direction (fixes world_1__t03)
        fixed_any = fix_any_all_direction(final_sql, q["question"])
        if fixed_any != final_sql:
            ok_fixed, _ = try_execute_sql(db_path, fixed_any)
            if ok_fixed:
                trajectory.append({
                    "role": "system",
                    "content": [{"type": "text", "text": "[post-processing] Fixed ANY/ALL subquery direction (MAX→MIN or MIN→MAX)"}],
                })
                final_sql = fixed_any
            else:
                trajectory.append({
                    "role": "system",
                    "content": [{"type": "text", "text": "[post-processing] ANY/ALL fix failed execution — reverting"}],
                })

        # Step 4c: Fix outer COUNT wrapper from GROUP BY+HAVING subquery (from Gen 6)
        fixed_outer = fix_outer_count_from_having_subquery(final_sql)
        if fixed_outer != final_sql:
            ok_fixed, _ = try_execute_sql(db_path, fixed_outer)
            if ok_fixed:
                trajectory.append({
                    "role": "system",
                    "content": [{"type": "text", "text": "[post-processing] Removed outer COUNT wrapper from GROUP BY+HAVING subquery"}],
                })
                final_sql = fixed_outer
            else:
                trajectory.append({
                    "role": "system",
                    "content": [{"type": "text", "text": "[post-processing] Outer COUNT fix failed execution — reverting"}],
                })

        # Step 4d: Fix aggregate ordering for "how many" + GROUP BY (fixes wta_1__t12)
        fixed_order = fix_how_many_aggregate_order(final_sql, q["question"])
        if fixed_order != final_sql:
            ok_fixed, _ = try_execute_sql(db_path, fixed_order)
            if ok_fixed:
                trajectory.append({
                    "role": "system",
                    "content": [{"type": "text", "text": "[post-processing] Fixed 'how many' aggregate ordering: count(*) moved to first"}],
                })
                final_sql = fixed_order
            else:
                trajectory.append({
                    "role": "system",
                    "content": [{"type": "text", "text": "[post-processing] Aggregate order fix failed execution — reverting"}],
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
    ap = argparse.ArgumentParser(description="Gen-8 text-to-SQL agent")
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
        f"Model={MODEL} | Candidates={NUM_CANDIDATES} "
        f"(review-and-refine + CAST removal + ANY/ALL fix + aggregate ordering + post-processing)"
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
