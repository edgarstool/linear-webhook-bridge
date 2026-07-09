#!/usr/bin/env python3
"""Verify or generate Linear webhook HMAC signatures."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_FRESHNESS_WINDOW_MS = 5 * 60 * 1000


def canonical_header_map(raw_headers: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for header in raw_headers:
        if "=" not in header:
            raise ValueError(f"Invalid header format: {header!r}; expected NAME=VALUE")
        name, value = header.split("=", 1)
        headers[name.strip()] = value.strip()
    return headers


def compute_signature(secret: str, raw_body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_linear_webhook(
    raw_body: bytes,
    headers: dict[str, str],
    secret: str,
    now_ms: int | None = None,
    freshness_window_ms: int = DEFAULT_FRESHNESS_WINDOW_MS,
) -> tuple[bool, str, dict[str, Any]]:
    sig_header = headers.get("Linear-Signature") or headers.get("linear-signature")
    expected = compute_signature(secret, raw_body)
    details: dict[str, Any] = {
        "expected_signature": expected,
        "delivery_id": headers.get("Linear-Delivery") or headers.get("linear-delivery"),
        "linear_event": headers.get("Linear-Event") or headers.get("linear-event"),
        "freshness_window_ms": freshness_window_ms,
    }

    if not sig_header:
        return False, "missing signature header", details

    details["provided_signature"] = sig_header
    if not hmac.compare_digest(expected, sig_header):
        return False, "signature mismatch", details

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return False, "body not valid json after signature match", details

    timestamp = payload.get("webhookTimestamp")
    details["webhook_timestamp"] = timestamp
    if not isinstance(timestamp, int):
        return False, "missing webhookTimestamp", details

    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    details["now_ms"] = now_ms
    delta_ms = abs(now_ms - timestamp)
    details["timestamp_delta_ms"] = delta_ms
    if delta_ms > freshness_window_ms:
        return False, "timestamp outside freshness window", details

    return True, "ok", details


def read_body(path: str | None) -> bytes:
    if not path or path == "-":
        return sys.stdin.buffer.read()
    return Path(path).read_bytes()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify or generate Linear webhook signatures using raw request bytes."
    )
    parser.add_argument(
        "--mode",
        choices=("verify", "sign"),
        default="verify",
        help="Use 'verify' to validate headers/body or 'sign' to print the expected signature.",
    )
    parser.add_argument(
        "--body-file",
        default="-",
        help="Path to raw request body. Defaults to stdin.",
    )
    parser.add_argument(
        "--secret",
        required=True,
        help="Linear webhook secret.",
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        help="Repeatable NAME=VALUE header input. Common ones: Linear-Signature, Linear-Delivery.",
    )
    parser.add_argument(
        "--signature",
        help="Shortcut for Linear-Signature when using verify mode.",
    )
    parser.add_argument(
        "--now-ms",
        type=int,
        help="Override current time in milliseconds for replay-window checks.",
    )
    parser.add_argument(
        "--freshness-window-ms",
        type=int,
        default=DEFAULT_FRESHNESS_WINDOW_MS,
        help=f"Replay window in milliseconds. Default: {DEFAULT_FRESHNESS_WINDOW_MS}.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    raw_body = read_body(args.body_file)
    if args.mode == "sign":
        print(compute_signature(args.secret, raw_body))
        return 0

    headers = canonical_header_map(args.header)
    if args.signature:
        headers.setdefault("Linear-Signature", args.signature)

    ok, reason, details = verify_linear_webhook(
        raw_body=raw_body,
        headers=headers,
        secret=args.secret,
        now_ms=args.now_ms,
        freshness_window_ms=args.freshness_window_ms,
    )
    payload = {
        "ok": ok,
        "reason": reason,
        **details,
    }
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
