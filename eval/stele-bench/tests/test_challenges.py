from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from stele_bench.certification import CertificationPolicy
from stele_bench.challenges import (
    DEFAULT_CHALLENGE_ROOT,
    ChallengeCatalogError,
    load_challenge,
    load_challenge_catalog,
    select_challenges,
)
from stele_bench.contracts import AnswerFileVerifierV1, ScriptVerifierV1


def _write_challenge(
    root: Path,
    challenge_id: str,
    *,
    verifier_kind: str = "script",
    digest: str | None = None,
) -> Path:
    directory = root / challenge_id
    directory.mkdir(parents=True)
    verifier = directory / "verifier.sh"
    verifier.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    actual_digest = hashlib.sha256(verifier.read_bytes()).hexdigest()
    verifier_data: dict[str, object] = {
        "kind": verifier_kind,
        "runtime": "posix-shell",
        "asset": {
            "path": "verifier.sh",
            "sha256": digest or actual_digest,
            "media_type": "text/x-shellscript",
        },
        "timeout_seconds": 30,
    }
    output_path = "/workspace/.ready"
    if verifier_kind == "answer-file":
        output_path = "/workspace/.submission/answer.md"
        verifier_data["answer_path"] = output_path
    manifest = {
        "schema_version": 1,
        "challenge_id": challenge_id,
        "title": challenge_id,
        "description": "A portable challenge.",
        "prompt": "Complete the requested work.\n",
        "allowed_output_paths": [output_path],
        "agent_timeout_seconds": 300,
        "environment": {
            "network_mode": "none",
            "build_timeout_seconds": 30,
            "cpus": 1,
            "memory_mb": 512,
            "storage_mb": 1024,
        },
        "verifier": verifier_data,
    }
    path = directory / "challenge.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_checked_in_catalog_is_digest_pinned_strict_and_neutral() -> None:
    catalog = load_challenge_catalog()
    assert [challenge.challenge_id for challenge in catalog] == [
        "click-clirunner-buffer-ownership",
        "click-clirunner-capture-modes",
        "click-clirunner-fileno-compatibility",
        "click-clirunner-finalization-order",
        "click-clirunner-stream-contract",
        "dotfile-drift-detection",
        "fieldnote-milestone-1",
        "fieldnote-milestone-2",
        "fieldnote-milestone-3",
        "fieldnote-milestone-4",
        "input-keypress-architecture",
        "package-cache-is-empty",
        "profile-selection-intake",
        "profile-selection-live-path",
        "service-readiness",
        "session-boundary-recall",
        "settings-label-roundtrip",
    ]
    # The catalog exercises both verifier kinds. Asserted by membership rather
    # than by index so adding a challenge cannot silently reorder this away.
    assert {type(challenge.manifest.verifier) for challenge in catalog} == {
        AnswerFileVerifierV1,
        ScriptVerifierV1,
    }
    settings_label = next(
        challenge for challenge in catalog if challenge.challenge_id == "settings-label-roundtrip"
    )
    assert settings_label.manifest.environment.prebuild_argv == (
        "cargo",
        "test",
        "--no-run",
        "--lib",
    )
    # A verifier ships into the task directory, so a stray harness word in one
    # of its COMMENTS fails the blindness audit at pack time -- mid-run, after
    # setup. Catching it here turns that into a test failure instead. (This is
    # not hypothetical: "which is the whole oracle" in a comment did exactly
    # that.)
    identity_markers = CertificationPolicy().harness_identity_markers
    for challenge in catalog:
        body = challenge.verifier_path.read_text(encoding="utf-8").casefold()
        leaked = sorted(marker for marker in identity_markers if marker in body)
        assert not leaked, f"{challenge.challenge_id} verifier leaks {leaked}"

    for challenge in catalog:
        subprocess.run(["sh", "-n", challenge.verifier_path], check=True)
        visible_text = " ".join(
            (
                challenge.manifest.title,
                challenge.manifest.description,
                challenge.manifest.prompt,
                challenge.manifest.follow_up_prompt or "",
            )
        ).lower()
        for forbidden in ("benchmark", "eval", "condition", "arm", "stele", "know-"):
            assert forbidden not in visible_text


def test_loader_rejects_digest_changes_and_path_escapes(tmp_path: Path) -> None:
    bad_digest = _write_challenge(tmp_path, "bad-digest", digest="a" * 64)
    with pytest.raises(ChallengeCatalogError, match="digest mismatch"):
        load_challenge(bad_digest, catalog_root=tmp_path)

    root = tmp_path / "contained"
    manifest_path = _write_challenge(root, "escaped-verifier")
    verifier_path = manifest_path.parent / "verifier.sh"
    verifier_path.unlink()
    outside = tmp_path / "outside.sh"
    outside.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    verifier_path.symlink_to(outside)
    with pytest.raises(ChallengeCatalogError, match="symlink"):
        load_challenge(manifest_path, catalog_root=root)

    outside_manifest = _write_challenge(tmp_path / "other", "outside-manifest")
    with pytest.raises(ChallengeCatalogError, match="escapes"):
        load_challenge(outside_manifest, catalog_root=root)


def test_loader_rejects_nonportable_ids_and_unknown_fields(tmp_path: Path) -> None:
    manifest_path = _write_challenge(tmp_path, "valid-id")
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["challenge_id"] = "Not_Portable"
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ChallengeCatalogError, match="invalid challenge manifest"):
        load_challenge(manifest_path)

    manifest_path = _write_challenge(tmp_path, "strict-fields")
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["unexpected"] = True
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ChallengeCatalogError, match="extra_forbidden"):
        load_challenge(manifest_path)


def test_selection_rejects_empty_duplicates_and_unknown_ids_and_preserves_order(
    tmp_path: Path,
) -> None:
    for challenge_id in ("z-last", "a-first", "middle"):
        _write_challenge(tmp_path, challenge_id)
    assert [challenge.challenge_id for challenge in load_challenge_catalog(tmp_path)] == [
        "a-first",
        "middle",
        "z-last",
    ]
    requested = ("z-last", "a-first")
    first = select_challenges(requested, root=tmp_path)
    second = select_challenges(requested, root=tmp_path)
    assert [challenge.challenge_id for challenge in first] == list(requested)
    assert [challenge.challenge_id for challenge in second] == list(requested)
    with pytest.raises(ChallengeCatalogError, match="at least one"):
        select_challenges((), root=tmp_path)
    with pytest.raises(ChallengeCatalogError, match="duplicate selected"):
        select_challenges(("a-first", "a-first"), root=tmp_path)
    with pytest.raises(ChallengeCatalogError, match="unknown"):
        select_challenges(("missing",), root=tmp_path)
    with pytest.raises(ChallengeCatalogError, match="not one string"):
        select_challenges("a-first", root=tmp_path)


def test_default_catalog_location_is_checked_in() -> None:
    assert DEFAULT_CHALLENGE_ROOT.name == "challenges"
    assert (DEFAULT_CHALLENGE_ROOT / "service-readiness" / "challenge.json").is_file()
