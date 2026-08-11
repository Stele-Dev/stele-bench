"""Scalar-only Harbor configuration entrypoint for the Claude adapter."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Literal

from .harbor_adapter import (
    AttemptState,
    LocalDirtyWorktreeArtifact,
    ReleasedArtifact,
    SteleClaudeCode,
)
from .preflight import ActiveProjectBinding, BindingValidationError

HARBOR_AGENT_IMPORT_PATH = "stele_bench.harbor_entrypoint:ConfiguredSteleClaudeCode"
_MAX_SECRET_JSON_BYTES = 1_048_576


class HarborConfigurationError(ValueError):
    """A scalar Harbor configuration could not be turned into safe adapter inputs."""


def _read_secret_json(path: str, label: str) -> bytes:
    try:
        initial = os.lstat(path)
        if stat.S_ISLNK(initial.st_mode):
            raise HarborConfigurationError(f"{label} configuration must not be a symlink")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except HarborConfigurationError:
        raise
    except OSError as error:
        raise HarborConfigurationError(f"could not read {label} configuration") from error
    try:
        metadata = os.fstat(descriptor)
        if (initial.st_dev, initial.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise HarborConfigurationError(f"{label} configuration changed while opening")
        if not stat.S_ISREG(metadata.st_mode):
            raise HarborConfigurationError(f"{label} configuration must be a regular file")
        if metadata.st_mode & 0o077:
            raise HarborConfigurationError(f"{label} configuration must be owner-only")
        if not metadata.st_mode & stat.S_IRUSR:
            raise HarborConfigurationError(f"{label} configuration must be owner-readable")
        if metadata.st_size > _MAX_SECRET_JSON_BYTES:
            raise HarborConfigurationError(f"{label} configuration exceeds the size limit")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) != metadata.st_size:
            raise HarborConfigurationError(f"could not read {label} configuration")
        return payload
    finally:
        os.close(descriptor)


def _artifact_from_scalars(
    *,
    artifact_path: str,
    artifact_sha256: str,
    artifact_source_kind: Literal["release", "local-dirty-worktree"],
    artifact_release_version: str | None,
) -> ReleasedArtifact | LocalDirtyWorktreeArtifact:
    if artifact_source_kind == "release":
        if artifact_release_version is None or not artifact_release_version.strip():
            raise HarborConfigurationError(
                "artifact_release_version is required for a release artifact"
            )
        return ReleasedArtifact(
            path=Path(artifact_path),
            sha256=artifact_sha256,
            release_version=artifact_release_version,
        )
    if artifact_source_kind == "local-dirty-worktree":
        if artifact_release_version not in (None, ""):
            raise HarborConfigurationError(
                "artifact_release_version is only valid for a release artifact"
            )
        return LocalDirtyWorktreeArtifact(path=Path(artifact_path), sha256=artifact_sha256)
    raise HarborConfigurationError("artifact_source_kind must be release or local-dirty-worktree")


def _make_configured_agent(base_class: type[Any]) -> type[Any]:
    """Keep the production class configurable while tests remain Harbor-free."""

    class ConfiguredSteleClaudeCode(base_class):  # type: ignore[misc, valid-type]
        def __init__(
            self,
            logs_dir: Path,
            *args: Any,
            artifact_path: str,
            artifact_sha256: str,
            artifact_source_kind: Literal["release", "local-dirty-worktree"],
            artifact_release_version: str | None = None,
            credentials_path: str,
            preferences_path: str,
            project_binding_path: str | None = None,
            auto_rag_injection: str = "on",
            project_provisioning: str = "restore",
            project_name: str = "Workspace",
            **kwargs: Any,
        ) -> None:
            try:
                artifact = _artifact_from_scalars(
                    artifact_path=artifact_path,
                    artifact_sha256=artifact_sha256,
                    artifact_source_kind=artifact_source_kind,
                    artifact_release_version=artifact_release_version,
                )
            except ValueError as error:
                if isinstance(error, HarborConfigurationError):
                    raise
                raise HarborConfigurationError("artifact configuration is not valid") from error
            try:
                attempt_state = AttemptState(
                    credentials_json=_read_secret_json(credentials_path, "credentials"),
                    preferences_json=_read_secret_json(preferences_path, "preferences"),
                )
            except ValueError as error:
                raise HarborConfigurationError("attempt configuration is not valid") from error
            if project_provisioning not in ("restore", "create"):
                raise HarborConfigurationError(
                    "project_provisioning must be restore or create"
                )
            # A created project has no binding to hand over: the container makes
            # the project itself and reads the slug back out of the CLI.
            project_binding: ActiveProjectBinding | None = None
            if project_provisioning == "restore":
                if not project_binding_path:
                    raise HarborConfigurationError(
                        "restoring a project requires project_binding_path"
                    )
                try:
                    project_binding = ActiveProjectBinding.from_json_bytes(
                        _read_secret_json(project_binding_path, "project binding")
                    )
                except (ValueError, BindingValidationError) as error:
                    raise HarborConfigurationError("project binding is not valid") from error
            if auto_rag_injection not in ("on", "off"):
                raise HarborConfigurationError("auto_rag_injection must be on or off")
            super().__init__(
                logs_dir,
                *args,
                artifact=artifact,
                attempt_state=attempt_state,
                project_binding=project_binding,
                auto_rag_injection=auto_rag_injection,
                project_provisioning=project_provisioning,
                project_name=project_name,
                **kwargs,
            )

    ConfiguredSteleClaudeCode.__name__ = "ConfiguredSteleClaudeCode"
    ConfiguredSteleClaudeCode.__qualname__ = "ConfiguredSteleClaudeCode"
    return ConfiguredSteleClaudeCode


ConfiguredSteleClaudeCode = _make_configured_agent(SteleClaudeCode)
