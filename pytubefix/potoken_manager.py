"""Small storage layer for proof-of-origin tokens.

This module intentionally keeps PoToken state separate from the OAuth cache.
PoTokens are short-lived and bound to the client/playback context, so storing
them beside OAuth refresh tokens makes invalidation too coarse.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


DEFAULT_TTL_SECONDS = 25 * 60
DEFAULT_SCOPE = "playback"


def _default_cache_dir() -> Path:
    configured_cache_dir = os.environ.get("PYTUBEFIX_POTOKEN_CACHE_DIR")
    if configured_cache_dir:
        return Path(configured_cache_dir).expanduser()

    if os.name == "nt":
        windows_cache_dir = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if windows_cache_dir:
            return Path(windows_cache_dir) / "pytubefix"

    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache_home:
        return Path(xdg_cache_home).expanduser() / "pytubefix"

    return Path.home() / ".cache" / "pytubefix"


def _default_token_file() -> Path:
    configured_token_file = os.environ.get("PYTUBEFIX_POTOKEN_FILE")
    if configured_token_file:
        return Path(configured_token_file).expanduser()
    return _default_cache_dir() / "po_tokens.json"


DEFAULT_CACHE_DIR = _default_cache_dir()
DEFAULT_TOKEN_FILE = _default_token_file()


@dataclass(frozen=True)
class PoTokenRecord:
    client: str
    visitor_data: str
    po_token: str
    video_id: Optional[str] = None
    scope: str = DEFAULT_SCOPE
    source: str = "unknown"
    created_at: float = 0.0
    expires_at: float = 0.0

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= time.time()

    def to_json(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "visitorData": self.visitor_data,
            "poToken": self.po_token,
            "videoId": self.video_id,
            "scope": self.scope,
            "source": self.source,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "PoTokenRecord":
        return cls(
            client=str(data["client"]),
            visitor_data=str(data["visitorData"]),
            po_token=str(data["poToken"]),
            video_id=data.get("videoId"),
            scope=str(data.get("scope") or DEFAULT_SCOPE),
            source=str(data.get("source") or "unknown"),
            created_at=float(data.get("createdAt") or 0.0),
            expires_at=float(data.get("expiresAt") or 0.0),
        )


class PoTokenManager:
    """Read/write PoToken records from a dedicated cache file."""

    def __init__(
        self,
        token_file: str | os.PathLike[str] | None = None,
        default_ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.token_file = Path(token_file).expanduser() if token_file else _default_token_file()
        self.default_ttl_seconds = default_ttl_seconds

    @staticmethod
    def cache_key(
        client: str,
        video_id: str | None = None,
        scope: str = DEFAULT_SCOPE,
        visitor_data: str | None = None,
    ) -> str:
        normalized_client = (client or "").strip().upper()
        normalized_scope = (scope or DEFAULT_SCOPE).strip().lower()
        normalized_video_id = (video_id or "*").strip()
        if normalized_scope == "sabr" and visitor_data:
            visitor_hash = hashlib.sha256(visitor_data.encode("utf-8")).hexdigest()[:16]
            return f"{normalized_client}:{normalized_scope}:{normalized_video_id}:{visitor_hash}"
        return f"{normalized_client}:{normalized_scope}:{normalized_video_id}"

    def _read_cache(self) -> dict[str, Any]:
        if not self.token_file.exists():
            return {"version": 1, "tokens": {}}

        try:
            data = json.loads(self.token_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"version": 1, "tokens": {}}

        if not isinstance(data, dict):
            return {"version": 1, "tokens": {}}
        if not isinstance(data.get("tokens"), dict):
            data["tokens"] = {}
        data.setdefault("version", 1)
        return data

    def _write_cache(self, data: dict[str, Any]) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.token_file.with_suffix(self.token_file.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, self.token_file)

    def put(
        self,
        *,
        client: str,
        visitor_data: str,
        po_token: str,
        video_id: str | None = None,
        scope: str = DEFAULT_SCOPE,
        source: str = "unknown",
        ttl_seconds: int | None = None,
    ) -> PoTokenRecord:
        now = time.time()
        ttl = self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
        record = PoTokenRecord(
            client=client.strip().upper(),
            visitor_data=visitor_data,
            po_token=po_token,
            video_id=video_id,
            scope=scope or DEFAULT_SCOPE,
            source=source or "unknown",
            created_at=now,
            expires_at=now + max(0, int(ttl)),
        )

        data = self._read_cache()
        key = self.cache_key(record.client, record.video_id, record.scope, record.visitor_data)
        data["tokens"][key] = record.to_json()
        self._write_cache(data)
        return record

    def import_token(
        self,
        *,
        client: str,
        visitor_data: str,
        po_token: str,
        video_id: str | None = None,
        scope: str = DEFAULT_SCOPE,
        source: str = "manual_import",
        ttl_seconds: int | None = None,
    ) -> PoTokenRecord:
        """Validate and store an externally supplied PoToken."""
        normalized_client = (client or "").strip().upper()
        normalized_visitor_data = (visitor_data or "").strip()
        normalized_po_token = (po_token or "").strip()
        normalized_video_id = (video_id or "").strip() or None

        if not normalized_client:
            raise ValueError("client is required when importing a PoToken")
        if not normalized_visitor_data:
            raise ValueError("visitor_data is required when importing a PoToken")
        if not normalized_po_token:
            raise ValueError("po_token is required when importing a PoToken")

        return self.put(
            client=normalized_client,
            visitor_data=normalized_visitor_data,
            po_token=normalized_po_token,
            video_id=normalized_video_id,
            scope=scope,
            source=source or "manual_import",
            ttl_seconds=ttl_seconds,
        )

    def get(
        self,
        *,
        client: str,
        video_id: str | None = None,
        scope: str = DEFAULT_SCOPE,
        visitor_data: str | None = None,
        allow_expired: bool = False,
    ) -> PoTokenRecord | None:
        data = self._read_cache()
        key = self.cache_key(client, video_id, scope, visitor_data)
        raw = data["tokens"].get(key)
        if raw is None and video_id:
            raw = data["tokens"].get(self.cache_key(client, None, scope))
        if raw is None and scope == "sabr":
            raw = data["tokens"].get(self.cache_key(client, video_id, scope))
        if not isinstance(raw, dict):
            return None

        try:
            record = PoTokenRecord.from_json(raw)
        except (KeyError, TypeError, ValueError):
            return None

        if visitor_data and record.visitor_data != visitor_data:
            return None
        if record.is_expired and not allow_expired:
            return None
        return record

    def invalidate(
        self,
        *,
        client: str,
        video_id: str | None = None,
        scope: str = DEFAULT_SCOPE,
    ) -> bool:
        data = self._read_cache()
        key = self.cache_key(client, video_id, scope)
        removed = data["tokens"].pop(key, None) is not None
        if scope == "sabr":
            prefix = self.cache_key(client, video_id, scope) + ":"
            matching_keys = [cached_key for cached_key in data["tokens"] if cached_key.startswith(prefix)]
            for cached_key in matching_keys:
                data["tokens"].pop(cached_key, None)
                removed = True
        if removed:
            self._write_cache(data)
        return removed

    def prune_expired(self) -> int:
        data = self._read_cache()
        before = len(data["tokens"])
        retained = {}
        for key, raw in data["tokens"].items():
            try:
                record = PoTokenRecord.from_json(raw)
            except (KeyError, TypeError, ValueError):
                continue
            if not record.is_expired:
                retained[key] = raw
        data["tokens"] = retained
        removed = before - len(retained)
        if removed:
            self._write_cache(data)
        return removed

    def clear(self) -> int:
        data = self._read_cache()
        removed = len(data["tokens"])
        data["tokens"] = {}
        self._write_cache(data)
        return removed
