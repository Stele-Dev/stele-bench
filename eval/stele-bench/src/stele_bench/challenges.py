"""Loading and selection for immutable, checked-in challenge definitions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from stele_bench.contracts import ChallengeManifestV1

DEFAULT_CHALLENGE_ROOT = Path(__file__).resolve().parents[2] / "challenges"
_MANIFEST_NAME = "challenge.json"


class ChallengeCatalogError(ValueError):
    """Raised when a challenge catalog is unsafe, corrupt, or ambiguous."""


@dataclass(frozen=True)
class LoadedChallenge:
    manifest: ChallengeManifestV1
    directory: Path
    verifier_path: Path

    @property
    def challenge_id(self) -> str:
        return self.manifest.challenge_id


def load_challenge(
    manifest_path: Path,
    *,
    catalog_root: Path | None = None,
) -> LoadedChallenge:
    """Load one challenge and authenticate its hidden verifier asset."""
    manifest_path = Path(manifest_path)
    if manifest_path.is_symlink():
        raise ChallengeCatalogError(f"challenge manifest must not be a symlink: {manifest_path}")
    try:
        resolved_manifest = manifest_path.resolve(strict=True)
    except FileNotFoundError as error:
        raise ChallengeCatalogError(
            f"challenge manifest does not exist: {manifest_path}"
        ) from error
    if not resolved_manifest.is_file():
        raise ChallengeCatalogError(f"challenge manifest is not a file: {manifest_path}")

    challenge_directory = resolved_manifest.parent
    if catalog_root is not None:
        root = _resolved_directory(catalog_root)
        _require_contained(root, resolved_manifest, "challenge manifest")

    try:
        raw = json.loads(resolved_manifest.read_text(encoding="utf-8"))
        manifest = ChallengeManifestV1.model_validate(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise ChallengeCatalogError(
            f"invalid challenge manifest {manifest_path}: {error}"
        ) from error

    if challenge_directory.name != manifest.challenge_id:
        raise ChallengeCatalogError(
            "challenge directory name must exactly match challenge_id: "
            f"{challenge_directory.name!r} != {manifest.challenge_id!r}"
        )

    verifier_path = challenge_directory / manifest.verifier.asset.path
    if verifier_path.is_symlink():
        raise ChallengeCatalogError(
            f"verifier asset must not be a symlink: {manifest.verifier.asset.path}"
        )
    try:
        resolved_verifier = verifier_path.resolve(strict=True)
    except FileNotFoundError as error:
        raise ChallengeCatalogError(
            f"verifier asset does not exist: {manifest.verifier.asset.path}"
        ) from error
    _require_contained(challenge_directory, resolved_verifier, "verifier asset")
    if not resolved_verifier.is_file():
        raise ChallengeCatalogError(
            f"verifier asset is not a file: {manifest.verifier.asset.path}"
        )
    actual_digest = hashlib.sha256(resolved_verifier.read_bytes()).hexdigest()
    if actual_digest != manifest.verifier.asset.sha256:
        raise ChallengeCatalogError(
            "verifier digest mismatch for "
            f"{manifest.challenge_id}: expected {manifest.verifier.asset.sha256}, "
            f"got {actual_digest}"
        )

    return LoadedChallenge(
        manifest=manifest,
        directory=challenge_directory,
        verifier_path=resolved_verifier,
    )


def load_challenge_catalog(root: Path = DEFAULT_CHALLENGE_ROOT) -> tuple[LoadedChallenge, ...]:
    """Load every challenge in stable id order and reject ambiguous catalogs."""
    resolved_root = _resolved_directory(root)
    manifest_paths = sorted(resolved_root.glob(f"*/{_MANIFEST_NAME}"))
    if not manifest_paths:
        raise ChallengeCatalogError(f"challenge catalog is empty: {root}")
    loaded = tuple(
        sorted(
            (load_challenge(path, catalog_root=resolved_root) for path in manifest_paths),
            key=lambda challenge: challenge.challenge_id,
        )
    )
    ids = tuple(challenge.challenge_id for challenge in loaded)
    duplicates = sorted(challenge_id for challenge_id in set(ids) if ids.count(challenge_id) > 1)
    if duplicates:
        raise ChallengeCatalogError(f"duplicate challenge ids: {', '.join(duplicates)}")
    return loaded


def select_challenges(
    challenge_ids: Iterable[str],
    *,
    root: Path = DEFAULT_CHALLENGE_ROOT,
) -> tuple[LoadedChallenge, ...]:
    """Select challenges in caller-provided order, rejecting partial selections."""
    if isinstance(challenge_ids, str):
        raise ChallengeCatalogError("challenge_ids must be an iterable of ids, not one string")
    requested = tuple(challenge_ids)
    if not requested:
        raise ChallengeCatalogError("at least one challenge id is required")
    duplicates = sorted(
        challenge_id for challenge_id in set(requested) if requested.count(challenge_id) > 1
    )
    if duplicates:
        raise ChallengeCatalogError(f"duplicate selected challenge ids: {', '.join(duplicates)}")

    catalog = load_challenge_catalog(root)
    by_id = {challenge.challenge_id: challenge for challenge in catalog}
    unknown = [challenge_id for challenge_id in requested if challenge_id not in by_id]
    if unknown:
        raise ChallengeCatalogError(f"unknown challenge ids: {', '.join(unknown)}")
    return tuple(by_id[challenge_id] for challenge_id in requested)


def _resolved_directory(path: Path) -> Path:
    try:
        resolved = Path(path).resolve(strict=True)
    except FileNotFoundError as error:
        raise ChallengeCatalogError(f"challenge catalog does not exist: {path}") from error
    if not resolved.is_dir():
        raise ChallengeCatalogError(f"challenge catalog is not a directory: {path}")
    return resolved


def _require_contained(root: Path, path: Path, label: str) -> None:
    try:
        path.relative_to(root.resolve(strict=True))
    except ValueError as error:
        raise ChallengeCatalogError(f"{label} escapes its challenge directory: {path}") from error
