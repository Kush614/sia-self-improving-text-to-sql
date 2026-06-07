# README_RUN — Self-Improving Text-to-SQL Agent (SIA, Applied track)

Reproduce commands for this build. Paths assume the project root `E:\sia`.
Read `INTERFACE_NOTES.md` first — it is the verified SIA contract and overrides the
original `specs.md` where they differ (the verifier is `evaluate.py` not `eval.py`,
the CLI is `sia run …`, models come from profiles/env not `--task_model`, etc.).

> **Platform note:** this machine is Windows 11 with Python only as the `py`
> launcher. SIA hardcodes a POSIX venv path; we patched the cloned copy
> (`sia/sia/layout.py`, `venv_python_path`/`venv_pip_path`) to be OS-aware. See
> `INTERFACE_NOTES.md` §8.

---

## 0. Layout produced by this build

```
E:\sia\
├── INTERFACE_NOTES.md          # verified SIA contract (Task 0)
├── README_RUN.md               # this file
├── prep_data.py                # builds the task data from Spider
├── make_sample_run.py          # labeled dashboard fixture (NOT a real run)
├── sia\                        # cloned + patched SIA framework (hexo-ai/sia)
├── downloads\                  # spider_old.zip + extracted Spider
├── tasks\text-to-sql\
│   ├── data\public\            # task.md, evaluate.py (verifier), test_questions.jsonl, train.jsonl,
│   │                           #   schemas.json, databases\<db>\<db>.sqlite
│   ├── data\private\test_gold.jsonl   # gold — agent NEVER sees this
│   ├── reference\              # reference_target_agent.py + SAMPLE_TASK_DESCRIPTIONS.md
│   │   (gold stays in data\private\, never in the agent's dataset_dir)
├── dashboard\app.py            # Streamlit demo
├── tests\                      # offline test suites (no API key needed)
└── runs_sample\run_demo\       # labeled fixture for the dashboard
```

## 1. Environment (one-time)

```powershell
# Python 3.13 via the py launcher is already present.
py -m venv E:\sia\.venv
& E:\sia\.venv\Scripts\python.exe -m pip install huggingface_hub datasets streamlit pandas gdown

# Clone SIA (already done here) and editable-install the PATCHED copy:
git clone https://github.com/hexo-ai/sia.git E:\sia\sia      # arXiv:2605.27276
& E:\sia\.venv\Scripts\python.exe -m pip install -e E:\sia\sia
```

The Windows venv-path patch is already applied to `E:\sia\sia\sia\layout.py`. If you
re-clone fresh, re-apply it (make `venv_python_path`/`venv_pip_path` return
`Scripts\python.exe` / `Scripts\pip.exe` when `os.name == "nt"`).

## 2. Build the task data (real Spider)

The Spider SQLite databases are not on HuggingFace (`xlangai/spider` ships only
parquet). We pulled the official archive via gdown:

```powershell
& E:\sia\.venv\Scripts\python.exe -m gdown "1TqleXec_OykOYFREKKtschzY29dUcVAQ" -O E:\sia\downloads\spider_old.zip
Expand-Archive E:\sia\downloads\spider_old.zip -DestinationPath E:\sia\downloads\spider_extracted -Force

# Build tasks\text-to-sql\data\* deterministically (120 scored, 200 train, 8 DBs):
& E:\sia\.venv\Scripts\python.exe E:\sia\prep_data.py
```
`prep_data.py` asserts 100% of emitted gold queries execute on the copied DBs.

## 3. Offline tests (no API key, no cost)

```powershell
& E:\sia\.venv\Scripts\python.exe E:\sia\tests\test_evaluate.py        # verifier: 21 checks
& E:\sia\.venv\Scripts\python.exe E:\sia\tests\test_reference_agent.py # agent wiring incl. 100% oracle path
& E:\sia\.venv\Scripts\python.exe E:\sia\make_sample_run.py            # build dashboard fixture
& E:\sia\.venv\Scripts\python.exe E:\sia\tests\test_dashboard.py       # dashboard helpers: 9 checks
```

## 4. Demo dashboard

### Primary: standalone interactive frontend (Gumroad-style, light/dark, offline)
Snapshot the run into a self-contained cache, then open the page. Works fully offline
(no server, no model, no live DB) and keeps showing results even if the SIA run stops.

```powershell
& E:\sia\.venv\Scripts\python.exe E:\sia\build_cache.py --run-id 1   # -> dashboard\cache\demo_data.js
# Re-run build_cache.py any time to refresh as new generations land.
```
Open `E:\sia\dashboard\index.html` directly in a browser (it reads the cached
`demo_data.js` via a <script> tag, so file:// works), or serve it:
```powershell
& E:\sia\.venv\Scripts\python.exe -m http.server 8700 --directory E:\sia\dashboard   # http://127.0.0.1:8700
```
Panels: hero climb, accuracy-by-generation SVG chart, per-generation `improvement.md`
(the agent's self-edits), and a before→after playground with **pre-executed** result
tables (wrong table vs right table vs gold). Theme toggle persists in localStorage.

### Secondary: Streamlit (live read-only SQL execution)
```powershell
& E:\sia\.venv\Scripts\python.exe -m streamlit run E:\sia\dashboard\app.py
```
Sidebar → set *Runs directory* to `E:\sia\runs_sample` (labeled fixture) or `E:\sia\runs`.

## 5. Run the SIA self-improvement loop (NEEDS API KEY + COSTS MONEY)

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."     # required by BOTH meta/feedback + target agents
# Optional model overrides (there is NO --task_model/--meta_model flag):
$env:SIA_META_MODEL = "sonnet"            # stronger feedback agent (default: haiku)
$env:SIA_TASK_MODEL = "claude-haiku-4-5-20251001"   # the target/answer model

# Smoke (2 gens), then the full run (fresh run_id each time — SIA refuses to reuse one):
sia run --task_dir E:\sia\tasks\text-to-sql --max_gen 2 --run_id 1
sia run --task_dir E:\sia\tasks\text-to-sql --max_gen 8 --run_id 2
```
- The verifier (`evaluate.py`) runs automatically after each generation and writes
  `runs\run_<id>\gen_<n>\results.json`.
- Artifacts per generation: `target_agent.py`, `predictions.jsonl`,
  `agent_execution.json` (or `agent_execution\`), `results.json`,
  `improvement.md` (gen ≥ 2), `target_agent_stdout.log`, `evaluation.log`.
- A live dashboard auto-starts on http://127.0.0.1:8000 during the run
  (`--no-web` to disable). Our Streamlit app is the richer demo (live before/after
  SQL playground).

### Cost / scaling levers (see specs.md §8)
- Keep the scored set ~120 (already) and `--max_gen` modest.
- `SIA_TASK_MODEL=haiku` keeps the high-volume answer calls cheap.
- Optional Nebius (sponsor) task model: author a target profile with an
  OpenAI-compatible provider and pass `--target-agent-profile path.json`
  (SIA's meta-agent refactors the target to the `openai` SDK automatically).

## 6. What "improvement" looks like
After ≥2 generations, `runs\run_<id>\gen_*\improvement.md` describe the concrete,
self-discovered harness edits (schema-linking, execute-and-repair, few-shot from
`train.jsonl`, robust SQL extraction, self-consistency). The dashboard's panel 2
renders these; panel 3 executes a gen-1-wrong / best-gen-right query live.

### Anti-Goodhart (Q&A)
The gold answers live only in `data\private\test_gold.jsonl`, which is **never** the
agent's `--dataset_dir` (that's `data\public`). `evaluate.py` must live in
`data\public` (the only place SIA's harness looks for it), but it contains no
answers — only the scoring method (already stated in `task.md`). `task.md` instructs
the agent to read only its dataset_dir, and `--sandbox docker` makes `data\private`
structurally unreachable (not mounted; network off). The task model is fixed every
generation — gains are harness-only (public SIA does not touch weights).
