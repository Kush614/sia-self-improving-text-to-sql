"""Real model calls: Anthropic target agent + Claude/OpenAI diverse mutation.

These are the injectable seams the engine expects:
  - generate_fn(system, user) -> sql        (target agent; Claude Haiku)
  - mutate_fn(prompt) -> raw json           (meta agent; Claude Sonnet and/or GPT)

Diverse mutation (alternating Claude + OpenAI) is the SPECS.md "diverse mutation
strategies" point. All calls retry with backoff; mutation degrades to a no-op ("")
on failure so one bad call never crashes a multi-generation run.
"""
from __future__ import annotations

import os
import random
import time

TARGET_MODEL = os.getenv("PHYLO_TARGET_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_MUTATE_MODEL = os.getenv("PHYLO_CLAUDE_MUTATE_MODEL", "claude-sonnet-4-6")
OPENAI_MUTATE_MODEL = os.getenv("PHYLO_OPENAI_MUTATE_MODEL", "gpt-5")
OPENAI_FALLBACK_MODEL = os.getenv("PHYLO_OPENAI_FALLBACK_MODEL", "gpt-4o")
MAX_RETRIES = 4
MUTATE_SYSTEM = "You are an expert AI engineer improving a text-to-SQL agent's harness. Respond with a JSON object only."


def _anthropic():
    import anthropic
    return anthropic.Anthropic()


def _openai():
    from openai import OpenAI
    return OpenAI()


# ── target agent (Anthropic) ─────────────────────────────────────────────────

def anthropic_generate(system: str, user: str, model: str = TARGET_MODEL, max_tokens: int = 1024) -> str:
    client = _anthropic()
    last = None
    for i in range(MAX_RETRIES):
        try:
            r = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                       messages=[{"role": "user", "content": user}])
            return "".join(b.text for b in r.content if getattr(b, "type", None) == "text")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 ** i)
    raise RuntimeError(f"anthropic generate failed after {MAX_RETRIES}: {last}")


def make_target_generate(model: str = TARGET_MODEL):
    return lambda system, user: anthropic_generate(system, user, model=model)


# ── mutation (Claude + OpenAI) ───────────────────────────────────────────────

def claude_mutate(prompt: str, model: str = CLAUDE_MUTATE_MODEL, max_tokens: int = 2500) -> str:
    client = _anthropic()
    for i in range(MAX_RETRIES):
        try:
            r = client.messages.create(model=model, max_tokens=max_tokens, system=MUTATE_SYSTEM,
                                       messages=[{"role": "user", "content": prompt}])
            return "".join(b.text for b in r.content if getattr(b, "type", None) == "text")
        except Exception:  # noqa: BLE001
            time.sleep(2 ** i)
    return ""  # no-op mutation rather than crash the run


def openai_mutate(prompt: str, model: str = OPENAI_MUTATE_MODEL, max_tokens: int = 2500) -> str:
    client = _openai()
    for attempt_model in (model, OPENAI_FALLBACK_MODEL):
        for i in range(MAX_RETRIES):
            try:
                r = client.chat.completions.create(
                    model=attempt_model,
                    messages=[{"role": "system", "content": MUTATE_SYSTEM}, {"role": "user", "content": prompt}],
                )
                return r.choices[0].message.content or ""
            except Exception as e:  # noqa: BLE001
                msg = str(e).lower()
                if "model" in msg and ("not" in msg or "exist" in msg or "access" in msg):
                    break  # unavailable model → try the fallback model
                time.sleep(2 ** i)
    return ""


def make_cached_generate(base, redis_url: str | None = None):
    """Wrap a generate_fn with a Redis cache keyed by (model, system, user).

    Memoizes identical target-agent calls — e.g. elites carried unchanged across
    generations re-evaluate the exact same prompts, so this saves real LLM cost
    (~the elite/duplicate fraction of calls). Degrades to the base fn if Redis is down.
    """
    import hashlib
    url = redis_url or os.getenv("REDIS_URL")
    if not url:
        return base
    try:
        import redis
        r = redis.from_url(url, decode_responses=True)
        r.ping()
    except Exception:  # noqa: BLE001
        return base

    hits = {"n": 0, "miss": 0}

    def gen(system: str, user: str) -> str:
        key = "phylo:cache:gen:" + hashlib.sha256(
            (TARGET_MODEL + "\x00" + system + "\x00" + user).encode("utf-8")).hexdigest()
        try:
            cached = r.get(key)
        except Exception:  # noqa: BLE001
            cached = None
        if cached is not None:
            hits["n"] += 1
            return cached
        out = base(system, user)
        hits["miss"] += 1
        try:
            r.set(key, out)
        except Exception:  # noqa: BLE001
            pass
        return out

    gen.cache_stats = hits  # type: ignore[attr-defined]
    return gen


def make_diverse_mutate(seed: int = 0, openai_share: float = 0.5):
    """Alternate Claude/OpenAI mutation for strategy diversity across the population."""
    rng = random.Random(seed)
    have_openai = bool(os.getenv("OPENAI_API_KEY"))

    def mutate(prompt: str) -> str:
        if have_openai and rng.random() < openai_share:
            out = openai_mutate(prompt)
            if out:
                return out
        return claude_mutate(prompt)

    return mutate
