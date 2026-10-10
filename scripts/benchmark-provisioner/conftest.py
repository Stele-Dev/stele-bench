"""Shared test guards for the maintainer provisioner scripts."""

from __future__ import annotations

import anthropic_reserve
import gateway_reserve
import model_provider
import pytest


@pytest.fixture(autouse=True)
def _no_live_gateway_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a test would query the real AI Gateway.

    This is not hypothetical tidiness: an earlier revision defaulted
    ``run_pair``'s between-arms tripwire to the live getter, so the oracle
    tests were quietly issuing authenticated requests to Vercel with a fake
    key and passing on the 401. Tests must inject their own getter; anything
    that reaches this guard is a seam that was left open.
    """

    def refuse(url: str, api_key: str) -> tuple[int, object]:
        raise AssertionError(
            f"a test tried to call the live AI Gateway ({url}); inject a getter instead"
        )

    monkeypatch.setattr(gateway_reserve, "_get_json", refuse)


@pytest.fixture(autouse=True)
def _no_live_anthropic_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same guard for the direct-Anthropic fence, plus a clean provider.

    The oracle exports the selected provider into the process environment, so a
    test that selected ``anthropic`` would otherwise leak it into every later
    test.
    """

    def refuse(url: str, api_key: str) -> tuple[int, object]:
        raise AssertionError(
            f"a test tried to call the live Anthropic API ({url}); inject a getter instead"
        )

    monkeypatch.setattr(anthropic_reserve, "_get_json", refuse)
    monkeypatch.delenv(model_provider.PROVIDER_ENV, raising=False)
