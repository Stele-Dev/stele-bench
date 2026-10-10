from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import model_provider
import pytest
import run_install_only_smoke as support
import run_scored_smoke as scored


def _job(tmp_path: Path) -> Path:
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"agents": [{"name": "claude-code"}]}), encoding="utf-8")
    return path


def _configure(path: Path, model: str = "anthropic/claude-haiku-4.5") -> dict:
    support.configure_agent_job(
        path,
        model=model,
        max_turns=60,
        max_budget_usd="5.00",
        gateway_url=model_provider.GATEWAY_URL,
    )
    return json.loads(path.read_text(encoding="utf-8"))["agents"][0]


@pytest.mark.parametrize(
    ("model", "direct"),
    [
        ("anthropic/claude-haiku-4.5", "claude-haiku-4-5"),
        ("anthropic/claude-sonnet-5.5", "claude-sonnet-5-5"),
        ("claude-opus-5-5", "claude-opus-5-5"),
        ("anthropic/claude-haiku-5.5", "claude-haiku-5-5"),
    ],
)
def test_benchmark_model_names_translate_to_first_party_ids(model: str, direct: str) -> None:
    assert model_provider.direct_model_id(model) == direct


@pytest.mark.parametrize(
    "model", ["openai/gpt-5", "anthropic/claude-mystery-9", "anthropic/../claude-haiku-4.5"]
)
def test_unpriced_or_foreign_models_are_refused_before_a_run(model: str) -> None:
    with pytest.raises(model_provider.ModelProviderError):
        model_provider.direct_model_id(model)


def test_the_gateway_agent_config_keeps_its_credential_wiring(tmp_path: Path) -> None:
    agent = _configure(_job(tmp_path))
    assert agent["model_name"] == "anthropic/claude-haiku-4.5"
    assert agent["env"] == {
        "ANTHROPIC_BASE_URL": "https://ai-gateway.vercel.sh",
        "ANTHROPIC_AUTH_TOKEN": "${AI_GATEWAY_KEY_EVAL}",
        "ANTHROPIC_API_KEY": "",
    }


def test_the_anthropic_agent_config_bills_the_direct_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(model_provider.PROVIDER_ENV, "anthropic")
    agent = _configure(_job(tmp_path))
    assert agent["model_name"] == "claude-haiku-4-5"
    assert agent["env"] == {
        "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
        "ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY_EVAL}",
        "ANTHROPIC_AUTH_TOKEN": "",
    }
    assert agent["kwargs"]["max_budget_usd"] == "5.00"


def test_the_anthropic_agent_config_refuses_an_unpriced_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(model_provider.PROVIDER_ENV, "anthropic")
    with pytest.raises(support.InstallOnlySmokeError, match="not_available_on_provider"):
        _configure(_job(tmp_path), model="openai/gpt-5")


def test_harbor_receives_only_the_selected_providers_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_GATEWAY_KEY_EVAL", "gateway-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY_EVAL", "direct-secret")
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY_EVAL", "admin-secret")

    gateway = support._child_environment(include_gateway=True)
    assert gateway["AI_GATEWAY_KEY_EVAL"] == "gateway-secret"
    assert gateway["ANTHROPIC_BASE_URL"] == "https://ai-gateway.vercel.sh"
    assert "ANTHROPIC_API_KEY_EVAL" not in gateway

    monkeypatch.setenv(model_provider.PROVIDER_ENV, "anthropic")
    direct = support._child_environment(include_gateway=True)
    assert direct["ANTHROPIC_API_KEY_EVAL"] == "direct-secret"
    assert direct["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"
    assert "AI_GATEWAY_KEY_EVAL" not in direct
    # The Admin key reads billing reports on the host; it never reaches Harbor.
    assert "ANTHROPIC_ADMIN_KEY_EVAL" not in direct
    assert "ANTHROPIC_ADMIN_KEY_EVAL" not in support._child_environment()


def test_receipts_find_the_cli_usage_under_the_direct_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = {"modelUsage": {"claude-haiku-4-5": {"inputTokens": 10}}}
    assert scored._model_usage("anthropic/claude-haiku-4.5", raw) is None
    monkeypatch.setenv(model_provider.PROVIDER_ENV, "anthropic")
    assert scored._model_usage("anthropic/claude-haiku-4.5", raw) == {"inputTokens": 10}


def test_usage_rows_are_priced_with_cache_multipliers() -> None:
    cost = model_provider.usage_cost_usd(
        "claude-sonnet-5-5",
        uncached_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
        cache_write_5m_tokens=1_000_000,
        cache_write_1h_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    # $2 input + $0.20 cache read + $2.50 five-minute write + $4 one-hour write + $10 output.
    assert cost == Decimal("18.70")


def test_an_invalid_provider_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(model_provider.PROVIDER_ENV, "bedrock")
    with pytest.raises(model_provider.ModelProviderError):
        model_provider.current()


def test_provider_and_image_survive_the_scoped_child_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pack generation runs inside a scrubbed environment; a provider lookup there
    # must not silently fall back to the Gateway, nor the image to its default.
    monkeypatch.setenv(model_provider.PROVIDER_ENV, "anthropic")
    monkeypatch.setenv("STELE_BENCH_TASK_IMAGE", "harbor-local/claude-rust:2.1.296-rust-1.88.0")
    with support._scoped_child_environment():
        assert model_provider.current() == "anthropic"
        import os

        assert os.environ["STELE_BENCH_TASK_IMAGE"].endswith("2.1.296-rust-1.88.0")


def test_the_output_cap_reaches_harbor_outside_the_scrubbed_agent_env(tmp_path: Path) -> None:
    # Harbor scrubs the value of every agent env key containing TOKEN from the
    # retained evidence; "2048" inside a trajectory then broke its JSON.
    agent = _configure(_job(tmp_path))
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in agent["env"]
    assert (
        support._child_environment(include_gateway=True)["CLAUDE_CODE_MAX_OUTPUT_TOKENS"]
        == support.AGENT_MAX_OUTPUT_TOKENS
    )


@pytest.mark.parametrize(
    ("command", "forbidden"),
    [
        ("git clone https://example.com/repo", True),
        ('cd /workspace && git -C "/workspace" log --oneline', True),
        ('bash -c "git fetch origin"', True),
        ("git status", False),
        (
            "cat >> src/cache.rs <<'EOF'\n    let key = \"git\");\n"
            "        data.entries.insert(key.clone(), entry);\nEOF",
            False,
        ),
    ],
)
def test_the_repository_access_guard_reads_commands_not_prose(
    command: str, forbidden: bool
) -> None:
    trajectory = {
        "steps": [
            {
                "source": "agent",
                "tool_calls": [{"function_name": "Bash", "arguments": {"command": command}}],
            }
        ]
    }
    assert scored._forbidden_repository_access(trajectory) is forbidden
