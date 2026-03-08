import logging
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

import config
from bot.handlers import (
    cmd_start,
    cmd_status,
    cmd_history,
    cmd_usage,
    cmd_cost,
    cmd_health,
    cmd_exec,
    cmd_context,
    cmd_cancel,
    cmd_projects,
    cmd_schedule,
    cmd_chain,
    cmd_debug,
    cmd_deploy,
    cmd_stopserver,
    cmd_servers,
    cmd_retry,
    cmd_setup,
    cmd_reindex,
    handle_message,
    handle_document,
    handle_photo,
)

logger = logging.getLogger(__name__)


def create_bot():
    """Create and configure the Telegram bot application."""
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in .env")

    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("exec", cmd_exec))
    app.add_handler(CommandHandler("context", cmd_context))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("chain", cmd_chain))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("deploy", cmd_deploy))
    app.add_handler(CommandHandler("stopserver", cmd_stopserver))
    app.add_handler(CommandHandler("servers", cmd_servers))
    app.add_handler(CommandHandler("retry", cmd_retry))
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("reindex", cmd_reindex))

    # File handlers (must be before text handler)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Text message handler (catch-all for non-commands)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Telegram bot configured with %d command handlers", 19)
    return app
