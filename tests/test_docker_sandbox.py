"""Tests for Docker sandbox isolation in tools/sandbox.py.

Tier 1: Unit tests (always run, Docker not required)
Tier 2: Integration tests (require Docker + agentsutra-sandbox image)
Tier 3: Security tests (require Docker + agentsutra-sandbox image)
"""
from __future__ import annotations

import os
import sys
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
from tools.sandbox import (
    _docker_available,
    _docker_status,
    _build_docker_cmd,
    _docker_pip_install,
    run_code,
    run_code_with_auto_install,
    ExecutionResult,
)


# ── Helpers ────────────────────────────────────────────────────────


def _docker_installed() -> bool:
    return shutil.which("docker") is not None


def _sandbox_image_exists() -> bool:
    if not _docker_installed():
        return False
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "agentsutra-sandbox"],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


requires_docker = pytest.mark.skipif(
    not _docker_installed(), reason="Docker not installed",
)
requires_sandbox_image = pytest.mark.skipif(
    not _sandbox_image_exists(), reason="agentsutra-sandbox image not built",
)


# ══════════════════════════════════════════════════════════════════
# TIER 1: Unit Tests (no Docker required)
# ══════════════════════════════════════════════════════════════════


class TestDockerAvailableUnit:
    """Test _docker_available() with mocked subprocess."""

    def setup_method(self):
        _docker_status["available"] = False
        _docker_status["checked_at"] = 0.0

    @patch("tools.sandbox.subprocess.run")
    def test_docker_not_installed(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        with patch("tools.sandbox.Path.exists", return_value=True):
            assert _docker_available() is False

    @patch("tools.sandbox.subprocess.run")
    def test_docker_daemon_not_running(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        with patch("tools.sandbox.Path.exists", return_value=True):
            assert _docker_available() is False

    @patch("tools.sandbox.subprocess.run")
    def test_docker_running_image_missing(self, mock_run):
        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["docker", "info"]:
                return MagicMock(returncode=0)
            if cmd[:3] == ["docker", "image", "inspect"]:
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)
        mock_run.side_effect = side_effect
        with patch("tools.sandbox.Path.exists", return_value=True):
            assert _docker_available() is False

    @patch("tools.sandbox.subprocess.run")
    def test_docker_running_image_exists(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        with patch("tools.sandbox.Path.exists", return_value=True):
            assert _docker_available() is True

    @patch("tools.sandbox.subprocess.run")
    def test_result_is_cached(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        with patch("tools.sandbox.Path.exists", return_value=True):
            assert _docker_available() is True
            assert _docker_available() is True
            # docker info + docker image inspect = 2 calls, not 4
            assert mock_run.call_count == 2

    @patch("tools.sandbox.subprocess.run")
    def test_timeout_treated_as_unavailable(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=5)
        with patch("tools.sandbox.Path.exists", return_value=True):
            assert _docker_available() is False

    def test_socket_missing_fast_fails(self):
        """Verify fast-fail when Docker socket doesn't exist."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOCKER_HOST", None)
            with patch("tools.sandbox.Path.exists", return_value=False):
                assert _docker_available() is False

    @patch("tools.sandbox.subprocess.run")
    def test_docker_host_skips_socket_check(self, mock_run):
        """DOCKER_HOST set (remote daemon) bypasses socket fast-fail."""
        mock_run.return_value = MagicMock(returncode=0)
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://remote:2375"}):
            with patch("tools.sandbox.Path.exists", return_value=False):
                assert _docker_available() is True
        # Should have reached subprocess.run despite missing socket
        assert mock_run.call_count == 2


class TestBuildDockerCmd:
    """Test _build_docker_cmd() produces correct command structure."""

    def test_python_command(self):
        cmd = _build_docker_cmd(
            container_name="test-123",
            working_dir=Path("/Users/test/workspace/outputs"),
            script_path_in_container="/Users/test/workspace/outputs/script.py",
            language="python",
        )
        assert cmd[0] == "docker"
        assert cmd[1] == "run"
        assert "--name" in cmd
        assert "test-123" in cmd
        assert "--rm" in cmd
        assert "python3" in cmd
        assert "-u" in cmd
        assert "/Users/test/workspace/outputs/script.py" in cmd

    def test_javascript_command(self):
        cmd = _build_docker_cmd(
            container_name="test-456",
            working_dir=Path("/tmp/test"),
            script_path_in_container="/tmp/test/script.js",
            language="javascript",
        )
        assert "node" in cmd
        assert "/tmp/test/script.js" in cmd

    def test_bash_command(self):
        cmd = _build_docker_cmd(
            container_name="test-789",
            working_dir=Path("/tmp/test"),
            script_path_in_container="/tmp/test/script.sh",
            language="bash",
        )
        assert "bash" in cmd
        assert "-e" in cmd

    def test_memory_limit_included(self):
        cmd = _build_docker_cmd(
            container_name="test",
            working_dir=Path("/tmp/test"),
            script_path_in_container="/tmp/test/s.py",
            language="python",
        )
        assert "--memory" in cmd
        mem_idx = cmd.index("--memory")
        assert cmd[mem_idx + 1] == config.DOCKER_MEMORY_LIMIT

    def test_network_mode_included(self):
        cmd = _build_docker_cmd(
            container_name="test",
            working_dir=Path("/tmp/test"),
            script_path_in_container="/tmp/test/s.py",
            language="python",
        )
        assert "--network" in cmd
        net_idx = cmd.index("--network")
        assert cmd[net_idx + 1] == config.DOCKER_NETWORK

    def test_user_flag_included(self):
        cmd = _build_docker_cmd(
            container_name="test",
            working_dir=Path("/tmp/test"),
            script_path_in_container="/tmp/test/s.py",
            language="python",
        )
        assert "--user" in cmd
        user_idx = cmd.index("--user")
        expected = f"{os.getuid()}:{os.getgid()}"
        assert cmd[user_idx + 1] == expected

    def test_volume_mounts_present(self):
        wd = Path("/Users/test/workspace/outputs")
        cmd = _build_docker_cmd(
            container_name="test",
            working_dir=wd,
            script_path_in_container="/Users/test/workspace/outputs/s.py",
            language="python",
        )
        # Check volume flags
        v_indices = [i for i, x in enumerate(cmd) if x == "-v"]
        volumes = [cmd[i + 1] for i in v_indices]
        # Working dir mount
        assert f"{wd}:{wd}" in volumes
        # Uploads dir mount (read-only)
        assert f"{config.UPLOADS_DIR}:{config.UPLOADS_DIR}:ro" in volumes
        # Pip cache mount
        assert f"{config.DOCKER_PIP_CACHE}:/pip-cache" in volumes


class TestRunCodeRouting:
    """Test that run_code() routes to Docker or subprocess correctly."""

    def setup_method(self):
        _docker_status["available"] = False
        _docker_status["checked_at"] = 0.0

    @patch("tools.sandbox._docker_available", return_value=True)
    @patch("tools.sandbox._run_code_docker")
    def test_routes_to_docker_when_enabled(self, mock_docker, mock_avail):
        with patch.object(config, "DOCKER_ENABLED", True):
            mock_docker.return_value = ExecutionResult(success=True, stdout="hello")
            result = run_code("print('hello')", "python")
            mock_docker.assert_called_once()
            assert result.success

    @patch("tools.sandbox._docker_available", return_value=False)
    def test_falls_back_when_docker_unavailable(self, mock_avail):
        with patch.object(config, "DOCKER_ENABLED", True):
            # Will use subprocess path (existing behavior)
            result = run_code("print('hello')", "python")
            assert result.success

    @patch("tools.sandbox._docker_available", return_value=True)
    def test_uses_subprocess_when_docker_disabled(self, mock_avail):
        with patch.object(config, "DOCKER_ENABLED", False):
            result = run_code("print('hello')", "python")
            assert result.success
            # _docker_available should not even be called
            mock_avail.assert_not_called()


# ══════════════════════════════════════════════════════════════════
# TIER 2: Integration Tests (require Docker + image)
# ══════════════════════════════════════════════════════════════════


@requires_sandbox_image
class TestDockerExecutionIntegration:
    """Integration tests that run real code inside Docker containers."""

    def setup_method(self):
        _docker_status["available"] = False
        _docker_status["checked_at"] = 0.0

    def test_python_hello_world(self):
        with patch.object(config, "DOCKER_ENABLED", True):
            result = run_code("print('hello from docker')", "python")
            assert result.success
            assert "hello from docker" in result.stdout

    def test_javascript_execution(self):
        with patch.object(config, "DOCKER_ENABLED", True):
            result = run_code("console.log('js works')", "javascript")
            assert result.success
            assert "js works" in result.stdout

    def test_bash_execution(self):
        with patch.object(config, "DOCKER_ENABLED", True):
            result = run_code("echo 'bash works'", "bash")
            assert result.success
            assert "bash works" in result.stdout

    def test_file_creation_visible_on_host(self):
        with patch.object(config, "DOCKER_ENABLED", True):
            code = "with open('docker_test_output.txt', 'w') as f: f.write('created in docker')"
            result = run_code(code, "python", working_dir=config.OUTPUTS_DIR)
            assert result.success
            out_file = config.OUTPUTS_DIR / "docker_test_output.txt"
            assert out_file.exists()
            assert out_file.read_text() == "created in docker"
            out_file.unlink()  # cleanup

    def test_timeout_kills_container(self):
        with patch.object(config, "DOCKER_ENABLED", True):
            code = "import time; time.sleep(60)"
            result = run_code(code, "python", timeout=5)
            assert not result.success
            assert result.timed_out

    def test_preinstalled_packages_work(self):
        with patch.object(config, "DOCKER_ENABLED", True):
            code = "import pandas; import numpy; print(f'pandas={pandas.__version__}')"
            result = run_code(code, "python")
            assert result.success
            assert "pandas=" in result.stdout


# ══════════════════════════════════════════════════════════════════
# TIER 3: Security Tests (require Docker + image)
# ══════════════════════════════════════════════════════════════════


@requires_sandbox_image
class TestDockerSecurityIntegration:
    """Verify that Docker containers cannot access host filesystem."""

    def setup_method(self):
        _docker_status["available"] = False
        _docker_status["checked_at"] = 0.0

    def test_cannot_read_ssh_keys(self):
        with patch.object(config, "DOCKER_ENABLED", True):
            code = (
                "import os\n"
                "ssh_path = os.path.expanduser('~/.ssh/id_rsa')\n"
                "try:\n"
                "    with open(ssh_path) as f:\n"
                "        print(f'BREACH: {f.read()[:50]}')\n"
                "except (FileNotFoundError, PermissionError) as e:\n"
                "    print(f'BLOCKED: {e}')\n"
            )
            result = run_code(code, "python")
            assert "BREACH" not in result.stdout
            assert "BLOCKED" in result.stdout

    def test_cannot_read_env_file(self):
        env_path = config.BASE_DIR / ".env"
        with patch.object(config, "DOCKER_ENABLED", True):
            code = (
                "try:\n"
                f"    with open('{env_path}') as f:\n"
                "        print(f'BREACH: {{f.read()[:50]}}')\n"
                "except (FileNotFoundError, PermissionError) as e:\n"
                "    print(f'BLOCKED: {{e}}')\n"
            )
            result = run_code(code, "python")
            assert "BREACH" not in result.stdout

    def test_cannot_access_home_directory(self):
        with patch.object(config, "DOCKER_ENABLED", True):
            code = (
                "import os\n"
                f"try:\n"
                f"    contents = os.listdir('{config.HOST_HOME}')\n"
                f"    print(f'CONTENTS: {{contents[:10]}}')\n"
                f"except (FileNotFoundError, PermissionError) as e:\n"
                f"    print(f'BLOCKED: {{e}}')\n"
            )
            result = run_code(code, "python")
            # Host home sensitive dirs should not be accessible from container.
            # Note: Docker volume mounts create parent directory structure, so
            # "Desktop" may appear as a mount-path artifact — that's fine as long
            # as sensitive directories (.ssh, .gnupg, Documents, etc.) are absent.
            assert ".ssh" not in result.stdout
            assert ".gnupg" not in result.stdout
            assert "Documents" not in result.stdout
            assert ".env" not in result.stdout

    def test_uploads_dir_is_read_only(self):
        with patch.object(config, "DOCKER_ENABLED", True):
            evil_path = config.UPLOADS_DIR / "evil_test.txt"
            code = (
                "try:\n"
                f"    with open('{evil_path}', 'w') as f:\n"
                "        f.write('should fail')\n"
                "    print('BREACH: wrote to uploads')\n"
                "except (PermissionError, OSError) as e:\n"
                "    print(f'BLOCKED: {e}')\n"
            )
            result = run_code(code, "python")
            assert "BREACH" not in result.stdout
