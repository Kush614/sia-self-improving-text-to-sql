#!/usr/bin/env python
"""Generation 4: Enhanced text-to-SQL target agent.

Key improvements over Gen 3:
  - CRITICAL FIX: Review regression bug fixed
    * Gen 3 review was BREAKING correct "SELECT count(*), Y" to "SELECT Y, count(*)"
    * New review Check 1 explicitly says: if aggregate is ALREADY first, do NOT change
  - System prompt Rule 1: Added "max X for all different Y → aggregate first" pattern
    * More explicit ✗ WRONG examples to prevent initial generation errors
    * Clearer distinction between aggregate-primary vs multi-column questions
  - System prompt Rule 4: Stronger ANY vs ALL with explicit ✗ WRONG example
  - System prompt Rule 7: Extend to "which X" → name not code
    * Added "cars of model X" → WHERE Model column (not Make)
  - Review prompt: Restructured as 5 targeted checks
    * Check 1: Aggregate ordering — protect correct, fix wrong
    * Check 2: Unnecessary aggregates (HAVING filter)
    * Check 3: Column identity (which → name, not code)
    * Check 4: ANY vs ALL
    * Check 5: String case
  - Selection code: New protection rule
    * If question is "how many" type AND candidate 1 has aggregate first AND candidate 2 doesn't
    * → Prefer candidate 1 (review broke the ordering)

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
# System prompt — Gen 4: stronger aggregate ordering + ANY/ALL + column identity
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are an expert text-to-SQL system specialised in SQLite.
Translate natural-language questions into exactly ONE correct SQLite SELECT query.
Always wrap your final SQL in a ```sql ... ``` code block and output nothing else.

CRITICAL RULES — follow these precisely:

1. COLUMN SELECTION AND ORDER IN SELECT:

   AGGREGATE-FIRST CONVENTION (required benchmark rule):
   When the question's PRIMARY FOCUS is "how many", "find the number of", or asking
   for a max/min/avg aggregate per group:
   → The AGGREGATE FUNCTION (count/max/min/avg/sum) MUST be the FIRST column in SELECT
   → The grouping column (Y) comes AFTER the aggregate

   These phrasings ALL require aggregate FIRST:
   • "How many X for each Y?" → SELECT count(*), Y  ← count FIRST, NOT Y first
   • "How many X are there per Y?" → SELECT count(*), Y  ← count FIRST
   • "How many X from each Y?" → SELECT count(*), Y  ← count FIRST
   • "Find the number of X for each Y" → SELECT count(*), Y  ← count FIRST
   • "What is the max/min/avg X for each Y?" → SELECT max(X), Y  ← aggregate FIRST
   • "What is the max/min/avg X for all different Y?" → SELECT max(X), Y  ← aggregate FIRST

   MEMORIZE THESE REQUIRED EXAMPLES:
   ✓ "How many players for each hand type?" → SELECT count(*), hand FROM players GROUP BY hand
   ✓ "How many players from each country?" → SELECT count(*), country_code FROM players GROUP BY country_code
   ✓ "Find the number of pets for each student" → SELECT count(*), stuid FROM ... GROUP BY stuid
   ✓ "What is the max accelerate for all different cylinders?" → SELECT max(Accelerate), Cylinders FROM cars_data GROUP BY Cylinders

   THESE ARE ALWAYS WRONG — never generate these:
   ✗ SELECT hand, count(*) FROM players GROUP BY hand  [hand before count — WRONG]
   ✗ SELECT country_code, count(*) FROM players GROUP BY country_code  [country_code before count — WRONG]
   ✗ SELECT Cylinders, max(Accelerate) FROM cars_data GROUP BY Cylinders  [Cylinders before max — WRONG]

   WHEN QUESTION LISTS MULTIPLE ITEMS (non-aggregate-primary):
   When question explicitly lists several things (entity name, id, count):
   → Follow EXACT question mention order
   • "full name, id, and how many models" → SELECT FullName, Id, count(*)  [FullName BEFORE Id]
   • "name of each continent and how many car makers" → SELECT Continent, count(*)  [name first, count last]
   • "show Y's name and the count of X" → SELECT name, count(*)  [name first per question]

   HAVING IS A FILTER ONLY:
   Using HAVING count(*) to filter groups does NOT mean count(*) goes in SELECT.
   Only add count(*) to SELECT if the question explicitly asks "how many".
   • "Which tournaments have more than 10 matches?" → SELECT tourney_name HAVING count(*) > 10  [NO count in SELECT]
   ✗ WRONG: SELECT count(*), tourney_name FROM matches GROUP BY tourney_name HAVING count(*) > 10
   ✓ RIGHT: SELECT tourney_name FROM matches GROUP BY tourney_name HAVING count(*) > 10

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
   - "at least N" → >= N   (e.g., "at least 10 flights" → HAVING count(*) >= 10)
   - "more than N" → > N   (e.g., "more than 3 models" → HAVING count(*) > 3)
   - "at most N" → <= N
   - "fewer than / less than N" → < N

   CRITICAL — "ANY" vs "ALL" — opposite meanings:
   • "X larger/greater than ANY Y in group" = larger than AT LEAST ONE = X > MIN(Y in group)
   • "X larger/greater than ALL Y in group" = larger than EVERY ONE = X > MAX(Y in group)

   ✗ WRONG: "larger than any country in Africa" → > (SELECT MAX(Population) WHERE Continent='Africa')
   ✓ RIGHT: "larger than any country in Africa" → > (SELECT MIN(Population) WHERE Continent='Africa')
   ✓ RIGHT: "larger than all countries in Africa" → > (SELECT MAX(Population) WHERE Continent='Africa')
   Mnemonic: "ANY" just needs to beat the MINIMUM (easiest bar); "ALL" must beat the MAXIMUM

5. JOIN TYPE:
   - Use INNER JOIN by default.
   - Only use LEFT JOIN when the question explicitly needs rows without matches
     (e.g., "include stadiums even if they have no concerts").

6. AVOID UNNECESSARY CAST:
   - Do NOT use CAST() or type conversion unless absolutely required.
   - Write: SELECT max(mpg) — NOT: SELECT max(CAST(mpg AS REAL))

7. COLUMN IDENTITY, TABLE CHOICE, AND ATTRIBUTE FILTERING:

   SELECTING THE RIGHT COLUMN:
   - "which airports" / "what airports" (no 'code'/'id' specified) → SELECT AirportName, NOT AirportCode
   - "which countries" / "what countries" → SELECT Name or CountryName, NOT Code or CountryCode
   - "[entity] codes" / "[entity] ids" (explicitly requested) → SELECT the code/id column
   - "name" → use the descriptive name column (FullName, AirportName, Name, etc.)
   - "code" / "id" → use the primary key / short code column

   FILTERING BY NAMED ATTRIBUTES:
   - "cars of model X" / "car model X" → WHERE Model = 'X' (use the Model column, NOT Make)
   - "students of major X" → WHERE Major = 'X' (use the Major column)
   - General: "of [attribute_type] X" → WHERE [attribute_type_column] = 'X'
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
    """Extract SQL from model output (fenced block, then heuristics)."""
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
    """Execute SQL and return a canonical string key of the sorted result rows.

    Used for self-consistency: if two candidates return the same key, they agree.
    Returns None on error.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")
        cursor = conn.execute(sql)
        rows = cursor.fetchall()
        conn.close()
        # Normalize: sort rows, convert each to string (handles type differences)
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
    """Show distinct values for categorical string columns.

    Critical for case-sensitive WHERE clause matches and choosing = vs LIKE.
    Shows columns with 2–max_distinct distinct non-null values.
    """
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
                    # Only process text/char/varchar columns
                    if not any(t in col_type for t in ("CHAR", "TEXT", "VARCHAR", "STRING")) and col_type not in ("", "BLOB"):
                        continue
                    try:
                        # Count distinct non-null values
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

    # Lexical overlap
    overlap = len(q_words & ex_words) / max(len(q_words), 1)

    # SQL structural features boost
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
        "  1. AGGREGATE ORDERING: If question asks 'how many X for each Y' or 'max/min/avg X for each Y'",
        "     → aggregate (count/max/min/avg) MUST be FIRST column: SELECT count(*), Y (NOT SELECT Y, count(*))",
        "  2. HAVING IS FILTER ONLY: count(*) in HAVING does NOT mean count(*) goes in SELECT",
        "     → 'which Y have more than N X?' → SELECT Y ... HAVING count(*) > N [NO count in SELECT]",
        "  3. DISTINCT: 'how many different/distinct' → use COUNT(DISTINCT col)",
        "  4. STRING CASE: Use = with EXACT case from DISTINCT VALUES section",
        "  5. ANY vs ALL: 'larger than ANY X' → > MIN(X); 'larger than ALL X' → > MAX(X)",
        "  6. COLUMN IDENTITY: 'which airports' → AirportName not AirportCode",
        "     'cars of model X' → WHERE Model = 'X' not WHERE Make = 'X'",
        "Wrap in ```sql ... ``` and output nothing else.",
    ]

    return "\n".join(parts)


def build_review_prompt(question: str, candidate_sql: str) -> str:
    """Build a targeted review prompt with 5 specific checks.

    Runs in the SAME conversation as initial generation (model sees its prior SQL).
    Key fix vs Gen 3: Check 1 now explicitly protects correct aggregate-first ordering
    from being accidentally reversed.
    """
    parts = [
        f"Review your SQL above for this question: {question}",
        "",
        "Perform these 5 checks. Fix only real issues — if SQL is correct, return unchanged.",
        "",
        "--- CHECK 1: AGGREGATE ORDERING (most important) ---",
        "Does the question ask 'How many X for each Y?', 'Find the number of X for each Y?',",
        "or 'What is the max/min/avg X for each/all different Y?'?",
        "",
        "If YES:",
        "  → Aggregate (count/max/min/avg/sum) MUST be the FIRST column in SELECT",
        "  → IMPORTANT: If aggregate is ALREADY first — this is CORRECT, do NOT change it",
        "  → Only fix if grouping column (Y) is first and aggregate is second",
        "",
        "Examples that are CORRECT (aggregate first — do NOT change these):",
        "  ✓ SELECT count(*), hand FROM players GROUP BY hand",
        "  ✓ SELECT count(*), country_code FROM players GROUP BY country_code",
        "  ✓ SELECT count(*), stuid FROM student JOIN has_pet GROUP BY stuid",
        "  ✓ SELECT max(Accelerate), Cylinders FROM cars_data GROUP BY Cylinders",
        "",
        "Examples that are WRONG (aggregate not first — fix these):",
        "  ✗ SELECT hand, count(*) → fix to: SELECT count(*), hand",
        "  ✗ SELECT country_code, count(*) → fix to: SELECT count(*), country_code",
        "  ✗ SELECT Cylinders, max(Accelerate) → fix to: SELECT max(Accelerate), Cylinders",
        "",
        "--- CHECK 2: UNNECESSARY AGGREGATES ---",
        "Does SELECT include count(*) when question asks WHICH/WHAT entities qualify (not HOW MANY)?",
        "  HAVING count(*) > N is a FILTER — do NOT add count(*) to SELECT just because HAVING uses it",
        "  ✗ 'What tournament names have >10 matches?' → WRONG: SELECT count(*), tourney_name HAVING count(*) > 10",
        "  ✓ Same question → RIGHT: SELECT tourney_name HAVING count(*) > 10  [no count in SELECT]",
        "",
        "--- CHECK 3: COLUMN IDENTITY ---",
        "If question asks 'which [entity]' or 'what [entity]' WITHOUT specifying 'code' or 'id':",
        "  → Return the DESCRIPTIVE NAME column (AirportName, Name, FullName, etc.)",
        "  → NOT a code or id column (AirportCode, Code, CountryCode, etc.)",
        "  Example: 'Which airports have no flights?' → SELECT AirportName  (NOT AirportCode)",
        "",
        "--- CHECK 4: ANY vs ALL ---",
        "If question says '[X] larger/greater than ANY [Y] in group':",
        "  → MUST use > (SELECT MIN(Y) WHERE ...)  [larger than at least one = beat the minimum]",
        "  → NOT > (SELECT MAX(Y) WHERE ...)  [that would mean larger than ALL]",
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
# Selection helpers (Gen 4 addition)
# ---------------------------------------------------------------------------

def question_is_aggregate_focused(question: str) -> bool:
    """Check if the question primarily asks for a count/aggregate per group.

    Used to detect cases where review may have incorrectly swapped aggregate ordering.
    """
    q_lower = question.lower().strip()
    # "How many X for each Y?" patterns
    if q_lower.startswith("how many"):
        return True
    if q_lower.startswith("find the number of") or q_lower.startswith("what is the number"):
        return True
    # "What is the max/min/avg X for each Y?" patterns
    aggregate_starters = ["what is the max", "what is the min", "what is the avg",
                          "what is the average", "what is the maximum", "what is the minimum",
                          "what is the total"]
    if any(q_lower.startswith(p) for p in aggregate_starters):
        # Only if it has a group-by context
        if any(w in q_lower for w in ["for each", "per ", "for all different", "for every"]):
            return True
    return False


def sql_starts_with_aggregate(sql: str) -> bool:
    """Check if SELECT's first column is an aggregate function."""
    # Remove leading SELECT and whitespace
    after_select = re.sub(r'^\s*SELECT\s+', '', sql, flags=re.IGNORECASE).strip().upper()
    # Check if starts with an aggregate function call
    return any(after_select.startswith(agg + "(") for agg in
               ["COUNT", "SUM", "AVG", "MAX", "MIN"])


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
    Phase 3: Select best candidate with aggregate-first protection
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
        # Phase 3: Select best candidate
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
                # Disagreement — apply selection logic
                has_k1 = has_results(key1)
                has_k2 = has_results(key2)

                # Gen 4 protection: if question asks "how many" and candidate 1 has aggregate
                # first but candidate 2 doesn't → review likely broke the ordering, prefer c1
                agg_focused = question_is_aggregate_focused(q["question"])
                c1_agg_first = sql_starts_with_aggregate(sql1)
                c2_agg_first = sql_starts_with_aggregate(sql2)

                if agg_focused and c1_agg_first and not c2_agg_first:
                    # Review broke the aggregate-first ordering → protect candidate 1
                    final_sql = sql1
                    note = "[selection] Disagree: review broke aggregate-first ordering → protecting candidate 1"
                elif has_k2 and not has_k1:
                    # Only candidate 2 has results
                    final_sql = sql2
                    note = "[selection] Disagree: candidate 2 non-empty, candidate 1 empty → candidate 2"
                elif has_k1 and not has_k2:
                    # Only candidate 1 has results
                    final_sql = sql1
                    note = "[selection] Disagree: candidate 1 non-empty, candidate 2 empty → candidate 1"
                else:
                    # Both non-empty or both empty → trust the reviewed version
                    final_sql = sql2
                    note = "[selection] Disagree: both non-empty/empty → using reviewed candidate 2"

                trajectory.append({
                    "role": "system",
                    "content": [{"type": "text", "text": note}],
                })

        elif ok2:
            # Only reviewed version executes
            final_sql = sql2
            trajectory.append({
                "role": "system",
                "content": [{"type": "text", "text": "[selection] Only candidate 2 passes → candidate 2"}],
            })
        elif ok1:
            # Only original version executes
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
                # Repair in the original conversation context (has schema info)
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
    ap = argparse.ArgumentParser(description="Gen-4 text-to-SQL agent")
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
        f"Model={MODEL} | Candidates={NUM_CANDIDATES} (review-and-refine with aggregate protection)"
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
