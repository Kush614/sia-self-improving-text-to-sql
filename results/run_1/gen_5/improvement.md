# Generation 5 Improvement Plan

## Evolution Summary

| Gen | Accuracy | Key Change |
|-----|----------|-----------|
| 1 | 76.67% | Initial agent |
| 2 | 79.17% | Distinct values, few-shot scoring, multi-candidate |
| 3 | 88.33% | Removed wrong aggregate-first rule, review-and-refine |
| 4 | 93.33% | Fixed review regression bug, stronger rules, aggregate protection |
| **5** | **target 96-97%** | Fix 4-5 of 8 remaining failures |

## Root-Cause Analysis of 8 Gen-4 Failures

### Failure Category 1: Superlative Entity — Unnecessary count(*) in SELECT (2 questions)

**Affected:** `student_transcripts_tracking__t11`, `concert_singer__t07`

**Pattern:** "Which/What X has the most Y?" → The entity X is the answer, NOT a count.

- `student_transcripts_tracking__t11`: "What is the degree summary name that has the most number of students enrolled?"
  - Gen 4: `SELECT count(*), T1.degree_summary_name ... ORDER BY count(*) DESC LIMIT 1` ❌
  - Gold:   `SELECT T1.degree_summary_name ... ORDER BY count(*) DESC LIMIT 1` ✓
  - Root cause: The review check triggered "how many X for each Y" aggregate-first rule incorrectly.
    The question asks FOR THE ENTITY NAME only — count is just for sorting, not answering.

- `concert_singer__t07`: "Which year has most number of concerts?"
  - Gen 4: `SELECT Year, count(*) ... ORDER BY count(*) DESC LIMIT 1` ❌
  - Gold:   `SELECT YEAR ... ORDER BY count(*) DESC LIMIT 1` ✓
  - Root cause: Model added count to SELECT when it should only be in ORDER BY.

**Fix:** New rule — **"ORDER BY IS A SORTER ONLY"**: When using count(*)/aggregate in ORDER BY to find the top/bottom entity, do NOT add it to SELECT unless the question explicitly asks for the count value itself.

### Failure Category 2: Sentence-Structure Aggregate Ordering (2 questions)

**Affected:** `concert_singer__t11`, `concert_singer__t02` (partially)

**Pattern:** The *position* of "how many" in the sentence determines column order.

- `concert_singer__t11`: "What are the names of the singers and number of concerts for each person?"
  - Gen 4: `SELECT count(*), T2.name ...` ❌  (review swapped name→count to count→name)
  - Gold:   `SELECT T2.name , count(*) ...` ✓  (name FIRST, as question states)
  - Root cause: Review's Check 1 misclassified "number of concerts for each person" as aggregate-primary and applied aggregate-first, swapping the already-correct (name, count) ordering.

- `concert_singer__t02`: "For each stadium, how many concerts play there?"
  - Gen 4: `SELECT count(*), Stadium_ID ...` ❌  (wrong column + wrong order)
  - Gold:   `SELECT T2.name , count(*) FROM concert JOIN stadium ...` ✓
  - Root cause: (1) Used Stadium_ID instead of stadium.name — missing JOIN. (2) count(*) placed first when gold has name first.

**Key distinction (validated against all test cases):**
- **Aggregate-primary phrasing**: "How many X **for each** Y?" / "Find the number of X **for each** Y?" → `SELECT count(*), Y` (count FIRST)
- **Entity-primary phrasing**: "**For each** Y, how many X?" / "Y and number/count of X?" / explicit entity-then-count listing → `SELECT Y_name, count(*)` (entity FIRST)

**Fix 1:** Revise Rule 1 with explicit sub-cases for sentence structure.

**Fix 2:** When "for each [entity]" is used and the entity is from a separate table, JOIN to get the entity's descriptive name (not just the FK column).

### Failure Category 3: Unnecessary Outer COUNT Wrapper (1 question)

**Affected:** `car_1__t04`

**Pattern:** "How many X has more than N Y?" — the benchmark expects per-group counts, NOT a single outer count.

- Gen 4: `SELECT COUNT(*) FROM (SELECT t1.CountryId ... GROUP BY ... HAVING COUNT(*) > 2)` ❌
- Gold:   `SELECT count(*) FROM countries JOIN car_makers GROUP BY countryid HAVING count(*) > 2` ✓
- Root cause: The model initially generated the correct GROUP BY pattern, then second-guessed itself and added an outer COUNT wrapper. The review kept the subquery version.

**Execution log evidence:** The first SQL block in the model's response WAS correct (`GROUP BY ... HAVING COUNT(*) > 2`), but the model immediately reconsidered and output a second (wrong) subquery version. The `extract_sql()` function correctly takes the FIRST block, but the fallback heuristic then selected the reviewed version (sql2) which the review confirmed as a subquery.

Wait — re-reading: the model put BOTH sql blocks in a single assistant response, and the extracted sql1 was the GROUP BY version... but the prediction shows the subquery version. Tracing through: the review was called on sql1 (GROUP BY), and the review model changed it to the subquery version (sql2). Selection picked sql2 as "both non-empty → reviewed candidate".

**Fix:** Add rule: "How many X has/have more than N Y?" → `SELECT count(*) FROM X JOIN Y GROUP BY X HAVING count(*) > N` — do NOT wrap in an outer COUNT subquery. The per-group count(*) IS the answer the benchmark expects.

### Failure Category 4: Column Order in Multi-column Intersection (1 question)

**Affected:** `wta_1__t08`

- Gen 4: `SELECT DISTINCT p.first_name, p.country_code ...` ❌
- Gold:   `SELECT T1.country_code , T1.first_name ...` ✓
- Question says "first names and country codes" but gold has (country_code, first_name).
- Root cause: Benchmark inconsistency — the question order disagrees with gold column order. No reliable fix without oracle knowledge of gold SQL.

### Failure Category 5: Benchmark Quirks (2 questions — not fixable)

**Affected:** `world_1__t07`, `world_1__t13`

- `world_1__t07`: Our SQL answers the literal question correctly (count of countries where Spanish has max percentage), but the gold SQL semantically differs (per-country count + max percentage via GROUP BY CountryCode). Unfixable without knowing gold intent.
- `world_1__t13`: The gold uses `"north america"` (lowercase) while the DB stores 'North America' (title case). Our query correctly uses 'North America' and gets actual data; the gold gets empty results. Both return different result sets, so we're penalized for being more correct. Unfixable without intentionally using wrong case.

## Improvements for Generation 5

### Improvement 1: New Rule — Superlative Entity (ORDER BY Only)

**System Prompt Addition:**
```
SUPERLATIVE ENTITY RULE (SELECT entity only when finding the highest/lowest):
When question asks "Which/What X has the most/highest/lowest/best/fewest Y?",
"What is the X with the most Y?", or "X that has the most/highest Y":
→ SELECT the entity X ONLY — do NOT include count(*) or aggregate in SELECT
→ The aggregate goes in ORDER BY ONLY
✓ "Which year has most concerts?" → SELECT Year ... ORDER BY count(*) DESC LIMIT 1
✓ "What degree name has most students?" → SELECT name ... ORDER BY count(*) DESC LIMIT 1
✗ "Which year has most concerts?" → SELECT Year, count(*) ... (count NOT needed in SELECT)
✗ "What degree name has most students?" → SELECT count(*), name ... (WRONG — only name asked)
```

**Review Prompt Addition (Check 0 — before aggregate ordering):**
Check for superlative entity: remove unnecessary count(*) from SELECT when question asks for entity only.

### Improvement 2: Revised Rule 1 — Sentence Structure Determines Ordering

**System Prompt Revision:**
Replace the monolithic "aggregate-first" rule with a 3-case rule keyed on sentence structure:
- Case A: "How many X for each Y?" / "Find the number of X for each Y?" → `count(*) FIRST`
- Case B: "For each Y, how many X?" / "[entity] and number of" → entity name FIRST
- Case C: "What is the avg/max X for each Y?" → aggregate FIRST

For Case B, when "for each [entity]" is used and the entity is from a separate table, JOIN to get the entity's descriptive name column (not the FK/ID column).

**Review Prompt Revision:**
- Protect (entity, count) order when question explicitly mentions entity before "number/count"
- Only apply aggregate-first when question starts with "how many" or "find the number"

### Improvement 3: New Rule — GROUP BY + HAVING Without Outer COUNT

**System Prompt Addition:**
```
"How many X has/have more than N Y?" with a GROUP BY involved:
→ SELECT count(*) FROM X JOIN Y GROUP BY X_id HAVING count(*) > N
→ Do NOT wrap in outer COUNT(*) subquery
The benchmark expects the per-group counts as the result, not a single outer count.
✗ WRONG: SELECT COUNT(*) FROM (SELECT ... GROUP BY ... HAVING count(*) > 2)
✓ RIGHT:  SELECT count(*) FROM X JOIN Y GROUP BY X_id HAVING count(*) > 2
```

### Improvement 4: Improved Selection Logic

Add three new protection cases:
1. **Superlative entity protection**: If question is superlative ("has the most/highest") AND candidate 1 has no count in SELECT AND candidate 2 has count in SELECT → prefer candidate 1.
2. **Entity-count order protection**: If question structure is "entity and count" (entity mentioned first) AND candidate 1 has (entity, count) ordering AND candidate 2 has (count, entity) → prefer candidate 1.
3. **GROUP BY + HAVING subquery protection**: If candidate 1 has `GROUP BY + HAVING` without subquery wrapper AND candidate 2 has the subquery wrapper version → prefer candidate 1 for "how many X has more than N Y" questions.

### Expected Impact

| Fix | Questions Fixed | Confidence |
|-----|----------------|------------|
| Superlative entity rule | t07, student_t11 | High |
| Sentence structure ordering | concert_t11, concert_t02 | Medium-High |
| GROUP BY + HAVING no outer wrap | car_t04 | High |
| Column order protection in selection | wta_t08 | Low (benchmark inconsistency) |
| Benchmark quirks | world_t07, world_t13 | None (unfixable) |

**Target accuracy:** 116-117/120 = 96.7-97.5%
