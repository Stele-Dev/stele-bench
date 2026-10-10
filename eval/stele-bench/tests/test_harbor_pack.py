from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from stele_bench.challenges import LoadedChallenge, select_challenges
from stele_bench.corpus import (
    CorpusEntry,
    DestinationError,
    MaterializedCorpus,
    materialize_pinned_history,
)
from stele_bench.harbor_entrypoint import HARBOR_AGENT_IMPORT_PATH
from stele_bench.harbor_pack import (
    HARBOR_TASK_IMAGE,
    FaithfulAgentConfig,
    HarborPackError,
    build_install_only_argv,
    build_print_config_argv,
    generate_harbor_task_pack,
)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _corpus(tmp_path: Path) -> MaterializedCorpus:
    root = tmp_path / "source"
    (root / "src").mkdir(parents=True)
    app = root / "src" / "app.txt"
    app.write_bytes(b"service\x00data\n")
    app.chmod(0o640)
    script = root / "check.sh"
    script.write_bytes(b"#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    link = root / "current"
    link.symlink_to("src/app.txt")
    entries = (
        CorpusEntry("check.sh", _digest(script.read_bytes()), "file", 0o755),
        CorpusEntry("current", _digest(b"symlink\0src/app.txt"), "symlink", 0o777),
        CorpusEntry("src/app.txt", _digest(app.read_bytes()), "file", 0o640),
    )
    return MaterializedCorpus("a" * 40, root, entries, "b" * 64)


def _faithful(tmp_path: Path) -> FaithfulAgentConfig:
    return FaithfulAgentConfig(
        artifact_path=tmp_path / "inputs" / "tool.tar.gz",
        artifact_sha256="c" * 64,
        artifact_source_kind="release",
        artifact_release_version="1.2.3",
        credentials_path=tmp_path / "inputs" / "account.json",
        preferences_path=tmp_path / "inputs" / "settings.json",
        project_binding_path=tmp_path / "inputs" / "scope.json",
        backend_url="http://host.docker.internal:54321",
        backend_anon_key="public-client-key",
    )


def _walk_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [str(key) for key in value] + [
            item for child in value.values() for item in _walk_strings(child)
        ]
    if isinstance(value, list):
        return [item for child in value for item in _walk_strings(child)]
    return []


def test_generates_harbor_020_structure_and_exact_project_copy(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    pack = generate_harbor_task_pack(corpus, tmp_path / "output", _faithful(tmp_path))

    assert pack.task_directories == (pack.task_directory,)
    assert pack.project_directories == (pack.project_directory,)
    assert pack.configs == (pack.plain_config, pack.configured_config)
    assert pack.plain_job_name == "route-one"
    assert pack.configured_job_name == "route-two"

    generated_paths = {
        path.relative_to(pack.task_directory).as_posix() for path in pack.task_directory.rglob("*")
    }
    assert generated_paths == {
        "environment",
        "environment/Dockerfile",
        "environment/project",
        "environment/project/check.sh",
        "environment/project/current",
        "environment/project/src",
        "environment/project/src/app.txt",
        "instruction.md",
        "task.toml",
        "tests",
        "tests/challenge-verifier.sh",
        "tests/context-redaction.json",
        "tests/mutation-baseline.tsv",
        "tests/test.sh",
    }
    task = tomllib.loads((pack.task_directory / "task.toml").read_text())
    assert task["schema_version"] == "1.3"
    assert task["task"]["name"] == "local/service-readiness"
    assert task["environment"]["network_mode"] == "public"
    assert (pack.project_directory / "src/app.txt").read_bytes() == b"service\x00data\n"
    assert (pack.project_directory / "src/app.txt").stat().st_mode & 0o777 == 0o640
    assert (pack.project_directory / "check.sh").read_bytes() == b"#!/bin/sh\nexit 0\n"
    assert (pack.project_directory / "check.sh").stat().st_mode & 0o777 == 0o755
    assert os.readlink(pack.project_directory / "current") == "src/app.txt"
    assert (pack.task_directory / "tests/test.sh").stat().st_mode & 0o777 == 0o755
    service = select_challenges(("service-readiness",))[0]
    assert (pack.task_directory / "instruction.md").read_text() == service.manifest.prompt
    assert (
        pack.task_directory / "tests/challenge-verifier.sh"
    ).read_bytes() == service.verifier_path.read_bytes()
    dockerfile = (pack.task_directory / "environment/Dockerfile").read_text()
    assert dockerfile.startswith(f"FROM {HARBOR_TASK_IMAGE}\n")
    assert "apt-get" not in dockerfile
    assert "npm install" not in dockerfile


def test_pack_preserves_truncated_git_history_without_auditing_git_metadata(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "-C", os.fspath(repository), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", os.fspath(repository), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", os.fspath(repository), "config", "user.name", "Test"],
        check=True,
    )
    (repository / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", os.fspath(repository), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", os.fspath(repository), "commit", "-q", "-m", "first"], check=True)
    first = subprocess.run(
        ["git", "-C", os.fspath(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "app.py").write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", os.fspath(repository), "commit", "-qam", "future"], check=True)
    future = subprocess.run(
        ["git", "-C", os.fspath(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    corpus = materialize_pinned_history(
        repository,
        first,
        tmp_path / "corpus",
        forbidden_object_ids=(future,),
    )
    challenge = select_challenges(("service-readiness",))[0]
    pack = generate_harbor_task_pack(corpus, tmp_path / "output", challenges=(challenge,))

    project = pack.project_directory
    assert (project / ".git").is_dir()
    head = subprocess.run(
        ["git", "-C", os.fspath(project), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == first
    target = subprocess.run(
        ["git", "-C", os.fspath(project), "cat-file", "-e", future],
        check=False,
        capture_output=True,
    )
    assert target.returncode != 0
    baseline = pack.task_directory / "tests/mutation-baseline.tsv"
    baseline_paths = {
        base64.b64decode(line.split(b"\t", 1)[0]).decode()
        for line in baseline.read_bytes().splitlines()
    }
    assert all(not path.startswith(".git") for path in baseline_paths)
    wrapper = (pack.task_directory / "tests/test.sh").read_text(encoding="utf-8")
    assert "-path ./.git" in wrapper


def test_multi_step_challenge_generates_steps_and_enables_resume(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    service = select_challenges(("service-readiness",))[0]
    follow_up = LoadedChallenge(
        manifest=service.manifest.model_copy(
            update={"follow_up_prompt": "Now do the deliberately vague part."}
        ),
        directory=service.directory,
        verifier_path=service.verifier_path,
    )

    pack = generate_harbor_task_pack(corpus, tmp_path / "output", challenges=(follow_up,))

    generated_paths = {
        path.relative_to(pack.task_directory).as_posix() for path in pack.task_directory.rglob("*")
    }
    assert "instruction.md" not in generated_paths
    assert "steps/establish/instruction.md" in generated_paths
    assert "steps/follow-up/instruction.md" in generated_paths
    assert (
        pack.task_directory / "steps/establish/instruction.md"
    ).read_text() == service.manifest.prompt
    assert (
        pack.task_directory / "steps/follow-up/instruction.md"
    ).read_text() == "Now do the deliberately vague part."
    # Both steps still share the one task-level verifier.
    assert "tests/test.sh" in generated_paths
    assert not any(
        path.startswith("steps/") and path.endswith("/tests") for path in generated_paths
    )

    task = tomllib.loads((pack.task_directory / "task.toml").read_text())
    assert task["multi_step_reward_strategy"] == "final"
    assert [step["name"] for step in task["steps"]] == ["establish", "follow-up"]

    plain = json.loads(pack.plain_config.read_text())
    assert plain["agents"] == [{"name": "claude-code", "resume_trajectory": True}]


def test_authored_session_boundary_recall_challenge_packs_cleanly(tmp_path: Path) -> None:
    """The checked-in challenge (not a synthetic variant) generates a valid pack."""

    root = tmp_path / "source"
    root.mkdir()
    (root / "src").mkdir()
    candidates = root / "src" / "dotfile_candidates.rs"
    candidates.write_bytes(b"pub const DEFAULT_DOTFILES: &[DotfileCandidate] = &[];\n")
    changelog = root / "CHANGELOG.md"
    changelog.write_bytes(b"# Changelog\n\n## [Unreleased]\n\n---\n")
    entries = tuple(
        CorpusEntry(relative, _digest(path.read_bytes()), "file", 0o644)
        for relative, path in (
            ("src/dotfile_candidates.rs", candidates),
            ("CHANGELOG.md", changelog),
        )
    )
    corpus = MaterializedCorpus("a" * 40, root, entries, "b" * 64)

    selected = select_challenges(("session-boundary-recall",))
    pack = generate_harbor_task_pack(corpus, tmp_path / "output", challenges=selected)

    task = tomllib.loads((pack.task_directory / "task.toml").read_text())
    assert task["multi_step_reward_strategy"] == "final"
    assert [step["name"] for step in task["steps"]] == ["establish", "follow-up"]
    plain = json.loads(pack.plain_config.read_text())
    assert plain["agents"] == [{"name": "claude-code", "resume_trajectory": True}]
    assert (pack.project_directory / "src/dotfile_candidates.rs").is_file()
    assert (pack.project_directory / "CHANGELOG.md").is_file()


def test_contract_none_network_mode_maps_to_harbor_no_network(tmp_path: Path) -> None:
    selected = select_challenges(("fieldnote-milestone-1",))[0]
    challenge = LoadedChallenge(
        manifest=selected.manifest.model_copy(
            update={
                "environment": selected.manifest.environment.model_copy(
                    update={"network_mode": "none"}
                )
            }
        ),
        directory=selected.directory,
        verifier_path=selected.verifier_path,
    )
    pack = generate_harbor_task_pack(
        _corpus(tmp_path),
        tmp_path / "output",
        challenges=(challenge,),
    )

    task = tomllib.loads((pack.task_directory / "task.toml").read_text())
    assert task["environment"]["network_mode"] == "no-network"


def test_contract_prebuild_argv_uses_docker_exec_form(tmp_path: Path) -> None:
    selected = select_challenges(("service-readiness",))[0]
    challenge = LoadedChallenge(
        manifest=selected.manifest.model_copy(
            update={
                "environment": selected.manifest.environment.model_copy(
                    update={"prebuild_argv": ("cargo", "test", "--no-run", "--lib")}
                )
            }
        ),
        directory=selected.directory,
        verifier_path=selected.verifier_path,
    )

    pack = generate_harbor_task_pack(
        _corpus(tmp_path),
        tmp_path / "output",
        challenges=(challenge,),
    )

    dockerfile = (pack.task_directory / "environment/Dockerfile").read_text()
    assert 'RUN ["cargo", "test", "--no-run", "--lib"]' in dockerfile
    assert "RUN cargo test" not in dockerfile


def test_single_step_pack_has_no_resume_trajectory_or_steps(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    pack = generate_harbor_task_pack(corpus, tmp_path / "output")
    generated_paths = {
        path.relative_to(pack.task_directory).as_posix() for path in pack.task_directory.rglob("*")
    }
    assert "instruction.md" in generated_paths
    assert not any(path.startswith("steps/") for path in generated_paths)
    task = tomllib.loads((pack.task_directory / "task.toml").read_text())
    assert "steps" not in task
    assert "multi_step_reward_strategy" not in task
    plain = json.loads(pack.plain_config.read_text())
    assert plain["agents"] == [{"name": "claude-code"}]


def test_generates_selected_tasks_in_order_with_isolated_project_copies(
    tmp_path: Path,
) -> None:
    corpus = _corpus(tmp_path)
    selected = select_challenges(("package-cache-is-empty", "input-keypress-architecture"))
    pack = generate_harbor_task_pack(
        corpus,
        tmp_path / "output",
        _faithful(tmp_path),
        challenges=selected,
    )

    assert [path.name for path in pack.task_directories] == [
        "package-cache-is-empty",
        "input-keypress-architecture",
    ]
    assert len(set(pack.task_directories)) == 2
    assert len(set(pack.project_directories)) == 2
    for task_directory, project_directory, challenge in zip(
        pack.task_directories, pack.project_directories, selected, strict=True
    ):
        assert project_directory == task_directory / "environment/project"
        assert (task_directory / "instruction.md").read_text() == challenge.manifest.prompt
        assert (
            task_directory / "tests/challenge-verifier.sh"
        ).read_bytes() == challenge.verifier_path.read_bytes()
        assert (project_directory / "src/app.txt").read_bytes() == b"service\x00data\n"
        task = tomllib.loads((task_directory / "task.toml").read_text())
        assert task["task"]["name"] == f"local/{challenge.challenge_id}"
        assert task["task"]["description"] == challenge.manifest.description
        assert task["agent"]["timeout_sec"] == challenge.manifest.agent_timeout_seconds
        assert task["verifier"]["timeout_sec"] == challenge.manifest.verifier.timeout_seconds
        assert task["environment"] == {
            "network_mode": challenge.manifest.environment.network_mode,
            "build_timeout_sec": challenge.manifest.environment.build_timeout_seconds,
            "cpus": challenge.manifest.environment.cpus,
            "memory_mb": challenge.manifest.environment.memory_mb,
            "storage_mb": challenge.manifest.environment.storage_mb,
        }

    (pack.project_directories[0] / "src/app.txt").write_bytes(b"changed")
    assert (pack.project_directories[1] / "src/app.txt").read_bytes() == b"service\x00data\n"
    with pytest.raises(HarborPackError, match="only for a one-task pack"):
        _ = pack.task_directory
    with pytest.raises(HarborPackError, match="only for a one-task pack"):
        _ = pack.project_directory


def test_hidden_grader_is_outside_environment_context_and_writes_one_reward(
    tmp_path: Path,
) -> None:
    pack = generate_harbor_task_pack(_corpus(tmp_path), tmp_path / "output", _faithful(tmp_path))
    dockerfile = (pack.task_directory / "environment/Dockerfile").read_text()
    script = (pack.task_directory / "tests/test.sh").read_text()

    assert "COPY project/ /workspace/" in dockerfile
    assert "tests" not in dockerfile.casefold()
    assert not (pack.task_directory / "environment/tests").exists()
    assert "undeclared workspace mutation" in script
    assert "mutation.json" in script
    assert (pack.task_directory / "tests/mutation-baseline.tsv").is_file()
    assert (pack.task_directory / "tests/challenge-verifier.sh").read_bytes() == select_challenges(
        ("service-readiness",)
    )[0].verifier_path.read_bytes()
    assert "reward.txt" not in script
    assert "/tests" not in (pack.task_directory / "instruction.md").read_text()


def _run_mutation_wrapper(pack, tmp_path: Path) -> dict[str, object]:
    logs = tmp_path / "verifier-logs"
    logs.mkdir()
    verifier = pack.task_directory / "tests/challenge-verifier.sh"
    verifier.write_text(
        '#!/bin/sh\nprintf \'{"reward":1}\\n\' > "$MUTATION_LOGS_ROOT/reward.json"\n',
        encoding="utf-8",
    )
    verifier.chmod(0o755)
    result = subprocess.run(
        [pack.task_directory / "tests/test.sh"],
        env={
            **os.environ,
            "MUTATION_WORKSPACE_ROOT": str(pack.project_directory),
            "MUTATION_TESTS_ROOT": str(pack.task_directory / "tests"),
            "MUTATION_LOGS_ROOT": str(logs),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout == ""
    assert result.stderr == ""
    return {
        "receipt": json.loads((logs / "mutation.json").read_text()),
        "reward": json.loads((logs / "reward.json").read_text()),
    }


@pytest.mark.parametrize(
    ("challenge_id", "allowed_path"),
    (
        ("package-cache-is-empty", "src/utils/package_cache.rs"),
        ("input-keypress-architecture", ".submission/answer.md"),
    ),
)
def test_mutation_audit_allows_only_manifest_outputs_for_coding_and_answer_tasks(
    tmp_path: Path, challenge_id: str, allowed_path: str
) -> None:
    pack = generate_harbor_task_pack(
        _corpus(tmp_path),
        tmp_path / "output",
        challenges=select_challenges((challenge_id,)),
    )
    output = pack.project_directory / allowed_path
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("declared output\n", encoding="utf-8")

    result = _run_mutation_wrapper(pack, tmp_path)

    assert result["receipt"]["status"] == "passed"
    assert result["receipt"]["before_sha256"] == result["receipt"]["after_sha256"]
    assert result["reward"] == {"reward": 1}


@pytest.mark.parametrize("challenge_id", ("package-cache-is-empty", "input-keypress-architecture"))
@pytest.mark.parametrize(
    "mutation",
    ("add", "change", "chmod", "chmod-ancestor", "delete", "symlink-target", "type-swap"),
)
def test_mutation_audit_forces_zero_for_undeclared_workspace_mutations(
    tmp_path: Path, challenge_id: str, mutation: str
) -> None:
    pack = generate_harbor_task_pack(
        _corpus(tmp_path),
        tmp_path / "output",
        challenges=select_challenges((challenge_id,)),
    )
    project = pack.project_directory
    if mutation == "add":
        (project / "undeclared.txt").write_text("unexpected\n", encoding="utf-8")
    elif mutation == "change":
        (project / "src/app.txt").write_text("changed\n", encoding="utf-8")
    elif mutation == "chmod":
        (project / "src/app.txt").chmod(0o600)
    elif mutation == "chmod-ancestor":
        (project / "src").chmod(0o700)
    elif mutation == "delete":
        (project / "check.sh").unlink()
    elif mutation == "symlink-target":
        (project / "current").unlink()
        (project / "current").symlink_to("check.sh")
    else:
        (project / "src/app.txt").unlink()
        (project / "src/app.txt").symlink_to("../check.sh")

    result = _run_mutation_wrapper(pack, tmp_path)

    assert result["receipt"]["status"] == "failed"
    assert result["receipt"]["before_sha256"] != result["receipt"]["after_sha256"]
    assert result["reward"] == {"reward": 0}


def test_mutation_receipt_lists_the_offending_paths(tmp_path: Path) -> None:
    pack = generate_harbor_task_pack(
        _corpus(tmp_path),
        tmp_path / "output",
        challenges=select_challenges(("package-cache-is-empty",)),
    )
    project = pack.project_directory
    (project / "undeclared.txt").write_text("unexpected\n", encoding="utf-8")
    (project / "check.sh").unlink()

    result = _run_mutation_wrapper(pack, tmp_path)

    receipt = result["receipt"]
    assert receipt["status"] == "failed"
    # A naked before/after sha is un-diagnosable; the receipt now names the files.
    assert "undeclared.txt" in receipt["only_in_workspace"]
    assert "check.sh" in receipt["only_in_baseline"]
    assert result["reward"] == {"reward": 0}


def test_mutation_audit_exempts_steles_workspace_binding(tmp_path: Path) -> None:
    pack = generate_harbor_task_pack(
        _corpus(tmp_path),
        tmp_path / "output",
        challenges=select_challenges(("package-cache-is-empty",)),
    )
    project = pack.project_directory
    # Running Stele writes its own project binding into the workspace at runtime;
    # this is infrastructure the verifier never reads, not an agent output, and
    # must NOT force-zero the reward before the real verifier runs.
    binding = project / ".stele"
    binding.mkdir()
    (binding / "project.json").write_text('{"project_id":"p"}\n', encoding="utf-8")

    result = _run_mutation_wrapper(pack, tmp_path)

    assert result["receipt"]["status"] == "passed"
    assert result["receipt"]["only_in_workspace"] == []
    # The real challenge verifier runs and awards the reward.
    assert result["reward"] == {"reward": 1}


def test_mutation_audit_exempts_python_runtime_bytecode(tmp_path: Path) -> None:
    pack = generate_harbor_task_pack(
        _corpus(tmp_path),
        tmp_path / "output",
        challenges=select_challenges(("package-cache-is-empty",)),
    )
    project = pack.project_directory
    cache = project / "src/__pycache__"
    cache.mkdir()
    (cache / "app.cpython-311.pyc").write_bytes(b"runtime bytecode")
    (project / "src/legacy.pyc").write_bytes(b"legacy runtime bytecode")

    result = _run_mutation_wrapper(pack, tmp_path)

    assert result["receipt"]["status"] == "passed"
    assert result["receipt"]["only_in_workspace"] == []
    assert result["reward"] == {"reward": 1}


def test_job_configs_isolate_stock_and_configured_agents(tmp_path: Path) -> None:
    selected = select_challenges(("input-keypress-architecture", "package-cache-is-empty"))
    pack = generate_harbor_task_pack(
        _corpus(tmp_path),
        tmp_path / "output",
        _faithful(tmp_path),
        challenges=selected,
    )
    plain = json.loads(pack.plain_config.read_text())
    configured = json.loads(pack.configured_config.read_text())

    expected_tasks = [{"path": str(path)} for path in pack.task_directories]
    assert plain["job_name"] == pack.plain_job_name
    assert configured["job_name"] == pack.configured_job_name
    assert plain["tasks"] == expected_tasks
    assert configured["tasks"] == expected_tasks
    assert plain["agents"] == [{"name": "claude-code"}]
    plain_text = "\n".join(_walk_strings(plain)).casefold()
    assert "stele" not in plain_text
    assert "credential" not in plain_text
    assert "artifact" not in plain_text
    assert "project_binding" not in plain_text
    assert "supabase" not in plain_text
    assert "host.docker.internal" not in plain_text
    assert "public-client-key" not in plain_text
    assert configured["agents"][0]["import_path"] == HARBOR_AGENT_IMPORT_PATH
    assert isinstance(configured["agents"][0]["import_path"], str)
    kwargs = configured["agents"][0]["kwargs"]
    assert kwargs["artifact_source_kind"] == "release"
    assert kwargs["artifact_release_version"] == "1.2.3"
    assert kwargs["project_binding_path"].endswith("/inputs/scope.json")
    assert all(isinstance(value, str) for value in kwargs.values())
    assert configured["agents"][0]["env"] == {
        "SUPABASE_URL": "http://host.docker.internal:54321",
        "SUPABASE_ANON_KEY": "public-client-key",
    }

    visible = "\n".join(
        path.read_text(errors="ignore")
        for task_directory in pack.task_directories
        for path in task_directory.rglob("*")
        if path.is_file() and not path.is_symlink()
    ).casefold()
    for secret_only in (
        "credentials_path",
        "preferences_path",
        "project_binding_path",
        "artifact_path",
        "tool.tar.gz",
        "scope.json",
        "supabase_url",
        "supabase_anon_key",
        "host.docker.internal",
        "public-client-key",
    ):
        assert secret_only not in visible


def test_plain_pack_needs_no_configured_agent_inputs(tmp_path: Path) -> None:
    selected = select_challenges(("input-keypress-architecture",))
    pack = generate_harbor_task_pack(
        _corpus(tmp_path),
        tmp_path / "output",
        challenges=selected,
    )

    assert pack.configs == (pack.plain_config,)
    assert pack.plain_job_name == "route-one"
    assert pack.configured_config is None
    assert pack.configured_job_name is None
    assert not (pack.destination / "route-two.json").exists()
    plain = json.loads(pack.plain_config.read_text())
    assert plain["agents"] == [{"name": "claude-code"}]
    assert plain["tasks"] == [{"path": str(pack.task_directory)}]


def test_process_argv_is_neutral_no_shell_and_has_schema_only_form(tmp_path: Path) -> None:
    pack = generate_harbor_task_pack(_corpus(tmp_path), tmp_path / "output", _faithful(tmp_path))
    install = build_install_only_argv(pack.plain_config)
    inspect = build_print_config_argv(pack.configured_config)

    assert install == (
        "harbor",
        "run",
        "--config",
        str(pack.plain_config),
        "--install-only",
        "--n-concurrent",
        "1",
        "--yes",
    )
    assert inspect == (
        "harbor",
        "run",
        "--config",
        str(pack.configured_config),
        "--print-config",
    )
    assert all(isinstance(argument, str) for argument in (*install, *inspect))
    revealing = ("benchmark", "eval", "condition", "arm", "challenge", "no-stele")
    assert not any(marker in "\n".join((*install, *inspect)).casefold() for marker in revealing)


def test_agent_visible_files_have_no_revealing_markers(tmp_path: Path) -> None:
    selected = select_challenges(
        ("service-readiness", "input-keypress-architecture", "package-cache-is-empty")
    )
    pack = generate_harbor_task_pack(
        _corpus(tmp_path),
        tmp_path / "output",
        _faithful(tmp_path),
        challenges=selected,
    )
    visible_files = [
        path
        for task_directory, project_directory in zip(
            pack.task_directories, pack.project_directories, strict=True
        )
        for path in (
            task_directory / "instruction.md",
            task_directory / "task.toml",
            task_directory / "environment/Dockerfile",
            *(candidate for candidate in project_directory.rglob("*") if candidate.is_file()),
        )
    ]
    payload = "\n".join(path.read_text(errors="ignore") for path in visible_files).casefold()
    revealing = ("benchmark", " eval ", "condition", " arm ", "stele", "no-stele")
    assert not any(marker in payload for marker in revealing)
    assert all(
        not (project_directory / ".git").exists() for project_directory in pack.project_directories
    )


def test_rejects_nonempty_or_symlink_destination(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    faithful = _faithful(tmp_path)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("keep")
    with pytest.raises(DestinationError, match="empty"):
        generate_harbor_task_pack(corpus, occupied, faithful)
    assert (occupied / "keep.txt").read_text() == "keep"

    target = tmp_path / "target"
    target.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(target, target_is_directory=True)
    with pytest.raises(DestinationError, match="symlink"):
        generate_harbor_task_pack(corpus, redirected, faithful)


def test_rejects_manifest_drift_and_cleans_new_destination(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    (corpus.destination / "extra.txt").write_text("unexpected")
    destination = tmp_path / "output"
    with pytest.raises(HarborPackError, match="manifest paths"):
        generate_harbor_task_pack(corpus, destination, _faithful(tmp_path))
    assert not destination.exists()


def test_rejects_revealing_marker_in_follow_up_prompt(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    service = select_challenges(("service-readiness",))[0]
    leaky = LoadedChallenge(
        manifest=service.manifest.model_copy(
            update={"follow_up_prompt": "Now double check the benchmark arm."}
        ),
        directory=service.directory,
        verifier_path=service.verifier_path,
    )
    with pytest.raises(HarborPackError, match="revealing marker"):
        generate_harbor_task_pack(corpus, tmp_path / "output", challenges=(leaky,))
    assert not (tmp_path / "output").exists()


def test_rejects_empty_duplicate_and_drifted_challenge_inputs(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    faithful = _faithful(tmp_path)
    selected = select_challenges(("service-readiness",))

    with pytest.raises(HarborPackError, match="at least one challenge"):
        generate_harbor_task_pack(corpus, tmp_path / "empty", faithful, challenges=())
    with pytest.raises(HarborPackError, match="duplicate challenge ids"):
        generate_harbor_task_pack(
            corpus,
            tmp_path / "duplicate",
            faithful,
            challenges=(*selected, *selected),
        )

    drifted_verifier = tmp_path / "drifted-verifier.sh"
    drifted_verifier.write_bytes(selected[0].verifier_path.read_bytes() + b"\n")
    drifted = LoadedChallenge(
        manifest=selected[0].manifest,
        directory=tmp_path,
        verifier_path=drifted_verifier,
    )
    with pytest.raises(HarborPackError, match="verifier digest changed"):
        generate_harbor_task_pack(
            corpus,
            tmp_path / "drifted",
            faithful,
            challenges=(drifted,),
        )
    assert not (tmp_path / "drifted").exists()


@pytest.mark.parametrize(
    "backend_url",
    ("host.docker.internal:54321", "ftp://host.docker.internal/data", "http://user@host/x"),
)
def test_rejects_invalid_backend_urls(tmp_path: Path, backend_url: str) -> None:
    with pytest.raises(ValueError, match="backend_url"):
        FaithfulAgentConfig(
            artifact_path=tmp_path / "tool.tar.gz",
            artifact_sha256="c" * 64,
            artifact_source_kind="release",
            artifact_release_version="1.2.3",
            credentials_path=tmp_path / "account.json",
            preferences_path=tmp_path / "settings.json",
            project_binding_path=tmp_path / "scope.json",
            backend_url=backend_url,
            backend_anon_key="public-client-key",
        )


def test_rejects_escaping_symlink_even_if_manifest_matches(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    link = corpus.destination / "current"
    link.unlink()
    link.symlink_to("../../outside")
    entries = tuple(
        CorpusEntry(entry.path, _digest(b"symlink\0../../outside"), entry.kind, entry.mode)
        if entry.path == "current"
        else entry
        for entry in corpus.entries
    )
    escaped = MaterializedCorpus(
        corpus.source_commit, corpus.destination, entries, corpus.tree_sha256
    )
    with pytest.raises(HarborPackError, match="escapes"):
        generate_harbor_task_pack(escaped, tmp_path / "output", _faithful(tmp_path))


def test_a_benchmark_arm_refuses_a_non_local_backend(tmp_path: Path) -> None:
    """An arm creates projects and writes nodes; pointed at a real backend it
    would do that to somebody's actual graph."""

    artifact = tmp_path / "cli"
    artifact.write_bytes(b"x")
    binding = tmp_path / "project.json"
    binding.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="loopback"):
        FaithfulAgentConfig(
            artifact_path=artifact,
            artifact_sha256="a" * 64,
            artifact_source_kind="local-dirty-worktree",
            credentials_path=binding,
            preferences_path=binding,
            project_binding_path=binding,
            backend_url="https://real-project.supabase.co",
            backend_anon_key="public-key",
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:54321",
        "http://localhost:54321",
        # How a container reaches the host's loopback stack — same local
        # Supabase, named from the other side of the network namespace.
        "http://host.docker.internal:54321",
    ],
)
def test_loopback_backends_are_accepted(tmp_path: Path, url: str) -> None:
    artifact = tmp_path / "cli"
    artifact.write_bytes(b"x")
    binding = tmp_path / "project.json"
    binding.write_text("{}", encoding="utf-8")

    config = FaithfulAgentConfig(
        artifact_path=artifact,
        artifact_sha256="a" * 64,
        artifact_source_kind="local-dirty-worktree",
        credentials_path=binding,
        preferences_path=binding,
        project_binding_path=binding,
        backend_url=url,
        backend_anon_key="public-key",
    )

    assert config.backend_url == url


def test_the_task_image_override_reaches_the_generated_dockerfile(monkeypatch) -> None:
    from stele_bench import harbor_pack

    assert harbor_pack._dockerfile().startswith(f"FROM {harbor_pack.HARBOR_TASK_IMAGE}\n")
    monkeypatch.setenv(harbor_pack.TASK_IMAGE_ENV, "harbor-local/claude-rust:2.1.296-rust-1.88.0")
    assert harbor_pack._dockerfile().startswith(
        "FROM harbor-local/claude-rust:2.1.296-rust-1.88.0\n"
    )
    monkeypatch.setenv(harbor_pack.TASK_IMAGE_ENV, "docker.io/someone/else:latest")
    with pytest.raises(ValueError, match="harbor-local/claude-rust"):
        harbor_pack._dockerfile()
