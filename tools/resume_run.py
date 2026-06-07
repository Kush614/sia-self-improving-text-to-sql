#!/usr/bin/env python
"""Resume SIA run_1 from generation 4 (gens 1-3 already exist and are preserved).

SIA has no native resume: orchestrator.main() always starts at gen 1 and refuses an
existing run dir. This driver reuses SIA's *own* orchestrator functions against the
existing run_1:

  1. Re-run only the crashed gen-3 -> gen-4 feedback step to (re)create
     gen_4/target_agent.py + gen_4/improvement.md.
  2. Drive run_generation() for gens 4..8 (each runs its target agent, scores it via
     evaluate.py, and produces the next gen's agent via the feedback agent).

Gens 1-3 (results.json, improvement.md, target_agent.py) are untouched. context.md is
appended to (ContextManager is constructed WITHOUT initialize(), which would overwrite).

Prereqs (set by the launching shell): ANTHROPIC_API_KEY, SIA_MAX_TURNS (raised).
Run from a non-shadowing cwd so the installed `sia` package is used, e.g.:
    python E:\\sia\\tools\\resume_run.py
"""
from __future__ import annotations

import os
from pathlib import Path

from sia.agent_reference import resolve_agent_reference
from sia.config import Config
from sia.context_manager import ContextManager
from sia.layout import RunLayout, TaskLayout, resolve_task_dir
from sia.logging_setup import configure_logging, get_logger
from sia.orchestrator import _build_feedback_context, _run_feedback_agent, run_generation
from sia.profiles import load_meta_agent_profile, load_target_agent_profile
from sia.run_setup import RunSetup, load_task_files

REPO = Path(r"E:\sia")
TASK = REPO / "tasks" / "text-to-sql"
RUN_DIR = str(REPO / "runs" / "run_1")
META_PROFILE = str(REPO / "profiles" / "sonnet-meta.json")

MAX_GEN = 8
RESUME_FROM = 4  # gen_1..gen_3 already done

configure_logging()
logger = get_logger("resume")


def main() -> None:
    env_config = Config.from_env()
    logger.info(f"Resuming run_1 from gen {RESUME_FROM} (max_gen={MAX_GEN}); "
                f"max_turns={env_config.DEFAULT_MAX_TURNS}")

    task_dir, shared_dir = resolve_task_dir(None, str(TASK))
    meta_profile = load_meta_agent_profile(META_PROFILE)
    target_profile = load_target_agent_profile(env_config.DEFAULT_TARGET_AGENT_PROFILE)
    task_layout = TaskLayout(task_dir, shared_dir)
    resolved_ref = resolve_agent_reference(target_profile.agent_reference, task_layout)
    task_files = load_task_files(task_dir, shared_dir, resolved_ref)

    dataset_dir = task_layout.dataset_dir
    abs_dataset_dir = task_layout.abs_dataset_dir
    task_model = target_profile.model
    target_provider = target_profile.provider

    layout = RunLayout(RUN_DIR)
    # Construct ContextManager WITHOUT initialize() so the existing context.md (gens 1-3) is kept.
    context_mgr = ContextManager(
        RUN_DIR,
        {
            "task_dir": task_dir,
            "meta_model": meta_profile.model,
            "task_model": task_model,
            "agent_impl": meta_profile.agent_impl,
            "max_gen": MAX_GEN,
        },
    )
    run_setup = RunSetup(
        run_directory=RUN_DIR,
        meta_agent_working_directory=layout.gen_dir(1),
        venv_dir=layout.venv_dir,
        context_mgr=context_mgr,
    )

    # ── Step 1: re-run the crashed gen-3 -> gen-4 feedback to create gen_4/target_agent.py ──
    prev = RESUME_FROM - 1  # 3
    gen_prev_dir = layout.gen_dir(prev)
    next_gen_dir = layout.gen_dir(RESUME_FROM)
    target_exists = os.path.exists(os.path.join(next_gen_dir, "target_agent.py"))
    if not target_exists:
        logger.info(f"Bootstrapping gen_{RESUME_FROM}: running feedback agent on gen_{prev}...")
        stdout_log_file = layout.stdout_log(prev)
        try:
            target_stdout = Path(stdout_log_file).read_text(encoding="utf-8")
        except OSError:
            target_stdout = ""
        execution_status, execution_section = _build_feedback_context(
            current_gen=prev,
            gen_dir=gen_prev_dir,
            dataset_dir=dataset_dir,
            target_agent_success=True,
            target_agent_error_msg="",
            target_agent_stdout=target_stdout,
            target_agent_stderr="",
            stdout_log_file=stdout_log_file,
            task_files=task_files,
            config=env_config,
        )
        _run_feedback_agent(
            current_gen=prev,
            max_gen=MAX_GEN,
            run_dir=RUN_DIR,
            next_gen_dir=next_gen_dir,
            task_files=task_files,
            execution_status=execution_status,
            execution_section=execution_section,
            meta_profile=meta_profile,
            env_config=env_config,
            dataset_dir=dataset_dir,
            task_model=task_model,
            target_provider=target_provider,
            focus="harness",
            resolved_ref=resolved_ref,
        )
        if not os.path.exists(os.path.join(next_gen_dir, "target_agent.py")):
            raise SystemExit(f"Feedback agent did not produce gen_{RESUME_FROM}/target_agent.py; aborting.")
        logger.info(f"  ✓ gen_{RESUME_FROM}/target_agent.py created")
    else:
        logger.info(f"gen_{RESUME_FROM}/target_agent.py already exists; skipping bootstrap")

    # ── Step 2: drive gens RESUME_FROM..MAX_GEN through the normal orchestrator path ──
    for current_gen in range(RESUME_FROM, MAX_GEN + 1):
        logger.info("=" * 80)
        logger.info(f"Resumed Generation {current_gen} of {MAX_GEN}")
        logger.info("=" * 80)
        run_generation(
            current_gen=current_gen,
            max_gen=MAX_GEN,
            run_setup=run_setup,
            task_files=task_files,
            abs_dataset_dir=abs_dataset_dir,
            dataset_dir=dataset_dir,
            meta_profile=meta_profile,
            sandbox="none",
            env_config=env_config,
            task_model=task_model,
            target_provider=target_provider,
            focus="harness",
            resolved_ref=resolved_ref,
        )

    context_mgr.finalize()
    logger.info("=" * 80)
    logger.info(f"Resume complete: run_1 now has generations 1..{MAX_GEN}.")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
