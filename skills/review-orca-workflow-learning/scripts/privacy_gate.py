#!/usr/bin/env python3
"""Apply a deterministic secondary privacy scan to bounded JSON receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_DEPTH = 16
MAX_NODES = 100_000
FORBIDDEN_KEY_PARTS = {
    "api_key",
    "apikey",
    "jwt",
    "run_jwt",
    "bridge_token",
    "prompt",
    "credential",
    "password",
    "secret",
    "token",
    "cookie",
    "authorization",
    "privatekey",
    "private_key",
    "sessionbody",
    "session_body",
    "transcript",
    "tooloutput",
    "tool_output",
    "hiddenreasoning",
    "hidden_reasoning",
    "chainofthought",
    "chain_of_thought",
    "messagebody",
    "message_body",
    "rawtext",
    "raw_text",
}
PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
PROVIDER_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b")
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
UNIX_PATH_RE = re.compile(r"^/(?:Users|Volumes|private|var|tmp|etc|opt|home)(?:/|$)")
WINDOWS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
EMAIL_RE = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,255}\.[A-Za-z]{2,24}$")
PHONE_RE = re.compile(r"^(?=(?:\D*\d){9,15}\D*$)\+?[0-9][0-9 ()-]{7,}[0-9]$")


class PrivacyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", value.casefold())


def _scan_string(value: str) -> None:
    if len(value.encode("utf-8")) > 8_192:
        raise PrivacyError("PRIVACY_VALUE_TOO_LARGE", "string exceeds privacy scan limit")
    if (
        PRIVATE_KEY_RE.search(value)
        or BEARER_RE.search(value)
        or AWS_KEY_RE.search(value)
        or PROVIDER_TOKEN_RE.search(value)
        or JWT_RE.search(value)
    ):
        raise PrivacyError("SECRET_SHAPE_REJECTED", "secret-shaped value is forbidden")
    if UNIX_PATH_RE.match(value) or WINDOWS_PATH_RE.match(value):
        raise PrivacyError("ABSOLUTE_PATH_REJECTED", "absolute path value is forbidden")
    if EMAIL_RE.fullmatch(value) or PHONE_RE.fullmatch(value):
        raise PrivacyError("PII_SHAPE_REJECTED", "PII-shaped value is forbidden")


def scan(value: Any) -> dict[str, Any]:
    nodes = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_NODES:
            raise PrivacyError("PRIVACY_NODE_LIMIT", "JSON node limit exceeded")
        if depth > MAX_DEPTH:
            raise PrivacyError("PRIVACY_DEPTH_LIMIT", "JSON nesting limit exceeded")
        if item is None or isinstance(item, (bool, int)):
            return
        if isinstance(item, float):
            if not (-float("inf") < item < float("inf")):
                raise PrivacyError("PRIVACY_SCHEMA_REJECTED", "non-finite number is forbidden")
            return
        if isinstance(item, str):
            _scan_string(item)
            return
        if isinstance(item, list):
            if len(item) > 4_096:
                raise PrivacyError("PRIVACY_ARRAY_LIMIT", "array exceeds privacy scan limit")
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, dict):
            if len(item) > 1_024:
                raise PrivacyError("PRIVACY_OBJECT_LIMIT", "object exceeds privacy scan limit")
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 128:
                    raise PrivacyError("PRIVACY_SCHEMA_REJECTED", "invalid JSON key")
                normalized = _normalized_key(key)
                if any(part in normalized for part in FORBIDDEN_KEY_PARTS):
                    raise PrivacyError("FORBIDDEN_FIELD_REJECTED", "forbidden content field is present")
                visit(child, depth + 1)
            return
        raise PrivacyError("PRIVACY_SCHEMA_REJECTED", "unsupported JSON value type")

    payload = canonical_bytes(value)
    if len(payload) > MAX_INPUT_BYTES:
        raise PrivacyError("PRIVACY_INPUT_TOO_LARGE", "JSON exceeds privacy byte limit")
    visit(value, 0)
    return {
        "status": "pass",
        "node_count": nodes,
        "value_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        if arguments.input.stat().st_size > MAX_INPUT_BYTES:
            raise PrivacyError("PRIVACY_INPUT_TOO_LARGE", "input file exceeds byte limit")
        value = json.loads(arguments.input.read_text(encoding="utf-8"))
        print(json.dumps(scan(value), sort_keys=True))
        return 0
    except (PrivacyError, OSError, json.JSONDecodeError) as exc:
        code = exc.code if isinstance(exc, PrivacyError) else "PRIVACY_IO_REJECTED"
        print(json.dumps({"status": "blocked", "error_code": code}, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
