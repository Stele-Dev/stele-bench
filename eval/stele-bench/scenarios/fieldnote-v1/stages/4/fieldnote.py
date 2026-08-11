#!/usr/bin/env python3
"""Summarize newline-delimited JSON diagnostic events."""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

VERSION = "1.0.0"
MAX_OUTPUT_BYTES = 50 * 1024
SECRET_KEYS = frozenset({"password", "secret", "token"})
SAMPLE_LIMIT = 5


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"line {line_number} is not an object")
        events.append(value)
    return events


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).lower() in SECRET_KEYS else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def build_summary(events: list[dict[str, Any]], *, include_events: bool) -> dict[str, Any]:
    levels = Counter(str(event.get("level", "unknown")).lower() for event in events)
    result: dict[str, Any] = {
        "event_count": len(events),
        "levels": dict(sorted(levels.items())),
        "samples": [sanitize(event) for event in events[:SAMPLE_LIMIT]],
    }
    if include_events:
        result["events"] = [sanitize(event) for event in events]
    return result


def render(summary: dict[str, Any], output_format: str, *, include_events: bool) -> str:
    if output_format == "json":
        payload = json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n"
    else:
        encoded = html.escape(json.dumps(summary, sort_keys=True, indent=2))
        payload = (
            "<!doctype html><html><head><meta charset=\"utf-8\"><title>Fieldnote</title>"
            "<style>body{font:14px system-ui;max-width:72rem;margin:2rem auto}pre{white-space:pre-wrap}</style>"
            f"</head><body><h1>Fieldnote report</h1><pre>{encoded}</pre></body></html>\n"
        )
    if not include_events and len(payload.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ValueError("report exceeds 50 KiB")
    return payload


def write_atomic(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="fieldnote")
    result.add_argument("input", type=Path, nargs="?")
    result.add_argument("-o", "--output", type=Path)
    result.add_argument("--format", choices=("json", "html"), default="json")
    result.add_argument("--include-events", action="store_true")
    result.add_argument("--version", action="version", version=f"fieldnote {VERSION}")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.input is None:
        parser().error("the following arguments are required: input")
    try:
        events = load_events(args.input)
        payload = render(
            build_summary(events, include_events=args.include_events),
            args.format,
            include_events=args.include_events,
        )
        if args.output is None:
            sys.stdout.write(payload)
        else:
            write_atomic(args.output, payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(f"fieldnote: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
