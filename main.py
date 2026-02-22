from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys

import config  # noqa: E402 - must load .env before other imports

# Configure logging with absolute path and rotation (10MB max, 3 backups)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            config.BASE_DIR / "agentsutra.log",
            maxBytes=10_000_000,
            backupCount=3,
        ),
    ],
)
logger = logging.getLogger("agentsutra")

from storage.db import init_db, recover_stale_tasks, prune_old_data, cleanup_workspace_files  # noqa: E402
from bot.telegram_bot import create_bot  # noqa: E402
from scheduler.cron import start_scheduler, stop_scheduler  # noqa: E402
from tools.projects import load_projects  # noqa: E402


def _ensure_shared_project_venv():
    """Create and verify the shared project venv used when projects have no venv: key."""
    venv_dir = config.PROJECTS_VENV_DIR
    python_bin = venv_dir / "bin" / "python3"
    pip_bin = venv_dir / "bin" / "pip"

    def _create_venv():
        import subprocess
        logger.info("Creating shared project venv at %s", venv_dir)
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            check=True, capture_output=True,
        )

    def _smoke_test() -> bool:
        import subprocess
        try:
            result = subprocess.run(
                [str(pip_bin), "--version"],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False

    try:
        if not python_bin.exists():
            _create_venv()

        if not _smoke_test():
            logger.warning("Shared project venv broken, recreating")
            import shutil
            shutil.rmtree(venv_dir, ignore_errors=True)
            _create_venv()
            if not _smoke_test():
                logger.error("Failed to create working shared project venv")
                return

        logger.info("Shared project venv ready at %s", venv_dir)
    except Exception as e:
        logger.error("Failed to bootstrap shared project venv: %s", e)


def main():
    """Main entry point."""
    # Validate config
    if not config.ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set. Add it to .env")
        sys.exit(1)
    if not config.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set. Add it to .env")
        sys.exit(1)
    if not config.ALLOWED_USER_IDS:
        logger.error("ALLOWED_USER_IDS not set. Add it to .env")
        sys.exit(1)

    logger.info("=" * 50)
    logger.info("AgentSutra v%s starting up", config.VERSION)
    logger.info("=" * 50)
    logger.info("Allowed user IDs: %s", config.ALLOWED_USER_IDS)
    logger.info("Default model: %s", config.DEFAULT_MODEL)
    logger.info("Workspace: %s", config.WORKSPACE_DIR)

    # Initialize database (run in temporary event loop)
    asyncio.run(init_db())

    # Crash recovery: mark tasks stuck in 'running'/'pending' from previous crash
    asyncio.run(recover_stale_tasks())

    # Storage cleanup on startup — prune old records and stale files
    asyncio.run(prune_old_data())
    cleanup_workspace_files()
    logger.info("Storage cleanup completed")

    # Bootstrap shared project venv (for projects without their own venv: key)
    _ensure_shared_project_venv()

    # Python 3.9 fix: asyncio.run() closes the event loop it creates.
    # python-telegram-bot's ApplicationBuilder needs an active loop.
    # Ensure one exists before building the bot.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Load project registry
    projects = load_projects()
    logger.info("Projects registered: %d", len(projects))

    # Create bot application
    bot = create_bot()

    # Start scheduler inside bot's post_init so it shares the same event loop
    async def on_startup(app):
        start_scheduler()
        logger.info("All services initialized")

    async def on_shutdown(app):
        stop_scheduler()
        logger.info("AgentSutra stopped")

    bot.post_init = on_startup
    bot.post_shutdown = on_shutdown

    logger.info("Starting Telegram bot (polling mode)...")
    logger.info("Send /start to your bot to begin")

    bot.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
