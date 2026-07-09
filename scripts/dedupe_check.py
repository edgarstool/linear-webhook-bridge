#!/usr/bin/env python3
"""Inspect dedupe keys for a Linear webhook payload."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


MARKER_RE = re.compile(r"<!--\s*hermes:run=([a-zA-Z0-9._:-]+)\s*-->")


def read_text(path: str | None) -> str:
    if not path or path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def parse_header(values: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"Invalid header format: {value!r}; expected NAME=VALUE")
        name, header_value = value.split("=", 1)
        headers[name.strip()] = header_value.strip()
    return headers


def stable_content(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") or {}
    parent = data.get("parent") or {}
    return {
        "type": payload.get("type"),
        "action": payload.get("action"),
        "issue_id": data.get("issue", {}).get("id") or data.get("id"),
        "comment_id": data.get("id") if payload.get("type") == "Comment" else None,
        "parent_type": parent.get("type"),
        "body": data.get("body"),
        "title": data.get("title"),
        "description": data.get("description"),
        "state": (data.get("state") or {}).get("name"),
    }


def content_hash(payload: dict[str, Any]) -> str:
    canonical = stable_content(payload)
    encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def extract_run_id(text: str) -> str | None:
    match = MARKER_RE.search(text)
    return match.group(1) if match else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compute L1/L2/L3 dedupe keys for a Linear webhook payload."
    )
    parser.add_argument(
        "--body-file",
        default="-",
        help="Path to JSON body. Defaults to stdin.",
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        help="Repeatable NAME=VALUE header input. Common one: Linear-Delivery.",
    )
    parser.add_argument(
        "--run-id",
        help="Override run ID for the L3 comment marker.",
    )
    parser.add_argument(
        "--comment-body-file",
        help="Optional comment body file to inspect for an existing hermes marker.",
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

    payload_text = read_text(args.body_file)
    payload = json.loads(payload_text)
    headers = parse_header(args.header)
    derived_run_id = None
    if args.comment_body_file:
        derived_run_id = extract_run_id(read_text(args.comment_body_file))
    run_id = args.run_id or derived_run_id

    data = payload.get("data") or {}
    issue_id = data.get("issue", {}).get("id") or data.get("id")
    action = payload.get("action")
    l2_hash = content_hash(payload)

    result = {
        "l1": {
            "delivery_id": headers.get("Linear-Delivery") or headers.get("linear-delivery"),
        },
        "l2": {
            "issue_id": issue_id,
            "action": action,
            "content_hash": l2_hash,
            "compound_key": f"{issue_id}:{action}:{l2_hash}",
        },
        "l3": {
            "run_id": run_id,
            "marker": f"<!-- hermes:run={run_id} -->" if run_id else None,
        },
        "derived_payload_fields": stable_content(payload),
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
