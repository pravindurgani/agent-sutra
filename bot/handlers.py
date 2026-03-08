from __future__ import annotations

import json
import re
import time as _time
import uuid
import asyncio
import functools
import logging
from pathlib import Path
from datetime import datetime, timezone

from telegram import Update, Bot
from telegram.ext import ContextTypes

import config
from brain.graph import run_task, get_stage, clear_stage
from storage import db
from tools.file_manager import save_upload
from tools.claude_client import get_usage_summary, get_cost_summary, get_daily_cost_breakdown, get_budget_remaining

logger = logging.getLogger(__name__)


def _check_resources(running_tasks: dict) -> str | None:
    """Check RAM and concurrent task limits. Returns rejection message or None."""
    import psutil

    # Prune completed tasks to prevent memory leak from accumulated futures
    done_ids = [tid for tid, f in running_tasks.items() if f.done()]
    for tid in done_ids:
        del running_tasks[tid]

    active = len(running_tasks)
    if active >= config.MAX_CONCURRENT_TASKS:
        return (
            f"Too many concurrent tasks ({active}/{config.MAX_CONCURRENT_TASKS}). "
            "Wait for one to finish or /cancel."
        )

    mem = psutil.virtual_memory()
    if mem.percent >= config.RAM_THRESHOLD_PERCENT:
        return (
            f"System memory at {mem.percent:.0f}% "
            f"(threshold: {config.RAM_THRESHOLD_PERCENT}%). "
            "Wait for tasks to finish."
        )

    return None


def _sanitize_error_for_user(error: str) -> str:
    """Sanitize error messages before sending to user via Telegram.

    Strips internal paths, API key fragments, and raw tracebacks.
    Preserves meaningful error descriptions.
    """
    msg = error[:500]
    # Strip absolute paths (keep just the filename)
    msg = re.sub(r'/[\w/.-]+/([^/\s]+)', r'\1', msg)
    # Strip anything that looks like an API key or token fragment
    msg = re.sub(r'(sk-|api[-_]key|token)[^\s,]{8,}', '[REDACTED]', msg, flags=re.IGNORECASE)
    return msg


# Stage labels for streaming status
STAGE_LABELS = {
    "classifying": "Classifying task...",
    "planning": "Creating execution plan...",
    "executing": "Generating and running code...",
    "auditing": "Auditing output quality...",
    "delivering": "Preparing response...",
}


def auth_required(func):
    """Decorator to restrict access to allowed user IDs."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # A-31: Guard against edited messages where update.message is None
        if not update.message:
            return
        if not update.effective_user:
            return
        user_id = update.effective_user.id
        if user_id not in config.ALLOWED_USER_IDS:
            logger.warning("Unauthorized access attempt from user %d", user_id)
            await update.message.reply_text("Unauthorized. Your user ID is not in the allow list.")
            return
        return await func(update, context)
    return wrapper


@auth_required
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    await update.message.reply_text(
        f"AgentSutra v{config.VERSION} is online.\n\n"
        "Send me a task:\n"
        "- Text prompts for code generation, data analysis, or automation\n"
        "- Files (CSV, Excel, images) with instructions\n"
        "- Invoke existing projects (job scraper, reports, etc.)\n"
        "- Build production frontends (React, Tailwind)\n"
        "- Scrape the web, call APIs, process big data\n\n"
        "Commands:\n"
        "/status - Current task status\n"
        "/history - Recent tasks\n"
        "/usage - API token usage\n"
        "/cost - Estimated API costs\n"
        "/health - System health check\n"
        "/exec <cmd> - Run a shell command directly\n"
        "/context - View/clear conversation memory\n"
        "/cancel - Cancel running task\n"
        "/retry - Re-run a failed task\n"
        "/projects - List registered projects\n"
        "/schedule - Schedule a recurring task\n"
        "/deploy <task_id> - Deploy frontend artifacts to live URL\n"
        "/setup - Validate system configuration"
    )


@auth_required
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command. Shows active tasks, or details for a specific task ID."""
    text = update.message.text.replace("/status", "", 1).strip()

    # If a task ID prefix is provided, show detailed state
    if text:
        task_id_prefix = text.split()[0]
        if not re.match(r'^[a-f0-9-]+$', task_id_prefix):
            await update.message.reply_text("Invalid task ID format.")
            return
        task = await db.get_task_by_prefix(task_id_prefix)
        if not task:
            await update.message.reply_text(f"No task found matching '{task_id_prefix}'.")
            return
        lines = [
            f"Task {task['id'][:8]}",
            f"Status: {task['status']}",
            f"Type: {task.get('task_type', 'unknown')}",
            f"Created: {task['created_at'][:19]}",
        ]
        if task.get("last_completed_stage"):
            lines.append(f"Last stage: {task['last_completed_stage']}")
        if task.get("task_state") and task["task_state"] != "{}":
            try:
                state = json.loads(task["task_state"])
                if state.get("plan"):
                    plan_preview = state["plan"][:200]
                    lines.append(f"\nPlan: {plan_preview}")
                if state.get("audit_verdict"):
                    lines.append(f"Audit: {state['audit_verdict']}")
                if state.get("audit_feedback"):
                    lines.append(f"Feedback: {state['audit_feedback'][:200]}")
                if state.get("stage_timings"):
                    timing_parts = [f"{t['name']}={t['duration_ms']}ms" for t in state["stage_timings"]]
                    lines.append(f"Timings: {', '.join(timing_parts)}")
            except (json.JSONDecodeError, TypeError):
                pass
        if task.get("error"):
            lines.append(f"\nError: {task['error'][:300]}")
        await update.message.reply_text("\n".join(lines))
        return

    # Default: show active tasks
    running_tasks = context.user_data.get("running_tasks", {})
    if not running_tasks:
        await update.message.reply_text("No active tasks.")
        return

    lines = []
    for tid, task_future in list(running_tasks.items()):
        stage = get_stage(tid)
        stage_label = STAGE_LABELS.get(stage, stage or "starting")
        if task_future.done():
            lines.append(f"Task {tid[:8]}: finished")
        else:
            lines.append(f"Task {tid[:8]}: {stage_label}")

    await update.message.reply_text("Active tasks:\n" + "\n".join(lines))


@auth_required
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /history command."""
    tasks = await db.list_tasks(update.effective_user.id, limit=5)
    if not tasks:
        await update.message.reply_text("No task history.")
        return

    lines = []
    for t in tasks:
        icon = {"completed": "done", "failed": "err", "cancelled": "stop", "pending": "..."}.get(
            t["status"], t["status"]
        )
        lines.append(f"[{icon}] {t['message'][:60]}")

    await update.message.reply_text("Recent tasks:\n" + "\n".join(lines))


@auth_required
async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /usage command."""
    usage = get_usage_summary()
    lines = [
        "API Usage (lifetime):",
        f"Total calls: {usage['total_calls']}",
        f"Input tokens: {usage['total_input_tokens']:,}",
        f"Output tokens: {usage['total_output_tokens']:,}",
    ]
    if usage.get("total_thinking_tokens"):
        lines.append(f"Thinking tokens: {usage['total_thinking_tokens']:,}")
    await update.message.reply_text("\n".join(lines))


@auth_required
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel command - cancel running tasks."""
    running_tasks = context.user_data.get("running_tasks", {})
    cancelled = 0

    for tid, task_future in list(running_tasks.items()):
        if not task_future.done():
            task_future.cancel()
            await db.update_task(tid, status="cancelled")
            clear_stage(tid)
            cancelled += 1

    context.user_data["running_tasks"] = {}

    if cancelled:
        await update.message.reply_text(
            f"Cancelled {cancelled} task(s).\n"
            "Note: background execution may take a moment to fully stop."
        )
    else:
        await update.message.reply_text("No running tasks to cancel.")


@auth_required
async def cmd_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /retry command - re-run a failed task with the same input.

    Usage: /retry [task_id_prefix]
    If no task ID given, retries the most recent failed task.
    """
    text = update.message.text.replace("/retry", "", 1).strip()
    user_id = update.effective_user.id

    if text:
        task_id_prefix = text.split()[0]
        if not re.match(r'^[a-f0-9-]+$', task_id_prefix):
            await update.message.reply_text("Invalid task ID format.")
            return
        task = await db.get_task_by_prefix(task_id_prefix)
    else:
        # Find most recent failed/crashed task for this user
        recent = await db.list_tasks(user_id, limit=10)
        task = None
        for t in recent:
            if t["status"] in ("failed", "crashed"):
                task = t
                break

    if not task:
        await update.message.reply_text("No failed task found to retry.")
        return

    if task["status"] not in ("failed", "crashed"):
        await update.message.reply_text(
            f"Task {task['id'][:8]} has status '{task['status']}'. Only failed/crashed tasks can be retried."
        )
        return

    # Resource guard
    running_tasks = context.user_data.get("running_tasks", {})
    rejection = _check_resources(running_tasks)
    if rejection:
        await update.message.reply_text(rejection)
        return

    # Re-submit as a new task with the original message
    new_task_id = str(uuid.uuid4())
    original_message = task["message"]
    await db.create_task(new_task_id, user_id, original_message)
    conversation_ctx = await db.build_conversation_context(user_id, limit=6)

    status_msg = await update.message.reply_text(
        f"Retrying task {task['id'][:8]} as {new_task_id[:8]}..."
    )

    try:
        await db.update_task(new_task_id, status="running")

        task_future = asyncio.ensure_future(
            asyncio.wait_for(
                asyncio.to_thread(
                    run_task,
                    task_id=new_task_id,
                    user_id=user_id,
                    message=original_message,
                    files=[],
                    conversation_context=conversation_ctx,
                ),
                timeout=config.LONG_TIMEOUT,
            )
        )

        running_tasks = context.user_data.setdefault("running_tasks", {})
        running_tasks[new_task_id] = task_future

        # Stream status updates
        last_edit_hash = 0
        while not task_future.done():
            await asyncio.sleep(3)
            stage = get_stage(new_task_id)
            if not stage:
                continue
            label = STAGE_LABELS.get(stage, stage)
            if stage == "executing":
                from tools.sandbox import get_live_output
                tail = get_live_output(new_task_id, tail=3)
                if tail:
                    label += f"\n\nLatest output:\n{tail[-200:]}"
            label += f" (retry of {task['id'][:8]})"
            content_hash = hash(label)
            if content_hash != last_edit_hash:
                try:
                    await status_msg.edit_text(label)
                    last_edit_hash = content_hash
                except Exception:
                    pass

        result = task_future.result()
        running_tasks.pop(new_task_id, None)

        try:
            await status_msg.edit_text(f"Completed. (retry {new_task_id[:8]})")
        except Exception:
            pass

        response_text = result.get("final_response", "Task completed but no output was generated.")
        await _send_long_message(update, response_text)
        await db.add_history(user_id, "assistant", response_text, new_task_id)

        # Send artifacts
        for fpath in result.get("artifacts", []):
            p = Path(fpath)
            if p.is_file() and p.stat().st_size < config.MAX_FILE_SIZE_BYTES:
                try:
                    with open(p, "rb") as f:
                        await update.message.reply_document(document=f, filename=p.name)
                except Exception as send_err:
                    logger.warning("Failed to send retry artifact %s: %s", p.name, send_err)

        await db.update_task(new_task_id, status="completed", result=response_text,
                            completed_at=datetime.now(timezone.utc).isoformat())

    except asyncio.CancelledError:
        await db.update_task(new_task_id, status="cancelled")
        await status_msg.edit_text(f"Retry {new_task_id[:8]} cancelled.")
    except asyncio.TimeoutError:
        await db.update_task(new_task_id, status="failed", error="Pipeline timeout")
        await status_msg.edit_text(f"Retry {new_task_id[:8]} timed out.")
    except Exception as e:
        logger.exception("Retry pipeline error for task %s", new_task_id)
        await db.update_task(new_task_id, status="failed", error=str(e)[:500])
        await status_msg.edit_text(f"Retry {new_task_id[:8]} failed: {str(e)[:200]}")
    finally:
        running_tasks.pop(new_task_id, None)


@auth_required
async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /projects command - list registered projects."""
    from tools.projects import get_projects

    projects = get_projects()
    if not projects:
        await update.message.reply_text("No projects registered. Edit projects.yaml to add them.")
        return

    lines = ["Registered projects:"]
    for p in projects:
        commands = list(p.get("commands", {}).keys())
        cmd_str = f" ({', '.join(commands)})" if commands else ""
        lines.append(f"\n{p['name']}{cmd_str}")
        triggers = p.get("triggers", [])[:3]
        lines.append(f"  Triggers: {', '.join(triggers)}")

    await update.message.reply_text("\n".join(lines))


@auth_required
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /health command - system health check."""
    import sys
    import shutil
    import psutil

    lines = ["System Health:"]

    # Python
    lines.append(f"Python: {sys.version.split()[0]}")

    # RAM
    mem = psutil.virtual_memory()
    used_gb = mem.used / (1024 ** 3)
    total_gb = mem.total / (1024 ** 3)
    lines.append(f"RAM: {used_gb:.1f} / {total_gb:.1f} GB ({mem.percent:.0f}%)")

    # Active tasks
    running_tasks = context.user_data.get("running_tasks", {})
    active = sum(1 for f in running_tasks.values() if not f.done())
    lines.append(f"Active tasks: {active} / {config.MAX_CONCURRENT_TASKS}")

    # Ollama
    try:
        import requests
        resp = await asyncio.to_thread(requests.get, f"{config.OLLAMA_BASE_URL}/api/tags", timeout=3)
        if resp.ok:
            models = [m["name"] for m in resp.json().get("models", [])]
            lines.append(f"Ollama: online ({len(models)} models)")
            if models:
                lines.append(f"  Models: {', '.join(models[:5])}")
        else:
            lines.append("Ollama: error (bad response)")
    except Exception:
        lines.append("Ollama: offline")

    # Disk
    disk = shutil.disk_usage(config.BASE_DIR)
    free_gb = disk.free / (1024 ** 3)
    lines.append(f"Disk free: {free_gb:.1f} GB")

    # API usage
    usage_info = get_usage_summary()
    lines.append(f"API calls (total): {usage_info['total_calls']}")
    lines.append(f"Tokens: {usage_info['total_input_tokens']:,} in / {usage_info['total_output_tokens']:,} out")

    # Cost
    cost_info = get_cost_summary()
    lines.append(f"Est. cost: ${cost_info['total_cost_usd']:.2f}")

    # Project venv health
    from tools.projects import get_projects
    venv_issues = []
    for proj in get_projects():
        venv = proj.get("venv")
        if venv:
            python_bin = Path(venv) / "bin" / "python3"
            if not python_bin.exists():
                venv_issues.append(f"  '{proj['name']}': venv python not found at {python_bin}")
    if venv_issues:
        lines.append("Project venv issues:")
        lines.extend(venv_issues)

    # Pipeline performance (last 24h)
    try:
        recent = await db.list_tasks(update.effective_user.id, limit=50)
        completed_with_timings = []
        for t in recent:
            if t.get("task_state") and t["task_state"] != "{}":
                try:
                    state = json.loads(t["task_state"])
                    if state.get("stage_timings"):
                        completed_with_timings.append(state["stage_timings"])
                except (json.JSONDecodeError, TypeError):
                    pass
        if completed_with_timings:
            stage_totals: dict[str, list[int]] = {}
            for timings in completed_with_timings:
                for entry in timings:
                    stage_totals.setdefault(entry["name"], []).append(entry["duration_ms"])
            lines.append(f"\nPipeline ({len(completed_with_timings)} recent tasks):")
            for stage_name in ["classifying", "planning", "executing", "auditing", "delivering"]:
                if stage_name in stage_totals:
                    vals = stage_totals[stage_name]
                    avg_ms = sum(vals) // len(vals)
                    lines.append(f"  {stage_name}: {avg_ms}ms avg")
    except Exception:
        pass  # Pipeline stats are informational, never crash /health

    await update.message.reply_text("\n".join(lines))


@auth_required
async def cmd_exec(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /exec command - run a shell command directly via sandbox safety.

    Usage: /exec <command>
    Example: /exec ls -la ~/Desktop
    """
    from tools.sandbox import run_shell

    command = update.message.text.replace("/exec", "", 1).strip()
    if not command:
        await update.message.reply_text("Usage: /exec <command>\nExample: /exec ls -la ~/Desktop")
        return

    await update.message.reply_text(f"Running: {command[:100]}...")

    try:
        result = await asyncio.to_thread(
            run_shell,
            command,
            working_dir=str(config.HOST_HOME),
            timeout=60,
        )
        output = ""
        if result.stdout:
            output += result.stdout[:3000]
        if result.stderr:
            output += f"\n[stderr]\n{_sanitize_error_for_user(result.stderr[:1000])}"
        if not output.strip():
            output = "(no output)"

        status = "OK" if result.success else f"EXIT {result.return_code}"
        await _send_long_message(update, f"[{status}]\n{output}")
    except Exception as e:
        await update.message.reply_text(f"Error: {_sanitize_error_for_user(str(e))}")


@auth_required
async def cmd_context(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /context command - view or clear conversation memory.

    Usage: /context - view recent history
           /context clear - clear all memory
    """
    user_id = update.effective_user.id
    text = update.message.text.replace("/context", "", 1).strip()

    if text == "clear":
        await db.clear_context(user_id)
        await db.clear_history(user_id)
        await update.message.reply_text("Conversation memory cleared (context + history).")
        return

    history = await db.get_recent_history(user_id, limit=8)
    if not history:
        await update.message.reply_text("No conversation history yet.")
        return

    lines = ["Recent conversation memory:"]
    for msg in history:
        role = "You" if msg["role"] == "user" else "Agent"
        content = msg["content"][:120]
        lines.append(f"\n[{role}] {content}")

    ctx = await db.get_all_context(user_id)
    if ctx:
        lines.append("\n\nStored context:")
        for k, v in ctx.items():
            lines.append(f"  {k}: {v[:80]}")

    await _send_long_message(update, "\n".join(lines))


@auth_required
async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cost command - show estimated API costs with daily breakdown."""
    cost = get_cost_summary()
    daily = get_daily_cost_breakdown(days=7)
    budget = get_budget_remaining()

    lines = ["Cost Summary (Last 7 Days)"]
    lines.append("-" * 28)

    # Daily breakdown
    if daily:
        for day in daily[:7]:
            lines.append(f"{day['date']}:  ${day['cost_usd']:.2f}  ({day['calls']} calls)")
    else:
        lines.append("No API usage recorded.")

    # Today's model breakdown
    if daily:
        today = daily[0] if daily else None
        if today and today.get("by_model"):
            lines.append("\nBy Model (Today)")
            total_today = today["cost_usd"] or 1
            for model, model_cost in sorted(today["by_model"].items(), key=lambda x: x[1], reverse=True):
                pct = (model_cost / total_today * 100) if total_today > 0 else 0
                lines.append(f"  {model}: ${model_cost:.2f} ({pct:.0f}%)")

    # Lifetime totals
    lines.append(f"\nLifetime: ${cost['total_cost_usd']:.2f} ({cost['total_calls']} calls)")

    # Budget remaining
    budget_parts = []
    if budget.get("daily_limit"):
        remaining = budget.get("daily_remaining")
        if remaining is not None:
            budget_parts.append(f"${remaining:.2f} daily")
    if budget.get("monthly_limit"):
        remaining = budget.get("monthly_remaining")
        if remaining is not None:
            budget_parts.append(f"${remaining:.2f} monthly")
    if budget_parts:
        lines.append(f"Budget remaining: {' / '.join(budget_parts)}")

    await update.message.reply_text("\n".join(lines))


@auth_required
async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /schedule command - schedule recurring tasks.

    Usage: /schedule <interval_minutes> <task description>
    Example: /schedule 360 Run the job scraper and send results
    """
    text = update.message.text.replace("/schedule", "", 1).strip()

    if not text:
        await update.message.reply_text(
            "Usage: /schedule <minutes> <task>\n"
            "Example: /schedule 360 Run the job scraper\n\n"
            "/schedule list - Show scheduled tasks\n"
            "/schedule remove <id> - Remove a scheduled task"
        )
        return

    if text == "list":
        from scheduler.cron import list_jobs
        jobs = list_jobs()
        if not jobs:
            await update.message.reply_text("No scheduled tasks.")
        else:
            lines = [f"- {j['id'][:8]}: {j['name']} (next: {j['next_run']})" for j in jobs]
            await update.message.reply_text("Scheduled tasks:\n" + "\n".join(lines))
        return

    if text.startswith("remove "):
        job_id = text.replace("remove ", "").strip()
        from scheduler.cron import remove_job
        try:
            remove_job(job_id)
            await update.message.reply_text(f"Removed scheduled task {job_id[:8]}.")
        except Exception as e:
            await update.message.reply_text(f"Could not remove: {e}")
        return

    # Parse: first token is interval in minutes, rest is the task
    parts = text.split(" ", 1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /schedule <minutes> <task description>")
        return

    try:
        interval_minutes = int(parts[0])
    except ValueError:
        await update.message.reply_text(f"Invalid interval: {parts[0]}. Must be a number of minutes.")
        return

    if interval_minutes < 1:
        await update.message.reply_text("Interval must be at least 1 minute.")
        return
    if interval_minutes > 43200:
        await update.message.reply_text("Interval must be at most 43200 minutes (30 days).")
        return

    task_message = parts[1]
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    job_id = str(uuid.uuid4())

    from scheduler.cron import add_interval_job

    # Use module-level function with serializable kwargs (not a closure)
    # so SQLAlchemyJobStore can pickle the job for persistence
    add_interval_job(
        _scheduled_task_run,
        minutes=interval_minutes,
        job_id=job_id,
        chat_id=chat_id,
        user_id=user_id,
        task_message=task_message,
    )

    hours = interval_minutes // 60
    mins = interval_minutes % 60
    interval_str = f"{hours}h {mins}m" if hours else f"{mins}m"

    await update.message.reply_text(
        f"Scheduled: \"{task_message}\"\n"
        f"Interval: every {interval_str}\n"
        f"Job ID: {job_id[:8]}\n"
        f"Use /schedule list to view, /schedule remove {job_id[:8]} to cancel."
    )


async def _scheduled_task_run(chat_id: int, user_id: int, task_message: str):
    """Execute a scheduled task and send results. Module-level for pickle serialization."""
    # Resource guard for scheduled tasks
    try:
        import psutil
        mem = psutil.virtual_memory()
        if mem.percent >= config.RAM_THRESHOLD_PERCENT:
            logger.warning(
                "Skipping scheduled task '%s': RAM at %.0f%% (threshold: %d%%)",
                task_message[:60], mem.percent, config.RAM_THRESHOLD_PERCENT,
            )
            return
    except Exception:
        pass  # If psutil fails, proceed anyway

    tid = str(uuid.uuid4())
    await db.create_task(tid, user_id, task_message)
    await db.update_task(tid, status="running")

    # Build conversation context for continuity
    conversation_ctx = await db.build_conversation_context(user_id, limit=4)

    # Use async context manager to properly close the httpx.AsyncClient session
    async with Bot(token=config.TELEGRAM_BOT_TOKEN) as bot:
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    run_task, tid, user_id, task_message, [],
                    conversation_context=conversation_ctx,
                ),
                timeout=config.LONG_TIMEOUT,
            )
            response = result.get("final_response", "Scheduled task completed.")
            await bot.send_message(chat_id=chat_id, text=f"[Scheduled] {response[:4000]}")

            artifacts = result.get("artifacts", [])
            seen_paths = set()
            for fpath in artifacts:
                if fpath in seen_paths:
                    continue
                seen_paths.add(fpath)
                p = Path(fpath)
                if not p.is_file():
                    logger.warning("Scheduled artifact not found, skipping: %s", fpath)
                    continue
                file_size = p.stat().st_size
                if file_size == 0:
                    logger.warning("Scheduled artifact is empty, skipping: %s", fpath)
                    continue
                if file_size >= config.MAX_FILE_SIZE_BYTES:
                    logger.warning("Scheduled artifact too large, skipping: %s", fpath)
                    continue
                try:
                    with open(p, "rb") as f:
                        await bot.send_document(chat_id=chat_id, document=f, filename=p.name)
                except Exception as send_err:
                    logger.warning("Failed to send scheduled artifact %s: %s", p.name, send_err)

            await db.update_task(tid, status="completed", result=response,
                                completed_at=datetime.now(timezone.utc).isoformat())
        except asyncio.TimeoutError:
            logger.error("Scheduled task %s timed out after %ds", tid, config.LONG_TIMEOUT)
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"[Scheduled] Task timed out after {config.LONG_TIMEOUT}s: {task_message[:100]}",
                )
            except Exception:
                pass
            await db.update_task(tid, status="failed", error=f"Timed out after {config.LONG_TIMEOUT}s")
        except Exception as e:
            logger.error("Scheduled task %s failed: %s", tid, e, exc_info=True)
            try:
                safe_msg = _sanitize_error_for_user(str(e))
                await bot.send_message(chat_id=chat_id, text=f"[Scheduled] Task failed: {safe_msg}")
            except Exception:
                pass
            await db.update_task(tid, status="failed", error=str(e))


@auth_required
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages - run the agent pipeline."""
    message = update.message.text
    user_id = update.effective_user.id
    task_id = str(uuid.uuid4())

    # Rate limiter: prevent accidental double-sends (5s cooldown)
    last_submit = context.user_data.get("last_task_submit", 0)
    # A-32: Use time.monotonic() instead of deprecated asyncio.get_event_loop().time()
    now_ts = _time.monotonic()
    if now_ts - last_submit < 5:
        await update.message.reply_text("Please wait a few seconds between tasks.")
        return
    context.user_data["last_task_submit"] = now_ts

    # Resource guard: check RAM and concurrent task limits before accepting
    running_tasks = context.user_data.get("running_tasks", {})
    rejection = _check_resources(running_tasks)
    if rejection:
        await update.message.reply_text(rejection)
        return

    await db.create_task(task_id, user_id, message)

    # Save user message to conversation history
    await db.add_history(user_id, "user", message, task_id)

    # Build conversation context from recent history
    conversation_ctx = await db.build_conversation_context(user_id, limit=6)

    # Budget warning: notify user if >80% daily budget consumed
    budget_warning = ""
    try:
        budget = get_budget_remaining()
        if budget.get("daily_limit") and budget.get("daily_spent"):
            utilization = budget["daily_spent"] / budget["daily_limit"]
            if utilization > 0.8:
                budget_warning = " (budget >80% — routing classify/plan to local model)"
    except Exception:
        pass

    # Send initial status message that we'll update with streaming status
    status_msg = await update.message.reply_text(f"Starting... (task {task_id[:8]}){budget_warning}")

    try:
        await db.update_task(task_id, status="running")

        # Snapshot which files this task will consume (Fix 4: preserve uploads during concurrent tasks)
        consumed = context.user_data.get("pending_files", [])
        context.user_data["_consumed_files_" + task_id] = list(consumed)

        # Launch the pipeline in a thread, with outer timeout matching scheduled tasks
        task_future = asyncio.ensure_future(
            asyncio.wait_for(
                asyncio.to_thread(
                    run_task,
                    task_id=task_id,
                    user_id=user_id,
                    message=message,
                    files=list(consumed),
                    conversation_context=conversation_ctx,
                ),
                timeout=config.LONG_TIMEOUT,
            )
        )

        # Track this task (concurrency-safe: dict keyed by task_id)
        running_tasks = context.user_data.setdefault("running_tasks", {})
        running_tasks[task_id] = task_future

        # Stream status updates while pipeline runs (hash-gated to avoid redundant edits)
        last_edit_hash = 0
        while not task_future.done():
            await asyncio.sleep(3)
            stage = get_stage(task_id)
            if not stage:
                continue

            label = STAGE_LABELS.get(stage, stage)

            # Enrich with live stdout during execution
            if stage == "executing":
                from tools.sandbox import get_live_output
                tail = get_live_output(task_id, tail=3)
                if tail:
                    label += f"\n\nLatest output:\n{tail[-200:]}"

            label += f" (task {task_id[:8]})"

            # Hash-gate: skip edit if content hasn't changed
            content_hash = hash(label)
            if content_hash != last_edit_hash:
                try:
                    await status_msg.edit_text(label)
                    last_edit_hash = content_hash
                except Exception:
                    pass  # "Message is not modified" or rate limit

        result = task_future.result()

        # Clean up tracking
        running_tasks.pop(task_id, None)

        # Update status message to completion
        try:
            await status_msg.edit_text(f"Completed. (task {task_id[:8]})")
        except Exception:
            pass

        # Send text response
        response_text = result.get("final_response", "Task completed but no output was generated.")
        await _send_long_message(update, response_text)

        # Save agent response to conversation history
        await db.add_history(user_id, "assistant", response_text, task_id)

        # Persist structured context for follow-up tasks
        try:
            await db.set_context(user_id, "last_task_type", result.get("task_type", ""))
            await db.set_context(user_id, "last_task_message", message[:500])
            if result.get("artifacts"):
                import json as _json
                file_names = [Path(f).name for f in result.get("artifacts", []) if Path(f).exists()]
                await db.set_context(user_id, "last_files_created", _json.dumps(file_names))
            if result.get("working_dir"):
                await db.set_context(user_id, "last_working_dir", result["working_dir"])
            if result.get("project_name"):
                await db.set_context(user_id, "last_project_name", result["project_name"])
        except Exception as ctx_err:
            logger.warning("Failed to save conversation context: %s", ctx_err)

        # Send artifact files (deduplicated, validated, error-resilient)
        seen_paths = set()
        sent_count = 0
        for fpath in result.get("artifacts", []):
            if fpath in seen_paths:
                continue
            seen_paths.add(fpath)
            p = Path(fpath)
            if not p.is_file():
                logger.warning("Artifact not found, skipping: %s", fpath)
                continue
            file_size = p.stat().st_size
            if file_size == 0:
                logger.warning("Artifact is empty (0 bytes), skipping: %s", fpath)
                continue
            if file_size >= config.MAX_FILE_SIZE_BYTES:
                logger.warning("Artifact too large (%d bytes), skipping: %s", file_size, fpath)
                continue
            try:
                with open(p, "rb") as f:
                    await update.message.reply_document(document=f, filename=p.name)
                sent_count += 1
            except Exception as send_err:
                logger.warning("Failed to send artifact %s: %s", p.name, send_err)

        if sent_count == 0 and result.get("artifacts"):
            logger.error(
                "No artifacts were successfully sent out of %d detected",
                len(result.get("artifacts", [])),
            )

        await db.update_task(
            task_id,
            status="completed",
            task_type=result.get("task_type", ""),
            plan=result.get("plan", ""),
            result=result.get("final_response", ""),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )

    except asyncio.CancelledError:
        logger.info("Task %s was cancelled", task_id)
        await update.message.reply_text("Task was cancelled.")
        await db.update_task(task_id, status="cancelled")
    except asyncio.TimeoutError:
        logger.error("Interactive task %s timed out after %ds", task_id, config.LONG_TIMEOUT)
        await update.message.reply_text(
            f"Task timed out after {config.LONG_TIMEOUT // 60} minutes. "
            "The task was too complex or an external service was unresponsive."
        )
        await db.update_task(task_id, status="failed", error=f"Pipeline timed out after {config.LONG_TIMEOUT}s")
    except Exception as e:
        logger.error("Task %s failed: %s", task_id, e, exc_info=True)
        safe_msg = _sanitize_error_for_user(str(e))
        await update.message.reply_text(f"Task failed: {safe_msg}")
        await db.update_task(task_id, status="failed", error=str(e))
    finally:
        running_tasks = context.user_data.get("running_tasks", {})
        running_tasks.pop(task_id, None)
        # Only clear pending_files that were consumed by THIS task.
        # If user uploaded new files while this task was running, preserve them.
        consumed_files = set(context.user_data.pop("_consumed_files_" + task_id, []))
        current_pending = context.user_data.get("pending_files", [])
        remaining = [f for f in current_pending if f not in consumed_files]
        if remaining:
            context.user_data["pending_files"] = remaining
        else:
            context.user_data.pop("pending_files", None)
        clear_stage(task_id)


@auth_required
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle file uploads. Saves file and waits for a text instruction."""
    doc = update.message.document
    if doc.file_size > config.MAX_FILE_SIZE_BYTES:
        await update.message.reply_text(f"File too large (max {config.MAX_FILE_SIZE_MB}MB).")
        return

    file = await context.bot.get_file(doc.file_id)
    data = await file.download_as_bytearray()
    filename = doc.file_name or f"upload_{uuid.uuid4().hex[:8]}"
    saved_path = save_upload(bytes(data), filename)

    pending = context.user_data.setdefault("pending_files", [])
    # A-17: Cap pending files to prevent unbounded memory growth
    if len(pending) >= 10:
        await update.message.reply_text("Too many pending files (max 10). Send a task first.")
        return
    pending.append(str(saved_path))

    await update.message.reply_text(
        f"File received: {filename}\n"
        f"Now send a text message describing what to do with it."
    )


@auth_required
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo uploads."""
    photo = update.message.photo[-1]  # Highest resolution
    file = await context.bot.get_file(photo.file_id)
    data = await file.download_as_bytearray()
    saved_path = save_upload(bytes(data), f"photo_{uuid.uuid4().hex[:8]}.jpg")

    pending = context.user_data.setdefault("pending_files", [])
    pending.append(str(saved_path))

    await update.message.reply_text(
        "Photo received. Send a text message describing what to do with it."
    )


@auth_required
async def cmd_chain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute a strict-AND chain of tasks: /chain step 1 -> step 2 -> step 3."""
    user_id = update.effective_user.id
    raw = update.message.text.replace("/chain", "", 1).strip()

    if not raw:
        await update.message.reply_text(
            "Usage: /chain step 1 -> step 2 -> step 3\n"
            "Use {output} to pass artifacts between steps."
        )
        return

    steps = [s.strip() for s in raw.split("->") if s.strip()]
    if len(steps) < 2:
        await update.message.reply_text("A chain needs at least 2 steps separated by ->")
        return

    # A-16: Concurrency guard — chains consume resources like regular tasks
    running_tasks = context.user_data.get("running_tasks", {})
    rejection = _check_resources(running_tasks)
    if rejection:
        await update.message.reply_text(rejection)
        return

    base_id = str(uuid.uuid4())[:8]
    previous_artifacts: list[str] = []

    await update.message.reply_text(
        f"Starting chain: {len(steps)} steps\n"
        + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(steps))
    )

    for i, step in enumerate(steps):
        # D-3: Safe — base_id is UUID hex prefix (no hyphens in hex), so -step{i} suffix never collides
        step_id = f"{base_id}-step{i}"

        # Replace {output} with actual artifact paths from previous step
        if previous_artifacts:
            artifact_paths = ", ".join(previous_artifacts)
            step_msg = step.replace("{output}", artifact_paths)
        else:
            step_msg = step.replace("{output}", "").strip()

        files = list(previous_artifacts)

        # Force literal execution: chain steps must NOT rewrite failing assertions
        # into graceful reports. This prefix instructs the planner to execute exactly
        # as written so that real pass/fail results flow back to the strict-AND gate.
        chain_prefix = (
            f"CHAIN STEP {i+1}/{len(steps)}: Execute this task EXACTLY as written. "
            "Do NOT catch exceptions, do NOT handle errors gracefully, do NOT rewrite "
            "failing assertions into passing ones. If the task says to assert something "
            "that will fail, let the assertion crash the program. The chain depends on "
            "real pass/fail results.\n\n"
        )
        pipeline_msg = chain_prefix + step_msg

        # DB lifecycle: track each step as its own task
        await db.create_task(step_id, user_id, step_msg)
        await update.message.reply_text(f"Step {i+1}/{len(steps)}: {step_msg[:100]}")

        try:
            await db.update_task(step_id, status="running")
            conversation_ctx = await db.build_conversation_context(user_id, limit=6)
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    run_task,
                    task_id=step_id,
                    user_id=user_id,
                    message=pipeline_msg,
                    files=files,
                    conversation_context=conversation_ctx,
                ),
                timeout=config.LONG_TIMEOUT,
            )

            now = datetime.now(timezone.utc).isoformat()
            await db.update_task(
                step_id,
                status="completed" if result.get("audit_verdict") == "pass" else "failed",
                result=result.get("final_response", "")[:5000],
                completed_at=now,
            )
        except asyncio.TimeoutError:
            await db.update_task(step_id, status="failed", error="Pipeline timeout")
            await update.message.reply_text(
                f"Chain halted at step {i+1}/{len(steps)}: Pipeline timeout.\n"
                f"Steps {i+2}-{len(steps)} were NOT executed."
            )
            return
        except Exception as e:
            await db.update_task(step_id, status="failed", error=str(e)[:500])
            await update.message.reply_text(
                f"Chain halted at step {i+1}/{len(steps)}: {str(e)[:200]}\n"
                f"Steps {i+2}-{len(steps)} were NOT executed."
            )
            return

        # STRICT-AND GATE: exit-code based + audit verdict
        # Check execution_result for non-zero exit code first — Claude can't fake this.
        # Then check audit_verdict as secondary gate.
        exec_result = result.get("execution_result", "")
        exec_failed = exec_result.startswith("Execution: FAILED")

        if exec_failed or result.get("audit_verdict") != "pass":
            if exec_failed:
                reason = "Execution returned non-zero exit code"
            else:
                reason = result.get("audit_feedback", "Unknown")[:300]
            await update.message.reply_text(
                f"Chain halted at step {i+1}/{len(steps)}.\n\n"
                f"Step failed: {step_msg[:100]}\n"
                f"Reason: {reason}\n\n"
                f"Steps {i+2}-{len(steps)} were NOT executed.\n"
                f"No artifacts from this step were forwarded."
            )
            return

        previous_artifacts = result.get("artifacts", [])

        # Send step result
        response_text = result.get("final_response", "Step completed.")
        await _send_long_message(update, f"Step {i+1}: {response_text}")

        # Send step artifacts
        for fpath in previous_artifacts:
            p = Path(fpath)
            if p.exists() and p.stat().st_size > 0:
                try:
                    with open(p, "rb") as f:
                        await update.message.reply_document(document=f)
                except Exception:
                    pass
    else:
        # All steps completed
        await update.message.reply_text(
            f"Chain complete - all {len(steps)} steps passed."
        )


@auth_required
async def cmd_deploy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deploy artifacts from a completed task: /deploy <task_id>."""
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /deploy <task_id>")
        return

    task_id_prefix = args[0]

    # A-14: Validate task_id_prefix — only hex chars and hyphens (no glob metacharacters)
    if not re.match(r'^[a-f0-9-]+$', task_id_prefix):
        await update.message.reply_text("Invalid task ID format.")
        return

    # Find HTML artifacts matching the task ID prefix
    html_files = list(config.OUTPUTS_DIR.glob(f"*{task_id_prefix}*.html"))
    if not html_files:
        await update.message.reply_text(
            f"No HTML artifacts found for '{task_id_prefix}'. "
            "/deploy works with frontend/ui_design outputs."
        )
        return

    artifact_dir = html_files[0].parent
    project_name = task_id_prefix

    try:
        from tools.deployer import deploy
        url = deploy(artifact_dir, project_name, "frontend")
        if url:
            await update.message.reply_text(f"Deployed: {url}")
        else:
            await update.message.reply_text(
                "Deployment failed. Check DEPLOY_ENABLED and credentials in .env."
            )
    except Exception as e:
        logger.warning("Deploy command failed: %s", e)
        await update.message.reply_text(f"Deployment error: {str(e)[:200]}")


@auth_required
async def cmd_stopserver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop a running local server: /stopserver <task_id> or /stopserver all."""
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /stopserver <task_id> or /stopserver all")
        return

    from tools.sandbox import stop_server, stop_all_servers

    if args[0] == "all":
        count = stop_all_servers()
        await update.message.reply_text(f"Stopped {count} server(s).")
    else:
        stopped = stop_server(args[0])
        msg = "Server stopped." if stopped else "No running server found for that task."
        await update.message.reply_text(msg)


@auth_required
async def cmd_servers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List running local servers: /servers."""
    from tools.sandbox import list_servers

    servers = list_servers()
    if not servers:
        await update.message.reply_text("No servers running.")
        return

    lines = ["Running servers:"]
    for s in servers:
        lines.append(
            f"• {s['task_id'][:8]} — port {s['port']} (pid {s['pid']}, {s['uptime']}s)"
        )
    await update.message.reply_text("\n".join(lines))


@auth_required
async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setup command - validate system configuration and report health."""
    from tools.projects import get_projects

    checks: list[tuple[str, bool | str]] = []

    # 1. Required environment variables
    for key in ["ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "ALLOWED_USER_IDS"]:
        checks.append((f"env:{key}", bool(getattr(config, key, None))))

    # 2. Ollama connectivity
    try:
        import requests
        resp = requests.get(f"{config.OLLAMA_BASE_URL}/api/tags", timeout=5)
        if resp.ok:
            models = [m.get("name", "") for m in resp.json().get("models", [])]
            checks.append(("ollama:connected", True))
            has_model = any(config.OLLAMA_DEFAULT_MODEL in m for m in models)
            checks.append((f"ollama:{config.OLLAMA_DEFAULT_MODEL}", has_model))
        else:
            checks.append(("ollama:connected", False))
    except Exception:
        checks.append(("ollama:connected", False))

    # 3. Project registry
    projects = get_projects()
    for p in projects:
        path_ok = Path(p.get("path", "")).is_dir() if p.get("path") else False
        checks.append((f"project:{p['name']}", path_ok))

    # 4. Database writable
    try:
        conn = __import__("sqlite3").connect(str(config.DB_PATH), timeout=5.0)
        conn.execute("SELECT 1")
        conn.close()
        checks.append(("db:writable", True))
    except Exception:
        checks.append(("db:writable", False))

    # 5. Budget configuration
    if config.DAILY_BUDGET_USD:
        checks.append(("budget:daily", f"${config.DAILY_BUDGET_USD}"))
    else:
        checks.append(("budget:daily", "unlimited"))
    if config.MONTHLY_BUDGET_USD:
        checks.append(("budget:monthly", f"${config.MONTHLY_BUDGET_USD}"))
    else:
        checks.append(("budget:monthly", "unlimited"))

    # 6. Workspace writable
    try:
        test_file = config.WORKSPACE_DIR / ".setup_test"
        test_file.write_text("ok")
        test_file.unlink()
        checks.append(("workspace:writable", True))
    except Exception:
        checks.append(("workspace:writable", False))

    # Format output
    lines = [f"AgentSutra v{config.VERSION} Setup Check", "-" * 30]
    pass_count = 0
    for name, status in checks:
        if isinstance(status, bool):
            icon = "OK" if status else "FAIL"
            if status:
                pass_count += 1
        else:
            icon = str(status)
            pass_count += 1
        lines.append(f"  [{icon}] {name}")

    total = len(checks)
    lines.append(f"\n{pass_count}/{total} checks passed")
    await update.message.reply_text("\n".join(lines))


@auth_required
async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show debug sidecar JSON for a task: /debug <task_id>."""
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /debug <task_id>")
        return
    task_id_prefix = args[0]

    # A-15: Validate task_id_prefix — only hex chars and hyphens (no glob metacharacters)
    if not re.match(r'^[a-f0-9-]+$', task_id_prefix):
        await update.message.reply_text("Invalid task ID format.")
        return

    matches = list(config.OUTPUTS_DIR.glob(f"{task_id_prefix}*.debug.json"))
    if not matches:
        await update.message.reply_text(f"No debug data found for '{task_id_prefix}'")
        return
    content = matches[0].read_text()
    # A-30: Send as plain text — JSON contains chars that break Markdown parse mode
    await update.message.reply_text(content[:3900])


async def _send_long_message(update: Update, text: str):
    """Split and send messages that exceed Telegram's character limit.

    Includes small delays between chunks to respect Telegram rate limits.
    """
    max_len = config.TELEGRAM_MAX_MESSAGE_LENGTH
    if len(text) <= max_len:
        await update.message.reply_text(text)
        return

    chunks = []
    current = ""
    for line in text.split("\n"):
        while len(line) > max_len:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:max_len])
            line = line[max_len:]
        if len(current) + len(line) + 1 > max_len:
            chunks.append(current)
            current = line
        else:
            current += ("\n" if current else "") + line
    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks):
        if chunk:
            try:
                await update.message.reply_text(chunk)
            except Exception as e:
                logger.warning("Failed to send message chunk %d/%d: %s", i + 1, len(chunks), e)
                # On RetryAfter, wait and retry once
                if "retry after" in str(e).lower():
                    wait_match = re.search(r'(\d+)', str(e))
                    wait_time = int(wait_match.group(1)) if wait_match else 5
                    await asyncio.sleep(wait_time)
                    try:
                        await update.message.reply_text(chunk)
                    except Exception:
                        pass  # Give up on this chunk
            # Small delay between chunks to avoid rate limits
            if i < len(chunks) - 1:
                await asyncio.sleep(0.3)
