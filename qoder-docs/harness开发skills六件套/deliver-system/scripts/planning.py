#!/usr/bin/env python3
"""Deterministic M3 planning-graph validation for ``deliver-system``.

The validator is deliberately read-only and standard-library-only.  It checks
the machine authority graph and bindings supplied by separate source, Git, and
OpenSpec adapters; it never invokes those tools and never repairs input files.

Authority artifacts use ``schema_version: 1`` and these default locations:

* ``delivery-docs/product/requirements.json`` (``requirements``)
* ``delivery-docs/product/acceptance.json`` (``acceptance_criteria``)
* ``delivery-docs/product/feature-ledger.json`` (``features``)
* ``delivery-docs/plans/delivery-roadmap.json`` (``milestones`` and ``slices``)
* ``delivery-docs/state/work-items/<change-id>.json`` (one delivery contract per slice)

``external_bindings`` is also versioned.  It carries registered source
digests, approved/pending decisions, an opaque Git identity, and OpenSpec
Change status previously obtained by their dedicated adapters.  This module
validates and digest-binds those facts without trusting them as evidence of
implementation or test success.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


PLANNING_SCHEMA_VERSION = 1
VALIDATOR_VERSION = "0.3.0"
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_PLANNING_BYTES = 64 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 128 * 1024 * 1024
MAX_REGISTERED_SOURCES = 256
MAX_DECISION_BYTES = 4 * 1024 * 1024
MAX_TOTAL_DECISION_BYTES = 32 * 1024 * 1024
MAX_DECISION_ARTIFACTS = 512
MAX_WORK_ITEMS = 512
MAX_REQUIREMENTS_PER_SLICE = 8
MAX_ACCEPTANCE_PER_SLICE = 12

DEFAULT_AUTHORITY_PATHS: dict[str, str] = {
    "requirements": "delivery-docs/product/requirements.json",
    "acceptance": "delivery-docs/product/acceptance.json",
    "features": "delivery-docs/product/feature-ledger.json",
    "roadmap": "delivery-docs/plans/delivery-roadmap.json",
    "work_items": "delivery-docs/state/work-items",
}

REQUIREMENT_KINDS = {"functional", "non_functional", "preservation"}
REQUIREMENT_PRIORITIES = {"P0", "P1", "P2", "P3"}
REQUIREMENT_STATUSES = {"confirmed", "draft", "blocked"}
SCOPE_STATUSES = {"in_scope", "deferred", "out_of_scope"}
ACCEPTANCE_CLASSIFICATIONS = {"positive", "negative", "regression"}
ACCEPTANCE_BOUNDARIES = {
    "static",
    "unit",
    "component",
    "contract",
    "integration",
    "e2e",
    "manual",
}
AUTOMATION_INTENTS = {
    "automated",
    "manual_with_reason",
    "not_applicable_with_reason",
}
M3_STATUSES = {"planned"}
RISK_LEVELS = {"R0", "R1", "R2", "R3", "R4"}
QODER_SURFACES = {"local", "quest", "goal", "experts", "worktree"}
CHANGE_STATES = {"reserved", "materialized"}
MIGRATION_STATES = {"none", "planned", "required"}
SENSITIVE_CHANGE_TYPES = {
    "breaking",
    "removal",
    "rename",
    "migration",
    "rollback",
    "security",
    "permission",
    "privacy",
    "external_cost",
    "production_data",
}
RISK_COVERAGE_TYPES = {
    "failure",
    "permission",
    "compatibility",
    "migration",
    "rollback",
    "security",
    "privacy",
    "external_cost",
    "production_data",
    "breaking",
    "removal",
    "rename",
}
GATE_LEVELS = {"GATE-0", "GATE-1", "GATE-2", "GATE-3"}
CHECK_LEVELS = ACCEPTANCE_BOUNDARIES - {"manual"}
CHECK_STATUSES = {"planned"}
DECISION_STATUSES = {"approved", "pending", "blocking"}
CHECK_ID = re.compile(r"CHECK-[A-Z0-9][A-Z0-9-]*\Z")
REQ_ID = re.compile(r"REQ-[A-Z0-9][A-Z0-9-]*\Z")
AC_ID = re.compile(r"AC-[A-Z0-9][A-Z0-9-]*\Z")
FEATURE_ID = re.compile(r"FEATURE-[A-Z0-9][A-Z0-9-]*\Z")
SLICE_ID = re.compile(r"SLICE-[A-Z0-9][A-Z0-9-]*\Z")
MILESTONE_ID = re.compile(r"MILESTONE-[A-Z0-9][A-Z0-9-]*\Z")
PDR_ID = re.compile(r"PDR-[A-Z0-9][A-Z0-9-]*\Z")
ADR_ID = re.compile(r"ADR-[0-9]{4}(?:-[A-Z0-9][A-Z0-9-]*)?\Z")
QUESTION_ID = re.compile(r"Q-[A-Z0-9][A-Z0-9-]*\Z")
CHANGE_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
GIT_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
REPOSITORY_SCOPE_ID = re.compile(r"repo-[0-9a-f]{64}\Z")
CHECKOUT_SCOPE_ID = re.compile(r"checkout-[0-9a-f]{64}\Z")

# Stable public reason codes.  Messages may improve; automation should key on
# these values only.
PATH_INVALID = "planning-path-invalid"
PATH_MISSING = "planning-path-missing"
PATH_LINK = "planning-path-link"
PATH_SPECIAL = "planning-path-special"
PATH_OVERSIZE = "planning-path-oversize"
JSON_INVALID = "planning-json-invalid"
JSON_DUPLICATE_KEY = "planning-json-duplicate-key"
JSON_ROOT_INVALID = "planning-json-root-invalid"
SCHEMA_UNSUPPORTED = "planning-schema-unsupported"
ARTIFACT_TYPE_INVALID = "planning-artifact-type-invalid"
FIELD_MISSING = "planning-field-missing"
FIELD_INVALID = "planning-field-invalid"
ENUM_INVALID = "planning-enum-invalid"
ID_INVALID = "planning-id-invalid"
ID_DUPLICATE = "planning-id-duplicate"
REFERENCE_MISSING = "planning-reference-missing"
REFERENCE_MISMATCH = "planning-reference-mismatch"
SOURCE_UNREGISTERED = "planning-source-unregistered"
SOURCE_DIGEST_INVALID = "planning-source-digest-invalid"
SOURCE_DIGEST_MISMATCH = "planning-source-digest-mismatch"
REQUIREMENT_UNCOVERED = "planning-requirement-uncovered"
ACCEPTANCE_ORPHAN = "planning-acceptance-orphan"
ACCEPTANCE_UNCOVERED = "planning-acceptance-uncovered"
ACCEPTANCE_ORACLE_MISSING = "planning-acceptance-oracle-missing"
FEATURE_ORPHAN = "planning-feature-orphan"
FEATURE_UNCOVERED = "planning-feature-uncovered"
SLICE_ORPHAN = "planning-slice-orphan"
CONTRACT_MISSING = "planning-contract-missing"
CONTRACT_ORPHAN = "planning-contract-orphan"
CONTRACT_DUPLICATE = "planning-contract-duplicate"
DEPENDENCY_MISSING = "planning-dependency-missing"
DEPENDENCY_SELF = "planning-dependency-self"
DEPENDENCY_CYCLE = "planning-dependency-cycle"
TOPOLOGY_INVALID = "planning-topology-invalid"
SCOPE_LEAK = "planning-scope-leak"
SLICE_OVERSIZED = "planning-slice-oversized"
OVERSIZE_PDR_REQUIRED = "planning-oversize-pdr-required"
CATCH_ALL_SLICE = "planning-catch-all-slice"
PRESERVATION_REQUIRED = "planning-preservation-required"
PRESERVATION_NOT_OBSERVABLE = "planning-preservation-not-observable"
PRESERVATION_ORACLE_MISSING = "planning-preservation-oracle-missing"
M4_LOCK_VIOLATION = "planning-m4-lock-violation"
CHECK_COVERAGE_MISSING = "planning-check-coverage-missing"
OPENSPEC_BINDING_INVALID = "planning-openspec-binding-invalid"
OPENSPEC_ORPHAN = "planning-openspec-orphan"
OPENSPEC_JIT_VIOLATION = "planning-openspec-jit-violation"
BLOCKING_QUESTION = "planning-blocking-question"
BLOCKING_DECISION = "planning-blocking-decision"
DECISION_UNAPPROVED = "planning-decision-unapproved"
DECISION_ARTIFACT_INVALID = "planning-decision-artifact-invalid"
DECISION_DIGEST_MISMATCH = "planning-decision-digest-mismatch"
DECISION_STATE_MISMATCH = "planning-decision-state-mismatch"
DECISION_AUTHORITY_DIGEST_MISMATCH = "planning-decision-authority-digest-mismatch"
SENSITIVE_CHANGE_UNDECLARED = "planning-sensitive-change-undeclared"
SENSITIVE_APPROVAL_REQUIRED = "planning-sensitive-approval-required"
SENSITIVE_ACCEPTANCE_MISSING = "planning-sensitive-acceptance-missing"
BINDING_INVALID = "planning-binding-invalid"


class _DuplicateKey(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


class _ReadFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_json_bytes(value: Any) -> bytes:
    """Return the canonical JSON encoding used by M3 planning digests."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_json_digest(value: Any) -> str:
    """Return a deterministic SHA-256 for JSON-compatible *value*."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _os_error_detail(error: OSError) -> str:
    """Describe an OS failure without leaking an absolute local path."""

    return f"{type(error).__name__} (errno {error.errno})"


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _json_scalar_or_container(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    if isinstance(value, list):
        return all(_json_scalar_or_container(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_scalar_or_container(item) for key, item in value.items())
    return False


def _canonical_relative_path(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        return None
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return None
    canonical = pure.as_posix()
    if canonical != raw or canonical.startswith("/"):
        return None
    return canonical


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _file_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _same_entry(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, stat.S_IFMT(left.st_mode)) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


class _DirectoryHandle:
    """Windows substitute for an open directory descriptor.

    POSIX traversal anchors every step in an open directory fd so a swapped
    or renamed ancestor is detected through ``fstat``. Windows cannot open
    directories, so the handle pins the verified (dev, ino, type) identity
    of each directory and re-validates it before every child operation.
    """

    __slots__ = ("path", "identity")

    def __init__(self, path: Path, metadata: os.stat_result) -> None:
        self.path = path
        self.identity = (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))

    def verify(self, artifact: str) -> None:
        try:
            current = self.path.lstat()
        except OSError as exc:
            raise _ReadFailure(
                PATH_INVALID,
                f"{artifact} cannot be reinspected: {_os_error_detail(exc)}",
            ) from exc
        identity = (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode))
        if identity != self.identity:
            raise _ReadFailure(PATH_INVALID, f"{artifact} changed while it was being traversed")


def _close_directory(handle: "int | _DirectoryHandle") -> None:
    if isinstance(handle, _DirectoryHandle):
        return
    os.close(handle)


def _open_root_directory(root: Path) -> "int | _DirectoryHandle":
    try:
        expected = root.lstat()
    except FileNotFoundError as exc:
        raise _ReadFailure(PATH_MISSING, "project root is missing") from exc
    except OSError as exc:
        raise _ReadFailure(
            PATH_INVALID,
            f"project root cannot be opened safely: {_os_error_detail(exc)}",
        ) from exc
    if os.name == "nt":
        # Windows cannot open directory descriptors; pin the identity instead.
        if stat.S_ISLNK(expected.st_mode) or not stat.S_ISDIR(expected.st_mode):
            raise _ReadFailure(PATH_LINK, "project root changed or is a symbolic link")
        return _DirectoryHandle(root, expected)
    try:
        descriptor = os.open(root, _directory_flags())
    except FileNotFoundError as exc:
        raise _ReadFailure(PATH_MISSING, "project root is missing") from exc
    except OSError as exc:
        raise _ReadFailure(
            PATH_INVALID,
            f"project root cannot be opened safely: {_os_error_detail(exc)}",
        ) from exc
    observed = os.fstat(descriptor)
    if (
        stat.S_ISLNK(expected.st_mode)
        or not stat.S_ISDIR(observed.st_mode)
        or not _same_entry(expected, observed)
    ):
        os.close(descriptor)
        raise _ReadFailure(PATH_LINK, "project root changed or is a symbolic link")
    return descriptor


def _open_child_directory(parent_fd: "int | _DirectoryHandle", name: str, *, artifact: str) -> "int | _DirectoryHandle":
    if isinstance(parent_fd, _DirectoryHandle):
        parent_fd.verify(artifact)
        child_path = parent_fd.path / name
        try:
            expected = child_path.lstat()
        except FileNotFoundError as exc:
            raise _ReadFailure(PATH_MISSING, f"{artifact} is missing") from exc
        except OSError as exc:
            raise _ReadFailure(
                PATH_INVALID,
                f"{artifact} cannot be inspected: {_os_error_detail(exc)}",
            ) from exc
        if stat.S_ISLNK(expected.st_mode):
            raise _ReadFailure(PATH_LINK, f"{artifact} crosses a symbolic link")
        if not stat.S_ISDIR(expected.st_mode):
            raise _ReadFailure(PATH_SPECIAL, f"{artifact} has a non-directory parent")
        return _DirectoryHandle(child_path, expected)
    try:
        expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise _ReadFailure(PATH_MISSING, f"{artifact} is missing") from exc
    except OSError as exc:
        raise _ReadFailure(
            PATH_INVALID,
            f"{artifact} cannot be inspected: {_os_error_detail(exc)}",
        ) from exc
    if stat.S_ISLNK(expected.st_mode):
        raise _ReadFailure(PATH_LINK, f"{artifact} crosses a symbolic link")
    if not stat.S_ISDIR(expected.st_mode):
        raise _ReadFailure(PATH_SPECIAL, f"{artifact} has a non-directory parent")
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise _ReadFailure(
            PATH_INVALID,
            f"{artifact} cannot be opened safely: {_os_error_detail(exc)}",
        ) from exc
    observed = os.fstat(descriptor)
    if not stat.S_ISDIR(observed.st_mode) or not _same_entry(expected, observed):
        os.close(descriptor)
        raise _ReadFailure(PATH_INVALID, f"{artifact} changed while it was being opened")
    return descriptor


def _open_relative_directory(root: Path, relative: str) -> "int | _DirectoryHandle":
    canonical = _canonical_relative_path(relative)
    if canonical is None:
        raise _ReadFailure(PATH_INVALID, f"{relative!r} is not a canonical project-relative path")
    descriptor = _open_root_directory(root)
    traversed: list[str] = []
    try:
        for component in PurePosixPath(canonical).parts:
            traversed.append(component)
            child = _open_child_directory(
                descriptor,
                component,
                artifact="/".join(traversed),
            )
            _close_directory(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        _close_directory(descriptor)
        raise


def _read_regular_at(
    parent_fd: "int | _DirectoryHandle",
    name: str,
    *,
    artifact: str,
    max_bytes: int,
    expected: os.stat_result | None = None,
) -> bytes:
    if not name or "/" in name or "\x00" in name:
        raise _ReadFailure(PATH_INVALID, f"{artifact} has an invalid final component")
    windows_handle = parent_fd if isinstance(parent_fd, _DirectoryHandle) else None
    if windows_handle is not None:
        windows_handle.verify(artifact)
    if expected is None:
        try:
            if windows_handle is not None:
                expected = (windows_handle.path / name).lstat()
            else:
                expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise _ReadFailure(PATH_MISSING, f"{artifact} is missing") from exc
        except OSError as exc:
            raise _ReadFailure(
                PATH_INVALID,
                f"{artifact} cannot be inspected: {_os_error_detail(exc)}",
            ) from exc
    if stat.S_ISLNK(expected.st_mode):
        raise _ReadFailure(PATH_LINK, f"{artifact} crosses a symbolic link")
    if not stat.S_ISREG(expected.st_mode):
        raise _ReadFailure(PATH_SPECIAL, f"{artifact} is not a regular file")
    try:
        if windows_handle is not None:
            file_flags = _file_flags()
            if hasattr(os, "O_BINARY"):
                file_flags |= os.O_BINARY
            descriptor = os.open(windows_handle.path / name, file_flags)
        else:
            descriptor = os.open(name, _file_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise _ReadFailure(
            PATH_INVALID,
            f"{artifact} cannot be opened safely: {_os_error_detail(exc)}",
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not _same_entry(expected, before):
            raise _ReadFailure(PATH_INVALID, f"{artifact} changed while it was being opened")
        if before.st_size > max_bytes:
            raise _ReadFailure(PATH_OVERSIZE, f"{artifact} exceeds the {max_bytes}-byte read budget")
        blocks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                break
            blocks.append(block)
            remaining -= len(block)
        data = b"".join(blocks)
        after = os.fstat(descriptor)
        if len(data) > max_bytes:
            raise _ReadFailure(PATH_OVERSIZE, f"{artifact} exceeds the {max_bytes}-byte read budget")
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_mode,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_mode,
        )
        if before_identity != after_identity or len(data) != after.st_size:
            raise _ReadFailure(PATH_INVALID, f"{artifact} changed while it was being read")
        return data
    finally:
        os.close(descriptor)


def _read_regular_bytes(root: Path, relative: str, *, max_bytes: int) -> bytes:
    canonical = _canonical_relative_path(relative)
    if canonical is None:
        raise _ReadFailure(PATH_INVALID, f"{relative!r} is not a canonical project-relative path")
    parts = PurePosixPath(canonical).parts
    parent_fd = _open_root_directory(root)
    traversed: list[str] = []
    try:
        for component in parts[:-1]:
            traversed.append(component)
            child = _open_child_directory(
                parent_fd,
                component,
                artifact="/".join(traversed),
            )
            _close_directory(parent_fd)
            parent_fd = child
        return _read_regular_at(
            parent_fd,
            parts[-1],
            artifact=canonical,
            max_bytes=max_bytes,
        )
    finally:
        _close_directory(parent_fd)


def _strict_json(data: bytes, relative: str) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _ReadFailure(JSON_INVALID, f"{relative} is not valid UTF-8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except _DuplicateKey as exc:
        raise _ReadFailure(
            JSON_DUPLICATE_KEY,
            f"{relative} contains duplicate JSON key {exc.key!r}",
        ) from exc
    except json.JSONDecodeError as exc:
        raise _ReadFailure(
            JSON_INVALID,
            f"{relative} has invalid JSON at line {exc.lineno}, column {exc.colno}",
        ) from exc
    if not isinstance(value, dict):
        raise _ReadFailure(JSON_ROOT_INVALID, f"{relative} must contain a JSON object")
    return value


class _Validation:
    def __init__(self) -> None:
        self.errors: list[dict[str, str]] = []
        self.warnings: list[dict[str, str]] = []

    def error(self, code: str, path: str, message: str) -> None:
        self.errors.append({"code": code, "path": path, "message": message})

    def warning(self, code: str, path: str, message: str) -> None:
        self.warnings.append({"code": code, "path": path, "message": message})

    def required(self, value: Mapping[str, Any], keys: Iterable[str], path: str) -> None:
        for key in sorted(set(keys) - set(value)):
            self.error(FIELD_MISSING, f"{path}.{key}", "required field is missing")

    def string(self, value: Any, path: str) -> str | None:
        if not _nonempty_string(value):
            self.error(FIELD_INVALID, path, "must be a non-empty string")
            return None
        return value.strip()

    def enum(self, value: Any, allowed: set[str], path: str) -> str | None:
        if not isinstance(value, str) or value not in allowed:
            self.error(ENUM_INVALID, path, f"must be one of {sorted(allowed)}")
            return None
        return value

    def identifier(self, value: Any, pattern: re.Pattern[str], path: str) -> str | None:
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            self.error(ID_INVALID, path, "has an invalid stable identifier")
            return None
        return value

    def id_list(self, value: Any, pattern: re.Pattern[str], path: str, *, nonempty: bool = False) -> list[str]:
        if not isinstance(value, list):
            self.error(FIELD_INVALID, path, "must be an array of stable IDs")
            return []
        if nonempty and not value:
            self.error(FIELD_INVALID, path, "must not be empty")
        result: list[str] = []
        seen: set[str] = set()
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]"
            identifier = self.identifier(item, pattern, item_path)
            if identifier is None:
                continue
            if identifier in seen:
                self.error(ID_DUPLICATE, item_path, f"duplicate ID {identifier}")
                continue
            seen.add(identifier)
            result.append(identifier)
        return result

    def string_list(self, value: Any, path: str, *, nonempty: bool = False) -> list[str]:
        if not isinstance(value, list):
            self.error(FIELD_INVALID, path, "must be an array of strings")
            return []
        if nonempty and not value:
            self.error(FIELD_INVALID, path, "must not be empty")
        result: list[str] = []
        for index, item in enumerate(value):
            if not _nonempty_string(item):
                self.error(FIELD_INVALID, f"{path}[{index}]", "must be a non-empty string")
            else:
                result.append(item.strip())
        return result


def _validate_header(
    validation: _Validation,
    value: Mapping[str, Any],
    path: str,
    artifact_type: str,
) -> None:
    validation.required(value, {"schema_version", "artifact_type"}, path)
    if value.get("schema_version") != PLANNING_SCHEMA_VERSION:
        validation.error(
            SCHEMA_UNSUPPORTED,
            f"{path}.schema_version",
            f"must be {PLANNING_SCHEMA_VERSION}",
        )
    if value.get("artifact_type") != artifact_type:
        validation.error(
            ARTIFACT_TYPE_INVALID,
            f"{path}.artifact_type",
            f"must be {artifact_type!r}",
        )


def _objects(value: Any, path: str, validation: _Validation) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        validation.error(FIELD_INVALID, path, "must be an array")
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            validation.error(FIELD_INVALID, f"{path}[{index}]", "must be an object")
        else:
            result.append(item)
    return result


def _index_items(
    validation: _Validation,
    items: Sequence[Mapping[str, Any]],
    path: str,
    pattern: re.Pattern[str],
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(items):
        identifier = validation.identifier(item.get("id"), pattern, f"{path}[{index}].id")
        if identifier is None:
            continue
        if identifier in result:
            validation.error(ID_DUPLICATE, f"{path}[{index}].id", f"duplicate ID {identifier}")
        else:
            result[identifier] = item
    return result


def _decision_ids(validation: _Validation, value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        validation.error(FIELD_INVALID, path, "must be an array of ADR/PDR IDs")
        return []
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not (PDR_ID.fullmatch(item) or ADR_ID.fullmatch(item)):
            validation.error(ID_INVALID, f"{path}[{index}]", "must be a PDR-* or ADR-* identifier")
            continue
        if item in seen:
            validation.error(ID_DUPLICATE, f"{path}[{index}]", f"duplicate decision ID {item}")
            continue
        seen.add(item)
        result.append(item)
    return result


def _enum_list(
    validation: _Validation,
    value: Any,
    allowed: set[str],
    path: str,
) -> list[str]:
    values = validation.string_list(value, path)
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(values):
        if item not in allowed:
            validation.error(ENUM_INVALID, f"{path}[{index}]", f"must be one of {sorted(allowed)}")
        elif item in seen:
            validation.error(ID_DUPLICATE, f"{path}[{index}]", f"duplicate value {item}")
        else:
            seen.add(item)
            result.append(item)
    return result


def _inferred_sensitive_changes(boundaries: Iterable[str]) -> set[str]:
    inferred: set[str] = set()
    for boundary in boundaries:
        lowered = boundary.lower()
        tokens = set(re.findall(r"[a-z0-9]+", lowered))
        compacted = re.sub(r"[^a-z0-9]+", "", lowered)
        if tokens & {
            "security",
            "auth",
            "authentication",
            "authorization",
            "login",
            "password",
            "credential",
            "credentials",
            "session",
            "token",
            "oauth",
            "sso",
            "mfa",
            "2fa",
            "webauthn",
        } or "signin" in compacted:
            inferred.add("security")
        if tokens & {"permission", "permissions", "role", "roles", "access"}:
            inferred.add("permission")
        if tokens & {"privacy", "pii", "personal"}:
            inferred.add("privacy")
        if tokens & {"paid", "payment", "billing", "cost"}:
            inferred.add("external_cost")
        if "production" in tokens and tokens & {"data", "database", "storage"}:
            inferred.add("production_data")
        if "breaking" in tokens:
            inferred.add("breaking")
        if tokens & {"removal", "remove", "deletion", "delete"}:
            inferred.add("removal")
        if tokens & {"rename", "renaming"}:
            inferred.add("rename")
        if tokens & {"migration", "migrate", "migrating"}:
            inferred.add("migration")
        if "rollback" in tokens:
            inferred.add("rollback")
        if any(term in lowered for term in ("登录", "登入", "认证", "授权", "密码", "凭据", "会话", "令牌", "单点登录", "多因素认证")):
            inferred.add("security")
        if any(term in lowered for term in ("权限", "角色控制", "访问控制")):
            inferred.add("permission")
        if any(term in lowered for term in ("隐私", "个人信息", "个人数据", "敏感数据")):
            inferred.add("privacy")
        if any(term in lowered for term in ("付费", "计费", "账单", "支付", "外部费用")):
            inferred.add("external_cost")
        if any(term in lowered for term in ("生产数据", "生产数据库")):
            inferred.add("production_data")
        if any(term in lowered for term in ("破坏性", "不兼容")):
            inferred.add("breaking")
        if any(term in lowered for term in ("删除", "移除", "下线")):
            inferred.add("removal")
        if "重命名" in lowered:
            inferred.add("rename")
        if "迁移" in lowered:
            inferred.add("migration")
        if "回滚" in lowered:
            inferred.add("rollback")
    return inferred


def _graph_order(
    validation: _Validation,
    items: Mapping[str, Mapping[str, Any]],
    dependency_key: str,
    path_prefix: str,
) -> tuple[list[str], dict[str, list[str]]]:
    dependencies: dict[str, list[str]] = {}
    reverse: dict[str, list[str]] = {identifier: [] for identifier in items}
    indegree: dict[str, int] = {identifier: 0 for identifier in items}
    for identifier, item in items.items():
        raw = item.get(dependency_key, [])
        deps = []
        if not isinstance(raw, list):
            validation.error(FIELD_INVALID, f"{path_prefix}.{identifier}.{dependency_key}", "must be an array")
            raw = []
        for index, dependency in enumerate(raw):
            dep_path = f"{path_prefix}.{identifier}.{dependency_key}[{index}]"
            if not isinstance(dependency, str):
                validation.error(ID_INVALID, dep_path, "dependency must be a stable ID")
                continue
            if dependency == identifier:
                validation.error(DEPENDENCY_SELF, dep_path, "an item cannot depend on itself")
                continue
            if dependency not in items:
                validation.error(DEPENDENCY_MISSING, dep_path, f"unknown dependency {dependency}")
                continue
            if dependency in deps:
                validation.error(ID_DUPLICATE, dep_path, f"duplicate dependency {dependency}")
                continue
            deps.append(dependency)
            reverse[dependency].append(identifier)
            indegree[identifier] += 1
        dependencies[identifier] = deps

    heap: list[tuple[int, str]] = []
    for identifier, degree in indegree.items():
        if degree == 0:
            order = items[identifier].get("order", 0)
            key = order if isinstance(order, int) and not isinstance(order, bool) else 0
            heapq.heappush(heap, (key, identifier))
    result: list[str] = []
    while heap:
        _, identifier = heapq.heappop(heap)
        result.append(identifier)
        for dependent in sorted(reverse[identifier]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                order = items[dependent].get("order", 0)
                key = order if isinstance(order, int) and not isinstance(order, bool) else 0
                heapq.heappush(heap, (key, dependent))
    if len(result) != len(items):
        cycle_nodes = sorted(set(items) - set(result))
        validation.error(
            DEPENDENCY_CYCLE,
            path_prefix,
            f"dependency cycle includes {', '.join(cycle_nodes)}",
        )
    return result, dependencies


def _load_json_artifact(
    validation: _Validation,
    root: Path,
    relative: str,
    digests: dict[str, str],
    byte_budget: list[int],
) -> dict[str, Any] | None:
    try:
        data = _read_regular_bytes(root, relative, max_bytes=MAX_JSON_BYTES)
        byte_budget[0] += len(data)
        if byte_budget[0] > MAX_TOTAL_PLANNING_BYTES:
            raise _ReadFailure(
                PATH_OVERSIZE,
                f"planning artifacts exceed the {MAX_TOTAL_PLANNING_BYTES}-byte total budget",
            )
        digests[relative] = _sha256_bytes(data)
        return _strict_json(data, relative)
    except _ReadFailure as exc:
        validation.error(exc.code, relative, str(exc))
        return None


def _load_work_items(
    validation: _Validation,
    root: Path,
    relative: str,
    digests: dict[str, str],
    byte_budget: list[int],
) -> list[tuple[str, dict[str, Any]]]:
    try:
        directory_fd = _open_relative_directory(root, relative)
    except _ReadFailure as exc:
        validation.error(exc.code, relative, str(exc))
        return []
    entries: list[tuple[str, os.stat_result]] = []
    try:
        if isinstance(directory_fd, _DirectoryHandle):
            directory_fd.verify(relative)
            scan_target: "int | str" = os.fspath(directory_fd.path)
        else:
            scan_target = directory_fd
        with os.scandir(scan_target) as iterator:
            for entry in iterator:
                if len(entries) >= MAX_WORK_ITEMS:
                    validation.error(PATH_OVERSIZE, relative, f"contains more than {MAX_WORK_ITEMS} entries")
                    break
                try:
                    if isinstance(directory_fd, _DirectoryHandle):
                        # Windows DirEntry.stat() leaves st_dev/st_ino zeroed;
                        # lstat() returns the real identity that _same_entry needs.
                        metadata = (directory_fd.path / entry.name).lstat()
                    else:
                        metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    validation.error(
                        PATH_INVALID,
                        f"{relative}/{entry.name}",
                        f"cannot inspect work item: {_os_error_detail(exc)}",
                    )
                    continue
                entries.append((entry.name, metadata))
        entries.sort(key=lambda item: item[0])
    except OSError as exc:
        validation.error(PATH_INVALID, relative, f"cannot enumerate work items: {_os_error_detail(exc)}")
        _close_directory(directory_fd)
        return []
    result: list[tuple[str, dict[str, Any]]] = []
    try:
        for name, metadata in entries:
            item_relative = f"{relative}/{name}"
            if stat.S_ISLNK(metadata.st_mode):
                validation.error(PATH_LINK, item_relative, "work item must not be a symbolic link")
                continue
            if not stat.S_ISREG(metadata.st_mode):
                validation.error(PATH_SPECIAL, item_relative, "work item must be a regular file")
                continue
            if not name.endswith(".json"):
                validation.error(PATH_INVALID, item_relative, "work-item directory may contain only .json contracts")
                continue
            change_id = name[:-5]
            if CHANGE_ID.fullmatch(change_id) is None:
                validation.error(ID_INVALID, item_relative, "contract filename must be <kebab-change-id>.json")
                continue
            try:
                data = _read_regular_at(
                    directory_fd,
                    name,
                    artifact=item_relative,
                    max_bytes=MAX_JSON_BYTES,
                    expected=metadata,
                )
                byte_budget[0] += len(data)
                if byte_budget[0] > MAX_TOTAL_PLANNING_BYTES:
                    raise _ReadFailure(
                        PATH_OVERSIZE,
                        f"planning artifacts exceed the {MAX_TOTAL_PLANNING_BYTES}-byte total budget",
                    )
                digests[item_relative] = _sha256_bytes(data)
                result.append((item_relative, _strict_json(data, item_relative)))
            except _ReadFailure as exc:
                validation.error(exc.code, item_relative, str(exc))
    finally:
        _close_directory(directory_fd)
    return result


def _validate_requirements(
    validation: _Validation,
    document: Mapping[str, Any],
    path: str,
    registered_sources: Mapping[str, str],
    actual_sources: Mapping[str, str],
) -> dict[str, Mapping[str, Any]]:
    _validate_header(validation, document, path, "requirements")
    validation.required(document, {"requirements"}, path)
    items = _objects(document.get("requirements"), f"{path}.requirements", validation)
    indexed = _index_items(validation, items, f"{path}.requirements", REQ_ID)
    for index, item in enumerate(items):
        item_path = f"{path}.requirements[{index}]"
        validation.required(
            item,
            {
                "id", "revision", "title", "statement", "kind", "priority", "status",
                "scope", "scope_reason", "source_refs", "depends_on", "decision_refs",
                "sensitivity_assessment",
            },
            item_path,
        )
        validation.string(item.get("title"), f"{item_path}.title")
        validation.string(item.get("statement"), f"{item_path}.statement")
        revision = item.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
            validation.error(FIELD_INVALID, f"{item_path}.revision", "must be a positive integer")
        kind = validation.enum(item.get("kind"), REQUIREMENT_KINDS, f"{item_path}.kind")
        validation.enum(item.get("priority"), REQUIREMENT_PRIORITIES, f"{item_path}.priority")
        status = validation.enum(item.get("status"), REQUIREMENT_STATUSES, f"{item_path}.status")
        scope = validation.enum(item.get("scope"), SCOPE_STATUSES, f"{item_path}.scope")
        if scope in {"deferred", "out_of_scope"}:
            validation.string(item.get("scope_reason"), f"{item_path}.scope_reason")
        elif scope == "in_scope" and item.get("scope_reason") not in (None, ""):
            validation.error(FIELD_INVALID, f"{item_path}.scope_reason", "must be null for in-scope work")
        if scope == "in_scope" and status != "confirmed":
            validation.error(BLOCKING_DECISION, f"{item_path}.status", "in-scope requirement is not confirmed")
        validation.id_list(item.get("depends_on"), REQ_ID, f"{item_path}.depends_on")
        _decision_ids(validation, item.get("decision_refs"), f"{item_path}.decision_refs")
        assessment = item.get("sensitivity_assessment")
        if not isinstance(assessment, dict):
            validation.error(
                FIELD_INVALID,
                f"{item_path}.sensitivity_assessment",
                "must explicitly assess controlled sensitive categories",
            )
        else:
            validation.required(
                assessment,
                {"categories", "rationale"},
                f"{item_path}.sensitivity_assessment",
            )
            _enum_list(
                validation,
                assessment.get("categories"),
                SENSITIVE_CHANGE_TYPES,
                f"{item_path}.sensitivity_assessment.categories",
            )
            validation.string(
                assessment.get("rationale"),
                f"{item_path}.sensitivity_assessment.rationale",
            )

        refs = item.get("source_refs")
        if not isinstance(refs, list) or not refs:
            validation.error(FIELD_INVALID, f"{item_path}.source_refs", "must contain at least one source reference")
        else:
            seen_paths: set[str] = set()
            for ref_index, ref in enumerate(refs):
                ref_path = f"{item_path}.source_refs[{ref_index}]"
                if not isinstance(ref, dict):
                    validation.error(FIELD_INVALID, ref_path, "must be an object")
                    continue
                validation.required(ref, {"path", "sha256"}, ref_path)
                source_path = _canonical_relative_path(ref.get("path"))
                if source_path is None:
                    validation.error(PATH_INVALID, f"{ref_path}.path", "must be a canonical project-relative path")
                    continue
                if source_path in seen_paths:
                    validation.error(ID_DUPLICATE, f"{ref_path}.path", f"duplicate source reference {source_path}")
                seen_paths.add(source_path)
                digest = ref.get("sha256")
                if not _is_sha256(digest):
                    validation.error(SOURCE_DIGEST_INVALID, f"{ref_path}.sha256", "must be a lowercase SHA-256")
                    continue
                registered = registered_sources.get(source_path)
                if registered is None:
                    validation.error(SOURCE_UNREGISTERED, f"{ref_path}.path", f"source {source_path} is not registered")
                elif registered != digest:
                    validation.error(SOURCE_DIGEST_MISMATCH, f"{ref_path}.sha256", "does not match registered digest")
                actual = actual_sources.get(source_path)
                if actual is not None and actual != digest:
                    validation.error(SOURCE_DIGEST_MISMATCH, f"{ref_path}.sha256", "does not match current source content")
                if "locator" in ref and ref["locator"] is not None and not _nonempty_string(ref["locator"]):
                    validation.error(FIELD_INVALID, f"{ref_path}.locator", "must be null or a non-empty string")

        if kind == "preservation":
            validation.required(
                item,
                {"observable_behavior", "preservation_boundary", "oracle", "evidence_status"},
                item_path,
            )
            behavior = validation.string(item.get("observable_behavior"), f"{item_path}.observable_behavior")
            boundary = validation.string(item.get("preservation_boundary"), f"{item_path}.preservation_boundary")
            oracle = item.get("oracle")
            if behavior is None or boundary is None:
                validation.error(PRESERVATION_NOT_OBSERVABLE, item_path, "preservation must describe observable behavior and boundary")
            if not isinstance(oracle, dict):
                validation.error(PRESERVATION_ORACLE_MISSING, f"{item_path}.oracle", "preservation oracle must be an object")
            else:
                method = validation.string(oracle.get("method"), f"{item_path}.oracle.method")
                validation.string(oracle.get("expected"), f"{item_path}.oracle.expected")
                if method in {"file_exists", "file_hash", "path_exists"}:
                    validation.error(PRESERVATION_NOT_OBSERVABLE, f"{item_path}.oracle.method", "file-only preservation is not a behavior oracle")
            if item.get("evidence_status") not in {"planned", "not_run"}:
                validation.error(ENUM_INVALID, f"{item_path}.evidence_status", "must be planned or not_run in M3")
    _graph_order(validation, indexed, "depends_on", f"{path}.requirements")
    return indexed


def _validate_acceptance(
    validation: _Validation,
    document: Mapping[str, Any],
    path: str,
) -> dict[str, Mapping[str, Any]]:
    _validate_header(validation, document, path, "acceptance-catalog")
    validation.required(document, {"acceptance_criteria"}, path)
    items = _objects(document.get("acceptance_criteria"), f"{path}.acceptance_criteria", validation)
    indexed = _index_items(validation, items, f"{path}.acceptance_criteria", AC_ID)
    for index, item in enumerate(items):
        item_path = f"{path}.acceptance_criteria[{index}]"
        validation.required(
            item,
            {
                "id", "requirement_ids", "criterion", "oracle", "preconditions", "action",
                "expected_outcome", "boundary", "classification", "automation",
                "automation_reason", "risk_coverage", "status",
            },
            item_path,
        )
        validation.id_list(item.get("requirement_ids"), REQ_ID, f"{item_path}.requirement_ids", nonempty=True)
        validation.string(item.get("criterion"), f"{item_path}.criterion")
        validation.string_list(item.get("preconditions"), f"{item_path}.preconditions")
        validation.string(item.get("action"), f"{item_path}.action")
        validation.string(item.get("expected_outcome"), f"{item_path}.expected_outcome")
        validation.enum(item.get("boundary"), ACCEPTANCE_BOUNDARIES, f"{item_path}.boundary")
        classification = validation.enum(item.get("classification"), ACCEPTANCE_CLASSIFICATIONS, f"{item_path}.classification")
        risk_coverage = _enum_list(
            validation,
            item.get("risk_coverage"),
            RISK_COVERAGE_TYPES,
            f"{item_path}.risk_coverage",
        )
        if classification in {"negative", "regression"} and not risk_coverage:
            validation.error(
                FIELD_INVALID,
                f"{item_path}.risk_coverage",
                f"{classification} Acceptance must name the behavior risk it covers",
            )
        automation = validation.enum(item.get("automation"), AUTOMATION_INTENTS, f"{item_path}.automation")
        validation.enum(item.get("status"), M3_STATUSES, f"{item_path}.status")
        if automation == "automated":
            if item.get("automation_reason") not in (None, ""):
                validation.error(FIELD_INVALID, f"{item_path}.automation_reason", "must be null for automated criteria")
        elif automation is not None:
            validation.string(item.get("automation_reason"), f"{item_path}.automation_reason")
        oracle = item.get("oracle")
        if not isinstance(oracle, dict):
            validation.error(ACCEPTANCE_ORACLE_MISSING, f"{item_path}.oracle", "must be an observable oracle object")
        else:
            method = validation.string(oracle.get("method"), f"{item_path}.oracle.method")
            expected = validation.string(oracle.get("expected"), f"{item_path}.oracle.expected")
            if method is None or expected is None:
                validation.error(ACCEPTANCE_ORACLE_MISSING, f"{item_path}.oracle", "must define method and expected result")
    return indexed


def _validate_features(
    validation: _Validation,
    document: Mapping[str, Any],
    path: str,
) -> dict[str, Mapping[str, Any]]:
    _validate_header(validation, document, path, "feature-ledger")
    validation.required(document, {"features"}, path)
    items = _objects(document.get("features"), f"{path}.features", validation)
    indexed = _index_items(validation, items, f"{path}.features", FEATURE_ID)
    for index, item in enumerate(items):
        item_path = f"{path}.features[{index}]"
        validation.required(
            item,
            {
                "id", "title", "outcome", "outcome_key", "requirement_ids",
                "acceptance_ids", "slice_ids", "depends_on", "milestone_id",
                "status", "owner",
            },
            item_path,
        )
        validation.string(item.get("title"), f"{item_path}.title")
        validation.string(item.get("outcome"), f"{item_path}.outcome")
        outcome_key = validation.string(item.get("outcome_key"), f"{item_path}.outcome_key")
        if outcome_key is not None and CHANGE_ID.fullmatch(outcome_key) is None:
            validation.error(ID_INVALID, f"{item_path}.outcome_key", "must be a kebab-case outcome key")
        validation.id_list(item.get("requirement_ids"), REQ_ID, f"{item_path}.requirement_ids", nonempty=True)
        validation.id_list(item.get("acceptance_ids"), AC_ID, f"{item_path}.acceptance_ids", nonempty=True)
        validation.id_list(item.get("slice_ids"), SLICE_ID, f"{item_path}.slice_ids", nonempty=True)
        validation.id_list(item.get("depends_on"), FEATURE_ID, f"{item_path}.depends_on")
        validation.identifier(item.get("milestone_id"), MILESTONE_ID, f"{item_path}.milestone_id")
        validation.enum(item.get("status"), M3_STATUSES, f"{item_path}.status")
        validation.string(item.get("owner"), f"{item_path}.owner")
    _graph_order(validation, indexed, "depends_on", f"{path}.features")
    return indexed


def _validate_roadmap(
    validation: _Validation,
    document: Mapping[str, Any],
    path: str,
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]], str | None]:
    _validate_header(validation, document, path, "delivery-roadmap")
    validation.required(
        document,
        {"project_mode", "milestones", "slices", "next_ready_slice_id", "blocking_questions", "pending_decisions"},
        path,
    )
    validation.enum(document.get("project_mode"), {"greenfield", "brownfield"}, f"{path}.project_mode")
    blocking_questions = validation.id_list(document.get("blocking_questions"), QUESTION_ID, f"{path}.blocking_questions")
    if blocking_questions:
        validation.error(BLOCKING_QUESTION, f"{path}.blocking_questions", "blocking questions must be resolved before planning is ready")
    pending_decisions = _decision_ids(validation, document.get("pending_decisions"), f"{path}.pending_decisions")
    if pending_decisions:
        validation.error(BLOCKING_DECISION, f"{path}.pending_decisions", "pending decisions must be resolved before planning is ready")

    milestone_items = _objects(document.get("milestones"), f"{path}.milestones", validation)
    milestones = _index_items(validation, milestone_items, f"{path}.milestones", MILESTONE_ID)
    seen_milestone_orders: set[int] = set()
    for index, item in enumerate(milestone_items):
        item_path = f"{path}.milestones[{index}]"
        validation.required(item, {"id", "title", "order", "status"}, item_path)
        validation.string(item.get("title"), f"{item_path}.title")
        order = item.get("order")
        if not isinstance(order, int) or isinstance(order, bool) or order <= 0:
            validation.error(FIELD_INVALID, f"{item_path}.order", "must be a positive integer")
        elif order in seen_milestone_orders:
            validation.error(ID_DUPLICATE, f"{item_path}.order", f"duplicate milestone order {order}")
        else:
            seen_milestone_orders.add(order)
        validation.enum(item.get("status"), M3_STATUSES, f"{item_path}.status")

    slice_items = _objects(document.get("slices"), f"{path}.slices", validation)
    slices = _index_items(validation, slice_items, f"{path}.slices", SLICE_ID)
    seen_change_ids: set[str] = set()
    seen_orders: set[int] = set()
    for index, item in enumerate(slice_items):
        item_path = f"{path}.slices[{index}]"
        validation.required(
            item,
            {
                "id", "status", "change_id", "outcome", "outcome_key", "non_goals",
                "requirement_ids", "acceptance_ids", "feature_ids",
                "preservation_requirement_ids", "decision_refs", "depends_on",
                "milestone_id", "order", "risk", "affected_boundaries", "migration",
                "rollback", "sensitive_changes", "qoder_surface", "oversize_exception_pdr", "openspec_state",
            },
            item_path,
        )
        validation.enum(item.get("status"), M3_STATUSES, f"{item_path}.status")
        change_id = validation.identifier(item.get("change_id"), CHANGE_ID, f"{item_path}.change_id")
        if change_id is not None:
            if change_id in seen_change_ids:
                validation.error(ID_DUPLICATE, f"{item_path}.change_id", f"duplicate Change ownership {change_id}")
            seen_change_ids.add(change_id)
        validation.string(item.get("outcome"), f"{item_path}.outcome")
        outcome_key = validation.string(item.get("outcome_key"), f"{item_path}.outcome_key")
        if outcome_key is not None and CHANGE_ID.fullmatch(outcome_key) is None:
            validation.error(ID_INVALID, f"{item_path}.outcome_key", "must be a kebab-case outcome key")
        validation.string_list(item.get("non_goals"), f"{item_path}.non_goals", nonempty=True)
        validation.id_list(item.get("requirement_ids"), REQ_ID, f"{item_path}.requirement_ids", nonempty=True)
        validation.id_list(item.get("acceptance_ids"), AC_ID, f"{item_path}.acceptance_ids", nonempty=True)
        validation.id_list(item.get("feature_ids"), FEATURE_ID, f"{item_path}.feature_ids", nonempty=True)
        validation.id_list(item.get("preservation_requirement_ids"), REQ_ID, f"{item_path}.preservation_requirement_ids")
        _decision_ids(validation, item.get("decision_refs"), f"{item_path}.decision_refs")
        validation.id_list(item.get("depends_on"), SLICE_ID, f"{item_path}.depends_on")
        validation.identifier(item.get("milestone_id"), MILESTONE_ID, f"{item_path}.milestone_id")
        order = item.get("order")
        if not isinstance(order, int) or isinstance(order, bool) or order <= 0:
            validation.error(FIELD_INVALID, f"{item_path}.order", "must be a positive integer")
        elif order in seen_orders:
            validation.error(ID_DUPLICATE, f"{item_path}.order", f"duplicate slice order {order}")
        else:
            seen_orders.add(order)
        validation.enum(item.get("risk"), RISK_LEVELS, f"{item_path}.risk")
        validation.string_list(item.get("affected_boundaries"), f"{item_path}.affected_boundaries", nonempty=True)
        validation.enum(item.get("migration"), MIGRATION_STATES, f"{item_path}.migration")
        validation.enum(item.get("rollback"), MIGRATION_STATES, f"{item_path}.rollback")
        _enum_list(
            validation,
            item.get("sensitive_changes"),
            SENSITIVE_CHANGE_TYPES,
            f"{item_path}.sensitive_changes",
        )
        validation.enum(item.get("qoder_surface"), QODER_SURFACES, f"{item_path}.qoder_surface")
        pdr = item.get("oversize_exception_pdr")
        if pdr is not None and (not isinstance(pdr, str) or PDR_ID.fullmatch(pdr) is None):
            validation.error(ID_INVALID, f"{item_path}.oversize_exception_pdr", "must be null or a PDR-* ID")
        validation.enum(item.get("openspec_state"), CHANGE_STATES, f"{item_path}.openspec_state")

    topological_order, dependencies = _graph_order(validation, slices, "depends_on", f"{path}.slices")
    for identifier, deps in dependencies.items():
        item = slices[identifier]
        current_order = item.get("order")
        current_milestone = milestones.get(item.get("milestone_id"))
        current_milestone_order = current_milestone.get("order") if current_milestone else None
        for dep in deps:
            dep_order = slices[dep].get("order")
            if isinstance(current_order, int) and isinstance(dep_order, int) and dep_order >= current_order:
                validation.error(TOPOLOGY_INVALID, f"{path}.slices.{identifier}.order", f"must be after dependency {dep}")
            dep_milestone = milestones.get(slices[dep].get("milestone_id"))
            dep_milestone_order = dep_milestone.get("order") if dep_milestone else None
            if (
                isinstance(current_milestone_order, int)
                and isinstance(dep_milestone_order, int)
                and dep_milestone_order > current_milestone_order
            ):
                validation.error(TOPOLOGY_INVALID, f"{path}.slices.{identifier}.milestone_id", f"cannot precede dependency {dep}")

    next_ready = document.get("next_ready_slice_id")
    if validation.identifier(next_ready, SLICE_ID, f"{path}.next_ready_slice_id") is not None:
        roots = [identifier for identifier in topological_order if not dependencies.get(identifier)]
        deterministic_next = roots[0] if roots else None
        if next_ready not in slices:
            validation.error(REFERENCE_MISSING, f"{path}.next_ready_slice_id", "references an unknown slice")
        elif deterministic_next is not None and next_ready != deterministic_next:
            validation.error(TOPOLOGY_INVALID, f"{path}.next_ready_slice_id", f"must be deterministic next-ready slice {deterministic_next}")
    return milestones, slices, next_ready if isinstance(next_ready, str) else None


def _validate_contracts(
    validation: _Validation,
    raw_contracts: Sequence[tuple[str, Mapping[str, Any]]],
) -> dict[str, tuple[str, Mapping[str, Any]]]:
    result: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for relative, contract in raw_contracts:
        _validate_header(validation, contract, relative, "delivery-contract")
        validation.required(
            contract,
            {
                "change_id", "slice_id", "source_digest", "requirement_ids", "acceptance_ids",
                "feature_ids", "outcome", "scope", "non_goals", "preservation_requirement_ids",
                "risk", "affected_components", "compatibility_constraints", "migration", "rollback",
                "sensitive_changes", "decision_refs", "unresolved_questions", "planned_checks", "required_gates",
                "evidence_classes", "repair_budget", "human_approvals", "integration_owner",
                "git_binding", "openspec_binding", "delivery_state", "implementation_authorized",
                "openspec_apply_authorized", "m4_lock",
            },
            relative,
        )
        change_id = validation.identifier(contract.get("change_id"), CHANGE_ID, f"{relative}.change_id")
        filename_change = PurePosixPath(relative).stem
        if change_id is not None and change_id != filename_change:
            validation.error(REFERENCE_MISMATCH, f"{relative}.change_id", "must match the contract filename")
        if change_id is not None:
            if change_id in result:
                validation.error(CONTRACT_DUPLICATE, relative, f"duplicate contract for {change_id}")
            else:
                result[change_id] = (relative, contract)
        validation.identifier(contract.get("slice_id"), SLICE_ID, f"{relative}.slice_id")
        if not _is_sha256(contract.get("source_digest")):
            validation.error(SOURCE_DIGEST_INVALID, f"{relative}.source_digest", "must be a lowercase SHA-256")
        validation.id_list(contract.get("requirement_ids"), REQ_ID, f"{relative}.requirement_ids", nonempty=True)
        validation.id_list(contract.get("acceptance_ids"), AC_ID, f"{relative}.acceptance_ids", nonempty=True)
        validation.id_list(contract.get("feature_ids"), FEATURE_ID, f"{relative}.feature_ids", nonempty=True)
        validation.string(contract.get("outcome"), f"{relative}.outcome")
        validation.string_list(contract.get("scope"), f"{relative}.scope", nonempty=True)
        validation.string_list(contract.get("non_goals"), f"{relative}.non_goals", nonempty=True)
        validation.id_list(contract.get("preservation_requirement_ids"), REQ_ID, f"{relative}.preservation_requirement_ids")
        validation.enum(contract.get("risk"), RISK_LEVELS, f"{relative}.risk")
        validation.string_list(contract.get("affected_components"), f"{relative}.affected_components", nonempty=True)
        validation.string_list(contract.get("compatibility_constraints"), f"{relative}.compatibility_constraints")
        validation.enum(contract.get("migration"), MIGRATION_STATES, f"{relative}.migration")
        validation.enum(contract.get("rollback"), MIGRATION_STATES, f"{relative}.rollback")
        _enum_list(
            validation,
            contract.get("sensitive_changes"),
            SENSITIVE_CHANGE_TYPES,
            f"{relative}.sensitive_changes",
        )
        _decision_ids(validation, contract.get("decision_refs"), f"{relative}.decision_refs")
        unresolved = validation.id_list(contract.get("unresolved_questions"), QUESTION_ID, f"{relative}.unresolved_questions")
        if unresolved:
            validation.error(BLOCKING_QUESTION, f"{relative}.unresolved_questions", "contract has unresolved questions")

        checks = _objects(contract.get("planned_checks"), f"{relative}.planned_checks", validation)
        check_ids: set[str] = set()
        for index, check in enumerate(checks):
            check_path = f"{relative}.planned_checks[{index}]"
            validation.required(check, {"id", "level", "status", "acceptance_ids"}, check_path)
            check_id = validation.identifier(check.get("id"), CHECK_ID, f"{check_path}.id")
            if check_id in check_ids:
                validation.error(ID_DUPLICATE, f"{check_path}.id", f"duplicate check ID {check_id}")
            elif check_id:
                check_ids.add(check_id)
            validation.enum(check.get("level"), CHECK_LEVELS, f"{check_path}.level")
            status = validation.enum(check.get("status"), CHECK_STATUSES, f"{check_path}.status")
            if status != "planned":
                validation.error(M4_LOCK_VIOLATION, f"{check_path}.status", "M3 checks cannot claim execution")
            validation.id_list(check.get("acceptance_ids"), AC_ID, f"{check_path}.acceptance_ids", nonempty=True)
        gates = contract.get("required_gates")
        if not isinstance(gates, list) or not gates:
            validation.error(FIELD_INVALID, f"{relative}.required_gates", "must list planned gate levels")
        else:
            for index, gate in enumerate(gates):
                validation.enum(gate, GATE_LEVELS, f"{relative}.required_gates[{index}]")
        validation.string_list(contract.get("evidence_classes"), f"{relative}.evidence_classes", nonempty=True)
        repair = contract.get("repair_budget")
        if not isinstance(repair, dict):
            validation.error(FIELD_INVALID, f"{relative}.repair_budget", "must be an object")
        else:
            validation.required(repair, {"max_attempts", "max_no_progress_attempts"}, f"{relative}.repair_budget")
            for key in ("max_attempts", "max_no_progress_attempts"):
                value = repair.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    validation.error(FIELD_INVALID, f"{relative}.repair_budget.{key}", "must be a positive integer")
            if (
                isinstance(repair.get("max_attempts"), int)
                and isinstance(repair.get("max_no_progress_attempts"), int)
                and repair["max_no_progress_attempts"] > repair["max_attempts"]
            ):
                validation.error(FIELD_INVALID, f"{relative}.repair_budget", "no-progress budget cannot exceed total attempts")
        validation.string_list(contract.get("human_approvals"), f"{relative}.human_approvals")
        if not isinstance(contract.get("integration_owner"), bool):
            validation.error(FIELD_INVALID, f"{relative}.integration_owner", "must be boolean")
        if not isinstance(contract.get("git_binding"), dict):
            validation.error(BINDING_INVALID, f"{relative}.git_binding", "must be an object")
        open_binding = contract.get("openspec_binding")
        if not isinstance(open_binding, dict):
            validation.error(OPENSPEC_BINDING_INVALID, f"{relative}.openspec_binding", "must be an object")
        else:
            validation.required(
                open_binding,
                {"change_id", "state", "artifact_digest"},
                f"{relative}.openspec_binding",
            )
            validation.identifier(
                open_binding.get("change_id"),
                CHANGE_ID,
                f"{relative}.openspec_binding.change_id",
            )
            binding_state = validation.enum(
                open_binding.get("state"),
                CHANGE_STATES,
                f"{relative}.openspec_binding.state",
            )
            binding_digest = open_binding.get("artifact_digest")
            if binding_state == "materialized" and not _is_sha256(binding_digest):
                validation.error(
                    OPENSPEC_BINDING_INVALID,
                    f"{relative}.openspec_binding.artifact_digest",
                    "materialized Change must bind a lowercase SHA-256",
                )
            if binding_state == "reserved" and binding_digest not in (None, ""):
                validation.error(
                    OPENSPEC_JIT_VIOLATION,
                    f"{relative}.openspec_binding.artifact_digest",
                    "reserved Change cannot bind materialized artifacts",
                )
        if contract.get("delivery_state") != "planned":
            validation.error(M4_LOCK_VIOLATION, f"{relative}.delivery_state", "must remain planned in M3")
        for key in ("implementation_authorized", "openspec_apply_authorized"):
            if contract.get(key) is not False:
                validation.error(M4_LOCK_VIOLATION, f"{relative}.{key}", "must be false until M4")
        if contract.get("m4_lock") is not True:
            validation.error(M4_LOCK_VIOLATION, f"{relative}.m4_lock", "must be true in M3")
    return result


def _validate_decision_bindings(
    validation: _Validation,
    root: Path,
    decisions: Mapping[str, Any],
) -> set[str]:
    """Validate the exact ADR/PDR authority set and current document bytes."""

    validation.required(
        decisions,
        {"approved", "pending", "blocking", "artifacts", "authority_digest"},
        "external_bindings.decisions",
    )
    states: dict[str, str] = {}
    ids_by_state: dict[str, list[str]] = {}
    for state in ("approved", "pending", "blocking"):
        identifiers = _decision_ids(
            validation,
            decisions.get(state),
            f"external_bindings.decisions.{state}",
        )
        ids_by_state[state] = identifiers
        for identifier in identifiers:
            previous = states.get(identifier)
            if previous is not None:
                validation.error(
                    DECISION_STATE_MISMATCH,
                    f"external_bindings.decisions.{state}",
                    f"{identifier} appears in both {previous} and {state}",
                )
            else:
                states[identifier] = state

    raw_artifacts = decisions.get("artifacts")
    normalized: dict[str, dict[str, str]] = {}
    if not isinstance(raw_artifacts, dict):
        validation.error(
            DECISION_ARTIFACT_INVALID,
            "external_bindings.decisions.artifacts",
            "must be an object keyed by decision ID",
        )
        raw_artifacts = {}
    if len(raw_artifacts) > MAX_DECISION_ARTIFACTS:
        validation.error(
            PATH_OVERSIZE,
            "external_bindings.decisions.artifacts",
            f"contains more than {MAX_DECISION_ARTIFACTS} decision artifacts",
        )

    expected_ids = set(states)
    artifact_ids = set(raw_artifacts)
    missing = sorted(expected_ids - artifact_ids)
    extra = sorted(
        str(identifier) for identifier in artifact_ids - expected_ids
    )
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unexpected {', '.join(extra)}")
        validation.error(
            DECISION_ARTIFACT_INVALID,
            "external_bindings.decisions.artifacts",
            "must exactly cover all decision states: " + "; ".join(details),
        )

    total_bytes = 0
    for raw_identifier, artifact in sorted(raw_artifacts.items(), key=lambda item: str(item[0]))[:MAX_DECISION_ARTIFACTS]:
        item_path = f"external_bindings.decisions.artifacts.{raw_identifier}"
        if not isinstance(raw_identifier, str) or not (
            ADR_ID.fullmatch(raw_identifier) or PDR_ID.fullmatch(raw_identifier)
        ):
            validation.error(ID_INVALID, item_path, "artifact key must be an ADR-* or PDR-* ID")
            continue
        if not isinstance(artifact, dict):
            validation.error(DECISION_ARTIFACT_INVALID, item_path, "must be an object")
            continue
        required_fields = {"path", "sha256", "status"}
        actual_fields = set(artifact)
        if actual_fields != required_fields:
            missing_fields = sorted(required_fields - actual_fields)
            extra_fields = sorted(actual_fields - required_fields)
            detail = []
            if missing_fields:
                detail.append(f"missing {', '.join(missing_fields)}")
            if extra_fields:
                detail.append(f"unknown {', '.join(extra_fields)}")
            validation.error(
                DECISION_ARTIFACT_INVALID,
                item_path,
                "must contain exactly path, sha256, and status: " + "; ".join(detail),
            )
        raw_path = artifact.get("path")
        relative = _canonical_relative_path(raw_path)
        if relative is None:
            validation.error(PATH_INVALID, f"{item_path}.path", "must be a canonical project-relative Markdown path")
            continue
        pure = PurePosixPath(relative)
        expected_parent = (
            "delivery-docs/decisions/adr"
            if ADR_ID.fullmatch(raw_identifier)
            else "delivery-docs/decisions/product"
        )
        if pure.parent.as_posix() != expected_parent or pure.suffix.lower() != ".md":
            validation.error(
                DECISION_ARTIFACT_INVALID,
                f"{item_path}.path",
                f"must be a Markdown file directly under {expected_parent}/",
            )
            continue
        digest = artifact.get("sha256")
        if not _is_sha256(digest):
            validation.error(
                SOURCE_DIGEST_INVALID,
                f"{item_path}.sha256",
                "must be a lowercase SHA-256",
            )
            continue
        status = artifact.get("status")
        if status not in DECISION_STATUSES:
            validation.error(
                ENUM_INVALID,
                f"{item_path}.status",
                f"must be one of {sorted(DECISION_STATUSES)}",
            )
            continue
        expected_state = states.get(raw_identifier)
        if expected_state is not None and status != expected_state:
            validation.error(
                DECISION_STATE_MISMATCH,
                f"{item_path}.status",
                f"must be {expected_state!r} to match its decision state",
            )
        normalized[raw_identifier] = {
            "path": relative,
            "sha256": digest,
            "status": status,
        }
        try:
            data = _read_regular_bytes(root, relative, max_bytes=MAX_DECISION_BYTES)
        except _ReadFailure as exc:
            validation.error(exc.code, relative, str(exc))
            continue
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_DECISION_BYTES:
            validation.error(
                PATH_OVERSIZE,
                relative,
                f"decision documents exceed the {MAX_TOTAL_DECISION_BYTES}-byte total budget",
            )
            break
        actual_digest = _sha256_bytes(data)
        if actual_digest != digest:
            validation.error(
                DECISION_DIGEST_MISMATCH,
                relative,
                f"current document digest does not match {raw_identifier}",
            )

    expected_authority_digest = canonical_json_digest(dict(sorted(normalized.items())))
    authority_digest = decisions.get("authority_digest")
    if not _is_sha256(authority_digest) or authority_digest != expected_authority_digest:
        validation.error(
            DECISION_AUTHORITY_DIGEST_MISMATCH,
            "external_bindings.decisions.authority_digest",
            "must equal the canonical digest of the exact decision artifact map",
        )

    pending = ids_by_state.get("pending", [])
    blocking = ids_by_state.get("blocking", [])
    if pending:
        validation.error(
            BLOCKING_DECISION,
            "external_bindings.decisions.pending",
            "pending decisions prevent planning readiness",
        )
    if blocking:
        validation.error(
            BLOCKING_DECISION,
            "external_bindings.decisions.blocking",
            "blocking decisions prevent planning readiness",
        )
    return set(ids_by_state.get("approved", []))


def _validate_external_bindings(
    validation: _Validation,
    root: Path,
    external: Mapping[str, Any],
    source_digests: dict[str, str],
) -> tuple[dict[str, str], dict[str, Any], dict[str, Mapping[str, Any]], set[str], str | None]:
    validation.required(external, {"schema_version", "project_mode", "registered_sources", "decisions", "git", "openspec"}, "external_bindings")
    if external.get("schema_version") != PLANNING_SCHEMA_VERSION:
        validation.error(SCHEMA_UNSUPPORTED, "external_bindings.schema_version", f"must be {PLANNING_SCHEMA_VERSION}")
    project_mode = validation.enum(external.get("project_mode"), {"greenfield", "brownfield"}, "external_bindings.project_mode")
    registered_raw = external.get("registered_sources")
    registered: dict[str, str] = {}
    if not isinstance(registered_raw, dict) or not registered_raw:
        validation.error(BINDING_INVALID, "external_bindings.registered_sources", "must be a non-empty object of path to SHA-256")
    else:
        if len(registered_raw) > MAX_REGISTERED_SOURCES:
            validation.error(
                PATH_OVERSIZE,
                "external_bindings.registered_sources",
                f"contains more than {MAX_REGISTERED_SOURCES} sources",
            )
        source_bytes = 0
        for raw_path, digest in sorted(registered_raw.items())[:MAX_REGISTERED_SOURCES]:
            canonical = _canonical_relative_path(raw_path)
            if canonical is None:
                validation.error(PATH_INVALID, f"external_bindings.registered_sources.{raw_path}", "source path is not canonical and project-relative")
                continue
            if not _is_sha256(digest):
                validation.error(SOURCE_DIGEST_INVALID, f"external_bindings.registered_sources.{canonical}", "must be a lowercase SHA-256")
                continue
            registered[canonical] = digest
            try:
                data = _read_regular_bytes(root, canonical, max_bytes=MAX_SOURCE_BYTES)
            except _ReadFailure as exc:
                validation.error(exc.code, canonical, str(exc))
                continue
            source_bytes += len(data)
            if source_bytes > MAX_TOTAL_SOURCE_BYTES:
                validation.error(
                    PATH_OVERSIZE,
                    canonical,
                    f"registered sources exceed the {MAX_TOTAL_SOURCE_BYTES}-byte total budget",
                )
                break
            actual = _sha256_bytes(data)
            source_digests[canonical] = actual
            if actual != digest:
                validation.error(SOURCE_DIGEST_MISMATCH, canonical, "registered digest does not match current source content")

    decisions = external.get("decisions")
    approved: set[str] = set()
    if not isinstance(decisions, dict):
        validation.error(BINDING_INVALID, "external_bindings.decisions", "must be an object")
    else:
        approved.update(_validate_decision_bindings(validation, root, decisions))

    git = external.get("git")
    git_binding: dict[str, Any] = {}
    if not isinstance(git, dict):
        validation.error(BINDING_INVALID, "external_bindings.git", "must be an opaque Git binding object")
    else:
        validation.required(
            git,
            {
                "repository_scope_id", "checkout_scope_id", "checkout_kind", "head", "baseline",
                "ref", "dirty_digest", "excluded_paths_digest", "integration_owner",
            },
            "external_bindings.git",
        )
        repository_scope_id = git.get("repository_scope_id")
        if not isinstance(repository_scope_id, str) or REPOSITORY_SCOPE_ID.fullmatch(repository_scope_id) is None:
            validation.error(
                BINDING_INVALID,
                "external_bindings.git.repository_scope_id",
                "must be repo- followed by a lowercase SHA-256",
            )
        checkout_scope_id = git.get("checkout_scope_id")
        if not isinstance(checkout_scope_id, str) or CHECKOUT_SCOPE_ID.fullmatch(checkout_scope_id) is None:
            validation.error(
                BINDING_INVALID,
                "external_bindings.git.checkout_scope_id",
                "must be checkout- followed by a lowercase SHA-256",
            )
        if not _is_sha256(git.get("dirty_digest")):
            validation.error(
                BINDING_INVALID,
                "external_bindings.git.dirty_digest",
                "must be a lowercase SHA-256",
            )
        if not _is_sha256(git.get("excluded_paths_digest")):
            validation.error(
                BINDING_INVALID,
                "external_bindings.git.excluded_paths_digest",
                "must be a lowercase SHA-256",
            )
        validation.enum(git.get("checkout_kind"), {"main", "linked"}, "external_bindings.git.checkout_kind")
        for key in ("head", "baseline"):
            if not isinstance(git.get(key), str) or GIT_OID.fullmatch(git[key]) is None:
                validation.error(BINDING_INVALID, f"external_bindings.git.{key}", "must be a full lowercase Git object ID")
        if git.get("ref") is not None and not _nonempty_string(git.get("ref")):
            validation.error(BINDING_INVALID, "external_bindings.git.ref", "must be null or a non-empty ref")
        if not isinstance(git.get("integration_owner"), bool):
            validation.error(BINDING_INVALID, "external_bindings.git.integration_owner", "must be boolean")
        if git.get("checkout_kind") == "linked" and git.get("integration_owner") is not False:
            validation.error(BINDING_INVALID, "external_bindings.git.integration_owner", "linked Worktree must not claim integration ownership in M3")
        git_binding = dict(git)

    openspec = external.get("openspec")
    changes: dict[str, Mapping[str, Any]] = {}
    if not isinstance(openspec, dict):
        validation.error(OPENSPEC_BINDING_INVALID, "external_bindings.openspec", "must be an object")
    else:
        validation.required(
            openspec,
            {
                "schema", "config_digest", "tree_digest", "cli_version",
                "executable_digest", "changes",
            },
            "external_bindings.openspec",
        )
        validation.string(openspec.get("schema"), "external_bindings.openspec.schema")
        for key in ("config_digest", "tree_digest", "executable_digest"):
            if not _is_sha256(openspec.get(key)):
                validation.error(
                    OPENSPEC_BINDING_INVALID,
                    f"external_bindings.openspec.{key}",
                    "must be a lowercase SHA-256",
                )
        validation.string(openspec.get("cli_version"), "external_bindings.openspec.cli_version")
        raw_changes = openspec.get("changes")
        if not isinstance(raw_changes, dict):
            validation.error(OPENSPEC_BINDING_INVALID, "external_bindings.openspec.changes", "must be an object keyed by Change ID")
        else:
            for change_id, value in sorted(raw_changes.items()):
                if not isinstance(change_id, str) or CHANGE_ID.fullmatch(change_id) is None:
                    validation.error(ID_INVALID, f"external_bindings.openspec.changes.{change_id}", "invalid kebab-case Change ID")
                    continue
                if not isinstance(value, dict):
                    validation.error(OPENSPEC_BINDING_INVALID, f"external_bindings.openspec.changes.{change_id}", "must be an object")
                    continue
                state = validation.enum(value.get("state"), CHANGE_STATES, f"external_bindings.openspec.changes.{change_id}.state")
                if state == "materialized":
                    validation.required(
                        value,
                        {
                            "metadata_valid",
                            "strict_valid",
                            "tasks_checked",
                            "artifact_digest",
                            "runtime_artifacts_digest",
                            "runtime_instructions_digest",
                            "requirement_ids",
                            "acceptance_ids",
                        },
                        f"external_bindings.openspec.changes.{change_id}",
                    )
                    if value.get("metadata_valid") is not True or value.get("strict_valid") is not True:
                        validation.error(OPENSPEC_BINDING_INVALID, f"external_bindings.openspec.changes.{change_id}", "materialized Change must have valid metadata and strict validation")
                    if value.get("tasks_checked") is not False:
                        validation.error(M4_LOCK_VIOLATION, f"external_bindings.openspec.changes.{change_id}.tasks_checked", "M3 OpenSpec tasks must remain unchecked")
                    if not _is_sha256(value.get("artifact_digest")):
                        validation.error(OPENSPEC_BINDING_INVALID, f"external_bindings.openspec.changes.{change_id}.artifact_digest", "must bind materialized artifacts")
                    for digest_key in ("runtime_artifacts_digest", "runtime_instructions_digest"):
                        if not _is_sha256(value.get(digest_key)):
                            validation.error(
                                OPENSPEC_BINDING_INVALID,
                                f"external_bindings.openspec.changes.{change_id}.{digest_key}",
                                "must bind the runtime OpenSpec artifact contract",
                            )
                    validation.id_list(value.get("requirement_ids"), REQ_ID, f"external_bindings.openspec.changes.{change_id}.requirement_ids", nonempty=True)
                    validation.id_list(value.get("acceptance_ids"), AC_ID, f"external_bindings.openspec.changes.{change_id}.acceptance_ids", nonempty=True)
                elif state == "reserved":
                    for digest_key in (
                        "artifact_digest",
                        "runtime_artifacts_digest",
                        "runtime_instructions_digest",
                    ):
                        if value.get(digest_key) not in (None, ""):
                            validation.error(
                                OPENSPEC_JIT_VIOLATION,
                                f"external_bindings.openspec.changes.{change_id}.{digest_key}",
                                "reserved Change cannot claim materialized artifacts",
                            )
                changes[change_id] = value

    return registered, git_binding, changes, approved, project_mode


def _all_decision_refs(
    requirements: Mapping[str, Mapping[str, Any]],
    slices: Mapping[str, Mapping[str, Any]],
    contracts: Mapping[str, tuple[str, Mapping[str, Any]]],
) -> set[str]:
    result: set[str] = set()
    for item in requirements.values():
        if isinstance(item.get("decision_refs"), list):
            result.update(value for value in item["decision_refs"] if isinstance(value, str))
    for item in slices.values():
        if isinstance(item.get("decision_refs"), list):
            result.update(value for value in item["decision_refs"] if isinstance(value, str))
        if isinstance(item.get("oversize_exception_pdr"), str):
            result.add(item["oversize_exception_pdr"])
    for _, item in contracts.values():
        if isinstance(item.get("decision_refs"), list):
            result.update(value for value in item["decision_refs"] if isinstance(value, str))
    return result


def _cross_validate(
    validation: _Validation,
    *,
    project_mode: str | None,
    roadmap_mode: Any,
    requirements: Mapping[str, Mapping[str, Any]],
    acceptance: Mapping[str, Mapping[str, Any]],
    features: Mapping[str, Mapping[str, Any]],
    milestones: Mapping[str, Mapping[str, Any]],
    slices: Mapping[str, Mapping[str, Any]],
    next_ready: str | None,
    contracts: Mapping[str, tuple[str, Mapping[str, Any]]],
    source_digest: str | None,
    git_binding: Mapping[str, Any],
    openspec_changes: Mapping[str, Mapping[str, Any]],
    approved_decisions: set[str],
    roadmap_path: str,
    readiness: str,
) -> tuple[list[str], list[str]]:
    if project_mode is not None and roadmap_mode != project_mode:
        validation.error(REFERENCE_MISMATCH, f"{roadmap_path}.project_mode", "must match external project mode")
    if project_mode == "brownfield" and not any(item.get("kind") == "preservation" and item.get("scope") == "in_scope" for item in requirements.values()):
        validation.error(PRESERVATION_REQUIRED, "requirements", "Brownfield planning requires observable Preservation Requirements")

    in_scope = {identifier for identifier, item in requirements.items() if item.get("scope") == "in_scope"}
    excluded = set(requirements) - in_scope
    req_to_ac: dict[str, set[str]] = {identifier: set() for identifier in requirements}
    ac_to_features: dict[str, set[str]] = {identifier: set() for identifier in acceptance}
    req_to_features: dict[str, set[str]] = {identifier: set() for identifier in requirements}
    feature_to_slices: dict[str, set[str]] = {identifier: set() for identifier in features}
    actual_feature_slices: dict[str, set[str]] = {identifier: set() for identifier in features}
    req_to_slices: dict[str, set[str]] = {identifier: set() for identifier in requirements}
    ac_to_slices: dict[str, set[str]] = {identifier: set() for identifier in acceptance}

    for ac_id, item in acceptance.items():
        requirement_ids = item.get("requirement_ids", []) if isinstance(item.get("requirement_ids"), list) else []
        for req_id in requirement_ids:
            if req_id not in requirements:
                validation.error(REFERENCE_MISSING, f"acceptance.{ac_id}.requirement_ids", f"unknown Requirement {req_id}")
            else:
                req_to_ac[req_id].add(ac_id)
                if req_id in excluded:
                    validation.error(SCOPE_LEAK, f"acceptance.{ac_id}.requirement_ids", f"excluded Requirement {req_id} leaked into acceptance")
        if not requirement_ids:
            validation.error(ACCEPTANCE_ORPHAN, f"acceptance.{ac_id}", "Acceptance must reference at least one Requirement")

    for feature_id, item in features.items():
        req_ids = item.get("requirement_ids", []) if isinstance(item.get("requirement_ids"), list) else []
        ac_ids = item.get("acceptance_ids", []) if isinstance(item.get("acceptance_ids"), list) else []
        slice_ids = item.get("slice_ids", []) if isinstance(item.get("slice_ids"), list) else []
        if item.get("milestone_id") not in milestones:
            validation.error(REFERENCE_MISSING, f"features.{feature_id}.milestone_id", "unknown milestone")
        for req_id in req_ids:
            if req_id not in requirements:
                validation.error(REFERENCE_MISSING, f"features.{feature_id}.requirement_ids", f"unknown Requirement {req_id}")
            else:
                req_to_features[req_id].add(feature_id)
                if req_id in excluded:
                    validation.error(SCOPE_LEAK, f"features.{feature_id}.requirement_ids", f"excluded Requirement {req_id} leaked into Feature")
        for ac_id in ac_ids:
            if ac_id not in acceptance:
                validation.error(REFERENCE_MISSING, f"features.{feature_id}.acceptance_ids", f"unknown Acceptance {ac_id}")
            else:
                ac_to_features[ac_id].add(feature_id)
                required = set(acceptance[ac_id].get("requirement_ids", []))
                if not required.issubset(set(req_ids)):
                    validation.error(REFERENCE_MISMATCH, f"features.{feature_id}.acceptance_ids", f"Acceptance {ac_id} Requirements are not covered by Feature")
        for slice_id in slice_ids:
            if slice_id not in slices:
                validation.error(REFERENCE_MISSING, f"features.{feature_id}.slice_ids", f"unknown slice {slice_id}")
            else:
                feature_to_slices[feature_id].add(slice_id)
        if not req_ids or not ac_ids or not slice_ids:
            validation.error(FEATURE_ORPHAN, f"features.{feature_id}", "Feature must connect Requirements, Acceptance, and slices")

    slice_change_ids: list[str] = []
    for slice_id, item in slices.items():
        req_ids = set(item.get("requirement_ids", [])) if isinstance(item.get("requirement_ids"), list) else set()
        ac_ids = set(item.get("acceptance_ids", [])) if isinstance(item.get("acceptance_ids"), list) else set()
        feature_ids = set(item.get("feature_ids", [])) if isinstance(item.get("feature_ids"), list) else set()
        preservation_ids = set(item.get("preservation_requirement_ids", [])) if isinstance(item.get("preservation_requirement_ids"), list) else set()
        if item.get("milestone_id") not in milestones:
            validation.error(REFERENCE_MISSING, f"slices.{slice_id}.milestone_id", "unknown milestone")
        feature_req_union: set[str] = set()
        feature_ac_union: set[str] = set()
        outcome_keys: set[str] = set()
        for feature_id in feature_ids:
            feature = features.get(feature_id)
            if feature is None:
                validation.error(REFERENCE_MISSING, f"slices.{slice_id}.feature_ids", f"unknown Feature {feature_id}")
                continue
            actual_feature_slices[feature_id].add(slice_id)
            if slice_id not in feature.get("slice_ids", []):
                validation.error(REFERENCE_MISMATCH, f"slices.{slice_id}.feature_ids", f"Feature {feature_id} does not map back to slice")
            if feature.get("milestone_id") != item.get("milestone_id"):
                validation.error(
                    REFERENCE_MISMATCH,
                    f"slices.{slice_id}.milestone_id",
                    f"must match Feature {feature_id} milestone",
                )
            feature_req_union.update(feature.get("requirement_ids", []))
            feature_ac_union.update(feature.get("acceptance_ids", []))
            if isinstance(feature.get("outcome_key"), str):
                outcome_keys.add(feature["outcome_key"])
        if req_ids != feature_req_union:
            validation.error(REFERENCE_MISMATCH, f"slices.{slice_id}.requirement_ids", "must equal the Requirements mapped through its Features")
        if ac_ids != feature_ac_union:
            validation.error(REFERENCE_MISMATCH, f"slices.{slice_id}.acceptance_ids", "must equal the Acceptance mapped through its Features")
        if len(outcome_keys) > 1 or (outcome_keys and item.get("outcome_key") not in outcome_keys):
            validation.error(CATCH_ALL_SLICE, f"slices.{slice_id}.outcome_key", "slice combines unrelated Feature outcomes")
        for req_id in req_ids:
            if req_id not in requirements:
                validation.error(REFERENCE_MISSING, f"slices.{slice_id}.requirement_ids", f"unknown Requirement {req_id}")
            else:
                req_to_slices[req_id].add(slice_id)
                if req_id in excluded:
                    validation.error(SCOPE_LEAK, f"slices.{slice_id}.requirement_ids", f"excluded Requirement {req_id} leaked into slice")
        for ac_id in ac_ids:
            if ac_id not in acceptance:
                validation.error(REFERENCE_MISSING, f"slices.{slice_id}.acceptance_ids", f"unknown Acceptance {ac_id}")
            else:
                ac_to_slices[ac_id].add(slice_id)
        for req_id in preservation_ids:
            req = requirements.get(req_id)
            if req is None:
                validation.error(REFERENCE_MISSING, f"slices.{slice_id}.preservation_requirement_ids", f"unknown Requirement {req_id}")
            elif req.get("kind") != "preservation":
                validation.error(REFERENCE_MISMATCH, f"slices.{slice_id}.preservation_requirement_ids", f"{req_id} is not a preservation Requirement")
            if req_id not in req_ids:
                validation.error(REFERENCE_MISMATCH, f"slices.{slice_id}.preservation_requirement_ids", f"{req_id} must also be in requirement_ids")
        expected_preservation_ids = {
            req_id
            for req_id in req_ids
            if requirements.get(req_id, {}).get("kind") == "preservation"
        }
        if preservation_ids != expected_preservation_ids:
            validation.error(
                REFERENCE_MISMATCH,
                f"slices.{slice_id}.preservation_requirement_ids",
                "must exactly identify preservation Requirements in the slice",
            )
        oversized = len(req_ids) > MAX_REQUIREMENTS_PER_SLICE or len(ac_ids) > MAX_ACCEPTANCE_PER_SLICE
        if oversized:
            pdr = item.get("oversize_exception_pdr")
            detail = f"contains {len(req_ids)} Requirements and {len(ac_ids)} Acceptance criteria"
            if isinstance(pdr, str) and PDR_ID.fullmatch(pdr) is not None and pdr in approved_decisions:
                validation.warning(SLICE_OVERSIZED, f"slices.{slice_id}", f"{detail}; approved by {pdr}")
            else:
                validation.error(SLICE_OVERSIZED, f"slices.{slice_id}", detail)
                validation.error(OVERSIZE_PDR_REQUIRED, f"slices.{slice_id}.oversize_exception_pdr", "oversized slice requires an approved PDR")
        change_id = item.get("change_id")
        if isinstance(change_id, str):
            slice_change_ids.append(change_id)
            contract_entry = contracts.get(change_id)
            if contract_entry is None:
                validation.error(CONTRACT_MISSING, f"slices.{slice_id}.change_id", f"missing Delivery Contract for {change_id}")
                continue
            contract_path, contract = contract_entry
            if contract.get("slice_id") != slice_id:
                validation.error(REFERENCE_MISMATCH, f"{contract_path}.slice_id", f"must bind {slice_id}")
            exact_fields = {
                "requirement_ids": req_ids,
                "acceptance_ids": ac_ids,
                "feature_ids": feature_ids,
                "preservation_requirement_ids": preservation_ids,
            }
            for key, expected in exact_fields.items():
                actual = set(contract.get(key, [])) if isinstance(contract.get(key), list) else set()
                if actual != expected:
                    validation.error(REFERENCE_MISMATCH, f"{contract_path}.{key}", f"must exactly match slice {slice_id}")
            for key in ("outcome", "risk", "migration", "rollback"):
                if contract.get(key) != item.get(key):
                    validation.error(REFERENCE_MISMATCH, f"{contract_path}.{key}", f"must match slice {slice_id}")
            slice_sensitive = set(item.get("sensitive_changes", [])) if isinstance(item.get("sensitive_changes"), list) else set()
            contract_sensitive = set(contract.get("sensitive_changes", [])) if isinstance(contract.get("sensitive_changes"), list) else set()
            if contract_sensitive != slice_sensitive:
                validation.error(
                    REFERENCE_MISMATCH,
                    f"{contract_path}.sensitive_changes",
                    f"must exactly match slice {slice_id}",
                )
            if set(contract.get("non_goals", [])) != set(item.get("non_goals", [])):
                validation.error(
                    REFERENCE_MISMATCH,
                    f"{contract_path}.non_goals",
                    f"must exactly match slice {slice_id}",
                )
            if set(contract.get("decision_refs", [])) != set(item.get("decision_refs", [])):
                validation.error(
                    REFERENCE_MISMATCH,
                    f"{contract_path}.decision_refs",
                    f"must exactly match slice {slice_id}",
                )
            semantic_inputs: list[str] = []
            inferred_sensitive: set[str] = set()
            for value in (item.get("outcome"), contract.get("outcome")):
                if isinstance(value, str):
                    semantic_inputs.append(value)
            for owner, key in (
                (item, "affected_boundaries"),
                (contract, "affected_components"),
                (contract, "scope"),
                (contract, "compatibility_constraints"),
            ):
                values = owner.get(key)
                if isinstance(values, list):
                    semantic_inputs.extend(value for value in values if isinstance(value, str))
            for req_id in req_ids:
                requirement = requirements.get(req_id, {})
                assessment = requirement.get("sensitivity_assessment")
                if isinstance(assessment, dict) and isinstance(assessment.get("categories"), list):
                    inferred_sensitive.update(
                        value
                        for value in assessment["categories"]
                        if isinstance(value, str) and value in SENSITIVE_CHANGE_TYPES
                    )
                for key in ("title", "statement", "observable_behavior", "preservation_boundary"):
                    if isinstance(requirement.get(key), str):
                        semantic_inputs.append(requirement[key])
            for ac_id in ac_ids:
                criterion = acceptance.get(ac_id, {})
                for key in ("criterion", "action", "expected_outcome"):
                    if isinstance(criterion.get(key), str):
                        semantic_inputs.append(criterion[key])
            for feature_id in feature_ids:
                feature = features.get(feature_id, {})
                for key in ("title", "outcome"):
                    if isinstance(feature.get(key), str):
                        semantic_inputs.append(feature[key])
            inferred_sensitive.update(_inferred_sensitive_changes(semantic_inputs))
            if item.get("migration") in {"planned", "required"}:
                inferred_sensitive.add("migration")
            if item.get("rollback") == "required":
                inferred_sensitive.add("rollback")
            undeclared_sensitive = sorted(inferred_sensitive - slice_sensitive)
            if undeclared_sensitive:
                validation.error(
                    SENSITIVE_CHANGE_UNDECLARED,
                    f"slices.{slice_id}.sensitive_changes",
                    "must declare inferred sensitive changes: " + ", ".join(undeclared_sensitive),
                )
            effective_sensitive = slice_sensitive | inferred_sensitive
            approval_required = (
                item.get("risk") in {"R3", "R4"}
                or bool(effective_sensitive)
                or item.get("migration") in {"planned", "required"}
                or item.get("rollback") == "required"
            )
            decision_refs = {
                value for value in item.get("decision_refs", []) if isinstance(value, str)
            }
            approved_refs = decision_refs & approved_decisions
            human_approvals = {
                value for value in contract.get("human_approvals", []) if isinstance(value, str)
            }
            if approval_required:
                if not approved_refs:
                    validation.error(
                        SENSITIVE_APPROVAL_REQUIRED,
                        f"slices.{slice_id}.decision_refs",
                        "high-risk or sensitive planning requires an approved ADR/PDR",
                    )
                if not human_approvals or not human_approvals.issubset(approved_refs):
                    validation.error(
                        SENSITIVE_APPROVAL_REQUIRED,
                        f"{contract_path}.human_approvals",
                        "must reference one or more approved ADR/PDR decisions for this sensitive slice",
                    )
                if "GATE-3" not in set(contract.get("required_gates", [])):
                    validation.error(
                        SENSITIVE_APPROVAL_REQUIRED,
                        f"{contract_path}.required_gates",
                        "high-risk or sensitive planning requires GATE-3",
                    )
            coverage_by_class: dict[str, set[str]] = {
                "negative": set(),
                "regression": set(),
            }
            for ac_id in ac_ids:
                criterion = acceptance.get(ac_id, {})
                classification = criterion.get("classification")
                if classification in coverage_by_class and isinstance(criterion.get("risk_coverage"), list):
                    coverage_by_class[classification].update(
                        value for value in criterion["risk_coverage"] if isinstance(value, str)
                    )
            regression_sensitive = {
                "breaking",
                "removal",
                "rename",
                "migration",
                "rollback",
                "production_data",
            }
            required_negative_coverage: set[str] = set()
            if approval_required:
                required_negative_coverage.add("failure")
            required_negative_coverage.update(effective_sensitive - regression_sensitive)
            required_regression_coverage = effective_sensitive & regression_sensitive
            if item.get("migration") in {"planned", "required"}:
                required_regression_coverage.add("migration")
            if item.get("rollback") == "required":
                required_regression_coverage.add("rollback")
            missing_negative = sorted(
                required_negative_coverage - coverage_by_class["negative"]
            )
            missing_regression = sorted(
                required_regression_coverage - coverage_by_class["regression"]
            )
            if missing_negative or missing_regression:
                details: list[str] = []
                if missing_negative:
                    details.append("negative=" + ",".join(missing_negative))
                if missing_regression:
                    details.append("regression=" + ",".join(missing_regression))
                validation.error(
                    SENSITIVE_ACCEPTANCE_MISSING,
                    f"slices.{slice_id}.acceptance_ids",
                    "sensitive slice lacks exact risk coverage: " + "; ".join(details),
                )
            if source_digest is not None and contract.get("source_digest") != source_digest:
                validation.error(SOURCE_DIGEST_MISMATCH, f"{contract_path}.source_digest", "must match the registered-source set")
            if contract.get("git_binding") != git_binding:
                validation.error(BINDING_INVALID, f"{contract_path}.git_binding", "must exactly bind the ambient Git identity")
            if contract.get("integration_owner") != git_binding.get("integration_owner"):
                validation.error(BINDING_INVALID, f"{contract_path}.integration_owner", "must match ambient integration ownership")
            open_binding = contract.get("openspec_binding")
            if isinstance(open_binding, dict):
                if open_binding.get("change_id") != change_id or open_binding.get("state") != item.get("openspec_state"):
                    validation.error(OPENSPEC_BINDING_INVALID, f"{contract_path}.openspec_binding", "must bind this slice Change and JIT state")
                external_change = openspec_changes.get(change_id)
                if external_change is not None and open_binding.get("artifact_digest") != external_change.get("artifact_digest"):
                    validation.error(OPENSPEC_BINDING_INVALID, f"{contract_path}.openspec_binding.artifact_digest", "must match external OpenSpec binding")
            covered_checks: set[str] = set()
            if isinstance(contract.get("planned_checks"), list):
                for check in contract["planned_checks"]:
                    if isinstance(check, dict) and isinstance(check.get("acceptance_ids"), list):
                        check_acceptance = {
                            value for value in check["acceptance_ids"] if isinstance(value, str)
                        }
                        covered_checks.update(check_acceptance)
                        unexpected = sorted(check_acceptance - ac_ids)
                        if unexpected:
                            validation.error(
                                REFERENCE_MISMATCH,
                                f"{contract_path}.planned_checks",
                                f"check references Acceptance outside the slice: {', '.join(unexpected)}",
                            )
            missing_checks = sorted(ac_ids - covered_checks)
            if missing_checks:
                validation.error(CHECK_COVERAGE_MISSING, f"{contract_path}.planned_checks", f"missing planned checks for {', '.join(missing_checks)}")

    for req_id in sorted(in_scope):
        if not req_to_ac[req_id] or not req_to_features[req_id] or not req_to_slices[req_id]:
            validation.error(REQUIREMENT_UNCOVERED, f"requirements.{req_id}", "in-scope Requirement must map to Acceptance, Feature, and slice")
        if requirements[req_id].get("kind") == "preservation":
            regression = [ac_id for ac_id in req_to_ac[req_id] if acceptance.get(ac_id, {}).get("classification") == "regression"]
            if not regression:
                validation.error(PRESERVATION_ORACLE_MISSING, f"requirements.{req_id}", "preservation Requirement needs regression Acceptance with an oracle")
    for ac_id in sorted(acceptance):
        if not ac_to_features[ac_id]:
            validation.error(ACCEPTANCE_ORPHAN, f"acceptance.{ac_id}", "Acceptance is not mapped to a Feature")
        if not ac_to_slices[ac_id]:
            validation.error(ACCEPTANCE_UNCOVERED, f"acceptance.{ac_id}", "Acceptance is not mapped to a slice")
    for feature_id in sorted(features):
        if not feature_to_slices[feature_id]:
            validation.error(FEATURE_UNCOVERED, f"features.{feature_id}", "Feature is not mapped to a slice")
        if feature_to_slices[feature_id] != actual_feature_slices[feature_id]:
            validation.error(
                REFERENCE_MISMATCH,
                f"features.{feature_id}.slice_ids",
                "Feature and slice mappings must be bidirectional and exact",
            )

    for change_id, (path, _contract) in contracts.items():
        if change_id not in slice_change_ids:
            validation.error(CONTRACT_ORPHAN, path, f"contract {change_id} has no owning slice")
    for change_id in openspec_changes:
        if change_id not in slice_change_ids:
            validation.error(OPENSPEC_ORPHAN, f"external_bindings.openspec.changes.{change_id}", "OpenSpec Change has no owning slice")
    for change_id in slice_change_ids:
        if change_id not in openspec_changes:
            validation.error(OPENSPEC_BINDING_INVALID, f"external_bindings.openspec.changes.{change_id}", "all slice Change IDs must be reserved")

    materialized = [change_id for change_id, item in openspec_changes.items() if item.get("state") == "materialized"]
    expected_change = slices.get(next_ready, {}).get("change_id") if next_ready else None
    if readiness == "seal":
        if len(materialized) != 1 or materialized[0] != expected_change:
            validation.error(
                OPENSPEC_JIT_VIOLATION,
                "external_bindings.openspec.changes",
                "exactly the deterministic next-ready slice must be materialized for a planning seal",
            )
    elif readiness == "change_ready":
        if materialized:
            validation.error(
                OPENSPEC_JIT_VIOLATION,
                "external_bindings.openspec.changes",
                "change-ready validation requires every Change, including next-ready, to remain reserved",
            )
        if expected_change is not None:
            next_slice = slices.get(next_ready, {}) if next_ready else {}
            next_contract = contracts.get(expected_change)
            contract_binding = next_contract[1].get("openspec_binding", {}) if next_contract else {}
            if (
                next_slice.get("openspec_state") != "reserved"
                or openspec_changes.get(expected_change, {}).get("state") != "reserved"
                or not isinstance(contract_binding, dict)
                or contract_binding.get("state") != "reserved"
            ):
                validation.error(
                    OPENSPEC_JIT_VIOLATION,
                    "external_bindings.openspec.changes",
                    "next-ready slice and contract must remain reserved before Change creation",
                )
    for slice_id, item in slices.items():
        change_id = item.get("change_id")
        external = openspec_changes.get(change_id, {}) if isinstance(change_id, str) else {}
        if external.get("state") != item.get("openspec_state"):
            validation.error(OPENSPEC_BINDING_INVALID, f"slices.{slice_id}.openspec_state", "must match external OpenSpec Change state")
        if external.get("state") == "materialized":
            if set(external.get("requirement_ids", [])) != set(item.get("requirement_ids", [])):
                validation.error(OPENSPEC_BINDING_INVALID, f"external_bindings.openspec.changes.{change_id}.requirement_ids", "must carry slice Requirement IDs")
            if set(external.get("acceptance_ids", [])) != set(item.get("acceptance_ids", [])):
                validation.error(OPENSPEC_BINDING_INVALID, f"external_bindings.openspec.changes.{change_id}.acceptance_ids", "must carry slice Acceptance IDs")

    for decision in sorted(_all_decision_refs(requirements, slices, contracts)):
        if decision not in approved_decisions:
            validation.error(DECISION_UNAPPROVED, f"decisions.{decision}", "referenced decision is not approved")

    topological_order, _ = _graph_order(validation, slices, "depends_on", "slices")
    return topological_order, slice_change_ids


def validate_planning_bundle(
    root: str | os.PathLike[str],
    external_bindings: Mapping[str, Any] | None = None,
    *,
    readiness: str = "seal",
) -> dict[str, Any]:
    """Validate an M3 planning bundle without writing or invoking subprocesses.

    The returned report is deterministic for identical files and bindings.  It
    contains stable reason codes, artifact/source/binding digests, the computed
    topological order, and the next JIT Change.  ``readiness='change_ready'``
    validates the fully reserved graph before the next Change is scaffolded;
    the default ``readiness='seal'`` requires exactly that next Change to be
    materialized.  Only a valid ``planning_ready`` report can build a seal.
    """

    validation = _Validation()
    if readiness not in {"seal", "change_ready"}:
        validation.error(
            ENUM_INVALID,
            "readiness",
            "must be 'seal' or 'change_ready'",
        )
    artifact_digests: dict[str, str] = {}
    source_digests: dict[str, str] = {}
    planning_byte_budget = [0]
    root_raw = Path(os.path.abspath(os.fspath(root)))
    try:
        mode = root_raw.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise _ReadFailure(PATH_LINK, "project root must not be a symbolic link")
        if not stat.S_ISDIR(mode):
            raise _ReadFailure(PATH_SPECIAL, "project root must be a directory")
        project_root = root_raw.resolve(strict=True)
    except FileNotFoundError:
        validation.error(PATH_MISSING, "<project-root>", "project root is missing")
        project_root = root_raw
    except _ReadFailure as exc:
        validation.error(exc.code, "<project-root>", str(exc))
        project_root = root_raw
    except OSError as exc:
        validation.error(PATH_INVALID, "<project-root>", f"cannot inspect project root: {_os_error_detail(exc)}")
        project_root = root_raw

    external: Mapping[str, Any]
    if external_bindings is None:
        external = {}
        validation.error(BINDING_INVALID, "external_bindings", "versioned external bindings are required")
    elif not isinstance(external_bindings, Mapping) or not _json_scalar_or_container(dict(external_bindings)):
        external = {}
        validation.error(BINDING_INVALID, "external_bindings", "must be a JSON-compatible object")
    else:
        external = dict(external_bindings)

    authority_paths = dict(DEFAULT_AUTHORITY_PATHS)
    override = external.get("authority_paths") if isinstance(external, Mapping) else None
    if override is not None:
        if not isinstance(override, dict):
            validation.error(BINDING_INVALID, "external_bindings.authority_paths", "must be an object")
        else:
            unknown = sorted(set(override) - set(DEFAULT_AUTHORITY_PATHS))
            for key in unknown:
                validation.error(BINDING_INVALID, f"external_bindings.authority_paths.{key}", "unknown authority path key")
            for key in DEFAULT_AUTHORITY_PATHS:
                if key in override:
                    canonical = _canonical_relative_path(override[key])
                    if canonical is None:
                        validation.error(PATH_INVALID, f"external_bindings.authority_paths.{key}", "must be a canonical project-relative path")
                    else:
                        authority_paths[key] = canonical

    registered, git_binding, openspec_changes, approved_decisions, project_mode = _validate_external_bindings(
        validation,
        project_root,
        external,
        source_digests,
    )

    documents: dict[str, dict[str, Any] | None] = {}
    for key in ("requirements", "acceptance", "features", "roadmap"):
        documents[key] = _load_json_artifact(
            validation,
            project_root,
            authority_paths[key],
            artifact_digests,
            planning_byte_budget,
        )
    raw_contracts = _load_work_items(
        validation,
        project_root,
        authority_paths["work_items"],
        artifact_digests,
        planning_byte_budget,
    )

    requirements: dict[str, Mapping[str, Any]] = {}
    acceptance: dict[str, Mapping[str, Any]] = {}
    features: dict[str, Mapping[str, Any]] = {}
    milestones: dict[str, Mapping[str, Any]] = {}
    slices: dict[str, Mapping[str, Any]] = {}
    contracts: dict[str, tuple[str, Mapping[str, Any]]] = {}
    next_ready: str | None = None
    if documents["requirements"] is not None:
        requirements = _validate_requirements(
            validation,
            documents["requirements"],
            authority_paths["requirements"],
            registered,
            source_digests,
        )
    if documents["acceptance"] is not None:
        acceptance = _validate_acceptance(validation, documents["acceptance"], authority_paths["acceptance"])
    if documents["features"] is not None:
        features = _validate_features(validation, documents["features"], authority_paths["features"])
    if documents["roadmap"] is not None:
        milestones, slices, next_ready = _validate_roadmap(validation, documents["roadmap"], authority_paths["roadmap"])
    contracts = _validate_contracts(validation, raw_contracts)

    source_digest = canonical_json_digest(dict(sorted(source_digests.items()))) if source_digests else None
    topological_order: list[str] = []
    change_ids: list[str] = []
    if documents["roadmap"] is not None:
        topological_order, change_ids = _cross_validate(
            validation,
            project_mode=project_mode,
            roadmap_mode=documents["roadmap"].get("project_mode"),
            requirements=requirements,
            acceptance=acceptance,
            features=features,
            milestones=milestones,
            slices=slices,
            next_ready=next_ready,
            contracts=contracts,
            source_digest=source_digest,
            git_binding=git_binding,
            openspec_changes=openspec_changes,
            approved_decisions=approved_decisions,
            roadmap_path=authority_paths["roadmap"],
            readiness=readiness,
        )

    try:
        external_digest = canonical_json_digest(external)
    except (TypeError, ValueError):
        external_digest = None
        validation.error(BINDING_INVALID, "external_bindings", "cannot be canonically encoded")
    bundle_material = {
        "planning_artifacts": dict(sorted(artifact_digests.items())),
        "sources": dict(sorted(source_digests.items())),
        "external_bindings_digest": external_digest,
    }
    bundle_digest = canonical_json_digest(bundle_material)
    error_index = {
        (item["code"], item["path"], item["message"]): item for item in validation.errors
    }
    warning_index = {
        (item["code"], item["path"], item["message"]): item for item in validation.warnings
    }
    errors = [error_index[key] for key in sorted(error_index)]
    warnings = [warning_index[key] for key in sorted(warning_index)]
    valid = not errors
    return {
        "schema_version": PLANNING_SCHEMA_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "valid": valid,
        "status": ("planning_ready" if readiness == "seal" else "change_ready") if valid else "blocked",
        "readiness": readiness,
        "reason_codes": sorted({item["code"] for item in errors}),
        "errors": errors,
        "warnings": warnings,
        "authority_paths": authority_paths,
        "planning_artifact_digests": dict(sorted(artifact_digests.items())),
        "source_artifact_digests": dict(sorted(source_digests.items())),
        "source_digest": source_digest,
        "external_bindings_digest": external_digest,
        "bundle_digest": bundle_digest,
        "topological_order": topological_order,
        "next_ready_slice_id": next_ready,
        "change_ids": change_ids,
        "capability_ceiling": "PLANNING",
        "implementation_authorized": False,
    }


def build_planning_seal_payload(
    validation_report: Mapping[str, Any],
    *,
    approval_ref: str,
    approval_revision: int,
) -> dict[str, Any]:
    """Purely construct the digest-bound payload written by a seal adapter.

    No clock or filesystem value is inferred.  A separate control-plane command
    may add its event timestamp when it atomically persists this payload.
    """

    if not isinstance(validation_report, Mapping) or validation_report.get("valid") is not True:
        raise ValueError("a valid planning report is required")
    if validation_report.get("status") != "planning_ready" or validation_report.get("readiness") != "seal":
        raise ValueError("only seal-readiness validation can build a planning seal")
    if validation_report.get("schema_version") != PLANNING_SCHEMA_VERSION:
        raise ValueError("planning report schema is unsupported")
    if validation_report.get("validator_version") != VALIDATOR_VERSION:
        raise ValueError("planning report validator version is unsupported")
    if validation_report.get("errors") != [] or validation_report.get("reason_codes") != []:
        raise ValueError("planning report still contains blocking validation results")
    if (
        validation_report.get("capability_ceiling") != "PLANNING"
        or validation_report.get("implementation_authorized") is not False
    ):
        raise ValueError("planning report does not preserve the M4 capability lock")
    for key in ("bundle_digest", "source_digest", "external_bindings_digest"):
        if not _is_sha256(validation_report.get(key)):
            raise ValueError(f"planning report {key} is invalid")
    if not _nonempty_string(approval_ref):
        raise ValueError("approval_ref must be a non-empty string")
    if not isinstance(approval_revision, int) or isinstance(approval_revision, bool) or approval_revision < 0:
        raise ValueError("approval_revision must be a non-negative integer")
    artifact_digests = validation_report.get("planning_artifact_digests")
    source_digests = validation_report.get("source_artifact_digests")
    if not isinstance(artifact_digests, Mapping) or not isinstance(source_digests, Mapping):
        raise ValueError("planning report digest maps are missing")
    normalized_artifacts: dict[str, str] = {}
    normalized_sources: dict[str, str] = {}
    for label, values, target in (
        ("planning artifact", artifact_digests, normalized_artifacts),
        ("source artifact", source_digests, normalized_sources),
    ):
        for path, digest in values.items():
            canonical = _canonical_relative_path(path)
            if canonical is None or canonical != path or not _is_sha256(digest):
                raise ValueError(f"planning report {label} digests are invalid")
            target[canonical] = digest
    expected_source_digest = canonical_json_digest(dict(sorted(normalized_sources.items())))
    if validation_report["source_digest"] != expected_source_digest:
        raise ValueError("planning report source digest does not match its source map")
    expected_bundle_digest = canonical_json_digest(
        {
            "planning_artifacts": dict(sorted(normalized_artifacts.items())),
            "sources": dict(sorted(normalized_sources.items())),
            "external_bindings_digest": validation_report["external_bindings_digest"],
        }
    )
    if validation_report["bundle_digest"] != expected_bundle_digest:
        raise ValueError("planning report bundle digest does not match its digest maps")
    topological_order = validation_report.get("topological_order")
    change_ids = validation_report.get("change_ids")
    next_ready = validation_report.get("next_ready_slice_id")
    if (
        not isinstance(topological_order, list)
        or not topological_order
        or any(not isinstance(item, str) or SLICE_ID.fullmatch(item) is None for item in topological_order)
        or len(set(topological_order)) != len(topological_order)
    ):
        raise ValueError("planning report topological order is invalid")
    if not isinstance(next_ready, str) or next_ready not in topological_order:
        raise ValueError("planning report next-ready slice is invalid")
    if (
        not isinstance(change_ids, list)
        or len(change_ids) != len(topological_order)
        or any(not isinstance(item, str) or CHANGE_ID.fullmatch(item) is None for item in change_ids)
        or len(set(change_ids)) != len(change_ids)
    ):
        raise ValueError("planning report Change IDs are invalid")
    payload = {
        "schema_version": PLANNING_SCHEMA_VERSION,
        "artifact_type": "planning-seal",
        "validator_version": validation_report.get("validator_version"),
        "bundle_digest": validation_report["bundle_digest"],
        "source_digest": validation_report["source_digest"],
        "external_bindings_digest": validation_report["external_bindings_digest"],
        "planning_artifact_digests": dict(sorted(normalized_artifacts.items())),
        "source_artifact_digests": dict(sorted(normalized_sources.items())),
        "topological_order": list(topological_order),
        "next_ready_slice_id": next_ready,
        "change_ids": list(change_ids),
        "approval": {"ref": approval_ref.strip(), "revision": approval_revision},
        "capability_ceiling": "PLANNING",
        "implementation_authorized": False,
        "openspec_apply_authorized": False,
        "m4_lock": True,
    }
    # Prove now that callers cannot smuggle non-JSON values into durable state.
    canonical_json_bytes(payload)
    return payload


def planning_seal_digest(payload: Mapping[str, Any]) -> str:
    """Purely return the canonical digest of a planning seal payload."""

    if not isinstance(payload, Mapping):
        raise ValueError("planning seal payload must be an object")
    if payload.get("schema_version") != PLANNING_SCHEMA_VERSION or payload.get("artifact_type") != "planning-seal":
        raise ValueError("planning seal payload is unsupported")
    if (
        payload.get("m4_lock") is not True
        or payload.get("implementation_authorized") is not False
        or payload.get("openspec_apply_authorized") is not False
        or payload.get("capability_ceiling") != "PLANNING"
    ):
        raise ValueError("planning seal must preserve the M4 capability lock")
    try:
        return canonical_json_digest(dict(payload))
    except (TypeError, ValueError) as exc:
        raise ValueError("planning seal payload must be canonical JSON") from exc
