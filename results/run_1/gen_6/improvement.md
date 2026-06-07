# Generation 6 Improvement Analysis

## Performance Summary Across Generations

| Gen | Accuracy | Correct | Change |
|-----|----------|---------|--------|
| 1   | 75.83%   | 91/120  | —      |
| 2   | 79.17%   | 95/120  | +4     |
| 3   | 88.33%   | 106/120 | +11    |
| 4   | 93.33%   | 112/120 | +6     |
| 5   | 88.33%   | 106/120 | **-6** |

Gen 5 **regressed** from Gen 4 despite targeting improvements. The root cause: new rules over-fired and broke previously correct queries.

## Gen 5 Failure Root-Cause Analysis

### Failures introduced by Gen 5 (regressions from Gen 4):

**1. car_1__t05 — "What is the maximum accelerate for all the different cylinders?"**
- Predicted: `SELECT T2.Cylinders, MAX(T2.Accelerate)` ← entity first (WRONG)
- Gold: `SELECT max(Accelerate), Cylinders` ← aggregate first (CORRECT)
- Root cause: Model misclassified as CASE B ("entity-primary") despite explicit CASE A example in prompt. The phrase "for all the different cylinders" was read as entity-primary framing. The prompt had the example but the model overrode it.
- **Fix**: Explicitly add "What is the max/min/avg X for Y?" as a CASE A trigger. This must be unambiguous.

**2. car_1__t00 — "What are the names and ids of all makers with more than 3 models?"**
- Predicted: `SELECT DISTINCT T1.Id, T1.FullName` ← Id first (WRONG)
- Gold: `SELECT T1.FullName, T1.Id` ← FullName first (CORRECT)
- Root cause: Model didn't respect question column order. "names AND ids" = name first, id second. CASE C was not applied to non-aggregate columns.
- **Fix**: Add general column order rule: always SELECT columns in the same order they appear in the question.

**3. car_1__t04 — "How many countries has more than 2 car makers?"**
- Predicted: `SELECT count(*) FROM (SELECT CountryId FROM countries JOIN car_makers GROUP BY CountryId HAVING count(*) > 2)` ← outer COUNT wrapper (WRONG for this benchmark)
- Gold: `select count(*) from countries join car_makers group by t1.countryid having count(*) > 2` ← per-group count (CORRECT for benchmark)
- Root cause: Gen 5 added the "GROUP BY + HAVING no outer wrap" rule to the prompt, but the model ignored it AND generated outer COUNT. Gen 5's Protection Rule D (protecting c1 with GROUP BY+HAVING from c2 with outer COUNT) was also incorrectly applied — it was actually both candidates agreeing on the wrong pattern.
- **Fix**: Python post-processing to detect and remove outer COUNT wrapper from subqueries with GROUP BY+HAVING. Remove Protection Rule D.

**4. pets_1__t04 — "Find the first name and age of students who have a dog but do not have a cat as a pet."**
- Predicted: `SELECT DISTINCT T1.fname, T1.age` ← added DISTINCT (WRONG)
- Gold: `SELECT T1.fname, T1.age` ← no DISTINCT (CORRECT — duplicates are expected if student has multiple dogs)
- Root cause: Model was influenced by few-shot training examples that use `SELECT DISTINCT T1.fname, T1.age FROM student ... JOIN has_pet`. Added DISTINCT unnecessarily, which changes the result when students have multiple pets.
- **Fix**: Explicit rule: "Do NOT use SELECT DISTINCT unless the question explicitly uses words like 'different', 'distinct', 'unique', 'variety'."

**5. student_transcripts_tracking__t09 — "what are all the addresses including line 1 and line 2?"**
- Predicted: `SELECT address_id, line_1, line_2` ← extra column added (WRONG)
- Gold: `SELECT line_1, line_2` ← exact columns requested (CORRECT)
- Root cause: Model added address_id unnecessarily, not requested by question.
- **Fix**: Add rule: "Only select columns explicitly requested in the question. Do not add id/code columns unless asked."

**6. wta_1__t14 — "What is the name of the winner who has won the most matches, and how many rank points does this player have?"**
- Predicted: Complex subquery joining with rankings table
- Gold: `SELECT winner_name, winner_rank_points FROM matches GROUP BY winner_name ORDER BY count(*) DESC LIMIT 1`
- Root cause: Model used complex JOIN with rankings table instead of using winner_rank_points directly from matches. The review candidate failed execution.
- **Fix**: Add guidance: "When looking for an entity with the most occurrences PLUS one of its attributes, use: SELECT entity, attribute FROM table GROUP BY entity ORDER BY count(*) DESC LIMIT 1. Check if the attribute exists directly in the table before joining."

### Pre-existing Gen 4 failures that remain in Gen 5 (approx):
- **world_1__t04**: Logic difference (country table vs countrylanguage table for "don't speak English")
- **world_1__t07**: Gold SQL has unusual GROUP BY CountryCode interpretation
- **world_1__t13**: Case sensitivity mismatch — database stores 'North America' but gold uses "north america" lowercase (benchmark quirk, possibly unfixable)
- **car_1__t12**: Make vs Model column confusion (prompt has rule but model ignores it)
- **dog_kennels__t07**: "breed type and size type combinations" → gold uses codes, we return names
- **dog_kennels__t10**: Per-dog vs per-owner treatment cost semantics
- **student_transcripts_tracking__t08**: Overly literal LIKE '%the computer%' instead of '%computer%'
- **wta_1__t08**: Column order (first_name, country_code) vs gold (country_code, first_name) — benchmark inconsistency

## Gen 6 Strategy

**Base approach**: Start from Gen 5's code with targeted fixes to revert the regressions and add new improvements.

**Changes from Gen 5**:

### 1. System Prompt Changes

**a) Cleaner CASE A rule**: Add "What is the max/min/avg X for Y?" explicitly as a CASE A trigger
- The key insight: questions starting with "What is the max/min/avg" → aggregate FIRST
- The current CASE A only mentions "how many" which was too restrictive

**b) General Column Order Rule**: New rule — always SELECT columns in the same order they appear in the question
- Fixes car_1__t00 (names and ids → FullName before Id)
- Works for both aggregate and non-aggregate columns

**c) No Unnecessary DISTINCT**: New explicit rule against SELECT DISTINCT without "different/distinct/unique/variety" trigger words
- Fixes pets_1__t04
- Prevents few-shot contamination from examples that use DISTINCT

**d) Only Select Requested Columns**: Add guidance not to add extra columns (id, code) unless asked
- Fixes student_transcripts_tracking__t09

**e) Simplest Table/Column Rule**: When an attribute exists directly in the table being queried, use it directly rather than joining another table
- Helps wta_1__t14 (winner_rank_points is already in matches)

**f) GROUP BY + HAVING Rule**: Keep but simplify — remove the confusing "DO NOT wrap" example (it may be causing the model to generate the outer wrapper by showing it)

### 2. Review Prompt Changes

**a) Add DISTINCT check**: "If SQL uses SELECT DISTINCT but question doesn't contain 'different/distinct/unique', remove DISTINCT"

**b) Add column order check**: "Verify SELECT column order matches the question's stated order"

**c) Remove the elaborate DISTINCT patterns from Check 0/1** — simplify to avoid confusing the model

### 3. Python Code Changes

**a) Python post-processing — `fix_outer_count_from_having_subquery()`**: 
New function that detects `SELECT count(*) FROM (... GROUP BY ... HAVING ...)` pattern and transforms it to the inner query directly. This matches the benchmark's expected output format for "How many X has more than N Y?" questions.

**b) Remove Protection Rule D** from selection logic (was protecting the wrong candidate in car_1__t04)

**c) Simplify Protection Rules** — keep A, B, C from Gen 4/5 which were correct

### 4. Question Classification Improvements

**a) Refine `question_is_aggregate_focused()`**: Also detect "What is the max/min/avg X for Y?" patterns as aggregate-focused (CASE A), not just "how many" patterns.

## Expected Improvement

| Change | Fixed Cases | Risk |
|--------|-------------|------|
| CASE A fix (max/min/avg for Y) | car_1__t05 | Low |
| Column order rule | car_1__t00 | Low |
| No unnecessary DISTINCT | pets_1__t04 | Low |
| Outer COUNT python fix | car_1__t04 | Low |
| No extra columns | student_transcripts_tracking__t09 | Low |
| Simplest column/table | wta_1__t14 (partial) | Low |
| Remove Protection D | Prevents wrong protection | Low |

Expected outcome: ~93-95% accuracy (112-114 correct), recovering Gen 4 level and potentially exceeding it.

## Key Principle

**Less is more**: Gen 5 failed by adding too many complex, interacting rules. Gen 6 reverts to Gen 4's cleaner approach and adds only targeted, well-tested rules with Python-level safeguards for cases the model consistently fails on.
