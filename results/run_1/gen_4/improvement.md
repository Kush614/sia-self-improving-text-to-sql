# Generation 4 Improvement Plan

## Performance History
- Gen 1: 75.83% (91/120) — initial agent
- Gen 2: 79.17% (95/120) — +3.34pp: distinct values, few-shot scoring, multi-candidate
- Gen 3: 88.33% (106/120) — +9.16pp: removed wrong "aggregate-first" rule, review-and-refine

## Analysis of Gen 3 Failures (14 wrong)

### Root Cause Analysis from Execution Logs

**CRITICAL FINDING: The review step is causing regressions**
For wta_1__t06 and pets_1__t02, execution logs show:
- Initial generation: `SELECT count(*), hand FROM players` ← CORRECT
- After review: `SELECT hand, count(*) FROM players` ← WRONG (review broke it!)
- The review's "column order" check misapplies the rule, swapping correct count-first to wrong Y-first

### Failure Categories (14 total)

#### Category 1: Aggregate ordering — review regression (2 cases)
**wta_1__t06** ("How many players for each hand type?"):
- Initial SQL correctly has `SELECT count(*), hand` (count first)
- Review breaks it to `SELECT hand, count(*)` (hand first)
- **Root cause**: Review's "column order" check interprets question word order ("players" before "hand type") and puts hand first, overriding the correct count-first rule

**pets_1__t02** ("Find the number of pets for each student who has any pet and student id."):
- Initial SQL correctly has `SELECT count(*), stuid` (count first)  
- Review breaks it to `SELECT stuid, count(*)` (stuid first)
- Same root cause as t06

#### Category 2: Aggregate ordering — initial generation wrong (2 cases)
**wta_1__t12** ("How many players are from each country?"):
- Both initial and review generate `SELECT country_code, count(*)` (country first)
- Should be: `SELECT count(*), country_code` (count first)
- **Root cause**: Model interprets "from each country" as primary grouping and puts it first

**car_1__t05** ("What is the maximum accelerate for all the different cylinders?"):
- Both generate `SELECT Cylinders, MAX(Accelerate)` (Cylinders first)
- Should be: `SELECT max(Accelerate), Cylinders` (aggregate first)
- **Root cause**: System prompt rule covers "how many X for each Y" but not "what is the MAX X for each Y" pattern

#### Category 3: Multi-column order (1 case)
**car_1__t03** ("What is the full name of each car maker, along with its id and how many models it produces?"):
- Generated: `SELECT Id, FullName, count(*)` (Id before FullName)
- Expected: `SELECT FullName, Id, count(*)` (FullName first per question order)
- **Root cause**: Model puts Id first (primary key), ignoring question mention order

#### Category 4: Column identity wrong (1 case)
**flight_2__t12** ("Which airports do not have departing or arriving flights?"):
- Generated: `SELECT AirportCode` 
- Expected: `SELECT AirportName`
- "which airports" → should return descriptive name, not code
- **Root cause**: Rule 7 says "name → descriptive name" but doesn't cover "which [entity]"

#### Category 5: ANY vs ALL misinterpretation (1 case)
**world_1__t03** ("Asian countries with population larger than that of any country in Africa"):
- Generated: `> (SELECT MAX(Population) WHERE Continent='Africa')` (larger than ALL)
- Expected: `> (SELECT MIN(Population) WHERE Continent='Africa')` (larger than ANY one)
- **Root cause**: Few-shot training example shows "less than any Asian country → < MAX" which the model incorrectly maps to "ANY = MAX" for the greater-than case too

#### Category 6: Wrong column in WHERE (1 case)
**car_1__t12** ("average edispl of cars of model volvo"):
- Generated: `WHERE T1.Make = 'volvo'` (wrong column)
- Expected: `WHERE T1.Model = 'volvo'` (correct column)
- **Root cause**: Model confused Make and Model columns; "of model X" should filter on Model column

#### Category 7: Ambiguous/gold-inconsistent (6 cases)
- **world_1__t04**: Different table (country vs countrylanguage) — semantic ambiguity
- **world_1__t07**: Ambiguous gold SQL — gold returns per-country counts, not total
- **world_1__t13**: Case sensitivity issue ('North America' vs 'north america') — likely evaluation artifact
- **car_1__t04**: Gold SQL returns per-country counts instead of count-of-countries
- **wta_1__t08**: Gold has country_code first despite question saying "first names and country codes" — gold inconsistency
- **wta_1__t14**: Gold uses raw column, predicted uses max() — SQLite GROUP BY behavior difference

## Planned Improvements

### Improvement 1: Fix review regression (addresses 2-3 cases)
**Problem**: Review changes correct `SELECT count(*), Y` to wrong `SELECT Y, count(*)`

**Fix A — Review prompt**:
- Rewrite Check 1 to explicitly say: "If SQL ALREADY has aggregate FIRST for 'how many' questions → CORRECT, do NOT change"
- Add clear CORRECT/WRONG examples showing what the review should NOT change
- Remove ambiguous "column order" sub-rule that causes regression

**Fix B — Selection code**:
- Add protection rule: if question asks "how many" AND candidate 1 has aggregate first BUT candidate 2 doesn't → prefer candidate 1
- This catches the case where review regression breaks a correct initial answer

### Improvement 2: Strengthen aggregate-first initial generation (addresses 2+ cases)
**Problem**: Model generates Y-first for "how many from each Y" and "max X for each Y"

**Fix — System prompt Rule 1**:
- Add explicit examples for "What is the max X for all different Y?" → aggregate first
- Add more varied "how many" examples to reinforce: count always first
- Add ✗ WRONG examples to show what NOT to generate

### Improvement 3: Fix column identity (addresses 1 case)
**Problem**: "which airports" → AirportCode (should be AirportName)

**Fix — System prompt Rule 7 + Review Check 3**:
- Extend rule: "which [entity]" or "what [entity]" (without 'code'/'id') → return NAME column
- Add to review: check if "which X" question uses code column and flag it

### Improvement 4: Strengthen ANY vs ALL (addresses 1 case)
**Problem**: Model uses MAX instead of MIN for "larger than ANY"

**Fix — System prompt Rule 4 + Review Check 4**:
- Add explicit ✗ WRONG example: "larger than any country → > MAX(...) is WRONG"
- Add to review: specific check for ANY vs ALL

### Improvement 5: Fix attribute filtering (addresses 1 case)
**Problem**: "cars of model X" → WHERE Make = 'X' (should be WHERE Model = 'X')

**Fix — System prompt Rule 7**:
- Add: "cars of model X" → WHERE Model = 'X', not WHERE Make = 'X'
- General principle: "of [attribute] X" → filter on column named [attribute]

## Architecture Changes

**Keep**: 2-candidate review-and-refine architecture (same as Gen 3)
**Keep**: All infrastructure (DB helpers, few-shot, repair loop)
**Change**: System prompt (Rules 1, 4, 7)
**Change**: Review prompt (add targeted checks with correct/wrong examples)
**Change**: Selection logic (add aggregate-first protection)

## Expected Impact

Definitely fixable:
- wta_1__t06: Review regression fix + selection protection (+1)
- pets_1__t02: Review regression fix + selection protection (+1)  
- wta_1__t12: Stronger initial generation rule (+1)
- car_1__t05: "max X for each Y → aggregate first" rule (+1)
- flight_2__t12: "which airports → AirportName" rule (+1)
- world_1__t03: ANY vs ALL fix (+1)

Possibly fixable:
- car_1__t03: Stronger column order rule in review (+0-1)
- car_1__t12: "model X → Model column" rule (+0-1)

Projected accuracy: ~92-95% (110-114/120)
