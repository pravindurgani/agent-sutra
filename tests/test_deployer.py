"""Tests for tools/deployer.py — static deployment module.

Covers:
- Deploy disabled returns None
- GitHub Pages success path (mocked subprocess)
- Deploy failure degrades gracefully (returns None, no exception)
- Name sanitization
- Token not leaked into logs
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import config


class TestDeploy:
    """Top-level deploy() entry point tests."""

    def test_deploy_disabled_returns_none(self, tmp_path):
        """When DEPLOY_ENABLED is False, deploy() returns None immediately."""
        from tools.deployer import deploy

        with patch.object(config, "DEPLOY_ENABLED", False):
            result = deploy(tmp_path, "test-project", "frontend")
        assert result is None

    def test_deploy_nonexistent_dir_returns_none(self, tmp_path):
        """When output_dir doesn't exist, deploy() returns None."""
        from tools.deployer import deploy

        with patch.object(config, "DEPLOY_ENABLED", True):
            result = deploy(tmp_path / "nonexistent", "test", "frontend")
        assert result is None

    def test_deploy_failure_returns_none(self, tmp_path):
        """When deployment raises, deploy() catches and returns None."""
        from tools.deployer import deploy

        (tmp_path / "index.html").write_text("<h1>Hello</h1>")

        with (
            patch.object(config, "DEPLOY_ENABLED", True),
            patch.object(config, "DEPLOY_PROVIDER", "github_pages"),
            patch("tools.deployer._deploy_github_pages", side_effect=RuntimeError("git push failed")),
        ):
            result = deploy(tmp_path, "test", "frontend")
        assert result is None

    def test_deploy_timeout_returns_none(self, tmp_path):
        """When deployment times out, deploy() catches and returns None."""
        from tools.deployer import deploy

        (tmp_path / "index.html").write_text("<h1>Hello</h1>")

        with (
            patch.object(config, "DEPLOY_ENABLED", True),
            patch.object(config, "DEPLOY_PROVIDER", "github_pages"),
            patch("tools.deployer._deploy_github_pages", side_effect=subprocess.TimeoutExpired("git", 60)),
        ):
            result = deploy(tmp_path, "test", "frontend")
        assert result is None


class TestDeployGitHubPages:
    """GitHub Pages deployment path tests."""

    def test_github_pages_success(self, tmp_path):
        """Mocked GitHub Pages deploy returns correct URL."""
        from tools.deployer import _deploy_github_pages

        # Create fake artifacts in a separate directory
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        (artifacts / "index.html").write_text("<h1>Hello</h1>")

        # Set up a fake deploy repo dir (separate from artifacts)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        deploy_repo = workspace / "deploy_repo"
        deploy_repo.mkdir()
        (deploy_repo / ".git").mkdir()

        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))

        with (
            patch.object(config, "DEPLOY_REPO", "user/sites"),
            patch.object(config, "DEPLOY_GITHUB_TOKEN", "ghp_fake123"),
            patch.object(config, "DEPLOY_BASE_URL", "https://user.github.io/sites"),
            patch.object(config, "WORKSPACE_DIR", workspace),
            patch("tools.deployer.subprocess.run", mock_run),
            patch("tools.deployer._safe_env", return_value={}),
        ):
            url = _deploy_github_pages(artifacts, "my-project")

        assert url == "https://user.github.io/sites/my-project/"
        # Verify git commands were called
        git_commands = [call.args[0] for call in mock_run.call_args_list]
        assert ["git", "pull", "--ff-only"] in git_commands
        assert ["git", "add", "-A"] in git_commands
        assert ["git", "push"] in git_commands

    def test_github_pages_no_repo_raises(self, tmp_path):
        """Missing DEPLOY_REPO raises ValueError."""
        from tools.deployer import _deploy_github_pages

        with (
            patch.object(config, "DEPLOY_REPO", ""),
            patch.object(config, "DEPLOY_GITHUB_TOKEN", "ghp_fake"),
        ):
            with pytest.raises(ValueError, match="DEPLOY_REPO"):
                _deploy_github_pages(tmp_path, "test")

    def test_github_pages_no_token_raises(self, tmp_path):
        """Missing DEPLOY_GITHUB_TOKEN raises ValueError."""
        from tools.deployer import _deploy_github_pages

        with (
            patch.object(config, "DEPLOY_REPO", "user/sites"),
            patch.object(config, "DEPLOY_GITHUB_TOKEN", ""),
        ):
            with pytest.raises(ValueError, match="DEPLOY_GITHUB_TOKEN"):
                _deploy_github_pages(tmp_path, "test")


class TestDeployVercel:
    """Vercel deployment path tests."""

    def test_vercel_success(self, tmp_path):
        """Mocked Vercel deploy returns the URL from stdout."""
        from tools.deployer import _deploy_vercel

        mock_result = MagicMock(
            returncode=0,
            stdout="Deploying...\nhttps://my-project.vercel.app\n",
            stderr="",
        )

        with (
            patch.object(config, "DEPLOY_VERCEL_TOKEN", "tok_fake"),
            patch("tools.deployer.subprocess.run", return_value=mock_result),
            patch("tools.deployer._safe_env", return_value={}),
        ):
            url = _deploy_vercel(tmp_path, "my-project")

        assert url == "https://my-project.vercel.app"

    def test_vercel_no_token_raises(self, tmp_path):
        """Missing DEPLOY_VERCEL_TOKEN raises ValueError."""
        from tools.deployer import _deploy_vercel

        with patch.object(config, "DEPLOY_VERCEL_TOKEN", ""):
            with pytest.raises(ValueError, match="DEPLOY_VERCEL_TOKEN"):
                _deploy_vercel(tmp_path, "test")

    def test_vercel_failure_raises(self, tmp_path):
        """Vercel CLI failure raises RuntimeError."""
        from tools.deployer import _deploy_vercel

        mock_result = MagicMock(returncode=1, stdout="", stderr="Error: deploy failed")

        with (
            patch.object(config, "DEPLOY_VERCEL_TOKEN", "tok_fake"),
            patch("tools.deployer.subprocess.run", return_value=mock_result),
            patch("tools.deployer._safe_env", return_value={}),
        ):
            with pytest.raises(RuntimeError, match="Vercel deploy failed"):
                _deploy_vercel(tmp_path, "test")


class TestSanitizeName:
    """Name sanitization for URL-safe directory names."""

    def test_basic_sanitization(self):
        from tools.deployer import _sanitize_name
        assert _sanitize_name("Light & Wonder Report") == "light-wonder-report"

    def test_strips_special_chars(self):
        from tools.deployer import _sanitize_name
        assert _sanitize_name("My App (v2.0)!") == "my-app-v20"

    def test_collapses_multiple_hyphens(self):
        from tools.deployer import _sanitize_name
        assert _sanitize_name("some---thing") == "some-thing"

    def test_strips_leading_trailing_hyphens(self):
        from tools.deployer import _sanitize_name
        assert _sanitize_name("  -hello-  ") == "hello"

    def test_empty_string_returns_deploy(self):
        from tools.deployer import _sanitize_name
        assert _sanitize_name("!!!") == "deploy"

    def test_already_clean(self):
        from tools.deployer import _sanitize_name
        assert _sanitize_name("my-project") == "my-project"


class TestTokenSafety:
    """Verify deployment tokens are never logged."""

    def test_deploy_token_not_in_logs(self, tmp_path, caplog):
        """DEPLOY_GITHUB_TOKEN must not appear in any log output."""
        from tools.deployer import deploy

        fake_token = "ghp_SuperSecretToken12345"
        (tmp_path / "index.html").write_text("<h1>Test</h1>")

        with (
            caplog.at_level(logging.DEBUG),
            patch.object(config, "DEPLOY_ENABLED", True),
            patch.object(config, "DEPLOY_PROVIDER", "github_pages"),
            patch.object(config, "DEPLOY_GITHUB_TOKEN", fake_token),
            patch.object(config, "DEPLOY_REPO", "user/sites"),
            patch.object(config, "DEPLOY_BASE_URL", "https://user.github.io/sites"),
            patch.object(config, "WORKSPACE_DIR", tmp_path),
            patch("tools.deployer.subprocess.run", side_effect=RuntimeError("simulated")),
            patch("tools.deployer._safe_env", return_value={}),
        ):
            deploy(tmp_path, "test", "frontend")

        # Check that the token never appears in any log record
        for record in caplog.records:
            assert fake_token not in record.getMessage(), (
                f"Token leaked in log: {record.getMessage()}"
            )
