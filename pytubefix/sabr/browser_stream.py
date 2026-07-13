"""Optional browser-owned SABR download backend.

YouTube's browser player owns the evolving SABR request state while pytubefix
extracts the selected representation from UMP responses or exact-itag
SourceBuffer appends. Playwright is imported lazily so normal pytubefix users
do not gain a hard dependency.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import platform
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urlsplit

from pytubefix.exceptions import SABRError
from pytubefix.sabr.core.UMP import UMP
from pytubefix.sabr.core.chunked_data_buffer import ChunkedDataBuffer
from pytubefix.sabr.core.server_abr_stream import PART
from pytubefix.sabr.video_streaming.format_initialization_metadata import (
    FormatInitializationMetadata,
)
from pytubefix.sabr.video_streaming.media_header import MediaHeader


logger = logging.getLogger(__name__)


@dataclass
class BrowserCapture:
    itag: int
    last_modified: int = 0
    xtags: str = ""
    chunks: Dict[int, List[bytes]] = field(default_factory=dict)
    expected_lengths: Dict[int, int] = field(default_factory=dict)
    hashes: Dict[int, Set[int]] = field(default_factory=dict)
    ended_sequences: Set[int] = field(default_factory=set)
    end_segment_number: Optional[int] = None
    duration_ms: Optional[int] = None
    mime_type: Optional[str] = None
    next_sequence: int = 0
    total_bytes: int = 0
    spool: Any = field(
        default_factory=lambda: tempfile.SpooledTemporaryFile(
            max_size=64 * 1024 * 1024,
            mode="w+b",
        ),
        repr=False,
    )

    def add_metadata(self, metadata: FormatInitializationMetadata) -> None:
        if metadata.endSegmentNumber is not None:
            self.end_segment_number = int(metadata.endSegmentNumber)
        if metadata.durationMs:
            self.duration_ms = int(metadata.durationMs)
        if metadata.mimeType:
            self.mime_type = metadata.mimeType
        self._flush_ready()

    def add_chunk(self, header: MediaHeader, chunk: bytes) -> bool:
        sequence = int(header.sequenceNumber or 0)
        expected = int(header.contentLength or 0)
        if expected:
            self.expected_lengths[sequence] = expected

        chunk_hash = hash(chunk)
        hashes = self.hashes.setdefault(sequence, set())
        if chunk_hash in hashes:
            return False

        current = sum(len(part) for part in self.chunks.get(sequence, ()))
        if expected and current >= expected:
            return False
        if expected and current + len(chunk) > expected:
            chunk = chunk[:expected - current]
        if not chunk:
            return False

        self.chunks.setdefault(sequence, []).append(chunk)
        hashes.add(chunk_hash)
        self._flush_ready()
        return True

    def mark_ended(self, sequence: int) -> None:
        self.ended_sequences.add(sequence)
        self._flush_ready()

    def _sequence_complete(self, sequence: int) -> bool:
        parts = self.chunks.get(sequence)
        if not parts:
            return False
        expected = self.expected_lengths.get(sequence)
        if expected:
            return sum(len(part) for part in parts) >= expected
        return sequence in self.ended_sequences

    def _flush_ready(self) -> None:
        while self._sequence_complete(self.next_sequence):
            for chunk in self.chunks.pop(self.next_sequence):
                self.spool.write(chunk)
                self.total_bytes += len(chunk)
            self.hashes.pop(self.next_sequence, None)
            self.ended_sequences.discard(self.next_sequence)
            self.next_sequence += 1

    def complete(self) -> bool:
        if self.end_segment_number is None:
            return False
        return self.next_sequence > self.end_segment_number

    def completion(self) -> float:
        if self.end_segment_number is None:
            return 0.0
        expected = self.end_segment_number + 1
        complete_segments = self.next_sequence + sum(
            1
            for sequence in range(self.next_sequence, expected)
            if self._sequence_complete(sequence)
        )
        return complete_segments / expected

    def segment_count(self) -> int:
        return self.next_sequence + len(self.chunks)

    def iter_chunks(self):
        if not self.complete():
            raise SABRError("Browser SABR capture is incomplete")
        self.spool.seek(0)
        while True:
            chunk = self.spool.read(1024 * 1024)
            if not chunk:
                break
            yield chunk

    def close(self) -> None:
        self.spool.close()


@dataclass
class SourceBufferCapture:
    """Ordered media bytes appended by the browser for one active itag."""

    itag: int
    stream_type: str
    total_bytes: int = 0
    append_count: int = 0
    finished: bool = False
    last_append_at: float = field(default_factory=time.time)
    mime_type: Optional[str] = None
    spool: Any = field(
        default_factory=lambda: tempfile.SpooledTemporaryFile(
            max_size=64 * 1024 * 1024,
            mode="w+b",
        ),
        repr=False,
    )

    def add(self, payload: Dict[str, Any]) -> bool:
        active_itag = payload.get("fmt") if self.stream_type == "video" else payload.get("afmt")
        if str(active_itag or "") != str(self.itag):
            return False
        mime_type = str(payload.get("mime") or "").lower()
        if not mime_type.startswith(f"{self.stream_type}/"):
            return False
        encoded = payload.get("body")
        if not encoded:
            return False
        try:
            chunk = base64.b64decode(encoded)
        except (TypeError, ValueError, binascii.Error):
            return False
        if not chunk:
            return False
        self.mime_type = mime_type
        self.spool.write(chunk)
        self.total_bytes += len(chunk)
        self.append_count += 1
        self.last_append_at = time.time()
        return True

    def complete(self) -> bool:
        return self.finished and self.total_bytes > 0

    def iter_chunks(self):
        if not self.complete():
            raise SABRError("Browser SourceBuffer capture is incomplete")
        self.spool.seek(0)
        while True:
            chunk = self.spool.read(1024 * 1024)
            if not chunk:
                break
            yield chunk

    def close(self) -> None:
        self.spool.close()


class BrowserSabrStream:
    """Download one exact SABR representation through YouTube's browser player."""

    def __init__(self, stream, write_chunk: Callable[[bytes, int], None], monostate) -> None:
        self.stream = stream
        self.write_chunk = write_chunk
        self.youtube = monostate.youtube
        self.headers_by_id: Dict[int, MediaHeader] = {}
        self.capture = BrowserCapture(
            itag=int(stream.itag),
            last_modified=int(stream.last_Modified or 0),
            xtags=stream.xtags or "",
        )
        self.source_capture = SourceBufferCapture(
            itag=int(stream.itag),
            stream_type=stream.type,
        )
        self.source_buffer_mode = self._use_source_buffer_capture()
        self.source_player_state: Dict[str, Any] = {}
        self.source_target_interrupted = False
        self.observed_itags: Set[int] = set()
        self.last_media_at = time.time()
        self.player_error = ""

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise SABRError(
                "Browser SABR fallback requires Playwright. Install it with "
                "'pip install playwright' and 'playwright install chromium'."
            ) from exc

        try:
            logger.warning(
                "SABR browser fallback enabled; using YouTube's browser player to download itag %s.",
                self.stream.itag,
            )
            with sync_playwright() as playwright:
                browser = None
                context = None
                try:
                    browser, context = self._create_context(playwright)
                    self._capture(context)
                finally:
                    if context is not None:
                        context.close()
                    if browser is not None:
                        browser.close()

            output_capture = self.source_capture if self.source_buffer_mode else self.capture
            if not output_capture.complete():
                observed = ", ".join(str(itag) for itag in sorted(self.observed_itags)) or "none"
                if self.source_buffer_mode:
                    detail = (
                        f"captured bytes: {self.source_capture.total_bytes}; "
                        f"append calls: {self.source_capture.append_count}; "
                        f"target interrupted: {self.source_target_interrupted}; "
                        f"player state: {self.source_player_state}"
                    )
                else:
                    detail = (
                        f"completion: {self.capture.completion() * 100:.1f}%; "
                        f"first missing segment: {self.capture.next_sequence}"
                    )
                raise SABRError(
                    "Browser SABR fallback did not receive a complete exact itag "
                    f"{self.stream.itag}. {detail}; observed itags: {observed}; "
                    f"player error: {self.player_error or 'none'}"
                )

            total_bytes = output_capture.total_bytes
            self.stream._filesize = total_bytes
            bytes_remaining = total_bytes
            for chunk in output_capture.iter_chunks():
                bytes_remaining -= len(chunk)
                self.write_chunk(chunk, max(0, bytes_remaining))
        finally:
            self.capture.close()
            self.source_capture.close()

    def parse_response(self, body: bytes) -> int:
        accepted = 0
        ump = UMP(ChunkedDataBuffer([body]))

        def callback(part):
            nonlocal accepted
            data = part["data"]
            raw = list(data.chunks[0] if data.chunks else [])

            if part["type"] == PART.FORMAT_INITIALIZATION_METADATA.value:
                metadata = FormatInitializationMetadata.decode(raw)
                format_id = metadata.formatId or {}
                itag = int(format_id.get("itag") or 0)
                if itag:
                    self.observed_itags.add(itag)
                if self._matches_target(format_id):
                    self.capture.add_metadata(metadata)
                return

            if part["type"] == PART.MEDIA_HEADER.value:
                header = MediaHeader.decode(raw)
                self.headers_by_id[int(header.headerId)] = header
                format_id = header.formatId or {
                    "itag": header.itag,
                    "lastModified": header.lmt,
                    "xtags": header.xtags,
                }
                itag = int(format_id.get("itag") or 0)
                if itag:
                    self.observed_itags.add(itag)
                return

            if part["type"] == PART.MEDIA.value:
                header_id = data.get_uint8(0)
                header = self.headers_by_id.get(int(header_id))
                if header is None:
                    return
                format_id = header.formatId or {
                    "itag": header.itag,
                    "lastModified": header.lmt,
                    "xtags": header.xtags,
                }
                if not self._matches_target(format_id):
                    return
                chunks = data.split(1)["remaining_buffer"].chunks
                if not chunks:
                    return
                if self.capture.add_chunk(header, bytes(chunks[0])):
                    accepted += 1
                    self.last_media_at = time.time()
                return

            if part["type"] == PART.MEDIA_END.value:
                header_id = data.get_uint8(0)
                header = self.headers_by_id.pop(int(header_id), None)
                if header is not None:
                    format_id = header.formatId or {
                        "itag": header.itag,
                        "lastModified": header.lmt,
                        "xtags": header.xtags,
                    }
                    if self._matches_target(format_id):
                        self.capture.mark_ended(int(header.sequenceNumber or 0))

        ump.parse(callback)
        return accepted

    def _matches_target(self, format_id: Dict[str, Any]) -> bool:
        if int(format_id.get("itag") or 0) != self.capture.itag:
            return False
        last_modified = int(format_id.get("lastModified") or 0)
        if self.capture.last_modified and last_modified:
            return last_modified == self.capture.last_modified
        return True

    def _use_source_buffer_capture(self) -> bool:
        override = os.environ.get("PYTUBEFIX_SABR_BROWSER_CAPTURE", "auto").lower()
        if override in {"sourcebuffer", "source-buffer"}:
            return True
        if override == "ump":
            return False
        codec = (self.stream.video_codec or self.stream.audio_codec or "").lower()
        return codec.startswith(("vp9", "vp09", "av1", "av01")) or "opus" in codec

    def _create_context(self, playwright) -> Tuple[Any, Any]:
        mode = os.environ.get("PYTUBEFIX_SABR_BROWSER_MODE", "auto").lower()
        if mode not in {"auto", "headless", "headed", "hidden-headed"}:
            raise SABRError(
                "PYTUBEFIX_SABR_BROWSER_MODE must be auto, headless, headed, or hidden-headed"
            )
        if mode == "auto":
            if platform.system() == "Windows":
                mode = "hidden-headed"
            elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
                mode = "headed"
            else:
                mode = "headless"

        launch_args = [
            "--autoplay-policy=no-user-gesture-required",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=AutomationControlled",
            "--disable-infobars",
            "--mute-audio",
        ]
        headless = mode == "headless"
        if mode == "hidden-headed":
            launch_args.extend([
                "--window-size=1280,720",
                "--window-position=-32000,-32000",
            ])

        launch_options: Dict[str, Any] = {
            "headless": headless,
            "args": launch_args,
            "timeout": self._env_int("PYTUBEFIX_SABR_BROWSER_LAUNCH_TIMEOUT_MS", 60000),
        }
        channel = os.environ.get("PYTUBEFIX_SABR_BROWSER_CHANNEL", "").strip()
        if channel:
            launch_options["channel"] = channel
        executable = os.environ.get("PYTUBEFIX_SABR_BROWSER_PATH", "").strip()
        if executable:
            launch_options["executable_path"] = executable
        proxy = self._playwright_proxy(getattr(self.youtube, "proxies", None))
        if proxy:
            launch_options["proxy"] = proxy

        browser = playwright.chromium.launch(**launch_options)
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            screen={"width": 1280, "height": 720},
            locale=os.environ.get("PYTUBEFIX_SABR_BROWSER_LOCALE", "en-US"),
            color_scheme="light",
        )
        context.add_init_script(
            """(() => {
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                window.chrome = window.chrome || {runtime: {}};
            })();"""
        )
        self._install_codec_preference(context)
        disabled_codecs = self._disabled_codecs()
        if disabled_codecs:
            self._install_codec_mask(context, disabled_codecs)
        return browser, context

    def _install_codec_preference(self, context) -> None:
        if not self.source_buffer_mode or self.stream.type != "video":
            return
        codec = (self.stream.video_codec or "").lower()
        if codec.startswith(("vp9", "vp09")):
            av1_preference = "480"
        elif codec.startswith(("av1", "av01")):
            av1_preference = "8192"
        else:
            return
        context.add_init_script(
            """(() => {
                const value = %s;
                try {
                    localStorage.setItem('yt-player-av1-pref', value);
                    sessionStorage.setItem('yt-player-av1-pref', value);
                } catch (_) {}
            })();""" % json.dumps(av1_preference)
        )

    @staticmethod
    def _playwright_proxy(proxies: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
        if not proxies:
            return None
        proxy_url = proxies.get("https") or proxies.get("http")
        if not proxy_url:
            raise SABRError(
                "Browser SABR fallback cannot use the supplied proxy mapping; "
                "an http or https proxy URL is required."
            )
        parsed = urlsplit(proxy_url)
        if not parsed.scheme or not parsed.hostname:
            raise SABRError(
                "Browser SABR fallback requires a complete proxy URL such as "
                "http://host:port."
            )
        server = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            server += f":{parsed.port}"
        result = {"server": server}
        if parsed.username is not None:
            result["username"] = unquote(parsed.username)
        if parsed.password is not None:
            result["password"] = unquote(parsed.password)
        return result

    def _capture(self, context) -> None:
        if self.source_buffer_mode:
            logger.info("Installing SABR SourceBuffer capture hook")
            self._install_source_buffer_capture(context)
        logger.info("Creating SABR browser page")
        page = context.new_page()
        page.set_default_timeout(self._env_int("PYTUBEFIX_SABR_BROWSER_ACTION_TIMEOUT_MS", 15000))

        def on_request_finished(request) -> None:
            if request.method != "POST" or "googlevideo.com" not in request.url:
                return
            response = request.response()
            if response is None:
                return
            try:
                self.parse_response(response.body())
            except Exception:
                logger.debug("Unable to parse browser SABR response", exc_info=True)

        if not self.source_buffer_mode:
            page.on("requestfinished", on_request_finished)
        logger.info("Navigating SABR browser to %s", self.youtube.watch_url)
        page.goto(
            self.youtube.watch_url,
            wait_until="domcontentloaded",
            timeout=self._env_int("PYTUBEFIX_SABR_BROWSER_NAVIGATION_TIMEOUT_MS", 60000),
        )
        logger.info("SABR browser navigation ready; waiting for video element")
        page.wait_for_selector(
            "video",
            timeout=self._env_int("PYTUBEFIX_SABR_BROWSER_VIDEO_TIMEOUT_MS", 30000),
        )
        logger.info("SABR browser video element ready")
        self._move_window_offscreen(page)
        self._wait_for_main_video(page)
        logger.info("Starting SABR browser playback")
        self._start_playback(page)
        logger.info("SABR browser playback started")

        timeout = self._env_int("PYTUBEFIX_SABR_BROWSER_CAPTURE_TIMEOUT", 240)
        deadline = time.time() + timeout
        if self.source_buffer_mode:
            self._capture_source_buffer(page, deadline)
            return

        last_progress_at = 0.0
        while time.time() < deadline and not self.capture.complete():
            page.wait_for_timeout(500)
            self._keep_playing(page)
            if time.time() - self.last_media_at > 5:
                self._seek_to_missing_segment(page)
            if time.time() - last_progress_at >= 5:
                logger.info(
                    "SABR browser download itag %s: %.1f%% (%s/%s segments)",
                    self.stream.itag,
                    self.capture.completion() * 100,
                    self.capture.segment_count(),
                    (self.capture.end_segment_number + 1)
                    if self.capture.end_segment_number is not None else "?",
                )
                last_progress_at = time.time()

        if not self.capture.complete():
            self.player_error = self._player_error(page)
            if self.player_error:
                logger.warning("YouTube browser player error: %s", self.player_error)

    def _install_source_buffer_capture(self, context) -> None:
        target_itag = json.dumps(str(self.stream.itag))
        stream_type = json.dumps(self.stream.type)
        context.add_init_script(
            """(() => {
                const targetItag = %s;
                const streamType = %s;
                const mimes = new WeakMap();
                const pending = [];
                window.__pytubefixSabrPending = pending;
                window.__pytubefixSabrDrain = maxBytes => {
                    const selected = [];
                    let total = 0;
                    while (pending.length) {
                        const next = pending[0];
                        if (selected.length && total + next.bytes.byteLength > maxBytes) break;
                        pending.shift();
                        total += next.bytes.byteLength;
                        let binary = '';
                        const blockSize = 0x8000;
                        for (let offset = 0; offset < next.bytes.length; offset += blockSize) {
                            binary += String.fromCharCode(
                                ...next.bytes.subarray(offset, offset + blockSize)
                            );
                        }
                        selected.push({
                            mime: next.mime,
                            fmt: next.fmt,
                            afmt: next.afmt,
                            body: btoa(binary),
                        });
                    }
                    return selected;
                };
                const addSourceBuffer = MediaSource.prototype.addSourceBuffer;
                MediaSource.prototype.addSourceBuffer = function(mime) {
                    const buffer = addSourceBuffer.call(this, mime);
                    mimes.set(buffer, mime);
                    return buffer;
                };

                const appendBuffer = SourceBuffer.prototype.appendBuffer;
                SourceBuffer.prototype.appendBuffer = function(data) {
                    try {
                        const mime = mimes.get(this) || '';
                        const bytes = data instanceof ArrayBuffer
                            ? new Uint8Array(data)
                            : new Uint8Array(data.buffer, data.byteOffset || 0, data.byteLength);
                        const player = document.getElementById('movie_player');
                        const stats = player?.getVideoStats?.() || {};
                        const activeItag = streamType === 'video' ? stats.fmt : stats.afmt;
                        if (String(activeItag || '') === targetItag
                                && mime.toLowerCase().startsWith(streamType + '/')) {
                            pending.push({
                                mime,
                                fmt: String(stats.fmt || ''),
                                afmt: String(stats.afmt || ''),
                                bytes: bytes.slice(),
                            });
                        }
                    } catch (_) {}
                    return appendBuffer.call(this, data);
                };
            })();""" % (target_itag, stream_type)
        )

    def _drain_source_buffers(self, page) -> None:
        try:
            payloads = page.evaluate(
                """() => window.__pytubefixSabrDrain?.(4 * 1024 * 1024) || []"""
            )
        except Exception:
            logger.debug("Unable to drain browser SourceBuffer capture", exc_info=True)
            return
        for payload in payloads:
            for value in (payload.get("fmt"), payload.get("afmt")):
                try:
                    if value:
                        self.observed_itags.add(int(value))
                except (TypeError, ValueError):
                    pass
            self.source_capture.add(payload)

    def _capture_source_buffer(self, page, deadline: float) -> None:
        last_progress_at = 0.0
        target_seen = False
        while time.time() < deadline and not self.source_capture.finished:
            page.wait_for_timeout(250)
            self._drain_source_buffers(page)
            self._keep_playing(page)
            try:
                state = page.evaluate(
                    """() => {
                        const video = document.querySelector('video');
                        const stats = document.getElementById('movie_player')?.getVideoStats?.() || {};
                        return {
                            currentTime: Number(video?.currentTime || 0),
                            duration: Number(video?.duration || 0),
                            ended: Boolean(video?.ended),
                            paused: Boolean(video?.paused),
                            fmt: String(stats.fmt || ''),
                            afmt: String(stats.afmt || ''),
                        };
                    }"""
                )
            except Exception:
                state = {}
            self.source_player_state = state

            active_itag = state.get("fmt") if self.stream.type == "video" else state.get("afmt")
            if str(active_itag or "") == str(self.stream.itag):
                target_seen = True
            elif target_seen and active_itag:
                self.source_target_interrupted = True
            for key in ("fmt", "afmt"):
                try:
                    if state.get(key):
                        self.observed_itags.add(int(state[key]))
                except (TypeError, ValueError):
                    pass

            current_time = float(state.get("currentTime") or 0)
            duration = float(state.get("duration") or 0)
            at_end = bool(state.get("ended")) or (
                duration > 0 and current_time >= max(0, duration - 0.35)
            )
            append_idle = time.time() - self.source_capture.last_append_at >= 1.0
            if (
                target_seen
                and not self.source_target_interrupted
                and self.source_capture.append_count
                and at_end
                and append_idle
            ):
                self.source_capture.finished = True
                break

            if time.time() - last_progress_at >= 5:
                percentage = min(100.0, current_time / duration * 100) if duration > 0 else 0.0
                logger.info(
                    "SABR browser SourceBuffer itag %s: %.1f%% (%s bytes, %s appends; active %s)",
                    self.stream.itag,
                    percentage,
                    self.source_capture.total_bytes,
                    self.source_capture.append_count,
                    active_itag or "unknown",
                )
                last_progress_at = time.time()

        self._drain_source_buffers(page)

        if not self.source_capture.complete():
            self.player_error = self._player_error(page)
            if self.player_error:
                logger.warning("YouTube browser player error: %s", self.player_error)

    def _start_playback(self, page) -> None:
        playback_rate = self._env_float("PYTUBEFIX_SABR_BROWSER_PLAYBACK_RATE", 16.0)
        page.locator("video").evaluate(
            """(video, rate) => {
                video.muted = true;
                video.playbackRate = rate;
                video.play().catch(() => {});
            }""",
            playback_rate,
        )
        quality = self._quality_for_resolution(self.stream.resolution)
        page.evaluate(
            """quality => {
                const player = document.getElementById('movie_player');
                if (!player || !quality) return;
                try { player.setPlaybackQualityRange?.(quality, quality); } catch (_) {}
                try { player.setPlaybackQuality?.(quality); } catch (_) {}
            }""",
            quality,
        )

    def _keep_playing(self, page) -> None:
        rate = self._env_float("PYTUBEFIX_SABR_BROWSER_PLAYBACK_RATE", 16.0)
        try:
            page.locator("video").evaluate(
                """(video, value) => {
                    video.muted = true;
                    video.playbackRate = value;
                    if (video.paused && !video.ended) video.play();
                }""",
                rate,
            )
        except Exception:
            pass

    def _seek_to_missing_segment(self, page) -> None:
        end = self.capture.end_segment_number
        if end is None or end < 1:
            return
        missing = next(
            (sequence for sequence in range(end + 1) if sequence not in self.capture.chunks),
            None,
        )
        if missing is None:
            return
        duration_seconds = float(self.stream.durationMs or 0) / 1000
        seek_seconds = max(0.0, duration_seconds * missing / (end + 1) - 1.0)
        try:
            page.locator("video").evaluate(
                "(video, second) => { video.currentTime = second; video.play(); }",
                seek_seconds,
            )
            self.last_media_at = time.time()
        except Exception:
            pass

    def _wait_for_main_video(self, page) -> None:
        expected_seconds = float(self.stream.durationMs or 0) / 1000
        deadline = time.time() + self._env_int("PYTUBEFIX_SABR_BROWSER_AD_WAIT", 20)
        while time.time() < deadline:
            try:
                duration = float(page.locator("video").evaluate("video => video.duration || 0") or 0)
                if duration >= max(30, expected_seconds * 0.5):
                    return
                skip = page.locator(
                    ".ytp-ad-skip-button, .ytp-ad-skip-button-modern, .ytp-skip-ad-button"
                ).first
                if skip.is_visible(timeout=250):
                    skip.click(timeout=500)
            except Exception:
                pass
            page.wait_for_timeout(500)

    def _move_window_offscreen(self, page) -> None:
        mode = os.environ.get("PYTUBEFIX_SABR_BROWSER_MODE", "auto").lower()
        hidden = mode == "hidden-headed" or (mode == "auto" and platform.system() == "Windows")
        if not hidden:
            return
        try:
            session = page.context.new_cdp_session(page)
            window_id = session.send("Browser.getWindowForTarget").get("windowId")
            if window_id is not None:
                session.send(
                    "Browser.setWindowBounds",
                    {
                        "windowId": window_id,
                        "bounds": {
                            "windowState": "normal",
                            "left": -32000,
                            "top": -32000,
                            "width": 1280,
                            "height": 720,
                        },
                    },
                )
        except Exception:
            logger.debug("Unable to move SABR browser off-screen", exc_info=True)

    def _disabled_codecs(self) -> List[str]:
        # SourceBuffer mode validates the active itag. VP9 additionally masks
        # only AV1, leaving H.264 available for startup/ad media.
        if self.source_buffer_mode:
            codec = (self.stream.video_codec or "").lower()
            if codec.startswith(("vp9", "vp09")):
                return ["av01", "av1"]
            return []
        video_codec = (self.stream.video_codec or "").lower()
        audio_codec = (self.stream.audio_codec or "").lower()
        if video_codec.startswith("avc1"):
            disabled = ["av01", "vp09", "vp9"]
        elif video_codec.startswith(("vp09", "vp9")):
            disabled = ["av01", "av1", "avc1"]
        elif video_codec.startswith(("av01", "av1")):
            disabled = ["avc1", "vp09", "vp9"]
        else:
            disabled = []
        if audio_codec.startswith("mp4a"):
            disabled.append("opus")
        elif "opus" in audio_codec:
            disabled.append("mp4a")
        return disabled

    @staticmethod
    def _install_codec_mask(context, codecs: List[str]) -> None:
        disabled = json.dumps([codec.lower() for codec in codecs])
        context.add_init_script(
            """(() => {
                const disabled = new Set(%s);
                const blocked = value => [...disabled].some(codec => String(value || '').toLowerCase().includes(codec));
                const canPlayType = HTMLMediaElement.prototype.canPlayType;
                HTMLMediaElement.prototype.canPlayType = function(type) {
                    return blocked(type) ? '' : canPlayType.call(this, type);
                };
                const isTypeSupported = MediaSource.isTypeSupported.bind(MediaSource);
                MediaSource.isTypeSupported = function(type) {
                    return blocked(type) ? false : isTypeSupported(type);
                };
            })();""" % disabled
        )

    @staticmethod
    def _quality_for_resolution(resolution: Optional[str]) -> Optional[str]:
        return {
            "144p": "tiny",
            "240p": "small",
            "360p": "medium",
            "480p": "large",
            "720p": "hd720",
            "1080p": "hd1080",
            "1440p": "hd1440",
            "2160p": "hd2160",
        }.get(resolution or "")

    @staticmethod
    def _player_error(page) -> str:
        try:
            return page.evaluate(
                """() => document.querySelector('.ytp-error-content-wrap-reason')?.innerText
                    || document.querySelector('.ytp-error-content-wrap-subreason')?.innerText
                    || ''"""
            ) or ""
        except Exception:
            return ""

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default
