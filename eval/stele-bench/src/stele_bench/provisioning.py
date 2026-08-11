"""Provisioning boundary and the test-only in-memory implementation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from .contracts import (
    ArtifactDescriptor,
    CanonicalGraphState,
    FixtureManifestV1,
    GraphArm,
    GraphDescriptor,
    ProvisionedProject,
    RestoreResult,
    RetrievalProbe,
    RetrievalProbeKind,
    RunIdentity,
    SnapshotResult,
)


class ProvisionerCommandError(RuntimeError):
    """Base error which retains only bounded, operator-safe stderr."""

    def __init__(self, message: str, stderr: bytes = b"") -> None:
        super().__init__(message)
        self.stderr = stderr.decode("utf-8", "replace")


class ProvisionerTimeoutError(ProvisionerCommandError):
    pass


class ProvisionerExitError(ProvisionerCommandError):
    pass


class ProvisionerOutputTooLargeError(ProvisionerCommandError):
    pass


class ProvisionerMalformedResponseError(ProvisionerCommandError):
    pass


class ProvisionerResponseContractError(ProvisionerCommandError):
    pass


class ProvisionerSpawnError(ProvisionerCommandError):
    pass


class ExternalCommandProvisioner:
    """Schema-blind JSON-over-stdin adapter for a backend-specific provisioner.

    The configured argv prefix receives no repository path.  It is invoked as
    ``<prefix> restore|snapshot|destroy`` with exactly one JSON request on stdin and
    exactly one JSON object on stdout.  Restore gets ``fixture``, ``graph_arm``,
    ``run_identity`` and resolved fixture-relative artifact paths; snapshot/destroy
    get ``project``.  Responses are the corresponding contract object, while destroy
    may return ``{"ok": true}``.
    """

    def __init__(
        self,
        argv_prefix: tuple[str, ...] | list[str],
        fixture_root: Path,
        *,
        timeout_seconds: float = 30,
        max_output_bytes: int = 1_000_000,
    ) -> None:
        if not argv_prefix or timeout_seconds <= 0 or max_output_bytes <= 0:
            raise ValueError("argv_prefix, timeout_seconds, and max_output_bytes must be positive")
        self._argv_prefix, self._fixture_root = tuple(argv_prefix), fixture_root.resolve()
        self._timeout, self._limit = timeout_seconds, max_output_bytes

    def _artifact(self, descriptor: ArtifactDescriptor) -> str:
        path = descriptor.path
        resolved = (self._fixture_root / path).resolve()
        try:
            resolved.relative_to(self._fixture_root)
        except ValueError as error:
            raise ValueError(f"fixture artifact escapes fixture root: {path}") from error
        if not resolved.is_file():
            raise ProvisionerCommandError(f"fixture artifact is missing: {path}")
        hasher = hashlib.sha256()
        with resolved.open("rb") as artifact:
            while chunk := artifact.read(1024 * 1024):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        if digest != descriptor.sha256:
            raise ProvisionerCommandError(f"fixture artifact digest does not match: {path}")
        return str(resolved)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 1)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def _read_bounded(self, stream: asyncio.StreamReader) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while chunk := await stream.read(min(65536, self._limit + 1 - total)):
            total += len(chunk)
            if total > self._limit:
                raise ProvisionerOutputTooLargeError("command output exceeded byte limit")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _call(self, operation: str, request: dict[str, object]) -> dict[str, object]:
        try:
            process = await asyncio.create_subprocess_exec(
                *self._argv_prefix,
                operation,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise ProvisionerSpawnError(f"could not start {operation} command") from error
        payload = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()

        async def communicate_lifecycle() -> tuple[bytes, bytes]:
            assert (
                process.stdin is not None
                and process.stdout is not None
                and process.stderr is not None
            )
            process.stdin.write(payload)
            await process.stdin.drain()
            process.stdin.close()
            stdout, stderr = await asyncio.gather(
                self._read_bounded(process.stdout), self._read_bounded(process.stderr)
            )
            await process.wait()
            return stdout, stderr

        try:
            stdout, stderr = await asyncio.wait_for(
                communicate_lifecycle(),
                self._timeout,
            )
        except ProvisionerOutputTooLargeError as error:
            await self._terminate(process)
            raise ProvisionerOutputTooLargeError(str(error), b"") from error
        except TimeoutError as error:
            await self._terminate(process)
            raise ProvisionerTimeoutError(f"{operation} timed out", b"") from error
        except (BrokenPipeError, ConnectionError, OSError) as error:
            await self._terminate(process)
            raise ProvisionerCommandError(f"{operation} command I/O failed", b"") from error
        if process.returncode:
            raise ProvisionerExitError(
                f"{operation} exited with status {process.returncode}", stderr
            )
        try:
            decoded = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise ProvisionerMalformedResponseError(
                f"{operation} did not return one JSON object", stderr
            ) from error
        if not isinstance(decoded, dict):
            raise ProvisionerMalformedResponseError(
                f"{operation} response must be a JSON object", stderr
            )
        return decoded

    async def restore(
        self, fixture: FixtureManifestV1, arm: GraphArm, identity: RunIdentity
    ) -> ProvisionedProject:
        if arm not in fixture.graphs:
            raise ValueError(f"fixture does not provide graph arm: {arm}")
        response = await self._call(
            "restore",
            {
                "fixture": fixture.model_dump(mode="json"),
                "graph_arm": arm.value,
                "run_identity": identity.model_dump(mode="json"),
                "artifacts": {
                    "evidence_corpus": self._artifact(fixture.evidence_corpus),
                    "graph": self._artifact(fixture.graphs[arm]),
                },
            },
        )
        try:
            if "project" in response:
                return RestoreResult.model_validate(response).project
            return ProvisionedProject.model_validate(response)
        except ValidationError as error:
            raise ProvisionerResponseContractError(
                "restore response violates ProvisionedProject", b""
            ) from error

    async def restore_with_receipt(
        self, fixture: FixtureManifestV1, arm: GraphArm, identity: RunIdentity
    ) -> RestoreResult:
        if arm not in fixture.graphs:
            raise ValueError(f"fixture does not provide graph arm: {arm}")
        response = await self._call(
            "restore",
            {
                "fixture": fixture.model_dump(mode="json"),
                "graph_arm": arm.value,
                "run_identity": identity.model_dump(mode="json"),
                "artifacts": {
                    "evidence_corpus": self._artifact(fixture.evidence_corpus),
                    "graph": self._artifact(fixture.graphs[arm]),
                },
            },
        )
        try:
            return RestoreResult.model_validate(response)
        except ValidationError as error:
            raise ProvisionerResponseContractError(
                "restore response violates RestoreResult", b""
            ) from error

    async def snapshot(self, project: ProvisionedProject) -> CanonicalGraphState:
        response = await self._call("snapshot", {"project": project.model_dump(mode="json")})
        try:
            if "state" in response:
                return SnapshotResult.model_validate(response).state
            return CanonicalGraphState.model_validate(response)
        except ValidationError as error:
            raise ProvisionerResponseContractError(
                "snapshot response violates CanonicalGraphState", b""
            ) from error

    async def snapshot_with_receipt(self, project: ProvisionedProject) -> SnapshotResult:
        response = await self._call("snapshot", {"project": project.model_dump(mode="json")})
        try:
            return SnapshotResult.model_validate(response)
        except ValidationError as error:
            raise ProvisionerResponseContractError(
                "snapshot response violates SnapshotResult", b""
            ) from error

    async def destroy(self, project: ProvisionedProject) -> None:
        response = await self._call("destroy", {"project": project.model_dump(mode="json")})
        if response != {"ok": True}:
            raise ProvisionerResponseContractError("destroy response must be {'ok': true}", b"")


class Provisioner(Protocol):
    async def restore(
        self, fixture: FixtureManifestV1, arm: GraphArm, identity: RunIdentity
    ) -> ProvisionedProject: ...

    async def restore_with_receipt(
        self, fixture: FixtureManifestV1, arm: GraphArm, identity: RunIdentity
    ) -> RestoreResult: ...

    async def snapshot(self, project: ProvisionedProject) -> CanonicalGraphState: ...

    async def snapshot_with_receipt(self, project: ProvisionedProject) -> SnapshotResult: ...

    async def destroy(self, project: ProvisionedProject) -> None: ...


class FakeProvisioner:
    """Test-only provisioner that keeps each fake project isolated in memory."""

    def __init__(self) -> None:
        self._projects: dict[str, ProvisionedProject] = {}
        self._next_id = 0

    @property
    def projects(self) -> Mapping[str, ProvisionedProject]:
        return dict(self._projects)

    async def restore(
        self, fixture: FixtureManifestV1, arm: GraphArm, identity: RunIdentity
    ) -> ProvisionedProject:
        if arm not in fixture.graphs:
            raise ValueError(f"fixture does not provide graph arm: {arm}")
        self._next_id += 1
        project = ProvisionedProject(
            project_id=f"fake-project-{self._next_id}",
            fixture_id=fixture.fixture_id,
            run_id=identity.run_id,
            graph_arm=arm,
        )
        self._projects[project.project_id] = project
        return project

    async def restore_with_receipt(
        self, fixture: FixtureManifestV1, arm: GraphArm, identity: RunIdentity
    ) -> RestoreResult:
        return RestoreResult(
            project=await self.restore(fixture, arm, identity),
            queue_before_count=0,
            restore_seconds=0,
        )

    async def snapshot(self, project: ProvisionedProject) -> CanonicalGraphState:
        if project.project_id not in self._projects:
            raise KeyError(project.project_id)
        return CanonicalGraphState(
            fixture_id=project.fixture_id,
            project_id=project.project_id,
            graph_arm=project.graph_arm,
            canonical_state=GraphDescriptor(
                path=f"snapshots/{project.project_id}.json",
                sha256="0" * 64,
                media_type="application/json",
                graph_format_version="fake-v1",
                backend_compatibility_id="fake-memory-v1",
                embedding_model_id="fake-embedding-v1",
                embedding_dimensions=1,
            ),
            declared_node_count=0,
            declared_edge_count=0,
        )

    async def snapshot_with_receipt(self, project: ProvisionedProject) -> SnapshotResult:
        return SnapshotResult(
            state=await self.snapshot(project),
            probes=(
                RetrievalProbe(name="fts", kind=RetrievalProbeKind.FTS, stable_ids=()),
                RetrievalProbe(
                    name="semantic", kind=RetrievalProbeKind.SEMANTIC, stable_ids=(), scores=()
                ),
                RetrievalProbe(name="graph", kind=RetrievalProbeKind.GRAPH, stable_ids=()),
            ),
            queue_after_count=0,
            snapshot_seconds=0,
        )

    async def destroy(self, project: ProvisionedProject) -> None:
        self._projects.pop(project.project_id, None)
