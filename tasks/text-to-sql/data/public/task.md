# Task: Text-to-SQL (SQLite execution accuracy)

Build an agent that translates natural-language questions into **SQLite `SELECT`
queries** that, when executed against the referenced database, return the correct
answer.

## What your `target_agent.py` receives

It is launched as:

```
python target_agent.py --dataset_dir <DATASET> --working_dir <WORKDIR>
```

- `--dataset_dir` (READ-ONLY) contains everything you may use as input:
  - `test_questions.jsonl` — one JSON object per line: `{"id", "db_id", "question"}`.
    These are the questions you must answer. There is **no gold/answer here.**
  - `schemas.json` — `{db_id: "<CREATE TABLE ... DDL string>"}` for every database
    (a convenience copy of each database's schema).
  - `databases/<db_id>/<db_id>.sqlite` — the actual SQLite database for each `db_id`.
    You may open these **read-only** to inspect tables/columns/sample rows.
  - `train.jsonl` — one JSON object per line: `{"id", "db_id", "question", "query"}`.
    A pool of **labeled** examples drawn from the *same databases* (but different
    questions than the test set). You may use these however you find helpful.
- `--working_dir` (READ-WRITE) is where you write your output and logs.

## What you must produce

Write your predictions to **`<WORKDIR>/predictions.jsonl`** — one JSON object per
line, exactly:

```json
{"id": "<the question id>", "predicted_sql": "<a single SQLite SELECT query>"}
```

There must be one line per question in `test_questions.jsonl` (matched by `id`).

Also write an execution log in `<WORKDIR>` (per the SIA logging convention shown to
you) capturing, for each question, what you attempted — this is used to analyze and
improve the agent later.

## How you are scored

**Execution accuracy.** For each `id`, your `predicted_sql` and the held-out gold
query are both executed against `databases/<db_id>/<db_id>.sqlite`, and your answer
is counted correct iff the two result sets match. **Row order does not matter.**
A query that is rejected, errors, returns the wrong rows, or times out scores 0.

## Hard rules for every predicted query

- **SQLite dialect.**
- **`SELECT` or `WITH` only** — read-only. No `INSERT/UPDATE/DELETE/DROP/CREATE/PRAGMA/ATTACH`.
- **Exactly one statement** — no semicolon-separated multiple statements.
- Queries are executed **read-only with a per-query timeout**, so avoid runaway
  cross joins / unbounded recursion.

## Notes

- The questions are **independent items**: each is a separate question over its own
  database, processed on its own.
- Only the configured task model may be used for any model calls.
- Do not attempt to access anything outside `--dataset_dir` (read) and
  `--working_dir` (write). The gold answers are intentionally not provided.
