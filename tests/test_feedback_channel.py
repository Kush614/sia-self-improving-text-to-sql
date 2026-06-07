#!/usr/bin/env python
"""Prove our results.json diagnostics actually reach the Feedback-Agent prompt.

This de-risks the #1 "no improvement" failure mode: the Feedback-Agent can only
fix what it can see. We drive a REAL results.json (from the fixture, produced by
our evaluate.py) through SIA's actual `_build_feedback_context` +
`build_feedback_prompt` and assert the error histogram + concrete failure samples
survive into the prompt text the feedback agent receives. No API calls.

Run: python tests/test_feedback_channel.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASK_DIR = ROOT / "tasks" / "text-to-sql"
PUBLIC = TASK_DIR / "data" / "public"
FIXTURE_RESULTS = ROOT / "runs_sample" / "run_demo" / "gen_1" / "results.json"

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  PASS  {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name}  {extra}")


def main():
    from sia.config import Config
    from sia.layout import resolve_task_dir
    from sia.orchestrator import _build_feedback_context
    from sia.profiles import load_target_agent_profile
    from sia.prompts import build_feedback_prompt
    from sia.agent_reference import resolve_agent_reference
    from sia.layout import TaskLayout
    from sia.run_setup import load_task_files

    cfg = Config()
    task_dir, shared_dir = resolve_task_dir(None, str(TASK_DIR))
    layout = TaskLayout(task_dir, shared_dir)
    target_profile = load_target_agent_profile(cfg.DEFAULT_TARGET_AGENT_PROFILE)
    resolved_ref = resolve_agent_reference(target_profile.agent_reference, layout)
    task_files = load_task_files(task_dir, shared_dir, resolved_ref)

    results = json.loads(FIXTURE_RESULTS.read_text(encoding="utf-8"))
    a_failing_question = results["failure_samples"][0]["question"]

    with tempfile.TemporaryDirectory() as td:
        gen_dir = Path(td)
        shutil.copy2(FIXTURE_RESULTS, gen_dir / "results.json")
        # Minimal agent_execution.json so the loader takes the single-file path.
        (gen_dir / "agent_execution.json").write_text(
            json.dumps([{"role": "user", "content": "q"}, {"role": "assistant", "content": "SELECT 1"}]),
            encoding="utf-8")

        execution_status, execution_section = _build_feedback_context(
            current_gen=1,
            gen_dir=str(gen_dir),
            dataset_dir=str(PUBLIC),
            target_agent_success=True,
            target_agent_error_msg="",
            target_agent_stdout="...\nWrote 120 predictions\n",
            target_agent_stderr="",
            stdout_log_file=str(gen_dir / "target_agent_stdout.log"),
            task_files=task_files,
            config=cfg,
        )

    # The orchestrator injects results.json into execution_status.
    check("accuracy reaches feedback channel", "0.3" in execution_status, "")
    check("error histogram reaches feedback channel", "error_summary" in execution_status
          and "wrong-result" in execution_status)
    check("concrete failure samples reach feedback channel", "failure_samples" in execution_status)
    check("a specific failing question is visible", a_failing_question[:30] in execution_status,
          a_failing_question[:30])

    # And it survives assembly into the full feedback prompt the agent is sent.
    prompt = build_feedback_prompt(
        current_gen=1, max_gen=8, task_files=task_files,
        agent_py="# current target agent\n", task=task_files.task_md,
        execution_status=execution_status, execution_section=execution_section,
        run_dir=str(ROOT / "runs" / "run_1"), next_gen_dir=str(ROOT / "runs" / "run_1" / "gen_2"),
        previous_gens="None", task_model=target_profile.model, provider=target_profile.provider,
    )
    check("diagnostics survive into final feedback prompt",
          "error_summary" in prompt and a_failing_question[:30] in prompt)
    check("feedback prompt asks for improvement.md + target_agent.py",
          "improvement.md" in prompt and "target_agent.py" in prompt)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
