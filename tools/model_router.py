"""Model router — decides whether to use Claude or Ollama for each call.

Routing rules:
  - audit     → ALWAYS Claude Opus (cross-model adversarial review invariant)
  - code_gen  → ALWAYS Claude Sonnet (quality-critical)
  - classify / plan with complexity="low" → Ollama if available + RAM < 75%
  - Budget escalation: if daily spend > 70% of DAILY_BUDGET_USD → Ollama
  - Everything else → Claude Sonnet
"""
from __future__ import annotations

import logging
import sqlite3
import time

import requests

import config
from tools import claude_client

logger = logging.getLogger(__name__)

# ── Cost lookup (mirrors claude_client.MODEL_COSTS) ──────────────────
_MODEL_COSTS = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-opus-4-6": {"input": 15.00, "output": 75.00},
}


def route_and_call(
    prompt: str,
    system: str = "",
    purpose: str = "general",
    complexity: str = "high",
    max_tokens: int = 2000,
    temperature: float = 0.0,
    thinking: bool = False,
) -> str:
    """Route to optimal model and execute the call. Returns response text."""

    provider, model = _select_model(purpose, complexity)
    logger.info("Routed %s (complexity=%s) to %s/%s", purpose, complexity, provider, model)

    if provider == "ollama":
        try:
            return _call_ollama(prompt, system, model, max_tokens)
        except Exception as e:
            logger.warning("Ollama call failed, falling back to Claude: %s", e)
            provider, model = "claude", config.DEFAULT_MODEL

    # Claude path
    return claude_client.call(
        prompt,
        system=system,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        thinking=thinking,
    )


def _select_model(purpose: str, complexity: str) -> tuple[str, str]:
    """Decide (provider, model) based on purpose, complexity, and resource state."""

    # Rule (a): Audit → ALWAYS Opus
    if purpose == "audit":
        return ("claude", config.COMPLEX_MODEL)

    # Rule (b): Code generation → ALWAYS Sonnet
    if purpose == "code_gen":
        return ("claude", config.DEFAULT_MODEL)

    # Rule (d): Budget escalation — check before complexity routing
    if purpose in ("classify", "plan") and _daily_spend_exceeds_threshold(0.7):
        if _ollama_available():
            return ("ollama", config.OLLAMA_DEFAULT_MODEL)

    # Rule (c): Low-complexity classify/plan → try Ollama
    if purpose in ("classify", "plan") and complexity == "low":
        if _ollama_available() and _ram_below_threshold(75):
            return ("ollama", config.OLLAMA_DEFAULT_MODEL)

    # Rule (e): Default → Sonnet
    return ("claude", config.DEFAULT_MODEL)


# ── Ollama helpers ───────────────────────────────────────────────────

def _ollama_available() -> bool:
    """Check if Ollama API is responding (2s timeout)."""
    try:
        r = requests.get(f"{config.OLLAMA_BASE_URL}/api/tags", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _ram_below_threshold(percent: int) -> bool:
    """True if current RAM usage is below the given percentage."""
    try:
        import psutil
        return psutil.virtual_memory().percent < percent
    except ImportError:
        # psutil not installed — default to safe (don't route to Ollama)
        return False


def _daily_spend_exceeds_threshold(fraction: float) -> bool:
    """True if today's spend > fraction * DAILY_BUDGET_USD."""
    if not config.DAILY_BUDGET_USD:
        return False  # No budget set, never escalate

    today_spend = _get_today_spend()
    return today_spend > (config.DAILY_BUDGET_USD * fraction)


def _get_today_spend() -> float:
    """Query api_usage table for today's total cost."""
    try:
        from tools.claude_client import _usage_db_path, _init_usage_db
        _init_usage_db()

        # Start of today (UTC midnight) as Unix timestamp
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        midnight_ts = midnight.timestamp()

        conn = sqlite3.connect(str(_usage_db_path), timeout=10.0)
        try:
            rows = conn.execute(
                "SELECT model, SUM(input_tokens), SUM(output_tokens), SUM(thinking_tokens) "
                "FROM api_usage WHERE timestamp > ? GROUP BY model",
                (midnight_ts,),
            ).fetchall()
        finally:
            conn.close()

        total = 0.0
        for model, inp, out, think in rows:
            think = think or 0
            costs = _MODEL_COSTS.get(model, {"input": 3.00, "output": 15.00})
            total += (inp * costs["input"] + (out + think) * costs["output"]) / 1_000_000
        return total

    except Exception as e:
        logger.warning("Failed to query daily spend: %s", e)
        return 0.0


def _call_ollama(prompt: str, system: str, model: str, max_tokens: int) -> str:
    """Call Ollama's /api/generate endpoint."""
    full_prompt = f"{system}\n\n{prompt}" if system else prompt
    response = requests.post(
        f"{config.OLLAMA_BASE_URL}/api/generate",
        json={"model": model, "prompt": full_prompt, "stream": False},
        timeout=60,
    )
    response.raise_for_status()
    return response.json().get("response", "")
