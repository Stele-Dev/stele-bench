#!/usr/bin/env python3
"""Summarize newline-delimited JSON diagnostic events."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

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


def build_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    levels = Counter(str(event.get("level", "unknown")).lower() for event in events)
    return {
        "event_count": len(events),
        "levels": dict(sorted(levels.items())),
        "samples": [sanitize(event) for event in events[:SAMPLE_LIMIT]],
    }


def render_json(summary: dict[str, Any]) -> str:
    payload = json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n"
    if len(payload.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ValueError("report exceeds 50 KiB")
    return payload


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="fieldnote")
    result.add_argument("input", type=Path)
    result.add_argument("-o", "--output", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        payload = render_json(build_summary(load_events(args.input)))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(f"fieldnote: {error}", file=sys.stderr)
        return 2
    if args.output is None:
        sys.stdout.write(payload)
    else:
        args.output.write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
