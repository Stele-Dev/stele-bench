"""Fixture serialization helpers with no repository-relative behavior."""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import FixtureManifestV1


def load_fixture(path: Path) -> FixtureManifestV1:
    """Load one JSON fixture using the pinned v1 contract."""
    return FixtureManifestV1.model_validate_json(path.read_text(encoding="utf-8"))


def dump_fixture(manifest: FixtureManifestV1) -> str:
    """Produce deterministic JSON suitable for fixtures and tests."""
    return json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
