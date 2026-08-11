#!/usr/bin/env python3
# ruff: noqa: E501
"""Reference local-Supabase implementation of the public provisioner protocol."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import secrets
import stat
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

GRAPH_FORMAT_VERSION = "stele-canonical-graph-v2"
BACKEND_COMPATIBILITY_ID = "stele-local-schema-2026-07"
EMBEDDING_MODEL_ID = "openai/text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
MEDIA_TYPE = "application/json"
REQUEST_LIMIT = 2_000_000
GRAPH_ARTIFACT_LIMIT = 100_000_000
COMMAND_TIMEOUT_SECONDS = 30

SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
PROJECT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
SHORT_ID = re.compile(r"^(TASK|KNOW|COMP|DOC|DIAG)-[1-9][0-9]*$")
UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
PORTABLE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
SHORT_ID_PREFIX_RANK = {"COMP": 0, "KNOW": 1, "TASK": 2, "DOC": 3, "DIAG": 4}
NODE_TYPE_PREFIX = {
    "component": "COMP",
    "knowledge": "KNOW",
    "task": "TASK",
    "document": "DOC",
    "diagram": "DIAG",
}
# Node lifecycle states a frozen graph may carry. Knowledge and components use
# active/archived; tasks additionally carry their workflow states.
NODE_STATUSES = frozenset(
    {"active", "archived", "backlog", "open", "in_progress", "done", "cancelled"}
)
ATTEMPT_STATE_VERSION = 1
PRIVATE_FILE_LIMIT = 100_000
ATTEMPT_RECOVERY_MARKER = "local_graph_attempt_recovery_v1"


class ProvisionerError(RuntimeError):
    """An operator-safe failure; details are written only to stderr."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _configuration() -> tuple[str, str, str]:
    values = (
        os.environ.get("STELE_BENCH_DB_CONTAINER", "supabase_db_stele"),
        os.environ.get("STELE_BENCH_DB_USER", "postgres"),
        os.environ.get("STELE_BENCH_DB_NAME", "postgres"),
    )
    if any(not SAFE_NAME.fullmatch(value) for value in values):
        raise ProvisionerError("database configuration contains unsupported characters")
    return values


def _psql(sql: str) -> str:
    container, user, database = _configuration()
    try:
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                container,
                "psql",
                "-U",
                user,
                "-d",
                database,
                "-X",
                "-qAt",
                "-v",
                "ON_ERROR_STOP=1",
            ],
            input=sql,
            text=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as error:
        raise ProvisionerError("docker executable is unavailable") from error
    except subprocess.TimeoutExpired as error:
        raise ProvisionerError("database command timed out") from error
    if result.returncode:
        diagnostic = result.stderr.strip().splitlines()
        if diagnostic:
            boundary = [diagnostic[0]]
            if diagnostic[-1] != diagnostic[0]:
                boundary.append(diagnostic[-1])
            detail = " | ".join(boundary)[:500]
        else:
            detail = "database command failed"
        raise ProvisionerError(detail)
    lines = [line for line in result.stdout.splitlines() if line]
    if len(lines) != 1:
        raise ProvisionerError("database command returned an unexpected response")
    return lines[0]


def _read_request() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(REQUEST_LIMIT + 1)
    if len(raw) > REQUEST_LIMIT:
        raise ProvisionerError("request exceeds the input limit")
    try:
        request = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProvisionerError("request must be one JSON object") from error
    if not isinstance(request, dict):
        raise ProvisionerError("request must be one JSON object")
    return request


def _require_object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProvisionerError(f"{name} must be an object")
    return value


def _require_exact_keys(value: dict[str, Any], keys: set[str], name: str) -> None:
    if set(value) != keys:
        raise ProvisionerError(f"{name} has unexpected fields")


def _read_bounded_artifact(path: Path) -> bytes:
    try:
        with path.open("rb") as artifact:
            raw = artifact.read(GRAPH_ARTIFACT_LIMIT + 1)
    except OSError as error:
        raise ProvisionerError("could not read graph artifact") from error
    if len(raw) > GRAPH_ARTIFACT_LIMIT:
        raise ProvisionerError("graph artifact exceeds the input limit")
    return raw


def _require_timestamp(value: object, name: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not UTC_TIMESTAMP.fullmatch(value):
        raise ProvisionerError(f"{name} must be a canonical UTC timestamp")


def _short_id_key(value: str) -> tuple[int, int]:
    match = SHORT_ID.fullmatch(value)
    if match is None:
        raise ProvisionerError("node short ID is not canonical")
    prefix, suffix = value.split("-", 1)
    return SHORT_ID_PREFIX_RANK[prefix], int(suffix)


def _quantized_embedding(
    value: object, name: str, *, require_quantized: bool = True
) -> list[float]:
    if not isinstance(value, list) or len(value) != EMBEDDING_DIMENSIONS:
        raise ProvisionerError(f"{name} must contain exactly 1536 numbers")
    result: list[float] = []
    for item in value:
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
        ):
            raise ProvisionerError(f"{name} must contain only finite numbers")
        if abs(item) > 65_504:
            raise ProvisionerError(f"{name} contains a number outside the binary16 range")
        quantized = struct.unpack(">e", struct.pack(">e", float(item)))[0]
        if require_quantized and quantized != float(item):
            raise ProvisionerError(f"{name} must already be binary16-quantized")
        result.append(quantized)
    return result


def _identity_digest(project: dict[str, Any]) -> str:
    identity = {
        "project_id": project["project_id"],
        "fixture_id": project["fixture_id"],
        "run_id": project["run_id"],
        "graph_arm": project["graph_arm"],
    }
    return hashlib.sha256(_canonical_bytes(identity)).hexdigest()


def _attempt_key(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()


def _attempt_root(*, create: bool = False) -> Path:
    root_value = os.environ.get("STELE_BENCH_ATTEMPT_ROOT")
    if not root_value:
        raise ProvisionerError("STELE_BENCH_ATTEMPT_ROOT is required")
    root = Path(root_value).expanduser().resolve()
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
    return root


def _attempt_directory(run_id: str, *, create_root: bool = False) -> Path:
    return _attempt_root(create=create_root) / _attempt_key(run_id)


def _open_attempt_directory(
    run_id: str, *, missing_ok: bool = False
) -> tuple[int, int, str] | None:
    root = _attempt_root()
    directory_name = _attempt_key(run_id)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(root, directory_flags | no_follow)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ProvisionerError("attempt directory is unavailable") from None
    except OSError as error:
        raise ProvisionerError("attempt root is unavailable") from error
    try:
        try:
            initial = os.stat(directory_name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                os.close(root_fd)
                return None
            raise ProvisionerError("attempt directory is unavailable") from None
        if stat.S_ISLNK(initial.st_mode) or not stat.S_ISDIR(initial.st_mode):
            raise ProvisionerError("attempt entry must be a directory, not a link")
        try:
            directory_fd = os.open(
                directory_name, directory_flags | no_follow, dir_fd=root_fd
            )
        except OSError as error:
            raise ProvisionerError("attempt directory could not be opened safely") from error
        metadata = os.fstat(directory_fd)
        if (
            (initial.st_dev, initial.st_ino) != (metadata.st_dev, metadata.st_ino)
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            os.close(directory_fd)
            raise ProvisionerError("attempt directory changed while opening")
        return root_fd, directory_fd, directory_name
    except Exception:
        os.close(root_fd)
        raise


def _write_private_json(path: Path, value: object) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > PRIVATE_FILE_LIMIT:
        raise ProvisionerError("private attempt file exceeds the size limit")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception as error:
        try:
            temporary.unlink()
        except OSError:
            pass
        if isinstance(error, ProvisionerError):
            raise
        raise ProvisionerError("could not write private attempt file") from error


def _read_private_json_at(directory_fd: int, filename: str, name: str) -> dict[str, Any]:
    try:
        initial = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(initial.st_mode):
            raise ProvisionerError(f"{name} must not be a symlink")
        descriptor = os.open(
            filename,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
    except ProvisionerError:
        raise
    except OSError as error:
        raise ProvisionerError(f"{name} is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if (initial.st_dev, initial.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ProvisionerError(f"{name} changed while opening")
        if not stat.S_ISREG(metadata.st_mode):
            raise ProvisionerError(f"{name} must be a regular file")
        if metadata.st_mode & 0o077:
            raise ProvisionerError(f"{name} permissions are too broad")
        if metadata.st_size > PRIVATE_FILE_LIMIT:
            raise ProvisionerError(f"{name} exceeds the size limit")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != metadata.st_size:
            raise ProvisionerError(f"{name} could not be read completely")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProvisionerError(f"{name} is invalid") from error
    return _require_object(value, name)


def _load_attempt_state_at(directory_fd: int, run_id: str) -> dict[str, Any]:
    state = _read_private_json_at(directory_fd, "state.json", "attempt state")
    _require_exact_keys(
        state, {"schema_version", "run_id", "user_id", "nonce"}, "attempt state"
    )
    if state["schema_version"] != ATTEMPT_STATE_VERSION or state["run_id"] != run_id:
        raise ProvisionerError("attempt state does not match the run")
    if not isinstance(state["user_id"], str) or not PROJECT_ID.fullmatch(state["user_id"]):
        raise ProvisionerError("attempt state user ID is invalid")
    if not isinstance(state["nonce"], str) or len(state["nonce"]) < 32:
        raise ProvisionerError("attempt state nonce is invalid")
    return state


def _load_attempt_state(run_id: str) -> dict[str, Any]:
    opened = _open_attempt_directory(run_id)
    assert opened is not None
    root_fd, directory_fd, _ = opened
    try:
        return _load_attempt_state_at(directory_fd, run_id)
    finally:
        os.close(directory_fd)
        os.close(root_fd)


def _remove_attempt_files(run_id: str) -> None:
    opened = _open_attempt_directory(run_id, missing_ok=True)
    if opened is None:
        return
    root_fd, directory_fd, directory_name = opened
    try:
        for name in (
            "credentials.json",
            "preferences.json",
            "project.json",
            "state.json",
        ):
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.fsync(directory_fd)
        try:
            os.rmdir(directory_name, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ProvisionerError("attempt directory contains unexpected files") from error
        os.fsync(root_fd)
    finally:
        os.close(directory_fd)
        os.close(root_fd)


def _local_api_configuration() -> tuple[str, str]:
    url = os.environ.get("STELE_BENCH_SUPABASE_URL") or os.environ.get("SUPABASE_TEST_URL")
    anon = os.environ.get("STELE_BENCH_SUPABASE_ANON_KEY") or os.environ.get(
        "SUPABASE_TEST_ANON_KEY"
    )
    if not url or not anon:
        try:
            result = subprocess.run(
                ["supabase", "status", "-o", "env"],
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            raise ProvisionerError("local Supabase configuration is unavailable") from error
        if result.returncode:
            raise ProvisionerError("local Supabase configuration is unavailable")
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value.strip().strip('"')
        url = url or values.get("API_URL")
        anon = anon or values.get("ANON_KEY") or values.get("PUBLISHABLE_KEY")
    if not url or not anon:
        raise ProvisionerError("local Supabase configuration is incomplete")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ProvisionerError("attempt authentication is restricted to local Supabase")
    return url.rstrip("/"), anon


def _auth_post(path: str, payload: dict[str, object]) -> dict[str, Any]:
    url, anon = _local_api_configuration()
    request = urllib.request.Request(
        f"{url}{path}",
        data=_canonical_bytes(payload),
        method="POST",
        headers={
            "apikey": anon,
            "Authorization": f"Bearer {anon}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read(PRIVATE_FILE_LIMIT + 1)
    except (urllib.error.URLError, TimeoutError) as error:
        raise ProvisionerError("local authentication request failed") from error
    if len(raw) > PRIVATE_FILE_LIMIT:
        raise ProvisionerError("local authentication response is too large")
    try:
        return _require_object(json.loads(raw), "local authentication response")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProvisionerError("local authentication response is invalid") from error


def _delete_fresh_user(user_id: str) -> None:
    sql = f"""
    begin;
    delete from auth.users where id='{user_id}'::uuid;
    select json_build_object('ok', true)::text;
    commit;
    """
    _psql(sql)


def _prepare(request: dict[str, Any]) -> dict[str, object]:
    _require_exact_keys(request, {"run_id", "harnesses"}, "prepare request")
    run_id = request["run_id"]
    harnesses = request["harnesses"]
    if not isinstance(run_id, str) or not run_id:
        raise ProvisionerError("run_id is required")
    if not isinstance(harnesses, list) or not all(
        isinstance(value, str) and PORTABLE_IDENTIFIER.fullmatch(value) for value in harnesses
    ):
        raise ProvisionerError("harnesses must be portable identifiers")
    directory = _attempt_directory(run_id, create_root=True)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError as error:
        raise ProvisionerError("attempt state already exists") from error
    user_id: str | None = None
    try:
        email = f"attempt-{secrets.token_hex(16)}@example.dev"
        password = f"{secrets.token_urlsafe(36)}A1!"
        nonce = secrets.token_urlsafe(36)
        run_hash = _attempt_key(run_id)
        nonce_hash = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        signup = _auth_post(
            "/auth/v1/signup",
            {
                "email": email,
                "password": password,
                "data": {
                    ATTEMPT_RECOVERY_MARKER: {
                        "run_id_sha256": run_hash,
                        "nonce_sha256": nonce_hash,
                    }
                },
            },
        )
        signup_user = _require_object(signup.get("user"), "signup user")
        user_id = signup_user.get("id")
        if not isinstance(user_id, str) or not PROJECT_ID.fullmatch(user_id):
            raise ProvisionerError("local signup returned an invalid user")
        _write_private_json(
            directory / "state.json",
            {
                "schema_version": ATTEMPT_STATE_VERSION,
                "run_id": run_id,
                "user_id": user_id,
                "nonce": nonce,
            },
        )
        session = _auth_post(
            "/auth/v1/token?grant_type=password", {"email": email, "password": password}
        )
        session_user = _require_object(session.get("user"), "session user")
        if session_user.get("id") != user_id:
            raise ProvisionerError("local session principal does not match signup")
        access_token = session.get("access_token")
        refresh_token = session.get("refresh_token")
        if not isinstance(access_token, str) or not isinstance(refresh_token, str):
            raise ProvisionerError("local signin did not return a complete session")
        expires_at = session.get("expires_at")
        if not isinstance(expires_at, int):
            expires_in = session.get("expires_in")
            if not isinstance(expires_in, int):
                raise ProvisionerError("local signin did not return session expiry")
            expires_at = int(time.time()) + expires_in

        marker_sql = f"""
        begin;
        update auth.users
        set raw_app_meta_data = coalesce(raw_app_meta_data, '{{}}'::jsonb) ||
          jsonb_build_object('local_graph_attempt_v1', jsonb_build_object(
            'run_id_sha256', '{run_hash}', 'nonce_sha256', '{nonce_hash}'
          ))
        where id='{user_id}'::uuid;
        select json_build_object('updated', count(*))::text
        from auth.users
        where id='{user_id}'::uuid
          and raw_app_meta_data #>> '{{local_graph_attempt_v1,run_id_sha256}}' = '{run_hash}'
          and raw_app_meta_data #>> '{{local_graph_attempt_v1,nonce_sha256}}' = '{nonce_hash}';
        commit;
        """
        marker_result = json.loads(_psql(marker_sql))
        if marker_result.get("updated") != 1:
            raise ProvisionerError("could not bind the prepared local principal")

        _write_private_json(
            directory / "credentials.json",
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": expires_at,
            },
        )
        _write_private_json(directory / "preferences.json", {"harnesses": harnesses})
        return {"ok": True, "attempt_key": _attempt_key(run_id)}
    except Exception:
        if user_id is not None:
            try:
                _delete_fresh_user(user_id)
            except Exception:
                pass
        for name in (
            "credentials.json",
            "preferences.json",
            "project.json",
            "state.json",
        ):
            try:
                (directory / name).unlink()
            except OSError:
                pass
        try:
            directory.rmdir()
        except OSError:
            pass
        raise


CLEANUP_ATTEMPT_SQL = r"""
begin;
create temporary table cleanup_result(projects bigint, users bigint) on commit drop;
do $cleanup$
declare
  owner_matched boolean := false;
  removed_projects bigint := 0;
  removed_users bigint := 0;
begin
  select true
  into owner_matched
  from auth.users owner
  where owner.id='__OWNER_ID__'::uuid
    and (
      (
        owner.raw_app_meta_data #>> '{local_graph_attempt_v1,run_id_sha256}' = '__RUN_SHA__'
        and owner.raw_app_meta_data #>> '{local_graph_attempt_v1,nonce_sha256}' = '__NONCE_SHA__'
      ) or (
        owner.raw_user_meta_data #>> '{local_graph_attempt_recovery_v1,run_id_sha256}' = '__RUN_SHA__'
        and owner.raw_user_meta_data #>> '{local_graph_attempt_recovery_v1,nonce_sha256}' = '__NONCE_SHA__'
        and owner.raw_app_meta_data #>> '{local_graph_attempt_v1,run_id_sha256}' is null
        and owner.raw_app_meta_data #>> '{local_graph_attempt_v1,nonce_sha256}' is null
      )
    );
  -- Every project this principal created, not just the one a restore marked.
  -- An arm that provisions in-container (`stele project create`) leaves a
  -- project the marker never names. Deleting the user below does currently
  -- remove it anyway, but only via a three-table cascade that has nothing to do
  -- with this cleanup: user → accounts.personal_user_id → projects.account_id →
  -- nodes.project_id. Every link there is about the ACCOUNT model, so a project
  -- that ever belongs to an account the attempt user does not personally own —
  -- a team account, a shared workspace — silently stops being cleaned up, and
  -- the failure is invisible until the local database has a pile of graphs in
  -- it. Deleting by authorship says what this actually means: the principal is
  -- an ephemeral `attempt-…@example.dev` user created seconds ago, so anything
  -- it made is benchmark state.
  if owner_matched then
    delete from projects where created_by_user_id='__OWNER_ID__'::uuid;
    get diagnostics removed_projects = row_count;
  end if;
  delete from auth.users owner
  where owner.id='__OWNER_ID__'::uuid
    and (
      (
        owner.raw_app_meta_data #>> '{local_graph_attempt_v1,run_id_sha256}' = '__RUN_SHA__'
        and owner.raw_app_meta_data #>> '{local_graph_attempt_v1,nonce_sha256}' = '__NONCE_SHA__'
      ) or (
        owner.raw_user_meta_data #>> '{local_graph_attempt_recovery_v1,run_id_sha256}' = '__RUN_SHA__'
        and owner.raw_user_meta_data #>> '{local_graph_attempt_recovery_v1,nonce_sha256}' = '__NONCE_SHA__'
        and owner.raw_app_meta_data #>> '{local_graph_attempt_v1,run_id_sha256}' is null
        and owner.raw_app_meta_data #>> '{local_graph_attempt_v1,nonce_sha256}' is null
      )
    );
  get diagnostics removed_users = row_count;
  insert into cleanup_result values (removed_projects, removed_users);
end
$cleanup$;
select json_build_object('projects', projects, 'users', users)::text from cleanup_result;
commit;
"""


CLEANUP_RECOVERY_SQL = r"""
begin;
create temporary table recovery_candidates(user_id uuid, project_id uuid) on commit drop;
insert into recovery_candidates(user_id, project_id)
select
  owner.id,
  nullif(owner.raw_app_meta_data #>> '{local_graph_provisioner_v1,project_id}', '')::uuid
from auth.users owner
where owner.email like 'attempt-%@example.dev'
  and owner.raw_user_meta_data #>> '{local_graph_attempt_recovery_v1,run_id_sha256}' = '__RUN_SHA__'
  and (
    (
      owner.raw_app_meta_data #>> '{local_graph_attempt_v1,run_id_sha256}' is null
      and owner.raw_app_meta_data #>> '{local_graph_attempt_v1,nonce_sha256}' is null
    ) or (
      owner.raw_app_meta_data #>> '{local_graph_attempt_v1,run_id_sha256}' = '__RUN_SHA__'
      and owner.raw_app_meta_data #>> '{local_graph_attempt_v1,nonce_sha256}' =
        owner.raw_user_meta_data #>> '{local_graph_attempt_recovery_v1,nonce_sha256}'
    )
  );
do $cleanup$
declare
  candidate_count bigint;
  candidate_user uuid;
begin
  select count(*) into candidate_count from recovery_candidates;
  if candidate_count > 1 then
    raise exception 'multiple recovery principals match this attempt';
  end if;
  if candidate_count = 1 then
    select user_id into candidate_user from recovery_candidates;
    -- Same reasoning as CLEANUP_ATTEMPT_SQL: delete by authorship, because an
    -- in-container `project create` is never marked and relying on the account
    -- cascade makes cleanup depend on the account model.
    delete from projects where created_by_user_id=candidate_user;
    delete from auth.users where id=candidate_user;
  end if;
end
$cleanup$;
select json_build_object('ok', true)::text;
commit;
"""


def _cleanup_by_recovery_marker(run_id: str) -> None:
    _psql(CLEANUP_RECOVERY_SQL.replace("__RUN_SHA__", _attempt_key(run_id)))


def _cleanup(request: dict[str, Any]) -> dict[str, bool]:
    _require_exact_keys(request, {"run_id"}, "cleanup request")
    run_id = request["run_id"]
    if not isinstance(run_id, str) or not run_id:
        raise ProvisionerError("run_id is required")
    opened = _open_attempt_directory(run_id, missing_ok=True)
    state: dict[str, Any] | None = None
    state_invalid = opened is None
    if opened is not None:
        root_fd, directory_fd, _ = opened
        try:
            try:
                state = _load_attempt_state_at(directory_fd, run_id)
            except ProvisionerError:
                state_invalid = True
        finally:
            os.close(directory_fd)
            os.close(root_fd)
    if state_invalid:
        _cleanup_by_recovery_marker(run_id)
    else:
        assert state is not None
        sql = (
            CLEANUP_ATTEMPT_SQL.replace("__OWNER_ID__", state["user_id"])
            .replace("__RUN_SHA__", _attempt_key(run_id))
            .replace(
                "__NONCE_SHA__",
                hashlib.sha256(state["nonce"].encode("utf-8")).hexdigest(),
            )
        )
        _psql(sql)
    _remove_attempt_files(run_id)
    return {"ok": True}


INSPECT_SQL = r"""
with owned as (
  select proj.id as project_id, proj.slug as slug
  from projects proj
  join auth.users owner on owner.id = proj.created_by_user_id
  where owner.id='__OWNER_ID__'::uuid
    and owner.raw_app_meta_data #>> '{local_graph_attempt_v1,run_id_sha256}' = '__RUN_SHA__'
    and owner.raw_app_meta_data #>> '{local_graph_attempt_v1,nonce_sha256}' = '__NONCE_SHA__'
)
select coalesce(
  json_agg(
    json_build_object(
      'project_id', o.project_id::text,
      'slug', o.slug,
      'nodes_total', (select count(*) from nodes n where n.project_id=o.project_id),
      'edges_total', (select count(*) from edges e where e.project_id=o.project_id),
      'agent_nodes_by_type', coalesce((
        select json_object_agg(grouped.type, grouped.total)
        from (
          select n.type as type, count(*) as total
          from nodes n
          where n.project_id=o.project_id
            and (
              n.created_by_source='agent'
              or (
                n.type='knowledge'
                and n.source_type='claude'
                and n.short_id <> 'KNOW-3'
              )
            )
          group by n.type
        ) grouped
      ), '{}'::json),
      'agent_nodes', coalesce((
        select json_agg(json_build_object(
          'short_id', n.short_id,
          'type', n.type,
          'status', n.status,
          'title', n.title,
          'body', n.body,
          'anchor_files', to_jsonb(n.anchor_files),
          'anchor_symbols', to_jsonb(n.anchor_symbols)
        ) order by substring(n.short_id from '[0-9]+$')::bigint)
        from nodes n
        where n.project_id=o.project_id
          and (
            n.created_by_source='agent'
            or (
              n.type='knowledge'
              and n.source_type='claude'
              and n.short_id <> 'KNOW-3'
            )
          )
      ), '[]'::json),
      'agent_tasks', json_build_object(
        'total', (
          select count(*) from nodes n
          where n.project_id=o.project_id and n.created_by_source='agent' and n.type='task'
        ),
        'claimed', (
          select count(*) from nodes n
          where n.project_id=o.project_id and n.created_by_source='agent'
            and n.type='task' and n.claimed_at is not null
        ),
        'completed', (
          select count(*) from nodes n
          where n.project_id=o.project_id and n.created_by_source='agent'
            and n.type='task' and n.status='done'
        )
      ),
      'seeded_tasks_touched', (
        select count(*) from nodes n
        where n.project_id=o.project_id and n.created_by_source='human'
          and n.type='task' and n.status <> 'open'
      )
    )
    order by o.slug
  ),
  '[]'::json
)::text
from owned o;
"""


def _inspect(request: dict[str, Any]) -> dict[str, object]:
    """Report what an attempt's principal actually left behind in the graph.

    The counterpart to `snapshot`, for the arm that has no fixture to compare
    against: a project made in-container by `stele project create` has no
    declared expected state, so the question is not "does it match" but "what
    did the agent write". A trajectory shows tool calls that were *attempted*;
    this is what survived to the database, which is the honest answer to whether
    the overhead bought anything.

    A new project seeds three nodes as `human` (the root component, the welcome
    task, and the welcome decision), so counting totals would report uptake for
    an agent that wrote nothing. Task/component writes through the MCP surface
    use `created_by_source='agent'`. Knowledge writes currently retain the SDK
    compatibility value `created_by_source='human'`, so they are recognized by
    `type='knowledge' AND source_type='claude'`; the known starter decision
    `KNOW-3` is excluded from that fallback.

    `seeded_tasks_touched` is deliberately reported: the seeded task asks the
    reader to go and fill the graph, and an agent that does that INSTEAD of the
    borrowed exercise is the pre-registered failure mode. It has to be visible
    in the record, not inferred from a bad score.

    Takes only `run_id`. The project id is not accepted from the caller on
    purpose — it would come from a file the container wrote, and the point of a
    host-side read is to be independent of what the container claims. Every
    project this principal owns is returned, so an agent that made several is
    visible rather than silently reduced to the one that was expected.
    """

    _require_exact_keys(request, {"run_id"}, "inspect request")
    run_id = request["run_id"]
    if not isinstance(run_id, str) or not run_id:
        raise ProvisionerError("run_id is required")
    attempt = _load_attempt_state(run_id)
    sql = (
        INSPECT_SQL.replace("__OWNER_ID__", attempt["user_id"])
        .replace("__RUN_SHA__", _attempt_key(run_id))
        .replace(
            "__NONCE_SHA__",
            hashlib.sha256(attempt["nonce"].encode("utf-8")).hexdigest(),
        )
    )
    projects = json.loads(_psql(sql))
    if not isinstance(projects, list):
        raise ProvisionerError("inspect returned an unexpected response")
    return {"ok": True, "projects": projects}


def _load_graph(request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    _require_exact_keys(
        request, {"fixture", "graph_arm", "run_identity", "artifacts"}, "restore request"
    )
    fixture = _require_object(request["fixture"], "fixture")
    identity = _require_object(request["run_identity"], "run_identity")
    artifacts = _require_object(request["artifacts"], "artifacts")
    arm = request["graph_arm"]
    if arm not in {"faithful", "know-it-all", "antagonist"}:
        raise ProvisionerError("unsupported graph arm")
    if not isinstance(fixture.get("fixture_id"), str) or not fixture["fixture_id"]:
        raise ProvisionerError("fixture_id is required")
    if not isinstance(identity.get("run_id"), str) or not identity["run_id"]:
        raise ProvisionerError("run_id is required")
    graph_path = artifacts.get("graph")
    if not isinstance(graph_path, str):
        raise ProvisionerError("resolved graph artifact is required")
    path = Path(graph_path)
    if not path.is_file():
        raise ProvisionerError("resolved graph artifact does not exist")
    descriptor = _require_object(_require_object(fixture.get("graphs"), "graphs").get(arm), "graph")
    expected = {
        "graph_format_version": GRAPH_FORMAT_VERSION,
        "backend_compatibility_id": BACKEND_COMPATIBILITY_ID,
        "embedding_model_id": EMBEDDING_MODEL_ID,
        "embedding_dimensions": EMBEDDING_DIMENSIONS,
        "media_type": MEDIA_TYPE,
    }
    for field, value in expected.items():
        if descriptor.get(field) != value:
            raise ProvisionerError(f"unsupported graph descriptor field: {field}")
    raw = _read_bounded_artifact(path)
    if hashlib.sha256(raw).hexdigest() != descriptor.get("sha256"):
        raise ProvisionerError("graph artifact digest does not match its descriptor")
    try:
        graph = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProvisionerError("graph artifact is not valid JSON") from error
    if _canonical_bytes(graph) != raw:
        raise ProvisionerError("graph artifact is not canonical compact JSON")
    _validate_graph(graph)
    return graph, descriptor, fixture["fixture_id"], identity["run_id"]


def _validate_graph(value: object) -> None:
    graph = _require_object(value, "graph")
    _require_exact_keys(
        graph,
        {"schema_version", "project", "workspaces", "branches", "nodes", "edges", "comments"},
        "graph",
    )
    if graph["schema_version"] != 1:
        raise ProvisionerError("unsupported graph schema version")
    project = _require_object(graph["project"], "project")
    _require_exact_keys(
        project,
        {
            "name",
            "description",
            "repo_url",
            "created_at",
            "settings",
            "default_branch",
            "visibility",
            "website",
        },
        "project",
    )
    if not isinstance(project["name"], str) or not project["name"].strip():
        raise ProvisionerError("project name is required")
    if not isinstance(project["settings"], dict):
        raise ProvisionerError("project settings must be an object")
    _require_timestamp(project["created_at"], "project.created_at")
    for name in ("workspaces", "branches", "nodes", "edges", "comments"):
        if not isinstance(graph[name], list):
            raise ProvisionerError(f"{name} must be an array")

    workspace_keys = {
        "name",
        "description",
        "kind",
        "auto_activate",
        "state",
        "created_at",
        "promoted_at",
        "archived_at",
    }
    workspace_names: list[str] = []
    for workspace_value in graph["workspaces"]:
        workspace = _require_object(workspace_value, "workspace")
        _require_exact_keys(workspace, workspace_keys, "workspace")
        name = workspace["name"]
        if not isinstance(name, str) or not PORTABLE_IDENTIFIER.fullmatch(name):
            raise ProvisionerError("workspace name must be a portable ASCII identifier")
        workspace_names.append(name)
        _require_timestamp(workspace["created_at"], "workspace.created_at")
        _require_timestamp(workspace["promoted_at"], "workspace.promoted_at", nullable=True)
        _require_timestamp(workspace["archived_at"], "workspace.archived_at", nullable=True)
    if workspace_names != sorted(set(workspace_names)):
        raise ProvisionerError("workspaces must be uniquely ordered by name")
    workspaces = set(workspace_names)

    branch_keys = {"workspace", "branch_name", "git_merge_base"}
    branch_order: list[tuple[str, str]] = []
    for branch_value in graph["branches"]:
        branch = _require_object(branch_value, "branch")
        _require_exact_keys(branch, branch_keys, "branch")
        if branch["workspace"] not in workspaces:
            raise ProvisionerError("branch references an unknown workspace")
        if not isinstance(branch["branch_name"], str) or not PORTABLE_IDENTIFIER.fullmatch(
            branch["branch_name"]
        ):
            raise ProvisionerError("branch name must be a portable ASCII identifier")
        branch_order.append((branch["workspace"], branch["branch_name"]))
    if branch_order != sorted(set(branch_order)):
        raise ProvisionerError("branches must be uniquely ordered by workspace and name")

    node_keys = {
        "short_id",
        "parent",
        "workspace",
        "type",
        "status",
        "title",
        "body",
        "category",
        "topics",
        "created_by_source",
        "created_at",
        "updated_at",
        "content_updated_at",
        "embedding_updated_at",
        "embedding",
        "completed_at",
        "priority",
        "kind",
        "source_type",
        "anchor_files",
        "anchor_symbols",
        "hidden",
    }
    short_ids: set[str] = set()
    parents_seen: set[str] = set()
    node_order: list[str] = []
    active_knowledge = 0
    components = 0
    for node_value in graph["nodes"]:
        node = _require_object(node_value, "node")
        _require_exact_keys(node, node_keys, "node")
        short_id = node.get("short_id")
        if not isinstance(short_id, str):
            raise ProvisionerError("node short ID is not canonical")
        _short_id_key(short_id)
        if short_id in short_ids:
            raise ProvisionerError("node short IDs must be unique")
        expected_prefix = NODE_TYPE_PREFIX.get(node.get("type"))
        if expected_prefix is None or not short_id.startswith(expected_prefix + "-"):
            raise ProvisionerError("node short ID prefix must match node type")
        # Checked explicitly rather than inferred from a downstream count. An
        # unrecognised status used to be caught only because it made the active
        # knowledge tally zero, which stopped being a check the moment a graph
        # was allowed to have no knowledge in it.
        if node.get("status") not in NODE_STATUSES:
            raise ProvisionerError("node status is not a recognised status")
        node_order.append(short_id)
        parent = node.get("parent")
        if parent is not None and parent not in parents_seen:
            raise ProvisionerError("parents must precede their children")
        if node.get("workspace") is not None and node.get("workspace") not in workspaces:
            raise ProvisionerError("node references an unknown workspace")
        _quantized_embedding(node.get("embedding"), f"embedding for {short_id}")
        for field in ("topics", "anchor_files", "anchor_symbols"):
            if not isinstance(node[field], list) or not all(
                isinstance(item, str) for item in node[field]
            ):
                raise ProvisionerError(f"node.{field} must be an array of strings")
        for field in (
            "created_at",
            "updated_at",
            "content_updated_at",
            "embedding_updated_at",
        ):
            _require_timestamp(node[field], f"node.{field}")
        _require_timestamp(node["completed_at"], "node.completed_at", nullable=True)
        if node["type"] == "knowledge" and node["status"] == "active":
            if (
                not isinstance(node["title"], str)
                or not re.search(r"[A-Za-z0-9]", node["title"])
            ):
                raise ProvisionerError("active knowledge needs a title for retrieval probes")
            active_knowledge += 1
        if node["type"] == "component":
            components += 1
        short_ids.add(short_id)
        parents_seen.add(short_id)
    if node_order != sorted(node_order, key=_short_id_key):
        raise ProvisionerError("nodes must use canonical natural short-ID ordering")
    if active_knowledge == 0 and components == 0:
        # A graph with neither knowledge nor structure is a failed build, not a
        # fixture. But a project with a component tree and no knowledge yet is a
        # legitimate one — it is exactly what a real project looks like on the
        # day Stele is installed, and it is the arm the overhead measurement
        # needs: Stele bound and running, with nothing to find. The retrieval
        # probes adapt to it rather than rejecting it (see `_snapshot`).
        raise ProvisionerError("graph needs knowledge or components to be restorable")

    edge_keys = {"src", "dst", "relation", "created_at", "reason", "evidence"}
    edge_order: list[tuple[str, str, str]] = []
    for edge_value in graph["edges"]:
        edge = _require_object(edge_value, "edge")
        _require_exact_keys(edge, edge_keys, "edge")
        if edge.get("src") not in short_ids or edge.get("dst") not in short_ids:
            raise ProvisionerError("edge references an unknown node")
        if not isinstance(edge["relation"], str) or not PORTABLE_IDENTIFIER.fullmatch(
            edge["relation"]
        ):
            raise ProvisionerError("edge relation must be a portable ASCII identifier")
        edge_order.append((edge["src"], edge["dst"], edge["relation"]))
        _require_timestamp(edge["created_at"], "edge.created_at")
    if not edge_order and active_knowledge:
        # Knowledge with no edges is a degenerate graph — a pile of notes, which
        # is the thing Stele exists not to be, and a fixture in that shape would
        # quietly test retrieval over an unlinked corpus. A graph with no
        # knowledge at all has nothing to link, so the requirement does not
        # apply to it.
        raise ProvisionerError("graph needs an edge for the graph probe")
    canonical_edge_order = sorted(
        set(edge_order), key=lambda edge: (_short_id_key(edge[0]), _short_id_key(edge[1]), edge[2])
    )
    if edge_order != canonical_edge_order:
        raise ProvisionerError("edges must be uniquely ordered by source, destination, relation")

    comment_keys_expected = {
        "key",
        "node",
        "parent",
        "body",
        "author_source",
        "created_at",
        "updated_at",
        "resolved_at",
        "anchor_node_version",
        "anchor_quote",
        "anchor_offset",
        "anchor_status",
    }
    comment_keys: set[str] = set()
    comment_timestamps: list[str] = []
    for index, comment_value in enumerate(graph["comments"], 1):
        comment = _require_object(comment_value, "comment")
        _require_exact_keys(comment, comment_keys_expected, "comment")
        key = comment.get("key")
        if key != f"comment-{index}":
            raise ProvisionerError("comments must use canonical ordinal keys")
        if comment.get("node") not in short_ids:
            raise ProvisionerError("comment references an unknown node")
        parent = comment.get("parent")
        if parent is not None and parent not in comment_keys:
            raise ProvisionerError("parent comments must precede replies")
        _require_timestamp(comment["created_at"], "comment.created_at")
        _require_timestamp(comment["updated_at"], "comment.updated_at")
        _require_timestamp(comment["resolved_at"], "comment.resolved_at", nullable=True)
        comment_timestamps.append(comment["created_at"])
        comment_keys.add(key)
    if comment_timestamps != sorted(set(comment_timestamps)):
        raise ProvisionerError("comments need unique ascending created_at values")


RESTORE_SQL = r"""
begin;
create temporary table provision_result(project_id uuid, queue_before bigint) on commit drop;
do $restore$
declare
  graph jsonb := convert_from(decode('__GRAPH__', 'base64'), 'utf8')::jsonb;
  owner_id uuid := '__OWNER_ID__'::uuid;
  destination_id uuid := '__PROJECT_ID__'::uuid;
  workspace_id uuid;
  node_id uuid;
  comment_id uuid;
  item jsonb;
  queue_before bigint;
  marker_count bigint;
  slug text := '__SLUG__';
begin
  create temporary table workspace_map(name text primary key, id uuid not null) on commit drop;
  create temporary table node_map(short_id text primary key, id uuid not null) on commit drop;
  create temporary table comment_map(key text primary key, id uuid not null) on commit drop;

  select count(*) into queue_before from pgmq.q_embed_jobs;
  update auth.users owner
  set raw_app_meta_data = coalesce(owner.raw_app_meta_data, '{}'::jsonb) ||
    jsonb_build_object('local_graph_provisioner_v1', jsonb_build_object(
      'project_id', destination_id::text, 'owner_id', owner_id::text,
      'identity_sha256', '__IDENTITY_SHA__'
    ))
  where owner.id=owner_id
    and owner.raw_app_meta_data #>> '{local_graph_attempt_v1,run_id_sha256}' = '__RUN_SHA__'
    and owner.raw_app_meta_data #>> '{local_graph_attempt_v1,nonce_sha256}' = '__NONCE_SHA__'
    and owner.raw_app_meta_data -> 'local_graph_provisioner_v1' is null;
  get diagnostics marker_count = row_count;
  if marker_count <> 1 then
    raise exception 'prepared local principal is unavailable or already bound';
  end if;
  insert into projects (
    id, slug, name, description, repo_url, created_by_user_id, created_at,
    settings, default_branch, visibility, website
  ) values (
    destination_id, slug, graph #>> '{project,name}', graph #>> '{project,description}',
    graph #>> '{project,repo_url}', owner_id, (graph #>> '{project,created_at}')::timestamptz,
    graph #> '{project,settings}', graph #>> '{project,default_branch}',
    graph #>> '{project,visibility}', graph #>> '{project,website}'
  );
  insert into project_members(project_id, user_id, role, added_at)
  values (destination_id, owner_id, 'owner', (graph #>> '{project,created_at}')::timestamptz);

  for item in select value from jsonb_array_elements(graph->'workspaces') loop
    workspace_id := gen_random_uuid();
    insert into workspace_map values (item->>'name', workspace_id);
    insert into workspaces (
      id, project_id, name, description, kind, auto_activate, state,
      owner_user_id, created_at, promoted_at, archived_at
    ) values (
      workspace_id, destination_id, item->>'name', item->>'description', item->>'kind',
      item->>'auto_activate', item->>'state', owner_id, (item->>'created_at')::timestamptz,
      (item->>'promoted_at')::timestamptz, (item->>'archived_at')::timestamptz
    );
  end loop;
  for item in select value from jsonb_array_elements(graph->'branches') loop
    insert into workspace_branches(workspace_id, branch_name, git_merge_base)
    values (
      (select id from workspace_map where name=item->>'workspace'),
      item->>'branch_name', item->>'git_merge_base'
    );
  end loop;

  execute 'alter table public.nodes disable trigger trg_nodes_enqueue_embed';
  for item in select value from jsonb_array_elements(graph->'nodes') loop
    node_id := gen_random_uuid();
    insert into node_map values (item->>'short_id', node_id);
    insert into nodes (
      id, project_id, type, parent_component_id, title, body, status, category,
      completed_at, topics, created_by_user_id, created_by_source, created_at, updated_at,
      short_id, source_type, content_updated_at, workspace_id, embedding,
      embedding_updated_at, priority, kind, anchor_files, anchor_symbols, hidden
    ) values (
      node_id, destination_id, item->>'type',
      (select id from node_map where short_id=item->>'parent'), item->>'title', item->>'body',
      item->>'status', item->>'category', (item->>'completed_at')::timestamptz,
      array(select jsonb_array_elements_text(item->'topics')), owner_id,
      item->>'created_by_source', (item->>'created_at')::timestamptz,
      (item->>'updated_at')::timestamptz, item->>'short_id', item->>'source_type',
      (item->>'content_updated_at')::timestamptz,
      (select id from workspace_map where name=item->>'workspace'),
      (item->'embedding')::text::halfvec(1536),
      (item->>'embedding_updated_at')::timestamptz, item->>'priority', item->>'kind',
      array(select jsonb_array_elements_text(item->'anchor_files')),
      array(select jsonb_array_elements_text(item->'anchor_symbols')),
      (item->>'hidden')::boolean
    );
  end loop;
  execute 'alter table public.nodes enable trigger trg_nodes_enqueue_embed';
  insert into node_counters(project_id, next_id)
  select destination_id, max((regexp_match(short_id, '[0-9]+$'))[1]::bigint) from node_map;

  for item in select value from jsonb_array_elements(graph->'edges') loop
    insert into edges (
      project_id, src_node_id, dst_node_id, relation, created_by_user_id,
      created_at, reason, evidence
    ) values (
      destination_id, (select id from node_map where short_id=item->>'src'),
      (select id from node_map where short_id=item->>'dst'), item->>'relation', owner_id,
      (item->>'created_at')::timestamptz, item->>'reason', item->>'evidence'
    );
  end loop;
  for item in select value from jsonb_array_elements(graph->'comments') loop
    comment_id := gen_random_uuid();
    insert into comment_map values (item->>'key', comment_id);
    insert into comments (
      id, project_id, node_id, body, author_user_id, author_source, created_at,
      parent_comment_id, updated_at, resolved_at, resolved_by_user_id,
      anchor_node_version, anchor_quote, anchor_offset, anchor_status
    ) values (
      comment_id, destination_id, (select id from node_map where short_id=item->>'node'),
      item->>'body', owner_id, item->>'author_source', (item->>'created_at')::timestamptz,
      (select id from comment_map where key=item->>'parent'), (item->>'updated_at')::timestamptz,
      (item->>'resolved_at')::timestamptz,
      case when item->>'resolved_at' is null then null else owner_id end,
      (item->>'anchor_node_version')::int, item->>'anchor_quote',
      (item->>'anchor_offset')::int, item->>'anchor_status'
    );
  end loop;
  insert into provision_result values (destination_id, queue_before);
end
$restore$;
select json_build_object(
  'project_id', provision_result.project_id::text,
  'queue_before_count', provision_result.queue_before,
  'active_project', json_build_object(
    'projectId', projects.id::text,
    'slug', projects.slug,
    'name', projects.name,
    'defaultBranch', projects.default_branch,
    'activeWorkspaceIds', '[]'::json
  )
)::text
from provision_result
join projects on projects.id=provision_result.project_id;
commit;
"""


def _validate_active_project(
    value: object,
    *,
    project_id: str,
    slug: str,
    name: str,
    default_branch: str,
) -> dict[str, Any]:
    active_project = _require_object(value, "active project")
    _require_exact_keys(
        active_project,
        {"projectId", "slug", "name", "defaultBranch", "activeWorkspaceIds"},
        "active project",
    )
    expected = {
        "projectId": project_id,
        "slug": slug,
        "name": name,
        "defaultBranch": default_branch,
        "activeWorkspaceIds": [],
    }
    if active_project != expected:
        raise ProvisionerError("database returned unexpected active project state")
    return active_project


def _restore(request: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    graph, _descriptor, fixture_id, run_id = _load_graph(request)
    attempt = _load_attempt_state(run_id)
    arm = request["graph_arm"]
    slug_stem = re.sub(r"[^a-z0-9]+", "-", graph["project"]["name"].lower()).strip("-")
    slug = f"{(slug_stem or 'state-project')[:40]}-{uuid.uuid4().hex[:12]}"
    encoded = base64.b64encode(_canonical_bytes(graph)).decode("ascii")
    project = {
        "project_id": str(uuid.uuid4()),
        "fixture_id": fixture_id,
        "run_id": run_id,
        "graph_arm": arm,
    }
    sql = (
        RESTORE_SQL.replace("__GRAPH__", encoded)
        .replace("__SLUG__", slug)
        .replace("__PROJECT_ID__", project["project_id"])
        .replace("__IDENTITY_SHA__", _identity_digest(project))
        .replace("__OWNER_ID__", attempt["user_id"])
        .replace("__RUN_SHA__", _attempt_key(run_id))
        .replace(
            "__NONCE_SHA__",
            hashlib.sha256(attempt["nonce"].encode("utf-8")).hexdigest(),
        )
    )
    result = json.loads(_psql(sql))
    if result["project_id"] != project["project_id"]:
        raise ProvisionerError("database returned an unexpected project identity")
    active_project = _validate_active_project(
        result.get("active_project"),
        project_id=project["project_id"],
        slug=slug,
        name=graph["project"]["name"],
        default_branch=graph["project"]["default_branch"],
    )
    _write_private_json(_attempt_directory(run_id) / "project.json", active_project)
    return {
        "project": project,
        "queue_before_count": result["queue_before_count"],
        "restore_seconds": time.monotonic() - started,
    }


def _project_request(request: dict[str, Any], operation: str) -> dict[str, Any]:
    _require_exact_keys(request, {"project"}, f"{operation} request")
    project = _require_object(request["project"], "project")
    _require_exact_keys(
        project, {"project_id", "fixture_id", "run_id", "graph_arm"}, "project"
    )
    project_id = project.get("project_id")
    if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
        raise ProvisionerError("project_id is not a UUID")
    if project.get("graph_arm") not in {"faithful", "know-it-all", "antagonist"}:
        raise ProvisionerError("unsupported graph arm")
    for field in ("fixture_id", "run_id"):
        if not isinstance(project.get(field), str) or not project[field]:
            raise ProvisionerError(f"{field} is required")
    return project


SNAPSHOT_SQL = r"""
with selected_project as (
  select p.* from projects p
  join auth.users owner on owner.id=p.created_by_user_id
  where p.id='__PROJECT_ID__'::uuid
    and owner.id='__OWNER_ID__'::uuid
    and owner.raw_app_meta_data #>> '{local_graph_provisioner_v1,project_id}' = p.id::text
    and owner.raw_app_meta_data #>> '{local_graph_provisioner_v1,owner_id}' = owner.id::text
    and owner.raw_app_meta_data #>> '{local_graph_provisioner_v1,identity_sha256}' = '__IDENTITY_SHA__'
    and owner.raw_app_meta_data #>> '{local_graph_attempt_v1,run_id_sha256}' = '__RUN_SHA__'
    and owner.raw_app_meta_data #>> '{local_graph_attempt_v1,nonce_sha256}' = '__NONCE_SHA__'
), probe_knowledge as (
  select n.id, n.project_id,
         regexp_replace(n.title, '[^[:alnum:] ]+', ' ', 'g') as fts_query,
         n.embedding, n.workspace_id
  from nodes n join selected_project p on p.id=n.project_id
  where n.type='knowledge' and n.status='active' and n.embedding is not null
  order by substring(n.short_id from '[0-9]+$')::bigint limit 1
), probe_graph_seed as (
  select e.src_node_id as id
  from edges e
  join selected_project p on p.id=e.project_id
  join nodes src on src.id=e.src_node_id
  join nodes dst on dst.id=e.dst_node_id
  order by
    case src.type when 'component' then 0 when 'knowledge' then 1 when 'task' then 2 when 'document' then 3 else 4 end,
    substring(src.short_id from '[0-9]+$')::bigint,
    case dst.type when 'component' then 0 when 'knowledge' then 1 when 'task' then 2 when 'document' then 3 else 4 end,
    substring(dst.short_id from '[0-9]+$')::bigint,
    e.relation collate "C"
  limit 1
), ordered_comments as (
  select c.*, row_number() over (order by c.created_at, c.id) as ordinal
  from comments c join selected_project p on p.id=c.project_id
), canonical as (
  select jsonb_build_object(
    'schema_version', 1,
    'project', jsonb_build_object(
      'name', p.name, 'description', p.description, 'repo_url', p.repo_url,
      'created_at', to_char(p.created_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
      'settings', p.settings, 'default_branch', p.default_branch,
      'visibility', p.visibility, 'website', p.website
    ),
    'workspaces', (select coalesce(jsonb_agg(jsonb_build_object(
      'name', w.name, 'description', w.description, 'kind', w.kind,
      'auto_activate', w.auto_activate, 'state', w.state,
      'created_at', to_char(w.created_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
      'promoted_at', case when w.promoted_at is null then null else to_char(w.promoted_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') end,
      'archived_at', case when w.archived_at is null then null else to_char(w.archived_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') end
    ) order by w.name collate "C"), '[]'::jsonb) from workspaces w where w.project_id=p.id),
    'branches', (select coalesce(jsonb_agg(jsonb_build_object(
      'workspace', w.name, 'branch_name', b.branch_name, 'git_merge_base', b.git_merge_base
    ) order by w.name collate "C", b.branch_name collate "C"), '[]'::jsonb) from workspace_branches b
      join workspaces w on w.id=b.workspace_id where w.project_id=p.id),
    'nodes', (select coalesce(jsonb_agg(jsonb_build_object(
      'short_id', n.short_id, 'parent', parent.short_id, 'workspace', w.name,
      'type', n.type, 'status', n.status, 'title', n.title, 'body', n.body,
      'category', n.category, 'topics', to_jsonb(n.topics),
      'created_by_source', n.created_by_source,
      'created_at', to_char(n.created_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
      'updated_at', to_char(n.updated_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
      'content_updated_at', to_char(n.content_updated_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
      'embedding_updated_at', to_char(n.embedding_updated_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
      'embedding', n.embedding::text,
      'completed_at', case when n.completed_at is null then null else to_char(n.completed_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') end,
      'priority', n.priority, 'kind', n.kind, 'source_type', n.source_type,
      'anchor_files', to_jsonb(n.anchor_files), 'anchor_symbols', to_jsonb(n.anchor_symbols),
      'hidden', n.hidden
    ) order by
      case n.type when 'component' then 0 when 'knowledge' then 1 when 'task' then 2 when 'document' then 3 else 4 end,
      substring(n.short_id from '[0-9]+$')::bigint
    ), '[]'::jsonb) from nodes n
      left join nodes parent on parent.id=n.parent_component_id
      left join workspaces w on w.id=n.workspace_id where n.project_id=p.id),
    'edges', (select coalesce(jsonb_agg(jsonb_build_object(
      'src', src.short_id, 'dst', dst.short_id, 'relation', e.relation,
      'created_at', to_char(e.created_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
      'reason', e.reason, 'evidence', e.evidence
    ) order by
      case src.type when 'component' then 0 when 'knowledge' then 1 when 'task' then 2 when 'document' then 3 else 4 end,
      substring(src.short_id from '[0-9]+$')::bigint,
      case dst.type when 'component' then 0 when 'knowledge' then 1 when 'task' then 2 when 'document' then 3 else 4 end,
      substring(dst.short_id from '[0-9]+$')::bigint,
      e.relation collate "C"
    ), '[]'::jsonb) from edges e
      join nodes src on src.id=e.src_node_id join nodes dst on dst.id=e.dst_node_id
      where e.project_id=p.id),
    'comments', (select coalesce(jsonb_agg(jsonb_build_object(
      'key', 'comment-' || c.ordinal::text, 'node', n.short_id,
      'parent', case when parent.ordinal is null then null else 'comment-' || parent.ordinal::text end,
      'body', c.body, 'author_source', c.author_source,
      'created_at', to_char(c.created_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
      'updated_at', to_char(c.updated_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
      'resolved_at', case when c.resolved_at is null then null else to_char(c.resolved_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') end,
      'anchor_node_version', c.anchor_node_version, 'anchor_quote', c.anchor_quote,
      'anchor_offset', c.anchor_offset, 'anchor_status', c.anchor_status
    ) order by c.ordinal), '[]'::jsonb) from ordered_comments c
      join nodes n on n.id=c.node_id left join ordered_comments parent on parent.id=c.parent_comment_id)
  ) as value
  from selected_project p
)
select json_build_object(
  'canonical', (select value from canonical),
  'node_count', (select count(*) from nodes n join selected_project p on p.id=n.project_id),
  'edge_count', (select count(*) from edges e join selected_project p on p.id=e.project_id),
  'queue_count', (select count(*) from pgmq.q_embed_jobs),
  'fts', (select coalesce(json_agg(json_build_object('id', n.short_id, 'score', round(s.score::numeric, 6)) order by s.score desc,
            case n.type when 'component' then 0 when 'knowledge' then 1 when 'task' then 2 when 'document' then 3 else 4 end,
            substring(n.short_id from '[0-9]+$')::bigint), '[]'::json)
          from probe_knowledge seed
          cross join lateral stele_search_nodes(
            seed.project_id, seed.fts_query, 20, null, 999999,
            case when seed.workspace_id is null then '{}'::uuid[] else array[seed.workspace_id] end
          ) s
          join nodes n on n.id=s.id),
  'semantic', (select coalesce(json_agg(json_build_object('id', n.short_id, 'score', round(s.score::numeric, 6)) order by s.score desc,
            case n.type when 'component' then 0 when 'knowledge' then 1 when 'task' then 2 when 'document' then 3 else 4 end,
            substring(n.short_id from '[0-9]+$')::bigint), '[]'::json)
          from probe_knowledge seed
          cross join lateral stele_semantic_seeds(
            seed.project_id, seed.embedding,
            case when seed.workspace_id is null then '{}'::uuid[] else array[seed.workspace_id] end,
            20, true, true
          ) s
          join nodes n on n.id=s.id),
  'graph', (with recursive walk(id, depth) as (
      select id, 0 from probe_graph_seed
      union
      select e.dst_node_id, walk.depth+1 from walk join edges e on e.src_node_id=walk.id where walk.depth < 8
    ) select coalesce(json_agg(n.short_id order by walk.depth,
        case n.type when 'component' then 0 when 'knowledge' then 1 when 'task' then 2 when 'document' then 3 else 4 end,
        substring(n.short_id from '[0-9]+$')::bigint), '[]'::json)
      from walk join nodes n on n.id=walk.id)
)::text;
"""


def _embedding_vector_from_text(text: object) -> list[float]:
    if not isinstance(text, str) or not text.startswith("[") or not text.endswith("]"):
        raise ProvisionerError("snapshot contains an invalid embedding")
    try:
        values = [float(value) for value in text[1:-1].split(",")]
    except ValueError as error:
        raise ProvisionerError("snapshot contains an invalid embedding") from error
    # pgvector renders halfvec values as short decimal strings. Those strings
    # need not equal the exact Python float for the underlying binary16 value,
    # so normalize them back to binary16 before canonical hashing.
    return _quantized_embedding(
        values, "snapshot embedding", require_quantized=False
    )


def _snapshot(request: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    project = _project_request(request, "snapshot")
    attempt = _load_attempt_state(project["run_id"])
    sql = (
        SNAPSHOT_SQL.replace("__PROJECT_ID__", project["project_id"])
        .replace("__IDENTITY_SHA__", _identity_digest(project))
        .replace("__OWNER_ID__", attempt["user_id"])
        .replace("__RUN_SHA__", _attempt_key(project["run_id"]))
        .replace(
            "__NONCE_SHA__",
            hashlib.sha256(attempt["nonce"].encode("utf-8")).hexdigest(),
        )
    )
    result = json.loads(_psql(sql))
    canonical = result.get("canonical")
    if canonical is None:
        raise ProvisionerError("project is not owned by the local graph provisioner")
    for node in canonical["nodes"]:
        node["embedding"] = _embedding_vector_from_text(node["embedding"])
    digest = hashlib.sha256(_canonical_bytes(canonical)).hexdigest()
    restored_knowledge = sum(
        1
        for node in canonical["nodes"]
        if node.get("type") == "knowledge" and node.get("status") == "active"
    )
    if restored_knowledge and any(
        not result[name] for name in ("fts", "semantic", "graph")
    ):
        raise ProvisionerError("snapshot retrieval proof returned no results")
    if not restored_knowledge and any(result[name] for name in ("fts", "semantic", "graph")):
        # The inverse assertion, and the one that matters for the overhead arm:
        # an "empty" graph that returns hits is not empty, and whatever it is
        # returning would be silently available to an arm whose entire premise
        # is that there is nothing to find.
        raise ProvisionerError("graph declares no knowledge but retrieval returned results")

    def scored_probe(name: str) -> dict[str, object]:
        rows = result[name]
        return {
            "name": name,
            "kind": name,
            "stable_ids": [row["id"] for row in rows],
            "scores": [float(row["score"]) for row in rows],
        }

    arm = project["graph_arm"]
    return {
        "state": {
            "fixture_id": project["fixture_id"],
            "project_id": project["project_id"],
            "graph_arm": arm,
            "canonical_state": {
                "path": f"graphs/{arm}.json",
                "sha256": digest,
                "media_type": MEDIA_TYPE,
                "graph_format_version": GRAPH_FORMAT_VERSION,
                "backend_compatibility_id": BACKEND_COMPATIBILITY_ID,
                "embedding_model_id": EMBEDDING_MODEL_ID,
                "embedding_dimensions": EMBEDDING_DIMENSIONS,
            },
            "declared_node_count": result["node_count"],
            "declared_edge_count": result["edge_count"],
        },
        "probes": [
            scored_probe("fts"),
            scored_probe("semantic"),
            {"name": "graph", "kind": "graph", "stable_ids": result["graph"]},
        ],
        "queue_after_count": result["queue_count"],
        "snapshot_seconds": time.monotonic() - started,
    }


DESTROY_SQL = r"""
begin;
create temporary table destroy_result(removed bigint) on commit drop;
do $destroy$
declare
  owner_id uuid;
  removed_count bigint;
begin
  select p.created_by_user_id into owner_id
  from projects p join auth.users owner on owner.id=p.created_by_user_id
  where p.id='__PROJECT_ID__'::uuid
    and owner.id='__OWNER_ID__'::uuid
    and owner.raw_app_meta_data #>> '{local_graph_provisioner_v1,project_id}' = p.id::text
    and owner.raw_app_meta_data #>> '{local_graph_provisioner_v1,owner_id}' = owner.id::text
    and owner.raw_app_meta_data #>> '{local_graph_provisioner_v1,identity_sha256}' = '__IDENTITY_SHA__'
    and owner.raw_app_meta_data #>> '{local_graph_attempt_v1,run_id_sha256}' = '__RUN_SHA__'
    and owner.raw_app_meta_data #>> '{local_graph_attempt_v1,nonce_sha256}' = '__NONCE_SHA__'
  for update of p;
  delete from projects
  where id='__PROJECT_ID__'::uuid and created_by_user_id=owner_id;
  get diagnostics removed_count = row_count;
  if removed_count = 1 then
    delete from auth.users where id=owner_id;
  end if;
  insert into destroy_result values (removed_count);
end
$destroy$;
select json_build_object('removed', removed)::text from destroy_result;
commit;
"""


def _destroy(request: dict[str, Any]) -> dict[str, bool]:
    project = _project_request(request, "destroy")
    attempt = _load_attempt_state(project["run_id"])
    sql = (
        DESTROY_SQL.replace("__PROJECT_ID__", project["project_id"])
        .replace("__IDENTITY_SHA__", _identity_digest(project))
        .replace("__OWNER_ID__", attempt["user_id"])
        .replace("__RUN_SHA__", _attempt_key(project["run_id"]))
        .replace(
            "__NONCE_SHA__",
            hashlib.sha256(attempt["nonce"].encode("utf-8")).hexdigest(),
        )
    )
    result = json.loads(_psql(sql))
    if result["removed"] != 1:
        raise ProvisionerError("project is not owned by the local graph provisioner")
    _remove_attempt_files(project["run_id"])
    return {"ok": True}


def main() -> int:
    operations = {
        "prepare": _prepare,
        "restore": _restore,
        "snapshot": _snapshot,
        "inspect": _inspect,
        "destroy": _destroy,
        "cleanup": _cleanup,
    }
    if len(sys.argv) != 2 or sys.argv[1] not in operations:
        print(
            "usage: provision.py prepare|restore|snapshot|inspect|destroy|cleanup",
            file=sys.stderr,
        )
        return 2
    operation = sys.argv[1]
    request: dict[str, Any] = {}
    try:
        request = _read_request()
        result = operations[operation](request)
    except ProvisionerError as error:
        if operation == "restore":
            identity = request.get("run_identity")
            if isinstance(identity, dict) and isinstance(identity.get("run_id"), str):
                try:
                    _cleanup({"run_id": identity["run_id"]})
                except ProvisionerError:
                    pass
        print(str(error)[:1000], file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
