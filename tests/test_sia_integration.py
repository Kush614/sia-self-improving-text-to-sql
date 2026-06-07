#!/usr/bin/env python
"""Offline integration check: our task wired into the REAL (patched) SIA loader.

Exercises the same code paths sia.orchestrator.main() uses to assemble a run —
WITHOUT calling any model API (no key, no cost). Confirms:
  - the Windows venv-path patch is active
  - resolve_task_dir / profiles / agent reference / task files all load
  - the meta-agent prompt assembles and contains our task contract
  - the verifier is discovered by SIA (it lives in data/public, where the harness looks)

Run: python tests/test_sia_integration.py   (needs `pip install -e sia`)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASK_DIR = ROOT / "tasks" / "text-to-sql"

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  PASS  {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name}  {extra}")


def main():
    try:
        from sia.agent_reference import resolve_agent_reference
        from sia.layout import TaskLayout, resolve_task_dir, venv_python_path
        from sia.profiles import load_meta_agent_profile, load_target_agent_profile
        from sia.prompts import build_meta_prompt
        from sia.run_setup import load_task_files
    except ImportError as e:
        print(f"  FAIL  import sia ({e}); run: pip install -e sia")
        sys.exit(1)

    # Windows venv-path patch active?
    vp = venv_python_path("X")
    if os.name == "nt":
        check("patch: venv python -> Scripts\\python.exe", vp.endswith(os.path.join("Scripts", "python.exe")), vp)
    else:
        check("venv python -> bin/python", vp.endswith(os.path.join("bin", "python")), vp)

    task_dir, shared_dir = resolve_task_dir(None, str(TASK_DIR))
    check("resolve_task_dir resolves our task", Path(task_dir) == TASK_DIR, task_dir)
    check("shared_dir exists (bundled _shared fallback)", Path(shared_dir).is_dir(), shared_dir)

    layout = TaskLayout(task_dir, shared_dir)
    eval_script = layout.evaluate_script()
    check("verifier discovered", eval_script is not None and Path(eval_script).name == "evaluate.py", str(eval_script))
    check("verifier in data/public (where SIA's run_evaluation searches)",
          eval_script is not None and Path(eval_script).parent == TASK_DIR / "data" / "public", str(eval_script))
    # Gold stays out of the agent's reach: it is in data/private, never the dataset_dir.
    check("gold is in data/private, not data/public",
          (TASK_DIR / "data" / "private" / "test_gold.jsonl").exists()
          and not (TASK_DIR / "data" / "public" / "test_gold.jsonl").exists())

    from sia.config import Config
    cfg = Config()
    meta_profile = load_meta_agent_profile(cfg.DEFAULT_META_AGENT_PROFILE)
    target_profile = load_target_agent_profile(cfg.DEFAULT_TARGET_AGENT_PROFILE)
    resolved_ref = resolve_agent_reference(target_profile.agent_reference, layout)
    check("reference seed is our cold-start agent",
          resolved_ref.inline_seed is not None and "DELIBERATELY MINIMAL" in resolved_ref.inline_seed)

    task_files = load_task_files(task_dir, shared_dir, resolved_ref)
    check("task.md loaded", "Text-to-SQL" in task_files.task_md or "text-to-sql" in task_files.task_md.lower())
    check("sample descriptions loaded", "concert_singer" in task_files.sample_task_descriptions)
    check("sample agent execution loaded", isinstance(task_files.sample_agent_execution, (list, dict)))

    prompt = build_meta_prompt(
        task_files,
        target_profile.model,
        working_dir=str(ROOT / "runs" / "run_X" / "gen_1"),
        provider=target_profile.provider,
        reference_dir=None,
        focus="harness",
    )
    check("meta prompt mentions predictions.jsonl", "predictions.jsonl" in prompt)
    check("meta prompt mentions --dataset_dir / --working_dir", "--dataset_dir" in prompt and "--working_dir" in prompt)
    check("meta prompt embeds our task spec", "SELECT" in prompt and "execution accuracy" in prompt.lower())
    check("meta prompt embeds the reference seed", "DELIBERATELY MINIMAL" in prompt)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
