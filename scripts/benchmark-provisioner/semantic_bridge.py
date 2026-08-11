#!/usr/bin/env python3
"""Short-lived semantic-seeds bridge for isolated benchmark containers."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

GATEWAY_EMBEDDINGS_URL = "https://ai-gateway.vercel.sh/v1/embeddings"
EMBEDDING_MODEL = "openai/text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
MAX_QUERY_CHARS = 8_000
MAX_REQUEST_BYTES = 64 * 1024
MAX_UPSTREAM_BYTES = 2 * 1024 * 1024
MAX_LIMIT = 50
DEFAULT_LIMIT = 10
MAX_PAID_ATTEMPTS_PER_TOKEN = 32
MAX_AUTHENTICATED_TOKENS = 4
MAX_TOTAL_PAID_ATTEMPTS = MAX_PAID_ATTEMPTS_PER_TOKEN * MAX_AUTHENTICATED_TOKENS

OpenUrl = Callable[..., Any]


class SemanticBridgeError(RuntimeError):
    """An upstream call could not produce a safe semantic-seeds response."""


class InvalidAuthorizationError(SemanticBridgeError):
    """The local Auth service rejected the caller's bearer token."""


@dataclass(frozen=True)
class SemanticBridgeConfig:
    gateway_key: str
    supabase_url: str
    supabase_anon_key: str
    timeout_seconds: float = 10.0
    expected_token_count: int = 1
    opener: OpenUrl = urllib.request.urlopen

    def __post_init__(self) -> None:
        if not self.gateway_key or not self.supabase_anon_key:
            raise ValueError("semantic bridge credentials must be non-empty")
        parsed = urllib.parse.urlsplit(self.supabase_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("supabase_url must be an absolute HTTP(S) URL")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 60:
            raise ValueError("timeout_seconds must be greater than zero and at most 60")
        if (
            isinstance(self.expected_token_count, bool)
            or not isinstance(self.expected_token_count, int)
            or self.expected_token_count < 1
            or self.expected_token_count > MAX_AUTHENTICATED_TOKENS
        ):
            raise ValueError(
                "expected_token_count must be an integer between 1 and "
                f"{MAX_AUTHENTICATED_TOKENS}"
            )


def _host_reachable_url(url: str) -> str:
    """Translate Docker's host alias when this host-side bridge calls Supabase."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname != "host.docker.internal":
        return url.rstrip("/")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urllib.parse.urlunsplit(
        (parsed.scheme, f"127.0.0.1{port}", parsed.path.rstrip("/"), "", "")
    )


def _read_json_response(response: Any) -> object:
    raw = response.read(MAX_UPSTREAM_BYTES + 1)
    if len(raw) > MAX_UPSTREAM_BYTES:
        raise SemanticBridgeError("upstream_response_too_large")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SemanticBridgeError("upstream_response_is_not_json") from error


class SemanticBridge:
    """Lifecycle-managed HTTP bridge reachable from Docker via the host alias."""

    def __init__(self, config: SemanticBridgeConfig) -> None:
        self._config = config
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._slots = threading.BoundedSemaphore(4)
        self._state_lock = threading.Lock()
        self._paid_attempts_by_token: dict[bytes, int] = {}
        self._endpoint_secret = secrets.token_urlsafe(32)

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("semantic bridge has not started")
        return int(self._server.server_address[1])

    @property
    def container_endpoint(self) -> str:
        token = urllib.parse.quote(self._endpoint_secret, safe="")
        return (
            f"http://host.docker.internal:{self.port}/api/semantic-seeds"
            f"?bridge_token={token}"
        )

    @property
    def local_endpoint(self) -> str:
        token = urllib.parse.quote(self._endpoint_secret, safe="")
        return (
            f"http://127.0.0.1:{self.port}/api/semantic-seeds"
            f"?bridge_token={token}"
        )

    @property
    def paid_attempts_consumed(self) -> int:
        """Return charged-or-potentially-charged embedding attempts so far."""
        with self._state_lock:
            return sum(self._paid_attempts_by_token.values())

    @property
    def authenticated_token_count(self) -> int:
        """Return the number of distinct validated attempt tokens admitted."""
        with self._state_lock:
            return len(self._paid_attempts_by_token)

    @property
    def exhausted_token_count(self) -> int:
        """Return how many admitted token buckets have reached their ceiling."""
        with self._state_lock:
            return sum(
                attempts >= MAX_PAID_ATTEMPTS_PER_TOKEN
                for attempts in self._paid_attempts_by_token.values()
            )

    def __enter__(self) -> SemanticBridge:
        if self._server is not None:
            raise RuntimeError("semantic bridge is already running")
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                bridge._handle(self)

            def do_GET(self) -> None:  # noqa: N802
                bridge._respond(self, 405, {"error": "method_not_allowed"})

            def log_message(self, format: str, *args: object) -> None:
                # Never let request headers, URLs, or upstream errors leak secrets.
                return

        self._server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="benchmark-semantic-bridge",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)

    @staticmethod
    def _respond(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(body)

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        parsed_path = urllib.parse.urlsplit(handler.path)
        if parsed_path.path != "/api/semantic-seeds":
            self._respond(handler, 404, {"error": "not_found"})
            return
        endpoint_tokens = urllib.parse.parse_qs(
            parsed_path.query, keep_blank_values=True
        ).get("bridge_token", [])
        if (
            len(endpoint_tokens) != 1
            or not secrets.compare_digest(endpoint_tokens[0], self._endpoint_secret)
        ):
            self._respond(handler, 401, {"error": "not_authenticated"})
            return
        authorization = handler.headers.get("Authorization", "")
        if not authorization.lower().startswith("bearer "):
            self._respond(handler, 401, {"error": "not_authenticated"})
            return
        bearer_token = authorization[7:].strip()
        if not bearer_token:
            self._respond(handler, 401, {"error": "not_authenticated"})
            return
        authorization = f"Bearer {bearer_token}"
        try:
            content_length = int(handler.headers.get("Content-Length", ""))
        except ValueError:
            self._respond(handler, 400, {"error": "invalid_content_length"})
            return
        if content_length < 0 or content_length > MAX_REQUEST_BYTES:
            self._respond(handler, 413, {"error": "request_too_large"})
            return
        try:
            body = json.loads(handler.rfile.read(content_length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._respond(handler, 400, {"error": "invalid_json"})
            return
        if not isinstance(body, dict):
            self._respond(handler, 400, {"error": "invalid_request"})
            return
        query = body.get("query", "")
        if not isinstance(query, str):
            self._respond(handler, 400, {"error": "invalid_query"})
            return
        query = query.strip()
        if not query:
            self._respond(handler, 200, {"seeds": []})
            return
        project_id = body.get("projectId")
        if not isinstance(project_id, str) or not project_id.strip():
            self._respond(handler, 400, {"error": "missing_project"})
            return
        workspace_ids = body.get("activeWorkspaceIds", [])
        if (
            not isinstance(workspace_ids, list)
            or len(workspace_ids) > 64
            or any(not isinstance(value, str) for value in workspace_ids)
        ):
            self._respond(handler, 400, {"error": "invalid_workspaces"})
            return
        raw_limit = body.get("limit", DEFAULT_LIMIT)
        limit = (
            max(1, min(MAX_LIMIT, math.floor(raw_limit)))
            if isinstance(raw_limit, (int, float))
            and not isinstance(raw_limit, bool)
            and math.isfinite(raw_limit)
            else DEFAULT_LIMIT
        )
        try:
            token_digest = self._authorize(bearer_token, authorization)
        except (SemanticBridgeError, OSError, TimeoutError, urllib.error.URLError):
            self._respond(handler, 502, {"error": "upstream_failed"})
            return
        if token_digest is None:
            self._respond(handler, 401, {"error": "not_authenticated"})
            return
        if not self._slots.acquire(blocking=False):
            self._respond(handler, 503, {"error": "bridge_busy"})
            return
        try:
            with self._state_lock:
                attempts = self._paid_attempts_by_token[token_digest]
                if attempts >= MAX_PAID_ATTEMPTS_PER_TOKEN:
                    self._respond(handler, 429, {"error": "request_limit_reached"})
                    return
                # Count attempts before the Gateway call: a failed or malformed
                # embedding response may still have incurred provider cost.
                self._paid_attempts_by_token[token_digest] = attempts + 1
            seeds = self._semantic_seeds(
                query=query[:MAX_QUERY_CHARS],
                project_id=project_id,
                workspace_ids=workspace_ids,
                limit=limit,
                include_superseded=body.get("includeSuperseded") is True,
                include_done=body.get("includeDone") is True,
                authorization=authorization,
            )
        except (SemanticBridgeError, OSError, TimeoutError, urllib.error.URLError):
            self._respond(handler, 502, {"error": "upstream_failed"})
        else:
            self._respond(handler, 200, {"seeds": seeds})
        finally:
            self._slots.release()

    def _authorize(self, bearer_token: str, authorization: str) -> bytes | None:
        """Validate and admit a bounded number of distinct attempt tokens."""
        digest = hashlib.sha256(bearer_token.encode("utf-8")).digest()
        with self._state_lock:
            if digest in self._paid_attempts_by_token:
                return digest
            if len(self._paid_attempts_by_token) >= self._config.expected_token_count:
                return None

        auth_url = f"{_host_reachable_url(self._config.supabase_url)}/auth/v1/user"
        try:
            identity = self._get_json(
                auth_url,
                {
                    "apikey": self._config.supabase_anon_key,
                    "Authorization": authorization,
                },
            )
        except InvalidAuthorizationError:
            return None
        if not isinstance(identity, dict) or not isinstance(identity.get("id"), str):
            raise SemanticBridgeError("invalid_auth_response")
        if not identity["id"]:
            raise SemanticBridgeError("invalid_auth_response")

        with self._state_lock:
            if digest in self._paid_attempts_by_token:
                return digest
            if len(self._paid_attempts_by_token) >= self._config.expected_token_count:
                return None
            self._paid_attempts_by_token[digest] = 0
            return digest

    def _get_json(self, url: str, headers: dict[str, str]) -> object:
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._config.opener(request, timeout=self._config.timeout_seconds) as response:
                status = getattr(response, "status", 200)
                if status in (401, 403):
                    raise InvalidAuthorizationError("authorization_rejected")
                if not isinstance(status, int) or status < 200 or status >= 300:
                    raise SemanticBridgeError("upstream_http_error")
                return _read_json_response(response)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code in (401, 403):
                raise InvalidAuthorizationError("authorization_rejected") from error
            raise SemanticBridgeError("upstream_http_error") from error

    def _post_json(self, url: str, payload: object, headers: dict[str, str]) -> object:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with self._config.opener(request, timeout=self._config.timeout_seconds) as response:
                status = getattr(response, "status", 200)
                if not isinstance(status, int) or status < 200 or status >= 300:
                    raise SemanticBridgeError("upstream_http_error")
                return _read_json_response(response)
        except urllib.error.HTTPError as error:
            error.close()
            raise SemanticBridgeError("upstream_http_error") from error

    def _semantic_seeds(
        self,
        *,
        query: str,
        project_id: str,
        workspace_ids: list[str],
        limit: int,
        include_superseded: bool,
        include_done: bool,
        authorization: str,
    ) -> list[dict[str, object]]:
        embedded = self._post_json(
            GATEWAY_EMBEDDINGS_URL,
            {"model": EMBEDDING_MODEL, "input": query, "encoding_format": "float"},
            {"Authorization": f"Bearer {self._config.gateway_key}"},
        )
        if not isinstance(embedded, dict) or not isinstance(embedded.get("data"), list):
            raise SemanticBridgeError("invalid_embedding_response")
        data = embedded["data"]
        embedding = (
            data[0].get("embedding")
            if len(data) == 1 and isinstance(data[0], dict)
            else None
        )
        if (
            not isinstance(embedding, list)
            or len(embedding) != EMBEDDING_DIMENSIONS
            or any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                for value in embedding
            )
        ):
            raise SemanticBridgeError("invalid_embedding_response")
        rpc_url = (
            f"{_host_reachable_url(self._config.supabase_url)}"
            "/rest/v1/rpc/stele_semantic_seeds"
        )
        rows = self._post_json(
            rpc_url,
            {
                "p_project_id": project_id,
                "p_embedding": f"[{','.join(str(value) for value in embedding)}]",
                "p_active_workspaces": workspace_ids,
                "p_limit": limit,
                "p_include_superseded": include_superseded,
                "p_include_done": include_done,
            },
            {
                "apikey": self._config.supabase_anon_key,
                "Authorization": authorization,
            },
        )
        if not isinstance(rows, list) or len(rows) > limit:
            raise SemanticBridgeError("invalid_rpc_response")
        seeds: list[dict[str, object]] = []
        for row in rows:
            if not isinstance(row, dict):
                raise SemanticBridgeError("invalid_rpc_response")
            node_id, score = row.get("id"), row.get("score")
            if (
                not isinstance(node_id, str)
                or not node_id
                or not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not math.isfinite(score)
            ):
                raise SemanticBridgeError("invalid_rpc_response")
            seeds.append({"id": node_id, "score": float(score)})
        return seeds


def start_semantic_bridge(config: SemanticBridgeConfig) -> SemanticBridge:
    """Return a context manager that starts and cleanly stops the bridge."""
    return SemanticBridge(config)
