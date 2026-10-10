"""Which API the benchmark agent's model calls are billed through.

Two providers exist, and a run uses exactly one of them for every arm:

``gateway`` (the default)
    Claude Code talks to the Vercel AI Gateway with ``AI_GATEWAY_KEY_EVAL``.
    This is what every published run used, and its spending fence is
    :mod:`gateway_reserve`.

``anthropic``
    Claude Code talks to the first-party Anthropic API with
    ``ANTHROPIC_API_KEY_EVAL``. This is the only way to spend Anthropic API
    credits, which the Gateway cannot draw on. Its spending fence is
    :mod:`anthropic_reserve`.

Only the agent's model calls move. The semantic bridge still embeds through the
Gateway, because the Anthropic API has no embeddings endpoint, so a faithful
arm needs ``AI_GATEWAY_KEY_EVAL`` under either provider.

The oracle selects the provider once and exports it in
``STELE_BENCH_MODEL_PROVIDER``; every runner subprocess reads it from there, so
no arm can be configured for a different provider than the fence it was
checked against.
"""

from __future__ import annotations

import os
import re
from decimal import Decimal
from typing import Literal

PROVIDER_ENV = "STELE_BENCH_MODEL_PROVIDER"
GATEWAY = "gateway"
ANTHROPIC = "anthropic"
PROVIDERS = (GATEWAY, ANTHROPIC)
Provider = Literal["gateway", "anthropic"]

GATEWAY_URL = "https://ai-gateway.vercel.sh"
ANTHROPIC_URL = "https://api.anthropic.com"
GATEWAY_KEY_ENV = "AI_GATEWAY_KEY_EVAL"
ANTHROPIC_KEY_ENV = "ANTHROPIC_API_KEY_EVAL"
ANTHROPIC_ADMIN_KEY_ENV = "ANTHROPIC_ADMIN_KEY_EVAL"

# First-party list prices in USD per million tokens, used only to turn the
# Admin API's token counts into the between-arms drain estimate. Anthropic's
# cost report is authoritative for billing; keep this table in step with the
# pricing page when a model is added. Cache writes are 1.25x input for the
# five-minute TTL and 2x input for the one-hour TTL.
_PER_MILLION: dict[str, tuple[str, str, str]] = {
    # model id: (input, output, cache read)
    "claude-haiku-4-5": ("1.00", "5.00", "0.10"),
    "claude-haiku-5-5": ("0.10", "0.50", "0.01"),
    "claude-sonnet-4-6": ("3.00", "15.00", "0.30"),
    "claude-sonnet-5": ("2.00", "10.00", "0.20"),
    "claude-sonnet-5-5": ("2.00", "10.00", "0.20"),
    "claude-opus-4-8": ("5.00", "25.00", "0.50"),
    "claude-opus-5": ("5.00", "25.00", "0.50"),
    "claude-opus-5-5": ("4.00", "20.00", "0.20"),
}
_MILLION = Decimal(1_000_000)
_VERSION_DOT = re.compile(r"(?<=\d)\.(?=\d)")
# Snapshot ids such as ``claude-haiku-4-5-20251001`` price like their alias.
_SNAPSHOT_SUFFIX = re.compile(r"-20\d{6}$")
_MODEL_ID = re.compile(r"^claude-[a-z]+-[0-9][0-9a-z-]*$")


class ModelProviderError(ValueError):
    """The selected provider or model cannot be used for a paid run."""


def current() -> Provider:
    """The provider this process runs under; the Gateway when nothing is set."""

    value = os.environ.get(PROVIDER_ENV, "").strip() or GATEWAY
    if value not in PROVIDERS:
        raise ModelProviderError(
            f"{PROVIDER_ENV} must be one of {', '.join(PROVIDERS)}; got {value!r}"
        )
    return value  # type: ignore[return-value]


def activate(provider: str) -> Provider:
    """Select the provider for this process and every runner it launches."""

    if provider not in PROVIDERS:
        raise ModelProviderError(f"model provider must be one of {', '.join(PROVIDERS)}")
    os.environ[PROVIDER_ENV] = provider
    return provider  # type: ignore[return-value]


def key_env(provider: Provider | None = None) -> str:
    """The environment variable holding the agent's model-call key."""

    return ANTHROPIC_KEY_ENV if (provider or current()) == ANTHROPIC else GATEWAY_KEY_ENV


def direct_model_id(model: str) -> str:
    """Translate a benchmark model name into a first-party Anthropic model id.

    The benchmark names models the Gateway way (``anthropic/claude-haiku-4.5``).
    The first-party API wants ``claude-haiku-4-5``: no provider prefix, and
    dashes between version digits. An unpriced model is refused here, before a
    run starts, because the between-arms drain check could not price its usage.
    """

    name = model.strip()
    if "/" in name:
        prefix, _, name = name.rpartition("/")
        if prefix != "anthropic":
            raise ModelProviderError(f"the anthropic provider cannot run model {model!r}")
    name = _VERSION_DOT.sub("-", name)
    if not _MODEL_ID.fullmatch(name):
        raise ModelProviderError(f"{model!r} is not a Claude model id")
    if name not in _PER_MILLION:
        raise ModelProviderError(
            f"no price is recorded for {name}; add it to model_provider._PER_MILLION "
            "so the drain check can price this run"
        )
    return name


def harbor_model_name(model: str, provider: Provider | None = None) -> str:
    """The ``model_name`` written into the Harbor agent config.

    Under the Gateway the benchmark name is kept verbatim; the retained runs
    show Claude Code receiving ``anthropic/claude-haiku-4.5`` and the Gateway
    accepting it. The first-party API rejects a provider prefix, so the direct
    id is written bare. A bare id reaches the CLI unchanged whether or not
    Harbor strips everything before the last ``/``.
    """

    if (provider or current()) == ANTHROPIC:
        return direct_model_id(model)
    return model


def harbor_agent_env(provider: Provider | None = None) -> dict[str, str]:
    """Credentials and endpoint for the Claude Code agent inside the container.

    The key is the literal ``${...}`` placeholder: Harbor expands it at run
    time, so the secret never enters a config file kept as evidence. Each
    provider blanks the other credential variable so that a value inherited from
    anywhere else cannot redirect billing.
    """

    if (provider or current()) == ANTHROPIC:
        return {
            "ANTHROPIC_BASE_URL": ANTHROPIC_URL,
            "ANTHROPIC_API_KEY": "${" + ANTHROPIC_KEY_ENV + "}",
            "ANTHROPIC_AUTH_TOKEN": "",
        }
    return {
        "ANTHROPIC_BASE_URL": GATEWAY_URL,
        "ANTHROPIC_AUTH_TOKEN": "${" + GATEWAY_KEY_ENV + "}",
        "ANTHROPIC_API_KEY": "",
    }


def harbor_process_env(provider: Provider | None = None) -> dict[str, str]:
    """The host variables Harbor needs to expand :func:`harbor_agent_env`."""

    selected = provider or current()
    name = key_env(selected)
    environment = {
        "ANTHROPIC_BASE_URL": ANTHROPIC_URL if selected == ANTHROPIC else GATEWAY_URL,
    }
    value = os.environ.get(name)
    if value:
        environment[name] = value
    return environment


def usage_cost_usd(
    model: str,
    *,
    uncached_input_tokens: int,
    cache_read_input_tokens: int,
    cache_write_5m_tokens: int,
    cache_write_1h_tokens: int,
    output_tokens: int,
) -> Decimal:
    """Price one usage-report row at first-party list prices."""

    priced = _SNAPSHOT_SUFFIX.sub("", model)
    if priced not in _PER_MILLION:
        raise ModelProviderError(f"no price is recorded for {model}")
    input_rate, output_rate, cache_read_rate = (Decimal(rate) for rate in _PER_MILLION[priced])
    total = (
        Decimal(uncached_input_tokens) * input_rate
        + Decimal(cache_read_input_tokens) * cache_read_rate
        + Decimal(cache_write_5m_tokens) * input_rate * Decimal("1.25")
        + Decimal(cache_write_1h_tokens) * input_rate * 2
        + Decimal(output_tokens) * output_rate
    )
    return total / _MILLION
