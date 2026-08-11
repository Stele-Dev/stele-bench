"""Local-only command line interface for fixture contracts."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

import typer
from pydantic import ValidationError

from .certification import (
    CertificationInputError,
    certify_packaged_inputs,
    redact_packaged_context,
)
from .challenges import (
    DEFAULT_CHALLENGE_ROOT,
    ChallengeCatalogError,
    load_challenge,
    load_challenge_catalog,
    select_challenges,
)
from .contracts import RunIdentity
from .corpus import CorpusError, materialize_pinned_tree, prepare_pinned_repository
from .fixtures import load_fixture
from .smoke import SmokeMode, SmokeRequest, prepare_smoke, select_smoke_conditions

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _source_cache() -> Path:
    root = os.environ.get("XDG_CACHE_HOME")
    return Path(root).expanduser() / "stele-bench" / "sources" if root else (
        Path.home() / ".cache" / "stele-bench" / "sources"
    )


def _validate(path: Path) -> None:
    if not path.is_file():
        raise typer.BadParameter(f"fixture does not exist: {path}")
    try:
        load_fixture(path)
    except (OSError, ValidationError) as error:
        raise typer.BadParameter(f"invalid fixture {path}: {error}") from error


@app.command()
def validate(path: Path) -> None:
    """Validate one fixture manifest."""
    _validate(path)
    typer.echo(f"valid: {path}")


@app.command(name="list")
def list_fixtures(directory: Path) -> None:
    """List valid JSON fixture manifests directly within DIRECTORY."""
    if not directory.is_dir():
        raise typer.BadParameter(f"fixture directory does not exist: {directory}")
    invalid: list[Path] = []
    for path in sorted(directory.glob("*.json")):
        try:
            _validate(path)
        except typer.BadParameter:
            invalid.append(path)
            typer.echo(f"invalid: {path.name}", err=True)
            continue
        typer.echo(path.name)
    if invalid:
        raise typer.Exit(code=1)


@app.command()
def doctor() -> None:
    """Confirm that the locally installed contract package can be imported."""
    typer.echo("ready: local contracts available")


@app.command(name="source-prepare")
def source_prepare(
    repository_url: str,
    revision: str,
    cache_directory: Path = typer.Option(None, "--cache-directory"),
) -> None:
    """Clone or refresh the repository containing one exact pinned commit."""

    try:
        repository = prepare_pinned_repository(
            repository_url,
            revision,
            cache_directory or _source_cache(),
        )
    except (CorpusError, OSError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(repository)


@app.command(name="challenge-list")
def challenge_list(
    directory: Path = typer.Argument(DEFAULT_CHALLENGE_ROOT),
) -> None:
    """List challenge ids, verifier kinds, and titles in stable id order."""
    try:
        catalog = load_challenge_catalog(directory)
    except ChallengeCatalogError as error:
        raise typer.BadParameter(str(error)) from error
    for challenge in catalog:
        manifest = challenge.manifest
        typer.echo(f"{manifest.challenge_id}\t{manifest.verifier.kind}\t{manifest.title}")


@app.command(name="challenge-validate")
def challenge_validate(
    path: Path = typer.Argument(DEFAULT_CHALLENGE_ROOT),
) -> None:
    """Validate one challenge manifest or an entire challenge catalog."""
    try:
        if path.is_dir():
            catalog = load_challenge_catalog(path)
            typer.echo(f"valid: {path} ({len(catalog)} challenges)")
        else:
            challenge = load_challenge(path)
            typer.echo(f"valid: {challenge.challenge_id}")
    except ChallengeCatalogError as error:
        raise typer.BadParameter(str(error)) from error


@app.command()
def certify(
    repository: Path,
    revision: str,
    fixture_path: Path,
    challenge: list[str] = typer.Option(None, "--challenge"),
    receipt_out: Path = typer.Option(None, "--receipt-out"),
) -> None:
    """Certify every packaged input against leakage and benchmark awareness.

    Materializes the pinned corpus into a temporary directory, applies the
    declared redaction, then statically probes the corpus, the evidence
    corpus, every graph arm, and every selected challenge.  No model is
    invoked and no backend is contacted.  A missing corpus, fixture artifact,
    or graph is an error, never a skipped check.
    """

    _validate(fixture_path)
    if not repository.is_dir():
        raise typer.BadParameter(
            f"pinned corpus repository is unavailable: {repository}; "
            "certification is never skipped"
        )
    try:
        fixture = load_fixture(fixture_path)
        catalog = (
            select_challenges(challenge)
            if challenge
            else tuple(load_challenge_catalog(DEFAULT_CHALLENGE_ROOT))
        )
    except (ChallengeCatalogError, OSError, ValidationError) as error:
        raise typer.BadParameter(str(error)) from error

    workspace = Path(tempfile.mkdtemp(prefix="stele-bench-certify-"))
    try:
        corpus = materialize_pinned_tree(repository, revision, workspace / "project")
        corpus, redactions = redact_packaged_context(corpus)
        receipt = certify_packaged_inputs(
            corpus_root=corpus.destination,
            fixture_root=fixture_path.parent,
            fixture=fixture,
            challenges=tuple(loaded.manifest for loaded in catalog),
            corpus_tree_sha256=corpus.tree_sha256,
            redactions=redactions,
            raise_on_finding=False,
        )
    except (CertificationInputError, CorpusError, OSError) as error:
        raise typer.BadParameter(str(error)) from error
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    payload = json.dumps(receipt.as_dict(), indent=2, sort_keys=True)
    if receipt_out is not None:
        receipt_out.parent.mkdir(parents=True, exist_ok=True)
        receipt_out.write_text(payload + "\n", encoding="utf-8")
    typer.echo(payload)
    if not receipt.certified:
        typer.echo(
            f"certification failed with {len(receipt.findings)} finding(s)", err=True
        )
        raise typer.Exit(code=1)


@app.command(name="smoke-dry-run")
def smoke_dry_run(
    repository: Path,
    revision: str,
    fixture_path: Path,
    evaluated_project: Path,
    condition: list[str] = typer.Option(None, "--condition"),
) -> None:
    """Materialize and audit selected conditions without a backend, agent, or model call."""

    _validate(fixture_path)
    try:
        conditions = select_smoke_conditions(condition)
        fixture = load_fixture(fixture_path)
        identities = {
            selected: RunIdentity(
                run_id="smoke-dry-run",
                experiment_id="smoke-readiness",
                condition_id=selected.value,
                challenge_id="prepare",
                attempt_id=f"{selected.value}-dry-run",
                attempt_index=0,
            )
            for selected in conditions
        }
        request = SmokeRequest(
            repository=repository,
            revision=revision,
            evaluated_project=evaluated_project,
            fixture_root=fixture_path.parent,
            fixture=fixture,
            identities=identities,
            conditions=conditions,
            mode=SmokeMode.DRY_RUN,
        )
        receipts = asyncio.run(prepare_smoke(request))
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(json.dumps([receipt.as_dict() for receipt in receipts], sort_keys=True))


if __name__ == "__main__":  # pragma: no cover - module entry point
    app()
