from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from stele_bench.certification import (
    KIND_AUTO_LOADED_CONTEXT,
    KIND_BENCHMARK_AWARENESS,
    KIND_PATCH_OR_DIFF,
    KIND_SOLUTION_LEAK,
    KIND_SUSPICIOUS_METADATA,
    CertificationError,
    CertificationInputError,
    certify_challenge_manifest,
    certify_graph_document,
    certify_packaged_inputs,
    certify_project_tree,
    redact_packaged_context,
    write_certification_receipt,
)
from stele_bench.challenges import load_challenge_catalog
from stele_bench.contracts import ChallengeManifestV1
from stele_bench.corpus import materialize_pinned_tree
from stele_bench.fixtures import load_fixture

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PINNED_CORPUS = Path(
    os.environ.get("STELE_BENCH_CORPUS_REPOSITORY", REPOSITORY_ROOT.parent / "dotstate")
)
PINNED_REVISION = "a4a5986eafa496dee73a93141588cffaed559f80"
FIXTURE_ROOT = REPOSITORY_ROOT / "eval" / "stele-bench" / "fixtures"


def _write(root: Path, relative: str, contents: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    return path


def _kinds(findings: tuple[object, ...]) -> set[str]:
    return {finding.kind for finding in findings}  # type: ignore[attr-defined]


def _graph(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 1,
        "project": {"name": "DotState", "description": "Dotfile manager."},
        "workspaces": [],
        "branches": [],
        "nodes": [],
        "edges": [],
        "comments": [],
    }
    document.update(overrides)
    return document


def test_auto_loaded_harness_context_is_reported_once_per_directory(tmp_path: Path) -> None:
    _write(tmp_path, "CLAUDE.md", "# project memory\n")
    _write(tmp_path, ".claude/skills/dev/SKILL.md", "always run cargo fmt\n")
    _write(tmp_path, ".claude/commands/publish.md", "publish flow\n")
    _write(tmp_path, "src/main.rs", "fn main() {}\n")

    findings, probed = certify_project_tree(tmp_path)

    assert probed == 4
    locators = {finding.locator for finding in findings}
    # The whole `.claude` tree collapses to its shallowest path so a single
    # directory cannot bury every other finding.
    assert locators == {"CLAUDE.md", ".claude"}
    assert _kinds(findings) == {KIND_AUTO_LOADED_CONTEXT}


def test_product_binding_and_repository_metadata_are_suspicious(tmp_path: Path) -> None:
    _write(tmp_path, ".stele/project.json", json.dumps({"slug": "dotstate"}))
    _write(tmp_path, "task.toml", "version = 1\n")

    findings, _probed = certify_project_tree(tmp_path)

    assert _kinds(findings) == {KIND_SUSPICIOUS_METADATA}
    assert {finding.locator for finding in findings} == {".stele", "task.toml"}


def test_solution_marker_in_the_corpus_is_a_leak_with_a_line_number(tmp_path: Path) -> None:
    _write(tmp_path, "docs/rules.md", "intro\nmore\nAlways update the changelog first\n")

    findings, _probed = certify_project_tree(
        tmp_path, solution_markers=("Always update the changelog first",)
    )

    assert len(findings) == 1
    assert findings[0].kind == KIND_SOLUTION_LEAK
    assert findings[0].locator == "docs/rules.md"
    assert findings[0].line == 3


def test_patches_are_rejected_by_suffix_and_by_content(tmp_path: Path) -> None:
    _write(tmp_path, "fix.patch", "nothing diff-like inside\n")
    _write(tmp_path, "notes.md", "context\ndiff --git a/src/x.rs b/src/x.rs\n")
    _write(tmp_path, "hunk.txt", "before\n@@ -1,4 +1,6 @@\n")

    findings, _probed = certify_project_tree(tmp_path)

    assert _kinds(findings) == {KIND_PATCH_OR_DIFF}
    assert {finding.locator for finding in findings} == {"fix.patch", "notes.md", "hunk.txt"}


def test_harness_identity_in_corpus_text_is_benchmark_awareness(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "Run the benchmark before shipping.\n")

    findings, _probed = certify_project_tree(tmp_path)

    assert [(f.kind, f.marker) for f in findings] == [(KIND_BENCHMARK_AWARENESS, "benchmark")]


def test_ordinary_engineering_prose_does_not_trip_certification(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "tests/README.md",
        "Spin up a temp directory with test fixtures, then assert the condition holds "
        "under trial. The manifest is the ground truth for one arm of the symlink.\n",
    )

    findings, _probed = certify_project_tree(tmp_path)

    assert findings == ()


def test_graph_identity_fields_are_stricter_than_node_bodies() -> None:
    leaky = _graph(
        project={"name": "Recall Fixture", "description": "Synthetic single-lesson fixture."}
    )
    findings, probed = certify_graph_document(leaky, arm="faithful")

    assert probed == 2
    assert {finding.locator for finding in findings} == {"project.name", "project.description"}
    assert _kinds(findings) == {KIND_BENCHMARK_AWARENESS}

    # The identical word inside a node body is real project prose, not a leak.
    realistic = _graph(
        nodes=[{"short_id": "KNOW-1", "body": "Spin up a temp directory with test fixtures."}]
    )
    assert certify_graph_document(realistic, arm="faithful")[0] == ()


def test_harness_identity_leaks_anywhere_in_a_graph() -> None:
    document = _graph(
        nodes=[{"short_id": "KNOW-9", "body": "This node exists for the benchmark oracle."}],
        comments=[{"key": "c1", "body": "the antagonist arm should miss this"}],
    )

    findings, _probed = certify_graph_document(document, arm="antagonist")

    assert {finding.locator for finding in findings} == {
        "comments[c1].body",
        "nodes[KNOW-9].body",
    }
    assert {finding.marker for finding in findings} == {"antagonist", "benchmark", "oracle"}


def test_challenge_text_may_not_contain_its_own_answer() -> None:
    manifest = ChallengeManifestV1.model_validate(
        {
            "challenge_id": "example-challenge",
            "title": "Example",
            "description": "Do the thing.",
            "prompt": "Do the thing, and remember to update the changelog too.",
            "solution_markers": ["remember to update the changelog"],
            "allowed_output_paths": ["/workspace/src/main.rs"],
            "verifier": {
                "kind": "script",
                "runtime": "posix-shell",
                "asset": {"path": "verifier.sh", "sha256": "a" * 64, "media_type": "text/plain"},
            },
        }
    )

    findings = certify_challenge_manifest(manifest)

    assert [(f.locator, f.kind) for f in findings] == [("prompt", KIND_SOLUTION_LEAK)]


def test_solution_markers_must_be_trimmed_distinctive_and_unique() -> None:
    base = {
        "challenge_id": "example-challenge",
        "title": "Example",
        "description": "Do the thing.",
        "prompt": "Do the thing.",
        "allowed_output_paths": ["/workspace/src/main.rs"],
        "verifier": {
            "kind": "script",
            "runtime": "posix-shell",
            "asset": {"path": "verifier.sh", "sha256": "a" * 64, "media_type": "text/plain"},
        },
    }
    for markers in (["changelog"], [" a distinctive marker "], ["a distinctive marker"] * 2):
        with pytest.raises(ValidationError):
            ChallengeManifestV1.model_validate({**base, "solution_markers": markers})


def test_redaction_removes_matches_prunes_directories_and_remanifests(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _write(repository, "src/main.rs", "fn main() {}\n")
    _write(repository, "CLAUDE.md", "project memory\n")
    _write(repository, ".claude/skills/dev/SKILL.md", "skill\n")
    _run_git(repository)

    destination = tmp_path / "project"
    corpus = materialize_pinned_tree(repository, _head(repository), destination)
    assert len(corpus.entries) == 3

    redacted, records = redact_packaged_context(corpus)

    assert {record.path for record in records} == {"CLAUDE.md", ".claude/skills/dev/SKILL.md"}
    assert {record.kind for record in records} == {KIND_AUTO_LOADED_CONTEXT}
    assert [entry.path for entry in redacted.entries] == ["src/main.rs"]
    assert redacted.tree_sha256 != corpus.tree_sha256
    # Empty parents left behind would themselves be a path-shaped hint.
    assert not (destination / ".claude").exists()
    assert certify_project_tree(destination)[0] == ()


def _run_git(repository: Path) -> None:
    import subprocess

    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"], cwd=repository, check=True, env=environment
    )


def _head(repository: Path) -> str:
    import subprocess

    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_missing_inputs_are_errors_rather_than_skipped_checks(tmp_path: Path) -> None:
    with pytest.raises(CertificationInputError, match="never skipped"):
        certify_packaged_inputs(corpus_root=tmp_path / "absent")
    with pytest.raises(CertificationInputError, match="supplied together"):
        certify_packaged_inputs(corpus_root=None, fixture_root=tmp_path)


def test_a_graph_artifact_that_does_not_match_its_digest_is_an_error(tmp_path: Path) -> None:
    fixture_root = tmp_path / "fixture"
    _write(fixture_root, "corpora/evidence.json", "[]")
    _write(fixture_root, "graphs/faithful.json", json.dumps(_graph()))
    manifest = {
        "schema_version": 1,
        "fixture_id": "example-v1",
        "cohort_id": "example",
        "evidence_corpus": {
            "path": "corpora/evidence.json",
            "sha256": "b" * 64,
            "media_type": "application/json",
            "ordering": "chronological ascending; no truncation",
        },
        "graphs": {
            "faithful": {
                "path": "graphs/faithful.json",
                "sha256": "c" * 64,
                "media_type": "application/json",
                "graph_format_version": "v2",
                "backend_compatibility_id": "local",
                "embedding_model_id": "model",
                "embedding_dimensions": 8,
            }
        },
    }
    fixture = load_fixture(_write(fixture_root, "fixture.json", json.dumps(manifest)))

    with pytest.raises(CertificationInputError, match="declared digest"):
        certify_packaged_inputs(corpus_root=None, fixture_root=fixture_root, fixture=fixture)


def test_certification_raises_by_default_and_reports_a_receipt_otherwise(tmp_path: Path) -> None:
    _write(tmp_path, "CLAUDE.md", "memory\n")

    with pytest.raises(CertificationError, match="not certifiable"):
        certify_packaged_inputs(corpus_root=tmp_path)

    receipt = certify_packaged_inputs(corpus_root=tmp_path, raise_on_finding=False)
    assert receipt.certified is False
    assert receipt.surfaces_probed == ("corpus",)
    digest = write_certification_receipt(receipt, tmp_path / "out" / "certification.json")
    persisted = json.loads((tmp_path / "out" / "certification.json").read_text())
    assert persisted["zero_model"] is True
    assert persisted["findings"][0]["kind"] == KIND_AUTO_LOADED_CONTEXT
    assert len(digest) == 64


def test_checked_in_fixtures_and_challenges_certify_clean() -> None:
    """Every authored graph arm and challenge in the repository is certifiable."""

    challenges = tuple(loaded.manifest for loaded in load_challenge_catalog())
    for fixture_path in sorted(FIXTURE_ROOT.rglob("fixture.json")):
        receipt = certify_packaged_inputs(
            corpus_root=None,
            fixture_root=fixture_path.parent,
            fixture=load_fixture(fixture_path),
            challenges=challenges,
            raise_on_finding=False,
        )
        assert receipt.certified, f"{fixture_path}: {[f.describe() for f in receipt.findings]}"
        assert receipt.arms


def test_pinned_corpus_certifies_only_after_the_declared_redaction(tmp_path: Path) -> None:
    """The real packaged corpus is certified, and never quietly not certified.

    A missing sibling checkout fails this test instead of skipping it: a
    certification suite that silently covers nothing still reports success,
    which is worse than having none. Set STELE_BENCH_ALLOW_MISSING_CORPUS=1
    to opt out deliberately, and accept that the corpus went unchecked.
    """

    if not PINNED_CORPUS.is_dir():
        if os.environ.get("STELE_BENCH_ALLOW_MISSING_CORPUS") == "1":
            pytest.skip("pinned corpus checkout absent and explicitly opted out")
        pytest.fail(
            f"pinned corpus checkout is unavailable at {PINNED_CORPUS}; clone it or set "
            "STELE_BENCH_ALLOW_MISSING_CORPUS=1 to deliberately leave it uncertified"
        )

    fixture_path = FIXTURE_ROOT / "dotstate-v1" / "fixture.json"
    challenges = tuple(loaded.manifest for loaded in load_challenge_catalog())
    corpus = materialize_pinned_tree(PINNED_CORPUS, PINNED_REVISION, tmp_path / "project")

    unredacted = certify_packaged_inputs(corpus_root=corpus.destination, raise_on_finding=False)
    assert not unredacted.certified
    assert KIND_AUTO_LOADED_CONTEXT in _kinds(unredacted.findings)

    corpus, redactions = redact_packaged_context(corpus)
    receipt = certify_packaged_inputs(
        corpus_root=corpus.destination,
        fixture_root=fixture_path.parent,
        fixture=load_fixture(fixture_path),
        challenges=challenges,
        corpus_tree_sha256=corpus.tree_sha256,
        redactions=redactions,
        raise_on_finding=False,
    )
    assert receipt.certified, [finding.describe() for finding in receipt.findings]
    assert receipt.surfaces_probed == ("corpus", "evidence-corpus", "graph", "challenge")
    assert {record.path for record in receipt.redactions} >= {"CLAUDE.md", ".stele/project.json"}
