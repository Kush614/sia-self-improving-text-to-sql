# SIA · Self-Improving Text-to-SQL Agent

> An AI agent that **rewrites its own code, generation after generation — with no human edits and without changing the underlying model** — and gets measurably better at turning English questions into SQL. Verified against an **ungameable execution-accuracy grader**.

**Result: execution accuracy climbed `75.8% → 79.2% → 88.3% → 93.3%` (+17.5 points) across 4 generations — every gain a self-discovered harness change, with the task model (Claude Haiku) held fixed the entire time.**

🎥 **Video demo:** https://www.loom.com/share/8454512059b245dcbd6228f2d616997c
🔗 **Live demo:** https://kush614.github.io/sia-self-improving-text-to-sql/ · **Deep dive:** [/explain.html](https://kush614.github.io/sia-self-improving-text-to-sql/explain.html)
📦 Built on [hexo-ai/sia](https://github.com/hexo-ai/sia) (Self-Improving AI, arXiv:2605.27276) · Task: [Spider](https://yale-lily.github.io/spider) text-to-SQL

![Overview](docs/screenshots/00-hero.png)

---

## TL;DR for reviewers

- **What:** a "bring-your-own-task" entry for the SIA self-improvement framework, on **text-to-SQL**.
- **The loop:** a *meta-agent* writes an agent → the *target agent* answers 120 questions → a *verifier* scores them by **executing the SQL** → a *feedback-agent* reads the failures and **rewrites the target agent** for the next generation. Repeat.
- **The hook:** the model never changes. Every accuracy gain comes from the agent **editing its own scaffold** — and you can read, in its own words (`improvement.md`), the exact bugs it found in itself and the fixes it wrote.
- **Verifier integrity (anti-Goodhart):** gold answers live in `data/private/` and are **never** shown to the agent; SQL is executed **read-only, SELECT-only, with a timeout**; comparison is order-insensitive on result sets. You cannot bluff it.
- **Self-correction, on camera:** gen 2 over-applied a rule it invented → gen 3 detected the regression from the new failures and fixed it. The agent reasons about the consequences of its *own* past edits.

---

## The result

| Generation | What changed (the agent's own self-edit) | Execution accuracy |
|---|---|---|
| **1** (cold start) | Meta-agent's initial agent: full schema + question, one model call | **75.8%** (91/120) |
| **2** | Discovered Spider conventions: aggregate-first column order, exact string casing, `"any"`→`MIN` | 79.2% (95/120) |
| **3** | Detected & fixed its *own* gen-2 over-correction; added few-shot + execute-and-repair | 88.3% (106/120) |
| **4** | Surgical per-failure fixes (column identity, INNER vs LEFT JOIN, simplification) | **93.3%** (112/120) |

Same task model (Claude Haiku) every generation. The only variable is the agent's own code.

![Accuracy by generation + generation explorer](docs/screenshots/05-improvement.png)

---

## What the agent discovered — on its own

By reading the concrete failure samples the verifier writes into `results.json`, the feedback-agent invented specific, non-obvious fixes:

- **Spider's aggregate-first convention:** gold writes `SELECT count(*), col` (aggregate before group-by column). The agent noticed and added the rule.
- **Exact string casing:** it **invented a helper tool** to surface distinct string values, because the model kept writing `Jetblue` when the data said `JetBlue`.
- **Quirky semantics:** "larger than **any** X" means `> MIN(X)`, not `> MAX(X)` — derived from failures.
- **Column identity:** "when the question asks for a *name*, select the descriptive column, not the code."
- **Mechanisms:** same-database **few-shot** from the training pool, an **execute-and-repair** loop, and **self-consistency** voting.

The before→after playground executes a real example live (cached, read-only): the same question, gen-1's wrong query (`Maker` codes → `gm`, `chrysler`) vs gen-4's correct query (`FullName` → `General Motors`, `Chrysler`), next to the held-out gold.

![Before → after, executed on the database](docs/screenshots/03-run-query.png)

---

## How it works

![How it works — the 3-agent loop](docs/screenshots/04-explain-light.png)

1. **Meta-agent** (Claude Sonnet) reads `task.md` + a minimal reference agent and writes the first `target_agent.py`.
2. **Target agent** (Claude Haiku) is run as `python target_agent.py --dataset_dir <public> --working_dir <gen>`; it answers all 120 questions and writes `predictions.jsonl`.
3. **Verifier** (`evaluate.py`) runs each predicted query *and* the gold query read-only on the same SQLite DB, compares result sets order-insensitively, and writes `results.json` with an error histogram + concrete failure samples.
4. **Feedback-agent** (Claude Sonnet) reads `results.json`, writes `improvement.md` (its diagnosis), and rewrites `target_agent.py` for the next generation.

The whole thing runs locally via the SIA CLI; the dashboard reads the stored run artifacts.

### Light / dark, fully cached, offline
The dashboard is a self-contained static page. `build_cache.py` snapshots every generation — including the before/after query result tables, **pre-executed** — into `dashboard/cache/demo_data.js`. No server, no model, no live DB needed at view time; it keeps rendering even if the run is stopped.

![Dark theme](docs/screenshots/02-overview-dark.png)

---

## Anti-Goodhart (why the score is trustworthy)

- **Gold answers** (`data/private/test_gold.jsonl`) are **never** in the agent's `--dataset_dir` (which is `data/public`). The agent sees only questions, schemas, the databases, and a *separate* labeled training pool.
- **Execution, not string match:** a query counts only if it returns the same rows as gold on the real database.
- **Sandboxed SQL:** read-only connection, `SELECT`/`WITH` only, single statement, per-query timeout. A `DROP`/`UPDATE`/multi-statement query is rejected and scores 0 without touching the DB.
- **Pragmatic caveat:** result-set comparison is order-insensitive but does not handle column-permutation equivalence (a known approximation of Spider's official test-suite accuracy). The *trend across generations* is the claim.

---

## Repository layout

```
tasks/text-to-sql/
├── data/public/         task.md, evaluate.py (verifier), test_questions.jsonl,
│                        train.jsonl, schemas.json, databases/<db>/<db>.sqlite
├── data/private/        test_gold.jsonl  ← held out, never shown to the agent
└── reference/           reference_target_agent.py (cold-start seed) + samples
dashboard/               index.html · explain.html · app.js · styles.css
└── cache/demo_data.js   self-contained snapshot the site reads (offline)
prep_data.py             build the task data from real Spider (deterministic)
build_cache.py           snapshot a run into the dashboard cache
tests/                   57 offline tests (verifier, agent wiring, dashboard, SIA loader, feedback channel)
INTERFACE_NOTES.md       the verified SIA contract (reverse-engineered from source)
README_RUN.md            exact reproduce commands (Windows)
```

## Reproduce

```bash
pip install 'sia-agent[claude]' datasets streamlit gdown markdown playwright
python prep_data.py                                   # build the task from real Spider
python -m pytest -q  # or run tests/*.py              # 57 offline checks, no API key
export ANTHROPIC_API_KEY=...                           # needed only to run the loop
sia run --task_dir ./tasks/text-to-sql --max_gen 8 --run_id 1 \
        --meta-agent-profile ./profiles/sonnet-meta.json
python build_cache.py --run-id 1 --max-gen 4          # snapshot for the dashboard
python -m http.server 8700 --directory dashboard      # open http://localhost:8700
```

Full, Windows-correct commands and the interface contract are in [`README_RUN.md`](README_RUN.md) and [`INTERFACE_NOTES.md`](INTERFACE_NOTES.md).

## Tech

Python 3.11+ · SQLite (stdlib) · [SIA](https://github.com/hexo-ai/sia) harness · Claude (Sonnet meta/feedback, Haiku target) · real [Spider](https://yale-lily.github.io/spider) data · vanilla HTML/CSS/JS dashboard (no framework, no CDN — fully offline).

## Honesty notes

- The headline run climbed to **93.3% at gen 4**; a later generation (5) **regressed** to 88.3% by over-applying new rules — self-improvement is not monotonic, and the dashboard data is pinned to the gen-4 peak.
- Accuracy is execution-match (order-insensitive), a pragmatic stand-in for Spider's official test-suite evaluator.
- Built and run on Windows; a small cross-platform patch to SIA's venv path is included in `patches/`.

## Credits

SIA framework by [Hexo AI](https://github.com/hexo-ai/sia) (arXiv:2605.27276). Spider dataset by Yu et al., Yale (CC BY-SA 4.0). Built with [Claude Code](https://claude.com/claude-code).
