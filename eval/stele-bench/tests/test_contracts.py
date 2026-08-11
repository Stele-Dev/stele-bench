from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from stele_bench.contracts import (
    AggregateRecord,
    AnswerFileVerifierV1,
    AttemptResultEnvelope,
    CanonicalGraphState,
    ChallengeManifestV1,
    ConditionKind,
    CostMetrics,
    CostReceipt,
    EvidenceCorpusDescriptor,
    EvidenceDescriptor,
    FixtureManifestV1,
    GraphArm,
    GraphDescriptor,
    Outcome,
    ProvisionedProject,
    RestoreObservation,
    RetrievalProbe,
    RetrievalProbeKind,
    RunIdentity,
    TimingMetrics,
    TokenMetrics,
    build_restoration_report,
)
from stele_bench.fixtures import dump_fixture, load_fixture
from stele_bench.provisioning import FakeProvisioner

HASH = "a" * 64


def graph_descriptor(path: str = "graphs/faithful.json") -> GraphDescriptor:
    return GraphDescriptor(
        path=path,
        sha256=HASH,
        media_type="application/json",
        graph_format_version="graph-v1",
        backend_compatibility_id="backend-v1",
        embedding_model_id="embedding-v1",
        embedding_dimensions=1536,
    )


def fixture() -> FixtureManifestV1:
    return FixtureManifestV1(
        fixture_id="fixture-1",
        cohort_id="cohort-1",
        evidence_corpus=EvidenceCorpusDescriptor(
            path="corpora/evidence.json",
            sha256=HASH,
            media_type="application/json",
            ordering="timestamp asc; truncate after 1000 events",
        ),
        graphs={GraphArm.FAITHFUL: graph_descriptor()},
    )


def test_schema_version_digest_and_portable_path_are_validated() -> None:
    with pytest.raises(ValidationError):
        FixtureManifestV1.model_validate({**fixture().model_dump(), "schema_version": 2})
    with pytest.raises(ValidationError):
        EvidenceDescriptor(path="x", sha256="bad", media_type="text/plain")
    for bad_path in (
        "/absolute.json",
        "graphs/../escape.json",
        "graphs\\windows.json",
        "C:/windows.json",
        "./graphs.json",
        "graphs//repeated.json",
    ):
        with pytest.raises(ValidationError):
            EvidenceDescriptor(path=bad_path, sha256=HASH, media_type="text/plain")


def test_fixture_round_trip_is_cohort_scoped(tmp_path: Path) -> None:
    path = tmp_path / "fixture.json"
    path.write_text(dump_fixture(fixture()), encoding="utf-8")
    restored = load_fixture(path)
    assert restored == fixture()
    assert "challenge_id" not in restored.model_dump()
    assert restored.evidence_corpus.ordering.startswith("timestamp")
    assert restored.graphs[GraphArm.FAITHFUL].embedding_dimensions == 1536
    with pytest.raises(ValidationError, match="faithful"):
        FixtureManifestV1.model_validate(
            {**fixture().model_dump(), "graphs": {GraphArm.ANTAGONIST: graph_descriptor()}}
        )


def test_memory_marker_is_optional_and_must_be_distinctive() -> None:
    # Absent by default (a fixture with no session-boundary memory probe).
    assert fixture().memory_marker is None
    marked = FixtureManifestV1.model_validate(
        {**fixture().model_dump(), "memory_marker": "Close out with a CHANGELOG entry"}
    )
    assert marked.memory_marker == "Close out with a CHANGELOG entry"
    # A too-short or untrimmed marker would false-positive on injection.
    with pytest.raises(ValidationError, match="at least 12 characters"):
        FixtureManifestV1.model_validate({**fixture().model_dump(), "memory_marker": "short"})
    with pytest.raises(ValidationError, match="at least 12 characters"):
        FixtureManifestV1.model_validate(
            {**fixture().model_dump(), "memory_marker": "  padded around the edges  "}
        )


def test_fixture_and_challenge_contracts_are_strictly_separate() -> None:
    challenge = ChallengeManifestV1(
        challenge_id="answer-one-question",
        title="One question",
        description="Write one answer.",
        prompt="Write the answer to `/workspace/.submission/answer.md`.\n",
        allowed_output_paths=("/workspace/.submission/answer.md",),
        verifier=AnswerFileVerifierV1(
            kind="answer-file",
            runtime="posix-shell",
            answer_path="/workspace/.submission/answer.md",
            asset=EvidenceDescriptor(
                path="verifier.sh", sha256=HASH, media_type="text/x-shellscript"
            ),
        ),
    )
    with pytest.raises(ValidationError):
        FixtureManifestV1.model_validate(challenge.model_dump())
    with pytest.raises(ValidationError):
        ChallengeManifestV1.model_validate(fixture().model_dump())
    with pytest.raises(ValidationError):
        ChallengeManifestV1.model_validate({**challenge.model_dump(), "fixture_id": "fixture-1"})
    with pytest.raises(ValidationError, match="answer_path"):
        ChallengeManifestV1.model_validate(
            {
                **challenge.model_dump(),
                "allowed_output_paths": ["/workspace/different.md"],
            }
        )


def test_follow_up_prompt_makes_a_challenge_multi_step_and_must_differ() -> None:
    base = dict(
        challenge_id="answer-one-question",
        title="One question",
        description="Write one answer.",
        allowed_output_paths=("/workspace/.submission/answer.md",),
        verifier=AnswerFileVerifierV1(
            kind="answer-file",
            runtime="posix-shell",
            answer_path="/workspace/.submission/answer.md",
            asset=EvidenceDescriptor(
                path="verifier.sh", sha256=HASH, media_type="text/x-shellscript"
            ),
        ),
    )
    single_step = ChallengeManifestV1(prompt="Establish the work.", **base)
    assert single_step.is_multi_step is False
    assert single_step.follow_up_prompt is None

    multi_step = ChallengeManifestV1(
        prompt="Establish the work.", follow_up_prompt="Now the vague part.", **base
    )
    assert multi_step.is_multi_step is True

    with pytest.raises(ValidationError, match="differ"):
        ChallengeManifestV1(prompt="Same text.", follow_up_prompt="Same text.", **base)
    with pytest.raises(ValidationError):
        ChallengeManifestV1(prompt="Establish the work.", follow_up_prompt="", **base)


def test_attempt_conditions_and_cost_sources_remain_distinct() -> None:
    receipt = CostReceipt(
        provider="provider", receipt_id="receipt-1", amount="1.25", currency="USD"
    )
    common = dict(
        cohort_id="cohort-1",
        fixture_id="fixture-1",
        experiment_id="exp",
        condition_id="condition",
        challenge_id="challenge",
        attempt_id="attempt",
        attempt_index=0,
        outcome=Outcome.PASSED,
        quality_evidence=(
            EvidenceDescriptor(path="evidence/quality.txt", sha256=HASH, media_type="text/plain"),
        ),
        memory_evidence=(
            EvidenceDescriptor(path="evidence/memory.txt", sha256=HASH, media_type="text/plain"),
        ),
        token_metrics=TokenMetrics(
            input_tokens=5, output_tokens=3, cache_read_tokens=1, cache_write_tokens=2
        ),
        cost_metrics=CostMetrics(
            authoritative_receipt=receipt,
            calculated_cross_check_cost="1.5",
            calculated_cross_check_currency="USD",
        ),
        timings=TimingMetrics(queue_seconds=1, execution_seconds=2, active_seconds=1.5),
        provenance_hashes={"fixture": HASH},
    )
    no_stele = AttemptResultEnvelope(condition_kind=ConditionKind.NO_STELE, **common)
    full_context = AttemptResultEnvelope(condition_kind=ConditionKind.FULL_CONTEXT, **common)
    graph = AttemptResultEnvelope(
        condition_kind=ConditionKind.GRAPH, graph_arm=GraphArm.ANTAGONIST, **common
    )
    assert no_stele.graph_arm is None
    assert full_context.model_dump(mode="json")["condition_kind"] == "full-context"
    assert no_stele.model_dump(mode="json")["cohort_id"] == "cohort-1"
    assert no_stele.model_dump(mode="json")["fixture_id"] == "fixture-1"
    assert graph.graph_arm is GraphArm.ANTAGONIST
    assert (
        graph.cost_metrics.authoritative_receipt.amount
        != graph.cost_metrics.calculated_cross_check_cost
    )
    with pytest.raises(ValidationError):
        AttemptResultEnvelope(condition_kind=ConditionKind.GRAPH, **common)
    with pytest.raises(ValidationError):
        AttemptResultEnvelope(
            condition_kind=ConditionKind.FULL_CONTEXT,
            graph_arm=GraphArm.FAITHFUL,
            **common,
        )
    with pytest.raises(ValidationError):
        CostMetrics(calculated_cross_check_cost="1", calculated_cross_check_currency=None)
    with pytest.raises(ValidationError):
        CostReceipt(provider="provider", receipt_id="receipt", amount="1", currency="usd")


def test_aggregate_requires_real_n_and_complete_comparative_interval() -> None:
    common = dict(
        cohort_id="cohort",
        challenge_id="challenge",
        fixture_id="fixture",
        experiment_id="e",
        condition_id="c",
        metric_name="score",
    )
    neutral = AggregateRecord(n=1, **common)
    assert neutral.value is None
    assert neutral.model_dump(mode="json")["cohort_id"] == "cohort"
    assert AggregateRecord(n=1, interval_low=0.1, interval_high=0.2, **common).interval_low == 0.1
    with pytest.raises(ValidationError):
        AggregateRecord(n=0, **common)
    with pytest.raises(ValidationError):
        AggregateRecord(n=2, comparative_delta=0.1, **common)
    with pytest.raises(ValidationError):
        AggregateRecord(n=2, interval_low=0.1, **common)
    with pytest.raises(ValidationError):
        AggregateRecord(n=1, comparative_delta=0.1, interval_low=0, interval_high=1, **common)


def test_fake_provisioner_isolates_snapshots_rejects_missing_arms_and_cleans_up() -> None:
    async def run() -> None:
        provisioner = FakeProvisioner()
        identity = RunIdentity(
            run_id="run-1",
            experiment_id="experiment",
            condition_id="condition",
            challenge_id="challenge",
            attempt_id="attempt",
            attempt_index=0,
        )
        first = await provisioner.restore(fixture(), GraphArm.FAITHFUL, identity)
        second = await provisioner.restore(fixture(), GraphArm.FAITHFUL, identity)
        assert first.project_id != second.project_id
        assert first.run_id == "run-1"
        snapshot = await provisioner.snapshot(first)
        assert snapshot.project_id == first.project_id
        assert snapshot.fixture_id == first.fixture_id
        assert snapshot.graph_arm is GraphArm.FAITHFUL
        assert snapshot.canonical_state.path.startswith("snapshots/")
        with pytest.raises(ValueError, match="does not provide"):
            await provisioner.restore(fixture(), GraphArm.KNOW_IT_ALL, identity)
        await provisioner.destroy(first)
        await provisioner.destroy(first)
        assert first.project_id not in provisioner.projects
        assert second.project_id in provisioner.projects
        await provisioner.destroy(second)
        assert not provisioner.projects

    asyncio.run(run())


def test_restoration_report_requires_complete_exact_independent_observations() -> None:
    graph = graph_descriptor("different-snapshot-path.json")

    def observation(project_id: str, **changes: object) -> RestoreObservation:
        state = CanonicalGraphState(
            fixture_id="fixture-1",
            project_id=project_id,
            graph_arm=GraphArm.FAITHFUL,
            canonical_state=graph,
            declared_node_count=2,
            declared_edge_count=1,
        )
        project = ProvisionedProject(
            project_id=project_id,
            fixture_id="fixture-1",
            run_id=project_id,
            graph_arm=GraphArm.FAITHFUL,
        )
        values = dict(
            project=project,
            state=state,
            probes=(
                RetrievalProbe(name="f", kind=RetrievalProbeKind.FTS, stable_ids=("N1",)),
                RetrievalProbe(
                    name="s", kind=RetrievalProbeKind.SEMANTIC, stable_ids=("N2",), scores=(1.0,)
                ),
                RetrievalProbe(name="g", kind=RetrievalProbeKind.GRAPH, stable_ids=("N3",)),
            ),
            queue_before_count=0,
            queue_after_count=0,
            restore_seconds=0,
            snapshot_seconds=0,
        )
        return RestoreObservation(**(values | changes))

    first, second = observation("one"), observation("two")
    assert build_restoration_report(fixture(), GraphArm.FAITHFUL, first, second).passed
    assert not build_restoration_report(
        fixture(), GraphArm.FAITHFUL, first, observation("one")
    ).passed
    with pytest.raises(ValidationError, match="queue count"):
        observation("three", queue_after_count=1)
    with pytest.raises(ValidationError, match="project_id"):
        observation("three", state=first.state)
    with pytest.raises(ValidationError, match="cover"):
        RestoreObservation.model_validate({**first.model_dump(), "probes": first.probes[:2]})
