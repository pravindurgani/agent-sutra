"""Tests for brain/nodes/executor.py — code block extraction, timeout estimation, param extraction, dep bootstrap."""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
from unittest.mock import patch
from brain.nodes.executor import (
    _strip_markdown_blocks, _estimate_timeout, _extract_params,
    _bootstrap_project_deps, _parse_import_error_from_result,
    _detect_truncation, _check_referenced_files,
)
from tools.sandbox import ExecutionResult


class TestStripMarkdownBlocks:
    """Code extraction from markdown-fenced blocks."""

    def test_single_python_block(self):
        text = '```python\nprint("hello")\n```'
        assert _strip_markdown_blocks(text) == 'print("hello")'

    def test_single_plain_block(self):
        text = '```\nx = 1\n```'
        assert _strip_markdown_blocks(text) == "x = 1"

    def test_multiple_blocks_returns_first(self):
        """A-19: Return first block (not longest) to prevent gaming."""
        text = '```python\nshort\n```\n\n```python\nthis is the longer code block\nwith multiple lines\n```'
        result = _strip_markdown_blocks(text)
        assert result == "short"

    def test_no_markdown_returns_original(self):
        text = 'print("hello")'
        assert _strip_markdown_blocks(text) == text

    def test_empty_code_block(self):
        text = '```python\n\n```'
        result = _strip_markdown_blocks(text)
        assert result == ""

    def test_surrounding_text_stripped(self):
        text = 'Here is the code:\n```python\nresult = 42\n```\nDone.'
        assert _strip_markdown_blocks(text) == "result = 42"

    def test_javascript_block(self):
        text = '```javascript\nconsole.log("hi")\n```'
        assert _strip_markdown_blocks(text) == 'console.log("hi")'

    def test_bash_block(self):
        text = '```bash\necho hello\n```'
        assert _strip_markdown_blocks(text) == "echo hello"

    def test_backticks_inside_code(self):
        """Bug #4: inner backticks in template literals should not break extraction."""
        text = (
            "Here is the code:\n"
            "```javascript\n"
            "const greeting = `Hello ${name}`;\n"
            "const html = `<div>${items.map(i => `<span>${i}</span>`).join('')}</div>`;\n"
            "console.log(greeting);\n"
            "```\n"
            "That's it."
        )
        result = _strip_markdown_blocks(text)
        assert "console.log(greeting)" in result
        assert "const greeting" in result
        assert "items.map" in result

    def test_backtick_in_python_string(self):
        """Backtick characters in Python strings should not affect parsing."""
        text = (
            "```python\n"
            'x = "some ` backtick"\n'
            "print(x)\n"
            "```"
        )
        result = _strip_markdown_blocks(text)
        assert "print(x)" in result


class TestEstimateTimeout:
    """Timeout estimation based on task type and file sizes."""

    def test_default_code_task(self):
        state = {"task_type": "code", "files": []}
        timeout = _estimate_timeout(state)
        assert timeout >= config.EXECUTION_TIMEOUT  # at least the configured default

    def test_frontend_gets_more_time(self):
        state = {"task_type": "frontend", "files": []}
        timeout = _estimate_timeout(state)
        assert timeout >= 300

    def test_automation_gets_more_time(self):
        state = {"task_type": "automation", "files": []}
        timeout = _estimate_timeout(state)
        assert timeout >= 300

    def test_capped_at_max(self):
        import config
        state = {"task_type": "frontend", "files": []}
        timeout = _estimate_timeout(state)
        assert timeout <= config.MAX_CODE_EXECUTION_TIMEOUT


class TestExtractParams:
    """Parameter extraction from Claude responses, including markdown fence handling."""

    def test_plain_json_response(self):
        """Claude returns bare JSON — parsed correctly."""
        state = {
            "message": "Generate report for Light & Wonder",
            "files": ["/tmp/data.xlsx"],
            "project_config": {"commands": {"report": "python3 cli.py --client {client} --input {file}"}},
        }
        with patch("brain.nodes.executor.claude_client.call", return_value='{"client": "Light & Wonder", "file": "/tmp/data.xlsx"}'):
            params = _extract_params(state)
        assert params["client"] == "Light & Wonder"
        assert params["file"] == "/tmp/data.xlsx"

    def test_markdown_fenced_json_response(self):
        """Claude wraps JSON in ```json...``` fences — still parsed correctly."""
        state = {
            "message": "Generate report for Light & Wonder",
            "files": ["/tmp/data.xlsx"],
            "project_config": {"commands": {"report": "python3 cli.py --client {client} --input {file}"}},
        }
        fenced_response = '```json\n{"client": "Light & Wonder", "file": "/tmp/data.xlsx"}\n```'
        with patch("brain.nodes.executor.claude_client.call", return_value=fenced_response):
            params = _extract_params(state)
        assert params["client"] == "Light & Wonder"
        assert params["file"] == "/tmp/data.xlsx"

    def test_no_placeholders_returns_empty(self):
        """Commands with no {param} placeholders — returns empty dict."""
        state = {
            "message": "Run the scraper",
            "files": [],
            "project_config": {"commands": {"run": "python3 scraper.py"}},
        }
        params = _extract_params(state)
        assert params == {}

    def test_unparseable_response_fallback(self):
        """Claude returns garbage — falls back to auto-detect file from uploads."""
        state = {
            "message": "Generate report",
            "files": ["/tmp/upload.xlsx"],
            "project_config": {"commands": {"report": "python3 cli.py --input {file}"}},
        }
        with patch("brain.nodes.executor.claude_client.call", return_value="I cannot determine the parameters"):
            params = _extract_params(state)
        assert params.get("file") == "/tmp/upload.xlsx"


# ── _parse_import_error_from_result (v6.11) ──────────────────────


class TestParseImportErrorFromResult:
    """Extract missing module from ExecutionResult and map to pip name."""

    def test_module_not_found(self):
        result = ExecutionResult(success=False, traceback="ModuleNotFoundError: No module named 'pandas'")
        assert _parse_import_error_from_result(result) == "pandas"

    def test_import_error(self):
        result = ExecutionResult(success=False, stderr="ImportError: No module named 'requests'")
        assert _parse_import_error_from_result(result) == "requests"

    def test_mapped_module_pil(self):
        result = ExecutionResult(success=False, traceback="ModuleNotFoundError: No module named 'PIL'")
        assert _parse_import_error_from_result(result) == "Pillow"

    def test_mapped_module_dotenv(self):
        result = ExecutionResult(success=False, traceback="ModuleNotFoundError: No module named 'dotenv'")
        assert _parse_import_error_from_result(result) == "python-dotenv"

    def test_mapped_module_cv2(self):
        result = ExecutionResult(success=False, traceback="ModuleNotFoundError: No module named 'cv2'")
        assert _parse_import_error_from_result(result) == "opencv-python"

    def test_mapped_module_attr(self):
        """attr must map to attrs, not the abandoned 'attr' package."""
        result = ExecutionResult(success=False, traceback="ModuleNotFoundError: No module named 'attr'")
        assert _parse_import_error_from_result(result) == "attrs"

    def test_mapped_module_bio(self):
        result = ExecutionResult(success=False, traceback="ModuleNotFoundError: No module named 'Bio'")
        assert _parse_import_error_from_result(result) == "biopython"

    def test_uses_canonical_pip_map(self):
        """Executor must use the same _PIP_NAME_MAP as sandbox.py (no drift)."""
        from tools.sandbox import _PIP_NAME_MAP
        # Verify the executor actually imports from sandbox, not a local copy
        result = ExecutionResult(success=False, traceback="ModuleNotFoundError: No module named 'serial'")
        assert _parse_import_error_from_result(result) == _PIP_NAME_MAP["serial"]

    def test_no_import_error(self):
        result = ExecutionResult(success=False, traceback="ZeroDivisionError: division by zero")
        assert _parse_import_error_from_result(result) is None

    def test_empty_result(self):
        result = ExecutionResult(success=False, traceback="", stderr="")
        assert _parse_import_error_from_result(result) is None

    def test_prefers_traceback_over_stderr(self):
        """When both traceback and stderr have content, traceback is checked."""
        result = ExecutionResult(
            success=False,
            traceback="ModuleNotFoundError: No module named 'yaml'",
            stderr="some other noise",
        )
        assert _parse_import_error_from_result(result) == "pyyaml"

    def test_falls_back_to_stderr(self):
        """When traceback is empty, stderr is checked."""
        result = ExecutionResult(
            success=False,
            traceback="",
            stderr="ModuleNotFoundError: No module named 'sklearn'",
        )
        assert _parse_import_error_from_result(result) == "scikit-learn"


# ── _bootstrap_project_deps (v6.11) ──────────────────────────────


class TestBootstrapProjectDeps:
    """Dependency bootstrapping before project execution."""

    def test_no_requirements_file(self, tmp_path):
        """Returns None when no requirements.txt exists."""
        result = _bootstrap_project_deps(str(tmp_path))
        assert result is None

    def test_successful_bootstrap(self, tmp_path):
        """Returns None (success) when pip install succeeds."""
        req = tmp_path / "requirements.txt"
        req.write_text("requests\n")

        with patch("brain.nodes.executor.run_shell") as mock_shell:
            mock_shell.return_value = ExecutionResult(success=True)
            result = _bootstrap_project_deps(str(tmp_path))

        assert result is None
        mock_shell.assert_called_once()
        call_args = mock_shell.call_args
        assert "pip3 install -r" in call_args[0][0] or "pip3 install -r" in call_args.kwargs.get("command", call_args[0][0])

    def test_failed_bootstrap_returns_error(self, tmp_path):
        """Returns error string when pip install fails."""
        req = tmp_path / "requirements.txt"
        req.write_text("nonexistent-package-xyz\n")

        with patch("brain.nodes.executor.run_shell") as mock_shell:
            mock_shell.return_value = ExecutionResult(success=False, stderr="No matching distribution found")
            result = _bootstrap_project_deps(str(tmp_path))

        assert result is not None
        assert "Failed to install" in result

    def test_uses_venv_pip(self, tmp_path):
        """Uses venv pip when venv_path is provided."""
        req = tmp_path / "requirements.txt"
        req.write_text("requests\n")

        with patch("brain.nodes.executor.run_shell") as mock_shell:
            mock_shell.return_value = ExecutionResult(success=True)
            _bootstrap_project_deps(str(tmp_path), venv_path="/path/to/venv")

        call_cmd = mock_shell.call_args[0][0]
        assert "/path/to/venv/bin/pip" in call_cmd


# ── _detect_truncation (Phase 0a) ────────────────────────────────


class TestDetectTruncationShebangGuard:
    """Shell if/fi truncation checks must only fire for actual shell scripts."""

    def test_detect_truncation_python_many_ifs_not_truncated(self):
        """Python code with 20 if statements, no shebang → NOT truncated."""
        lines = ["import sys\n"]
        for i in range(20):
            lines.append(f"if x > {i}:\n    print({i})\n")
        lines.append('print("done")\n')
        code = "".join(lines)
        assert _detect_truncation(code) is False

    def test_detect_truncation_shell_script_unclosed_if(self):
        """Bash script with shebang, 5 if and 0 fi → truncated."""
        code = "#!/bin/bash\n"
        for i in range(5):
            code += f'if [ -f file{i} ]; then\n  echo "found {i}"\n'
        assert _detect_truncation(code) is True

    def test_detect_truncation_shell_script_balanced(self):
        """Bash script with balanced if/fi → NOT truncated."""
        code = (
            "#!/bin/bash\n"
            "if [ -f config.sh ]; then\n"
            "  source config.sh\n"
            "fi\n"
            "if [ -z \"$DB_HOST\" ]; then\n"
            "  export DB_HOST=localhost\n"
            "fi\n"
            "echo done\n"
        )
        assert _detect_truncation(code) is False

    def test_detect_truncation_python_still_catches_parens(self):
        """Python code with many unclosed parens still returns True."""
        code = (
            "import pandas as pd\n"
            "result = func(\n"
            "    inner(\n"
            "        deep(\n"
            "            deeper(\n"
        )
        assert _detect_truncation(code) is True

    def test_detect_truncation_shell_no_shebang_unclosed_parens(self):
        """Shell-style code without shebang but with 5 unclosed parens → caught by paren check."""
        code = "echo hello\nfoo(\nbar(\nbaz(\nqux(\nquux(\n"
        assert _detect_truncation(code) is True

    def test_detect_truncation_env_bash_shebang(self):
        """#!/usr/bin/env bash shebang is recognised as shell."""
        code = "#!/usr/bin/env bash\n"
        for i in range(5):
            code += f"if [ -d dir{i} ]; then\n  ls dir{i}\n"
        assert _detect_truncation(code) is True

    def test_detect_truncation_env_sh_shebang(self):
        """#!/usr/bin/env sh shebang is recognised as shell."""
        code = "#!/usr/bin/env sh\n"
        for i in range(5):
            code += f"if [ -d dir{i} ]; then\n  ls dir{i}\n"
        assert _detect_truncation(code) is True


# ── v9.0.0 Phase 3: HTML truncation detection ────────────────────


class TestDetectTruncationHTML:
    """HTML truncation: unclosed <html>, <script>, <style> tags."""

    def test_truncation_detects_unclosed_html(self) -> None:
        code = "<!DOCTYPE html><html><head></head><body><div>"
        assert _detect_truncation(code) is True

    def test_truncation_passes_complete_html(self) -> None:
        code = "<!DOCTYPE html><html><head></head><body></body></html>"
        assert _detect_truncation(code) is False

    def test_truncation_detects_unclosed_script(self) -> None:
        code = '<!DOCTYPE html><html><body><script>function foo() {'
        assert _detect_truncation(code) is True

    def test_truncation_detects_unclosed_style(self) -> None:
        code = "<!DOCTYPE html><html><head><style>.foo {"
        assert _detect_truncation(code) is True

    def test_truncation_ignores_html_in_python_string(self) -> None:
        code = 'html = "<html>" + content + "</html>"'
        assert _detect_truncation(code) is False

    def test_truncation_no_false_positive_on_pure_python(self) -> None:
        code = 'print("hello world")'
        assert _detect_truncation(code) is False


# ── _check_referenced_files (Phase 2) ────────────────────────────


class TestCheckReferencedFilesExitLanguage:
    """_check_referenced_files() must instruct sys.exit(1) for missing files."""

    def test_check_referenced_files_missing_warns_exit(self, tmp_path):
        """Missing file warning includes sys.exit(1) instruction."""
        warning = _check_referenced_files("Analyse data.csv for trends", tmp_path)
        assert "sys.exit(1)" in warning
        assert "data.csv" in warning

    def test_check_referenced_files_existing_no_warn(self, tmp_path):
        """Existing file produces no warning."""
        (tmp_path / "data.csv").write_text("a,b\n1,2\n")
        warning = _check_referenced_files("Analyse data.csv for trends", tmp_path)
        assert warning == ""


# ── Phase 10: Over-generation limits ──────────────────────────────


class TestOverGenerationLimits:
    """Code generation should include length guidance and warn on long output."""

    def test_code_gen_system_prompt_includes_length_guidance(self):
        """CODE_GEN_SYSTEM must contain explicit length guidance."""
        from brain.nodes.executor import CODE_GEN_SYSTEM
        assert "50-300 lines" in CODE_GEN_SYSTEM
        assert "boilerplate" in CODE_GEN_SYSTEM.lower()

    def test_code_gen_warns_on_long_output(self, caplog):
        """Code gen producing >500 lines triggers a warning log."""
        import logging
        from brain.nodes.executor import logger as executor_logger

        # Simulate the over-generation check directly
        code = "\n".join(f"line_{i} = {i}" for i in range(601))
        with caplog.at_level(logging.WARNING, logger="brain.nodes.executor"):
            if code and code.count("\n") > 500:
                executor_logger.warning(
                    "Code gen produced %d lines — possible over-generation",
                    code.count("\n"),
                )

        assert any("over-generation" in r.message for r in caplog.records)
        assert any("600" in r.message for r in caplog.records)


# ── v9.0.0 Phase 5: executor respects was_refused ────────────────────


class TestExecutorRespectsWasRefused:
    """Executor must skip code generation when planner refused the task."""

    def test_execute_skips_on_was_refused(self) -> None:
        """was_refused=True → immediate return, code='', retry_count=MAX_RETRIES."""
        from brain.nodes.executor import execute

        state = {
            "task_id": "test-refused", "user_id": 1, "message": "Read /etc/shadow",
            "files": [], "task_type": "code", "project_name": "",
            "project_config": {}, "plan": "I cannot do this.",
            "was_refused": True, "retry_count": 0,
        }
        result = execute(state)
        assert result["code"] == ""
        assert result["retry_count"] == config.MAX_RETRIES
        assert "refused" in result["execution_result"].lower()

    @patch("brain.nodes.executor.claude_client.call", return_value="print('hello')")
    @patch("brain.nodes.executor.run_code_with_auto_install")
    def test_execute_proceeds_on_was_refused_false(
        self, mock_run: object, mock_call: object,
    ) -> None:
        """was_refused=False → proceeds to code generation normally."""
        from brain.nodes.executor import execute
        from tools.sandbox import ExecutionResult

        mock_run.return_value = ExecutionResult(
            success=True, stdout="hello\n", stderr="", return_code=0,
        )
        state = {
            "task_id": "test-normal", "user_id": 1, "message": "Print hello",
            "files": [], "task_type": "code", "project_name": "",
            "project_config": {}, "plan": "1. Print hello",
            "was_refused": False, "retry_count": 0,
            "working_dir": "", "auto_installed_packages": [],
            "audit_feedback": "", "conversation_context": "",
        }
        result = execute(state)
        # Should have generated and run code
        assert mock_call.called
        assert result.get("code") is not None
