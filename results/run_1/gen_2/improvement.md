# Generation 2 Improvement Analysis

## Previous Performance
- Generation 1: 75.83% accuracy (91/120 correct)

## Failure Pattern Analysis

### 1. Column Order Issues (~7 failures, ~24% of wrong answers)
**Failures**: wta_1__t06, wta_1__t12, pets_1__t02, pets_1__t06, pets_1__t08, pets_1__t09, car_1__t05

The benchmark (Spider) consistently puts aggregate functions (COUNT, AVG, MAX, MIN, SUM) **FIRST** in the SELECT list for GROUP BY queries, then the group-by column. The model reversed this order.

Examples:
- Gold: `SELECT count(*), hand FROM players GROUP BY hand`
- Predicted: `SELECT hand, COUNT(*) FROM players GROUP BY hand`

**Fix**: Add explicit instruction in system prompt: "For GROUP BY queries, put aggregate functions BEFORE group-by columns."

### 2. String Case Sensitivity (~3 failures)
**Failures**: flight_2__t01, flight_2__t11 (JetBlue Airways), world_1__t13

The SQLite `=` operator is case-sensitive. The model generated 'Jetblue Airways' but data has 'JetBlue Airways'. The sample rows showed only United/US Airways/Delta - NOT JetBlue - so the model guessed wrong case.

**Fix**: Add `get_distinct_string_values()` function that shows all distinct values for categorical string columns. This ensures the model sees the exact case for all string values used in WHERE clauses.

### 3. Unnecessary CAST() Issues (~1-2 failures)
**Failure**: car_1__t02 - `MAX(CAST(MPG AS REAL))` vs gold `max(mpg)`

When MPG is stored as TEXT, `MAX(CAST(MPG AS REAL))` returns a float (18.0) while `max(mpg)` on TEXT returns a string ('18'). These don't match in tuple comparison.

**Fix**: Explicit instruction: "Do NOT use CAST unless necessary. Use column values directly."

### 4. LEFT JOIN vs INNER JOIN Issues (~4 failures)
**Failures**: concert_singer__t02, concert_singer__t11, car_1__t03, dog_kennels__t10

The model used LEFT JOIN returning rows with NULL matches, inflating result sets with zero-count rows. Gold uses INNER JOIN which only returns rows with actual matches.

**Fix**: Explicit instruction: "Default to INNER JOIN. Use LEFT JOIN only when explicitly needed for rows without matches."

### 5. Wrong Column Selected (~3 failures)
**Failures**: 
- flight_2__t12: Selected `AirportCode` instead of `AirportName`
- car_1__t00: Selected `Maker` (short abbreviation) instead of `FullName`
- student_transcripts_tracking__t03: Counted by `degree_program_id` instead of `degree_summary_name`

**Fix**: Add guidance: "When question asks for 'name', prefer the descriptive name column over code/id columns."

### 6. Semantic Misinterpretation (~3 failures)
**Failures**: world_1__t03 ("any" → MIN vs MAX), world_1__t07 (complex aggregation), car_1__t04 (subquery vs join count)

"Larger than any X" should be > MIN(X) not > MAX(X). These are subtle natural language ambiguities.

**Fix**: Add semantic clarification in system prompt about "any" vs "all" semantics.

### 7. Complex Join Issues (~3 failures)
**Failures**: car_1__t13, car_1__t14, dog_kennels__t07

The model over-complicated joins when simpler approaches work. For example, "What are all the makers and models?" only needs `SELECT Maker, Model FROM MODEL_LIST` but model did complex multi-table join.

**Fix**: Instruction: "Prefer the simplest query structure that answers the question."

## Implemented Improvements

### 1. Enhanced System Prompt
Added explicit rules for:
- Aggregate-first column ordering in GROUP BY
- No unnecessary CAST operations
- Default INNER JOIN
- String case sensitivity with sample data reference
- Natural language semantics ("any" = MIN, "all" = MAX)
- Simplicity preference

### 2. Distinct String Values Display
New `get_distinct_string_values()` function shows all distinct values for categorical string columns (2-30 distinct values). This directly fixes case sensitivity issues and helps model select correct string literals.

### 3. Better Few-Shot Selection
- Increased MAX_FEW_SHOT from 5 to 10
- Added structural similarity scoring: prefer examples with similar SQL patterns (GROUP BY, JOIN, HAVING, subquery) to the current question
- Ensures aggregate-first patterns appear in few-shot examples

### 4. Multi-Candidate Self-Consistency
For each question:
1. Generate primary SQL candidate
2. Execute it
3. If successful AND question patterns suggest ambiguity (aggregates, JOINs), generate a second candidate with explicit verification prompt
4. Compare execution results; if they match → high confidence; if not → use primary
5. If primary fails → use secondary (if it passes) before falling back to repair

This catches systematic errors (like column order) because the second candidate with explicit verification prompt is more likely to apply the rules correctly.

### 5. Improved Schema Presentation
- Show schema with column types more clearly
- Added distinct values section between schema and sample rows
- Formatted schema with explicit foreign key relationship descriptions

### 6. Smarter Repair Prompt
Repair prompt now:
- Specifies the exact error type if identifiable
- Reminds about column order convention
- Reminds about string case sensitivity
- Suggests checking the schema for correct column names

## Expected Impact
- Column order fix: ~7 failures fixed
- String case fix: ~2-3 failures fixed  
- CAST fix: ~1-2 failures fixed
- JOIN type fix: ~3-4 failures fixed
- Combined improvements: expect ~13-17 additional correct answers
- Expected new accuracy: ~87-90%
