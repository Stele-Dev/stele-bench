"""Pinned source-tree materialization and evaluated-context blindness checks.

This module deliberately works only with Git objects.  A benchmark corpus must
not inherit a caller's worktree, ignored files, or repository metadata.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class CorpusError(RuntimeError):
    """Base error for safe corpus construction failures."""


class PinnedRevisionError(CorpusError):
    """The supplied revision does not resolve to a commit in the local repository."""


class DestinationError(CorpusError):
    """The requested destination is not an empty directory or absent path."""


class ArchiveSafetyError(CorpusError):
    """A Git archive contains an entry that cannot be extracted safely."""


class ContextAuditError(CorpusError):
    """The tree selected for an effective-context audit is not trustworthy."""


@dataclass(frozen=True)
class CorpusEntry:
    """One portable entry in a materialized Git tree."""

    path: str
    sha256: str
    kind: str
    mode: int


@dataclass(frozen=True)
class GitHistoryIdentity:
    """Content identity for the Git object graph reachable from one base."""

    head_tree: str
    commit_count: int
    object_count: int
    object_ids_sha256: str


@dataclass(frozen=True)
class MaterializedCorpus:
    """The immutable identity of a materialized source tree."""

    source_commit: str
    destination: Path
    entries: tuple[CorpusEntry, ...]
    tree_sha256: str
    history: GitHistoryIdentity | None = None

    @property
    def manifest(self) -> Mapping[str, str]:
        """A deterministic, relative-POSIX mapping for simple consumers."""

        return {entry.path: entry.sha256 for entry in self.entries}


@dataclass(frozen=True)
class BlindnessAuditConfig:
    """Markers that must never reach an evaluated agent's project tree.

    Path markers match whole components.  Content markers match token-like
    occurrences, so a marker ``eval`` does not flag ordinary prose such as
    ``evaluation``.
    """

    forbidden_path_components: tuple[str, ...] = (
        ".git",
        "benchmark",
        "benchmarks",
        "eval",
        "evals",
        "oracle",
        "answers",
        "answer-artifacts",
    )
    forbidden_content_markers: tuple[str, ...] = (
        "benchmark",
        "oracle",
        "answer artifact",
        "no-stele",
        "full-context",
        "know-it-all",
        "antagonist",
    )
    max_bytes_per_file: int = 128 * 1024

    def __post_init__(self) -> None:
        if self.max_bytes_per_file < 1:
            raise ValueError("max_bytes_per_file must be positive")
        if any(not marker.strip() for marker in self.forbidden_path_components):
            raise ValueError("forbidden path components must not be blank")
        if any(not marker.strip() for marker in self.forbidden_content_markers):
            raise ValueError("forbidden content markers must not be blank")


@dataclass(frozen=True)
class BlindnessViolation:
    path: str
    kind: str
    marker: str
    line: int | None = None


class BlindnessAuditError(CorpusError):
    """The materialized project reveals benchmark-only context."""

    def __init__(self, violations: Iterable[BlindnessViolation]) -> None:
        self.violations = tuple(violations)
        details = ", ".join(f"{v.kind}:{v.path}:{v.marker}" for v in self.violations)
        super().__init__(f"evaluated context is not blind: {details}")


_FULL_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_ARCHIVE_TIMEOUT_SECONDS = 60
_HISTORY_TIMEOUT_SECONDS = 120
_MAX_HISTORY_PACK_BYTES = 256 * 1024 * 1024
_MAX_STDERR_BYTES = 16 * 1024


def prepare_pinned_repository(
    repository_url: str,
    revision: str,
    cache_root: Path,
) -> Path:
    """Clone or refresh a public repository containing one exact commit.

    The returned checkout is only an object source for
    :func:`materialize_pinned_tree`; its worktree is never handed to an agent.
    """

    if not repository_url.strip():
        raise CorpusError("repository URL must not be blank")
    _validate_object_id(revision)
    cache_root = cache_root.expanduser().resolve(strict=False)
    cache_key = hashlib.sha256(repository_url.encode("utf-8")).hexdigest()[:20]
    repository = cache_root / f"source-{cache_key}"
    if repository.exists() and not (repository / ".git").is_dir():
        raise CorpusError(f"source cache is not a Git repository: {repository}")

    if not repository.exists():
        cache_root.mkdir(parents=True, exist_ok=True)
        temporary = cache_root / f".{repository.name}.{os.getpid()}.cloning"
        if temporary.exists():
            shutil.rmtree(temporary)
        try:
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--filter=blob:none",
                    "--no-checkout",
                    "--origin",
                    "origin",
                    "--",
                    repository_url,
                    os.fspath(temporary),
                ],
                check=True,
                capture_output=True,
                timeout=_HISTORY_TIMEOUT_SECONDS,
            )
            try:
                os.replace(temporary, repository)
            except OSError:
                if not repository.is_dir():
                    raise
        except (OSError, subprocess.SubprocessError) as error:
            raise CorpusError(f"could not clone source repository {repository_url!r}") from error
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    try:
        _resolve_commit(repository, revision)
    except PinnedRevisionError:
        try:
            subprocess.run(
                [
                    "git",
                    "-C",
                    os.fspath(repository),
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    "--depth=1",
                    "origin",
                    revision,
                ],
                check=True,
                capture_output=True,
                timeout=_HISTORY_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise CorpusError(
                f"could not fetch pinned commit {revision!r} from {repository_url!r}"
            ) from error
        _resolve_commit(repository, revision)
    return repository


def materialize_pinned_tree(
    repository: Path, revision: str, destination: Path
) -> MaterializedCorpus:
    """Extract exactly one local Git commit into an empty destination.

    ``destination`` may be absent or an empty directory.  Existing files are
    never overwritten.  Git's object database, rather than the source
    worktree, is the sole input.
    """

    repository = repository.resolve()
    destination = destination.resolve(strict=False)
    commit = _resolve_commit(repository, revision)
    tracked_modes = _tracked_file_modes(repository, commit)
    created_destination = _prepare_destination(destination)
    try:
        _extract_git_archive(repository, commit, destination, tracked_modes)
        entries = _manifest_entries(destination)
        return MaterializedCorpus(
            source_commit=commit,
            destination=destination,
            entries=entries,
            tree_sha256=_tree_digest(entries),
        )
    except BaseException:
        if created_destination:
            shutil.rmtree(destination)
        raise


def materialize_pinned_history(
    repository: Path,
    revision: str,
    destination: Path,
    *,
    forbidden_object_ids: Iterable[str] = (),
) -> MaterializedCorpus:
    """Materialize one commit plus only the Git objects reachable from it.

    The worktree is extracted through the same safe archive path as
    :func:`materialize_pinned_tree`. The attached repository has no remotes and
    cannot resolve descendant commits that were not already reachable from the
    registered base. This preserves honest local history without leaking a
    future canonical repair from the source clone.
    """

    repository = repository.resolve()
    destination = destination.resolve(strict=False)
    commit = _resolve_commit(repository, revision)
    forbidden = tuple(_validate_object_id(value) for value in forbidden_object_ids)
    tracked_modes = _tracked_file_modes(repository, commit)
    created_destination = _prepare_destination(destination)
    try:
        _extract_git_archive(repository, commit, destination, tracked_modes)
        _attach_reachable_history(repository, commit, destination)
        history = _history_identity(destination, commit)
        _verify_attached_history(destination, commit, forbidden)
        entries = _manifest_entries(destination, exclude_top_level={".git"})
        return MaterializedCorpus(
            source_commit=commit,
            destination=destination,
            entries=entries,
            tree_sha256=_tree_digest(entries),
            history=history,
        )
    except BaseException:
        if created_destination:
            shutil.rmtree(destination)
        raise


def rescan_materialized_tree(corpus: MaterializedCorpus) -> MaterializedCorpus:
    """Recompute a corpus manifest after a declared, recorded modification.

    Only a caller that already published *what* it changed and *why* — the
    certification redaction pass — may use this.  Everything downstream still
    compares the tree against this manifest and fails closed, so an undeclared
    edit remains detectable.
    """

    excluded = {".git"} if corpus.history is not None else set()
    entries = _manifest_entries(corpus.destination.resolve(strict=True), exclude_top_level=excluded)
    return MaterializedCorpus(
        source_commit=corpus.source_commit,
        destination=corpus.destination,
        entries=entries,
        tree_sha256=_tree_digest(entries),
        history=corpus.history,
    )


def audit_effective_context(
    corpus: MaterializedCorpus | Path,
    config: BlindnessAuditConfig = BlindnessAuditConfig(),
    *,
    raise_on_violation: bool = True,
) -> tuple[BlindnessViolation, ...]:
    """Inspect paths and bounded UTF-8 text for benchmark-identifying context."""

    root = _audit_root(corpus)
    violations: list[BlindnessViolation] = []
    forbidden_components = {component.casefold() for component in config.forbidden_path_components}
    patterns = {
        marker: re.compile(rf"(?<![A-Za-z0-9]){re.escape(marker)}(?![A-Za-z0-9])", re.IGNORECASE)
        for marker in config.forbidden_content_markers
    }
    paths = root.rglob("*")
    for path in sorted(paths, key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if isinstance(corpus, MaterializedCorpus) and corpus.history is not None:
            if PurePosixPath(relative).parts[:1] == (".git",):
                continue
        for component in PurePosixPath(relative).parts:
            if component.casefold() in forbidden_components:
                violations.append(BlindnessViolation(relative, "path", component))
        if not path.is_file() or path.is_symlink():
            continue
        with path.open("rb") as source:
            payload = source.read(config.max_bytes_per_file)
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for marker, pattern in patterns.items():
            match = pattern.search(text)
            if match is not None:
                violations.append(
                    BlindnessViolation(
                        relative,
                        "content",
                        marker,
                        text.count("\n", 0, match.start()) + 1,
                    )
                )
    result = tuple(violations)
    if result and raise_on_violation:
        raise BlindnessAuditError(result)
    return result


def _audit_root(corpus: MaterializedCorpus | Path) -> Path:
    declared = corpus.destination if isinstance(corpus, MaterializedCorpus) else corpus
    if not declared.exists():
        raise ContextAuditError(f"audit root does not exist: {declared}")
    if not declared.is_dir():
        raise ContextAuditError(f"audit root is not a directory: {declared}")
    resolved = declared.resolve(strict=True)
    if isinstance(corpus, MaterializedCorpus):
        expected = corpus.destination.absolute()
        if not expected.is_absolute() or resolved != expected:
            raise ContextAuditError(
                "materialized corpus destination was redirected after materialization"
            )
    return resolved


def _prepare_destination(destination: Path) -> bool:
    if destination.exists():
        if not destination.is_dir():
            raise DestinationError(f"destination exists and is not a directory: {destination}")
        if any(destination.iterdir()):
            raise DestinationError(f"destination must be empty: {destination}")
        return False
    destination.mkdir(parents=True, exist_ok=False)
    return True


def _resolve_commit(repository: Path, revision: str) -> str:
    if _FULL_OBJECT_ID.fullmatch(revision) is None:
        raise PinnedRevisionError(
            "revision must be a full 40- or 64-character hexadecimal object ID"
        )
    try:
        completed = subprocess.run(
            ["git", "-C", os.fspath(repository), "rev-parse", "--verify", f"{revision}^{{commit}}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PinnedRevisionError(f"could not resolve pinned commit {revision!r}") from error
    commit = completed.stdout.strip()
    if commit.casefold() != revision.casefold():
        raise PinnedRevisionError("revision must name the exact pinned commit object")
    return commit


def _validate_object_id(value: str) -> str:
    if _FULL_OBJECT_ID.fullmatch(value) is None:
        raise PinnedRevisionError(
            "forbidden object IDs must be full 40- or 64-character hexadecimal IDs"
        )
    return value.casefold()


def _attach_reachable_history(repository: Path, commit: str, destination: Path) -> None:
    """Attach a self-contained object pack rooted only at ``commit``."""

    try:
        subprocess.run(
            ["git", "init", "--quiet", os.fspath(destination)],
            check=True,
            capture_output=True,
            timeout=15,
        )
        subprocess.run(
            [
                "git",
                "-C",
                os.fspath(destination),
                "config",
                "core.logAllRefUpdates",
                "false",
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )
        packed = subprocess.run(
            ["git", "-C", os.fspath(repository), "pack-objects", "--revs", "--stdout"],
            input=f"{commit}\n".encode(),
            check=True,
            capture_output=True,
            timeout=_HISTORY_TIMEOUT_SECONDS,
        )
        if len(packed.stdout) > _MAX_HISTORY_PACK_BYTES:
            raise CorpusError("reachable Git history exceeds the materialization safety limit")
        subprocess.run(
            [
                "git",
                "-C",
                os.fspath(destination),
                "index-pack",
                "--stdin",
                "--fix-thin",
            ],
            input=packed.stdout,
            check=True,
            capture_output=True,
            timeout=_HISTORY_TIMEOUT_SECONDS,
        )
        subprocess.run(
            [
                "git",
                "-C",
                os.fspath(destination),
                "update-ref",
                "refs/heads/work",
                commit,
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )
        subprocess.run(
            [
                "git",
                "-C",
                os.fspath(destination),
                "symbolic-ref",
                "HEAD",
                "refs/heads/work",
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )
        subprocess.run(
            ["git", "-C", os.fspath(destination), "read-tree", commit],
            check=True,
            capture_output=True,
            timeout=15,
        )
        subprocess.run(
            ["git", "-C", os.fspath(destination), "config", "gc.auto", "0"],
            check=True,
            capture_output=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired as error:
        raise CorpusError(
            f"timed out while materializing reachable Git history for {commit}"
        ) from error
    except (OSError, subprocess.SubprocessError) as error:
        raise CorpusError(
            f"could not materialize reachable Git history for pinned commit {commit}"
        ) from error


def _history_identity(repository: Path, commit: str) -> GitHistoryIdentity:
    try:
        tree = subprocess.run(
            ["git", "-C", os.fspath(repository), "rev-parse", f"{commit}^{{tree}}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
        commit_count_text = subprocess.run(
            ["git", "-C", os.fspath(repository), "rev-list", "--count", commit],
            check=True,
            capture_output=True,
            text=True,
            timeout=_HISTORY_TIMEOUT_SECONDS,
        ).stdout.strip()
        object_output = subprocess.run(
            [
                "git",
                "-C",
                os.fspath(repository),
                "rev-list",
                "--objects",
                "--no-object-names",
                commit,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=_HISTORY_TIMEOUT_SECONDS,
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise CorpusError(f"could not identify reachable Git history for {commit}") from error
    object_ids = sorted(set(object_output.splitlines()))
    encoded = "".join(f"{object_id}\n" for object_id in object_ids).encode()
    return GitHistoryIdentity(
        head_tree=tree,
        commit_count=int(commit_count_text),
        object_count=len(object_ids),
        object_ids_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def _verify_attached_history(
    destination: Path, commit: str, forbidden_object_ids: Iterable[str]
) -> None:
    try:
        head = subprocess.run(
            ["git", "-C", os.fspath(destination), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
        if head != commit:
            raise CorpusError(f"materialized Git HEAD changed: {head} != {commit}")
        status = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "-C",
                os.fspath(destination),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
        if status:
            raise CorpusError("materialized Git worktree does not match its pinned HEAD")
        remotes = subprocess.run(
            ["git", "-C", os.fspath(destination), "remote"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
        if remotes.strip():
            raise CorpusError("materialized Git history unexpectedly contains a remote")
        subprocess.run(
            [
                "git",
                "-C",
                os.fspath(destination),
                "fsck",
                "--full",
                "--strict",
                "--no-reflogs",
            ],
            check=True,
            capture_output=True,
            timeout=_HISTORY_TIMEOUT_SECONDS,
        )
        for object_id in forbidden_object_ids:
            present = subprocess.run(
                [
                    "git",
                    "-C",
                    os.fspath(destination),
                    "cat-file",
                    "-e",
                    f"{object_id}^{{commit}}",
                ],
                check=False,
                capture_output=True,
                timeout=15,
            )
            if present.returncode == 0:
                raise CorpusError(
                    f"forbidden future commit is present in materialized history: {object_id}"
                )
    except subprocess.TimeoutExpired as error:
        raise CorpusError(f"timed out while validating Git history for {commit}") from error
    except (OSError, subprocess.SubprocessError) as error:
        raise CorpusError(f"could not validate Git history for {commit}") from error


def _safe_member_path(name: str, *, is_directory: bool) -> PurePosixPath:
    canonical_name = name[:-1] if is_directory and name.endswith("/") else name
    path = PurePosixPath(canonical_name)
    expected = path.as_posix()
    valid_directory_spelling = is_directory and name == f"{expected}/"
    if (
        not canonical_name
        or name.startswith("/")
        or ".." in path.parts
        or "." in path.parts
        or str(path) in {"", "."}
        or (name != expected and not valid_directory_spelling)
    ):
        raise ArchiveSafetyError(f"unsafe archive path: {name!r}")
    return path


def _tracked_file_modes(repository: Path, commit: str) -> Mapping[str, int]:
    """Read Git's executable-bit metadata, which tar archives do not retain."""

    try:
        completed = subprocess.run(
            ["git", "-C", os.fspath(repository), "ls-tree", "-r", "-z", commit],
            check=True,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CorpusError(f"could not read tracked modes for pinned commit {commit}") from error
    modes: dict[str, int] = {}
    for record in completed.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", maxsplit=1)
            raw_mode, _kind, _object_id = metadata.split(maxsplit=2)
            path = _safe_member_path(raw_path.decode("utf-8"), is_directory=False).as_posix()
        except (UnicodeDecodeError, ValueError) as error:
            raise CorpusError("Git returned malformed tree metadata") from error
        if path in modes:
            raise CorpusError(f"Git tree contains duplicate path metadata: {path!r}")
        if raw_mode == b"100644":
            modes[path] = 0o644
        elif raw_mode == b"100755":
            modes[path] = 0o755
        elif raw_mode == b"120000":
            modes[path] = 0o777
        else:
            raise ArchiveSafetyError(
                "unsupported Git tree mode for benchmark corpus: "
                f"{raw_mode.decode(errors='replace')}"
            )
    return modes


def _ensure_safe_parent(destination: Path, path: PurePosixPath) -> Path:
    target = destination.joinpath(*path.parts)
    parent = target.parent
    cursor = destination
    for component in path.parts[:-1]:
        cursor = cursor / component
        if cursor.exists() and cursor.is_symlink():
            raise ArchiveSafetyError(f"archive entry would traverse symlink: {path.as_posix()}")
    parent.mkdir(parents=True, exist_ok=True)
    return target


def _extract_git_archive(
    repository: Path, commit: str, destination: Path, tracked_modes: Mapping[str, int]
) -> None:
    try:
        process = subprocess.Popen(
            ["git", "-C", os.fspath(repository), "archive", "--format=tar", commit],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise CorpusError(f"could not start archive for pinned commit {commit}") from error
    assert process.stdout is not None
    assert process.stderr is not None
    stderr = _BoundedPipeReader(process.stderr)
    stderr.start()
    timed_out = threading.Event()

    def kill_on_timeout() -> None:
        timed_out.set()
        process.kill()

    timeout = threading.Timer(_ARCHIVE_TIMEOUT_SECONDS, kill_on_timeout)
    timeout.start()
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as tar:
            seen: set[str] = set()
            for member in tar:
                path = _safe_member_path(member.name, is_directory=member.isdir())
                canonical = path.as_posix()
                if canonical in seen:
                    raise ArchiveSafetyError(f"duplicate or aliased archive path: {member.name!r}")
                seen.add(canonical)
                _extract_member(tar, member, destination, path, tracked_modes)
        try:
            return_code = process.wait(timeout=1)
        except subprocess.TimeoutExpired as error:
            timed_out.set()
            raise _archive_timeout_error(commit) from error
        if timed_out.is_set():
            raise _archive_timeout_error(commit)
        if return_code != 0:
            message = stderr.text()
            raise CorpusError(f"could not archive pinned commit {commit}: {message}")
    except (CorpusError, subprocess.SubprocessError):
        _terminate_and_reap(process)
        raise
    except (EOFError, OSError, tarfile.TarError) as error:
        _terminate_and_reap(process)
        if timed_out.is_set():
            raise _archive_timeout_error(commit) from error
        raise ArchiveSafetyError(f"could not safely read Git archive for {commit}") from error
    except BaseException:
        _terminate_and_reap(process)
        raise
    finally:
        timeout.cancel()
        stderr.join()


def _archive_timeout_error(commit: str) -> CorpusError:
    return CorpusError(f"timed out while streaming Git archive for pinned commit {commit}")


def _extract_member(
    tar: tarfile.TarFile,
    member: tarfile.TarInfo,
    destination: Path,
    path: PurePosixPath,
    tracked_modes: Mapping[str, int],
) -> None:
    target = _ensure_safe_parent(destination, path)
    if member.isdir():
        target.mkdir(exist_ok=True)
        target.chmod(member.mode & 0o777)
        return
    if member.isreg():
        source = tar.extractfile(member)
        if source is None:
            raise ArchiveSafetyError(f"archive file has no data: {member.name!r}")
        with source, target.open("xb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        try:
            target.chmod(tracked_modes[path.as_posix()])
        except KeyError as error:
            raise ArchiveSafetyError(
                f"archive file is absent from pinned Git tree: {member.name!r}"
            ) from error
        return
    if member.issym():
        if tracked_modes.get(path.as_posix()) != 0o777:
            raise ArchiveSafetyError(
                f"archive symlink is absent from pinned Git tree: {member.name!r}"
            )
        _safe_symlink_target(destination, target, member.linkname)
        target.symlink_to(member.linkname)
        return
    raise ArchiveSafetyError(f"unsupported archive member type: {member.name!r}")


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
    process.wait()


class _BoundedPipeReader(threading.Thread):
    def __init__(self, pipe: io.BufferedReader) -> None:
        super().__init__(daemon=True)
        self._pipe = pipe
        self._chunks: list[bytes] = []
        self._size = 0

    def run(self) -> None:
        while block := self._pipe.read(4096):
            remaining = _MAX_STDERR_BYTES - self._size
            if remaining > 0:
                self._chunks.append(block[:remaining])
                self._size += min(len(block), remaining)

    def text(self) -> str:
        suffix = "…" if self._size == _MAX_STDERR_BYTES else ""
        return b"".join(self._chunks).decode("utf-8", errors="replace").strip() + suffix


def _safe_symlink_target(destination: Path, link: Path, target: str) -> None:
    if not target or Path(target).is_absolute():
        raise ArchiveSafetyError(f"unsafe symlink target for {link.name!r}: {target!r}")
    resolved = (link.parent / target).resolve(strict=False)
    try:
        resolved.relative_to(destination.resolve())
    except ValueError as error:
        raise ArchiveSafetyError(f"symlink escapes destination: {target!r}") from error


def _manifest_entries(
    destination: Path, *, exclude_top_level: set[str] | frozenset[str] = frozenset()
) -> tuple[CorpusEntry, ...]:
    entries: list[CorpusEntry] = []
    for path in sorted(destination.rglob("*"), key=lambda item: item.as_posix()):
        relative_path = path.relative_to(destination)
        if relative_path.parts and relative_path.parts[0] in exclude_top_level:
            continue
        if path.is_dir():
            continue
        relative = relative_path.as_posix()
        if path.is_symlink():
            digest = hashlib.sha256(f"symlink\0{os.readlink(path)}".encode()).hexdigest()
            entries.append(CorpusEntry(relative, digest, "symlink", 0o777))
        elif path.is_file():
            mode = path.stat().st_mode & 0o777
            entries.append(CorpusEntry(relative, _file_sha256(path), "file", mode))
        else:
            raise ArchiveSafetyError(f"unsupported materialized file type: {relative}")
    return tuple(entries)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_digest(entries: Iterable[CorpusEntry]) -> str:
    payload = [
        {"kind": entry.kind, "mode": entry.mode, "path": entry.path, "sha256": entry.sha256}
        for entry in sorted(entries, key=lambda item: item.path)
    ]
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()
