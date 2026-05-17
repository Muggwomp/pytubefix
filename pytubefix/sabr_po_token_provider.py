"""Provider helpers for SABR-scoped PoTokens.

The normal playback PoToken is enough for player responses, but browser SABR
requests use a separate token in the UMP request body. This module keeps that
capture path optional so importing pytubefix does not require Playwright.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pytubefix.sabr.video_streaming.video_playback_abr_request import VideoPlaybackAbrRequest


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SabrPoTokenResult:
    po_token: str
    source: str = "browser_sabr_capture"


class SabrPoTokenProvider:
    """Interface for SABR PoToken providers."""

    def fetch(self, *, url: str, video_id: str, client: str, visitor_data: str) -> SabrPoTokenResult:
        raise NotImplementedError


class BrowserSabrPoTokenProvider(SabrPoTokenProvider):
    """Capture a SABR body PoToken from a real Chromium browser session."""

    def __init__(
        self,
        *,
        browser_path: str | os.PathLike[str] | None = None,
        seconds: int = 15,
        headed: bool = False,
        max_captures: int = 8,
    ) -> None:
        browser_path_value = browser_path or os.environ.get("PYTUBEFIX_SABR_BROWSER_PATH")
        self.browser_path = Path(browser_path_value) if browser_path_value else None
        self.seconds = int(os.environ.get("PYTUBEFIX_SABR_CAPTURE_SECONDS", seconds))
        self.headed = headed or os.environ.get("PYTUBEFIX_SABR_CAPTURE_HEADED") == "1"
        self.max_captures = max_captures
        self.locale = os.environ.get("PYTUBEFIX_SABR_BROWSER_LOCALE", "en-US")
        self.timezone_id = os.environ.get("PYTUBEFIX_SABR_BROWSER_TIMEZONE")
        self.user_agent = os.environ.get("PYTUBEFIX_SABR_BROWSER_USER_AGENT")

    def fetch(self, *, url: str, video_id: str, client: str, visitor_data: str) -> SabrPoTokenResult:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError(
                "Playwright is required for BrowserSabrPoTokenProvider"
            ) from exc

        captured_tokens: list[bytes] = []

        logger.warning("SABR streams detected; launching Playwright browser to capture SABR PoToken.")
        with sync_playwright() as p:
            browser = None
            context = None
            launch_kwargs = {
                "headless": not self.headed,
                "args": [
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-blink-features=AutomationControlled",
                    "--mute-audio",
                ],
            }
            if self.browser_path:
                launch_kwargs["executable_path"] = str(self.browser_path)

            try:
                browser = p.chromium.launch(**launch_kwargs)
                context_kwargs = {
                    "viewport": {"width": 1280, "height": 720},
                    "locale": self.locale,
                }
                if self.timezone_id:
                    context_kwargs["timezone_id"] = self.timezone_id
                if self.user_agent:
                    context_kwargs["user_agent"] = self.user_agent

                context = browser.new_context(**context_kwargs)
                page = context.new_page()

                def on_request(request):
                    if len(captured_tokens) >= self.max_captures:
                        return
                    if request.method != "POST" or "googlevideo.com" not in request.url:
                        return
                    body = request.post_data_buffer or b""
                    if not body:
                        return
                    try:
                        decoded = VideoPlaybackAbrRequest.decode(body)
                        po_token = decoded.streamer_context.poToken
                    except Exception:
                        return
                    if po_token:
                        captured_tokens.append(bytes(po_token))

                page.on("request", on_request)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.keyboard.press("k")
                    page.locator("video").evaluate("video => { video.muted = true; video.play(); }")
                except Exception:
                    pass

                deadline = time.time() + self.seconds
                while time.time() < deadline and not self._has_full_sabr_token(captured_tokens):
                    page.wait_for_timeout(500)
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        logger.debug("Failed to close SABR Playwright context", exc_info=True)
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        logger.debug("Failed to close SABR Playwright browser", exc_info=True)

        token = self._select_token(captured_tokens)
        if not token:
            raise RuntimeError("Unable to capture a browser SABR PoToken")

        return SabrPoTokenResult(
            po_token=base64.urlsafe_b64encode(token).decode("ascii").rstrip("="),
            source="browser_sabr_capture",
        )

    @staticmethod
    def _has_full_sabr_token(tokens: list[bytes]) -> bool:
        return any(len(token) >= 64 for token in tokens)

    @staticmethod
    def _select_token(tokens: list[bytes]) -> Optional[bytes]:
        full_tokens = [token for token in tokens if len(token) >= 64]
        if full_tokens:
            return full_tokens[-1]
        return tokens[-1] if tokens else None
