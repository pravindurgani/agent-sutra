"""Visual verification of generated web apps using Playwright.

Takes a URL, navigates to it with headless Chromium, checks for:
- Page loads (200 OK)
- No console errors
- Screenshot capture

Degrades gracefully — if Playwright isn't installed or the check fails,
returns a result indicating the check was skipped.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class VisualCheckResult:
    """Result of a visual verification check."""

    checked: bool = False  # False if skipped (Playwright missing, etc.)
    loads: bool = False
    status_code: int = 0
    console_errors: list[str] = field(default_factory=list)
    screenshot_path: Path | None = None
    error: str = ""


def check_page(url: str, screenshot_dir: Path, timeout: int = 15) -> VisualCheckResult:
    """Navigate to URL with headless Chromium and verify it works.

    Args:
        url: The URL to check (e.g., http://127.0.0.1:8100).
        screenshot_dir: Directory to save preview.png screenshot.
        timeout: Navigation timeout in seconds.

    Returns:
        VisualCheckResult. Never raises — always returns a result.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.info("Playwright not installed, skipping visual check")
        return VisualCheckResult(error="Playwright not installed")

    # A-11: Validate URL to prevent SSRF (file://, internal network, etc.)
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1"):
        logger.warning("Visual check blocked: URL %s is not localhost HTTP", url)
        return VisualCheckResult(error=f"URL must be http://localhost or http://127.0.0.1, got: {url[:100]}")

    result = VisualCheckResult(checked=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()

                # Capture console errors
                console_errors: list[str] = []
                page.on(
                    "console",
                    lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
                )

                # Navigate
                response = page.goto(url, timeout=timeout * 1000)
                result.status_code = response.status if response else 0
                result.loads = result.status_code == 200

                # Wait for content to render
                page.wait_for_load_state("networkidle", timeout=5000)

                # Screenshot
                screenshot_path = screenshot_dir / "preview.png"
                page.screenshot(path=str(screenshot_path), full_page=True)
                result.screenshot_path = screenshot_path

                result.console_errors = console_errors
            finally:
                browser.close()
    except Exception as e:
        result.error = str(e)[:200]
        logger.warning("Visual check failed: %s", e)

    return result
