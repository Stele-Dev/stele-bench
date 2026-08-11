from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from stele_bench.challenges import DEFAULT_CHALLENGE_ROOT
from stele_bench.cli import app
from stele_bench.contracts import (
    EvidenceCorpusDescriptor,
    FixtureManifestV1,
    GraphArm,
    GraphDescriptor,
)
from stele_bench.fixtures import dump_fixture

HASH = "b" * 64
runner = CliRunner()


def graph_descriptor() -> GraphDescriptor:
    return GraphDescriptor(
        path="graphs/faithful.json",
        sha256=HASH,
        media_type="application/json",
        graph_format_version="graph-v1",
        backend_compatibility_id="backend-v1",
        embedding_model_id="embedding-v1",
        embedding_dimensions=1536,
    )


def write_fixture(path: Path) -> None:
    manifest = FixtureManifestV1(
        fixture_id="fixture",
        cohort_id="cohort",
        evidence_corpus=EvidenceCorpusDescriptor(
            path="corpora/evidence.json",
            sha256=HASH,
            media_type="application/json",
            ordering="source order; first 100 records",
        ),
        graphs={GraphArm.FAITHFUL: graph_descriptor()},
    )
    path.write_text(dump_fixture(manifest), encoding="utf-8")


def test_validate_list_and_doctor_success(tmp_path: Path) -> None:
    path = tmp_path / "fixture.json"
    write_fixture(path)
    assert runner.invoke(app, ["validate", str(path)]).exit_code == 0
    listed = runner.invoke(app, ["list", str(tmp_path)])
    assert listed.exit_code == 0
    assert "fixture.json" in listed.output
    assert runner.invoke(app, ["doctor"]).exit_code == 0


def test_validate_failure_and_list_reports_mixed_invalid_manifests(tmp_path: Path) -> None:
    valid = tmp_path / "valid.json"
    broken = tmp_path / "broken.json"
    write_fixture(valid)
    broken.write_text("{}", encoding="utf-8")
    result = runner.invoke(app, ["validate", str(broken)])
    assert result.exit_code != 0
    assert "invalid fixture" in result.output
    listed = runner.invoke(app, ["list", str(tmp_path)])
    assert listed.exit_code == 1
    assert "valid.json" in listed.output
    assert "invalid: broken.json" in listed.output


def test_challenge_list_reports_ids_kinds_and_titles_in_stable_order() -> None:
    result = runner.invoke(app, ["challenge-list"])
    assert result.exit_code == 0
    assert result.output.splitlines() == [
        "click-clirunner-buffer-ownership\tscript\t"
        "Fix CliRunner stream ownership without reviving the race",
        "click-clirunner-capture-modes\tscript\tResolve CliRunner's descriptor-exposure regression",
        "click-clirunner-fileno-compatibility\tscript\t"
        "Support file-descriptor consumers inside CliRunner",
        "click-clirunner-finalization-order\tscript\tMake StreamMixer finalization dependency-safe",
        "click-clirunner-stream-contract\tscript\t"
        "Separate CliRunner streams without losing terminal order",
        "dotfile-drift-detection\tscript\tDotfile drift detection",
        "fieldnote-milestone-1\tscript\tStart Fieldnote and implement its first milestone",
        "fieldnote-milestone-2\tscript\tContinue Fieldnote through milestone 2",
        "fieldnote-milestone-3\tscript\t"
        "Change Fieldnote milestone 3 and override its size boundary",
        "fieldnote-milestone-4\tscript\tFinish Fieldnote and assess release readiness",
        "input-keypress-architecture\tanswer-file\tInput field keypress architecture",
        "package-cache-is-empty\tscript\tPackage cache emptiness",
        "profile-selection-intake\tscript\tInvestigate the live profile-selection implementation",
        "profile-selection-live-path\tscript\tOnboarding profile-name clear shortcut",
        "service-readiness\tscript\tService readiness",
        "session-boundary-recall\tscript\tWindsurf dotfile candidate and its closing step",
        "settings-label-roundtrip\tscript\tClarify the automatic update setting label",
    ]


def test_challenge_validate_accepts_one_manifest_or_the_catalog() -> None:
    manifest = DEFAULT_CHALLENGE_ROOT / "input-keypress-architecture" / "challenge.json"
    single = runner.invoke(app, ["challenge-validate", str(manifest)])
    assert single.exit_code == 0
    assert "valid: input-keypress-architecture" in single.output

    catalog = runner.invoke(app, ["challenge-validate"])
    assert catalog.exit_code == 0
    assert "(17 challenges)" in catalog.output


def test_challenge_commands_report_catalog_errors(tmp_path: Path) -> None:
    empty = runner.invoke(app, ["challenge-list", str(tmp_path)])
    assert empty.exit_code != 0
    assert "challenge catalog is empty" in empty.output

    source = DEFAULT_CHALLENGE_ROOT / "service-readiness" / "challenge.json"
    challenge_dir = tmp_path / "service-readiness"
    challenge_dir.mkdir()
    manifest = challenge_dir / "challenge.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    data["verifier"]["asset"]["sha256"] = "a" * 64
    manifest.write_text(json.dumps(data), encoding="utf-8")
    (challenge_dir / "verifier.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    invalid = runner.invoke(app, ["challenge-validate", str(manifest)])
    assert invalid.exit_code != 0
    assert "verifier digest mismatch" in invalid.output
