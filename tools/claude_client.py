from __future__ import annotations

import sqlite3
import threading
import time
import logging
from anthropic import Anthropic, APIError, APITimeoutError, RateLimitError

import config

logger = logging.getLogger(__name__)

_client = None


def _get_client() -> Anthropic:
    """Lazy-initialize the Anthropic client."""
    global _client
    if _client is None:
        _client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


# ── Persistent usage tracking ───────────────────────────────────────
# Uses synchronous sqlite3 (NOT aiosqlite) because this module runs inside
# worker threads via asyncio.to_thread. A threading.Lock guards writes so
# concurrent pipeline threads don't collide.

_usage_db_path = config.DB_PATH  # shares agentsutra.db, separate table
_usage_lock = threading.Lock()
_usage_db_initialized = False


def _init_usage_db():
    """Create the api_usage table if it doesn't exist. Called once lazily."""
    global _usage_db_initialized
    if _usage_db_initialized:
        return
    with _usage_lock:
        if _usage_db_initialized:
            return
        conn = sqlite3.connect(str(_usage_db_path), timeout=20.0)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    thinking_tokens INTEGER NOT NULL DEFAULT 0,
                    timestamp REAL NOT NULL
                )
            """)
            # Migration: add thinking_tokens column to existing databases
            try:
                conn.execute("ALTER TABLE api_usage ADD COLUMN thinking_tokens INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # Column already exists
            conn.commit()
        finally:
            conn.close()
        _usage_db_initialized = True
        logger.info("API usage table initialized")


def _persist_usage(model: str, input_tokens: int, output_tokens: int, ts: float, thinking_tokens: int = 0):
    """Write a single usage record to SQLite. Thread-safe via lock."""
    _init_usage_db()
    with _usage_lock:
        conn = sqlite3.connect(str(_usage_db_path), timeout=20.0)
        try:
            conn.execute(
                "INSERT INTO api_usage (model, input_tokens, output_tokens, thinking_tokens, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (model, input_tokens, output_tokens, thinking_tokens, ts),
            )
            conn.commit()
        finally:
            conn.close()


class BudgetExceededError(RuntimeError):
    """Raised when daily or monthly API spend exceeds the configured budget."""
    pass


def _check_budget():
    """Check daily and monthly spend against configured limits. Raises BudgetExceededError."""
    daily_limit = config.DAILY_BUDGET_USD
    monthly_limit = config.MONTHLY_BUDGET_USD
    if not daily_limit and not monthly_limit:
        return  # No budget limits configured

    _init_usage_db()
    try:
        conn = sqlite3.connect(str(_usage_db_path), timeout=10.0)
        try:
            now = time.time()
            checks = []
            if daily_limit:
                checks.append(("daily", now - 86400, daily_limit))
            if monthly_limit:
                checks.append(("monthly", now - 86400 * 30, monthly_limit))

            for label, cutoff_ts, limit in checks:
                rows = conn.execute(
                    "SELECT model, SUM(input_tokens), SUM(output_tokens), SUM(thinking_tokens) "
                    "FROM api_usage WHERE timestamp > ? GROUP BY model",
                    (cutoff_ts,),
                ).fetchall()
                total_cost = 0.0
                for model_name, inp, out, think in rows:
                    think = think or 0
                    costs = MODEL_COSTS.get(model_name, {"input": 3.00, "output": 15.00})
                    total_cost += (inp * costs["input"] + (out + think) * costs["output"]) / 1_000_000
                if total_cost >= limit:
                    raise BudgetExceededError(
                        f"{label.title()} budget exceeded: ${total_cost:.2f} >= ${limit:.2f} limit. "
                        f"Adjust {label.upper()}_BUDGET_USD in .env or wait for the period to reset."
                    )
        finally:
            conn.close()
    except BudgetExceededError:
        raise
    except Exception as e:
        logger.warning("Budget check failed (allowing call): %s", e)


def call(
    prompt: str,
    system: str = "",
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    thinking: bool = False,
) -> str:
    """Call Claude API with retry and backoff. Returns response text.

    Args:
        thinking: Enable adaptive extended thinking for deeper reasoning.
                  When True, temperature is not set (API requirement) and
                  max_tokens is floored at 16000 for thinking headroom.

    WARNING: Uses synchronous time.sleep() for retry backoff. This is safe
    ONLY because it is executed via asyncio.to_thread() from the Telegram
    handler. Calling this directly from an async handler will freeze the
    entire bot's event loop.
    """
    _check_budget()

    # Runtime guard: detect if called from async event loop (would freeze the bot)
    try:
        import asyncio as _aio
        loop = _aio.get_running_loop()
        if loop.is_running():
            logger.error(
                "claude_client.call() invoked from a running event loop! "
                "This WILL freeze the bot. Wrap in asyncio.to_thread()."
            )
    except RuntimeError:
        pass  # No running event loop — correct usage

    model = model or config.DEFAULT_MODEL
    messages = [{"role": "user", "content": prompt}]

    for attempt in range(config.API_MAX_RETRIES):
        try:
            kwargs = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if system:
                kwargs["system"] = system

            # Extended thinking mode — adaptive thinking for supported models
            if thinking and config.ENABLE_THINKING:
                kwargs["thinking"] = {"type": "adaptive"}
                # max_tokens is the TOTAL budget for thinking + text combined.
                # If too low, Claude can consume it all on thinking and return
                # zero text blocks. Floor at 128000 to guarantee text headroom.
                if kwargs["max_tokens"] < 128000:
                    kwargs["max_tokens"] = 128000
                # Do NOT set temperature when thinking is enabled (API requirement)
            else:
                kwargs["temperature"] = temperature

            # Streaming required for thinking calls — Anthropic enforces a
            # 10-minute hard limit on non-streaming requests, and complex
            # thinking tasks easily exceed that.
            if thinking and config.ENABLE_THINKING:
                with _get_client().messages.stream(**kwargs) as stream:
                    response = stream.get_final_message()
            else:
                response = _get_client().messages.create(**kwargs)

            # Track usage (in-memory + persist to SQLite)
            usage_ts = time.time()
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens

            # Log thinking tokens if present
            thinking_tokens = getattr(response.usage, "thinking_tokens", 0) or 0
            logger.info(
                "Claude API call: model=%s input=%d output=%d%s",
                model,
                input_tokens,
                output_tokens,
                f" thinking={thinking_tokens}" if thinking_tokens else "",
            )

            try:
                _persist_usage(model, input_tokens, output_tokens, usage_ts, thinking_tokens)
            except Exception as e:
                logger.warning("Failed to persist usage record: %s", e)

            if not response.content:
                raise RuntimeError("Claude returned empty response")

            # With thinking mode, response contains thinking + text blocks.
            # Extract only text blocks; skip thinking blocks.
            text_parts = []
            for block in response.content:
                if block.type == "text":
                    text_parts.append(block.text)
            if not text_parts:
                raise RuntimeError("Claude returned no text content")
            return "\n".join(text_parts)

        except RateLimitError:
            wait = 2 ** (attempt + 1)
            logger.warning("Rate limited, waiting %ds (attempt %d)", wait, attempt + 1)
            time.sleep(wait)
        except APITimeoutError:
            wait = 2 ** attempt
            logger.warning("Timeout, waiting %ds (attempt %d)", wait, attempt + 1)
            time.sleep(wait)
        except APIError as e:
            if attempt == config.API_MAX_RETRIES - 1:
                logger.error("Claude API error after %d attempts: %s", config.API_MAX_RETRIES, e)
                raise
            wait = 2 ** attempt
            logger.warning("API error: %s, retrying in %ds", e, wait)
            time.sleep(wait)

        # Retry on empty/thinking-only responses (transient API behaviour)
        except RuntimeError as e:
            if "no text content" in str(e) or "empty response" in str(e):
                if attempt == config.API_MAX_RETRIES - 1:
                    logger.error("Claude returned no usable text after %d attempts", config.API_MAX_RETRIES)
                    raise
                wait = 2 ** (attempt + 1)
                logger.warning("Empty/thinking-only response, retrying in %ds (attempt %d)", wait, attempt + 1)
                time.sleep(wait)
            else:
                raise  # Don't swallow unrelated RuntimeErrors

    raise RuntimeError(f"Claude API failed after {config.API_MAX_RETRIES} attempts")


def get_usage_summary() -> dict:
    """Return total token usage from persistent storage (survives restarts)."""
    _init_usage_db()
    try:
        conn = sqlite3.connect(str(_usage_db_path), timeout=20.0)
        try:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0), "
                "COALESCE(SUM(thinking_tokens), 0) FROM api_usage"
            ).fetchone()
            return {
                "total_calls": row[0],
                "total_input_tokens": row[1],
                "total_output_tokens": row[2],
                "total_thinking_tokens": row[3],
            }
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Failed to read usage summary: %s", e)
        return {"total_calls": 0, "total_input_tokens": 0, "total_output_tokens": 0, "total_thinking_tokens": 0}


# ── Cost estimation ──────────────────────────────────────────────────

# USD per million tokens (input, output) — updated for gen-4 models
MODEL_COSTS = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-opus-4-6": {"input": 15.00, "output": 75.00},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
}


def get_cost_summary() -> dict:
    """Return estimated API costs broken down by model."""
    _init_usage_db()
    result = {
        "total_calls": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_thinking_tokens": 0,
        "total_cost_usd": 0.0,
        "by_model": {},
    }
    try:
        conn = sqlite3.connect(str(_usage_db_path), timeout=20.0)
        try:
            rows = conn.execute(
                "SELECT model, COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(thinking_tokens) "
                "FROM api_usage GROUP BY model"
            ).fetchall()
            for model, calls, inp, out, think in rows:
                think = think or 0
                costs = MODEL_COSTS.get(model, {"input": 3.00, "output": 15.00})
                cost_usd = (inp * costs["input"] + (out + think) * costs["output"]) / 1_000_000
                result["total_calls"] += calls
                result["total_input_tokens"] += inp
                result["total_output_tokens"] += out
                result["total_thinking_tokens"] += think
                result["total_cost_usd"] += cost_usd
                result["by_model"][model] = {
                    "calls": calls,
                    "input_tokens": inp,
                    "output_tokens": out,
                    "thinking_tokens": think,
                    "cost_usd": cost_usd,
                }
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Failed to read cost summary: %s", e)
    return result
