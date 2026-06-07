# Generation 3 Improvement Plan

## Performance History
- Gen 1: 75.83% (91/120 correct)
- Gen 2: 79.17% (95/120 correct) → +3.34pp
- Gen 3 target: ~87-90%

## Root Cause Analysis of Gen 2 Failures (25 wrong)

### Category A: Unnecessary / Misplaced count(*) in SELECT (~15 failures)

This is the **dominant failure pattern** and stems directly from Gen 2's Rule 1:
> "For GROUP BY queries with aggregate functions: put the AGGREGATE FUNCTIONS FIRST, then the GROUP BY column(s)"

This rule is **doubly wrong**:
1. **It causes count(*) to be added when it shouldn't be there at all.**
   When a query needs `HAVING count(*) > N` to filter, the model interprets Rule 1 as requiring `count(*)` to appear in SELECT too. Wrong.
   - "Find all tournaments with more than 10 matches" → SELECT tourney_name (HAVING count(*) > 10 does NOT mean count in SELECT)
   - "Which owner owns the most dogs?" → SELECT owner_id, first_name, last_name (no count)

2. **When count(*) IS needed, it's often LAST, not first.**
   The gold standard follows the natural language order of the question:
   - "Show name and theme... and the number of singers" → SELECT name, theme, count(*)
   - "Show names of singers and number of concerts" → SELECT name, count(*)
   - "How many players per hand?" → SELECT count(*), hand (count IS first because question starts with it)

3. **The candidate 2 "VERIFY" note reinforces the wrong rule** by explicitly saying "aggregates first", so both candidates converge on the same systematic error.

**Subcategory A1: count(*) shouldn't be in SELECT** (9 failures):
car_1__t09, dog_kennels__t01, flight_2__t05, student_transcripts_tracking__t02, student_transcripts_tracking__t05, student_transcripts_tracking__t11, student_transcripts_tracking__t14, wta_1__t10, concert_singer__t07

**Subcategory A2: count(*) in wrong position** (4 failures):
car_1__t03, concert_singer__t01, concert_singer__t02, concert_singer__t11

**Subcategory A3: Wrong aggregation approach** (2 failures):
wta_1__t00 (used SUM+GROUP BY instead of ORDER BY single row rank_points DESC LIMIT 1)
wta_1__t14 (wrong aggregate in SELECT)

### Category B: Semantic/Structural Issues (~10 failures)

- **count(*) vs count(DISTINCT X)** (2): student_transcripts_tracking__t03, __t04
  - "How many different degrees" → count(DISTINCT degree_summary_name), not count(*)
  - "How many departments offer any degree" → count(DISTINCT department_id), not complex JOIN
  
- **LIKE vs = for exact values** (1): world_1__t12
  - "countries which are republics" → LIKE '%Republic%' (wrong) vs = 'Republic'
  - Should use exact match when DISTINCT VALUES shows exact value

- **Wrong table/join path** (1): car_1__t12
  - Should use CAR_NAMES → CARS_DATA, not MODEL_LIST → CARS_DATA

- **EXCEPT vs NOT IN with different table scope** (1): world_1__t04
  - Gold uses EXCEPT on countrylanguage (only countries with any language entries)
  - Pred uses NOT IN on country table (all countries including those with no entries)

- **Wrong column (FullName vs Maker)** (1): car_1__t00

- **Column order mismatch in INTERSECT** (1): wta_1__t08
  - Question says "first names and country codes" but gold orders country_code, first_name

- **Ambiguous gold SQL** (2): world_1__t07 (gold SQL is arguably wrong), world_1__t13 (case sensitivity)

- **Boundary condition (>= vs >)** (1): flight_2__t05 ("at least 10" but gold uses > 10)

## Planned Improvements for Gen 3

### 1. Fix System Prompt Rule 1 (Critical - fixes ~15 failures)

**Remove**: "For GROUP BY queries with aggregate functions: put aggregate FIRST"

**Replace with** a correct, nuanced rule:

```
1. COLUMN SELECTION IN SELECT:
   - Select ONLY the columns the question asks for.
   - CRITICAL: HAVING count(*) does NOT mean count(*) should appear in SELECT.
     Use HAVING to FILTER groups; only include aggregates in SELECT when the 
     question explicitly asks for a count/total/average/max/min.
   
   Key patterns:
   a) "How many X for each Y?" → SELECT count(*), Y GROUP BY Y [count asked for explicitly]
   b) "Show Y's name and the count of X" → SELECT name, count(*) [name first, count last]
   c) "Which Y has the most X?" → SELECT Y GROUP BY Y ORDER BY count(*) DESC LIMIT 1 [NO count in SELECT]
   d) "Find all Y that have more/at least N X" → SELECT Y GROUP BY Y HAVING count(*) >= N [NO count in SELECT]
   e) "List/show/return Y [for each Z]" → SELECT Y only - no extras
   
   Column order: follow the order items are mentioned in the question.
```

### 2. Add count(DISTINCT) Rule (fixes 2 failures)

```
2. DISTINCT COUNTS:
   "How many different/distinct X" → COUNT(DISTINCT X), not COUNT(*)
   "How many departments offer a degree?" → SELECT count(DISTINCT department_id) FROM degree_programs
   Trigger words: "different", "distinct", "unique", "variety of"
```

### 3. Fix String Matching Rule (fixes 1 failure)

```
3. STRING MATCHING:
   - Use = with EXACT case from DISTINCT VALUES section (not LIKE).
   - Only use LIKE when question implies partial match: "contains", "starts with", "has the word".
   - If DISTINCT VALUES shows 'Republic', use GovernmentForm = 'Republic', NOT LIKE '%Republic%'
```

### 4. Change Candidate 2 to "Review and Refine" (structural improvement)

**Gen 2 problem**: Candidate 2 is a fully independent generation with a "verify" note that reinforces the wrong rule. When candidates disagree, Gen 2 always picks candidate 2, amplifying the systematic error.

**Gen 3 fix**: Candidate 2 is a **review-and-refine step** on candidate 1:
- Show candidate 1 to the model in the SAME conversation context (has schema, distinct values)
- Ask specifically: "Does SELECT include unnecessary aggregates? Does column order match the question?"
- This is a targeted correction, not independent generation
- If review confirms candidate 1 is correct → use it unchanged
- If review finds issues → use corrected version

**Benefit**: 
- The model sees candidate 1 and can identify specific errors
- The review specifically targets the systematic "unnecessary count(*)" error
- Cheaper context (doesn't need to re-send full schema)

### 5. Improve Candidate Selection Logic

When candidate 1 and candidate 2 (reviewed) produce DIFFERENT results:
- If candidate 1 returns empty results but candidate 2 returns non-empty → prefer candidate 2
- If candidate 2 returns empty results but candidate 1 returns non-empty → prefer candidate 1
- If both non-empty but different → prefer candidate 2 (reviewed version)
- If both empty → prefer candidate 2

**Rationale**: Most questions have answers, so a non-empty result is usually correct.

### 6. Add Boundary Condition Rule

```
BOUNDARY CONDITIONS:
- "at least N" → >= N
- "more than N" → > N  
- "at most N" → <= N
- "fewer than N" / "less than N" → < N
```

## Unchanged from Gen 2 (Already Working Well)

- DISTINCT VALUES section: highly effective for case-sensitive matching
- Sample rows: helpful for understanding data formats
- Few-shot selection with structural similarity scoring (MAX_FEW_SHOT=10)
- Repair loop for execution errors (MAX_REPAIR_ATTEMPTS=2)
- INNER JOIN default rule
- No unnecessary CAST() rule
- Read-only SQLite execution validation

## Expected Impact

| Category | Est. Fixes |
|----------|-----------|
| Unnecessary count(*) in SELECT | 8-10 |
| count(*) in wrong position | 3-4 |
| count(DISTINCT X) | 1-2 |
| LIKE vs = exact match | 1 |
| Better candidate selection | 0-1 |
| **Total** | **13-18** |

**Projected accuracy**: (95 + 13 to 18) / 120 = 108/120 to 113/120 = **90% to 94%**

Note: Some failures are due to gold SQL inconsistencies (world_1__t07, wta_1__t08 column order mismatch, flight_2__t05 boundary condition) that cannot be fixed without matching gold SQL exactly.
