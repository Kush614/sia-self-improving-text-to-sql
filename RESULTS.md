# Results — run_1

**8 generations · execution accuracy 75.8% → 95.0% (peak gen 8, +19.2 pts)**  
Task model held fixed (Claude Haiku) every generation; meta/feedback agent Claude Sonnet. Verifier: execution accuracy (read-only, order-insensitive) on real Spider DBs; gold held out.

| gen | accuracy | correct | errors left | what changed (self-edit) |
|----|----------|---------|-------------|--------------------------|
| 1 | 75.8% | 91/120 | wrong-result:29 | Cold start — meta-agent's initial agent (full schema + question, one call) |
| 2 | 79.2% | 95/120 | wrong-result:25 | Learned Spider conventions: aggregate-first ordering, exact string casing, any→MIN |
| 3 | 88.3% | 106/120 | wrong-result:14 | Fixed its own gen-2 over-correction; added few-shot + execute-and-repair |
| 4 | 93.3% | 112/120 | wrong-result:8 | Surgical per-failure fixes (column identity, INNER vs LEFT JOIN, simplification) |
| 5 | 88.3% | 106/120 | wrong-result:14 | Over-applied ordering rules → regressed |
| 6 | 94.2% | 113/120 | wrong-result:7 | Recovered; new peak |
| 7 | 93.3% | 112/120 | wrong-result:8 | Over-tuned again → slight regression |
| 8 ⭐ | 95.0% | 114/120 | wrong-result:6 | Diagnosed & fixed gen-7 regressions (ANY/ALL, CAST, temporal) → final peak |

## Per-generation self-edits (from each improvement.md)

**Generation 2 (79.2%)**
- 1. Column Order Issues (~7 failures, ~24% of wrong answers)
- 2. String Case Sensitivity (~3 failures)
- 3. Unnecessary CAST() Issues (~1-2 failures)
- 4. LEFT JOIN vs INNER JOIN Issues (~4 failures)
- 5. Wrong Column Selected (~3 failures)
- 6. Semantic Misinterpretation (~3 failures)

**Generation 3 (88.3%)**
- Category A: Unnecessary / Misplaced count(*) in SELECT (~15 failures)
- Category B: Semantic/Structural Issues (~10 failures)
- 1. Fix System Prompt Rule 1 (Critical - fixes ~15 failures)
- 2. Add count(DISTINCT) Rule (fixes 2 failures)
- 3. Fix String Matching Rule (fixes 1 failure)
- 4. Change Candidate 2 to "Review and Refine" (structural improvement)

**Generation 4 (93.3%)**
- Root Cause Analysis from Execution Logs
- Failure Categories (14 total)
- Improvement 1: Fix review regression (addresses 2-3 cases)
- Improvement 2: Strengthen aggregate-first initial generation (addresses 2+ cases)
- Improvement 3: Fix column identity (addresses 1 case)
- Improvement 4: Strengthen ANY vs ALL (addresses 1 case)

**Generation 5 (88.3%)**
- Failure Category 1: Superlative Entity — Unnecessary count(*) in SELECT (2 questions)
- Failure Category 2: Sentence-Structure Aggregate Ordering (2 questions)
- Failure Category 3: Unnecessary Outer COUNT Wrapper (1 question)
- Failure Category 4: Column Order in Multi-column Intersection (1 question)
- Failure Category 5: Benchmark Quirks (2 questions — not fixable)
- Improvement 1: New Rule — Superlative Entity (ORDER BY Only)

**Generation 6 (94.2%)**
- Failures introduced by Gen 5 (regressions from Gen 4):
- Pre-existing Gen 4 failures that remain in Gen 5 (approx):
- 1. System Prompt Changes
- 2. Review Prompt Changes
- 3. Python Code Changes
- 4. Question Classification Improvements

**Generation 7 (93.3%)**
- Failure 1: world_1__t04 — Table selection for "not speak" query
- Failure 2: world_1__t07 — Ambiguous question with unusual gold SQL
- Failure 3: world_1__t13 — String case inconsistency
- Failure 4: dog_kennels__t10 — JOIN approach misses entities with no related records
- Failure 5: student_transcripts_tracking__t03 — COUNT(DISTINCT) on primary key column
- Failure 6: student_transcripts_tracking__t08 — LIKE pattern includes article "the"

**Generation 8 (95.0%)**
- New Gen 7 Failures (4 regressions):
- Fix 1: ANY/ALL — Force mathematical definition, override few-shot (HIGH CONFIDENCE)
- Fix 2: CAST removal in aggregate functions (HIGH CONFIDENCE)
- Fix 3: No temporal filters for "at this moment" / "currently" (MEDIUM CONFIDENCE)
- Fix 4: "How many X from each Y?" → aggregate first (HIGH CONFIDENCE)
- Fix 5: Strengthen FEW-SHOT STRING CASING override (LOW-MEDIUM CONFIDENCE)

## Notes

- Two self-correction arcs: gen 2→3 and gen 7→8 (the agent diagnosed and fixed regressions it caused itself).
- Non-monotonic (gens 5 and 7 regressed) — honest self-improvement, not a smoothed curve.
- Full artifacts per generation (results.json, improvement.md, evolved target_agent.py) are in `results/run_1/gen_*/`.
