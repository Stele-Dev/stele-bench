"""No-cost preparation checks for the first two benchmark conditions.

This module prepares an evaluated project and, when requested, performs only
agent setup.  It never invokes an agent's run method or a model provider.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .contracts import (
    ConditionKind,
    FixtureManifestV1,
    GraphArm,
    RestoreObservation,
    RunIdentity,
)
from .corpus import MaterializedCorpus, audit_effective_context, materialize_pinned_tree
from .harbor_adapter import ArtifactProvenance
from .harbor_entrypoint import HARBOR_AGENT_IMPORT_PATH
from .provisioning import Provisioner


class SmokeCondition(StrEnum):
    """Conditions deliberately supported by the public smoke slice."""

    NO_STELE = "no-stele"
    FAITHFUL = "faithful"


class SmokeMode(StrEnum):
    """Dry-run has no backend side effects; install-only still never runs a model."""

    DRY_RUN = "dry-run"
    INSTALL_ONLY = "install-only"


class SmokeConfigurationError(ValueError):
    """A requested preparation flow lacks a required explicit dependency."""


class SmokeCleanupError(RuntimeError):
    """A restored project could not be removed after a successful preparation."""


class InstallOnlyAgent(Protocol):
    """The narrow Harbor-facing surface used by the smoke runner.

    ``SteleClaudeCode.setup(environment)`` satisfies this protocol.  Keeping the
    run method out of this protocol makes paid model invocation impossible here.
    """

    async def setup(self, environment: Any) -> None: ...


@dataclass(frozen=True)
class InstallOnlySetup:
    """Pair a configured Harbor adapter with its already-created environment."""

    agent: InstallOnlyAgent
    environment: Any

    async def prepare(self) -> None:
        await self.agent.setup(self.environment)


@dataclass(frozen=True)
class SmokeDependencies:
    """Backend and Harbor dependencies are injected, never imported from a repo."""

    provisioner: Provisioner | None = None
    no_stele_install: InstallOnlySetup | None = None
    faithful_install: InstallOnlySetup | None = None


@dataclass(frozen=True)
class SmokeRequest:
    """Inputs for one deterministic materialization and selected condition checks."""

    repository: Path
    revision: str
    evaluated_project: Path
    fixture_root: Path
    fixture: FixtureManifestV1
    identities: Mapping[SmokeCondition, RunIdentity]
    conditions: tuple[SmokeCondition, ...]
    mode: SmokeMode = SmokeMode.DRY_RUN
    integration_provenance: ArtifactProvenance | None = None

    def __post_init__(self) -> None:
        if self.evaluated_project.name != "project":
            raise SmokeConfigurationError(
                "evaluated_project must use the neutral final path component 'project'"
            )
        if not self.conditions:
            raise SmokeConfigurationError("select at least one smoke condition")
        if len(set(self.conditions)) != len(self.conditions):
            raise SmokeConfigurationError("smoke conditions must not be repeated")
        missing = set(self.conditions) - set(self.identities)
        if missing:
            raise SmokeConfigurationError(f"missing identities for: {sorted(missing)}")
        for condition in self.conditions:
            identity = self.identities[condition]
            if identity.condition_id != condition.value:
                raise SmokeConfigurationError(
                    f"identity condition_id must match selected condition: {condition.value}"
                )


@dataclass(frozen=True)
class ExternalProvenance:
    """Portable identity of external inputs; it intentionally has no local paths."""

    source_commit: str
    source_tree_sha256: str
    fixture_id: str
    evidence_corpus_sha256: str
    graph_sha256: str | None
    integration_artifact: ArtifactProvenance | None
    agent_import_path: str | None

    def as_dict(self) -> dict[str, object]:
        artifact = self.integration_artifact
        return {
            "source_commit": self.source_commit,
            "source_tree_sha256": self.source_tree_sha256,
            "fixture_id": self.fixture_id,
            "evidence_corpus_sha256": self.evidence_corpus_sha256,
            "graph_sha256": self.graph_sha256,
            "integration_artifact": (
                None
                if artifact is None
                else {
                    "source_kind": artifact.source_kind,
                    "sha256": artifact.sha256,
                    "release_version": artifact.release_version,
                }
            ),
            "agent_import_path": self.agent_import_path,
        }


@dataclass(frozen=True)
class SmokeReceipt:
    """A no-cost preparation receipt suitable for local diagnostics or CI."""

    condition: SmokeCondition
    condition_kind: ConditionKind
    graph_arm: GraphArm | None
    mode: SmokeMode
    run_id: str
    provenance: ExternalProvenance
    install_only_completed: bool
    restore_observation: RestoreObservation | None
    cleanup_completed: bool
    access_binding_verified: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "condition": self.condition.value,
            "condition_kind": self.condition_kind.value,
            "graph_arm": None if self.graph_arm is None else self.graph_arm.value,
            "mode": self.mode.value,
            "run_id": self.run_id,
            "provenance": self.provenance.as_dict(),
            "install_only_completed": self.install_only_completed,
            "restore_observation": (
                None
                if self.restore_observation is None
                else self.restore_observation.model_dump(mode="json")
            ),
            "cleanup_completed": self.cleanup_completed,
            "access_binding_verified": self.access_binding_verified,
        }


def select_smoke_conditions(
    selected: Iterable[str | SmokeCondition] | None = None,
) -> tuple[SmokeCondition, ...]:
    """Parse a narrow condition filter while keeping the default comparison pair."""

    values = tuple(selected or (SmokeCondition.NO_STELE, SmokeCondition.FAITHFUL))
    if not values:
        raise SmokeConfigurationError("select at least one smoke condition")
    try:
        conditions = tuple(SmokeCondition(value) for value in values)
    except ValueError as error:
        raise SmokeConfigurationError("unknown smoke condition") from error
    if len(set(conditions)) != len(conditions):
        raise SmokeConfigurationError("smoke conditions must not be repeated")
    return conditions


def _artifact_digest(fixture_root: Path, relative_path: str, expected: str) -> None:
    root = fixture_root.resolve(strict=True)
    candidate = (root / relative_path).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise SmokeConfigurationError("fixture artifact escapes fixture_root") from error
    if not candidate.is_file():
        raise SmokeConfigurationError(f"fixture artifact is missing: {relative_path}")
    hasher = hashlib.sha256()
    with candidate.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if digest != expected:
        raise SmokeConfigurationError(f"fixture artifact digest does not match: {relative_path}")


def _verify_fixture_artifacts(request: SmokeRequest) -> None:
    _artifact_digest(
        request.fixture_root,
        request.fixture.evidence_corpus.path,
        request.fixture.evidence_corpus.sha256,
    )
    if SmokeCondition.FAITHFUL in request.conditions:
        graph = request.fixture.graphs[GraphArm.FAITHFUL]
        _artifact_digest(request.fixture_root, graph.path, graph.sha256)


def _provenance(
    corpus: MaterializedCorpus,
    request: SmokeRequest,
    condition: SmokeCondition,
) -> ExternalProvenance:
    graph_sha256 = None
    artifact = None
    if condition is SmokeCondition.FAITHFUL:
        graph_sha256 = request.fixture.graphs[GraphArm.FAITHFUL].sha256
        artifact = request.integration_provenance
    return ExternalProvenance(
        source_commit=corpus.source_commit,
        source_tree_sha256=corpus.tree_sha256,
        fixture_id=request.fixture.fixture_id,
        evidence_corpus_sha256=request.fixture.evidence_corpus.sha256,
        graph_sha256=graph_sha256,
        integration_artifact=artifact,
        agent_import_path=(
            HARBOR_AGENT_IMPORT_PATH if condition is SmokeCondition.FAITHFUL else None
        ),
    )


def _require_faithful_dependencies(dependencies: SmokeDependencies) -> None:
    if dependencies.provisioner is None:
        raise SmokeConfigurationError("faithful install-only requires a provisioner")
    if dependencies.faithful_install is None:
        raise SmokeConfigurationError("faithful install-only requires an install-only adapter")


def _require_no_stele_dependency(dependencies: SmokeDependencies) -> None:
    if dependencies.no_stele_install is None:
        raise SmokeConfigurationError("no-stele install-only requires an install-only adapter")


async def _prepare_faithful_install_only(
    request: SmokeRequest,
    dependencies: SmokeDependencies,
    provenance: ExternalProvenance,
) -> SmokeReceipt:
    _require_faithful_dependencies(dependencies)
    assert dependencies.provisioner is not None
    assert dependencies.faithful_install is not None
    project = None
    primary_error: BaseException | None = None
    observation: RestoreObservation | None = None
    try:
        restore = await dependencies.provisioner.restore_with_receipt(
            request.fixture, GraphArm.FAITHFUL, request.identities[SmokeCondition.FAITHFUL]
        )
        project = restore.project
        await dependencies.faithful_install.prepare()
        snapshot = await dependencies.provisioner.snapshot_with_receipt(project)
        observation = snapshot.observation(restore)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if project is not None:
            try:
                await dependencies.provisioner.destroy(project)
            except BaseException as cleanup_error:
                if primary_error is not None:
                    primary_error.add_note(
                        f"also failed to destroy the faithful smoke project: {cleanup_error}"
                    )
                else:
                    raise SmokeCleanupError(
                        "could not destroy faithful smoke project"
                    ) from cleanup_error
    assert observation is not None
    return SmokeReceipt(
        condition=SmokeCondition.FAITHFUL,
        condition_kind=ConditionKind.GRAPH,
        graph_arm=GraphArm.FAITHFUL,
        mode=SmokeMode.INSTALL_ONLY,
        run_id=request.identities[SmokeCondition.FAITHFUL].run_id,
        provenance=provenance,
        install_only_completed=True,
        restore_observation=observation,
        cleanup_completed=True,
    )


async def prepare_smoke(
    request: SmokeRequest,
    dependencies: SmokeDependencies = SmokeDependencies(),
) -> tuple[SmokeReceipt, ...]:
    """Materialize, audit, and prepare selected conditions without a model invocation."""

    if request.mode is SmokeMode.INSTALL_ONLY:
        if SmokeCondition.NO_STELE in request.conditions:
            _require_no_stele_dependency(dependencies)
        if SmokeCondition.FAITHFUL in request.conditions:
            _require_faithful_dependencies(dependencies)
    _verify_fixture_artifacts(request)
    corpus = materialize_pinned_tree(
        request.repository, request.revision, request.evaluated_project
    )
    audit_effective_context(corpus)
    receipts: list[SmokeReceipt] = []
    for condition in request.conditions:
        provenance = _provenance(corpus, request, condition)
        identity = request.identities[condition]
        if condition is SmokeCondition.NO_STELE:
            install_completed = False
            if request.mode is SmokeMode.INSTALL_ONLY:
                assert dependencies.no_stele_install is not None
                await dependencies.no_stele_install.prepare()
                install_completed = True
            receipts.append(
                SmokeReceipt(
                    condition=condition,
                    condition_kind=ConditionKind.NO_STELE,
                    graph_arm=None,
                    mode=request.mode,
                    run_id=identity.run_id,
                    provenance=provenance,
                    install_only_completed=install_completed,
                    restore_observation=None,
                    cleanup_completed=False,
                )
            )
        elif request.mode is SmokeMode.DRY_RUN:
            receipts.append(
                SmokeReceipt(
                    condition=condition,
                    condition_kind=ConditionKind.GRAPH,
                    graph_arm=GraphArm.FAITHFUL,
                    mode=request.mode,
                    run_id=identity.run_id,
                    provenance=provenance,
                    install_only_completed=False,
                    restore_observation=None,
                    cleanup_completed=False,
                )
            )
        else:
            receipts.append(
                await _prepare_faithful_install_only(request, dependencies, provenance)
            )
    return tuple(receipts)
