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
from tools.claude_client import MODEL_COSTS as _MODEL_COSTS

logger = logging.getLogger(__name__)

# Ollama reliability counters (reset on process restart)
_ollama_stats = {
    "calls": 0,
    "empty_responses": 0,
    "errors": 0,
    "fallbacks_to_claude": 0,
}


def get_ollama_stats() -> dict:
    """Return Ollama reliability counters for /health display."""
    total = _ollama_stats["calls"]
    failures = _ollama_stats["empty_responses"] + _ollama_stats["errors"]
    return {
        **_ollama_stats,
        "reliability_pct": round((1 - failures / max(total, 1)) * 100, 1),
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
        for attempt in range(2):
            _ollama_stats["calls"] += 1
            try:
                result = _call_ollama(prompt, system, model, max_tokens)
                if result.strip():
                    return result
                _ollama_stats["empty_responses"] += 1
                logger.warning(
                    "Ollama empty response (attempt %d/2), %s",
                    attempt + 1, "retrying" if attempt == 0 else "falling back to Claude",
                )
                if attempt == 0:
                    time.sleep(2)
            except Exception as e:
                _ollama_stats["errors"] += 1
                logger.warning("Ollama failed: %s, falling back to Claude", e)
                break
        _ollama_stats["fallbacks_to_claude"] += 1
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
    # Also check RAM: don't route to Ollama under critical memory pressure
    if purpose in ("classify", "plan") and complexity != "high" and _daily_spend_exceeds_threshold(0.7):
        if _ollama_available() and _ram_below_threshold(90):
            model = config.OLLAMA_CLASSIFY_MODEL if purpose == "classify" else config.OLLAMA_DEFAULT_MODEL
            return ("ollama", model)

    # Rule (c): Low-complexity classify/plan → try Ollama
    if purpose in ("classify", "plan") and complexity == "low":
        if _ollama_available() and _ram_below_threshold(75):
            model = config.OLLAMA_CLASSIFY_MODEL if purpose == "classify" else config.OLLAMA_DEFAULT_MODEL
            return ("ollama", model)

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
            costs = _MODEL_COSTS.get(model, {"input": 15.00, "output": 75.00})
            total += (inp * costs["input"] + (out + think) * costs["output"]) / 1_000_000
        return total

    except Exception as e:
        logger.warning("Failed to query daily spend: %s", e)
        return 0.0


def _call_ollama(prompt: str, system: str, model: str, max_tokens: int) -> str:
    """Call Ollama's /api/chat endpoint (Ollama v0.5+).

    Uses the chat completions API which is the stable endpoint for Ollama v0.5+.
    Falls back to /api/generate if /api/chat returns 404 (older Ollama versions).
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"num_predict": max_tokens or 2048},
    }
    try:
        response = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/chat",
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        raw_content = response.json().get("message", {}).get("content", "")
        content = raw_content
        # Strip reasoning model thinking blocks (e.g. DeepSeek R1)
        if "<think>" in content and "</think>" in content:
            content = content.split("</think>", 1)[-1].strip()
        # Handle unclosed think block (model produced only reasoning, no answer)
        elif "<think>" in content and "</think>" not in content:
            content = ""  # Incomplete thinking — treat as empty for retry
        if not content:
            logger.warning("Ollama returned empty content (raw length: %d)", len(raw_content))
        return content
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            logger.warning("Ollama /api/chat returned 404, falling back to /api/generate")
            return _call_ollama_generate(prompt, system, model, max_tokens)
        raise


def _call_ollama_generate(prompt: str, system: str, model: str, max_tokens: int) -> str:
    """Legacy fallback: call Ollama's /api/generate endpoint (pre-v0.5)."""
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": max_tokens or 2048},
    }
    if system:
        payload["system"] = system
    response = requests.post(
        f"{config.OLLAMA_BASE_URL}/api/generate",
        json=payload,
        timeout=120,
    )
    response.raise_for_status()
    content = response.json().get("response", "")
    # Strip reasoning model thinking blocks (e.g. DeepSeek R1)
    if "<think>" in content and "</think>" in content:
        content = content.split("</think>", 1)[-1].strip()
    # Handle unclosed think block (model produced only reasoning, no answer)
    elif "<think>" in content and "</think>" not in content:
        content = ""  # Incomplete thinking — treat as empty for retry
    return content
