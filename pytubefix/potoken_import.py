"""Command line helper for importing externally generated PoTokens."""

from __future__ import annotations

import argparse
import json
from typing import Sequence

from pytubefix.potoken_manager import DEFAULT_SCOPE, PoTokenManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import a visitorData + PoToken pair into the pytubefix PoToken cache."
    )
    parser.add_argument("--client", default="WEB", help="Innertube client name. Defaults to WEB.")
    parser.add_argument("--visitor-data", required=True, help="visitorData paired with the PoToken.")
    parser.add_argument("--po-token", required=True, help="Proof-of-origin token to import.")
    parser.add_argument("--video-id", help="Optional video-specific token scope.")
    parser.add_argument("--scope", default=DEFAULT_SCOPE, help=f"Token scope. Defaults to {DEFAULT_SCOPE}.")
    parser.add_argument("--ttl-seconds", type=int, help="Optional token lifetime in seconds.")
    parser.add_argument("--token-file", help="Optional path to the PoToken cache file.")
    parser.add_argument("--source", default="manual_import", help="Source label stored with the token.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manager = PoTokenManager(token_file=args.token_file)
    record = manager.import_token(
        client=args.client,
        visitor_data=args.visitor_data,
        po_token=args.po_token,
        video_id=args.video_id,
        scope=args.scope,
        source=args.source,
        ttl_seconds=args.ttl_seconds,
    )

    print(json.dumps({
        "ok": True,
        "tokenFile": str(manager.token_file),
        "cacheKey": manager.cache_key(record.client, record.video_id, record.scope, record.visitor_data),
        "client": record.client,
        "videoId": record.video_id,
        "scope": record.scope,
        "source": record.source,
        "expiresAt": record.expires_at,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
