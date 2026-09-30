"""Authenticated Playwright session helpers for browser-backed YouTube access.

The regular pytubefix HTTP clients do not have access to a browser's login and
age-verification cookies.  This module provides an opt-in persistent Chromium
profile and captures the player response from that browser session when the
HTTP player response is login-gated.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import threading
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote, urlsplit

from pytubefix.exceptions import SABRError


logger = logging.getLogger(__name__)

# Chromium holds a lock on a persistent profile.  Serialize all uses in this
# process so playlist workers do not try to open the same profile concurrently.
PERSISTENT_PROFILE_LOCK = threading.Lock()


def browser_profile_dir(youtube=None, explicit: Optional[str] = None) -> Optional[str]:
    """Return the configured persistent profile directory, if any."""
    value = explicit
    if value is None and youtube is not None:
        value = getattr(youtube, "sabr_browser_profile_dir", None)
    if value is None:
        value = os.environ.get("PYTUBEFIX_SABR_BROWSER_PROFILE_DIR")
    if not value:
        return None
    return os.path.abspath(os.path.expanduser(os.path.expandvars(value)))


def browser_mode(mode: Optional[str] = None) -> str:
    """Resolve the Playwright browser mode used by the session."""
    value = (mode or os.environ.get("PYTUBEFIX_SABR_BROWSER_MODE", "auto")).lower()
    if value not in {"auto", "headless", "headed", "hidden-headed"}:
        raise SABRError(
            "PYTUBEFIX_SABR_BROWSER_MODE must be auto, headless, headed, or hidden-headed"
        )
    if value == "auto":
        if platform.system() == "Windows":
            return "hidden-headed"
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            return "headed"
        return "headless"
    return value


def _playwright_proxy(proxies: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    if not proxies:
        return None
    proxy_url = proxies.get("https") or proxies.get("http")
    if not proxy_url:
        raise SABRError(
            "Browser session cannot use the supplied proxy mapping; "
            "an http or https proxy URL is required."
        )
    parsed = urlsplit(proxy_url)
    if not parsed.scheme or not parsed.hostname:
        raise SABRError("Browser session proxy must be a complete URL")
    result = {"server": f"{parsed.scheme}://{parsed.hostname}"}
    if parsed.port:
        result["server"] += f":{parsed.port}"
    if parsed.username:
        result["username"] = unquote(parsed.username)
    if parsed.password:
        result["password"] = unquote(parsed.password)
    return result


def launch_options(youtube=None, mode: Optional[str] = None) -> Dict[str, Any]:
    """Build common Chromium launch options for all browser-backed paths."""
    resolved_mode = browser_mode(mode)
    launch_args = [
        "--autoplay-policy=no-user-gesture-required",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=AutomationControlled",
        "--disable-infobars",
        "--mute-audio",
    ]
    if resolved_mode == "hidden-headed":
        launch_args.extend([
            "--window-size=1280,720",
            "--window-position=-32000,-32000",
        ])

    options: Dict[str, Any] = {
        "headless": resolved_mode == "headless",
        "args": launch_args,
        "timeout": _env_int("PYTUBEFIX_SABR_BROWSER_LAUNCH_TIMEOUT_MS", 60000),
    }
    channel = os.environ.get("PYTUBEFIX_SABR_BROWSER_CHANNEL", "").strip()
    if channel:
        options["channel"] = channel
    executable = os.environ.get("PYTUBEFIX_SABR_BROWSER_PATH", "").strip()
    if executable:
        options["executable_path"] = executable
    proxy = _playwright_proxy(getattr(youtube, "proxies", None))
    if proxy:
        options["proxy"] = proxy
    return options


def open_persistent_context(
    playwright,
    profile_dir: str,
    youtube=None,
    mode: Optional[str] = None,
) -> Tuple[Any, threading.Lock]:
    """Open a locked persistent Chromium context."""
    resolved_dir = browser_profile_dir(explicit=profile_dir)
    if not resolved_dir:
        raise SABRError("A persistent browser profile directory is required")
    os.makedirs(resolved_dir, exist_ok=True)

    PERSISTENT_PROFILE_LOCK.acquire()
    try:
        context = playwright.chromium.launch_persistent_context(
            resolved_dir,
            **launch_options(youtube, mode),
        )
    except Exception:
        PERSISTENT_PROFILE_LOCK.release()
        raise
    return context, PERSISTENT_PROFILE_LOCK


def close_persistent_context(context, lock: threading.Lock) -> None:
    """Close a persistent context and always release its process lock."""
    try:
        context.close()
    finally:
        lock.release()


class BrowserPlayerSession:
    """Capture a playable player response from an authenticated browser."""

    def __init__(self, youtube):
        self.youtube = youtube

    def fetch_player_info(self) -> Dict[str, Any]:
        profile_dir = browser_profile_dir(self.youtube)
        if not profile_dir:
            raise SABRError(
                "Authenticated browser playback requires a persistent profile directory"
            )

        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise SABRError(
                "Authenticated browser playback requires Playwright. Install it with "
                "'pip install playwright' and 'playwright install chromium'."
            ) from exc

        responses = []
        with sync_playwright() as playwright:
            context, lock = open_persistent_context(
                playwright,
                profile_dir,
                youtube=self.youtube,
            )
            try:
                context.add_init_script(
                    """(() => {
                        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                        window.chrome = window.chrome || {runtime: {}};
                    })();"""
                )
                page = context.new_page()
                page.set_default_timeout(
                    _env_int("PYTUBEFIX_SABR_BROWSER_ACTION_TIMEOUT_MS", 15000)
                )

                def on_response(response) -> None:
                    if "/youtubei/v1/player" not in response.url:
                        return
                    try:
                        data = json.loads(response.body().decode("utf-8"))
                    except Exception:
                        return
                    if not isinstance(data, dict):
                        return
                    response_id = data.get("videoDetails", {}).get("videoId")
                    if response_id and response_id != self.youtube.video_id:
                        return
                    responses.append(data)

                page.on("response", on_response)
                navigation_error = None
                try:
                    page.goto(
                        self.youtube.watch_url,
                        wait_until="domcontentloaded",
                        timeout=_env_int(
                            "PYTUBEFIX_SABR_BROWSER_NAVIGATION_TIMEOUT_MS", 60000
                        ),
                    )
                except Exception as exc:
                    # The player response may still have arrived before a page
                    # navigation timeout, so inspect captured responses first.
                    navigation_error = exc

                try:
                    page.wait_for_selector(
                        "video",
                        timeout=min(
                            _env_int("PYTUBEFIX_SABR_BROWSER_VIDEO_TIMEOUT_MS", 30000),
                            5000,
                        ),
                    )
                    page.locator("video").evaluate("video => video.play()")
                except Exception:
                    # Player responses are normally requested during navigation;
                    # playback is only a best-effort nudge for lazy-loaded pages.
                    pass

                deadline = time.time() + _env_int(
                    "PYTUBEFIX_SABR_BROWSER_PLAYER_TIMEOUT", 45
                )
                while time.time() < deadline:
                    playable = [
                        response
                        for response in responses
                        if response.get("playabilityStatus", {}).get("status") == "OK"
                        and response.get("streamingData")
                    ]
                    if playable:
                        logger.info(
                            "Authenticated browser returned player info for %s",
                            self.youtube.video_id,
                        )
                        return playable[-1]
                    page.wait_for_timeout(250)

                status = "unknown"
                reason = "unknown"
                if responses:
                    player_status = responses[-1].get("playabilityStatus", {})
                    status = player_status.get("status", status)
                    reason = player_status.get("reason", reason)
                detail = f"status={status}; reason={reason}"
                if navigation_error is not None:
                    detail += f"; navigation={navigation_error}"
                raise SABRError(
                    "Authenticated browser did not return playable streams for "
                    f"{self.youtube.video_id} ({detail}). Confirm that the profile is "
                    "logged in and can play this video in YouTube."
                )
            finally:
                close_persistent_context(context, lock)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
