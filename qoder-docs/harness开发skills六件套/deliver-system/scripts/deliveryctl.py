#!/usr/bin/env python3
"""Deterministic control plane for the deliver-system Qoder skill.

This module intentionally depends only on the Python standard library.  It owns
machine state under ``delivery-docs/state/`` and never overwrites project-owned
files during initialization or report generation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

# `inspect` is contractually zero-write.  The reconnaissance implementation is
# an adjacent module, so suppress Python's implicit __pycache__ write before it
# is imported.  Callers may still opt into a separate PYTHONPYCACHEPREFIX.
sys.dont_write_bytecode = True
import reconnaissance
import git_scope
import openspec_adapter
import planning
import planning_bindings


SCHEMA_VERSION = 1
HARNESS_VERSION = "0.1.0"
QUALITY_TRANSITIONS_IMPLEMENTED = False
IMPLEMENTED_MILESTONE = 3
DEFAULT_GATE_MAX_AGE_SECONDS = 86_400
GIT_OUTPUT_LIMIT_BYTES = 4 * 1024 * 1024
GIT_METADATA_ENTRY_LIMIT = 200_000
GIT_METADATA_FILE_LIMIT_BYTES = 4 * 1024 * 1024
DELIVERY_DIRNAME = "delivery-docs/state"
LEGACY_DELIVERY_DIRNAME = ".delivery"

ACTIVE_PHASES = {
    "INTAKE",
    "BASELINING",
    "CLARIFYING",
    "READY",
    "ARCHITECTING",
    "PLANNING",
    "EXECUTING",
    "SYSTEM_VERIFYING",
    "ACCEPTANCE_READY",
    "ACCEPTED",
    "RELEASE_READY",
    "RELEASED",
    "CLOSED",
}
INTERRUPT_PHASES = {"WAITING_USER", "BLOCKED", "PAUSED"}
EXCEPTION_PHASES = INTERRUPT_PHASES | {"REWORK", "ABORTED"}
ALL_PHASES = ACTIVE_PHASES | EXCEPTION_PHASES

# A phase name in the target architecture is not proof that its implementation
# exists.  This compiled ceiling is intentionally separate from project policy:
# a project file cannot unlock a later milestone.  Tests that exercise future
# gate semantics must explicitly patch this constant.
PHASE_MINIMUM_MILESTONE: dict[str, int] = {
    "INTAKE": 1,
    "BASELINING": 2,
    "CLARIFYING": 3,
    "READY": 3,
    "ARCHITECTING": 3,
    "PLANNING": 3,
    "EXECUTING": 4,
    "SYSTEM_VERIFYING": 5,
    "ACCEPTANCE_READY": 5,
    "ACCEPTED": 7,
    "RELEASE_READY": 7,
    "RELEASED": 7,
    "CLOSED": 1,
    "WAITING_USER": 1,
    "BLOCKED": 1,
    "PAUSED": 1,
    "REWORK": 4,
    "ABORTED": 1,
}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "INTAKE": {"BASELINING", "CLARIFYING"},
    "BASELINING": {"CLARIFYING", "READY"},
    "CLARIFYING": {"READY", "BASELINING"},
    "READY": {"ARCHITECTING", "PLANNING", "EXECUTING"},
    "ARCHITECTING": {"PLANNING", "CLARIFYING", "REWORK"},
    "PLANNING": {"EXECUTING", "CLARIFYING", "REWORK"},
    "EXECUTING": {"SYSTEM_VERIFYING", "REWORK", "PLANNING"},
    "SYSTEM_VERIFYING": {"ACCEPTANCE_READY", "REWORK", "EXECUTING"},
    "ACCEPTANCE_READY": {"ACCEPTED", "REWORK", "EXECUTING"},
    "ACCEPTED": {"RELEASE_READY", "REWORK", "CLOSED"},
    "RELEASE_READY": {"RELEASED", "REWORK"},
    "RELEASED": {"CLOSED", "REWORK"},
    "CLOSED": set(),
    "REWORK": {"CLARIFYING", "PLANNING", "EXECUTING", "SYSTEM_VERIFYING"},
    "ABORTED": {"CLOSED"},
}

# These states make a quality claim and therefore require evidence for the
# current commit.  CLOSED is intentionally excluded because an aborted project
# may be closed without ever becoming acceptance-ready.
QUALITY_GATED_PHASES = {
    "ACCEPTANCE_READY",
    "ACCEPTED",
    "RELEASE_READY",
    "RELEASED",
}
HUMAN_APPROVAL_PHASES = {"ACCEPTED", "RELEASED"}
STATE_EVENT_TYPES = {"delivery.initialized", "state.transitioned", "state.context_updated"}
REPORT_KINDS = {"checkpoint", "blocked", "change-complete", "milestone-complete", "acceptance"}
REPORT_KINDS.add("planning-complete")
REPORT_PHASES_REQUIRING_GATE: dict[str, set[str]] = {
    "change-complete": {
        "SYSTEM_VERIFYING",
        "ACCEPTANCE_READY",
        "ACCEPTED",
        "RELEASE_READY",
        "RELEASED",
    },
    "milestone-complete": {"ACCEPTANCE_READY", "ACCEPTED", "RELEASE_READY", "RELEASED"},
    "acceptance": {"ACCEPTANCE_READY", "ACCEPTED", "RELEASE_READY", "RELEASED"},
}
PLANNING_GATED_PHASES = {"READY", "ARCHITECTING", "PLANNING"}
PLANNING_STAGES = {"plan_ready", "stale"}
REPORT_PHASES_REQUIRING_PLANNING: dict[str, set[str]] = {
    "planning-complete": {"PLANNING"},
}

PROJECT_MODES = {"greenfield", "brownfield"}
GATE_STATUSES = {"not_run", "passed", "failed", "blocked"}
RESULT_STATUSES = {"passed", "failed", "blocked", "not_applicable"}
QUESTION_STATUSES = {"open", "answered", "deferred", "closed"}
ASSUMPTION_STATUSES = {"active", "confirmed", "rejected", "expired"}

TEMPLATES_ROOT = Path(__file__).resolve().parent.parent / "assets" / "templates"
UNSET = object()


class DeliveryError(RuntimeError):
    """Expected, user-actionable control-plane error."""


def phase_is_implemented(phase: str) -> bool:
    minimum = PHASE_MINIMUM_MILESTONE.get(phase)
    return minimum is not None and minimum <= IMPLEMENTED_MILESTONE


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def isoformat(value: dt.datetime | None = None) -> str:
    return (value or utc_now()).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_datetime(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str) or not value.strip():
        raise DeliveryError(f"{field} must be a non-empty ISO-8601 timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise DeliveryError(f"{field} is not a valid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise DeliveryError(f"{field} must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def canonical_root(raw: str | os.PathLike[str]) -> Path:
    lexical = Path(os.path.abspath(os.path.expanduser(os.fspath(raw))))
    try:
        metadata = lexical.lstat()
        canonical = lexical.resolve(strict=True)
    except FileNotFoundError as exc:
        raise DeliveryError(f"project root does not exist: {lexical}") from exc
    except OSError as exc:
        raise DeliveryError(f"project root cannot be inspected safely: {lexical}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise DeliveryError("project root must be an existing non-symbolic directory")
    if canonical != lexical:
        raise DeliveryError("project root path crosses a symbolic-link parent")
    return lexical


def delivery_path(root: Path, name: str) -> Path:
    return root / DELIVERY_DIRNAME / name


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise DeliveryError(f"required file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DeliveryError(
            f"invalid JSON in {path}: line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except OSError as exc:
        raise DeliveryError(f"cannot read {path}: {exc}") from exc


def read_json_no_duplicates(path: Path, *, label: str) -> Any:
    """Read strict JSON and reject duplicate object keys."""

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DeliveryError(f"{label} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=pairs_hook)
    except FileNotFoundError as exc:
        raise DeliveryError(f"required file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DeliveryError(
            f"invalid JSON in {path}: line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except OSError as exc:
        raise DeliveryError(f"cannot read {path}: {exc}") from exc


def json_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def canonical_json_bytes(data: Any) -> bytes:
    """Return the stable JSON representation used by durable state digests."""

    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(data: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(data)).hexdigest()


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def ensure_safe_control_path(root: Path, path: Path, *, label: str = "controlled path") -> Path:
    """Fail closed when a controlled path escapes *root* or crosses a symlink.

    The check deliberately rejects even symlinks that currently resolve inside
    the project.  This keeps later writes independent of mutable link targets.
    """

    root = root.resolve()
    lexical = Path(os.path.abspath(os.fspath(path)))
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise DeliveryError(f"{label} escapes project root: {path}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise DeliveryError(f"{label} crosses a symbolic link: {current}")
    try:
        lexical.parent.resolve(strict=False).relative_to(root)
        lexical.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise DeliveryError(f"{label} resolves outside project root: {path}") from exc
    return lexical


def _write_all(descriptor: int, data: bytes) -> None:
    """Write every byte, handling legal short ``os.write`` results."""

    view = memoryview(data)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise OSError("os.write made no progress")
        written += count


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory fsync after atomic replacement."""

    try:
        descriptor = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace *path* with UTF-8 text."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json_text(data))


def exclusive_write_text(path: Path, text: str) -> None:
    """Create *path* atomically and fail rather than overwrite it."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise DeliveryError(f"refusing to overwrite existing file: {path}") from exc
        except OSError:
            # Portable fallback.  It is still exclusive, although the target is
            # written directly if hard-link creation is unavailable.
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            try:
                target_descriptor = os.open(str(path), flags, 0o644)
            except FileExistsError as exc:
                raise DeliveryError(f"refusing to overwrite existing file: {path}") from exc
            with os.fdopen(target_descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def exclusive_write_json(path: Path, data: Any) -> None:
    exclusive_write_text(path, json_text(data))


def controlled_atomic_write_text(root: Path, path: Path, text: str) -> None:
    atomic_write_text(ensure_safe_control_path(root, path), text)


def controlled_atomic_write_json(root: Path, path: Path, data: Any) -> None:
    controlled_atomic_write_text(root, path, json_text(data))


def controlled_exclusive_write_text(root: Path, path: Path, text: str) -> None:
    exclusive_write_text(ensure_safe_control_path(root, path), text)


def controlled_exclusive_write_json(root: Path, path: Path, data: Any) -> None:
    controlled_exclusive_write_text(root, path, json_text(data))


def controlled_unlink(root: Path, path: Path, *, missing_ok: bool = False) -> None:
    safe = ensure_safe_control_path(root, path)
    try:
        safe.unlink()
        _fsync_directory(safe.parent)
    except FileNotFoundError:
        if not missing_ok:
            raise


class ProjectLock:
    """Small cross-platform lock protecting state and event-log writers."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.path = delivery_path(root, ".deliveryctl.lock")
        self.descriptor: int | None = None

    def __enter__(self) -> "ProjectLock":
        ensure_safe_control_path(self.root, self.path, label="delivery lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            self.descriptor = os.open(str(self.path), flags, 0o600)
        except FileExistsError as exc:
            if not self._remove_stale_lock():
                raise DeliveryError(f"another deliveryctl writer is active ({self.path})") from exc
            try:
                self.descriptor = os.open(str(self.path), flags, 0o600)
            except FileExistsError as retry_exc:
                raise DeliveryError(f"another deliveryctl writer is active ({self.path})") from retry_exc
        payload = f"pid={os.getpid()} created_at={isoformat()}\n".encode("utf-8")
        _write_all(self.descriptor, payload)
        os.fsync(self.descriptor)
        return self

    def _remove_stale_lock(self) -> bool:
        """Remove a lock only when its recorded process is definitely absent."""

        ensure_safe_control_path(self.root, self.path, label="delivery lock")
        try:
            payload = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        match = re.search(r"(?:^|\s)pid=(\d+)(?:\s|$)", payload)
        if not match:
            return False
        pid = int(match.group(1))
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            controlled_unlink(self.root, self.path)
            return True
        except PermissionError:
            return False
        except OSError as exc:
            # Windows raises OSError (ERROR_INVALID_PARAMETER) for a pid that
            # does not exist instead of ProcessLookupError.
            if os.name == "nt" and exc.winerror == 87:
                controlled_unlink(self.root, self.path)
                return True
            return False
        return False

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
        try:
            controlled_unlink(self.root, self.path)
        except FileNotFoundError:
            pass


def append_jsonl(path: Path, item: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(dict(item), ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    descriptor = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def controlled_append_jsonl(root: Path, path: Path, item: Mapping[str, Any]) -> None:
    append_jsonl(ensure_safe_control_path(root, path, label="event log"), item)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise DeliveryError(f"required file is missing: {path}") from exc
    except OSError as exc:
        raise DeliveryError(f"cannot read {path}: {exc}") from exc
    events: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DeliveryError(f"invalid JSONL in {path} at line {number}: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise DeliveryError(f"event at {path}:{number} must be a JSON object")
        events.append(value)
    return events


def load_json_template(relative: str, substitutions: Mapping[str, Any]) -> Any:
    template = read_json(TEMPLATES_ROOT / relative)

    def replace(value: Any) -> Any:
        if isinstance(value, str) and value in substitutions:
            return substitutions[value]
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        return value

    return replace(template)


def load_text_template(relative: str) -> str:
    path = TEMPLATES_ROOT / relative
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DeliveryError(f"skill template is missing: {path}") from exc
    except OSError as exc:
        raise DeliveryError(f"cannot read skill template {path}: {exc}") from exc


def render_text_template(template: str, substitutions: Mapping[str, Any]) -> str:
    rendered = template
    for key, value in substitutions.items():
        rendered = rendered.replace("{{" + key + "}}", str(value))
    unresolved = sorted(set(re.findall(r"\{\{([A-Z0-9_]+)\}\}", rendered)))
    if unresolved:
        raise DeliveryError(f"unresolved template placeholders: {', '.join(unresolved)}")
    return rendered


def ensure_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DeliveryError(f"{label} must be a JSON object")
    return value


def require_keys(value: Mapping[str, Any], keys: Iterable[str], label: str) -> None:
    missing = sorted(key for key in keys if key not in value)
    if missing:
        raise DeliveryError(f"{label} is missing required keys: {', '.join(missing)}")


def validate_versioned(value: Mapping[str, Any], label: str, *, harness: bool = False) -> None:
    if value.get("schema_version") != SCHEMA_VERSION:
        raise DeliveryError(
            f"{label}.schema_version must be {SCHEMA_VERSION}; found {value.get('schema_version')!r}"
        )
    if harness and value.get("harness_version") != HARNESS_VERSION:
        raise DeliveryError(
            f"{label}.harness_version must be {HARNESS_VERSION}; "
            f"found {value.get('harness_version')!r}; migration is required"
        )


def validate_manifest(value: Any) -> dict[str, Any]:
    manifest = ensure_object(value, "manifest")
    validate_versioned(manifest, "manifest", harness=True)
    require_keys(
        manifest,
        {"project_id", "project_mode", "created_at", "paths", "policies"},
        "manifest",
    )
    if not isinstance(manifest["project_id"], str) or not manifest["project_id"].strip():
        raise DeliveryError("manifest.project_id must be a non-empty string")
    if manifest["project_mode"] not in PROJECT_MODES:
        raise DeliveryError(f"manifest.project_mode must be one of {sorted(PROJECT_MODES)}")
    parse_datetime(manifest["created_at"], "manifest.created_at")
    paths = ensure_object(manifest["paths"], "manifest.paths")
    expected_paths = {
        "state": "delivery-docs/state/state.json",
        "events": "delivery-docs/state/events.jsonl",
        "questions": "delivery-docs/state/questions.json",
        "assumptions": "delivery-docs/state/assumptions.json",
        "gate_manifest": "delivery-docs/state/gate-manifest.json",
    }
    for key, expected in expected_paths.items():
        if paths.get(key) != expected:
            raise DeliveryError(f"manifest.paths.{key} must be {expected!r}")
    if "planning_manifest" in paths and paths["planning_manifest"] != "delivery-docs/state/planning-manifest.json":
        raise DeliveryError(
            "manifest.paths.planning_manifest must be 'delivery-docs/state/planning-manifest.json'"
        )
    if "work_items" in paths and paths["work_items"] != "delivery-docs/state/work-items":
        raise DeliveryError("manifest.paths.work_items must be 'delivery-docs/state/work-items'")
    planning_manifest_paths = {
        "requirements": planning.DEFAULT_AUTHORITY_PATHS["requirements"],
        "acceptance": planning.DEFAULT_AUTHORITY_PATHS["acceptance"],
        "feature_ledger": planning.DEFAULT_AUTHORITY_PATHS["features"],
        "roadmap": planning.DEFAULT_AUTHORITY_PATHS["roadmap"],
    }
    effective_planning_paths: dict[str, PurePosixPath] = {}
    for key, default_path in planning_manifest_paths.items():
        raw_path = paths.get(key, default_path)
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or "\\" in raw_path
            or Path(raw_path).is_absolute()
            or any(part in {"", ".", ".."} for part in Path(raw_path).parts)
            or Path(raw_path).as_posix() != raw_path
        ):
            raise DeliveryError(
                f"manifest.paths.{key} must be a canonical project-relative path"
            )
        parsed = PurePosixPath(raw_path)
        lowered_parts = tuple(part.casefold() for part in parsed.parts)
        if lowered_parts[0] in {".git", ".delivery", ".qoder", "openspec"} or lowered_parts[:2] == (
            "delivery-docs",
            "state",
        ):
            raise DeliveryError(
                f"manifest.paths.{key} cannot overlap Git, delivery, Qoder, or OpenSpec control data"
            )
        effective_planning_paths[key] = parsed
    if len(set(effective_planning_paths.values())) != len(effective_planning_paths):
        raise DeliveryError("manifest planning authority paths must be distinct")
    for left_key, left in effective_planning_paths.items():
        for right_key, right in effective_planning_paths.items():
            if left_key >= right_key:
                continue
            if left.parts == right.parts[: len(left.parts)] or right.parts == left.parts[: len(right.parts)]:
                raise DeliveryError(
                    f"manifest planning authority paths cannot be ancestors of one another: {left_key}, {right_key}"
                )
    policies = ensure_object(manifest["policies"], "manifest.policies")
    max_age = policies.get("gate_max_age_seconds")
    if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age <= 0:
        raise DeliveryError("manifest.policies.gate_max_age_seconds must be a positive integer")
    for key in (
        "require_clean_worktree",
        "require_gate_artifacts",
        "quality_transitions_enabled",
    ):
        if not isinstance(policies.get(key), bool):
            raise DeliveryError(f"manifest.policies.{key} must be boolean")
    if policies.get("quality_transitions_enabled") and not QUALITY_TRANSITIONS_IMPLEMENTED:
        raise DeliveryError(
            f"quality transitions are not implemented by harness {HARNESS_VERSION}; "
            "the manifest cannot unlock a later milestone"
        )
    return manifest


def validate_state(value: Any) -> dict[str, Any]:
    state = ensure_object(value, "state")
    validate_versioned(state, "state", harness=True)
    require_keys(
        state,
        {
            "revision",
            "project_mode",
            "phase",
            "source_digest",
            "baseline_commit",
            "active_milestone",
            "active_change",
            "blocking_questions",
            "pending_decisions",
            "last_gate_manifest",
            "last_verified_commit",
            "next_action",
            "resume_phase",
            "created_at",
            "updated_at",
        },
        "state",
    )
    if not isinstance(state["revision"], int) or isinstance(state["revision"], bool) or state["revision"] < 0:
        raise DeliveryError("state.revision must be a non-negative integer")
    if state["project_mode"] not in PROJECT_MODES:
        raise DeliveryError(f"state.project_mode must be one of {sorted(PROJECT_MODES)}")
    if state["phase"] not in ALL_PHASES:
        raise DeliveryError(f"state.phase is unknown: {state['phase']!r}")
    if state["resume_phase"] is not None and state["resume_phase"] not in ALL_PHASES - INTERRUPT_PHASES:
        raise DeliveryError("state.resume_phase must be null or a non-interrupt phase")
    for key in ("blocking_questions", "pending_decisions"):
        if not isinstance(state[key], list) or not all(isinstance(item, str) for item in state[key]):
            raise DeliveryError(f"state.{key} must be an array of string IDs")
    for key in (
        "source_digest",
        "baseline_commit",
        "active_milestone",
        "active_change",
        "last_gate_manifest",
        "last_verified_commit",
        "next_action",
    ):
        if state[key] is not None and not isinstance(state[key], str):
            raise DeliveryError(f"state.{key} must be a string or null")
        if isinstance(state[key], str) and not state[key].strip():
            raise DeliveryError(f"state.{key} must be non-empty when provided")
    if state["source_digest"] is not None and not is_sha256(state["source_digest"]):
        raise DeliveryError("state.source_digest must contain 64 lowercase hexadecimal characters")
    if state["baseline_commit"] is not None and not re.fullmatch(
        r"(?:[0-9a-f]{40}|[0-9a-f]{64})", state["baseline_commit"]
    ):
        raise DeliveryError("state.baseline_commit must be a full 40- or 64-character lowercase Git object ID")
    planning_stage = state.get("planning_stage")
    planning_digest = state.get("planning_manifest_digest")
    if planning_stage is not None and planning_stage not in PLANNING_STAGES:
        raise DeliveryError(
            f"state.planning_stage must be null or one of {sorted(PLANNING_STAGES)}"
        )
    if planning_digest is not None and not is_sha256(planning_digest):
        raise DeliveryError("state.planning_manifest_digest must be null or a lowercase SHA-256")
    if planning_stage == "plan_ready" and not is_sha256(planning_digest):
        raise DeliveryError("state planning_stage=plan_ready requires planning_manifest_digest")
    if planning_stage == "stale" and planning_digest is not None:
        raise DeliveryError("state planning_stage=stale requires a null planning_manifest_digest")
    if state["phase"] in PLANNING_GATED_PHASES and planning_stage != "plan_ready":
        raise DeliveryError(
            f"state phase {state['phase']} requires planning_stage=plan_ready"
        )
    parse_datetime(state["created_at"], "state.created_at")
    created = parse_datetime(state["created_at"], "state.created_at")
    updated = parse_datetime(state["updated_at"], "state.updated_at")
    if updated < created:
        raise DeliveryError("state.updated_at cannot be earlier than state.created_at")
    return state


def validate_item_catalog(
    value: Any,
    label: str,
    *,
    id_prefix: str,
    statuses: set[str],
) -> dict[str, Any]:
    catalog = ensure_object(value, label)
    validate_versioned(catalog, label, harness=True)
    require_keys(catalog, {"items"}, label)
    if not isinstance(catalog["items"], list):
        raise DeliveryError(f"{label}.items must be an array")
    seen: set[str] = set()
    for index, raw_item in enumerate(catalog["items"]):
        item = ensure_object(raw_item, f"{label}.items[{index}]")
        require_keys(item, {"id", "status", "summary", "created_at", "updated_at"}, f"{label}.items[{index}]")
        item_id = item["id"]
        if not isinstance(item_id, str) or not re.fullmatch(rf"{re.escape(id_prefix)}-[A-Z0-9][A-Z0-9_-]*", item_id):
            raise DeliveryError(f"{label}.items[{index}].id must start with {id_prefix}-")
        if item_id in seen:
            raise DeliveryError(f"duplicate ID in {label}: {item_id}")
        seen.add(item_id)
        if item["status"] not in statuses:
            raise DeliveryError(f"{label}.items[{index}].status must be one of {sorted(statuses)}")
        if not isinstance(item["summary"], str) or not item["summary"].strip():
            raise DeliveryError(f"{label}.items[{index}].summary must be non-empty")
        parse_datetime(item["created_at"], f"{label}.items[{index}].created_at")
        parse_datetime(item["updated_at"], f"{label}.items[{index}].updated_at")
    return catalog


def validate_events(value: Sequence[Mapping[str, Any]], state: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not value:
        raise DeliveryError("events.jsonl must contain the initialization event")
    validated: list[dict[str, Any]] = []
    prior_sequence = -1
    seen_ids: set[str] = set()
    latest_state_revision: int | None = None
    latest_state_snapshot: dict[str, Any] | None = None
    latest_phase: str | None = None
    for index, raw in enumerate(value):
        event = ensure_object(raw, f"events[{index}]")
        validate_versioned(event, f"events[{index}]", harness=True)
        require_keys(
            event,
            {"event_id", "sequence", "event_type", "timestamp", "actor", "state_revision"},
            f"events[{index}]",
        )
        if not isinstance(event["event_id"], str) or not event["event_id"]:
            raise DeliveryError(f"events[{index}].event_id must be non-empty")
        if event["event_id"] in seen_ids:
            raise DeliveryError(f"duplicate event_id: {event['event_id']}")
        seen_ids.add(event["event_id"])
        sequence = event["sequence"]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence != prior_sequence + 1:
            raise DeliveryError(f"events[{index}].sequence must be {prior_sequence + 1}")
        prior_sequence = sequence
        if not isinstance(event["event_type"], str) or not event["event_type"]:
            raise DeliveryError(f"events[{index}].event_type must be non-empty")
        parse_datetime(event["timestamp"], f"events[{index}].timestamp")
        revision = event["state_revision"]
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise DeliveryError(f"events[{index}].state_revision must be non-negative")
        if event["event_type"] in STATE_EVENT_TYPES:
            require_keys(
                event,
                {"state_digest", "state_snapshot", "details"},
                f"events[{index}]",
            )
            snapshot = ensure_object(event["state_snapshot"], f"events[{index}].state_snapshot")
            validate_state(snapshot)
            if not phase_is_implemented(snapshot["phase"]):
                minimum = PHASE_MINIMUM_MILESTONE[snapshot["phase"]]
                raise DeliveryError(
                    f"events[{index}] uses phase {snapshot['phase']} from unavailable milestone M{minimum}; "
                    f"this harness implements through M{IMPLEMENTED_MILESTONE}"
                )
            if snapshot.get("resume_phase") and not phase_is_implemented(snapshot["resume_phase"]):
                minimum = PHASE_MINIMUM_MILESTONE[snapshot["resume_phase"]]
                raise DeliveryError(
                    f"events[{index}] resumes to unavailable phase {snapshot['resume_phase']} from M{minimum}"
                )
            digest = event["state_digest"]
            if not is_sha256(digest):
                raise DeliveryError(f"events[{index}].state_digest must be a canonical SHA-256")
            actual_digest = canonical_sha256(snapshot)
            if digest != actual_digest:
                raise DeliveryError(
                    f"events[{index}].state_digest does not match its state snapshot"
                )
            if snapshot["revision"] != revision:
                raise DeliveryError(
                    f"events[{index}].state_revision does not match its state snapshot"
                )
            expected_revision = 0 if latest_state_revision is None else latest_state_revision + 1
            if revision != expected_revision:
                raise DeliveryError(
                    f"events[{index}] state revision must be {expected_revision}; found {revision}"
                )
            details = ensure_object(event["details"], f"events[{index}].details")
            require_keys(details, {"from", "to"}, f"events[{index}].details")
            expected_from = None if latest_state_snapshot is None else latest_state_snapshot["phase"]
            if details["from"] != expected_from:
                raise DeliveryError(
                    f"events[{index}].details.from must be {expected_from!r}"
                )
            if details["to"] != snapshot["phase"]:
                raise DeliveryError(
                    f"events[{index}].details.to does not match snapshot phase"
                )
            if latest_state_snapshot is None:
                if event["event_type"] != "delivery.initialized":
                    raise DeliveryError("the first state event must be delivery.initialized")
            elif event["event_type"] == "state.context_updated" and details["from"] != details["to"]:
                raise DeliveryError("state.context_updated must not change phase")
            elif event["event_type"] == "state.transitioned" and details["from"] == details["to"]:
                raise DeliveryError("state.transitioned must change phase")
            elif event["event_type"] == "state.transitioned" and snapshot["phase"] not in allowed_targets(
                latest_state_snapshot
            ):
                raise DeliveryError(
                    f"events[{index}] contains an illegal transition "
                    f"{latest_state_snapshot['phase']} -> {snapshot['phase']}"
                )
            latest_state_revision = revision
            latest_state_snapshot = snapshot
            latest_phase = snapshot["phase"]
        validated.append(event)
    if latest_state_revision != state["revision"]:
        raise DeliveryError(
            "state revision does not match the latest state event; state may have been edited outside deliveryctl "
            f"(state={state['revision']}, event={latest_state_revision})"
        )
    if latest_state_snapshot is None or canonical_sha256(latest_state_snapshot) != canonical_sha256(state):
        raise DeliveryError(
            "state digest does not match the latest state event; state may have been edited outside deliveryctl"
        )
    if latest_phase != state["phase"]:
        raise DeliveryError("state phase does not match the latest state event")
    return validated


def _read_git_metadata_file(path: Path, *, label: str) -> str:
    """Read one bounded regular metadata file without following its final link."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DeliveryError(f"{label} cannot be safely opened: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise DeliveryError(f"{label} is not a regular file")
        if metadata.st_size > GIT_METADATA_FILE_LIMIT_BYTES:
            raise DeliveryError(
                f"{label} exceeds the {GIT_METADATA_FILE_LIMIT_BYTES}-byte inspection limit"
            )
        chunks: list[bytes] = []
        remaining = GIT_METADATA_FILE_LIMIT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > GIT_METADATA_FILE_LIMIT_BYTES:
            raise DeliveryError(
                f"{label} exceeds the {GIT_METADATA_FILE_LIMIT_BYTES}-byte inspection limit"
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DeliveryError(f"{label} is not valid UTF-8") from exc
    finally:
        os.close(descriptor)


def _git_config_scope_risks(config_text: str) -> list[str]:
    """Identify local Git config directives that can redirect metadata scope."""

    risks: list[str] = []
    section = ""
    for raw_line in config_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        section_match = re.match(r"^\[\s*([^\]\s]+)(?:\s+[^\]]*)?\]\s*$", stripped)
        if section_match:
            section = section_match.group(1).lower()
            if section in {"include", "includeif"}:
                risks.append(f"local Git config uses [{section}] indirection")
            continue
        key_match = re.match(r"^([A-Za-z][A-Za-z0-9.-]*)\s*(?:=\s*)?(.*)$", stripped)
        if not key_match:
            continue
        key = key_match.group(1).lower()
        value = key_match.group(2).strip().split("#", 1)[0].split(";", 1)[0].strip().lower()
        if section == "core" and key == "worktree":
            risks.append("local Git config sets core.worktree")
        elif section == "core" and key == "bare":
            if value not in {"false", "no", "off", "0"}:
                risks.append("local Git config enables or ambiguously defines core.bare")
        elif section == "extensions" and key == "worktreeconfig":
            if value not in {"false", "no", "off", "0"}:
                risks.append("local Git config enables extensions.worktreeConfig")
    return risks


def _git_metadata_safety_risks(root: Path) -> list[str]:
    """Inspect local Git metadata without invoking Git or following links.

    M2 supports only a conventional ``<canonical-root>/.git`` directory. Any
    indirection, special node, replacement reference, or incomplete inspection
    blocks Git before repository-controlled configuration can be interpreted.
    """

    risks: list[str] = []
    try:
        canonical = root.resolve(strict=True)
    except OSError as exc:
        return [f"project root cannot be canonicalized ({exc})"]
    if canonical != root or not canonical.is_dir():
        return ["project root must be an existing canonical directory"]
    git_dir = root / ".git"
    try:
        marker = git_dir.lstat()
    except FileNotFoundError:
        return [".git metadata directory is missing"]
    except OSError as exc:
        return [f".git metadata cannot be inspected ({exc})"]
    if stat.S_ISLNK(marker.st_mode) or not stat.S_ISDIR(marker.st_mode):
        return [".git metadata is external, symbolic, or not a directory"]

    observed = 0
    pending = [git_dir]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            risks.append(
                f"{directory.relative_to(root).as_posix()}: metadata directory cannot be read ({exc})"
            )
            continue
        for entry in entries:
            observed += 1
            relative = Path(entry.path).relative_to(root).as_posix()
            if observed > GIT_METADATA_ENTRY_LIMIT:
                risks.append(
                    f"Git metadata exceeds the {GIT_METADATA_ENTRY_LIMIT}-entry inspection limit"
                )
                return risks
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                risks.append(f"{relative}: metadata entry cannot be inspected ({exc})")
                continue
            if stat.S_ISLNK(metadata.st_mode):
                risks.append(f"{relative}: symbolic link in Git metadata")
            elif stat.S_ISDIR(metadata.st_mode):
                pending.append(Path(entry.path))
            elif not stat.S_ISREG(metadata.st_mode):
                risks.append(f"{relative}: non-regular Git metadata entry")

    # Do not resolve any deeper metadata path after a link, special node,
    # unreadable directory, or budget failure was observed. Parent-component
    # resolution could otherwise cross the project boundary while checking a
    # well-known child such as objects/info/alternates.
    if risks:
        return risks

    dangerous_paths = {
        ".git/commondir": "Git commondir indirection",
        ".git/config.worktree": "per-worktree Git config",
        ".git/objects/info/alternates": "Git object alternates",
        ".git/info/grafts": "Git graft replacement metadata",
    }
    for relative, description in dangerous_paths.items():
        path = root / relative
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            risks.append(f"{relative}: cannot determine metadata scope ({exc})")
        else:
            risks.append(f"{relative}: {description} requires an explicit adapter")

    replace_dir = git_dir / "refs" / "replace"
    try:
        replace_dir.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        risks.append(f".git/refs/replace: cannot inspect replacement refs ({exc})")
    else:
        risks.append(".git/refs/replace: replacement refs are not allowed during reconnaissance")

    config = git_dir / "config"
    try:
        config.lstat()
    except FileNotFoundError:
        risks.append(".git/config is missing")
    except OSError as exc:
        risks.append(f".git/config cannot be inspected ({exc})")
    else:
        try:
            risks.extend(_git_config_scope_risks(_read_git_metadata_file(config, label=".git/config")))
        except DeliveryError as exc:
            risks.append(str(exc))

    packed_refs = git_dir / "packed-refs"
    try:
        packed_refs.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        risks.append(f".git/packed-refs cannot be inspected ({exc})")
    else:
        try:
            packed_text = _read_git_metadata_file(packed_refs, label=".git/packed-refs")
        except DeliveryError as exc:
            risks.append(str(exc))
        else:
            if any(
                line.strip() and not line.lstrip().startswith(("#", "^"))
                and " refs/replace/" in f" {line.strip()}"
                for line in packed_text.splitlines()
            ):
                risks.append(".git/packed-refs contains replacement refs")
    return risks


def _resolve_git_executable(root: Path) -> tuple[str, str]:
    """Resolve Git from absolute, non-project PATH entries before changing cwd."""

    raw_path = os.environ.get("PATH") or os.defpath
    safe_entries: list[str] = []
    for raw_entry in raw_path.split(os.pathsep):
        if not raw_entry or not os.path.isabs(raw_entry):
            continue
        try:
            entry = Path(raw_entry).resolve(strict=True)
        except OSError:
            continue
        if not entry.is_dir():
            continue
        try:
            entry.relative_to(root)
        except ValueError:
            safe_entries.append(str(entry))
    safe_path = os.pathsep.join(dict.fromkeys(safe_entries))
    executable = shutil.which("git", path=safe_path) if safe_path else None
    if executable is None:
        raise DeliveryError(
            "unable to run git: no executable was found in absolute PATH directories outside the project"
        )
    resolved_executable = Path(executable).resolve(strict=True)
    try:
        resolved_executable.relative_to(root)
    except ValueError:
        pass
    else:
        raise DeliveryError(
            "unable to run git: the selected executable resolves inside the project"
        )
    return str(resolved_executable), safe_path


def run_git(root: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    # Git inherits several variables and repository settings that can execute
    # helpers or write trace files. Reconnaissance uses a minimal environment
    # and command-scoped overrides so read-only queries remain truly read-only.
    metadata_risks = _git_metadata_safety_risks(root)
    if metadata_risks:
        raise DeliveryError("unsafe Git metadata scope: " + "; ".join(metadata_risks))

    git_executable, safe_path = _resolve_git_executable(root)

    environment: dict[str, str] = {"PATH": safe_path}
    for key in ("SYSTEMROOT",):
        if os.environ.get(key):
            environment[key] = os.environ[key]
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_PAGER": "cat",
            "GIT_TRACE": "0",
            "GIT_TRACE_PACKET": "0",
            "GIT_TRACE_PERFORMANCE": "0",
            "GIT_TRACE_SETUP": "0",
            "GIT_TRACE_SHALLOW": "0",
            "GIT_TRACE_CURL": "0",
            "GIT_TRACE2": "0",
            "GIT_TRACE2_EVENT": "0",
            "GIT_TRACE2_PERF": "0",
            "LC_ALL": "C",
        }
    )
    command = [
        git_executable,
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        f"core.attributesFile={os.devnull}",
        "-c",
        "core.bare=false",
        "-c",
        f"core.worktree={root}",
        "-c",
        "submodule.recurse=false",
        *arguments,
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise DeliveryError(f"unable to run git: {exc}") from exc
    assert process.stdout is not None and process.stderr is not None
    if os.name == "nt":
        # Windows select() only supports sockets, not pipe descriptors.
        # communicate() reads both pipes on reader threads and provides the
        # same bounded wait; the output budget is enforced after collection.
        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=15)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise DeliveryError("unable to run git: command timed out") from exc
        finally:
            process.stdout.close()
            process.stderr.close()
        if len(stdout_bytes) + len(stderr_bytes) > GIT_OUTPUT_LIMIT_BYTES:
            raise DeliveryError(
                f"Git output exceeded the {GIT_OUTPUT_LIMIT_BYTES}-byte reconnaissance limit"
            )
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
        )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    deadline = time.monotonic() + 15
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise DeliveryError("unable to run git: command timed out")
            events = selector.select(timeout=remaining)
            if not events:
                continue
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > GIT_OUTPUT_LIMIT_BYTES:
                    process.kill()
                    process.wait()
                    raise DeliveryError(
                        f"Git output exceeded the {GIT_OUTPUT_LIMIT_BYTES}-byte reconnaissance limit"
                    )
                buffers[key.data].extend(chunk)
        return_code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        raise DeliveryError("unable to run git: command timed out") from exc
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    return subprocess.CompletedProcess(
        command,
        return_code,
        buffers["stdout"].decode("utf-8", errors="replace"),
        buffers["stderr"].decode("utf-8", errors="replace"),
    )


def _git_attribute_filter_risks(root: Path, *, max_entries: int = 50_000) -> list[str]:
    """Find repository attributes that could cause Git to execute filters."""

    risks: list[str] = []
    observed = 0

    def inspect_attribute(path: Path, relative: str) -> None:
        try:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                risks.append(f"{relative}: symbolic-link attribute file")
                return
            if not stat.S_ISREG(metadata.st_mode):
                risks.append(f"{relative}: non-regular attribute file")
                return
            if metadata.st_size > 1_048_576:
                risks.append(f"{relative}: attribute file exceeds inspection limit")
                return
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            risks.append(f"{relative}: attribute file cannot be safely read ({exc})")
            return
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and re.search(r"(?:^|\s)(?:-?filter)(?:=|\s|$)", stripped):
                risks.append(f"{relative}: Git filter attribute requires explicit review")
                return

    def onerror(error: OSError) -> None:
        risks.append(f"attribute preflight cannot scan project tree ({error})")

    for current_text, dirnames, filenames in os.walk(root, topdown=True, followlinks=False, onerror=onerror):
        dirnames[:] = sorted(name for name in dirnames if not (Path(current_text) == root and name == ".git"))
        for name in [*dirnames, *sorted(filenames)]:
            observed += 1
            if observed > max_entries:
                risks.append("attribute preflight exceeded its bounded entry budget")
                return risks
        if ".gitattributes" in filenames:
            path = Path(current_text) / ".gitattributes"
            inspect_attribute(path, path.relative_to(root).as_posix())

    info_attributes = root / ".git" / "info" / "attributes"
    if info_attributes.exists() or info_attributes.is_symlink():
        inspect_attribute(info_attributes, ".git/info/attributes")
    return risks


def git_info(root: Path) -> dict[str, Any]:
    git_marker = root / ".git"
    try:
        marker_mode = git_marker.lstat().st_mode
    except FileNotFoundError:
        return {
            "repository": False,
            "status": "absent",
            "commit": None,
            "dirty_paths": [],
            "clean_for_gate": False,
            "error": None,
        }
    except OSError as exc:
        return {
            "repository": None,
            "status": "error",
            "commit": None,
            "dirty_paths": [],
            "clean_for_gate": False,
            "error": f"Git metadata cannot be safely inspected: {exc}",
        }
    if stat.S_ISLNK(marker_mode) or not stat.S_ISDIR(marker_mode):
        return {
            "repository": None,
            "status": "error",
            "commit": None,
            "dirty_paths": [],
            "clean_for_gate": False,
            "error": "Git metadata is external, symbolic, or not a directory; an explicit Worktree adapter is required",
        }
    metadata_risks = _git_metadata_safety_risks(root)
    if metadata_risks:
        return {
            "repository": None,
            "status": "error",
            "commit": None,
            "dirty_paths": [],
            "clean_for_gate": False,
            "error": "Git metadata cannot be inspected safely before invoking Git",
            "safety_risks": metadata_risks,
        }
    try:
        probe = run_git(root, ["rev-parse", "--show-toplevel"])
    except DeliveryError as exc:
        return {
            "repository": None,
            "status": "unknown",
            "commit": None,
            "dirty_paths": [],
            "clean_for_gate": False,
            "error": str(exc),
        }
    if probe.returncode != 0:
        detail = probe.stderr.strip() or probe.stdout.strip() or f"exit status {probe.returncode}"
        return {
            "repository": None,
            "status": "error",
            "commit": None,
            "dirty_paths": [],
            "clean_for_gate": False,
            "error": f"Git metadata exists but cannot be inspected: {detail}",
        }
    top_level = Path(os.path.abspath(probe.stdout.strip()))
    if top_level != root:
        # Stop after the single scope probe. A project nested inside another
        # repository cannot safely bind evidence to its subtree, and no later
        # Git query may run after a scope mismatch.
        return {
            "repository": True,
            "status": "nested",
            "root": str(top_level),
            "commit": None,
            "dirty_paths": [],
            "gate_relevant_dirty_paths": [],
            "clean_for_gate": False,
            "nested_project": True,
            "error": "Git top-level differs from the canonical project root; no further Git queries were run",
        }
    filter_risks = _git_attribute_filter_risks(root)
    if filter_risks:
        try:
            commit = _git_commit(root)
        except DeliveryError:
            commit = None
        return {
            "repository": True,
            "status": "error",
            "root": str(top_level),
            "commit": commit,
            "dirty_paths": [],
            "gate_relevant_dirty_paths": [],
            "clean_for_gate": False,
            "nested_project": top_level != root,
            "error": "Git status was not run because repository attributes may execute a content filter",
            "safety_risks": filter_risks,
        }
    try:
        dirty_paths = _git_dirty_paths(root)
        status_error = None
    except DeliveryError as exc:
        dirty_paths = []
        status_error = str(exc)
    try:
        commit = _git_commit(root)
    except DeliveryError as exc:
        commit = None
        status_error = f"{status_error}; {exc}" if status_error else str(exc)
    relevant = [path for path in dirty_paths if not _gate_ignored_dirty_path(path)]
    return {
        "repository": True,
        "status": (
            "clean"
            if not relevant and status_error is None
            else "dirty"
            if status_error is None
            else "error"
        ),
        "root": str(top_level),
        "commit": commit,
        "dirty_paths": dirty_paths,
        "dirty_path_count": len(dirty_paths),
        "gate_relevant_dirty_paths": relevant,
        "gate_relevant_dirty_path_count": len(relevant),
        "clean_for_gate": not relevant and status_error is None,
        "nested_project": False,
        "error": status_error,
    }


def _git_commit(root: Path) -> str | None:
    result = run_git(root, ["rev-parse", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _git_dirty_paths(root: Path) -> list[str]:
    result = run_git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        raise DeliveryError(f"git status failed; worktree cleanliness is unknown: {detail}")
    paths: list[str] = []
    records = result.stdout.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 3:
            raise DeliveryError("git status returned a malformed NUL-delimited record")
        status = record[:2]
        path = record[3:] if record[2] == " " else record[2:]
        if path:
            paths.append(path)
        if any(code in status for code in ("R", "C")):
            if index >= len(records) or not records[index]:
                raise DeliveryError("git status returned an incomplete rename/copy record")
            source_path = records[index]
            index += 1
            paths.append(source_path)
    return paths


def _gate_ignored_dirty_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return (
        normalized in {"delivery-docs/state", ".delivery"}
        or normalized.startswith("delivery-docs/state/")
        or normalized.startswith("delivery-docs/verification/runs/")
        # Legacy layouts kept for backward compatibility.
        or normalized.startswith(".delivery/")
        or normalized.startswith("docs/verification/runs/")
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _artifact_reference(raw: Any, index: int) -> tuple[str, str | None]:
    if isinstance(raw, str) and raw.strip():
        return raw.strip(), None
    if isinstance(raw, dict):
        reference = raw.get("path") or raw.get("url")
        if not isinstance(reference, str) or not reference.strip():
            raise DeliveryError(f"gate.artifacts[{index}] must contain a non-empty path or url")
        expected_digest = raw.get("sha256")
        if expected_digest is not None and (
            not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_digest)
        ):
            raise DeliveryError(f"gate.artifacts[{index}].sha256 must contain 64 hexadecimal characters")
        return reference.strip(), expected_digest.lower() if expected_digest else None
    raise DeliveryError(f"gate.artifacts[{index}] must be a path string or object")


def check_gate_data(
    value: Any,
    root: Path,
    *,
    require_passed: bool,
    require_fresh: bool,
    require_artifacts: bool,
    require_clean: bool,
    max_age_seconds: int,
    expected_change_id: str | None = None,
    expected_source_digest: str | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    gate: dict[str, Any]
    try:
        gate = ensure_object(value, "gate manifest")
        validate_versioned(gate, "gate manifest", harness=True)
        require_keys(
            gate,
            {
                "change_id",
                "source_digest",
                "commit",
                "environment",
                "config_digest",
                "test_suite_digest",
                "started_at",
                "finished_at",
                "results",
                "artifacts",
                "status",
            },
            "gate manifest",
        )
    except DeliveryError as exc:
        return {"valid": False, "passed": False, "fresh": False, "errors": [str(exc)], "warnings": []}

    status = gate.get("status")
    if status not in GATE_STATUSES:
        errors.append(f"gate.status must be one of {sorted(GATE_STATUSES)}")
    if require_passed and status != "passed":
        errors.append(f"gate.status must be 'passed'; found {status!r}")

    for field in (
        "change_id",
        "source_digest",
        "commit",
        "environment",
        "config_digest",
        "test_suite_digest",
    ):
        item = gate.get(field)
        if status == "not_run" and item is None:
            continue
        if not isinstance(item, str) or not item.strip():
            errors.append(f"gate.{field} must be a non-empty string")
    if require_passed or require_fresh:
        if not isinstance(expected_change_id, str) or not expected_change_id.strip():
            errors.append("fresh gate validation requires a non-empty active Change in project state")
        if not is_sha256(expected_source_digest):
            errors.append("fresh gate validation requires a 64-hex source digest in project state")
    for field in ("config_digest", "test_suite_digest", "source_digest"):
        item = gate.get(field)
        if status == "not_run" and item is None:
            continue
        if not is_sha256(item):
            errors.append(f"gate.{field} must contain 64 lowercase hexadecimal characters")
    if expected_change_id is not None and gate.get("change_id") != expected_change_id:
        errors.append(
            f"gate change is stale: gate={gate.get('change_id')!r}, active={expected_change_id!r}"
        )
    if expected_source_digest is not None and gate.get("source_digest") != expected_source_digest:
        errors.append(
            "gate source digest is stale: "
            f"gate={gate.get('source_digest')!r}, current={expected_source_digest!r}"
        )

    results = gate.get("results")
    if not isinstance(results, dict):
        errors.append("gate.results must be an object")
        results = {}
    else:
        for name, result_status in results.items():
            if not isinstance(name, str) or not name.strip():
                errors.append("gate.results keys must be non-empty strings")
            if result_status not in RESULT_STATUSES:
                errors.append(f"gate.results.{name} must be one of {sorted(RESULT_STATUSES)}")
        if status == "passed":
            if not results:
                errors.append("a passed gate must contain at least one result")
            nonpassing = {name: result for name, result in results.items() if result not in {"passed", "not_applicable"}}
            if nonpassing:
                errors.append(f"a passed gate contains non-passing results: {nonpassing}")
            if results and all(result == "not_applicable" for result in results.values()):
                errors.append("a passed gate must contain at least one actually passed result")

    started: dt.datetime | None = None
    finished: dt.datetime | None = None
    if status != "not_run" or gate.get("started_at") is not None:
        try:
            started = parse_datetime(gate.get("started_at"), "gate.started_at")
            finished = parse_datetime(gate.get("finished_at"), "gate.finished_at")
            if finished < started:
                errors.append("gate.finished_at cannot be earlier than gate.started_at")
            if finished > utc_now() + dt.timedelta(minutes=5):
                errors.append("gate.finished_at is implausibly far in the future")
        except DeliveryError as exc:
            errors.append(str(exc))

    artifacts = gate.get("artifacts")
    artifact_details: list[dict[str, Any]] = []
    verified_local_artifacts = 0
    if not isinstance(artifacts, list):
        errors.append("gate.artifacts must be an array")
        artifacts = []
    if require_artifacts and not artifacts:
        errors.append("a gated transition requires at least one evidence artifact")
    for index, raw in enumerate(artifacts):
        try:
            reference, expected_digest = _artifact_reference(raw, index)
        except DeliveryError as exc:
            errors.append(str(exc))
            continue
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", reference):
            artifact_details.append(
                {
                    "reference": reference,
                    "kind": "url",
                    "exists": None,
                    "verified": False,
                }
            )
            continue
        artifact_path = Path(reference)
        if not artifact_path.is_absolute():
            artifact_path = root / artifact_path
        artifact_path = artifact_path.resolve()
        try:
            artifact_path.relative_to(root.resolve())
        except ValueError:
            errors.append(f"gate artifact escapes project root: {reference}")
            continue
        exists = artifact_path.is_file()
        detail: dict[str, Any] = {
            "reference": reference,
            "kind": "file",
            "exists": exists,
            "verified": False,
        }
        if not exists:
            errors.append(f"gate artifact does not exist or is not a file: {reference}")
        elif not expected_digest:
            errors.append(f"gate artifact requires a matching SHA-256 digest: {reference}")
        else:
            actual = sha256_file(artifact_path)
            detail["sha256"] = actual
            if actual != expected_digest:
                errors.append(f"gate artifact digest mismatch: {reference}")
            else:
                detail["verified"] = True
                verified_local_artifacts += 1
        artifact_details.append(detail)
    if require_artifacts and verified_local_artifacts < 1:
        errors.append(
            "a gated transition requires at least one project-local evidence file with a matching SHA-256"
        )

    git = git_info(root)
    fresh = status == "passed" and not errors
    if require_fresh:
        if git.get("error"):
            errors.append(str(git["error"]))
            fresh = False
        if not git.get("repository"):
            errors.append("fresh evidence requires the project root to be a Git repository")
            fresh = False
        elif git.get("nested_project"):
            errors.append("project root is nested inside another Git repository; evidence scope is ambiguous")
            fresh = False
        elif not git.get("commit"):
            errors.append("fresh evidence requires at least one Git commit")
            fresh = False
        elif gate.get("commit") != git.get("commit"):
            errors.append(
                f"gate commit is stale: gate={gate.get('commit')!r}, current={git.get('commit')!r}"
            )
            fresh = False
        if require_clean and not git.get("clean_for_gate"):
            errors.append(f"worktree has gate-relevant changes: {git.get('gate_relevant_dirty_paths', [])}")
            fresh = False
        if finished is None:
            fresh = False
        else:
            age = (utc_now() - finished).total_seconds()
            if age < -300:
                errors.append("gate evidence is dated in the future")
                fresh = False
            elif age > max_age_seconds:
                errors.append(
                    f"gate evidence is too old ({int(age)}s > {max_age_seconds}s); rerun the gate"
                )
                fresh = False
    elif finished is not None:
        age = (utc_now() - finished).total_seconds()
        if age > max_age_seconds:
            warnings.append(f"gate evidence is older than the configured freshness window ({int(age)}s)")

    valid = not errors
    passed = valid and status == "passed" and (fresh if require_fresh else True)
    effective_status = "stale" if status == "passed" and not passed else status
    return {
        "valid": valid,
        "passed": passed,
        "fresh": fresh if require_fresh else None,
        "status": status,
        "effective_status": effective_status,
        "commit": gate.get("commit"),
        "change_id": gate.get("change_id"),
        "source_digest": gate.get("source_digest"),
        "current_commit": git.get("commit"),
        "max_age_seconds": max_age_seconds,
        "artifacts": artifact_details,
        "errors": errors,
        "warnings": warnings,
    }


def event_record(
    *,
    sequence: int,
    event_type: str,
    actor: str,
    state_revision: int,
    details: Mapping[str, Any],
    state_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    record = {
        "schema_version": SCHEMA_VERSION,
        "harness_version": HARNESS_VERSION,
        "event_id": str(uuid.uuid4()),
        "sequence": sequence,
        "event_type": event_type,
        "timestamp": isoformat(),
        "actor": actor,
        "state_revision": state_revision,
        "details": dict(details),
    }
    if state_snapshot is not None:
        snapshot = json.loads(canonical_json_bytes(dict(state_snapshot)).decode("utf-8"))
        record["state_snapshot"] = snapshot
        record["state_digest"] = canonical_sha256(snapshot)
    return record


def next_event_sequence(events: Sequence[Mapping[str, Any]]) -> int:
    return int(events[-1]["sequence"]) + 1 if events else 0


def _slug(raw: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw.strip()).strip("-.").lower()
    return slug or "project"


def _inspect_delivery_control(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    delivery_dir = root / DELIVERY_DIRNAME
    directory_exists = delivery_dir.exists() or delivery_dir.is_symlink()
    manifest_path = delivery_dir / "manifest.json"
    manifest_present = manifest_path.exists() or manifest_path.is_symlink()
    validation: dict[str, Any] | None = None
    phase: str | None = None
    state: dict[str, Any] | None = None
    safety_error: str | None = None
    if directory_exists:
        try:
            ensure_safe_control_path(root, delivery_dir, label="delivery directory")
            for name in (
                "manifest.json",
                "state.json",
                "events.jsonl",
                "questions.json",
                "assumptions.json",
                "gate-manifest.json",
                "planning-manifest.json",
                "transaction.json",
            ):
                path = delivery_dir / name
                if path.exists() or path.is_symlink():
                    ensure_safe_control_path(root, path, label=f"delivery {name}")
        except DeliveryError as exc:
            safety_error = str(exc)
    if manifest_present and safety_error is None:
        validation = validate_project(root, check_fresh_gate=False)
        if validation.get("components", {}).get("state", {}).get("valid"):
            try:
                state = validate_state(read_json(delivery_dir / "state.json"))
                phase = state.get("phase")
            except DeliveryError:
                state = None
                phase = None
    valid = bool(validation and validation.get("valid")) and safety_error is None
    partial = directory_exists and not manifest_present
    validation_errors = list(validation.get("errors", [])) if validation else []
    if safety_error:
        validation_errors.insert(0, safety_error)
    gate = (
        validation.get("components", {}).get("gate", {})
        if validation
        else {}
    )
    public_validation = None
    if validation is not None:
        public_validation = json.loads(json.dumps(validation, ensure_ascii=False))
        public_validation.pop("validated_at", None)
        public_validation.pop("project_root", None)
    public = {
        "directory_exists": directory_exists,
        "initialized": manifest_present,
        "partial": partial,
        "valid": valid,
        "phase": phase,
        "validation": public_validation,
        "safety_error": safety_error,
    }
    context = {
        **public,
        "manifest_present": manifest_present,
        "state": state,
        "gate": gate,
        "errors": validation_errors,
    }
    return public, context


def _legacy_signals(capabilities: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "technology": [item["id"] for item in capabilities.get("stacks", [])],
        "source_directories": list(capabilities.get("source_directories", [])),
        "test_directories": list(capabilities.get("testing", {}).get("directories", [])),
        "spec_candidates": list(capabilities.get("spec_candidates", [])),
        "prototype_candidates": list(capabilities.get("prototype_candidates", [])),
        "openspec": bool(capabilities.get("openspec", {}).get("present")),
        "qoder": bool(capabilities.get("qoder", {}).get("present")),
        "ci": bool(capabilities.get("ci")),
    }


def inspect_project(
    root: Path,
    *,
    route_override: str = "auto",
    source_paths: Sequence[str] = (),
    scan_limits: Mapping[str, Any] | None = None,
    expected_git_dir: str | None = None,
    expected_common_dir: str | None = None,
    git_authority_ref: str | None = None,
) -> dict[str, Any]:
    marker = root / ".git"
    try:
        linked_marker = stat.S_ISREG(marker.lstat().st_mode)
    except OSError:
        linked_marker = False
    if linked_marker and any(
        value is not None
        for value in (expected_git_dir, expected_common_dir, git_authority_ref)
    ):
        try:
            scope = git_scope.inspect_git_scope(
                root,
                expected_git_dir=expected_git_dir,
                expected_common_dir=expected_common_dir,
                authority_ref=git_authority_ref,
            )
        except git_scope.GitScopeError as exc:
            git = {
                "repository": None,
                "status": "error",
                "commit": None,
                "dirty_paths": [],
                "gate_relevant_dirty_paths": [],
                "clean_for_gate": False,
                "error": str(exc),
                "reason_code": exc.code,
            }
        else:
            opaque_dirty = ["<opaque-worktree-dirty>"] if scope["dirty"] else []
            git = {
                "repository": True,
                "status": "dirty" if scope["dirty"] else "clean",
                "commit": scope["head"],
                "dirty_paths": opaque_dirty,
                "gate_relevant_dirty_paths": opaque_dirty,
                "clean_for_gate": not scope["dirty"],
                "error": None,
                "checkout_kind": scope["checkout_kind"],
                "repository_scope_id": scope["repository_scope_id"],
                "checkout_scope_id": scope["checkout_scope_id"],
                "dirty_digest": scope["dirty_digest"],
                "authority_ref": scope.get("authority_ref"),
            }
    else:
        git = git_info(root)
    delivery, delivery_context = _inspect_delivery_control(root)
    try:
        report = reconnaissance.build_reconnaissance(
            root,
            git=git,
            delivery=delivery_context,
            source_paths=source_paths,
            route_override=route_override,
            git_runner=None if linked_marker else run_git,
            limits=scan_limits,
            harness_version=HARNESS_VERSION,
        )
    except ValueError as exc:
        raise DeliveryError(f"invalid reconnaissance option: {exc}") from exc
    capabilities = report["capabilities"]
    report.update(
        {
            "schema_version": SCHEMA_VERSION,
            "git": report["capabilities"]["git"],
            "signals": _legacy_signals(capabilities),
            "delivery": delivery,
            "recommended_mode": report["route"]["recommended"],
            "routing_reason": ", ".join(report["route"]["reason_codes"]),
        }
    )
    return report


def initial_documents(root: Path, mode: str, project_id: str) -> dict[str, str]:
    created_at = isoformat()
    substitutions = {
        "__SCHEMA_VERSION__": SCHEMA_VERSION,
        "__HARNESS_VERSION__": HARNESS_VERSION,
        "__PROJECT_ID__": project_id,
        "__PROJECT_MODE__": mode,
        "__CREATED_AT__": created_at,
    }
    manifest = load_json_template("delivery/manifest.json.tmpl", substitutions)
    state = load_json_template("delivery/state.json.tmpl", substitutions)
    questions = load_json_template("delivery/questions.json.tmpl", substitutions)
    assumptions = load_json_template("delivery/assumptions.json.tmpl", substitutions)
    gate = load_json_template("delivery/gate-manifest.json.tmpl", substitutions)
    documents = {
        "manifest.json": json_text(manifest),
        "state.json": json_text(state),
        "questions.json": json_text(questions),
        "assumptions.json": json_text(assumptions),
        "gate-manifest.json": json_text(gate),
    }
    initial_event_template = load_json_template("delivery/event.json.tmpl", substitutions)
    initial_event_template.update(
        event_record(
            sequence=0,
            event_type="delivery.initialized",
            actor="deliveryctl",
            state_revision=0,
            details={
                "from": None,
                "to": state["phase"],
                "project_mode": mode,
                "project_id": project_id,
            },
            state_snapshot=state,
        )
    )
    documents["events.jsonl"] = json.dumps(initial_event_template, ensure_ascii=False, separators=(",", ":")) + "\n"
    return documents


def initialize_project(
    root: Path,
    *,
    mode: str,
    project_id: str | None,
    with_openspec_config: bool,
) -> dict[str, Any]:
    if with_openspec_config:
        raise DeliveryError(
            "M3 no longer writes OpenSpec config during init; use openspec-config-plan, "
            "review its candidate digest, then openspec-config-apply with an approval reference"
        )
    delivery_dir = root / DELIVERY_DIRNAME
    if delivery_dir.exists() or delivery_dir.is_symlink():
        ensure_safe_control_path(root, delivery_dir, label="delivery directory")
        if not delivery_dir.is_dir():
            raise DeliveryError("delivery-docs/state exists but is not a directory")
        try:
            has_entries = next(delivery_dir.iterdir(), None) is not None
        except OSError as exc:
            raise DeliveryError(f"cannot inspect existing delivery-docs/state directory: {exc}") from exc
        manifest_path = delivery_dir / "manifest.json"
        if has_entries and not manifest_path.exists():
            raise DeliveryError(
                "partial delivery-docs/state control plane exists without manifest.json; "
                "refusing initialization and requiring explicit recovery"
            )
        if manifest_path.exists():
            existing_validation = validate_project(root, check_fresh_gate=False)
            if not existing_validation["valid"]:
                raise DeliveryError(
                    "existing delivery-docs/state control plane is invalid; refusing initialization: "
                    f"{existing_validation['errors']}"
                )
    if mode == "auto":
        inspection = inspect_project(root)
        recommendation = inspection["recommended_mode"]
        if recommendation == "resume":
            existing_state = validate_state(read_json(delivery_path(root, "state.json")))
            mode = existing_state["project_mode"]
        else:
            mode = recommendation
    if mode not in PROJECT_MODES:
        raise DeliveryError(f"mode must be one of auto, {', '.join(sorted(PROJECT_MODES))}")
    project_id = _slug(project_id or root.name)
    documents = initial_documents(root, mode, project_id)
    ensure_safe_control_path(root, delivery_dir, label="delivery directory")
    for name in documents:
        ensure_safe_control_path(root, delivery_dir / name, label="delivery control file")
    # Preflight every existing control file before writing anything.  This makes
    # Only an empty control-plane path may be initialized. Partial or invalid
    # control data is a recovery case and must never be silently overwritten.
    existing = {name: delivery_dir / name for name in documents if (delivery_dir / name).exists()}
    if existing:
        validators = {
            "manifest.json": validate_manifest,
            "state.json": validate_state,
            "questions.json": lambda value: validate_item_catalog(
                value, "questions", id_prefix="Q", statuses=QUESTION_STATUSES
            ),
            "assumptions.json": lambda value: validate_item_catalog(
                value, "assumptions", id_prefix="A", statuses=ASSUMPTION_STATUSES
            ),
            "gate-manifest.json": lambda value: check_gate_data(
                value,
                root,
                require_passed=False,
                require_fresh=False,
                require_artifacts=False,
                require_clean=False,
                max_age_seconds=DEFAULT_GATE_MAX_AGE_SECONDS,
            ),
        }
        for name, path in existing.items():
            if name == "events.jsonl":
                events = read_jsonl(path)
                if not events:
                    raise DeliveryError(f"refusing to initialize over empty event log: {path}")
                continue
            checked = validators[name](read_json(path))
            if isinstance(checked, dict) and checked.get("valid") is False:
                raise DeliveryError(f"refusing to initialize over invalid {name}: {checked['errors']}")
        if "manifest.json" in existing:
            manifest = validate_manifest(read_json(existing["manifest.json"]))
            if manifest["project_mode"] != mode:
                raise DeliveryError(
                    f"existing project mode is {manifest['project_mode']!r}; refusing requested mode {mode!r}"
                )
            if manifest["project_id"] != project_id and project_id != _slug(root.name):
                raise DeliveryError(
                    f"existing project ID is {manifest['project_id']!r}; refusing requested ID {project_id!r}"
                )

    created: list[str] = []
    preserved: list[str] = []
    with ProjectLock(root):
        for name, text in documents.items():
            path = delivery_dir / name
            if path.exists():
                preserved.append(str(path.relative_to(root)))
                continue
            controlled_exclusive_write_text(root, path, text)
            created.append(str(path.relative_to(root)))
        for directory in (delivery_dir / "runs", delivery_dir / "work-items"):
            ensure_safe_control_path(root, directory, label="delivery directory")
            directory.mkdir(parents=True, exist_ok=True)

    validation = validate_project(root, check_fresh_gate=False)
    return {
        "initialized": validation["valid"],
        "project_root": str(root),
        "project_id": validate_manifest(read_json(delivery_path(root, "manifest.json")))["project_id"],
        "project_mode": validate_state(read_json(delivery_path(root, "state.json")))["project_mode"],
        "created": created,
        "preserved": preserved,
        "validation": validation,
    }


def _manifest_policy(manifest: Mapping[str, Any], key: str, default: Any) -> Any:
    policies = manifest.get("policies")
    return policies.get(key, default) if isinstance(policies, dict) else default


def _planning_authority_paths(manifest: Mapping[str, Any]) -> dict[str, str]:
    """Return validated planning paths, preserving Brownfield mappings."""

    paths = manifest.get("paths") if isinstance(manifest.get("paths"), dict) else {}
    result = dict(planning.DEFAULT_AUTHORITY_PATHS)
    mapping = {
        "requirements": "requirements",
        "acceptance": "acceptance",
        "features": "feature_ledger",
        "roadmap": "roadmap",
        "work_items": "work_items",
    }
    for planning_key, manifest_key in mapping.items():
        value = paths.get(manifest_key)
        if isinstance(value, str):
            result[planning_key] = value
    return result


def collect_planning_context(
    root: Path,
    *,
    approved_decisions: Sequence[str],
    pending_decisions: Sequence[str],
    blocking_decisions: Sequence[str],
    expected_git_dir: str | None,
    expected_common_dir: str | None,
    git_authority_ref: str | None,
    openspec_executable: str | None,
    readiness: str,
) -> dict[str, Any]:
    manifest = validate_manifest(read_json(delivery_path(root, "manifest.json")))
    state = validate_state(read_json(delivery_path(root, "state.json")))
    baseline = state.get("baseline_commit")
    if not isinstance(baseline, str):
        raise DeliveryError(
            "planning binding requires state.baseline_commit; capture Git scope and event it with set-context"
        )
    combined_pending = sorted(set(pending_decisions) | set(state.get("pending_decisions", [])))
    try:
        result = planning_bindings.collect_external_bindings(
            root,
            project_mode=state["project_mode"],
            baseline=baseline,
            approved_decisions=approved_decisions,
            pending_decisions=combined_pending,
            blocking_decisions=blocking_decisions,
            authority_paths=_planning_authority_paths(manifest),
            expected_git_dir=expected_git_dir,
            expected_common_dir=expected_common_dir,
            git_authority_ref=git_authority_ref,
            openspec_executable=openspec_executable,
            readiness=readiness,
        )
    except planning_bindings.PlanningBindingsError as exc:
        raise DeliveryError(f"{exc.code}: {exc.message}") from exc
    return result


def _sealed_decisions_and_paths(root: Path) -> tuple[dict[str, list[str]], dict[str, str]]:
    seal = read_json_no_duplicates(
        delivery_path(root, "planning-manifest.json"), label="planning manifest"
    )
    if not isinstance(seal, dict) or not isinstance(seal.get("external_bindings"), dict):
        raise DeliveryError("planning manifest does not contain reusable sanitized bindings")
    external = seal["external_bindings"]
    decisions = external.get("decisions")
    authority_paths = external.get("authority_paths")
    if not isinstance(decisions, dict) or not isinstance(authority_paths, dict):
        raise DeliveryError("planning manifest decision/path bindings are invalid")
    normalized = {
        key: list(decisions.get(key, []))
        for key in ("approved", "pending", "blocking")
    }
    return normalized, dict(authority_paths)


def collect_live_bindings_for_sealed_plan(
    root: Path,
    *,
    expected_git_dir: str | None,
    expected_common_dir: str | None,
    git_authority_ref: str | None,
    openspec_executable: str | None,
) -> dict[str, Any]:
    decisions, _ = _sealed_decisions_and_paths(root)
    return collect_planning_context(
        root,
        approved_decisions=decisions["approved"],
        pending_decisions=decisions["pending"],
        blocking_decisions=decisions["blocking"],
        expected_git_dir=expected_git_dir,
        expected_common_dir=expected_common_dir,
        git_authority_ref=git_authority_ref,
        openspec_executable=openspec_executable,
        readiness="seal",
    )


def initialize_planning_bundle(root: Path) -> dict[str, Any]:
    """Create empty M3 authorities without overwriting any project artifact."""

    validation = validate_project(root, check_fresh_gate=False)
    if not validation["valid"]:
        raise DeliveryError(f"cannot initialize planning from an invalid control plane: {validation['errors']}")
    manifest = validate_manifest(read_json(delivery_path(root, "manifest.json")))
    state = validate_state(read_json(delivery_path(root, "state.json")))
    if state["phase"] in PLANNING_GATED_PHASES or state["phase"] in QUALITY_GATED_PHASES:
        raise DeliveryError(
            f"planning authorities cannot be initialized while the project is in phase {state['phase']}"
        )
    authority = _planning_authority_paths(manifest)
    substitutions = {"__PROJECT_MODE__": state["project_mode"]}
    templates = {
        authority["requirements"]: load_json_template("planning/requirements.json.tmpl", substitutions),
        authority["acceptance"]: load_json_template("planning/acceptance.json.tmpl", substitutions),
        authority["features"]: load_json_template("planning/feature-ledger.json.tmpl", substitutions),
        authority["roadmap"]: load_json_template("planning/delivery-roadmap.json.tmpl", substitutions),
    }
    created: list[str] = []
    preserved: list[str] = []
    with ProjectLock(root):
        locked_validation = validate_project(root, check_fresh_gate=False)
        if not locked_validation["valid"]:
            raise DeliveryError(
                f"planning control plane changed before initialization: {locked_validation['errors']}"
            )
        locked_manifest = validate_manifest(read_json(delivery_path(root, "manifest.json")))
        locked_state = validate_state(read_json(delivery_path(root, "state.json")))
        if canonical_sha256(locked_manifest) != canonical_sha256(manifest):
            raise DeliveryError("manifest changed before planning initialization; retry")
        if canonical_sha256(locked_state) != canonical_sha256(state):
            raise DeliveryError("state changed before planning initialization; retry")
        prepared: list[tuple[str, Path, Any]] = []
        for relative, value in templates.items():
            path = ensure_safe_control_path(root, root / relative, label="planning authority")
            if path.exists():
                if path.is_symlink() or not path.is_file():
                    raise DeliveryError(f"planning authority is not a regular local file: {relative}")
                preserved.append(relative)
            prepared.append((relative, path, value))
        work_items = ensure_safe_control_path(
            root, root / authority["work_items"], label="planning work-items directory"
        )
        if work_items.exists() and (work_items.is_symlink() or not work_items.is_dir()):
            raise DeliveryError("planning work-items path is not a regular local directory")
        work_items_missing = not work_items.exists()
        # Every destination is preflighted before the first write so a later
        # unsafe Brownfield mapping cannot leave a partially initialized graph.
        for relative, path, value in prepared:
            if relative in preserved:
                continue
            controlled_exclusive_write_json(root, path, value)
            created.append(relative)
        work_items.mkdir(parents=True, exist_ok=True)
        if work_items_missing:
            created.append(authority["work_items"] + "/")
        if created:
            events = read_jsonl(delivery_path(root, "events.jsonl"))
            controlled_append_jsonl(
                root,
                delivery_path(root, "events.jsonl"),
                event_record(
                    sequence=next_event_sequence(events),
                    event_type="planning.initialized",
                    actor="deliveryctl",
                    state_revision=state["revision"],
                    details={"created": created, "preserved": preserved},
                ),
            )
    return {
        "initialized": True,
        "created": created,
        "preserved": preserved,
        "authority_paths": authority,
        "status": "draft",
        "capability_ceiling": "PLANNING",
        "implementation_authorized": False,
        "event_written": bool(created),
    }


def _validate_planning_manifest(
    root: Path,
    state: Mapping[str, Any],
    *,
    live_external_bindings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a seal and its local planning/source artifacts.

    A supplied live binding additionally proves current Git/OpenSpec adapter
    observations.  Without one, linked external Git freshness remains an
    explicit diagnostic, while all project-local content is still re-read.
    """

    path = ensure_safe_control_path(
        root,
        delivery_path(root, "planning-manifest.json"),
        label="planning manifest",
    )
    seal = read_json_no_duplicates(path, label="planning manifest")
    if not isinstance(seal, dict):
        raise DeliveryError("planning manifest must be a JSON object")
    sealed_digest = seal.get("seal_digest")
    payload = dict(seal)
    payload.pop("seal_digest", None)
    try:
        actual_digest = planning.planning_seal_digest(payload)
    except ValueError as exc:
        raise DeliveryError(f"invalid planning manifest payload: {exc}") from exc
    if not is_sha256(sealed_digest) or sealed_digest != actual_digest:
        raise DeliveryError("planning manifest seal_digest does not match its canonical payload")
    if payload.get("harness_version") != HARNESS_VERSION:
        raise DeliveryError("planning manifest harness_version is incompatible")
    if payload.get("status") != "planning_ready":
        raise DeliveryError("planning manifest status must be planning_ready")
    parse_datetime(payload.get("created_at"), "planning manifest.created_at")
    external = payload.get("external_bindings")
    if not isinstance(external, dict):
        raise DeliveryError("planning manifest must retain its sanitized external bindings")
    if live_external_bindings is not None:
        if planning.canonical_json_digest(dict(live_external_bindings)) != planning.canonical_json_digest(external):
            raise DeliveryError("current Git/OpenSpec/source bindings differ from the planning manifest")
        external = dict(live_external_bindings)
    report = planning.validate_planning_bundle(root, external, readiness="seal")
    if not report.get("valid"):
        raise DeliveryError(
            "planning manifest inputs no longer validate: "
            + ", ".join(report.get("reason_codes") or ["planning-validation-failed"])
        )
    comparisons = {
        "bundle_digest": report.get("bundle_digest"),
        "source_digest": report.get("source_digest"),
        "external_bindings_digest": report.get("external_bindings_digest"),
        "planning_artifact_digests": report.get("planning_artifact_digests"),
        "source_artifact_digests": report.get("source_artifact_digests"),
        "topological_order": report.get("topological_order"),
        "next_ready_slice_id": report.get("next_ready_slice_id"),
        "change_ids": report.get("change_ids"),
    }
    for key, current in comparisons.items():
        if payload.get(key) != current:
            raise DeliveryError(f"planning manifest {key} is stale")
    if state.get("planning_stage") == "plan_ready":
        if state.get("planning_manifest_digest") != sealed_digest:
            raise DeliveryError("state planning_manifest_digest does not match the planning manifest")
        if state.get("source_digest") != report.get("source_digest"):
            raise DeliveryError("state source_digest does not match the planning manifest")
    return {
        "valid": True,
        "status": "planning_ready",
        "seal_digest": sealed_digest,
        "bundle_digest": report["bundle_digest"],
        "source_digest": report["source_digest"],
        "external_bindings_digest": report["external_bindings_digest"],
        "next_ready_slice_id": report["next_ready_slice_id"],
        "change_ids": report["change_ids"],
        "live_bindings_checked": live_external_bindings is not None,
        "report": report,
    }


def validate_planning_now(
    root: Path,
    *,
    approved_decisions: Sequence[str],
    pending_decisions: Sequence[str],
    blocking_decisions: Sequence[str],
    expected_git_dir: str | None,
    expected_common_dir: str | None,
    git_authority_ref: str | None,
    openspec_executable: str | None,
    readiness: str,
) -> dict[str, Any]:
    collected = collect_planning_context(
        root,
        approved_decisions=approved_decisions,
        pending_decisions=pending_decisions,
        blocking_decisions=blocking_decisions,
        expected_git_dir=expected_git_dir,
        expected_common_dir=expected_common_dir,
        git_authority_ref=git_authority_ref,
        openspec_executable=openspec_executable,
        readiness=readiness,
    )
    report = planning.validate_planning_bundle(
        root,
        collected["external_bindings"],
        readiness=readiness,
    )
    state = validate_state(read_json(delivery_path(root, "state.json")))
    state_errors: list[dict[str, str]] = []
    if state.get("source_digest") != report.get("source_digest"):
        state_errors.append(
            {
                "code": "planning-state-source-mismatch",
                "path": "delivery-docs/state/state.json",
                "message": "state.source_digest must equal the registered source-set digest",
            }
        )
    if state.get("blocking_questions"):
        state_errors.append(
            {
                "code": "planning-state-blocking-questions",
                "path": "delivery-docs/state/state.json",
                "message": "state contains unresolved blocking question IDs",
            }
        )
    if state_errors:
        report = dict(report)
        report["errors"] = list(report.get("errors", [])) + state_errors
        report["reason_codes"] = sorted(
            set(report.get("reason_codes", [])) | {item["code"] for item in state_errors}
        )
        report["valid"] = False
        report["status"] = "blocked"
    return {
        "schema_version": planning.PLANNING_SCHEMA_VERSION,
        "readiness": readiness,
        "valid": bool(report.get("valid")),
        "report": report,
        "diagnostics": collected["diagnostics"],
        "external_bindings": collected["external_bindings"],
        "writes_performed": False,
        "implementation_authorized": False,
    }


def _next_change_id(root: Path, report: Mapping[str, Any], external: Mapping[str, Any]) -> str:
    changes = external.get("openspec", {}).get("changes", {})
    materialized = sorted(
        change_id
        for change_id, value in changes.items()
        if isinstance(value, dict) and value.get("state") == "materialized"
    )
    if report.get("status") == "planning_ready" and len(materialized) == 1:
        return materialized[0]
    authority = report.get("authority_paths")
    if not isinstance(authority, dict) or not isinstance(authority.get("roadmap"), str):
        raise DeliveryError("planning report does not identify its roadmap authority")
    roadmap = read_json_no_duplicates(root / authority["roadmap"], label="delivery roadmap")
    if not isinstance(roadmap, dict):
        raise DeliveryError("delivery roadmap must be a JSON object")
    next_slice = report.get("next_ready_slice_id")
    slices = roadmap.get("slices")
    if not isinstance(slices, list):
        raise DeliveryError("delivery roadmap slices are missing")
    matches = [
        item.get("change_id")
        for item in slices
        if isinstance(item, dict) and item.get("id") == next_slice
    ]
    if len(matches) != 1 or not isinstance(matches[0], str):
        raise DeliveryError("next-ready slice does not map to exactly one Change ID")
    return matches[0]


def create_planned_openspec_change(
    root: Path,
    *,
    change_id: str,
    approval_ref: str,
    expected_config_digest: str,
    approved_decisions: Sequence[str],
    pending_decisions: Sequence[str],
    blocking_decisions: Sequence[str],
    expected_git_dir: str | None,
    expected_common_dir: str | None,
    git_authority_ref: str | None,
    openspec_executable: str | None,
) -> dict[str, Any]:
    preliminary = validate_planning_now(
        root,
        approved_decisions=approved_decisions,
        pending_decisions=pending_decisions,
        blocking_decisions=blocking_decisions,
        expected_git_dir=expected_git_dir,
        expected_common_dir=expected_common_dir,
        git_authority_ref=git_authority_ref,
        openspec_executable=openspec_executable,
        readiness="change_ready",
    )
    if not preliminary["valid"]:
        raise DeliveryError(
            "OpenSpec Change creation requires a change_ready planning graph: "
            + ", ".join(preliminary["report"].get("reason_codes", []))
        )
    expected_change = _next_change_id(
        root, preliminary["report"], preliminary["external_bindings"]
    )
    if change_id != expected_change:
        raise DeliveryError(
            f"only the deterministic next-ready Change may be created: {expected_change}"
        )
    if preliminary["external_bindings"]["openspec"]["config_digest"] != expected_config_digest:
        raise DeliveryError("expected config digest differs from the change_ready binding")
    with ProjectLock(root):
        control = validate_project(root, check_fresh_gate=False)
        if not control["valid"]:
            raise DeliveryError(
                f"cannot create an OpenSpec Change from an invalid control plane: {control['errors']}"
            )
        current = validate_state(read_json(delivery_path(root, "state.json")))
        if current.get("planning_stage") == "plan_ready" or current["phase"] in PLANNING_GATED_PHASES:
            raise DeliveryError("a sealed plan must be invalidated before creating another OpenSpec Change")
        confirmed = validate_planning_now(
            root,
            approved_decisions=approved_decisions,
            pending_decisions=pending_decisions,
            blocking_decisions=blocking_decisions,
            expected_git_dir=expected_git_dir,
            expected_common_dir=expected_common_dir,
            git_authority_ref=git_authority_ref,
            openspec_executable=openspec_executable,
            readiness="change_ready",
        )
        if not confirmed["valid"]:
            raise DeliveryError("planning inputs changed while Change creation was being prepared")
        if planning.canonical_json_digest(preliminary["external_bindings"]) != planning.canonical_json_digest(
            confirmed["external_bindings"]
        ) or preliminary["report"].get("bundle_digest") != confirmed["report"].get("bundle_digest"):
            raise DeliveryError("planning inputs changed while Change creation was being prepared")
        if _next_change_id(root, confirmed["report"], confirmed["external_bindings"]) != change_id:
            raise DeliveryError("the deterministic next-ready Change changed before creation")
        if confirmed["external_bindings"]["openspec"]["config_digest"] != expected_config_digest:
            raise DeliveryError("OpenSpec config changed before Change creation")
        try:
            pin = openspec_adapter.resolve_openspec_executable(
                root, explicit=openspec_executable
            )
            result = openspec_adapter.create_new_change(
                root,
                pin,
                change_id,
                approval_ref=approval_ref,
                expected_config_digest=expected_config_digest,
            )
        except openspec_adapter.OpenSpecAdapterError as exc:
            raise _adapter_error(exc) from exc
        controlled_append_jsonl(
            root,
            delivery_path(root, "events.jsonl"),
            event_record(
                sequence=next_event_sequence(read_jsonl(delivery_path(root, "events.jsonl"))),
                event_type="openspec.change_created",
                actor="deliveryctl",
                state_revision=current["revision"],
                details={
                    "change_id": change_id,
                    "approval_ref": approval_ref,
                    "config_digest": expected_config_digest,
                    "next_action": "author-runtime-instructed-artifacts-and-mark-materialized",
                },
            ),
        )
    return {
        **result,
        "planning_status_before_create": "change_ready",
        "next_action": "author-runtime-instructed-artifacts-and-mark-materialized",
        "implementation_authorized": False,
    }


def seal_planning(
    root: Path,
    *,
    expected_revision: int,
    approval_ref: str,
    actor: str,
    approved_decisions: Sequence[str],
    pending_decisions: Sequence[str],
    blocking_decisions: Sequence[str],
    expected_git_dir: str | None,
    expected_common_dir: str | None,
    git_authority_ref: str | None,
    openspec_executable: str | None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", approval_ref or ""):
        raise DeliveryError("planning approval reference must be a durable opaque identifier")
    if not actor.strip():
        raise DeliveryError("planning seal actor must be non-empty")
    preliminary = validate_planning_now(
        root,
        approved_decisions=approved_decisions,
        pending_decisions=pending_decisions,
        blocking_decisions=blocking_decisions,
        expected_git_dir=expected_git_dir,
        expected_common_dir=expected_common_dir,
        git_authority_ref=git_authority_ref,
        openspec_executable=openspec_executable,
        readiness="seal",
    )
    if not preliminary["valid"]:
        raise DeliveryError(
            "planning seal rejected: "
            + ", ".join(preliminary["report"].get("reason_codes", []))
        )

    with ProjectLock(root):
        control = validate_project(root, check_fresh_gate=False)
        if not control["valid"]:
            raise DeliveryError(f"cannot seal an invalid control plane: {control['errors']}")
        state = validate_state(read_json(delivery_path(root, "state.json")))
        if state["revision"] != expected_revision:
            raise DeliveryError(
                f"state revision changed: expected {expected_revision}, found {state['revision']}"
            )
        if state["phase"] != "CLARIFYING":
            raise DeliveryError("planning may be sealed only from CLARIFYING")
        if state["blocking_questions"] or state["pending_decisions"]:
            raise DeliveryError("planning seal requires all state blockers and pending decisions resolved")
        confirmed = validate_planning_now(
            root,
            approved_decisions=approved_decisions,
            pending_decisions=pending_decisions,
            blocking_decisions=blocking_decisions,
            expected_git_dir=expected_git_dir,
            expected_common_dir=expected_common_dir,
            git_authority_ref=git_authority_ref,
            openspec_executable=openspec_executable,
            readiness="seal",
        )
        if not confirmed["valid"]:
            raise DeliveryError("planning inputs changed while the seal was being prepared")
        if planning.canonical_json_digest(preliminary["external_bindings"]) != planning.canonical_json_digest(
            confirmed["external_bindings"]
        ):
            raise DeliveryError("planning bindings changed between preflight and writer lock")
        report = confirmed["report"]
        if state.get("source_digest") != report.get("source_digest"):
            raise DeliveryError("state.source_digest differs from the planning source-set digest")
        active_change = _next_change_id(root, report, confirmed["external_bindings"])
        if state.get("active_change") not in {None, active_change}:
            raise DeliveryError("state.active_change differs from the materialized next-ready Change")
        payload = planning.build_planning_seal_payload(
            report,
            approval_ref=approval_ref,
            approval_revision=expected_revision,
        )
        payload.update(
            {
                "harness_version": HARNESS_VERSION,
                "status": "planning_ready",
                "created_at": isoformat(),
                "external_bindings": confirmed["external_bindings"],
            }
        )
        seal_digest = planning.planning_seal_digest(payload)
        durable_seal = {**payload, "seal_digest": seal_digest}
        controlled_atomic_write_json(
            root,
            delivery_path(root, "planning-manifest.json"),
            durable_seal,
        )
        events = read_jsonl(delivery_path(root, "events.jsonl"))
        new_state = dict(state)
        new_state.update(
            {
                "revision": state["revision"] + 1,
                "active_change": active_change,
                "planning_stage": "plan_ready",
                "planning_manifest_digest": seal_digest,
                "next_action": "review-seal-and-transition-to-ready",
                "updated_at": isoformat(),
            }
        )
        validate_state(new_state)
        event = event_record(
            sequence=next_event_sequence(events),
            event_type="state.context_updated",
            actor=actor.strip(),
            state_revision=new_state["revision"],
            details={
                "from": state["phase"],
                "to": state["phase"],
                "reason": "approved M3 planning graph sealed",
                "changed_fields": [
                    "active_change",
                    "next_action",
                    "planning_manifest_digest",
                    "planning_stage",
                ],
                "approval_ref": approval_ref,
                "planning_manifest_digest": seal_digest,
            },
            state_snapshot=new_state,
        )
        commit_state_event(root, state, new_state, event)

    post = validate_project(root, check_fresh_gate=False)
    if not post["valid"]:
        raise DeliveryError(f"planning seal post-validation failed: {post['errors']}")
    return {
        "sealed": True,
        "status": "planning_ready",
        "seal_digest": seal_digest,
        "bundle_digest": report["bundle_digest"],
        "active_change": active_change,
        "revision": new_state["revision"],
        "event_id": event["event_id"],
        "next_action": "review-seal-and-transition-to-ready",
        "implementation_authorized": False,
        "validation": post,
    }


def validate_project(root: Path, *, check_fresh_gate: bool) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    components: dict[str, Any] = {}
    manifest: dict[str, Any] | None = None
    state: dict[str, Any] | None = None
    events: list[dict[str, Any]] | None = None

    try:
        ensure_safe_control_path(root, root / DELIVERY_DIRNAME, label="delivery directory")
    except DeliveryError as exc:
        errors.append(str(exc))
        components["paths"] = {"valid": False, "error": str(exc)}
    else:
        components["paths"] = {"valid": True}

    transaction_path = delivery_path(root, "transaction.json")
    try:
        ensure_safe_control_path(root, transaction_path, label="delivery transaction journal")
        if transaction_path.exists():
            errors.append("a pending state transaction exists; run deliveryctl recover")
            components["transaction"] = {"valid": False, "pending": True}
        else:
            components["transaction"] = {"valid": True, "pending": False}
    except DeliveryError as exc:
        errors.append(str(exc))
        components["transaction"] = {"valid": False, "error": str(exc)}

    validators = [
        ("manifest", "manifest.json", validate_manifest),
        ("state", "state.json", validate_state),
        (
            "questions",
            "questions.json",
            lambda value: validate_item_catalog(value, "questions", id_prefix="Q", statuses=QUESTION_STATUSES),
        ),
        (
            "assumptions",
            "assumptions.json",
            lambda value: validate_item_catalog(value, "assumptions", id_prefix="A", statuses=ASSUMPTION_STATUSES),
        ),
    ]
    for label, filename, validator in validators:
        try:
            checked = validator(read_json(delivery_path(root, filename)))
            components[label] = {"valid": True}
            if label == "manifest":
                manifest = checked
            elif label == "state":
                state = checked
        except DeliveryError as exc:
            errors.append(str(exc))
            components[label] = {"valid": False, "error": str(exc)}

    try:
        events = read_jsonl(delivery_path(root, "events.jsonl"))
        if state is None:
            raise DeliveryError("cannot cross-check events without a valid state")
        validate_events(events, state)
        components["events"] = {"valid": True, "count": len(events)}
    except DeliveryError as exc:
        errors.append(str(exc))
        components["events"] = {"valid": False, "error": str(exc)}

    gate_path = delivery_path(root, "gate-manifest.json")
    if manifest:
        max_age = int(_manifest_policy(manifest, "gate_max_age_seconds", DEFAULT_GATE_MAX_AGE_SECONDS))
        require_artifacts = bool(_manifest_policy(manifest, "require_gate_artifacts", True))
        require_clean = bool(_manifest_policy(manifest, "require_clean_worktree", True))
    else:
        max_age = DEFAULT_GATE_MAX_AGE_SECONDS
        require_artifacts = True
        require_clean = True
    try:
        gate_value = read_json(gate_path)
        should_be_passed = bool(state and state.get("phase") in QUALITY_GATED_PHASES)
        if should_be_passed and (
            not QUALITY_TRANSITIONS_IMPLEMENTED
            or not bool(_manifest_policy(manifest, "quality_transitions_enabled", False))
        ):
            errors.append(
                "quality-gated state is unavailable in this harness milestone"
            )
        gate_check = check_gate_data(
            gate_value,
            root,
            require_passed=should_be_passed,
            require_fresh=check_fresh_gate and should_be_passed,
            require_artifacts=should_be_passed,
            require_clean=require_clean and should_be_passed,
            max_age_seconds=max_age,
            expected_change_id=state.get("active_change") if should_be_passed and state else None,
            expected_source_digest=state.get("source_digest") if should_be_passed and state else None,
        )
        components["gate"] = gate_check
        if not gate_check["valid"]:
            errors.extend(gate_check["errors"])
        warnings.extend(gate_check["warnings"])
        if should_be_passed and state:
            if not isinstance(state.get("active_change"), str) or not state["active_change"].strip():
                errors.append("a quality-gated state requires state.active_change")
            if not is_sha256(state.get("source_digest")):
                errors.append("a quality-gated state requires a 64-hex state.source_digest")
            if state.get("last_verified_commit") != gate_value.get("commit"):
                errors.append("state.last_verified_commit does not match gate.commit")
            if state.get("last_gate_manifest") not in {
                DELIVERY_DIRNAME + "/gate-manifest.json",
                # Legacy layout kept for backward compatibility.
                LEGACY_DELIVERY_DIRNAME + "/gate-manifest.json",
            }:
                errors.append(
                    "state.last_gate_manifest must reference " + DELIVERY_DIRNAME + "/gate-manifest.json"
                )
    except DeliveryError as exc:
        errors.append(str(exc))
        components["gate"] = {"valid": False, "error": str(exc)}

    planning_path = delivery_path(root, "planning-manifest.json")
    planning_required = bool(state and state.get("phase") in PLANNING_GATED_PHASES)
    planning_present = planning_path.exists() or planning_path.is_symlink()
    if planning_required or bool(state and state.get("planning_stage") == "plan_ready"):
        try:
            if state is None:
                raise DeliveryError("cannot validate planning manifest without a valid state")
            planning_check = _validate_planning_manifest(root, state)
            components["planning"] = planning_check
        except DeliveryError as exc:
            errors.append(str(exc))
            components["planning"] = {"valid": False, "error": str(exc)}
    else:
        components["planning"] = {
            "valid": True,
            "status": state.get("planning_stage") if state else None,
            "present": planning_present,
        }
        if planning_present and state and state.get("planning_stage") == "stale":
            warnings.append("a stale planning manifest is retained for audit and must be resealed")

    if manifest and state and manifest["project_mode"] != state["project_mode"]:
        errors.append("manifest.project_mode and state.project_mode disagree")

    if state and not phase_is_implemented(state["phase"]):
        minimum = PHASE_MINIMUM_MILESTONE[state["phase"]]
        errors.append(
            f"state phase {state['phase']} requires milestone M{minimum}; "
            f"this harness implements through M{IMPLEMENTED_MILESTONE}"
        )

    if state and state["phase"] in PLANNING_GATED_PHASES:
        if state["blocking_questions"] or state["pending_decisions"]:
            errors.append(
                f"state phase {state['phase']} cannot contain blocking questions or pending decisions"
            )
        if state["phase"] == "PLANNING" and state.get("next_action") != "await-m4-before-apply":
            errors.append(
                "M3 phase PLANNING requires next_action='await-m4-before-apply'"
            )

    if state:
        try:
            questions = read_json(delivery_path(root, "questions.json"))["items"]
            open_question_ids = sorted(item["id"] for item in questions if item["status"] == "open")
            if sorted(state["blocking_questions"]) != open_question_ids:
                warnings.append(
                    "state.blocking_questions does not match all open questions; only truly blocking questions "
                    "should be referenced, so review this intentionally"
                )
        except (DeliveryError, KeyError, TypeError):
            pass

    return {
        "schema_version": SCHEMA_VERSION,
        "validated_at": isoformat(),
        "project_root": str(root),
        "valid": not errors,
        "phase": state.get("phase") if state else None,
        "revision": state.get("revision") if state else None,
        "components": components,
        "errors": list(dict.fromkeys(errors)),
        "warnings": list(dict.fromkeys(warnings)),
    }


def allowed_targets(state: Mapping[str, Any]) -> set[str]:
    current = state["phase"]
    if current in INTERRUPT_PHASES:
        resume = state.get("resume_phase")
        targets = {"ABORTED"}
        if isinstance(resume, str):
            targets.add(resume)
        if current == "WAITING_USER":
            targets.add("CLARIFYING")
        if current == "BLOCKED":
            targets.add("REWORK")
        return targets
    targets = set(ALLOWED_TRANSITIONS.get(current, set()))
    if current not in {"CLOSED", "ABORTED"}:
        targets |= INTERRUPT_PHASES | {"ABORTED"}
    return targets


def _transaction_journal(
    previous_state: Mapping[str, Any],
    next_state: Mapping[str, Any],
    event: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "harness_version": HARNESS_VERSION,
        "transaction_id": event["event_id"],
        "created_at": isoformat(),
        "previous_state_digest": canonical_sha256(previous_state),
        "next_state_digest": canonical_sha256(next_state),
        "previous_state": dict(previous_state),
        "next_state": dict(next_state),
        "event": dict(event),
    }


def validate_transaction_journal(value: Any) -> dict[str, Any]:
    journal = ensure_object(value, "transaction journal")
    validate_versioned(journal, "transaction journal", harness=True)
    require_keys(
        journal,
        {
            "transaction_id",
            "created_at",
            "previous_state_digest",
            "next_state_digest",
            "previous_state",
            "next_state",
            "event",
        },
        "transaction journal",
    )
    parse_datetime(journal["created_at"], "transaction journal.created_at")
    previous = validate_state(journal["previous_state"])
    next_state = validate_state(journal["next_state"])
    if next_state["revision"] != previous["revision"] + 1:
        raise DeliveryError("transaction journal state revisions are not continuous")
    if journal["previous_state_digest"] != canonical_sha256(previous):
        raise DeliveryError("transaction journal previous state digest is invalid")
    if journal["next_state_digest"] != canonical_sha256(next_state):
        raise DeliveryError("transaction journal next state digest is invalid")
    event = ensure_object(journal["event"], "transaction journal.event")
    validate_versioned(event, "transaction journal.event", harness=True)
    if journal["transaction_id"] != event.get("event_id"):
        raise DeliveryError("transaction journal ID does not match its event")
    if event.get("event_type") not in STATE_EVENT_TYPES - {"delivery.initialized"}:
        raise DeliveryError("transaction journal must contain a state mutation event")
    if event.get("state_revision") != next_state["revision"]:
        raise DeliveryError("transaction event revision does not match next state")
    if event.get("state_digest") != canonical_sha256(next_state):
        raise DeliveryError("transaction event digest does not match next state")
    if event.get("state_snapshot") != next_state:
        raise DeliveryError("transaction event snapshot does not match next state")
    details = ensure_object(event.get("details"), "transaction journal.event.details")
    if details.get("from") != previous["phase"] or details.get("to") != next_state["phase"]:
        raise DeliveryError("transaction event from/to does not match journal states")
    if event["event_type"] == "state.context_updated" and previous["phase"] != next_state["phase"]:
        raise DeliveryError("transaction context update must not change phase")
    if event["event_type"] == "state.transitioned":
        if previous["phase"] == next_state["phase"]:
            raise DeliveryError("transaction state transition must change phase")
        if next_state["phase"] not in allowed_targets(previous):
            raise DeliveryError(
                f"transaction contains an illegal transition {previous['phase']} -> {next_state['phase']}"
            )
    return journal


def commit_state_event(
    root: Path,
    previous_state: Mapping[str, Any],
    next_state: Mapping[str, Any],
    event: Mapping[str, Any],
) -> None:
    """Commit a state snapshot and event through a recoverable write-ahead journal."""

    journal_path = delivery_path(root, "transaction.json")
    state_path = delivery_path(root, "state.json")
    events_path = delivery_path(root, "events.jsonl")
    for path in (journal_path, state_path, events_path):
        ensure_safe_control_path(root, path, label="state transaction path")
    journal = validate_transaction_journal(_transaction_journal(previous_state, next_state, event))
    try:
        controlled_atomic_write_json(root, journal_path, journal)
        controlled_atomic_write_json(root, state_path, next_state)
        controlled_append_jsonl(root, events_path, event)
        controlled_unlink(root, journal_path)
    except OSError as exc:
        raise DeliveryError(
            "state transaction was interrupted and left fail-closed; run deliveryctl recover"
        ) from exc


def _read_events_for_recovery(root: Path, expected_event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read an event log, repairing only a provably partial final journal event."""

    path = ensure_safe_control_path(root, delivery_path(root, "events.jsonl"), label="event log")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DeliveryError(f"cannot read {path}: {exc}") from exc
    events: list[dict[str, Any]] = []
    offset = 0
    for line_number, line in enumerate(raw.splitlines(keepends=True), start=1):
        complete = line.endswith((b"\n", b"\r"))
        try:
            decoded = line.decode("utf-8").strip()
            value = json.loads(decoded) if decoded else None
            if not isinstance(value, dict):
                raise ValueError("event is not an object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            if offset + len(line) != len(raw):
                raise DeliveryError(
                    f"event log corruption is not confined to the final record at line {line_number}"
                ) from exc
            expected_prefix = json.dumps(
                dict(expected_event),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            fragment = line.strip()
            if not fragment or not expected_prefix.startswith(fragment):
                raise DeliveryError(
                    "the final event fragment does not match the pending transaction; refusing recovery"
                ) from exc
            controlled_atomic_write_text(root, path, raw[:offset].decode("utf-8"))
            return events
        events.append(value)
        offset += len(line)
        if not complete and offset == len(raw):
            controlled_atomic_write_text(root, path, raw.decode("utf-8") + "\n")
    return events


def recover_project(
    root: Path,
    *,
    expected_git_dir: str | None = None,
    expected_common_dir: str | None = None,
    git_authority_ref: str | None = None,
    openspec_executable: str | None = None,
) -> dict[str, Any]:
    """Deterministically finish or clear a journaled state transaction."""

    journal_path = delivery_path(root, "transaction.json")
    ensure_safe_control_path(root, journal_path, label="delivery transaction journal")
    with ProjectLock(root):
        if not journal_path.exists():
            validation = validate_project(root, check_fresh_gate=False)
            return {"recovered": False, "reason": "no pending transaction", "validation": validation}
        journal = validate_transaction_journal(read_json(journal_path))
        previous = journal["previous_state"]
        next_state = journal["next_state"]
        expected_event = journal["event"]
        # Recovery may perform several writes, including repair of a partial
        # final event record.  Reject future-milestone state before any of them.
        for label, candidate in (("previous", previous), ("next", next_state)):
            phases = [candidate["phase"]]
            if candidate.get("resume_phase"):
                phases.append(candidate["resume_phase"])
            unsupported = [phase for phase in phases if not phase_is_implemented(phase)]
            if unsupported:
                raise DeliveryError(
                    f"transaction {label} state requires an unimplemented phase "
                    f"{unsupported[0]}; this harness implements through M{IMPLEMENTED_MILESTONE}"
                )
        state = validate_state(read_json(delivery_path(root, "state.json")))
        planning_check: dict[str, Any] | None = None
        if next_state["phase"] in PLANNING_GATED_PHASES:
            live = collect_live_bindings_for_sealed_plan(
                root,
                expected_git_dir=expected_git_dir,
                expected_common_dir=expected_common_dir,
                git_authority_ref=git_authority_ref,
                openspec_executable=openspec_executable,
            )
            planning_check = _validate_planning_manifest(
                root,
                next_state,
                live_external_bindings=live["external_bindings"],
            )
        events = _read_events_for_recovery(root, expected_event)
        matching = [event for event in events if event.get("event_id") == expected_event["event_id"]]
        if matching and matching[0] != expected_event:
            raise DeliveryError("pending transaction event ID exists with different content")
        if len(matching) > 1:
            raise DeliveryError("pending transaction event is duplicated")
        state_digest = canonical_sha256(state)
        if state_digest not in {
            journal["previous_state_digest"],
            journal["next_state_digest"],
        }:
            raise DeliveryError(
                "current state matches neither side of the pending transaction; refusing recovery"
            )
        event_present = bool(matching)
        if state_digest == journal["previous_state_digest"]:
            controlled_atomic_write_json(root, delivery_path(root, "state.json"), next_state)
        if not event_present:
            controlled_append_jsonl(root, delivery_path(root, "events.jsonl"), expected_event)
        controlled_unlink(root, journal_path)

    validation = validate_project(
        root,
        check_fresh_gate=next_state["phase"] in QUALITY_GATED_PHASES,
    )
    if not validation["valid"]:
        raise DeliveryError(f"transaction recovery completed but validation failed: {validation['errors']}")
    return {
        "recovered": True,
        "transaction_id": journal["transaction_id"],
        "phase": next_state["phase"],
        "revision": next_state["revision"],
        "planning": planning_check,
        "validation": validation,
    }


def transition_state(
    root: Path,
    *,
    target: str,
    reason: str,
    actor: str,
    expected_revision: int | None,
    next_action: str | None,
    milestone: str | None,
    change: str | None,
    approval_ref: str | None,
    planning_external_bindings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    target = re.sub(r"[-\s]+", "_", target.strip()).upper()
    if target not in ALL_PHASES:
        raise DeliveryError(f"unknown target phase {target!r}; choose one of {sorted(ALL_PHASES)}")
    if not phase_is_implemented(target):
        minimum = PHASE_MINIMUM_MILESTONE[target]
        raise DeliveryError(
            f"transition to {target} is not implemented before milestone M{minimum}; "
            f"this harness implements through M{IMPLEMENTED_MILESTONE}"
        )
    if not reason.strip():
        raise DeliveryError("transition reason must be non-empty")
    if not actor.strip():
        raise DeliveryError("transition actor must be non-empty")

    with ProjectLock(root):
        validation = validate_project(root, check_fresh_gate=False)
        if not validation["valid"]:
            raise DeliveryError(f"project control plane is invalid: {validation['errors']}")
        manifest = validate_manifest(read_json(delivery_path(root, "manifest.json")))
        state = validate_state(read_json(delivery_path(root, "state.json")))
        events = read_jsonl(delivery_path(root, "events.jsonl"))
        if expected_revision is not None and state["revision"] != expected_revision:
            raise DeliveryError(
                f"state revision changed: expected {expected_revision}, found {state['revision']}; re-read state"
            )
        if target == state["phase"]:
            raise DeliveryError(f"state is already in phase {target}; no transition was written")
        targets = allowed_targets(state)
        if target not in targets:
            raise DeliveryError(
                f"transition {state['phase']} -> {target} is not allowed; allowed targets: {sorted(targets)}"
            )
        if target in HUMAN_APPROVAL_PHASES and not approval_ref:
            raise DeliveryError(f"transition to {target} requires --approval-ref")

        previous = state["phase"]
        new_state = dict(state)
        new_state["revision"] = state["revision"] + 1
        new_state["phase"] = target
        new_state["updated_at"] = isoformat()
        if next_action is not None:
            new_state["next_action"] = next_action or None
        if milestone is not None:
            new_state["active_milestone"] = milestone or None
        if change is not None:
            new_state["active_change"] = change or None
        if target in INTERRUPT_PHASES:
            new_state["resume_phase"] = previous
        elif previous in INTERRUPT_PHASES:
            new_state["resume_phase"] = None
        if target == "CLARIFYING" and state.get("planning_stage") == "plan_ready":
            new_state["planning_stage"] = "stale"
            new_state["planning_manifest_digest"] = None
        validate_state(new_state)

        # Context completeness is an invariant of READY and every later delivery
        # phase.  Keep it independent from the M3 planning-seal feature flag so
        # future-milestone tests (and any temporary policy override) cannot
        # accidentally make READY accept unresolved questions or decisions.
        if target in PLANNING_GATED_PHASES | {"READY", "EXECUTING"} | QUALITY_GATED_PHASES:
            if new_state["blocking_questions"] or new_state["pending_decisions"]:
                raise DeliveryError(
                    f"transition to {target} requires all blocking questions and pending decisions to be resolved"
                )
        if target == "READY" and not is_sha256(new_state.get("source_digest")):
            raise DeliveryError("transition to READY requires a 64-hex state.source_digest")
        planning_check: dict[str, Any] | None = None
        if target in PLANNING_GATED_PHASES:
            if planning_external_bindings is None:
                raise DeliveryError(
                    f"transition to {target} requires current live planning bindings"
                )
            planning_check = _validate_planning_manifest(
                root,
                new_state,
                live_external_bindings=planning_external_bindings,
            )
            if target == "PLANNING":
                new_state["next_action"] = "await-m4-before-apply"
                validate_state(new_state)
        if target == "EXECUTING" and not new_state.get("active_change"):
            raise DeliveryError("transition to EXECUTING requires state.active_change")

        if target in QUALITY_GATED_PHASES:
            if not QUALITY_TRANSITIONS_IMPLEMENTED:
                raise DeliveryError(
                    f"transition to {target} is not implemented by harness {HARNESS_VERSION}"
                )
            if not bool(_manifest_policy(manifest, "quality_transitions_enabled", False)):
                raise DeliveryError(
                    f"transition to {target} is disabled by manifest.policies.quality_transitions_enabled"
                )

        gate_check: dict[str, Any] | None = None
        gate_required = target in QUALITY_GATED_PHASES or (
            target == "CLOSED" and previous != "ABORTED"
        )
        if gate_required:
            if not new_state.get("active_change"):
                raise DeliveryError(f"transition to {target} requires state.active_change")
            if not is_sha256(new_state.get("source_digest")):
                raise DeliveryError(
                    f"transition to {target} requires a 64-hex state.source_digest"
                )
            gate_value = read_json(delivery_path(root, "gate-manifest.json"))
            gate_check = check_gate_data(
                gate_value,
                root,
                require_passed=True,
                require_fresh=True,
                require_artifacts=True,
                require_clean=bool(_manifest_policy(manifest, "require_clean_worktree", True)),
                max_age_seconds=int(
                    _manifest_policy(manifest, "gate_max_age_seconds", DEFAULT_GATE_MAX_AGE_SECONDS)
                ),
                expected_change_id=new_state["active_change"],
                expected_source_digest=new_state["source_digest"],
            )
            if not gate_check["passed"]:
                raise DeliveryError(f"quality gate rejected transition to {target}: {gate_check['errors']}")
        else:
            gate_value = None

        if gate_value is not None:
            new_state["last_gate_manifest"] = DELIVERY_DIRNAME + "/gate-manifest.json"
            new_state["last_verified_commit"] = gate_value["commit"]

        details = {
            "from": previous,
            "to": target,
            "reason": reason.strip(),
            "active_milestone": new_state.get("active_milestone"),
            "active_change": new_state.get("active_change"),
            "next_action": new_state.get("next_action"),
            "approval_ref": approval_ref,
            "planning": planning_check,
            "gate": gate_check,
        }
        event = event_record(
            sequence=next_event_sequence(events),
            event_type="state.transitioned",
            actor=actor.strip(),
            state_revision=new_state["revision"],
            details=details,
            state_snapshot=new_state,
        )

        # The state file is the current snapshot and is atomically replaced.
        # The append-only event is immediately fsynced while the writer lock is
        # held.  Validation cross-checks revisions to expose any interrupted write.
        commit_state_event(root, state, new_state, event)

    post_validation = validate_project(root, check_fresh_gate=gate_required)
    if not post_validation["valid"]:
        raise DeliveryError(f"transition was written but post-validation failed: {post_validation['errors']}")
    return {
        "transitioned": True,
        "from": previous,
        "to": target,
        "revision": new_state["revision"],
        "event_id": event["event_id"],
        "state": new_state,
        "validation": post_validation,
    }


def set_context(
    root: Path,
    *,
    expected_revision: int,
    actor: str,
    reason: str,
    source_digest: Any = UNSET,
    baseline_commit: Any = UNSET,
    active_milestone: Any = UNSET,
    active_change: Any = UNSET,
    next_action: Any = UNSET,
    blocking_questions: Any = UNSET,
    pending_decisions: Any = UNSET,
) -> dict[str, Any]:
    """Event and atomically persist delivery context without changing phase."""

    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
        raise DeliveryError("set-context requires a non-negative expected revision")
    if not actor.strip():
        raise DeliveryError("set-context actor must be non-empty")
    if not reason.strip():
        raise DeliveryError("set-context reason must be non-empty")
    updates = {
        "source_digest": source_digest,
        "baseline_commit": baseline_commit,
        "active_milestone": active_milestone,
        "active_change": active_change,
        "next_action": next_action,
        "blocking_questions": blocking_questions,
        "pending_decisions": pending_decisions,
    }
    supplied = {key: value for key, value in updates.items() if value is not UNSET}
    if not supplied:
        raise DeliveryError("set-context requires at least one field update")
    for key in ("blocking_questions", "pending_decisions"):
        if key in supplied:
            value = supplied[key]
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise DeliveryError(f"set-context {key} must be an array of non-empty string IDs")
            if len(set(value)) != len(value):
                raise DeliveryError(f"set-context {key} must not contain duplicate IDs")

    with ProjectLock(root):
        validation = validate_project(root, check_fresh_gate=False)
        if not validation["valid"]:
            raise DeliveryError(f"project control plane is invalid: {validation['errors']}")
        state = validate_state(read_json(delivery_path(root, "state.json")))
        if state["revision"] != expected_revision:
            raise DeliveryError(
                f"state revision changed: expected {expected_revision}, found {state['revision']}; re-read state"
            )
        events = read_jsonl(delivery_path(root, "events.jsonl"))
        new_state = dict(state)
        for key, value in supplied.items():
            if key in {"blocking_questions", "pending_decisions"}:
                new_state[key] = list(value)
            elif isinstance(value, str):
                new_state[key] = value.strip() or None
            else:
                new_state[key] = value
        changed_fields = sorted(key for key in supplied if new_state[key] != state[key])
        if not changed_fields:
            raise DeliveryError("set-context did not change any field")
        planning_inputs = {
            "source_digest",
            "baseline_commit",
            "active_change",
            "blocking_questions",
            "pending_decisions",
        }
        changed_planning_inputs = planning_inputs.intersection(changed_fields)
        if state["phase"] in PLANNING_GATED_PHASES and changed_planning_inputs:
            raise DeliveryError(
                f"phase {state['phase']} planning context is sealed; "
                "transition to CLARIFYING before changing it"
            )
        if state.get("planning_stage") == "plan_ready" and changed_planning_inputs:
            new_state["planning_stage"] = "stale"
            new_state["planning_manifest_digest"] = None
            changed_fields = sorted(
                set(changed_fields) | {"planning_manifest_digest", "planning_stage"}
            )
        new_state["revision"] = state["revision"] + 1
        new_state["updated_at"] = isoformat()
        validate_state(new_state)

        phase = state["phase"]
        if phase in PLANNING_GATED_PHASES | {"EXECUTING"} | QUALITY_GATED_PHASES:
            if new_state["blocking_questions"] or new_state["pending_decisions"]:
                raise DeliveryError(
                    f"phase {phase} cannot contain blocking questions or pending decisions"
                )
        if phase == "READY" and not is_sha256(new_state.get("source_digest")):
            raise DeliveryError("phase READY requires a 64-hex state.source_digest")
        if phase == "EXECUTING" and not new_state.get("active_change"):
            raise DeliveryError("phase EXECUTING requires state.active_change")
        if phase == "PLANNING" and new_state.get("next_action") != "await-m4-before-apply":
            raise DeliveryError("phase PLANNING next_action is locked until M4")

        if phase in QUALITY_GATED_PHASES:
            if not new_state.get("active_change") or not is_sha256(new_state.get("source_digest")):
                raise DeliveryError(f"phase {phase} requires active change and source digest")
            manifest = validate_manifest(read_json(delivery_path(root, "manifest.json")))
            gate_check = check_gate_data(
                read_json(delivery_path(root, "gate-manifest.json")),
                root,
                require_passed=True,
                require_fresh=True,
                require_artifacts=True,
                require_clean=bool(_manifest_policy(manifest, "require_clean_worktree", True)),
                max_age_seconds=int(
                    _manifest_policy(manifest, "gate_max_age_seconds", DEFAULT_GATE_MAX_AGE_SECONDS)
                ),
                expected_change_id=new_state["active_change"],
                expected_source_digest=new_state["source_digest"],
            )
            if not gate_check["passed"]:
                raise DeliveryError(f"context update would stale the quality gate: {gate_check['errors']}")

        event = event_record(
            sequence=next_event_sequence(events),
            event_type="state.context_updated",
            actor=actor.strip(),
            state_revision=new_state["revision"],
            details={
                "from": phase,
                "to": phase,
                "reason": reason.strip(),
                "changed_fields": changed_fields,
            },
            state_snapshot=new_state,
        )
        commit_state_event(root, state, new_state, event)

    post_validation = validate_project(root, check_fresh_gate=phase in QUALITY_GATED_PHASES)
    if not post_validation["valid"]:
        raise DeliveryError(f"context update was written but post-validation failed: {post_validation['errors']}")
    return {
        "updated": True,
        "phase": phase,
        "revision": new_state["revision"],
        "changed_fields": changed_fields,
        "event_id": event["event_id"],
        "state": new_state,
        "validation": post_validation,
    }


def invalidate_planning(
    root: Path,
    *,
    expected_revision: int,
    reason: str,
    actor: str,
) -> dict[str, Any]:
    """Event a deliberate return from an approved plan to editable planning."""

    if not reason.strip() or not actor.strip():
        raise DeliveryError("planning invalidation requires non-empty reason and actor")
    with ProjectLock(root):
        validation = validate_project(root, check_fresh_gate=False)
        if not validation["valid"]:
            raise DeliveryError(f"cannot invalidate an invalid control plane: {validation['errors']}")
        state = validate_state(read_json(delivery_path(root, "state.json")))
        if state["revision"] != expected_revision:
            raise DeliveryError(
                f"state revision changed: expected {expected_revision}, found {state['revision']}"
            )
        if state["phase"] in PLANNING_GATED_PHASES:
            raise DeliveryError("transition to CLARIFYING before invalidating a sealed plan")
        if state.get("planning_stage") != "plan_ready":
            raise DeliveryError("there is no current plan_ready state to invalidate")
        events = read_jsonl(delivery_path(root, "events.jsonl"))
        new_state = dict(state)
        new_state.update(
            {
                "revision": state["revision"] + 1,
                "planning_stage": "stale",
                "planning_manifest_digest": None,
                "next_action": "revise-and-reseal-plan",
                "updated_at": isoformat(),
            }
        )
        validate_state(new_state)
        event = event_record(
            sequence=next_event_sequence(events),
            event_type="state.context_updated",
            actor=actor.strip(),
            state_revision=new_state["revision"],
            details={
                "from": state["phase"],
                "to": state["phase"],
                "reason": reason.strip(),
                "changed_fields": [
                    "next_action",
                    "planning_manifest_digest",
                    "planning_stage",
                ],
            },
            state_snapshot=new_state,
        )
        commit_state_event(root, state, new_state, event)
    post = validate_project(root, check_fresh_gate=False)
    if not post["valid"]:
        raise DeliveryError(f"planning invalidation post-validation failed: {post['errors']}")
    return {
        "invalidated": True,
        "revision": new_state["revision"],
        "planning_stage": "stale",
        "event_id": event["event_id"],
        "validation": post,
    }


def check_gate_file(root: Path, gate_path: Path | None, *, allow_dirty: bool, max_age: int | None) -> dict[str, Any]:
    manifest = validate_manifest(read_json(delivery_path(root, "manifest.json")))
    state = validate_state(read_json(delivery_path(root, "state.json")))
    path = gate_path or delivery_path(root, "gate-manifest.json")
    if not path.is_absolute():
        path = (root / path).resolve()
    return check_gate_data(
        read_json(path),
        root,
        require_passed=True,
        require_fresh=True,
        require_artifacts=True,
        require_clean=False if allow_dirty else bool(_manifest_policy(manifest, "require_clean_worktree", True)),
        max_age_seconds=max_age
        if max_age is not None
        else int(_manifest_policy(manifest, "gate_max_age_seconds", DEFAULT_GATE_MAX_AGE_SECONDS)),
        expected_change_id=state.get("active_change"),
        expected_source_digest=state.get("source_digest"),
    )


def _adapter_error(exc: Exception) -> DeliveryError:
    if isinstance(exc, openspec_adapter.OpenSpecAdapterError):
        return DeliveryError(f"{exc.code}: {exc.message}")
    if isinstance(exc, git_scope.GitScopeError):
        return DeliveryError(f"{exc.code}: {exc.detail}")
    return DeliveryError(str(exc))


def _read_candidate_file(root: Path, raw_path: str) -> bytes:
    path = Path(raw_path)
    if not path.is_absolute():
        path = root / path
    path = ensure_safe_control_path(root, path, label="OpenSpec config candidate")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DeliveryError(f"cannot inspect OpenSpec config candidate: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DeliveryError("OpenSpec config candidate must be a regular project-local file")
    if metadata.st_size > 1_048_576:
        raise DeliveryError("OpenSpec config candidate exceeds the 1 MiB review limit")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise DeliveryError(f"cannot read OpenSpec config candidate: {exc}") from exc


def plan_openspec_config(root: Path, *, candidate_path: str | None) -> dict[str, Any]:
    candidate = _read_candidate_file(root, candidate_path) if candidate_path else None
    try:
        return openspec_adapter.plan_config_candidate(root, candidate)
    except openspec_adapter.OpenSpecAdapterError as exc:
        raise _adapter_error(exc) from exc


def apply_openspec_config(
    root: Path,
    *,
    candidate_path: str,
    candidate_digest: str,
    expected_config_digest: str,
    approval_ref: str,
    allow_existing_replacement: bool,
) -> dict[str, Any]:
    candidate = _read_candidate_file(root, candidate_path)
    with ProjectLock(root):
        control = validate_project(root, check_fresh_gate=False)
        if not control["valid"]:
            raise DeliveryError(
                f"cannot apply OpenSpec config from an invalid control plane: {control['errors']}"
            )
        state = validate_state(read_json(delivery_path(root, "state.json")))
        if state.get("planning_stage") == "plan_ready" or state["phase"] in PLANNING_GATED_PHASES:
            raise DeliveryError(
                "OpenSpec config is bound by a current planning seal; return to CLARIFYING "
                "and invalidate the plan before applying new config bytes"
            )
        try:
            result = openspec_adapter.apply_config_candidate(
                root,
                candidate,
                candidate_digest=candidate_digest,
                expected_config_digest=expected_config_digest,
                approval_ref=approval_ref,
                allow_existing_replacement=allow_existing_replacement,
            )
        except openspec_adapter.OpenSpecAdapterError as exc:
            raise _adapter_error(exc) from exc
        controlled_append_jsonl(
            root,
            delivery_path(root, "events.jsonl"),
            event_record(
                sequence=next_event_sequence(read_jsonl(delivery_path(root, "events.jsonl"))),
                event_type="openspec.config_applied",
                actor="deliveryctl",
                state_revision=state["revision"],
                details={
                    "action": result.get("action"),
                    "config_digest": result.get("config_digest"),
                    "approval_ref": approval_ref,
                },
            ),
        )
    return result


def read_openspec(
    root: Path,
    *,
    operation: str,
    change_id: str | None,
    artifact_id: str | None,
    executable: str | None,
) -> dict[str, Any]:
    try:
        pin = openspec_adapter.resolve_openspec_executable(root, explicit=executable)
        return openspec_adapter.run_openspec(
            root,
            pin,
            operation,
            change_id=change_id,
            artifact_id=artifact_id,
        )
    except openspec_adapter.OpenSpecAdapterError as exc:
        raise _adapter_error(exc) from exc


def _open_items(path: Path) -> list[dict[str, Any]]:
    value = read_json(path)
    items = value.get("items", []) if isinstance(value, dict) else []
    return [item for item in items if isinstance(item, dict) and item.get("status") in {"open", "active"}]


def build_report(
    root: Path,
    *,
    run_id: str | None,
    kind: str,
    summary: str,
    actor: str,
    planning_external_bindings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if kind not in REPORT_KINDS:
        raise DeliveryError(f"unknown report kind {kind!r}; choose one of {sorted(REPORT_KINDS)}")
    validation = validate_project(root, check_fresh_gate=False)
    if not validation["valid"]:
        raise DeliveryError(f"cannot report from an invalid control plane: {validation['errors']}")
    state = validate_state(read_json(delivery_path(root, "state.json")))
    manifest = validate_manifest(read_json(delivery_path(root, "manifest.json")))
    events = read_jsonl(delivery_path(root, "events.jsonl"))
    questions = _open_items(delivery_path(root, "questions.json"))
    assumptions = _open_items(delivery_path(root, "assumptions.json"))
    gate_value = read_json(delivery_path(root, "gate-manifest.json"))
    gated_report = kind in REPORT_PHASES_REQUIRING_GATE
    allowed_report_phases = REPORT_PHASES_REQUIRING_GATE.get(kind)
    if allowed_report_phases is not None and state["phase"] not in allowed_report_phases:
        raise DeliveryError(
            f"report kind {kind!r} requires phase in {sorted(allowed_report_phases)}; "
            f"found {state['phase']}"
        )
    raw_gate_passed = isinstance(gate_value, dict) and gate_value.get("status") == "passed"
    gate = check_gate_data(
        gate_value,
        root,
        require_passed=gated_report,
        require_fresh=gated_report or raw_gate_passed,
        require_artifacts=gated_report or raw_gate_passed,
        require_clean=bool(_manifest_policy(manifest, "require_clean_worktree", True)),
        max_age_seconds=int(_manifest_policy(manifest, "gate_max_age_seconds", DEFAULT_GATE_MAX_AGE_SECONDS)),
        expected_change_id=state.get("active_change") if gated_report or raw_gate_passed else None,
        expected_source_digest=state.get("source_digest") if gated_report or raw_gate_passed else None,
    )
    if gated_report and not gate["passed"]:
        raise DeliveryError(f"report kind {kind!r} requires a fresh passed gate: {gate['errors']}")
    planning_report = kind in REPORT_PHASES_REQUIRING_PLANNING
    planning_result: dict[str, Any] | None = None
    if planning_report:
        allowed_planning_phases = REPORT_PHASES_REQUIRING_PLANNING[kind]
        if state["phase"] not in allowed_planning_phases:
            raise DeliveryError(
                f"report kind {kind!r} requires phase in {sorted(allowed_planning_phases)}; "
                f"found {state['phase']}"
            )
        if planning_external_bindings is None:
            raise DeliveryError(
                f"report kind {kind!r} requires current live Git/OpenSpec/source bindings"
            )
        planning_result = _validate_planning_manifest(
            root,
            state,
            live_external_bindings=planning_external_bindings,
        )
    report_gate = dict(gate)
    report_gate["manifest_status"] = gate.get("status")
    report_gate["status"] = gate.get("effective_status") or "invalid"
    if planning_report:
        seal = read_json_no_duplicates(
            delivery_path(root, "planning-manifest.json"), label="planning manifest"
        )
        external = seal.get("external_bindings") if isinstance(seal, dict) else None
        git = dict(external.get("git", {})) if isinstance(external, dict) else {}
    else:
        git = git_info(root)
    if run_id is None:
        stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        run_id = f"RUN-{stamp}-{uuid.uuid4().hex[:6].upper()}"
    if not re.fullmatch(r"RUN-[A-Za-z0-9._-]+", run_id):
        raise DeliveryError("run ID must start with RUN- and contain only letters, numbers, dot, underscore or dash")

    generated_at = isoformat()
    result = {
        "schema_version": SCHEMA_VERSION,
        "harness_version": HARNESS_VERSION,
        "run_id": run_id,
        "kind": kind,
        "generated_at": generated_at,
        "project_id": manifest["project_id"],
        "project_mode": state["project_mode"],
        "phase": state["phase"],
        "state_revision": state["revision"],
        "active_milestone": state["active_milestone"],
        "active_change": state["active_change"],
        "summary": summary.strip() or "No narrative summary was provided.",
        "next_action": state["next_action"],
        "git": git,
        "planning": planning_result,
        "gate": report_gate,
        "open_questions": questions,
        "active_assumptions": assumptions,
        "recent_events": events[-10:],
    }
    result_path = delivery_path(root, f"runs/{run_id}/result.json")
    markdown_path = root / "delivery-docs" / "verification" / "runs" / run_id / "report.md"
    ensure_safe_control_path(root, result_path, label="run result")
    ensure_safe_control_path(root, markdown_path, label="verification report")
    ensure_safe_control_path(
        root,
        delivery_path(root, "events.jsonl"),
        label="event log",
    )
    if result_path.exists() or markdown_path.exists():
        raise DeliveryError(
            f"refusing to overwrite existing report for {run_id}: {result_path} or {markdown_path}"
        )

    question_lines = "\n".join(
        f"- `{item.get('id')}` — {item.get('summary')}" for item in questions
    ) or "- None"
    assumption_lines = "\n".join(
        f"- `{item.get('id')}` — {item.get('summary')}" for item in assumptions
    ) or "- None"
    gate_errors = gate.get("errors") or []
    gate_lines = "\n".join(f"- {item}" for item in gate_errors) or "- None"
    markdown = render_text_template(
        load_text_template("report.md.tmpl"),
        {
            "RUN_ID": run_id,
            "KIND": kind,
            "GENERATED_AT": generated_at,
            "PROJECT_ID": manifest["project_id"],
            "PROJECT_MODE": state["project_mode"],
            "PHASE": state["phase"],
            "REVISION": state["revision"],
            "MILESTONE": state["active_milestone"] or "—",
            "CHANGE": state["active_change"] or "—",
            "SUMMARY": result["summary"],
            "NEXT_ACTION": state["next_action"] or "—",
            "COMMIT": git.get("commit") or "—",
            "GATE_STATUS": gate.get("effective_status") or "invalid",
            "GATE_PASSED": str(bool(gate.get("passed"))).lower(),
            "GATE_ERRORS": gate_lines,
            "PLANNING_STATUS": planning_result.get("status") if planning_result else "not_applicable",
            "PLANNING_DIGEST": planning_result.get("seal_digest") if planning_result else "—",
            "OPEN_QUESTIONS": question_lines,
            "ACTIVE_ASSUMPTIONS": assumption_lines,
        },
    )

    with ProjectLock(root):
        # The report is an evidence snapshot, not merely a pair of output files.
        # Recheck every mutable control-plane input after taking the writer lock
        # so a normal concurrent transition/report cannot emit a stale claim.
        current_validation = validate_project(root, check_fresh_gate=False)
        if not current_validation["valid"]:
            raise DeliveryError(
                f"control plane changed while the report was prepared: {current_validation['errors']}"
            )
        snapshots = (
            ("state", state, validate_state(read_json(delivery_path(root, "state.json")))),
            ("manifest", manifest, validate_manifest(read_json(delivery_path(root, "manifest.json")))),
            ("events", events, read_jsonl(delivery_path(root, "events.jsonl"))),
            ("questions", questions, _open_items(delivery_path(root, "questions.json"))),
            ("assumptions", assumptions, _open_items(delivery_path(root, "assumptions.json"))),
            ("gate", gate_value, read_json(delivery_path(root, "gate-manifest.json"))),
        )
        for label, prepared, current in snapshots:
            if canonical_sha256(prepared) != canonical_sha256(current):
                raise DeliveryError(
                    f"{label} changed while the report was prepared; retry from a fresh snapshot"
                )
        if planning_report:
            _validate_planning_manifest(
                root,
                state,
                live_external_bindings=planning_external_bindings,
            )
        if result_path.exists() or markdown_path.exists():
            raise DeliveryError(f"report already exists for {run_id}")
        controlled_exclusive_write_json(root, result_path, result)
        controlled_exclusive_write_text(root, markdown_path, markdown)
        report_event = event_record(
            sequence=next_event_sequence(read_jsonl(delivery_path(root, "events.jsonl"))),
            event_type="report.generated",
            actor=actor,
            state_revision=state["revision"],
            details={
                "run_id": run_id,
                "kind": kind,
                "result": str(result_path.relative_to(root)),
                "report": str(markdown_path.relative_to(root)),
            },
        )
        controlled_append_jsonl(root, delivery_path(root, "events.jsonl"), report_event)

    return {
        "generated": True,
        "run_id": run_id,
        "result": str(result_path),
        "report": str(markdown_path),
        "phase": state["phase"],
        "gate_status": gate.get("effective_status"),
        "open_question_count": len(questions),
        "active_assumption_count": len(assumptions),
    }


def print_json(value: Any) -> None:
    sys.stdout.write(json_text(value))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deliveryctl",
        description="Deterministic project control plane for the deliver-system Qoder skill.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {HARNESS_VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_git_authority_arguments(target: argparse.ArgumentParser) -> None:
        target.add_argument("--expected-git-dir")
        target.add_argument("--expected-common-dir")
        target.add_argument("--git-authority-ref")

    def add_planning_binding_arguments(
        target: argparse.ArgumentParser, *, decisions: bool
    ) -> None:
        add_git_authority_arguments(target)
        target.add_argument("--openspec-executable")
        if decisions:
            target.add_argument("--approved-decision", action="append", default=[])
            target.add_argument("--pending-planning-decision", action="append", default=[])
            target.add_argument("--blocking-decision", action="append", default=[])

    inspect_parser = subparsers.add_parser("inspect", help="inspect a project without modifying it")
    inspect_parser.add_argument("--project-root", default=".")
    inspect_parser.add_argument(
        "--route",
        choices=["auto", "greenfield", "brownfield", "resume"],
        default="auto",
        help="record an explicit route request; it cannot bypass Resume integrity",
    )
    inspect_parser.add_argument(
        "--source",
        action="append",
        default=[],
        help="registered source path used for Resume digest comparison; may be repeated",
    )
    inspect_parser.add_argument("--max-files", type=int)
    inspect_parser.add_argument("--max-directories", type=int)
    inspect_parser.add_argument("--max-depth", type=int)
    inspect_parser.add_argument("--max-marker-bytes", type=int)
    inspect_parser.add_argument("--max-results", type=int)
    add_git_authority_arguments(inspect_parser)

    init_parser = subparsers.add_parser("init", help="initialize delivery-docs/state without overwriting files")
    init_parser.add_argument("--project-root", default=".")
    init_parser.add_argument("--mode", choices=["auto", *sorted(PROJECT_MODES)], default="auto")
    init_parser.add_argument("--project-id")
    init_parser.add_argument(
        "--with-openspec-config",
        action="store_true",
        help="deprecated safety trap; use reviewed openspec-config-plan/apply instead",
    )

    git_scope_parser = subparsers.add_parser(
        "git-scope", help="validate the ambient main/linked checkout without managing it"
    )
    git_scope_parser.add_argument("--project-root", default=".")
    add_git_authority_arguments(git_scope_parser)
    git_scope_parser.add_argument("--exclude", action="append", default=[])

    plan_init_parser = subparsers.add_parser(
        "plan-init", help="create empty non-overwriting M3 planning authorities"
    )
    plan_init_parser.add_argument("--project-root", default=".")

    plan_validate_parser = subparsers.add_parser(
        "plan-validate", help="validate traceability and current Git/OpenSpec bindings"
    )
    plan_validate_parser.add_argument("--project-root", default=".")
    plan_validate_parser.add_argument(
        "--readiness", choices=["change_ready", "seal"], default="seal"
    )
    add_planning_binding_arguments(plan_validate_parser, decisions=True)

    plan_seal_parser = subparsers.add_parser(
        "plan-seal", help="seal an approved planning_ready graph and bind state"
    )
    plan_seal_parser.add_argument("--project-root", default=".")
    plan_seal_parser.add_argument("--expected-revision", type=int, required=True)
    plan_seal_parser.add_argument("--approval-ref", required=True)
    plan_seal_parser.add_argument("--actor", default="qoder")
    add_planning_binding_arguments(plan_seal_parser, decisions=True)

    plan_invalidate_parser = subparsers.add_parser(
        "plan-invalidate", help="event a return from a sealed plan to editable planning"
    )
    plan_invalidate_parser.add_argument("--project-root", default=".")
    plan_invalidate_parser.add_argument("--expected-revision", type=int, required=True)
    plan_invalidate_parser.add_argument("--reason", required=True)
    plan_invalidate_parser.add_argument("--actor", default="qoder")

    config_plan_parser = subparsers.add_parser(
        "openspec-config-plan", help="produce a zero-write reviewed config candidate"
    )
    config_plan_parser.add_argument("--project-root", default=".")
    config_plan_parser.add_argument("--candidate-file")

    config_apply_parser = subparsers.add_parser(
        "openspec-config-apply", help="CAS-apply exact approved OpenSpec config bytes"
    )
    config_apply_parser.add_argument("--project-root", default=".")
    config_apply_parser.add_argument("--candidate-file", required=True)
    config_apply_parser.add_argument("--candidate-digest", required=True)
    config_apply_parser.add_argument("--expected-config-digest", required=True)
    config_apply_parser.add_argument("--approval-ref", required=True)
    config_apply_parser.add_argument("--allow-existing-replacement", action="store_true")

    openspec_read_parser = subparsers.add_parser(
        "openspec-read", help="run one bounded M3 read-only OpenSpec JSON operation"
    )
    openspec_read_parser.add_argument("--project-root", default=".")
    openspec_read_parser.add_argument(
        "--operation",
        choices=["version", "schemas", "status", "instructions", "validate"],
        required=True,
    )
    openspec_read_parser.add_argument("--change")
    openspec_read_parser.add_argument("--artifact")
    openspec_read_parser.add_argument("--openspec-executable")

    openspec_new_parser = subparsers.add_parser(
        "openspec-new-change", help="create only the approved next-ready Change scaffold"
    )
    openspec_new_parser.add_argument("--project-root", default=".")
    openspec_new_parser.add_argument("--change", required=True)
    openspec_new_parser.add_argument("--approval-ref", required=True)
    openspec_new_parser.add_argument("--expected-config-digest", required=True)
    add_planning_binding_arguments(openspec_new_parser, decisions=True)

    validate_parser = subparsers.add_parser("validate", help="validate schemas and cross-file invariants")
    validate_parser.add_argument("--project-root", default=".")
    validate_parser.add_argument(
        "--fresh-gate",
        action="store_true",
        help="require current-commit evidence when the project is in a quality-gated phase",
    )
    validate_parser.add_argument(
        "--fresh-planning",
        action="store_true",
        help="recapture current Git/OpenSpec/source bindings for a sealed M3 plan",
    )
    add_planning_binding_arguments(validate_parser, decisions=False)

    recover_parser = subparsers.add_parser(
        "recover",
        help="finish or clear a journaled state transaction after interruption",
    )
    recover_parser.add_argument("--project-root", default=".")
    add_planning_binding_arguments(recover_parser, decisions=False)

    transition_parser = subparsers.add_parser("transition", help="perform an allowed atomic state transition")
    transition_parser.add_argument("--project-root", default=".")
    transition_parser.add_argument("--to", required=True)
    transition_parser.add_argument("--reason", required=True)
    transition_parser.add_argument("--actor", default="qoder")
    transition_parser.add_argument("--expected-revision", type=int)
    transition_parser.add_argument("--next-action")
    transition_parser.add_argument("--milestone")
    transition_parser.add_argument("--change")
    transition_parser.add_argument(
        "--approval-ref",
        help="human approval reference required for ACCEPTED and RELEASED",
    )
    add_planning_binding_arguments(transition_parser, decisions=False)

    context_parser = subparsers.add_parser(
        "set-context",
        help="optimistically update evented delivery context without changing phase",
    )
    context_parser.add_argument("--project-root", default=".")
    context_parser.add_argument("--expected-revision", type=int, required=True)
    context_parser.add_argument("--reason", required=True)
    context_parser.add_argument("--actor", default="qoder")
    for option, destination in (
        ("source-digest", "source_digest"),
        ("baseline-commit", "baseline_commit"),
        ("milestone", "active_milestone"),
        ("change", "active_change"),
        ("next-action", "next_action"),
    ):
        group = context_parser.add_mutually_exclusive_group()
        group.add_argument(f"--{option}", dest=destination, default=UNSET)
        group.add_argument(
            f"--clear-{option}",
            dest=destination,
            action="store_const",
            const=None,
        )
    question_group = context_parser.add_mutually_exclusive_group()
    question_group.add_argument("--blocking-question", action="append")
    question_group.add_argument("--clear-blocking-questions", action="store_true")
    decision_group = context_parser.add_mutually_exclusive_group()
    decision_group.add_argument("--pending-decision", action="append")
    decision_group.add_argument("--clear-pending-decisions", action="store_true")

    gate_parser = subparsers.add_parser("check-gate", help="check passed evidence against current Git state")
    gate_parser.add_argument("--project-root", default=".")
    gate_parser.add_argument("--gate")
    gate_parser.add_argument("--max-age-seconds", type=int)
    gate_parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="diagnostic only; gated state transitions never use this override",
    )

    report_parser = subparsers.add_parser("report", help="write a non-overwriting run result and Markdown report")
    report_parser.add_argument("--project-root", default=".")
    report_parser.add_argument("--run-id")
    report_parser.add_argument(
        "--kind",
        choices=sorted(REPORT_KINDS),
        default="checkpoint",
    )
    report_parser.add_argument("--summary", default="")
    report_parser.add_argument("--actor", default="qoder")
    add_git_authority_arguments(report_parser)
    report_parser.add_argument("--openspec-executable")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        root = canonical_root(args.project_root)
        if args.command == "inspect":
            limits = {
                key: value
                for key, value in {
                    "max_files": args.max_files,
                    "max_directories": args.max_directories,
                    "max_depth": args.max_depth,
                    "max_marker_bytes": args.max_marker_bytes,
                    "max_results": args.max_results,
                }.items()
                if value is not None
            }
            result = inspect_project(
                root,
                route_override=args.route,
                source_paths=args.source,
                scan_limits=limits,
                expected_git_dir=args.expected_git_dir,
                expected_common_dir=args.expected_common_dir,
                git_authority_ref=args.git_authority_ref,
            )
            print_json(result)
            return 0
        if args.command == "init":
            result = initialize_project(
                root,
                mode=args.mode,
                project_id=args.project_id,
                with_openspec_config=args.with_openspec_config,
            )
            print_json(result)
            return 0 if result["initialized"] else 1
        if args.command == "git-scope":
            try:
                result = git_scope.inspect_git_scope(
                    root,
                    expected_git_dir=args.expected_git_dir,
                    expected_common_dir=args.expected_common_dir,
                    authority_ref=args.git_authority_ref,
                    excluded_paths=args.exclude,
                )
            except git_scope.GitScopeError as exc:
                raise _adapter_error(exc) from exc
            print_json(result)
            return 0
        if args.command == "plan-init":
            result = initialize_planning_bundle(root)
            print_json(result)
            return 0
        if args.command == "plan-validate":
            result = validate_planning_now(
                root,
                approved_decisions=args.approved_decision,
                pending_decisions=args.pending_planning_decision,
                blocking_decisions=args.blocking_decision,
                expected_git_dir=args.expected_git_dir,
                expected_common_dir=args.expected_common_dir,
                git_authority_ref=args.git_authority_ref,
                openspec_executable=args.openspec_executable,
                readiness=args.readiness,
            )
            print_json(result)
            return 0 if result["valid"] else 1
        if args.command == "plan-seal":
            result = seal_planning(
                root,
                expected_revision=args.expected_revision,
                approval_ref=args.approval_ref,
                actor=args.actor,
                approved_decisions=args.approved_decision,
                pending_decisions=args.pending_planning_decision,
                blocking_decisions=args.blocking_decision,
                expected_git_dir=args.expected_git_dir,
                expected_common_dir=args.expected_common_dir,
                git_authority_ref=args.git_authority_ref,
                openspec_executable=args.openspec_executable,
            )
            print_json(result)
            return 0
        if args.command == "plan-invalidate":
            result = invalidate_planning(
                root,
                expected_revision=args.expected_revision,
                reason=args.reason,
                actor=args.actor,
            )
            print_json(result)
            return 0
        if args.command == "openspec-config-plan":
            result = plan_openspec_config(root, candidate_path=args.candidate_file)
            print_json(result)
            return 0
        if args.command == "openspec-config-apply":
            result = apply_openspec_config(
                root,
                candidate_path=args.candidate_file,
                candidate_digest=args.candidate_digest,
                expected_config_digest=args.expected_config_digest,
                approval_ref=args.approval_ref,
                allow_existing_replacement=args.allow_existing_replacement,
            )
            print_json(result)
            return 0
        if args.command == "openspec-read":
            result = read_openspec(
                root,
                operation=args.operation,
                change_id=args.change,
                artifact_id=args.artifact,
                executable=args.openspec_executable,
            )
            print_json(result)
            return 0
        if args.command == "openspec-new-change":
            result = create_planned_openspec_change(
                root,
                change_id=args.change,
                approval_ref=args.approval_ref,
                expected_config_digest=args.expected_config_digest,
                approved_decisions=args.approved_decision,
                pending_decisions=args.pending_planning_decision,
                blocking_decisions=args.blocking_decision,
                expected_git_dir=args.expected_git_dir,
                expected_common_dir=args.expected_common_dir,
                git_authority_ref=args.git_authority_ref,
                openspec_executable=args.openspec_executable,
            )
            print_json(result)
            return 0
        if args.command == "validate":
            result = validate_project(root, check_fresh_gate=args.fresh_gate)
            if args.fresh_planning and result["valid"]:
                state = validate_state(read_json(delivery_path(root, "state.json")))
                if state.get("planning_stage") != "plan_ready":
                    raise DeliveryError("--fresh-planning requires a plan_ready state")
                live = collect_live_bindings_for_sealed_plan(
                    root,
                    expected_git_dir=args.expected_git_dir,
                    expected_common_dir=args.expected_common_dir,
                    git_authority_ref=args.git_authority_ref,
                    openspec_executable=args.openspec_executable,
                )
                result["components"]["planning"] = _validate_planning_manifest(
                    root,
                    state,
                    live_external_bindings=live["external_bindings"],
                )
            print_json(result)
            return 0 if result["valid"] else 1
        if args.command == "recover":
            result = recover_project(
                root,
                expected_git_dir=args.expected_git_dir,
                expected_common_dir=args.expected_common_dir,
                git_authority_ref=args.git_authority_ref,
                openspec_executable=args.openspec_executable,
            )
            print_json(result)
            return 0 if result["validation"]["valid"] else 1
        if args.command == "transition":
            normalized_target = re.sub(r"[-\s]+", "_", args.to.strip()).upper()
            planning_external = None
            if normalized_target in PLANNING_GATED_PHASES:
                planning_external = collect_live_bindings_for_sealed_plan(
                    root,
                    expected_git_dir=args.expected_git_dir,
                    expected_common_dir=args.expected_common_dir,
                    git_authority_ref=args.git_authority_ref,
                    openspec_executable=args.openspec_executable,
                )["external_bindings"]
            result = transition_state(
                root,
                target=args.to,
                reason=args.reason,
                actor=args.actor,
                expected_revision=args.expected_revision,
                next_action=args.next_action,
                milestone=args.milestone,
                change=args.change,
                approval_ref=args.approval_ref,
                planning_external_bindings=planning_external,
            )
            print_json(result)
            return 0
        if args.command == "set-context":
            blocking_questions = (
                []
                if args.clear_blocking_questions
                else args.blocking_question
                if args.blocking_question is not None
                else UNSET
            )
            pending_decisions = (
                []
                if args.clear_pending_decisions
                else args.pending_decision
                if args.pending_decision is not None
                else UNSET
            )
            result = set_context(
                root,
                expected_revision=args.expected_revision,
                actor=args.actor,
                reason=args.reason,
                source_digest=args.source_digest,
                baseline_commit=args.baseline_commit,
                active_milestone=args.active_milestone,
                active_change=args.active_change,
                next_action=args.next_action,
                blocking_questions=blocking_questions,
                pending_decisions=pending_decisions,
            )
            print_json(result)
            return 0
        if args.command == "check-gate":
            gate_path = Path(args.gate) if args.gate else None
            if args.max_age_seconds is not None and args.max_age_seconds <= 0:
                raise DeliveryError("--max-age-seconds must be positive")
            result = check_gate_file(
                root,
                gate_path,
                allow_dirty=args.allow_dirty,
                max_age=args.max_age_seconds,
            )
            print_json(result)
            return 0 if result["passed"] else 1
        if args.command == "report":
            planning_external = None
            if args.kind in REPORT_PHASES_REQUIRING_PLANNING:
                planning_external = collect_live_bindings_for_sealed_plan(
                    root,
                    expected_git_dir=args.expected_git_dir,
                    expected_common_dir=args.expected_common_dir,
                    git_authority_ref=args.git_authority_ref,
                    openspec_executable=args.openspec_executable,
                )["external_bindings"]
            result = build_report(
                root,
                run_id=args.run_id,
                kind=args.kind,
                summary=args.summary,
                actor=args.actor,
                planning_external_bindings=planning_external,
            )
            print_json(result)
            return 0
        parser.error(f"unknown command: {args.command}")
    except DeliveryError as exc:
        print_json({"ok": False, "command": args.command, "error": str(exc)})
        return 2
    except OSError as exc:
        print_json({"ok": False, "command": args.command, "error": f"operating system error: {exc}"})
        return 2
    except KeyboardInterrupt:
        print_json({"ok": False, "command": args.command, "error": "interrupted"})
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
