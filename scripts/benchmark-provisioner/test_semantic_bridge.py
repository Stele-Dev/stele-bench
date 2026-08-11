from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import pytest
import semantic_bridge as bridge


@dataclass
class _Response:
    payload: object
    status: int = 200

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        return json.dumps(self.payload).encode("utf-8")[:amount]


def _request(url: str, body: object, authorization: str | None = "Bearer caller.jwt") -> Any:
    headers = {"Content-Type": "application/json"}
    if authorization is not None:
        headers["Authorization"] = authorization
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=2)


def test_bridge_embeds_then_calls_rpc_with_caller_jwt() -> None:
    calls: list[tuple[str, dict[str, str], dict[str, object], float]] = []

    def opener(request: urllib.request.Request, *, timeout: float) -> _Response:
        payload = json.loads(request.data or b"null")
        headers = {name.lower(): value for name, value in request.header_items()}
        calls.append((request.full_url, headers, payload, timeout))
        if request.full_url.endswith("/auth/v1/user"):
            return _Response({"id": "attempt-user"})
        if request.full_url == bridge.GATEWAY_EMBEDDINGS_URL:
            return _Response({"data": [{"embedding": [0.25] * 1536}]})
        return _Response([{"id": "node-1", "score": 0.875}])

    config = bridge.SemanticBridgeConfig(
        gateway_key="gateway-secret",
        supabase_url="http://host.docker.internal:54321",
        supabase_anon_key="anon-public",
        timeout_seconds=1.5,
        opener=opener,
    )
    with bridge.start_semantic_bridge(config) as running:
        thread = running._thread
        with _request(
            running.local_endpoint,
            {
                "query": "why do keys leak into navigation?",
                "projectId": "project-1",
                "activeWorkspaceIds": ["workspace-1"],
                "limit": 4,
                "includeSuperseded": True,
                "includeDone": True,
            },
        ) as response:
            assert json.load(response) == {"seeds": [{"id": "node-1", "score": 0.875}]}

    assert thread is not None and not thread.is_alive()
    assert [call[0] for call in calls] == [
        "http://127.0.0.1:54321/auth/v1/user",
        "https://ai-gateway.vercel.sh/v1/embeddings",
        "http://127.0.0.1:54321/rest/v1/rpc/stele_semantic_seeds",
    ]
    assert calls[0][1]["authorization"] == "Bearer caller.jwt"
    assert calls[0][1]["apikey"] == "anon-public"
    assert calls[1][1]["authorization"] == "Bearer gateway-secret"
    assert calls[1][2] == {
        "model": "openai/text-embedding-3-small",
        "input": "why do keys leak into navigation?",
        "encoding_format": "float",
    }
    assert calls[2][1]["authorization"] == "Bearer caller.jwt"
    assert calls[2][1]["apikey"] == "anon-public"
    assert calls[2][2]["p_project_id"] == "project-1"
    assert calls[2][2]["p_active_workspaces"] == ["workspace-1"]
    assert calls[2][2]["p_limit"] == 4
    assert calls[2][2]["p_include_superseded"] is True
    assert calls[2][2]["p_include_done"] is True
    assert len(str(calls[2][2]["p_embedding"]).strip("[]").split(",")) == 1536
    assert calls[2][3] == 1.5


def test_bridge_rejects_missing_bearer_without_calling_upstream() -> None:
    called = False

    def opener(request: urllib.request.Request, *, timeout: float) -> _Response:
        nonlocal called
        called = True
        return _Response({})

    config = bridge.SemanticBridgeConfig(
        gateway_key="gateway-secret",
        supabase_url="http://127.0.0.1:54321",
        supabase_anon_key="anon-public",
        opener=opener,
    )
    with bridge.start_semantic_bridge(config) as running:
        with pytest.raises(urllib.error.HTTPError) as captured:
            _request(
                running.local_endpoint,
                {"query": "hello", "projectId": "project-1"},
                authorization=None,
            )

    assert captured.value.code == 401
    assert called is False


def test_bridge_rejects_request_without_per_run_endpoint_secret() -> None:
    called = False

    def opener(request: urllib.request.Request, *, timeout: float) -> _Response:
        nonlocal called
        called = True
        return _Response({})

    config = bridge.SemanticBridgeConfig(
        gateway_key="gateway-secret",
        supabase_url="http://127.0.0.1:54321",
        supabase_anon_key="anon-public",
        opener=opener,
    )
    with bridge.start_semantic_bridge(config) as running:
        endpoint_without_secret = running.local_endpoint.split("?", 1)[0]
        with pytest.raises(urllib.error.HTTPError) as captured:
            _request(
                endpoint_without_secret,
                {"query": "hello", "projectId": "project-1"},
            )

    assert captured.value.code == 401
    assert called is False


def test_bridge_bounds_request_body_before_calling_upstream() -> None:
    config = bridge.SemanticBridgeConfig(
        gateway_key="gateway-secret",
        supabase_url="http://127.0.0.1:54321",
        supabase_anon_key="anon-public",
        opener=lambda *args, **kwargs: pytest.fail("upstream must not be called"),
    )
    with bridge.start_semantic_bridge(config) as running:
        request = urllib.request.Request(
            running.local_endpoint,
            data=b"x" * (bridge.MAX_REQUEST_BYTES + 1),
            headers={"Authorization": "Bearer caller.jwt"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(request, timeout=2)

    assert captured.value.code == 413


def test_bridge_returns_generic_error_for_malformed_upstream() -> None:
    def opener(request: urllib.request.Request, *, timeout: float) -> _Response:
        if request.full_url.endswith("/auth/v1/user"):
            return _Response({"id": "attempt-user"})
        return _Response({"data": [{"embedding": [1.0]}]})

    config = bridge.SemanticBridgeConfig(
        gateway_key="gateway-secret",
        supabase_url="http://127.0.0.1:54321",
        supabase_anon_key="anon-public",
        opener=opener,
    )
    with bridge.start_semantic_bridge(config) as running:
        with pytest.raises(urllib.error.HTTPError) as captured:
            _request(running.local_endpoint, {"query": "hello", "projectId": "project-1"})
        assert json.loads(captured.value.read()) == {"error": "upstream_failed"}

    assert captured.value.code == 502


def test_bridge_exposes_ephemeral_host_bound_port() -> None:
    config = bridge.SemanticBridgeConfig(
        gateway_key="gateway-secret",
        supabase_url="http://127.0.0.1:54321",
        supabase_anon_key="anon-public",
    )
    first = bridge.start_semantic_bridge(config)
    second = bridge.start_semantic_bridge(config)
    with first as one, second as two:
        assert one.port > 0
        assert two.port > 0
        assert one.port != two.port
        assert one.container_endpoint.startswith("http://host.docker.internal:")
        assert "?bridge_token=" in one.container_endpoint
        assert isinstance(one._thread, threading.Thread)


def test_bridge_rejects_invalid_bearer_before_paid_embedding() -> None:
    calls: list[str] = []

    def opener(request: urllib.request.Request, *, timeout: float) -> _Response:
        calls.append(request.full_url)
        return _Response({"message": "invalid token"}, status=401)

    config = bridge.SemanticBridgeConfig(
        gateway_key="gateway-secret",
        supabase_url="http://127.0.0.1:54321",
        supabase_anon_key="anon-public",
        opener=opener,
    )
    with bridge.start_semantic_bridge(config) as running:
        with pytest.raises(urllib.error.HTTPError) as captured:
            _request(
                running.local_endpoint,
                {"query": "hello", "projectId": "project-1"},
                authorization="Bearer arbitrary-nonempty-value",
            )

    assert captured.value.code == 401
    assert calls == ["http://127.0.0.1:54321/auth/v1/user"]


def test_bridge_default_admits_one_validated_token_and_rejects_another() -> None:
    auth_calls = 0

    def opener(request: urllib.request.Request, *, timeout: float) -> _Response:
        nonlocal auth_calls
        if request.full_url.endswith("/auth/v1/user"):
            auth_calls += 1
            return _Response({"id": "attempt-user"})
        if request.full_url == bridge.GATEWAY_EMBEDDINGS_URL:
            return _Response({"data": [{"embedding": [0.25] * 1536}]})
        return _Response([])

    config = bridge.SemanticBridgeConfig(
        gateway_key="gateway-secret",
        supabase_url="http://127.0.0.1:54321",
        supabase_anon_key="anon-public",
        opener=opener,
    )
    with bridge.start_semantic_bridge(config) as running:
        with _request(
            running.local_endpoint,
            {"query": "first", "projectId": "project-1"},
            authorization="Bearer attempt-token",
        ) as response:
            assert json.load(response) == {"seeds": []}
        with pytest.raises(urllib.error.HTTPError) as captured:
            _request(
                running.local_endpoint,
                {"query": "second", "projectId": "project-1"},
                authorization="Bearer another-valid-token",
            )

    assert captured.value.code == 401
    assert auth_calls == 1


def test_bridge_gives_each_validated_token_an_independent_paid_attempt_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "MAX_PAID_ATTEMPTS_PER_TOKEN", 2)
    embedding_calls = 0

    def opener(request: urllib.request.Request, *, timeout: float) -> _Response:
        nonlocal embedding_calls
        if request.full_url.endswith("/auth/v1/user"):
            return _Response({"id": "attempt-user"})
        if request.full_url == bridge.GATEWAY_EMBEDDINGS_URL:
            embedding_calls += 1
            return _Response({"data": [{"embedding": [0.25] * 1536}]})
        return _Response([])

    config = bridge.SemanticBridgeConfig(
        gateway_key="gateway-secret",
        supabase_url="http://127.0.0.1:54321",
        supabase_anon_key="anon-public",
        expected_token_count=2,
        opener=opener,
    )
    with bridge.start_semantic_bridge(config) as running:
        for query in ("one", "two"):
            with _request(
                running.local_endpoint,
                {"query": query, "projectId": "project-1"},
                authorization="Bearer attempt-one",
            ) as response:
                assert json.load(response) == {"seeds": []}
        with pytest.raises(urllib.error.HTTPError) as captured:
            _request(
                running.local_endpoint,
                {"query": "three", "projectId": "project-1"},
                authorization="Bearer attempt-one",
            )
        for query in ("four", "five"):
            with _request(
                running.local_endpoint,
                {"query": query, "projectId": "project-1"},
                authorization="Bearer attempt-two",
            ) as response:
                assert json.load(response) == {"seeds": []}

    assert captured.value.code == 429
    assert json.loads(captured.value.read()) == {"error": "request_limit_reached"}
    assert embedding_calls == 4
    assert running.paid_attempts_consumed == 4
    assert running.authenticated_token_count == 2
    assert running.exhausted_token_count == 2


@pytest.mark.parametrize("value", [0, 5, True, 1.5])
def test_bridge_rejects_invalid_expected_token_count(value: object) -> None:
    with pytest.raises(ValueError, match="expected_token_count"):
        bridge.SemanticBridgeConfig(
            gateway_key="gateway-secret",
            supabase_url="http://127.0.0.1:54321",
            supabase_anon_key="anon-public",
            expected_token_count=value,  # type: ignore[arg-type]
        )


def test_bridge_accepts_hard_authenticated_token_ceiling() -> None:
    config = bridge.SemanticBridgeConfig(
        gateway_key="gateway-secret",
        supabase_url="http://127.0.0.1:54321",
        supabase_anon_key="anon-public",
        expected_token_count=bridge.MAX_AUTHENTICATED_TOKENS,
    )

    assert config.expected_token_count == 4
    assert bridge.MAX_TOTAL_PAID_ATTEMPTS == 128
    assert bridge.SemanticBridgeConfig(
        gateway_key="gateway-secret",
        supabase_url="http://127.0.0.1:54321",
        supabase_anon_key="anon-public",
    ).expected_token_count == 1


def test_bridge_rejects_unknown_token_after_expected_count_without_auth_call() -> None:
    auth_calls = 0

    def opener(request: urllib.request.Request, *, timeout: float) -> _Response:
        nonlocal auth_calls
        if request.full_url.endswith("/auth/v1/user"):
            auth_calls += 1
            return _Response({"id": f"attempt-user-{auth_calls}"})
        if request.full_url == bridge.GATEWAY_EMBEDDINGS_URL:
            return _Response({"data": [{"embedding": [0.25] * 1536}]})
        return _Response([])

    config = bridge.SemanticBridgeConfig(
        gateway_key="gateway-secret",
        supabase_url="http://127.0.0.1:54321",
        supabase_anon_key="anon-public",
        expected_token_count=2,
        opener=opener,
    )
    with bridge.start_semantic_bridge(config) as running:
        for token in ("attempt-one", "attempt-two"):
            with _request(
                running.local_endpoint,
                {"query": token, "projectId": "project-1"},
                authorization=f"Bearer {token}",
            ) as response:
                assert json.load(response) == {"seeds": []}
        with pytest.raises(urllib.error.HTTPError) as captured:
            _request(
                running.local_endpoint,
                {"query": "overflow", "projectId": "project-1"},
                authorization="Bearer attempt-three",
            )

    assert captured.value.code == 401
    assert auth_calls == 2
    assert running.authenticated_token_count == 2
