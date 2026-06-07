"""Weave / W&B integration (sponsor: Weights & Biases).

Traces every target-agent call and every mutation as a Weave op, so the full
ancestry of a genome — and exactly how each parent answered/failed — is inspectable
in the Weave UI and queryable via the W&B MCP server. Per-generation fitness is logged
to W&B for the evolution dashboard.

Degrades gracefully: if `weave`/`wandb` aren't installed or `WANDB_API_KEY` is unset,
every method is a no-op, so the engine still runs fully offline.
"""
from __future__ import annotations

import contextlib
import os
from typing import Callable


def weave_enabled() -> bool:
    if not os.getenv("WANDB_API_KEY"):
        return False
    try:
        import weave  # noqa: F401
        import wandb  # noqa: F401
        return True
    except ImportError:
        return False


class Tracer:
    """Wraps injected model calls as Weave ops and logs fitness to W&B."""

    def __init__(self, project: str = "phylo", entity: str | None = None, enabled: bool | None = None):
        self.enabled = weave_enabled() if enabled is None else (enabled and weave_enabled())
        self.project = project
        self.weave = None
        self.wandb = None
        self.url = None
        if self.enabled:
            try:
                import wandb
                import weave
                full = f"{entity}/{project}" if entity else project
                client = weave.init(full)
                self.weave, self.wandb = weave, wandb
                self.url = getattr(client, "ui_url", None) or f"https://wandb.ai/{full}/weave"
            except Exception as e:  # noqa: BLE001 - a W&B network/auth blip must not kill the run
                print(f"[weave] init failed ({e}); continuing without tracing this run")
                self.enabled = False
                self.weave = self.wandb = None

    # ── tracing ───────────────────────────────────────────────────────────────
    def op(self, fn: Callable, name: str) -> Callable:
        """Return `fn` wrapped as a named Weave op (identity if disabled)."""
        if not self.enabled:
            return fn
        return self.weave.op(fn, name=name)

    def attributes(self, **kw):
        """Context manager tagging all ops inside it with agent/lineage metadata."""
        if not self.enabled:
            return contextlib.nullcontext()
        return self.weave.attributes(kw)

    # ── metrics ───────────────────────────────────────────────────────────────
    def log_generation(self, gen: int, populations: dict[int, list[float]]) -> None:
        """populations: {pop_index: [fitness, ...]}. Logs best/mean per pop + overall."""
        if not self.enabled:
            return
        all_f = [f for fs in populations.values() for f in fs]
        row = {"generation": gen, "overall_best": max(all_f, default=0.0),
               "overall_mean": (sum(all_f) / len(all_f)) if all_f else 0.0}
        for pop, fs in populations.items():
            row[f"pop{pop}_best"] = max(fs, default=0.0)
            row[f"pop{pop}_mean"] = (sum(fs) / len(fs)) if fs else 0.0
        with contextlib.suppress(Exception):
            self.wandb.log(row, step=gen)

    def finish(self) -> None:
        if self.enabled:
            with contextlib.suppress(Exception):
                self.wandb.finish()
