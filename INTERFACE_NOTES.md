# INTERFACE_NOTES.md — the REAL SIA contract (Task 0)

> Source of truth. Verified by reading the cloned source at `E:\sia\sia` (commit fetched 2026-06-06),
> not the spec. **Where this file and `specs.md` disagree, this file wins.**
> Key files read: `sia/orchestrator.py`, `sia/cli.py`, `sia/layout.py`, `sia/run_setup.py`,
> `sia/prompts.py`, `sia/config.py`, `sia/results.py`, `sia/agent_impls/claude.py`,
> `docs/walkthrough.md`, `EVALUATION_GUIDE.md`, `sia/tasks/_shared/`, `sia/tasks/spaceship-titanic/`.

---

## TL;DR of corrections to specs.md

| specs.md said | Reality |
|---|---|
| Verifier file is `eval.py` | It is **`evaluate.py`**, and in practice it MUST live at **`data/public/evaluate.py`** (the "task_dir fallback" never fires — see §4; confirmed by a live smoke that *skipped* evaluation when the file was at the task root). |
| `eval.py` returns a score via some channel | `evaluate.py` is run as `python evaluate.py --gen-dir <gen_dir>`; it must **write `results.json` into that gen dir**. Return value/stdout are ignored except for logging; exit code 0 required. |
| CLI: `sia --task_dir ... --max_gen 8 --run_id 1 --task_model haiku --meta_model sonnet` | CLI: **`sia run --task_dir ... --max_gen 8 --run_id 1`**. There is **no `--task_model`/`--meta_model`**. Models come from JSON **profiles** (`--meta-agent-profile`, `--target-agent-profile`) or `SIA_TASK_MODEL`/`SIA_META_MODEL` env vars. |
| Default meta model = sonnet | Default meta/feedback model = **haiku** (`sia/defaults/profiles/default-meta.json`). |
| Agent emits "predictions" (format TBD) | Format is **whatever `task.md` + `evaluate.py` agree on**. We choose `predictions.jsonl` written to `--working_dir`. |
| `agent_execution.json` is produced by SIA | **The target agent must write it itself** into `--working_dir` (single file) or an `agent_execution/execution_q*.json` folder (multi-trajectory). |

---

## 1. CLI contract

- Entry point: `sia` (console script) → `sia.orchestrator:main`. Subcommands: `run`, `web`.
  Bare `sia --task gpqa` is back-compat-rewritten to `sia run --task gpqa`.
- Run a custom task:
  ```
  sia run --task_dir ./tasks/text-to-sql --max_gen 8 --run_id 1
  ```
- Useful flags (`sia/cli.py`): `--max_gen` (default 3), `--run_id` (default 1),
  `--meta-agent-profile NAME|path.json` (default `default-meta`),
  `--target-agent-profile NAME|path.json` (default `default-target`),
  `--sandbox none|docker` (default `none`), `--focus harness|weights` (default `harness` — we use harness),
  `--no-web`, `--web-port 8000`, `--web-host 127.0.0.1`, `--log-level`.
- **Run dir must NOT already exist** — `setup_run_directory` `sys.exit(1)`s if `runs/run_<id>` exists. Use a fresh `--run_id` each run.

### Choosing models (no --task_model flag!)
- Meta/feedback model + impl + provider = the **meta profile** JSON.
- Target model + provider + seed code = the **target profile** JSON.
- Quickest override without authoring profiles: env vars before `sia run`:
  - `SIA_META_MODEL=sonnet` (string passed to Claude Agent SDK as model name)
  - `SIA_TASK_MODEL=claude-haiku-4-5-20251001`
  (See `Config.from_env` in `sia/config.py`.) Bumping meta to a stronger model = author a profile JSON like the bundled ones with `"model": "sonnet"` and pass `--meta-agent-profile path.json`.

---

## 2. Two Python execution contexts (CRITICAL)

1. **Host interpreter** runs `sia` itself **and the meta/feedback agents**. Those agents run via the
   **Claude Agent SDK** (`claude_agent_sdk.query`, see `sia/agent_impls/claude.py`):
   headless Claude Code, `permission_mode="bypassPermissions"`, tools `Bash/Read/Write/Edit/Glob`,
   `model=<meta model>`, `cwd=<gen dir>`. Authenticates via **`ANTHROPIC_API_KEY`** (needs the `claude` binary / Claude Code on the host).
2. **Per-run venv** at `runs/run_<id>/venv` runs the **target agent and evaluate.py** as subprocesses.
   Baseline packages installed: `anthropic, openai, python-dotenv, google-genai, claude-agent-sdk, tqdm, pydantic, scikit-learn, pandas, numpy` (`Config.VENV_PACKAGES`). The target agent may ship a `requirements.txt` in its gen dir to add deps.

> The target agent (the thing being improved) calls the model **itself** with the `anthropic` SDK.
> The model id is baked into `target_agent.py` by the meta-agent = the target profile's `model`
> (default `claude-haiku-4-5-20251001`).

---

## 3. Target agent contract (what the meta-agent will be told to produce)

From `sia/prompts.py::build_meta_prompt` (harness mode) and `orchestrator._run_target_agent`:

- File: **`target_agent.py`**, written into the gen dir (the meta-agent's cwd).
- Invoked: `python -u target_agent.py --dataset_dir <ABS data/public> --working_dir <gen_dir>`.
  - `--dataset_dir` = `<task_dir>/data/public` **only** (absolute). **`data/private` is never passed and never mounted** (in docker mode only `/data` (=public) ro + `/work` rw, network off).
  - `--working_dir` = the generation dir (`runs/run_<id>/gen_<n>`), read-write. **Predictions + logs go here.**
- Must call **only** the configured task model.
- Must write an **execution log**:
  - Single file `agent_execution.json` in working dir, **or**
  - Folder `agent_execution/` with `execution_q0.json, execution_q1.json, …` (multi-trajectory).
  - Format mirrors `sia/tasks/_shared/sample_agent_execution.json` (list of role/content/tool messages).
- Exit code 0 expected; non-zero still continues to the feedback agent but is marked FAILED.

### What the feedback agent actually SEES (so we can make improvement work)
`orchestrator._build_feedback_context`:
- `results.json` is injected **in full** (only skipped if >50 MB). ← **highest-signal channel.**
- Single-file execution log: JSON **truncated to 1000 chars** (`TRAJECTORY_PREVIEW_LIMIT`). Multi-trajectory: only **first 3** trajectories previewed (others must be read by the agent's own tools).
- Last 10 lines of target-agent stdout.
- The feedback agent can `Read`/`Bash` any file in the run dir.

**Design consequence:** put rich per-question diagnostics (predicted SQL, exec error, error-type
histogram, worked/failed examples) **into `results.json`**, because the raw execution log is heavily
truncated. This is how the Feedback-Agent will "discover" schema-linking / execute-and-repair / few-shot.

---

## 4. Verifier (`evaluate.py`) contract

From `EVALUATION_GUIDE.md`, `layout.find_evaluate_script`, `orchestrator.run_evaluation`:

- **Effective location = `data/public/evaluate.py` ONLY.** `find_evaluate_script(task_dir)` nominally
  checks `task_dir/data/public/evaluate.py` then `task_dir/evaluate.py`, BUT `run_generation` calls
  `run_evaluation(gen_dir, dataset_dir, …)` — passing the **dataset dir (`data/public`)** as the
  search root, not the real task dir. So both candidates resolve inside `data/public`, and a verifier
  placed at the task root is **never found** (the live smoke skipped evaluation entirely until we moved
  it). SIA's own `tests/test_run_evaluation.py` confirms: "create a minimal task dir with evaluate.py
  in data/public/."
- Invoked: `<venv python> <evaluate.py> --gen-dir <gen_dir>` with timeout `EVAL_TIMEOUT=600s`.
- Contract: **find the submission file yourself inside `--gen-dir`, score it, write `<gen_dir>/results.json`.**
  Exit non-zero or missing `results.json` ⇒ logged as error/warning (no score reaches feedback).
  Our `evaluate.py` always exits 0 and always writes `results.json` (even on a broken submission) so the
  feedback agent always gets a signal.
- Ground truth lives in `data/private/` (read by evaluate.py via `__file__.parent.parent.parent/data/private`).

### Anti-Goodhart posture (corrected)
The original plan (verifier at task root, invisible to the agent) is **not possible** — SIA only finds
the verifier in `data/public`. The real guarantee is simpler and still solid: **the gold answers live
only in `data/private/test_gold.jsonl`, which is never the agent's `--dataset_dir`** (that's
`data/public`). `task.md` instructs the agent to read only its dataset_dir, and `--sandbox docker`
makes `data/private` structurally unreachable (not mounted; network off). `evaluate.py` being in
`data/public` exposes only the *scoring method* (already disclosed in `task.md`) and a relative path
string to the private dir — exploitable only if the agent deliberately reads outside its instructions
(and impossible under docker). Verified by `tests/test_sia_integration.py`.

---

## 5. Task directory layout SIA requires

```
<task_dir>/
├── data/
│   ├── public/
│   │   ├── task.md                 # meta-agent reads this (REQUIRED, exact path data/public/task.md)
│   │   ├── evaluate.py             # our verifier — MUST be here for SIA to find it (see §4)
│   │   └── <our agent inputs>      # test_questions.jsonl, schemas.json, databases/, train.jsonl
│   └── private/                    # held-out gold (never shown to agent)
└── reference/
    ├── reference_target_agent.py   # seed shown to meta-agent (REQUIRED)
    └── SAMPLE_TASK_DESCRIPTIONS.md  # REQUIRED (load_task_files reads it unconditionally)
```
Notes:
- `SAMPLE_TASK_DESCRIPTIONS.md` is read unconditionally — **must exist** or the run crashes.
- `_shared/sample_agent_execution.json` is taken from the bundled `_shared` (resolved automatically for `--task_dir`, since `<task_dir>/../_shared` won't exist → falls back to bundled). Fine.
- `agent_reference: "default"` (target profile) ⇒ the meta-agent is shown `reference/reference_target_agent.py` inline.

## 6. Run artifacts (for the dashboard)

`runs/run_<id>/`:
- `context.md`, `profiles.json`
- `gen_<n>/`: `target_agent.py`, `agent_execution.json` (or `agent_execution/`), `results.json`,
  `target_agent_stdout.log`, `evaluation.log`, `meta_agent_prompt.txt` (gen 1),
  `feedback_agent_prompt.txt` + `improvement.md` (gen ≥ 2).
- The dashboard reads `gen_*/results.json` (accuracy + per-question detail) and `gen_*/improvement.md`.

## 7. Built-in web dashboard
`sia web --runs-dir ./runs` (or auto-started during `sia run` unless `--no-web`), serves on :8000.
We still build our own Streamlit dashboard per spec §5.4 (the before/after live-SQL playground is the differentiator the built-in viewer lacks).

---

## 8. ENVIRONMENT BLOCKERS on this machine (Windows 11) — must resolve before `sia run`

1. **`venv_python_path` is hardcoded POSIX** (`sia/layout.py`): `os.path.join(venv_dir, "bin", "python")`
   and `venv_pip_path` → `venv/bin/pip`. On Windows, `venv.create` produces `venv\Scripts\python.exe`.
   ⇒ Both the target-agent subprocess and `evaluate.py` launch will fail with FileNotFoundError.
   **SIA cannot run natively on Windows unprotected.** Fix options:
   - (a) Patch the clone (editable install) to make these two functions OS-aware (`Scripts/python.exe` on `nt`). ~6 lines. Lowest friction; keeps everything native.
   - (b) Run inside a real Linux env (WSL Ubuntu / Docker container with the repo mounted).
2. **No real WSL distro**: `wsl -l -v` shows only `docker-desktop` (stopped) — not a general Linux shell.
3. **`docker` CLI not on PATH**; **`uv` not installed**; venv creation will fall back to stdlib `venv`+`pip` (works, slower).
4. **`ANTHROPIC_API_KEY` is not set.** Required for BOTH the meta/feedback agents (Claude Agent SDK)
   and the target agent (anthropic SDK). Running the full loop also **costs real money** (meta/feedback
   are agentic multi-turn Claude Code sessions per generation; target = ~120 haiku calls/gen).
5. Python available only as `py` (3.13.3). No `python`/`pip` on PATH — use `py -m pip`, `py -m venv`.

**Conclusion:** All *static* deliverables (data prep, evaluate.py, task.md, reference agent, dashboard,
README) can be built and unit-tested offline now. Actually executing `sia run` additionally requires:
an API key, the Windows venv-path patch (option 8a), and acceptance of API cost.
