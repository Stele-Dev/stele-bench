from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import stele_bench.corpus as corpus_module
from stele_bench.corpus import (
    ArchiveSafetyError,
    BlindnessAuditConfig,
    BlindnessAuditError,
    ContextAuditError,
    CorpusError,
    DestinationError,
    MaterializedCorpus,
    PinnedRevisionError,
    _archive_timeout_error,
    _safe_member_path,
    _tracked_file_modes,
    audit_effective_context,
    materialize_pinned_history,
    materialize_pinned_tree,
    prepare_pinned_repository,
    rescan_materialized_tree,
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", os.fspath(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "src").mkdir()
    (repo / "src" / "app.txt").write_text("first version\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "first")
    first = git(repo, "rev-parse", "HEAD")
    (repo / "src" / "app.txt").write_text("second version\n", encoding="utf-8")
    git(repo, "commit", "-am", "second")
    (repo / "dirty.txt").write_text("host worktree only\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("ignored host file\n", encoding="utf-8")
    return repo, first


def test_prepare_pinned_repository_clones_and_reuses_exact_commit(tmp_path: Path) -> None:
    source, first = repository(tmp_path)
    cache = tmp_path / "cache"

    prepared = prepare_pinned_repository(source.as_uri(), first, cache)
    repeated = prepare_pinned_repository(source.as_uri(), first, cache)
    materialized = materialize_pinned_tree(prepared, first, tmp_path / "materialized")

    assert prepared == repeated
    assert materialized.source_commit == first
    assert (materialized.destination / "src" / "app.txt").read_text() == "first version\n"


def test_prepare_pinned_repository_rejects_unpinned_revision(tmp_path: Path) -> None:
    source, _ = repository(tmp_path)

    with pytest.raises(PinnedRevisionError, match="full 40- or 64-character"):
        prepare_pinned_repository(source.as_uri(), "HEAD", tmp_path / "cache")


def test_pinned_older_commit_ignores_dirty_and_untracked_worktree(tmp_path: Path) -> None:
    repo, first = repository(tmp_path)
    corpus = materialize_pinned_tree(repo, first, tmp_path / "out")
    assert corpus.source_commit == first
    assert (corpus.destination / "src/app.txt").read_text() == "first version\n"
    assert not (corpus.destination / "dirty.txt").exists()
    assert not (corpus.destination / "ignored.txt").exists()
    assert not (corpus.destination / ".git").exists()


def test_bad_or_non_commit_revision_is_rejected(tmp_path: Path) -> None:
    repo, _ = repository(tmp_path)
    with pytest.raises(PinnedRevisionError):
        materialize_pinned_tree(repo, "does-not-exist", tmp_path / "bad")
    assert not (tmp_path / "bad").exists()
    with pytest.raises(PinnedRevisionError):
        materialize_pinned_tree(repo, "HEAD", tmp_path / "head")
    full = git(repo, "rev-parse", "HEAD")
    with pytest.raises(PinnedRevisionError):
        materialize_pinned_tree(repo, full[:12], tmp_path / "abbrev")
    blob = git(repo, "hash-object", "src/app.txt")
    with pytest.raises(PinnedRevisionError):
        materialize_pinned_tree(repo, blob, tmp_path / "blob")


def test_digest_is_deterministic_and_destination_is_never_overwritten(tmp_path: Path) -> None:
    repo, first = repository(tmp_path)
    first_copy = materialize_pinned_tree(repo, first, tmp_path / "one")
    second_copy = materialize_pinned_tree(repo, first, tmp_path / "two")
    assert first_copy.entries == second_copy.entries
    assert first_copy.tree_sha256 == second_copy.tree_sha256
    with pytest.raises(DestinationError):
        materialize_pinned_tree(repo, first, first_copy.destination)


def test_pinned_history_preserves_ancestors_and_excludes_future_commit(
    tmp_path: Path,
) -> None:
    repo, first = repository(tmp_path)
    future = git(repo, "rev-parse", "HEAD")
    plain = materialize_pinned_tree(repo, first, tmp_path / "plain")
    retained = materialize_pinned_history(
        repo,
        first,
        tmp_path / "retained",
        forbidden_object_ids=(future,),
    )

    assert retained.history is not None
    assert retained.history.head_tree == git(repo, "rev-parse", f"{first}^{{tree}}")
    assert retained.history.commit_count == 1
    assert retained.tree_sha256 == plain.tree_sha256
    assert retained.entries == plain.entries
    assert (retained.destination / ".git").is_dir()
    assert git(retained.destination, "rev-parse", "HEAD") == first
    assert git(retained.destination, "log", "-1", "--format=%s") == "first"
    assert git(retained.destination, "remote") == ""
    assert git(retained.destination, "status", "--porcelain=v1") == ""
    missing = subprocess.run(
        ["git", "-C", os.fspath(retained.destination), "cat-file", "-e", future],
        check=False,
        capture_output=True,
    )
    assert missing.returncode != 0
    assert all(not entry.path.startswith(".git/") for entry in retained.entries)
    assert audit_effective_context(retained) == ()


def test_pinned_history_is_content_identical_across_materializations(tmp_path: Path) -> None:
    repo, first = repository(tmp_path)
    one = materialize_pinned_history(repo, first, tmp_path / "one")
    two = materialize_pinned_history(repo, first, tmp_path / "two")

    def git_files(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted((root / ".git").rglob("*"))
            if path.is_file()
        }

    assert one.history == two.history
    assert git_files(one.destination) == git_files(two.destination)
    rescanned = rescan_materialized_tree(one)
    assert rescanned.history == one.history
    assert rescanned.entries == one.entries


def test_pinned_history_rejects_a_forbidden_reachable_commit(tmp_path: Path) -> None:
    repo, first = repository(tmp_path)
    future = git(repo, "rev-parse", "HEAD")
    with pytest.raises(CorpusError, match="forbidden future commit"):
        materialize_pinned_history(
            repo,
            future,
            tmp_path / "out",
            forbidden_object_ids=(first,),
        )
    assert not (tmp_path / "out").exists()


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "escape").symlink_to("../../outside")
    git(repo, "add", "escape")
    git(repo, "commit", "-m", "unsafe link")
    with pytest.raises(ArchiveSafetyError, match="escapes destination"):
        materialize_pinned_tree(repo, git(repo, "rev-parse", "HEAD"), tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("name", ["a//b", "a/./b", "a/../b", "/absolute"])
def test_alternate_archive_path_spellings_are_rejected(name: str) -> None:
    with pytest.raises(ArchiveSafetyError):
        _safe_member_path(name, is_directory=False)


def test_executable_mode_is_preserved_and_part_of_manifest_identity(tmp_path: Path) -> None:
    repo, first = repository(tmp_path)
    script = repo / "script.sh"
    script.write_text("#!/bin/sh\ntrue\n", encoding="utf-8")
    script.chmod(0o755)
    git(repo, "add", "script.sh")
    git(repo, "commit", "-m", "add script")
    commit = git(repo, "rev-parse", "HEAD")
    executable = materialize_pinned_tree(repo, commit, tmp_path / "executable")
    older = materialize_pinned_tree(repo, first, tmp_path / "older")
    assert (executable.destination / "script.sh").stat().st_mode & 0o777 == 0o755
    entry = next(item for item in executable.entries if item.path == "script.sh")
    assert entry.mode == 0o755
    assert executable.tree_sha256 != older.tree_sha256


def test_blindness_audit_reports_planted_paths_and_content(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "benchmark").mkdir(parents=True)
    (root / "benchmark" / "notes.txt").write_text("this is an oracle\n", encoding="utf-8")
    violations = audit_effective_context(root, raise_on_violation=False)
    assert {(v.kind, v.marker) for v in violations} >= {
        ("path", "benchmark"),
        ("content", "oracle"),
    }
    with pytest.raises(BlindnessAuditError) as error:
        audit_effective_context(root)
    assert error.value.violations == violations


def test_blindness_audit_avoids_eval_substring_false_positives(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "evaluation").mkdir()
    (root / "evaluation" / "readme.txt").write_text("evaluation is legitimate\n", encoding="utf-8")
    config = BlindnessAuditConfig(
        forbidden_path_components=("eval",),
        forbidden_content_markers=("eval",),
    )
    assert audit_effective_context(root, config, raise_on_violation=False) == ()


def test_blindness_audit_respects_bounded_content_reads(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "large.txt").write_text("clean\n" * 100 + "oracle\n", encoding="utf-8")
    config = BlindnessAuditConfig(forbidden_content_markers=("oracle",), max_bytes_per_file=10)
    assert audit_effective_context(root, config, raise_on_violation=False) == ()


@pytest.mark.parametrize("name", ["missing", "file"])
def test_blindness_audit_rejects_invalid_roots(tmp_path: Path, name: str) -> None:
    root = tmp_path / name
    if name == "file":
        root.write_text("not a tree", encoding="utf-8")
    with pytest.raises(ContextAuditError):
        audit_effective_context(root)


def test_blindness_audit_rejects_redirected_materialized_destination(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)
    corpus = MaterializedCorpus("a" * 40, redirected, (), "b" * 64)
    with pytest.raises(ContextAuditError, match="redirected"):
        audit_effective_context(corpus)


def test_archive_timeout_is_typed() -> None:
    error = _archive_timeout_error("a" * 40)
    assert isinstance(error, CorpusError)
    assert "timed out" in str(error)


def test_tar_stream_read_error_is_wrapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, first = repository(tmp_path)

    def broken_tar(*_args: object, **_kwargs: object) -> None:
        raise corpus_module.tarfile.ReadError("broken stream")

    monkeypatch.setattr(corpus_module.tarfile, "open", broken_tar)
    with pytest.raises(ArchiveSafetyError, match="could not safely read"):
        materialize_pinned_tree(repo, first, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_stream_timeout_is_wrapped_without_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, first = repository(tmp_path)

    class ImmediateTimer:
        def __init__(self, _seconds: float, callback: object) -> None:
            self.callback = callback

        def start(self) -> None:
            self.callback()  # type: ignore[operator]

        def cancel(self) -> None:
            pass

    monkeypatch.setattr(corpus_module.threading, "Timer", ImmediateTimer)
    with pytest.raises(CorpusError, match="timed out"):
        materialize_pinned_tree(repo, first, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_unsupported_git_tree_mode_is_rejected(tmp_path: Path) -> None:
    repo, first = repository(tmp_path)
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{first},nested")
    tree = git(repo, "write-tree")
    commit = git(repo, "commit-tree", tree, "-p", first, "-m", "submodule")
    with pytest.raises(ArchiveSafetyError, match="unsupported Git tree mode"):
        _tracked_file_modes(repo, commit)
