"""Deployment module for publishing generated artifacts to live URLs.

Supports GitHub Pages and Vercel. Designed to be called by the executor
after successful frontend/ui_design task execution with audit pass.
Degrades gracefully — deployment failure never crashes the pipeline.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

import config

logger = logging.getLogger(__name__)


def deploy(output_dir: Path, project_name: str, task_type: str) -> str | None:
    """Deploy artifacts to a live URL.

    Args:
        output_dir: Directory containing the artifacts to deploy.
        project_name: Human-readable project name (sanitized internally).
        task_type: Task type (e.g., "frontend", "ui_design").

    Returns:
        Live URL string on success, None on failure or if disabled.
    """
    if not config.DEPLOY_ENABLED:
        logger.info("Deployment disabled (DEPLOY_ENABLED=false)")
        return None

    if not output_dir.exists():
        logger.warning("Deploy output dir does not exist: %s", output_dir)
        return None

    safe_name = _sanitize_name(project_name)

    try:
        if config.DEPLOY_PROVIDER == "vercel":
            return _deploy_vercel(output_dir, safe_name)
        else:
            return _deploy_github_pages(output_dir, safe_name)
    except subprocess.TimeoutExpired:
        logger.warning("Deployment timed out for %s", safe_name)
        return None
    except Exception as e:
        logger.warning("Deployment failed for %s: %s", safe_name, e)
        return None


def _deploy_github_pages(output_dir: Path, project_name: str) -> str:
    """Deploy to GitHub Pages via a dedicated deployment repo.

    Clones (or updates) the deploy repo, copies artifacts into a project
    subdirectory, commits, and pushes. The repo should have GitHub Pages
    enabled on the default branch.

    Returns:
        The live URL on GitHub Pages.
    """
    if not config.DEPLOY_REPO:
        raise ValueError("DEPLOY_REPO not configured")
    if not config.DEPLOY_GITHUB_TOKEN:
        raise ValueError("DEPLOY_GITHUB_TOKEN not configured")

    repo_dir = config.WORKSPACE_DIR / "deploy_repo"
    env = _safe_env()

    # Clone or update the deploy repo
    if not (repo_dir / ".git").exists():
        repo_url = f"https://x-access-token:{config.DEPLOY_GITHUB_TOKEN}@github.com/{config.DEPLOY_REPO}.git"
        logger.info("Cloning deploy repo to %s", repo_dir)
        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(repo_dir)],
            capture_output=True, text=True, timeout=60, env=env,
        )
    else:
        logger.info("Pulling latest deploy repo")
        subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=repo_dir, capture_output=True, text=True, timeout=30, env=env,
        )

    # Copy artifacts to project subdirectory
    project_dir = repo_dir / project_name
    if project_dir.exists():
        shutil.rmtree(project_dir)
    shutil.copytree(output_dir, project_dir, dirs_exist_ok=True)

    # Commit and push
    subprocess.run(
        ["git", "add", "-A"],
        cwd=repo_dir, capture_output=True, text=True, timeout=15, env=env,
    )

    commit_result = subprocess.run(
        ["git", "commit", "-m", f"Deploy {project_name}"],
        cwd=repo_dir, capture_output=True, text=True, timeout=15, env=env,
    )
    if commit_result.returncode != 0 and "nothing to commit" in (commit_result.stdout + commit_result.stderr):
        logger.info("No changes to deploy for %s", project_name)

    subprocess.run(
        ["git", "push"],
        cwd=repo_dir, capture_output=True, text=True, timeout=60, env=env,
    )

    url = f"{config.DEPLOY_BASE_URL}/{project_name}/"
    logger.info("Deployed %s to %s", project_name, url)
    return url


def _deploy_vercel(output_dir: Path, project_name: str) -> str:
    """Deploy to Vercel using the CLI.

    Requires the Vercel CLI to be installed (npm i -g vercel).

    Returns:
        The live URL from Vercel.
    """
    if not config.DEPLOY_VERCEL_TOKEN:
        raise ValueError("DEPLOY_VERCEL_TOKEN not configured")

    result = subprocess.run(
        [
            "vercel", "--token", config.DEPLOY_VERCEL_TOKEN,
            "--yes", "--name", project_name, str(output_dir),
        ],
        capture_output=True, text=True, timeout=120,
        env=_safe_env(),
    )

    if result.returncode != 0:
        raise RuntimeError(f"Vercel deploy failed: {result.stderr[:300]}")

    # Vercel prints the live URL on stdout
    url = result.stdout.strip().split("\n")[-1].strip()
    if not url.startswith("http"):
        raise RuntimeError(f"Could not parse Vercel URL from output: {result.stdout[:200]}")

    logger.info("Deployed %s to %s via Vercel", project_name, url)
    return url


def _sanitize_name(name: str) -> str:
    """Convert a project name to a URL-safe directory name.

    Args:
        name: Human-readable name (e.g., "Light & Wonder Report").

    Returns:
        URL-safe string (e.g., "light-wonder-report").
    """
    safe = name.lower().strip()
    safe = re.sub(r"[^a-z0-9\s-]", "", safe)
    safe = re.sub(r"[\s]+", "-", safe)
    safe = re.sub(r"-+", "-", safe)
    return safe.strip("-") or "deploy"


def _safe_env() -> dict[str, str]:
    """Build a safe environment dict for deployment subprocesses.

    Reuses the sandbox's credential-stripping logic.
    """
    from tools.sandbox import _filter_env
    return _filter_env()
