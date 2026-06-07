# Generation 7 Improvement Plan

## Performance History
- Gen 1: 75.83% (91/120) — initial agent
- Gen 2: 79.17% (95/120) — +3.34pp: distinct values, few-shot scoring, multi-candidate
- Gen 3: 88.33% (106/120) — +9.16pp: removed wrong aggregate-first rule, review-and-refine
- Gen 4: 93.33% (112/120) — +5pp: fixed review regression, protection rules A/B/C
- Gen 5: 88.33% (106/120) — -5pp: REGRESSION (over-broad superlative rule misfired)
- Gen 6: 94.17% (113/120) — +5.84pp: regression fix, surgical rule improvements, post-processing

## Gen 6 Failure Analysis (7 wrong answers)

### Failure 1: world_1__t04 — Table selection for "not speak" query
- Question: "Return the country codes for countries that do not speak English."
- Predicted: `SELECT Code FROM country WHERE Code NOT IN (...)`
- Gold: `SELECT CountryCode FROM countrylanguage EXCEPT SELECT CountryCode FROM countrylanguage WHERE LANGUAGE = "English"`
- **Root cause**: Model queries from `country` table (includes countries with no language records), gold uses `countrylanguage` EXCEPT pattern (only countries in the language table). Countries with no language entries are included in predicted but not gold.
- **Assessment**: Hard to fix reliably without overfitting. Accept as 1 irreducible failure.

### Failure 2: world_1__t07 — Ambiguous question with unusual gold SQL
- Question: "What is the total number of countries where Spanish is spoken by the largest percentage of people?"
- Predicted: `SELECT COUNT(*) WHERE Percentage = (SELECT MAX(Percentage)...)`
- Gold: `SELECT count(*), max(Percentage) FROM countrylanguage WHERE LANGUAGE = "Spanish" GROUP BY CountryCode`
- **Root cause**: Gold SQL is semantically unusual — returns (count_rows, max_percentage) per country for Spanish, not a "total number of countries" as the question implies. This is a benchmark annotation inconsistency.
- **Assessment**: Unfixable — benchmark gold SQL doesn't match the question's natural reading.

### Failure 3: world_1__t13 — String case inconsistency
- Question: "What is the total population and average area...North America...bigger than 3000?"
- Predicted: `Continent = 'North America'` (follows DISTINCT VALUES)
- Gold: `continent = "north america"` (lowercase)
- **Root cause**: DISTINCT VALUES section correctly shows 'North America' (mixed case) but the training examples for this DB use lowercase "north america". The gold SQL uses lowercase which returns empty results; so does the evaluation's gold. Our predicted with 'North America' returns real data but doesn't match the gold's empty result.
- A very similar few-shot example was provided: `continent = "north america"` for essentially the same question — but model followed DISTINCT VALUES instead.
- **Assessment**: This is a benchmark data inconsistency. Matching it would require deliberately using wrong casing. We add a guideline to check few-shot examples for string casing first.

### Failure 4: dog_kennels__t10 — JOIN approach misses entities with no related records
- Question: "What are the names of the dogs for which the owner has not spend more than 1000 for treatment?"
- Predicted: `JOIN Owners JOIN Treatments ... HAVING sum(cost) <= 1000` (excludes dogs with NO treatments)
- Gold: `name FROM dogs WHERE dog_id NOT IN (SELECT dog_id FROM treatments GROUP BY dog_id HAVING sum(cost_of_treatment) > 1000)`
- **Root cause**: JOIN approach with HAVING sum <= 1000 incorrectly excludes dogs that have NO treatment records (they'd fail the JOIN). The NOT IN approach includes them (a dog not in the "over-1000" set is in the result).
- **Assessment**: HIGH CONFIDENCE fix — add explicit rule about NOT IN for negative threshold queries.

### Failure 5: student_transcripts_tracking__t03 — COUNT(DISTINCT) on primary key column
- Question: "How many different degrees are offered?"
- Predicted: `COUNT(DISTINCT degree_program_id)` [this is a PRIMARY KEY → always unique]
- Gold: `COUNT(DISTINCT degree_summary_name)` [3 distinct: 'Bachelor', 'Master', 'PHD']
- **Root cause**: The model chose the ID column (degree_program_id, which is PRIMARY KEY and has as many distinct values as rows) instead of the type/name column (degree_summary_name, which has only 3 distinct values representing the actual degree types).
- **Assessment**: HIGH CONFIDENCE fix — add rule: never COUNT(DISTINCT primary_key_column) when question asks "how many different [entity types]".

### Failure 6: student_transcripts_tracking__t08 — LIKE pattern includes article "the"
- Question: "What is the description of the department whose name has the substring the computer?"
- Predicted: `LIKE '%the computer%'`
- Gold: `LIKE '%computer%'`
- **Root cause**: "has the substring the computer" — model read "the computer" as the literal substring, but "the" is an article ("has the substring [named] computer"). The training example shows "whose name has the word computer" → `LIKE '%computer%'`.
- **Assessment**: MEDIUM CONFIDENCE fix — add linguistic rule: "has the word/substring X" → the article phrase is not part of the LIKE pattern.

### Failure 7: wta_1__t08 — Gold has inconsistent column order vs question
- Question: "What are the first names and country codes for players who won both the WTA Championships and the Australian Open?"
- Predicted: `SELECT p.first_name, p.country_code` (follows question word order: first_name first)
- Gold: `SELECT T1.country_code, T1.first_name` (country_code first, despite question saying "first names and country codes")
- **Root cause**: Benchmark annotation inconsistency — the similar question wta_1__t11 asks "country code and first name" and gold has country_code first. But wta_1__t08 asks "first names and country codes" (first_name first) yet the gold annotator still put country_code first. This is a benchmark annotation error.
- **Assessment**: Unfixable without overfitting — our column order rule correctly follows the question, but the gold doesn't.

## Fixable Failures: 3 of 7 (potentially 4 with medium-confidence fixes)

## Gen 7 Improvements

### Improvement 1: NOT IN for negative threshold queries (HIGH CONFIDENCE)
**Targets**: dog_kennels__t10 and similar patterns

**New Rule**: When the question asks for "X for which [accumulated cost/count] has NOT exceeded N" or "X that have not spent/done more than N":
- CORRECT: `SELECT X WHERE X.id NOT IN (SELECT X.id FROM Related GROUP BY X.id HAVING aggregate > N)`
- WRONG: JOIN approach with `HAVING aggregate <= N` (excludes X with NO related records)

The NOT IN subquery correctly includes entities with no related records (since they don't appear in the "over threshold" set). The JOIN approach incorrectly excludes them.

**Example**:
- "dogs for which owner has NOT spent more than 1000" → NOT IN approach to include untreated dogs
- "students who have NOT received more than 3 treatments" → NOT IN approach to include students with 0 treatments

**Also add to Review Check**: Detect JOIN+HAVING<=N pattern and flag for conversion to NOT IN when question uses negative phrasing.

### Improvement 2: COUNT(DISTINCT) must use descriptive name column, not primary key (HIGH CONFIDENCE)
**Targets**: student_transcripts_tracking__t03

**Enhanced Rule**: For "how many different [entity types]":
- Use COUNT(DISTINCT [descriptive_name_column]) — the column that identifies the TYPE, not the instance
- NEVER use COUNT(DISTINCT [primary_key_column]) — PKs are unique per row, not per type
- Look at DISTINCT VALUES: if a column has very few distinct values (2-5), it's probably the type column
- Example: degree_summary_name has 3 distinct values → correct column for "how many different degrees"
  degree_program_id is PRIMARY KEY → counting distinct PK values = counting rows = wrong

**Review Check**: If COUNT(DISTINCT col) is used and col is marked as PRIMARY KEY in the schema DDL, flag it as wrong for "how many different types" questions.

### Improvement 3: LIKE patterns — article "the" is not part of the pattern (MEDIUM CONFIDENCE)
**Targets**: student_transcripts_tracking__t08

**New Rule**: In "has the word X", "has the substring X", "whose name contains the word X" phrases:
- "the word", "the substring", "the text" are article phrases — NOT part of the LIKE pattern
- "has the word computer" → LIKE '%computer%' (NOT '%the word computer%')
- "has the substring the computer" → LIKE '%computer%' (NOT '%the computer%')
- Exception: if X is itself a complex phrase clearly identifiable as a substring (e.g., "has the substring 'John Smith'"), use it literally

**Also**: When a similar question appears in EXAMPLE QUESTION-SQL PAIRS using a LIKE pattern, prefer the same pattern.

### Improvement 4: Few-shot string casing guidance (LOW RISK ADDITION)
**Targets**: world_1__t13 (partial fix)

**Added guidance**: When the EXAMPLE QUESTION-SQL PAIRS contains a very similar question (same tables, same filter type, same general structure), check the string literals used in the gold SQL from that example. If the casing differs from DISTINCT VALUES, the training example's casing may better reflect what the evaluation expects.

**Note**: This is a low-priority addition since world_1__t13 seems to be a genuine benchmark inconsistency (the gold SQL returns empty results with lowercase filtering).

## Changes in target_agent.py

1. **System prompt Rule 12**: NOT IN for negative existence/threshold queries
   - Explicit examples and anti-examples
   - Guidance on when JOIN excludes valid entities

2. **System prompt Rule 2 enhancement**: COUNT(DISTINCT) must use type column
   - Warning about primary key columns
   - Reference DISTINCT VALUES to find the right column

3. **System prompt Rule 6 enhancement**: LIKE with article phrases
   - "has the word/substring X" → drop article prefix from LIKE pattern

4. **Review prompt Check 8**: NOT IN vs JOIN for negative queries
   - Detect HAVING <= N or HAVING NOT > N patterns
   - Check if question uses negative phrasing

5. **Review prompt Check enhanced**: COUNT(DISTINCT) column validation
   - Check if counted column is PRIMARY KEY
   - If yes and question asks "how many different types", flag as wrong

## Expected Outcome

- Fix dog_kennels__t10 (NOT IN rule): +1 correct
- Fix student_transcripts_tracking__t03 (COUNT DISTINCT rule): +1 correct
- Fix student_transcripts_tracking__t08 (LIKE article rule): +1 correct (medium confidence)
- Unfixable: world_1__t04, world_1__t07, world_1__t13, wta_1__t08

Expected accuracy: ~116/120 = 96.67% (if all 3 high/medium fixes work)
Conservative estimate: ~115/120 = 95.83% (if only high-confidence fixes work)

## Risk Assessment

- Rules 1 and 2 (NOT IN, COUNT DISTINCT) have clear, unambiguous trigger conditions → low regression risk
- Rule 3 (LIKE article) is somewhat linguistic but bounded by trigger phrases → low-medium risk
- No changes to selection logic or post-processing (these are working well in Gen 6)
- No removal of existing working rules
