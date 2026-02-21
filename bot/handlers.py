import uuid
import asyncio
import functools
import logging
from pathlib import Path
from datetime import datetime, timezone

from telegram import Update, Bot
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

import config
from brain.graph import run_task, get_stage, clear_stage
from storage import db
from tools.file_manager import save_upload
from tools.claude_client import get_usage_summary, get_cost_summary

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
        "AgentCore v6 is online.\n\n"
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
        "/projects - List registered projects\n"
        "/schedule - Schedule a recurring task"
    )


@auth_required
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command."""
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
        await update.message.reply_text(f"Cancelled {cancelled} task(s).")
    else:
        await update.message.reply_text("No running tasks to cancel.")


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
        resp = requests.get(f"{config.OLLAMA_BASE_URL}/api/tags", timeout=3)
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
            output += f"\n[stderr]\n{result.stderr[:1000]}"
        if not output.strip():
            output = "(no output)"

        status = "OK" if result.success else f"EXIT {result.return_code}"
        await _send_long_message(update, f"[{status}]\n{output}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


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
    """Handle /cost command - show estimated API costs."""
    cost = get_cost_summary()
    lines = [
        "API Cost Estimate:",
        f"Total calls: {cost['total_calls']}",
        f"Input tokens: {cost['total_input_tokens']:,}",
        f"Output tokens: {cost['total_output_tokens']:,}",
    ]
    if cost.get("total_thinking_tokens"):
        lines.append(f"Thinking tokens: {cost['total_thinking_tokens']:,}")
    lines.append(f"Estimated cost: ${cost['total_cost_usd']:.4f}")
    if cost.get("by_model"):
        lines.append("\nBy model:")
        for model, info in cost["by_model"].items():
            short_name = model.split("-")[-1] if "-" in model else model
            lines.append(f"  {short_name}: {info['calls']} calls, ${info['cost_usd']:.4f}")

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
            for fpath in artifacts:
                p = Path(fpath)
                if p.is_file() and p.stat().st_size < config.MAX_FILE_SIZE_BYTES:
                    with open(p, "rb") as f:
                        await bot.send_document(chat_id=chat_id, document=f, filename=p.name)

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
                await bot.send_message(chat_id=chat_id, text=f"[Scheduled] Task failed: {e}")
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
    now_ts = asyncio.get_event_loop().time()
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

    # Send initial status message that we'll update with streaming status
    status_msg = await update.message.reply_text(f"Starting... (task {task_id[:8]})")

    try:
        await db.update_task(task_id, status="running")

        # Launch the pipeline in a thread
        task_future = asyncio.ensure_future(asyncio.to_thread(
            run_task,
            task_id=task_id,
            user_id=user_id,
            message=message,
            files=context.user_data.get("pending_files", []),
            conversation_context=conversation_ctx,
        ))

        # Track this task (concurrency-safe: dict keyed by task_id)
        running_tasks = context.user_data.setdefault("running_tasks", {})
        running_tasks[task_id] = task_future

        # Stream status updates while pipeline runs
        last_stage = ""
        while not task_future.done():
            await asyncio.sleep(3)
            stage = get_stage(task_id)
            if stage and stage != last_stage:
                label = STAGE_LABELS.get(stage, stage)
                try:
                    await status_msg.edit_text(f"{label} (task {task_id[:8]})")
                except Exception:
                    pass  # Message edit can fail if identical text
                last_stage = stage

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

        # Send artifact files
        for fpath in result.get("artifacts", []):
            p = Path(fpath)
            if p.is_file() and p.stat().st_size < config.MAX_FILE_SIZE_BYTES:
                with open(p, "rb") as f:
                    await update.message.reply_document(document=f, filename=p.name)

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
    except Exception as e:
        logger.error("Task %s failed: %s", task_id, e, exc_info=True)
        await update.message.reply_text(f"Task failed: {e}")
        await db.update_task(task_id, status="failed", error=str(e))
    finally:
        running_tasks = context.user_data.get("running_tasks", {})
        running_tasks.pop(task_id, None)
        context.user_data.pop("pending_files", None)  # Always clear, even on error
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


async def _send_long_message(update: Update, text: str):
    """Split and send messages that exceed Telegram's character limit."""
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

    for chunk in chunks:
        if chunk:
            await update.message.reply_text(chunk)
