"""Tests for bot/handlers.py — auth, resource guards, message splitting, file upload."""
from __future__ import annotations

import sys
import os
import asyncio

# Ensure project root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

import config
from bot.handlers import auth_required, _check_resources, _send_long_message, handle_document, _sanitize_error_for_user


# ── Helpers ──────────────────────────────────────────────────────


def _mock_update(user_id=12345, text="hello"):
    """Create a mock Telegram Update object."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = AsyncMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.message.reply_document = AsyncMock()
    return update


def _mock_context(user_data=None):
    """Create a mock Telegram context."""
    context = MagicMock()
    context.user_data = user_data if user_data is not None else {}
    context.bot = AsyncMock()
    return context


# ── Auth decorator ───────────────────────────────────────────────


class TestAuthRequired:
    """auth_required decorator must enforce ALLOWED_USER_IDS."""

    @pytest.mark.asyncio
    async def test_authorized_user_proceeds(self):
        """User ID in ALLOWED_USER_IDS — handler runs normally."""
        handler_ran = False

        @auth_required
        async def dummy_handler(update, context):
            nonlocal handler_ran
            handler_ran = True

        update = _mock_update(user_id=12345)
        context = _mock_context()

        with patch.object(config, "ALLOWED_USER_IDS", [12345]):
            await dummy_handler(update, context)

        assert handler_ran is True
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_unauthorized_user_rejected(self):
        """User ID NOT in ALLOWED_USER_IDS — rejected with message."""
        handler_ran = False

        @auth_required
        async def dummy_handler(update, context):
            nonlocal handler_ran
            handler_ran = True

        update = _mock_update(user_id=99999)
        context = _mock_context()

        with patch.object(config, "ALLOWED_USER_IDS", [12345]):
            await dummy_handler(update, context)

        assert handler_ran is False
        update.message.reply_text.assert_called_once()
        call_args = update.message.reply_text.call_args[0][0]
        assert "Unauthorized" in call_args

    def test_wraps_preserves_function_name(self):
        """Decorated function retains __name__ and __doc__."""

        @auth_required
        async def my_handler(update, context):
            """My docstring."""
            pass

        assert my_handler.__name__ == "my_handler"
        assert my_handler.__doc__ == "My docstring."

    @pytest.mark.asyncio
    async def test_missing_user_rejected(self):
        """effective_user is None — returns silently without crash."""
        handler_ran = False

        @auth_required
        async def dummy_handler(update, context):
            nonlocal handler_ran
            handler_ran = True

        update = _mock_update()
        update.effective_user = None
        context = _mock_context()

        with patch.object(config, "ALLOWED_USER_IDS", [12345]):
            await dummy_handler(update, context)

        assert handler_ran is False


# ── Resource guards ──────────────────────────────────────────────


class TestCheckResources:
    """_check_resources must enforce RAM and concurrency limits."""

    def test_accepts_when_under_limits(self):
        """No active tasks, normal RAM — returns None."""
        mem_mock = MagicMock()
        mem_mock.percent = 50.0

        with patch("psutil.virtual_memory", return_value=mem_mock):
            result = _check_resources({})
        assert result is None

    def test_rejects_at_max_concurrent(self):
        """At MAX_CONCURRENT_TASKS active futures — returns rejection."""
        futures = {}
        for i in range(config.MAX_CONCURRENT_TASKS):
            f = asyncio.Future()
            futures[f"task_{i}"] = f  # Not done — still active

        result = _check_resources(futures)
        assert result is not None
        assert "concurrent" in result.lower() or "Too many" in result

    def test_rejects_high_ram(self):
        """RAM above threshold — returns rejection."""
        mem_mock = MagicMock()
        mem_mock.percent = 95.0

        with patch("psutil.virtual_memory", return_value=mem_mock):
            result = _check_resources({})
        assert result is not None
        assert "memory" in result.lower() or "%" in result

    def test_prunes_done_futures(self):
        """Completed futures are pruned before counting."""
        futures = {}
        # 2 done futures
        for i in range(2):
            f = asyncio.Future()
            f.set_result(None)
            futures[f"done_{i}"] = f
        # 1 active future
        futures["active_1"] = asyncio.Future()

        mem_mock = MagicMock()
        mem_mock.percent = 50.0

        with patch("psutil.virtual_memory", return_value=mem_mock):
            result = _check_resources(futures)

        assert result is None  # Only 1 active, under limit
        assert len(futures) == 1  # Done futures were pruned
        assert "active_1" in futures

    def test_prunes_then_rejects(self):
        """All active after prune — still rejects."""
        futures = {}
        for i in range(config.MAX_CONCURRENT_TASKS):
            futures[f"active_{i}"] = asyncio.Future()  # Not done

        result = _check_resources(futures)
        assert result is not None
        assert len(futures) == config.MAX_CONCURRENT_TASKS  # Nothing pruned


# ── Message splitting ────────────────────────────────────────────


class TestSendLongMessage:
    """_send_long_message must split messages at Telegram's 4096 char limit."""

    @pytest.mark.asyncio
    async def test_short_message_single_send(self):
        """Short message — exactly 1 reply_text call."""
        update = _mock_update()
        await _send_long_message(update, "Hello, world!")
        update.message.reply_text.assert_called_once_with("Hello, world!")

    @pytest.mark.asyncio
    async def test_message_at_limit_single_send(self):
        """Exactly 4096 chars — single send, no split."""
        update = _mock_update()
        text = "x" * 4096
        await _send_long_message(update, text)
        update.message.reply_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_long_message_splits_at_newlines(self):
        """Message exceeding limit with newlines — splits into multiple chunks."""
        update = _mock_update()
        # Create a message with many lines that exceeds 4096
        lines = ["Line " + str(i) + " " * 50 for i in range(100)]
        text = "\n".join(lines)
        assert len(text) > 4096

        await _send_long_message(update, text)
        call_count = update.message.reply_text.call_count
        assert call_count >= 2

        # Verify each chunk is within limit
        for call in update.message.reply_text.call_args_list:
            chunk = call[0][0]
            assert len(chunk) <= config.TELEGRAM_MAX_MESSAGE_LENGTH

    @pytest.mark.asyncio
    async def test_very_long_line_hard_splits(self):
        """Single 8000-char line with no newlines — hard-split at 4096."""
        update = _mock_update()
        text = "x" * 8000
        await _send_long_message(update, text)
        assert update.message.reply_text.call_count >= 2

        for call in update.message.reply_text.call_args_list:
            chunk = call[0][0]
            assert len(chunk) <= config.TELEGRAM_MAX_MESSAGE_LENGTH

    @pytest.mark.asyncio
    async def test_empty_chunks_skipped(self):
        """Message with lots of blank lines — no empty reply_text calls."""
        update = _mock_update()
        text = "Hello\n\n\n\n\nWorld"
        await _send_long_message(update, text)

        for call in update.message.reply_text.call_args_list:
            chunk = call[0][0]
            assert len(chunk) > 0


# ── File upload handling ─────────────────────────────────────────


class TestHandleDocument:
    """handle_document must validate file size and save uploads."""

    @pytest.mark.asyncio
    async def test_oversized_file_rejected(self):
        """File exceeding MAX_FILE_SIZE_BYTES — rejected with message."""
        update = _mock_update()
        update.message.document = MagicMock()
        update.message.document.file_size = config.MAX_FILE_SIZE_BYTES + 1
        context = _mock_context()

        with patch.object(config, "ALLOWED_USER_IDS", [12345]):
            await handle_document(update, context)

        update.message.reply_text.assert_called_once()
        call_args = update.message.reply_text.call_args[0][0]
        assert "too large" in call_args.lower() or "max" in call_args.lower()

    @pytest.mark.asyncio
    async def test_valid_file_saved_to_pending(self):
        """File within limits — saved, path added to pending_files."""
        update = _mock_update()
        update.message.document = MagicMock()
        update.message.document.file_size = 1024
        update.message.document.file_id = "test_file_id"
        update.message.document.file_name = "test.csv"

        mock_file = AsyncMock()
        mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"test data"))

        context = _mock_context()
        context.bot.get_file = AsyncMock(return_value=mock_file)

        with patch.object(config, "ALLOWED_USER_IDS", [12345]), \
             patch("bot.handlers.save_upload", return_value=Path("/tmp/test.csv")) as mock_save:
            await handle_document(update, context)

        mock_save.assert_called_once()
        assert "pending_files" in context.user_data
        assert len(context.user_data["pending_files"]) == 1

    @pytest.mark.asyncio
    async def test_multiple_files_accumulate(self):
        """Upload 2 files — pending_files has 2 entries."""
        context = _mock_context(user_data={"pending_files": ["/tmp/first.csv"]})

        update = _mock_update()
        update.message.document = MagicMock()
        update.message.document.file_size = 1024
        update.message.document.file_id = "test_file_id"
        update.message.document.file_name = "second.csv"

        mock_file = AsyncMock()
        mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"data"))

        context.bot.get_file = AsyncMock(return_value=mock_file)

        with patch.object(config, "ALLOWED_USER_IDS", [12345]), \
             patch("bot.handlers.save_upload", return_value=Path("/tmp/second.csv")):
            await handle_document(update, context)

        assert len(context.user_data["pending_files"]) == 2

    @pytest.mark.asyncio
    async def test_filename_sanitization(self):
        """Filename with path components — save_upload receives the raw name for sanitization."""
        update = _mock_update()
        update.message.document = MagicMock()
        update.message.document.file_size = 1024
        update.message.document.file_id = "test_file_id"
        update.message.document.file_name = "../../etc/passwd"

        mock_file = AsyncMock()
        mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"data"))

        context = _mock_context()
        context.bot.get_file = AsyncMock(return_value=mock_file)

        with patch.object(config, "ALLOWED_USER_IDS", [12345]), \
             patch("bot.handlers.save_upload", return_value=Path("/tmp/passwd")) as mock_save:
            await handle_document(update, context)

        # save_upload is called and handles sanitization internally
        mock_save.assert_called_once()
        # The function was called with the raw filename — save_upload strips path components
        call_args = mock_save.call_args
        assert call_args[0][1] == "../../etc/passwd"  # Raw name passed to save_upload


# ── Scheduled task timeout ──────────────────────────────────────────


class TestArtifactDeliveryResilience:
    """Artifact delivery must handle empty files and send failures gracefully."""

    @pytest.mark.asyncio
    async def test_scheduled_empty_file_skipped(self):
        """Empty artifact files (0 bytes) should be skipped with a warning."""
        import tempfile
        from bot.handlers import _scheduled_task_run

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
            empty_path = f.name  # 0 bytes

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, mode='wb') as f:
            f.write(b"%PDF-content")
            valid_path = f.name

        mock_result = {
            "final_response": "Done",
            "artifacts": [empty_path, valid_path],
        }

        try:
            with patch("bot.handlers.db") as mock_db, \
                 patch("bot.handlers.run_task", return_value=mock_result), \
                 patch("bot.handlers.Bot") as MockBot, \
                 patch.object(config, "LONG_TIMEOUT", 30), \
                 patch.object(config, "RAM_THRESHOLD_PERCENT", 99):

                mock_db.create_task = AsyncMock()
                mock_db.update_task = AsyncMock()
                mock_db.build_conversation_context = AsyncMock(return_value="")

                mock_bot_instance = AsyncMock()
                MockBot.return_value.__aenter__ = AsyncMock(return_value=mock_bot_instance)
                MockBot.return_value.__aexit__ = AsyncMock(return_value=False)

                await _scheduled_task_run(chat_id=123, user_id=456, task_message="test")

                # Only the valid PDF should be sent (empty HTML skipped)
                doc_calls = mock_bot_instance.send_document.call_args_list
                assert len(doc_calls) == 1
                assert doc_calls[0][1]["filename"].endswith(".pdf")
        finally:
            os.unlink(empty_path)
            os.unlink(valid_path)

    @pytest.mark.asyncio
    async def test_scheduled_send_failure_continues(self):
        """If one artifact send fails, remaining artifacts still get sent."""
        import tempfile
        from bot.handlers import _scheduled_task_run

        files = []
        for content in [b"first", b"second"]:
            with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode='wb') as f:
                f.write(content)
                files.append(f.name)

        mock_result = {
            "final_response": "Done",
            "artifacts": files,
        }

        call_count = [0]
        async def mock_send_doc(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Telegram API error")

        try:
            with patch("bot.handlers.db") as mock_db, \
                 patch("bot.handlers.run_task", return_value=mock_result), \
                 patch("bot.handlers.Bot") as MockBot, \
                 patch.object(config, "LONG_TIMEOUT", 30), \
                 patch.object(config, "RAM_THRESHOLD_PERCENT", 99):

                mock_db.create_task = AsyncMock()
                mock_db.update_task = AsyncMock()
                mock_db.build_conversation_context = AsyncMock(return_value="")

                mock_bot_instance = AsyncMock()
                mock_bot_instance.send_document = mock_send_doc
                MockBot.return_value.__aenter__ = AsyncMock(return_value=mock_bot_instance)
                MockBot.return_value.__aexit__ = AsyncMock(return_value=False)

                # Should not crash despite one send failing
                await _scheduled_task_run(chat_id=123, user_id=456, task_message="test")

                # Both were attempted
                assert call_count[0] == 2
        finally:
            for f in files:
                os.unlink(f)


class TestScheduledTaskTimeout:
    """_scheduled_task_run must respect LONG_TIMEOUT."""

    @pytest.mark.asyncio
    async def test_scheduled_task_times_out(self):
        """Scheduled task exceeding LONG_TIMEOUT is killed and marked failed."""
        import asyncio as _asyncio
        from bot.handlers import _scheduled_task_run

        with patch("bot.handlers.db") as mock_db, \
             patch("bot.handlers.run_task", side_effect=lambda *a, **kw: __import__("time").sleep(10)), \
             patch("bot.handlers.Bot") as MockBot, \
             patch.object(config, "LONG_TIMEOUT", 0.1), \
             patch.object(config, "RAM_THRESHOLD_PERCENT", 99):

            mock_db.create_task = AsyncMock()
            mock_db.update_task = AsyncMock()
            mock_db.build_conversation_context = AsyncMock(return_value="")

            mock_bot_instance = AsyncMock()
            MockBot.return_value.__aenter__ = AsyncMock(return_value=mock_bot_instance)
            MockBot.return_value.__aexit__ = AsyncMock(return_value=False)

            await _scheduled_task_run(chat_id=123, user_id=456, task_message="slow task")

            # Verify task was marked as failed with timeout error
            mock_db.update_task.assert_called()
            last_call = mock_db.update_task.call_args_list[-1]
            assert last_call[1].get("status") == "failed" or "failed" in str(last_call)
            assert "timed out" in str(last_call).lower() or "Timed out" in str(last_call)


# ── Error sanitization ─────────────────────────────────────────────


class TestErrorSanitization:
    """_sanitize_error_for_user must strip sensitive details."""

    def test_strips_absolute_paths(self):
        """Absolute paths are reduced to just the filename."""
        msg = _sanitize_error_for_user("FileNotFoundError: /Users/prav/secret/project/data.csv not found")
        assert "/Users/prav" not in msg
        assert "data.csv" in msg

    def test_strips_api_key_fragments(self):
        """API key-like strings are redacted."""
        msg = _sanitize_error_for_user("Auth failed: sk-ant-api03-abcdef1234567890xyz")
        assert "abcdef" not in msg
        assert "[REDACTED]" in msg

    def test_preserves_meaningful_error(self):
        """Normal error messages are kept intact."""
        msg = _sanitize_error_for_user("Division by zero in calculate()")
        assert "Division by zero" in msg

    def test_truncates_long_errors(self):
        """Very long error strings are truncated to 500 chars."""
        long_msg = "x" * 1000
        msg = _sanitize_error_for_user(long_msg)
        assert len(msg) <= 500

    def test_token_keyword_redacted(self):
        """token=<value> patterns are redacted."""
        msg = _sanitize_error_for_user("failed with token=abc123def456ghi789jkl")
        assert "abc123" not in msg


# ── Rate-limited message sending ───────────────────────────────────


class TestSendLongMessageRateLimit:
    """_send_long_message must handle Telegram rate limit errors."""

    @pytest.mark.asyncio
    async def test_retries_on_retry_after_error(self):
        """When Telegram returns RetryAfter, waits and retries the chunk."""
        update = _mock_update()
        call_count = [0]

        async def flaky_reply(text):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Flood control exceeded. Retry after 1 seconds")

        update.message.reply_text = flaky_reply
        # Create a long message that needs splitting
        text = ("A" * 4000 + "\n") * 2  # ~8000 chars, splits into 2 chunks
        # Should not raise
        await _send_long_message(update, text)
        # First chunk: 1 fail + 1 retry + second chunk = 3 total
        assert call_count[0] >= 2
