#!/usr/bin/env python3
"""Read-only assembly of M3 planning validator external bindings.

This module is deliberately glue, not another planning validator.  It reads
the two planning documents needed to discover source and Change identities,
captures a hardened ambient Git scope, audits and queries OpenSpec through its
bounded adapter, and returns path-free facts for :mod:`planning`.

It never creates an OpenSpec Change, changes configuration, or writes project
state.  Semantic contradictions (orphan Changes, JIT state mismatches, checked
tasks) remain visible in the returned facts so the single planning validator
can report them with its stable reason codes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import date
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

import git_scope
import openspec_adapter
import planning


BINDINGS_SCHEMA_VERSION = 1
MAX_AUTHORITY_BYTES = 4 * 1024 * 1024
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 128 * 1024 * 1024
MAX_TREE_FILE_BYTES = 8 * 1024 * 1024
MAX_TREE_BYTES = 64 * 1024 * 1024
MAX_TREE_ENTRIES = 20_000
MAX_TREE_DEPTH = 32
MAX_CHANGES = 512
MAX_ACTIVE_CHANGES = 8
MAX_RUNTIME_ARTIFACTS = 32
MAX_SOURCE_REFS = 2_048
MAX_RELATIVE_PATH_BYTES = 4_096
MAX_RELATIVE_PATH_DEPTH = 64
MAX_DECISION_DOCS = 512
MAX_DECISION_BYTES = 4 * 1024 * 1024
MAX_TOTAL_DECISION_BYTES = 32 * 1024 * 1024
MAX_TASK_INDENT_COLUMNS = 64

SHA256 = re.compile(r"[0-9a-f]{64}\Z")
GIT_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
CHANGE_ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
SCHEMA_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
REQ_ID = re.compile(r"(?<![A-Z0-9-])REQ-[A-Z0-9][A-Z0-9-]*(?![A-Z0-9-])")
AC_ID = re.compile(r"(?<![A-Z0-9-])AC-[A-Z0-9][A-Z0-9-]*(?![A-Z0-9-])")
TASK_LINE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)[-*]\s+\[(?P<mark>[ xX])\]\s+(?P<body>\S[^\r\n]*)\r?$"
)
REQUIREMENT_HEADING = re.compile(r"(?m)^###\s+Requirement:\s*(?P<title>[^\r\n]+)$")
SCENARIO_HEADING = re.compile(r"(?m)^####\s+Scenario:\s*(?P<title>[^\r\n]+)$")
MARKDOWN_HEADING = re.compile(r"^(?P<marks>#{1,6})\s+")
DECISION_ID = re.compile(r"(?:PDR-[A-Z0-9][A-Z0-9-]*|ADR-[0-9]{4}(?:-[A-Z0-9][A-Z0-9-]*)?)\Z")
DECISION_HEADING = re.compile(
    r"(?m)^#\s+(?P<id>PDR-[A-Z0-9][A-Z0-9-]*|ADR-[0-9]{4}(?:-[A-Z0-9][A-Z0-9-]*)?)(?=\s|:|$)"
)
DECISION_STATUS = re.compile(r"(?mi)^(?:-\s*)?status\s*:\s*(?P<status>[A-Za-z][A-Za-z _-]*)\s*$")
ARCHIVED_NAME = re.compile(r"(?P<day>\d{4}-\d{2}-\d{2})-(?P<change>[a-z][a-z0-9]*(?:-[a-z0-9]+)*)\Z")
FORBIDDEN_CHANGE_IDS = set(openspec_adapter.FORBIDDEN_LIFECYCLE_WORDS) | {"archive"}


class PlanningBindingsError(RuntimeError):
    """A stable, shareable binding-collection failure.

    Details are intentionally limited to reason codes, relative artifact
    labels, and identifiers.  Raw exception strings can contain local paths or
    untrusted process output and are never forwarded.
    """

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class _DuplicateKey(ValueError):
    pass


def _fail(code: str, message: str, **details: Any) -> None:
    raise PlanningBindingsError(code, message, **details)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(encoded)


def _canonical_relative(raw: Any, *, label: str) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        _fail("planning-bindings-path-invalid", "Path must be canonical and project-relative", field=label)
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw):
        _fail("planning-bindings-path-invalid", "Path contains control characters", field=label)
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts) or pure.as_posix() != raw:
        _fail("planning-bindings-path-invalid", "Path must be canonical and project-relative", field=label)
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError:
        _fail("planning-bindings-path-invalid", "Path is not valid UTF-8", field=label)
    if len(encoded) > MAX_RELATIVE_PATH_BYTES or len(pure.parts) > MAX_RELATIVE_PATH_DEPTH:
        _fail("planning-bindings-path-budget", "Path exceeds its length or depth budget", field=label)
    return raw


def _canonical_root(raw: str | os.PathLike[str]) -> Path:
    lexical = Path(os.path.abspath(os.fspath(raw)))
    try:
        metadata = lexical.lstat()
        canonical = lexical.resolve(strict=True)
    except OSError:
        _fail("planning-bindings-root-unavailable", "Project root cannot be inspected")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) or lexical != canonical:
        _fail("planning-bindings-root-unsafe", "Project root must be an existing canonical directory")
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

    Windows cannot open directories, so the handle pins the verified
    (dev, ino, type) identity of each directory and re-validates it
    before every child operation.
    """

    __slots__ = ("path", "identity", "metadata")

    def __init__(self, path: Path, metadata: os.stat_result) -> None:
        self.path = path
        self.metadata = metadata
        self.identity = (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))

    def verify(self, artifact: str) -> None:
        try:
            current = self.path.lstat()
        except OSError:
            _fail("planning-bindings-file-uninspectable", "Required directory cannot be reinspected", artifact=artifact)
        identity = (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode))
        if identity != self.identity:
            _fail("planning-bindings-file-raced", "Required directory changed while being traversed", artifact=artifact)


def _close_directory(handle: "int | _DirectoryHandle") -> None:
    if isinstance(handle, _DirectoryHandle):
        return
    os.close(handle)


def _fstat_directory(handle: "int | _DirectoryHandle") -> os.stat_result:
    if isinstance(handle, _DirectoryHandle):
        return handle.metadata
    return os.fstat(handle)


def _open_root_directory(root: Path) -> "int | _DirectoryHandle":
    try:
        expected = root.lstat()
    except OSError:
        _fail("planning-bindings-root-unsafe", "Project root cannot be opened safely")
    if os.name == "nt":
        # Windows cannot open directory descriptors; pin the identity instead.
        if stat.S_ISLNK(expected.st_mode) or not stat.S_ISDIR(expected.st_mode):
            _fail("planning-bindings-root-unsafe", "Project root cannot be opened safely")
        return _DirectoryHandle(root, expected)
    try:
        descriptor = os.open(root, _directory_flags())
    except OSError:
        _fail("planning-bindings-root-unsafe", "Project root cannot be opened safely")
    observed = os.fstat(descriptor)
    if not stat.S_ISDIR(observed.st_mode) or not _same_entry(expected, observed):
        os.close(descriptor)
        _fail("planning-bindings-root-raced", "Project root changed while being opened")
    return descriptor


def _open_child_directory(parent_fd: "int | _DirectoryHandle", name: str, *, artifact: str) -> "int | _DirectoryHandle":
    if isinstance(parent_fd, _DirectoryHandle):
        parent_fd.verify(artifact)
        child_path = parent_fd.path / name
        try:
            expected = child_path.lstat()
        except FileNotFoundError:
            _fail("planning-bindings-file-missing", "Required directory is missing", artifact=artifact)
        except OSError:
            _fail("planning-bindings-file-uninspectable", "Required directory cannot be inspected", artifact=artifact)
        if stat.S_ISLNK(expected.st_mode):
            _fail("planning-bindings-file-link", "Required path crosses a symbolic link", artifact=artifact)
        if not stat.S_ISDIR(expected.st_mode):
            _fail("planning-bindings-file-special", "Required path has a non-directory parent", artifact=artifact)
        return _DirectoryHandle(child_path, expected)
    try:
        expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        _fail("planning-bindings-file-missing", "Required directory is missing", artifact=artifact)
    except OSError:
        _fail("planning-bindings-file-uninspectable", "Required directory cannot be inspected", artifact=artifact)
    if stat.S_ISLNK(expected.st_mode):
        _fail("planning-bindings-file-link", "Required path crosses a symbolic link", artifact=artifact)
    if not stat.S_ISDIR(expected.st_mode):
        _fail("planning-bindings-file-special", "Required path has a non-directory parent", artifact=artifact)
    try:
        child_fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except OSError:
        _fail("planning-bindings-file-uninspectable", "Required directory cannot be opened safely", artifact=artifact)
    observed = os.fstat(child_fd)
    if not stat.S_ISDIR(observed.st_mode) or not _same_entry(expected, observed):
        os.close(child_fd)
        _fail("planning-bindings-file-raced", "Required directory changed while being opened", artifact=artifact)
    return child_fd


def _open_relative_directory(root: Path, relative: str) -> "int | _DirectoryHandle":
    relative = _canonical_relative(relative, label="directory")
    current_fd = _open_root_directory(root)
    traversed: list[str] = []
    try:
        for component in PurePosixPath(relative).parts:
            traversed.append(component)
            child_fd = _open_child_directory(
                current_fd,
                component,
                artifact="/".join(traversed),
            )
            _close_directory(current_fd)
            current_fd = child_fd
        return current_fd
    except BaseException:
        _close_directory(current_fd)
        raise


def _read_regular_at(
    parent_fd: "int | _DirectoryHandle",
    name: str,
    *,
    artifact: str,
    max_bytes: int,
    expected: os.stat_result | None = None,
) -> tuple[bytes, os.stat_result]:
    if not name or "/" in name or "\x00" in name:
        _fail("planning-bindings-path-invalid", "File component is invalid", artifact=artifact)
    windows_handle = parent_fd if isinstance(parent_fd, _DirectoryHandle) else None
    if windows_handle is not None:
        windows_handle.verify(artifact)
    if expected is None:
        try:
            if windows_handle is not None:
                expected = (windows_handle.path / name).lstat()
            else:
                expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            _fail("planning-bindings-file-missing", "Required regular file is missing", artifact=artifact)
        except OSError:
            _fail("planning-bindings-file-uninspectable", "Required file cannot be inspected", artifact=artifact)
    if stat.S_ISLNK(expected.st_mode):
        _fail("planning-bindings-file-link", "Required file is a symbolic link", artifact=artifact)
    if not stat.S_ISREG(expected.st_mode):
        _fail("planning-bindings-file-special", "Required file is not regular", artifact=artifact)
    try:
        if windows_handle is not None:
            file_flags = _file_flags()
            if hasattr(os, "O_BINARY"):
                file_flags |= os.O_BINARY
            descriptor = os.open(windows_handle.path / name, file_flags)
        else:
            descriptor = os.open(name, _file_flags(), dir_fd=parent_fd)
    except OSError:
        _fail("planning-bindings-file-uninspectable", "Required file cannot be opened safely", artifact=artifact)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not _same_entry(expected, before):
            _fail("planning-bindings-file-raced", "Required file changed while being opened", artifact=artifact)
        if before.st_size > max_bytes:
            _fail("planning-bindings-file-oversize", "Required file exceeds its read budget", artifact=artifact)
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
            _fail("planning-bindings-file-oversize", "Required file exceeds its read budget", artifact=artifact)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode)
            or len(data) != after.st_size
        ):
            _fail("planning-bindings-file-raced", "Required file changed while being read", artifact=artifact)
        return data, after
    finally:
        os.close(descriptor)


def _safe_relative_file(root: Path, relative: str, *, max_bytes: int) -> bytes:
    relative = _canonical_relative(relative, label="artifact")
    parts = PurePosixPath(relative).parts
    parent_fd = _open_root_directory(root)
    traversed: list[str] = []
    try:
        for component in parts[:-1]:
            traversed.append(component)
            child_fd = _open_child_directory(parent_fd, component, artifact="/".join(traversed))
            _close_directory(parent_fd)
            parent_fd = child_fd
        data, _ = _read_regular_at(
            parent_fd,
            parts[-1],
            artifact=relative,
            max_bytes=max_bytes,
        )
        return data
    finally:
        _close_directory(parent_fd)


def _strict_json(data: bytes, *, artifact: str) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _fail("planning-bindings-json-invalid", "Authority document is not UTF-8", artifact=artifact)

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise _DuplicateKey(key)
            value[key] = item
        return value

    def invalid_constant(_value: str) -> None:
        raise ValueError("non-finite number")

    try:
        value = json.loads(text, object_pairs_hook=object_pairs, parse_constant=invalid_constant)
    except _DuplicateKey:
        _fail("planning-bindings-json-duplicate-key", "Authority document contains a duplicate key", artifact=artifact)
    except (json.JSONDecodeError, ValueError):
        _fail("planning-bindings-json-invalid", "Authority document is not strict JSON", artifact=artifact)
    if not isinstance(value, dict):
        _fail("planning-bindings-json-root-invalid", "Authority document must contain an object", artifact=artifact)
    return value


def _authority_paths(overrides: Mapping[str, str] | None) -> dict[str, str]:
    result = dict(planning.DEFAULT_AUTHORITY_PATHS)
    if overrides is not None:
        if not isinstance(overrides, Mapping):
            _fail("planning-bindings-authority-invalid", "authority_paths must be an object")
        if any(not isinstance(key, str) for key in overrides):
            _fail("planning-bindings-authority-invalid", "authority_paths keys must be strings")
        unknown = sorted(set(overrides) - set(result))
        if unknown:
            _fail("planning-bindings-authority-invalid", "authority_paths contains unknown keys", keys=unknown)
        for key, value in overrides.items():
            result[key] = _canonical_relative(value, label=f"authority_paths.{key}")
    for key, value in result.items():
        result[key] = _canonical_relative(value, label=f"authority_paths.{key}")
    if len(set(result.values())) != len(result):
        _fail("planning-bindings-authority-invalid", "Authority paths must be distinct")
    protected_roots = {".git", ".delivery", ".qoder", "openspec"}
    for key in ("requirements", "acceptance", "features", "roadmap"):
        candidate_parts = tuple(part.casefold() for part in PurePosixPath(result[key]).parts)
        if candidate_parts[0] in protected_roots or candidate_parts[:2] == ("delivery-docs", "state"):
            _fail(
                "planning-bindings-authority-invalid",
                "Planning authority cannot overlap Git or OpenSpec authority",
                field=f"authority_paths.{key}",
            )
    file_authorities = [PurePosixPath(result[key]) for key in ("requirements", "acceptance", "features", "roadmap")]
    for index, left in enumerate(file_authorities):
        for right in file_authorities[index + 1 :]:
            if left.parts == right.parts[: len(left.parts)] or right.parts == left.parts[: len(right.parts)]:
                _fail(
                    "planning-bindings-authority-invalid",
                    "Planning authority paths cannot be ancestors of one another",
                )
    work_items_parts = PurePosixPath(result["work_items"]).parts
    if len(work_items_parts) < 3 or work_items_parts[:2] != ("delivery-docs", "state"):
        _fail(
            "planning-bindings-authority-invalid",
            "work_items must remain a descendant of delivery-docs/state",
            field="authority_paths.work_items",
        )
    return result


def _strict_discovery_documents(
    root: Path,
    paths: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, str], dict[str, bytes]]:
    raw: dict[str, bytes] = {}
    digests: dict[str, str] = {}
    for key in ("requirements", "acceptance", "features", "roadmap"):
        data = _safe_relative_file(root, paths[key], max_bytes=MAX_AUTHORITY_BYTES)
        raw[key] = data
        digests[paths[key]] = _sha256(data)

    requirements = _strict_json(raw["requirements"], artifact=paths["requirements"])
    acceptance = _strict_json(raw["acceptance"], artifact=paths["acceptance"])
    roadmap = _strict_json(raw["roadmap"], artifact=paths["roadmap"])
    expected = (
        (requirements, "requirements", "requirements"),
        (acceptance, "acceptance-catalog", "acceptance_criteria"),
        (roadmap, "delivery-roadmap", "slices"),
    )
    for document, artifact_type, collection in expected:
        if document.get("schema_version") != BINDINGS_SCHEMA_VERSION or document.get("artifact_type") != artifact_type:
            _fail(
                "planning-bindings-authority-schema-invalid",
                "Discovery authority has an unsupported schema or artifact type",
                artifact_type=artifact_type,
            )
        if not isinstance(document.get(collection), list):
            _fail(
                "planning-bindings-authority-shape-invalid",
                "Discovery authority has an invalid collection",
                artifact_type=artifact_type,
                field=collection,
            )
    if len(roadmap["slices"]) > MAX_CHANGES:
        _fail("planning-bindings-change-budget", "Roadmap exceeds the Change binding budget")
    return requirements, acceptance, roadmap, dict(sorted(digests.items())), raw


def _registered_sources(root: Path, requirements: Mapping[str, Any], authority: Mapping[str, str]) -> tuple[dict[str, str], int]:
    items = requirements.get("requirements")
    assert isinstance(items, list)
    paths: set[str] = set()
    declared_mismatches = 0
    declared: dict[str, set[str]] = {}
    source_ref_count = 0
    for item_index, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("source_refs"), list) or not item["source_refs"]:
            _fail(
                "planning-bindings-source-ref-invalid",
                "Every Requirement must expose a non-empty source_refs list",
                requirement_index=item_index,
            )
        for ref_index, ref in enumerate(item["source_refs"]):
            source_ref_count += 1
            if source_ref_count > MAX_SOURCE_REFS:
                _fail("planning-bindings-source-budget", "Source reference count exceeds its budget")
            if not isinstance(ref, dict):
                _fail(
                    "planning-bindings-source-ref-invalid",
                    "Source reference must be an object",
                    requirement_index=item_index,
                    source_ref_index=ref_index,
                )
            relative = _canonical_relative(ref.get("path"), label="source_ref.path")
            expected = ref.get("sha256")
            if not isinstance(expected, str) or SHA256.fullmatch(expected) is None:
                _fail(
                    "planning-bindings-source-ref-invalid",
                    "Source reference must carry a lowercase SHA-256",
                    source=relative,
                )
            source_parts = tuple(PurePosixPath(relative).parts)
            if (
                source_parts[0] in {".git", ".delivery", "openspec"}
                or source_parts[:2] == ("delivery-docs", "state")
                or relative in authority.values()
            ):
                _fail(
                    "planning-bindings-source-scope-invalid",
                    "Registered source overlaps a control or planning authority",
                    source=relative,
                )
            paths.add(relative)
            declared.setdefault(relative, set()).add(expected)
    if not paths:
        _fail("planning-bindings-source-ref-invalid", "At least one registered source is required")
    if len(paths) > planning.MAX_REGISTERED_SOURCES:
        _fail("planning-bindings-source-budget", "Registered source count exceeds its budget")

    total = 0
    result: dict[str, str] = {}
    for relative in sorted(paths):
        data = _safe_relative_file(root, relative, max_bytes=MAX_SOURCE_BYTES)
        total += len(data)
        if total > MAX_TOTAL_SOURCE_BYTES:
            _fail("planning-bindings-source-budget", "Registered source bytes exceed their total budget")
        actual = _sha256(data)
        result[relative] = actual
        if declared.get(relative) != {actual}:
            declared_mismatches += 1
    return result, declared_mismatches


def _normalize_decisions(values: Iterable[str], *, label: str) -> list[str]:
    if isinstance(values, (str, bytes, bytearray)):
        _fail("planning-bindings-decisions-invalid", "Decision set must be a sequence", field=label)
    try:
        items = list(values)
    except (TypeError, ValueError):
        _fail("planning-bindings-decisions-invalid", "Decision set must be a sequence", field=label)
    if any(not isinstance(item, str) or DECISION_ID.fullmatch(item) is None for item in items):
        _fail("planning-bindings-decisions-invalid", "Decision set contains an invalid ID", field=label)
    if len(set(items)) != len(items):
        _fail("planning-bindings-decisions-invalid", "Decision set contains duplicates", field=label)
    return sorted(items)


def _scan_decision_directory(root: Path, relative: str) -> list[tuple[str, bytes]]:
    try:
        directory_fd = _open_relative_directory(root, relative)
    except PlanningBindingsError as exc:
        if exc.code == "planning-bindings-file-missing":
            return []
        _fail(
            "planning-bindings-decision-authority-unsafe",
            "Decision authority cannot be inspected safely",
            authority=relative,
            cause_code=exc.code,
        )
    try:
        try:
            if isinstance(directory_fd, _DirectoryHandle):
                directory_fd.verify(relative)
                scan_target: "int | str" = os.fspath(directory_fd.path)
            else:
                scan_target = directory_fd
            with os.scandir(scan_target) as iterator:
                if isinstance(directory_fd, _DirectoryHandle):
                    # Windows DirEntry.stat() leaves st_dev/st_ino zeroed;
                    # lstat() returns the real identity that _same_entry needs.
                    entries = [
                        (entry.name, (directory_fd.path / entry.name).lstat())
                        for entry in iterator
                    ]
                else:
                    entries = [(entry.name, entry.stat(follow_symlinks=False)) for entry in iterator]
        except OSError:
            _fail(
                "planning-bindings-decision-authority-unsafe",
                "Decision authority cannot be enumerated",
                authority=relative,
            )
        entries.sort(key=lambda item: item[0])
        if len(entries) > MAX_DECISION_DOCS:
            _fail("planning-bindings-decision-budget", "Decision document count exceeds its budget")
        result: list[tuple[str, bytes]] = []
        for name, metadata in entries:
            artifact = _canonical_relative(f"{relative}/{name}", label="decision-document")
            if stat.S_ISLNK(metadata.st_mode) or not (
                stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
            ):
                _fail(
                    "planning-bindings-decision-authority-unsafe",
                    "Decision authority contains a link or special entry",
                    artifact=artifact,
                )
            if stat.S_ISDIR(metadata.st_mode) or not name.lower().endswith(".md"):
                continue
            data, _ = _read_regular_at(
                directory_fd,
                name,
                artifact=artifact,
                max_bytes=MAX_DECISION_BYTES,
                expected=metadata,
            )
            result.append((artifact, data))
        return result
    finally:
        _close_directory(directory_fd)


def _bind_decision_documents(
    root: Path,
    decisions: Mapping[str, Sequence[str]],
) -> tuple[dict[str, dict[str, str]], dict[str, bytes]]:
    requested: dict[str, str] = {}
    for state in ("approved", "pending", "blocking"):
        for identifier in decisions[state]:
            requested[identifier] = state
    if not requested:
        return {}, {}

    directories: list[str] = []
    if any(identifier.startswith("ADR-") for identifier in requested):
        directories.append("delivery-docs/decisions/adr")
    if any(identifier.startswith("PDR-") for identifier in requested):
        directories.append("delivery-docs/decisions/product")
    documents: dict[str, tuple[str, str, bytes]] = {}
    total_bytes = 0
    for directory in directories:
        for relative, data in _scan_decision_directory(root, directory):
            total_bytes += len(data)
            if total_bytes > MAX_TOTAL_DECISION_BYTES:
                _fail("planning-bindings-decision-budget", "Decision document bytes exceed their budget")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                _fail(
                    "planning-bindings-decision-document-invalid",
                    "Decision document is not UTF-8",
                    artifact=relative,
                )
            heading = DECISION_HEADING.search(text)
            status_match = DECISION_STATUS.search(text)
            if heading is None or status_match is None:
                continue
            identifier = heading.group("id")
            status_value = re.sub(r"[ _]+", "-", status_match.group("status").strip().lower())
            if identifier in documents:
                _fail(
                    "planning-bindings-decision-document-duplicate",
                    "Decision ID is declared by more than one document",
                    decision_id=identifier,
                )
            documents[identifier] = (relative, status_value, data)

    allowed_statuses = {
        "approved": {"accepted", "approved"},
        "pending": {"proposed", "pending", "draft", "under-review"},
        "blocking": {"blocked", "pending", "draft", "proposed", "under-review"},
    }
    bindings: dict[str, dict[str, str]] = {}
    raw: dict[str, bytes] = {}
    for identifier, state in sorted(requested.items()):
        document = documents.get(identifier)
        if document is None:
            _fail(
                "planning-bindings-decision-document-missing",
                "Decision ID has no project authority document",
                decision_id=identifier,
            )
        relative, status_value, data = document
        if status_value not in allowed_statuses[state]:
            _fail(
                "planning-bindings-decision-status-mismatch",
                "Decision document status does not support the supplied decision state",
                decision_id=identifier,
                supplied_state=state,
                document_status=status_value,
            )
        digest = _sha256(data)
        # The document status proves that the caller-supplied state is
        # legitimate; the public fact uses the planning schema's canonical
        # state so the collector and validator do not create two enums.
        bindings[identifier] = {"path": relative, "sha256": digest, "status": state}
        raw[relative] = data
    return bindings, raw


def _strict_trace_ids(
    value: Any,
    *,
    pattern: re.Pattern[str],
    artifact: str,
    field: str,
) -> set[str]:
    if not isinstance(value, list) or not value:
        _fail(
            "planning-bindings-roadmap-invalid",
            "Roadmap Change trace lists must be non-empty",
            artifact=artifact,
            field=field,
        )
    result: set[str] = set()
    for identifier in value:
        if not isinstance(identifier, str) or pattern.fullmatch(identifier) is None:
            _fail(
                "planning-bindings-roadmap-invalid",
                "Roadmap Change trace contains an invalid ID",
                artifact=artifact,
                field=field,
            )
        if identifier in result:
            _fail(
                "planning-bindings-roadmap-invalid",
                "Roadmap Change trace contains a duplicate ID",
                artifact=artifact,
                field=field,
            )
        result.add(identifier)
    return result


def _acceptance_relationships(acceptance: Mapping[str, Any]) -> dict[str, set[str]]:
    items = acceptance.get("acceptance_criteria")
    assert isinstance(items, list)
    result: dict[str, set[str]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            _fail(
                "planning-bindings-acceptance-invalid",
                "Acceptance criterion must be an object",
                acceptance_index=index,
            )
        identifier = item.get("id")
        if not isinstance(identifier, str) or planning.AC_ID.fullmatch(identifier) is None:
            _fail(
                "planning-bindings-acceptance-invalid",
                "Acceptance criterion has an invalid ID",
                acceptance_index=index,
            )
        if identifier in result:
            _fail(
                "planning-bindings-acceptance-invalid",
                "Acceptance criterion ID is duplicated",
                acceptance_id=identifier,
            )
        raw_requirements = item.get("requirement_ids")
        if not isinstance(raw_requirements, list) or not raw_requirements:
            _fail(
                "planning-bindings-acceptance-invalid",
                "Acceptance criterion must bind at least one Requirement",
                acceptance_id=identifier,
            )
        requirements: set[str] = set()
        for requirement_id in raw_requirements:
            if not isinstance(requirement_id, str) or planning.REQ_ID.fullmatch(requirement_id) is None:
                _fail(
                    "planning-bindings-acceptance-invalid",
                    "Acceptance criterion binds an invalid Requirement ID",
                    acceptance_id=identifier,
                )
            if requirement_id in requirements:
                _fail(
                    "planning-bindings-acceptance-invalid",
                    "Acceptance criterion repeats a Requirement ID",
                    acceptance_id=identifier,
                )
            requirements.add(requirement_id)
        result[identifier] = requirements
    return result


def _roadmap_change_trace(roadmap: Mapping[str, Any]) -> dict[str, dict[str, set[str]]]:
    seen: set[str] = set()
    trace: dict[str, dict[str, set[str]]] = {}
    slices = roadmap.get("slices")
    assert isinstance(slices, list)
    for index, item in enumerate(slices):
        if not isinstance(item, dict):
            _fail("planning-bindings-roadmap-invalid", "Roadmap slice must be an object", slice_index=index)
        change_id = item.get("change_id")
        if (
            not isinstance(change_id, str)
            or len(change_id) > 100
            or CHANGE_ID.fullmatch(change_id) is None
            or change_id in FORBIDDEN_CHANGE_IDS
        ):
            _fail("planning-bindings-change-id-invalid", "Roadmap contains an invalid Change ID", slice_index=index)
        if change_id in seen:
            _fail("planning-bindings-change-id-duplicate", "Roadmap reuses a Change ID", change_id=change_id)
        seen.add(change_id)
        artifact = f"slice-{index + 1}"
        trace[change_id] = {
            "requirement_ids": _strict_trace_ids(
                item.get("requirement_ids"),
                pattern=planning.REQ_ID,
                artifact=artifact,
                field="requirement_ids",
            ),
            "acceptance_ids": _strict_trace_ids(
                item.get("acceptance_ids"),
                pattern=planning.AC_ID,
                artifact=artifact,
                field="acceptance_ids",
            ),
        }
    return trace


def _tree_snapshot(root: Path) -> dict[str, Any]:
    try:
        openspec_fd = _open_relative_directory(root, "openspec")
    except PlanningBindingsError as exc:
        _fail(
            "planning-bindings-openspec-missing"
            if exc.code == "planning-bindings-file-missing"
            else "planning-bindings-openspec-unsafe",
            "OpenSpec root cannot be opened safely",
            cause_code=exc.code,
        )
    openspec_mode = _fstat_directory(openspec_fd).st_mode
    records: list[list[Any]] = [["directory", "openspec", stat.S_IMODE(openspec_mode)]]
    file_data: dict[str, bytes] = {}
    total_bytes = 0
    entries = 0
    stack: list[tuple["int | _DirectoryHandle", str, int]] = [(openspec_fd, "openspec", 0)]
    try:
        while stack:
            directory_fd, relative_directory, depth = stack.pop()
            try:
                if depth > MAX_TREE_DEPTH:
                    _fail("planning-bindings-openspec-tree-budget", "OpenSpec tree exceeds its depth budget")
                try:
                    if isinstance(directory_fd, _DirectoryHandle):
                        directory_fd.verify(relative_directory)
                        scan_target: "int | str" = os.fspath(directory_fd.path)
                    else:
                        scan_target = directory_fd
                    with os.scandir(scan_target) as iterator:
                        children: list[tuple[str, os.stat_result]] = []
                        for entry in iterator:
                            if isinstance(directory_fd, _DirectoryHandle):
                                # Windows DirEntry.stat() leaves st_dev/st_ino
                                # zeroed; lstat() returns the real identity.
                                children.append((entry.name, (directory_fd.path / entry.name).lstat()))
                            else:
                                children.append((entry.name, entry.stat(follow_symlinks=False)))
                        children.sort(key=lambda item: item[0])
                except OSError:
                    _fail("planning-bindings-openspec-unsafe", "OpenSpec directory cannot be enumerated")
                for child_name, metadata in children:
                    entries += 1
                    if entries > MAX_TREE_ENTRIES:
                        _fail("planning-bindings-openspec-tree-budget", "OpenSpec tree exceeds its entry budget")
                    relative = _canonical_relative(
                        f"{relative_directory}/{child_name}",
                        label="openspec-tree-entry",
                    )
                    mode = metadata.st_mode
                    if stat.S_ISLNK(mode):
                        _fail("planning-bindings-openspec-unsafe", "OpenSpec tree contains a symbolic link")
                    if stat.S_ISDIR(mode):
                        child_fd = _open_child_directory(directory_fd, child_name, artifact=relative)
                        child_mode = _fstat_directory(child_fd).st_mode
                        records.append(["directory", relative, stat.S_IMODE(child_mode)])
                        stack.append((child_fd, relative, depth + 1))
                        continue
                    if not stat.S_ISREG(mode):
                        _fail("planning-bindings-openspec-unsafe", "OpenSpec tree contains a special entry")
                    data, observed = _read_regular_at(
                        directory_fd,
                        child_name,
                        artifact=relative,
                        max_bytes=MAX_TREE_FILE_BYTES,
                        expected=metadata,
                    )
                    total_bytes += len(data)
                    if total_bytes > MAX_TREE_BYTES:
                        _fail("planning-bindings-openspec-tree-budget", "OpenSpec tree exceeds its byte budget")
                    digest = _sha256(data)
                    records.append(["file", relative, stat.S_IMODE(observed.st_mode), len(data), digest])
                    file_data[relative] = data
            finally:
                _close_directory(directory_fd)
    except BaseException:
        for descriptor, _relative, _depth in stack:
            try:
                _close_directory(descriptor)
            except OSError:
                pass
        raise
    records.sort(key=lambda item: (item[1], item[0]))
    return {
        "digest": _canonical_digest(records),
        "records": records,
        "files": file_data,
        "entries": entries,
    }


def _active_changes(snapshot: Mapping[str, Any], roadmap_changes: Sequence[str]) -> list[str]:
    records = snapshot.get("records")
    assert isinstance(records, list)
    by_path = {record[1]: record[0] for record in records}
    if "openspec/changes" not in by_path:
        return []
    if by_path["openspec/changes"] != "directory":
        _fail("planning-bindings-openspec-unsafe", "Active Change root is not a regular directory")
    result: list[str] = []
    direct_prefix = "openspec/changes/"
    direct = [
        (path[len(direct_prefix):], kind)
        for path, kind in by_path.items()
        if path.startswith(direct_prefix) and "/" not in path[len(direct_prefix):]
    ]
    for name, kind in sorted(direct):
        if name == "archive" and kind == "directory":
            continue
        if kind != "directory":
            _fail("planning-bindings-openspec-unsafe", "Active Change root contains an unexpected entry")
        if (
            len(name) > 100
            or CHANGE_ID.fullmatch(name) is None
            or name in FORBIDDEN_CHANGE_IDS
        ):
            _fail("planning-bindings-change-id-invalid", "Active Change has an invalid ID")
        result.append(name)
        if len(result) > MAX_ACTIVE_CHANGES:
            _fail("planning-bindings-change-budget", "Active Change count exceeds the read-only CLI budget")

    archive_prefix = "openspec/changes/archive/"
    archived_ids: set[str] = set()
    archived_direct = [
        (path[len(archive_prefix):], kind)
        for path, kind in by_path.items()
        if path.startswith(archive_prefix) and "/" not in path[len(archive_prefix):]
    ]
    for name, kind in sorted(archived_direct):
        match = ARCHIVED_NAME.fullmatch(name)
        if kind != "directory" or match is None:
            _fail("planning-bindings-openspec-archive-invalid", "OpenSpec archive contains an invalid entry")
        try:
            date.fromisoformat(match.group("day"))
        except ValueError:
            _fail("planning-bindings-openspec-archive-invalid", "OpenSpec archive contains an invalid date")
        archived_ids.add(match.group("change"))
    collisions = sorted(archived_ids & (set(result) | set(roadmap_changes)))
    if collisions:
        _fail(
            "planning-bindings-openspec-archive-conflict",
            "A roadmap or active Change reuses an archived Change ID",
            change_ids=collisions,
        )
    return result


def _decode_markdown(data: bytes, *, artifact: str, change_id: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        _fail(
            "planning-bindings-markdown-invalid",
            "OpenSpec Markdown artifact is not UTF-8",
            artifact=artifact,
            change_id=change_id,
        )


def _runtime_output_pattern(raw: Any, *, artifact: str, change_id: str) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        _fail(
            "planning-bindings-openspec-instructions-invalid",
            "Runtime artifact outputPath must be project-relative Markdown",
            artifact=artifact,
            change_id=change_id,
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw):
        _fail(
            "planning-bindings-openspec-instructions-invalid",
            "Runtime artifact outputPath contains control characters",
            artifact=artifact,
            change_id=change_id,
        )
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or path.as_posix() != raw
        or any(part in {"", ".", ".."} for part in path.parts)
        or len(raw.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES
        or len(path.parts) > MAX_RELATIVE_PATH_DEPTH
    ):
        _fail(
            "planning-bindings-openspec-instructions-invalid",
            "Runtime artifact outputPath escapes or exceeds the Change boundary",
            artifact=artifact,
            change_id=change_id,
        )
    if any(character in raw for character in "?[]{}"):
        _fail(
            "planning-bindings-openspec-instructions-invalid",
            "Runtime artifact outputPath uses an unsupported glob",
            artifact=artifact,
            change_id=change_id,
        )
    for part in path.parts:
        if "*" in part and part not in {"**", "*.md"}:
            _fail(
                "planning-bindings-openspec-instructions-invalid",
                "Runtime artifact outputPath uses an unsupported glob",
                artifact=artifact,
                change_id=change_id,
            )
    if path.parts[-1] != "*.md" and not path.parts[-1].endswith(".md"):
        _fail(
            "planning-bindings-openspec-artifact-unsupported",
            "Runtime planning artifacts must resolve to Markdown",
            artifact=artifact,
            change_id=change_id,
        )
    return raw


def _runtime_absolute_path(
    root: Path,
    raw: Any,
    expected_relative: str,
    *,
    artifact: str,
    change_id: str,
) -> str:
    if not isinstance(raw, str) or not os.path.isabs(raw) or "\x00" in raw:
        _fail(
            "planning-bindings-openspec-instructions-invalid",
            "Runtime artifact path must be absolute before normalization",
            artifact=artifact,
            change_id=change_id,
        )
    expected = os.path.normpath(os.path.join(os.fspath(root), *PurePosixPath(expected_relative).parts))
    if os.path.normpath(raw) != expected:
        _fail(
            "planning-bindings-openspec-instructions-invalid",
            "Runtime artifact path escapes or disagrees with the Change output",
            artifact=artifact,
            change_id=change_id,
        )
    return expected_relative


def _runtime_existing_outputs(
    root: Path,
    raw: Any,
    expected_relatives: Sequence[str],
    *,
    artifact: str,
    change_id: str,
) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(path, str) for path in raw):
        _fail(
            "planning-bindings-openspec-instructions-invalid",
            "Runtime existingOutputPaths must be a path list",
            artifact=artifact,
            change_id=change_id,
        )
    normalized: list[str] = []
    remaining = set(expected_relatives)
    for path in raw:
        matches = [
            relative
            for relative in remaining
            if os.path.normpath(path)
            == os.path.normpath(os.path.join(os.fspath(root), *PurePosixPath(relative).parts))
        ]
        if len(matches) != 1:
            _fail(
                "planning-bindings-openspec-instructions-invalid",
                "Runtime existing output escapes, duplicates, or disagrees with the safe snapshot",
                artifact=artifact,
                change_id=change_id,
            )
        relative = matches[0]
        _runtime_absolute_path(
            root,
            path,
            relative,
            artifact=artifact,
            change_id=change_id,
        )
        remaining.remove(relative)
        normalized.append(relative)
    if remaining:
        _fail(
            "planning-bindings-openspec-instructions-invalid",
            "Runtime existing outputs do not exactly match the safe snapshot",
            artifact=artifact,
            change_id=change_id,
        )
    return sorted(normalized)


def _artifact_output_files(
    files: Mapping[str, bytes],
    prefix: str,
    output_pattern: str,
    *,
    artifact: str,
    change_id: str,
) -> list[str]:
    prefix_with_separator = prefix + "/"
    matched = [
        relative
        for relative in sorted(files)
        if relative.startswith(prefix_with_separator)
        and fnmatchcase(relative[len(prefix_with_separator):], output_pattern)
    ]
    if not matched:
        _fail(
            "planning-bindings-openspec-artifact-invalid",
            "Runtime-required artifact has no regular output file",
            artifact=artifact,
            change_id=change_id,
        )
    return matched


def _artifact_trace(
    files: Mapping[str, bytes],
    relatives: Sequence[str],
    *,
    artifact: str,
    change_id: str,
    expected_requirements: set[str],
    expected_acceptance: set[str],
) -> None:
    requirements: set[str] = set()
    acceptance: set[str] = set()
    for relative in relatives:
        text = _decode_markdown(files[relative], artifact=artifact, change_id=change_id)
        requirements.update(REQ_ID.findall(text))
        acceptance.update(AC_ID.findall(text))
    if requirements != expected_requirements or acceptance != expected_acceptance:
        _fail(
            "planning-bindings-openspec-trace-invalid",
            "Required OpenSpec artifact must carry the exact Change trace IDs",
            artifact=artifact,
            change_id=change_id,
        )


def _change_artifact_facts(
    snapshot: Mapping[str, Any],
    change_id: str,
    *,
    expected_trace: Mapping[str, set[str]] | None,
    acceptance_relationships: Mapping[str, set[str]],
    runtime_artifacts: Mapping[str, str],
) -> dict[str, Any]:
    prefix = f"openspec/changes/{change_id}"
    records = [record for record in snapshot["records"] if record[1] == prefix or record[1].startswith(prefix + "/")]
    if not records:
        _fail("planning-bindings-change-missing", "Materialized Change disappeared", change_id=change_id)
    metadata = f"{prefix}/.openspec.yaml"
    files: Mapping[str, bytes] = snapshot["files"]
    if metadata not in files:
        _fail("planning-bindings-metadata-invalid", "Materialized Change metadata is missing or non-regular", change_id=change_id)

    if "specs" not in runtime_artifacts or "tasks" not in runtime_artifacts:
        _fail(
            "planning-bindings-openspec-status-incomplete",
            "OpenSpec runtime schema must expose specs and tasks artifacts",
            change_id=change_id,
        )
    artifact_files: dict[str, list[str]] = {}
    claimed_files: dict[str, str] = {}
    for artifact, output_pattern in sorted(runtime_artifacts.items()):
        outputs = _artifact_output_files(
            files,
            prefix,
            output_pattern,
            artifact=artifact,
            change_id=change_id,
        )
        for relative in outputs:
            owner = claimed_files.get(relative)
            if owner is not None:
                _fail(
                    "planning-bindings-openspec-instructions-invalid",
                    "Runtime artifact output paths overlap",
                    artifact=artifact,
                    conflicting_artifact=owner,
                    change_id=change_id,
                )
            claimed_files[relative] = artifact
        artifact_files[artifact] = outputs

    requirements: set[str] = set()
    acceptance: set[str] = set()
    acceptance_requirement_edges: set[tuple[str, str]] = set()
    delta_spec_count = 0
    for relative in artifact_files["specs"]:
        data = files[relative]
        delta_spec_count += 1
        text = _decode_markdown(data, artifact="specs", change_id=change_id)
        current_requirement: str | None = None
        for line in text.splitlines():
            requirement_heading = REQUIREMENT_HEADING.fullmatch(line)
            if requirement_heading is not None:
                identifiers = REQ_ID.findall(requirement_heading.group("title"))
                if len(identifiers) != 1:
                    _fail(
                        "planning-bindings-openspec-trace-invalid",
                        "Every delta Requirement heading must contain exactly one REQ ID",
                        change_id=change_id,
                    )
                current_requirement = identifiers[0]
                requirements.add(current_requirement)
                continue
            scenario_heading = SCENARIO_HEADING.fullmatch(line)
            if scenario_heading is not None:
                identifiers = AC_ID.findall(scenario_heading.group("title"))
                if len(identifiers) != 1 or current_requirement is None:
                    _fail(
                        "planning-bindings-openspec-trace-invalid",
                        "Every delta Scenario must carry one AC ID under a traced Requirement",
                        change_id=change_id,
                    )
                acceptance_id = identifiers[0]
                acceptance.add(acceptance_id)
                acceptance_requirement_edges.add((acceptance_id, current_requirement))
                continue
            heading = MARKDOWN_HEADING.match(line)
            if heading is not None and len(heading.group("marks")) <= 3:
                current_requirement = None
    if delta_spec_count == 0 or not requirements or not acceptance:
        _fail(
            "planning-bindings-openspec-trace-invalid",
            "Materialized Change needs delta Requirement and Scenario heading trace IDs",
            change_id=change_id,
        )

    target_requirements = requirements
    target_acceptance = acceptance
    if expected_trace is not None:
        target_requirements = set(expected_trace["requirement_ids"])
        target_acceptance = set(expected_trace["acceptance_ids"])
        if requirements != target_requirements or acceptance != target_acceptance:
            _fail(
                "planning-bindings-openspec-trace-invalid",
                "Delta trace IDs must exactly match the owning roadmap slice",
                artifact="specs",
                change_id=change_id,
            )
        expected_edges: set[tuple[str, str]] = set()
        for acceptance_id in sorted(target_acceptance):
            related = acceptance_relationships.get(acceptance_id)
            if related is None:
                _fail(
                    "planning-bindings-openspec-trace-invalid",
                    "Roadmap Acceptance ID has no catalog relationship",
                    acceptance_id=acceptance_id,
                    change_id=change_id,
                )
            expected_edges.update((acceptance_id, requirement_id) for requirement_id in related)
        if acceptance_requirement_edges != expected_edges:
            _fail(
                "planning-bindings-openspec-trace-invalid",
                "Delta Scenario-to-Requirement relationships must match the acceptance catalog",
                artifact="specs",
                change_id=change_id,
            )

    for artifact, outputs in sorted(artifact_files.items()):
        if artifact in {"specs", "tasks"}:
            continue
        _artifact_trace(
            files,
            outputs,
            artifact=artifact,
            change_id=change_id,
            expected_requirements=target_requirements,
            expected_acceptance=target_acceptance,
        )

    tasks_text = "\n".join(
        _decode_markdown(files[relative], artifact="tasks", change_id=change_id)
        for relative in artifact_files["tasks"]
    )
    task_lines = list(TASK_LINE.finditer(tasks_text))
    if any(len(task.group("indent").expandtabs(4)) > MAX_TASK_INDENT_COLUMNS for task in task_lines):
        _fail(
            "planning-bindings-tasks-invalid",
            "Task indentation exceeds the supported nesting budget",
            change_id=change_id,
        )
    unchecked = [match for match in task_lines if match.group("mark") == " "]
    if not unchecked:
        _fail(
            "planning-bindings-tasks-invalid",
            "M3 tasks.md must contain at least one real unchecked task",
            change_id=change_id,
        )
    task_requirements: set[str] = set()
    task_acceptance: set[str] = set()
    for task in task_lines:
        body = task.group("body")
        body_requirements = set(REQ_ID.findall(body))
        body_acceptance = set(AC_ID.findall(body))
        if task.group("mark") == " " and not (body_requirements or body_acceptance):
            _fail(
                "planning-bindings-tasks-invalid",
                "Every unchecked task must carry at least one Change trace ID",
                change_id=change_id,
            )
        task_requirements.update(body_requirements)
        task_acceptance.update(body_acceptance)
    if task_requirements != target_requirements or task_acceptance != target_acceptance:
        _fail(
            "planning-bindings-openspec-trace-invalid",
            "Task checkboxes must collectively carry the exact Change trace IDs",
            artifact="tasks",
            change_id=change_id,
        )

    return {
        "artifact_digest": _canonical_digest(records),
        "requirement_ids": sorted(requirements),
        "acceptance_ids": sorted(acceptance),
        "acceptance_requirement_edges": [
            {"acceptance_id": acceptance_id, "requirement_id": requirement_id}
            for acceptance_id, requirement_id in sorted(acceptance_requirement_edges)
        ],
        "runtime_artifacts_digest": _canonical_digest(dict(sorted(runtime_artifacts.items()))),
        "tasks_checked": any(task.group("mark") in {"x", "X"} for task in task_lines),
    }


def _status_is_complete(payload: Any, *, change_id: str, schema: str) -> bool:
    if not isinstance(payload, dict):
        return False
    artifacts = payload.get("artifacts")
    artifact_ids = (
        [item.get("id") for item in artifacts]
        if isinstance(artifacts, list) and all(isinstance(item, dict) for item in artifacts)
        else []
    )
    return (
        payload.get("changeName") == change_id
        and payload.get("schemaName") == schema
        and payload.get("isComplete") is True
        and isinstance(artifacts, list)
        and bool(artifacts)
        and len(artifacts) <= MAX_RUNTIME_ARTIFACTS
        and all(
            isinstance(item, dict)
            and item.get("status") == "done"
            and isinstance(item.get("id"), str)
            and SCHEMA_ID.fullmatch(item["id"]) is not None
            and isinstance(item.get("outputPath"), str)
            and bool(item["outputPath"])
            for item in artifacts
        )
        and len(artifact_ids) == len(set(artifact_ids))
    )


def _runtime_artifact_bindings(
    project_root: Path,
    pin: Any,
    status_payload: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    change_id: str,
    schema: str,
) -> tuple[dict[str, str], str]:
    artifacts = status_payload.get("artifacts")
    assert isinstance(artifacts, list)
    artifact_order = [item["id"] for item in artifacts]
    artifact_ids = set(artifact_order)
    prefix = f"openspec/changes/{change_id}"
    files = snapshot.get("files")
    assert isinstance(files, Mapping)
    _runtime_absolute_path(
        project_root,
        status_payload.get("changeRoot"),
        prefix,
        artifact="status",
        change_id=change_id,
    )
    status_artifact_paths = status_payload.get("artifactPaths")
    if not isinstance(status_artifact_paths, dict) or set(status_artifact_paths) != artifact_ids:
        _fail(
            "planning-bindings-openspec-status-incomplete",
            "Runtime status artifactPaths do not exactly cover the artifact graph",
            change_id=change_id,
        )
    raw_apply_requires = status_payload.get("applyRequires")
    if (
        not isinstance(raw_apply_requires, list)
        or not raw_apply_requires
        or not all(isinstance(identifier, str) and identifier in artifact_ids for identifier in raw_apply_requires)
        or len(raw_apply_requires) != len(set(raw_apply_requires))
    ):
        _fail(
            "planning-bindings-openspec-status-incomplete",
            "Runtime status has an invalid apply requirement set",
            change_id=change_id,
        )
    result: dict[str, str] = {}
    instruction_facts: dict[str, dict[str, Any]] = {}
    for item in artifacts:
        assert isinstance(item, dict)
        artifact_id = item["id"]
        assert isinstance(artifact_id, str)
        status_output = _runtime_output_pattern(
            item.get("outputPath"),
            artifact=artifact_id,
            change_id=change_id,
        )
        expected_existing = _artifact_output_files(
            files,
            prefix,
            status_output,
            artifact=artifact_id,
            change_id=change_id,
        )
        relative_output = f"{prefix}/{status_output}"
        status_paths = status_artifact_paths.get(artifact_id)
        if not isinstance(status_paths, dict) or status_paths.get("outputPath") != status_output:
            _fail(
                "planning-bindings-openspec-status-incomplete",
                "Runtime status path summary disagrees with its artifact",
                artifact=artifact_id,
                change_id=change_id,
            )
        _runtime_absolute_path(
            project_root,
            status_paths.get("resolvedOutputPath"),
            relative_output,
            artifact=artifact_id,
            change_id=change_id,
        )
        normalized_status_existing = _runtime_existing_outputs(
            project_root,
            status_paths.get("existingOutputPaths"),
            expected_existing,
            artifact=artifact_id,
            change_id=change_id,
        )
        try:
            response = openspec_adapter.run_openspec(
                project_root,
                pin,
                "instructions",
                change_id=change_id,
                artifact_id=artifact_id,
            )
        except openspec_adapter.OpenSpecAdapterError as exc:
            _openspec_error(
                "planning-bindings-openspec-instructions-failed",
                "Runtime artifact instructions could not be verified",
                exc,
                artifact=artifact_id,
                change_id=change_id,
            )
        instructions = response.get("result")
        if not isinstance(instructions, dict):
            _fail(
                "planning-bindings-openspec-instructions-invalid",
                "Runtime artifact instructions have an invalid shape",
                artifact=artifact_id,
                change_id=change_id,
            )
        instruction_output = _runtime_output_pattern(
            instructions.get("outputPath"),
            artifact=artifact_id,
            change_id=change_id,
        )
        description = instructions.get("description")
        instruction = instructions.get("instruction")
        template = instructions.get("template")
        context = instructions.get("context")
        rules = instructions.get("rules")
        dependencies = instructions.get("dependencies")
        unlocks = instructions.get("unlocks")
        if (
            instructions.get("changeName") != change_id
            or instructions.get("artifactId") != artifact_id
            or instructions.get("schemaName") != schema
            or instruction_output != status_output
            or not isinstance(description, str)
            or not description.strip()
            or not isinstance(instruction, str)
            or not instruction.strip()
            or not isinstance(template, str)
            or not template.strip()
            or (context is not None and not isinstance(context, str))
            or (rules is not None and (not isinstance(rules, list) or not all(isinstance(rule, str) for rule in rules)))
            or not isinstance(dependencies, list)
            or not isinstance(unlocks, list)
        ):
            _fail(
                "planning-bindings-openspec-instructions-invalid",
                "Runtime status and instructions disagree or have an invalid shape",
                artifact=artifact_id,
                change_id=change_id,
            )
        _runtime_absolute_path(
            project_root,
            instructions.get("changeDir"),
            prefix,
            artifact=artifact_id,
            change_id=change_id,
        )
        normalized_instruction_output = _runtime_absolute_path(
            project_root,
            instructions.get("resolvedOutputPath"),
            relative_output,
            artifact=artifact_id,
            change_id=change_id,
        )
        normalized_instruction_existing = _runtime_existing_outputs(
            project_root,
            instructions.get("existingOutputPaths"),
            expected_existing,
            artifact=artifact_id,
            change_id=change_id,
        )
        if normalized_instruction_existing != normalized_status_existing:
            _fail(
                "planning-bindings-openspec-instructions-invalid",
                "Runtime status and instructions disagree on existing outputs",
                artifact=artifact_id,
                change_id=change_id,
            )
        dependency_facts: list[dict[str, Any]] = []
        dependency_ids: set[str] = set()
        for dependency in dependencies:
            if not isinstance(dependency, dict):
                _fail(
                    "planning-bindings-openspec-instructions-invalid",
                    "Runtime dependency entry must be an object",
                    artifact=artifact_id,
                    change_id=change_id,
                )
            dependency_id = dependency.get("id")
            dependency_description = dependency.get("description")
            if (
                not isinstance(dependency_id, str)
                or dependency_id not in artifact_ids
                or dependency_id == artifact_id
                or dependency_id in dependency_ids
                or dependency.get("done") is not True
                or not isinstance(dependency_description, str)
            ):
                _fail(
                    "planning-bindings-openspec-instructions-invalid",
                    "Runtime dependency entry is invalid or inconsistent with complete status",
                    artifact=artifact_id,
                    change_id=change_id,
                )
            dependency_path = _runtime_output_pattern(
                dependency.get("path"),
                artifact=dependency_id,
                change_id=change_id,
            )
            dependency_ids.add(dependency_id)
            dependency_facts.append(
                {
                    "id": dependency_id,
                    "done": True,
                    "output_path": dependency_path,
                    "description_digest": _sha256(dependency_description.encode("utf-8")),
                }
            )
        if (
            not all(isinstance(identifier, str) and identifier in artifact_ids for identifier in unlocks)
            or artifact_id in unlocks
            or len(unlocks) != len(set(unlocks))
        ):
            _fail(
                "planning-bindings-openspec-instructions-invalid",
                "Runtime unlock graph contains invalid artifact IDs",
                artifact=artifact_id,
                change_id=change_id,
            )
        result[artifact_id] = status_output
        instruction_facts[artifact_id] = {
            "output_path": status_output,
            "resolved_output": normalized_instruction_output,
            "existing_outputs": normalized_instruction_existing,
            "description_digest": _sha256(description.encode("utf-8")),
            "instruction_digest": _sha256(instruction.encode("utf-8")),
            "template_digest": _sha256(template.encode("utf-8")),
            "context_digest": _sha256(context.encode("utf-8")) if isinstance(context, str) else None,
            "rules_digest": _canonical_digest(rules) if isinstance(rules, list) else None,
            "dependencies": sorted(dependency_facts, key=lambda dependency: dependency["id"]),
            "unlocks": sorted(unlocks),
        }
    runtime_digest = _canonical_digest(
        {
            "artifact_order": artifact_order,
            "apply_requires": raw_apply_requires,
            "instructions": dict(sorted(instruction_facts.items())),
        }
    )
    return result, runtime_digest


def _strict_validation_passed(payload: Any, *, change_id: str) -> bool:
    if not isinstance(payload, dict):
        return False
    items = payload.get("items")
    totals = payload.get("summary", {}).get("totals") if isinstance(payload.get("summary"), dict) else None
    return (
        isinstance(items, list)
        and len(items) == 1
        and isinstance(items[0], dict)
        and items[0].get("id") == change_id
        and items[0].get("type") == "change"
        and items[0].get("valid") is True
        and isinstance(totals, dict)
        and totals.get("items") == 1
        and totals.get("passed") == 1
        and totals.get("failed") == 0
    )


def _normalize_git(result: Mapping[str, Any], *, baseline: str, exclusions: Sequence[str]) -> dict[str, Any]:
    expected_exclusion_digest = _canonical_digest(sorted(exclusions))
    if result.get("excluded_paths_digest") != expected_exclusion_digest:
        _fail("planning-bindings-git-exclusion-mismatch", "Git adapter returned an unexpected exclusion binding")
    keys = (
        "repository_scope_id",
        "checkout_scope_id",
        "checkout_kind",
        "head",
        "ref",
        "dirty_digest",
        "excluded_paths_digest",
        "integration_owner",
    )
    normalized = {key: result.get(key) for key in keys}
    normalized["baseline"] = baseline
    return normalized


def _validate_git_baseline(
    root: Path,
    git_binding: Mapping[str, Any],
    *,
    baseline: str,
    expected_git_dir: str | os.PathLike[str] | None,
) -> None:
    if git_binding.get("checkout_kind") == "linked":
        if expected_git_dir is None:
            _fail("planning-bindings-baseline-invalid", "Linked checkout baseline lacks authorized Git metadata")
        git_dir = Path(os.path.abspath(os.fspath(expected_git_dir)))
    else:
        git_dir = root / ".git"
    try:
        exists = git_scope._run_git(root, git_dir, ["cat-file", "-e", f"{baseline}^{{commit}}"])
        ancestor = git_scope._run_git(
            root,
            git_dir,
            ["merge-base", "--is-ancestor", baseline, str(git_binding.get("head"))],
        )
    except git_scope.GitScopeError as exc:
        _fail(
            "planning-bindings-baseline-invalid",
            "Git baseline could not be validated safely",
            cause_code=exc.code,
        )
    if exists.returncode != 0:
        _fail("planning-bindings-baseline-invalid", "Git baseline is not an existing commit")
    if ancestor.returncode == 1:
        _fail("planning-bindings-baseline-not-ancestor", "Git baseline is not an ancestor of checkout HEAD")
    if ancestor.returncode != 0:
        _fail("planning-bindings-baseline-invalid", "Git baseline ancestry could not be verified")


def _capture_git(
    root: Path,
    *,
    baseline: str,
    exclusions: Sequence[str],
    expected_git_dir: str | os.PathLike[str] | None,
    expected_common_dir: str | os.PathLike[str] | None,
    authority_ref: str | None,
) -> tuple[dict[str, Any], bool]:
    try:
        raw = git_scope.inspect_git_scope(
            root,
            expected_git_dir=expected_git_dir,
            expected_common_dir=expected_common_dir,
            authority_ref=authority_ref,
            excluded_paths=exclusions,
        )
    except git_scope.GitScopeError as exc:
        _fail(
            "planning-bindings-git-scope-failed",
            "Ambient Git scope could not be validated",
            cause_code=exc.code,
        )
    return _normalize_git(raw, baseline=baseline, exclusions=exclusions), bool(raw.get("dirty"))


def _openspec_error(code: str, message: str, error: openspec_adapter.OpenSpecAdapterError, **details: Any) -> None:
    _fail(code, message, cause_code=error.code, **details)


def collect_external_bindings(
    root: str | os.PathLike[str],
    *,
    project_mode: str,
    baseline: str,
    approved_decisions: Iterable[str],
    pending_decisions: Iterable[str],
    blocking_decisions: Iterable[str],
    authority_paths: Mapping[str, str] | None = None,
    expected_git_dir: str | os.PathLike[str] | None = None,
    expected_common_dir: str | os.PathLike[str] | None = None,
    git_authority_ref: str | None = None,
    openspec_executable: str | os.PathLike[str] | None = None,
    readiness: str = "seal",
) -> dict[str, Any]:
    """Collect current source, Git, and OpenSpec facts without writing.

    The returned object has exactly two public sections: ``external_bindings``
    for :func:`planning.validate_planning_bundle`, and safe ``diagnostics``.
    No absolute path, process output, or source/artifact content is returned.
    """

    project_root = _canonical_root(root)
    if project_mode not in {"greenfield", "brownfield"}:
        _fail("planning-bindings-project-mode-invalid", "project_mode must be greenfield or brownfield")
    if not isinstance(baseline, str) or GIT_OID.fullmatch(baseline) is None:
        _fail("planning-bindings-baseline-invalid", "baseline must be a full lowercase Git object ID")
    if readiness not in {"seal", "change_ready"}:
        _fail("planning-bindings-readiness-invalid", "readiness must be seal or change_ready")

    paths = _authority_paths(authority_paths)
    requirements, acceptance, roadmap, authority_digests, authority_bytes = _strict_discovery_documents(
        project_root,
        paths,
    )
    registered_sources, declared_source_mismatches = _registered_sources(project_root, requirements, paths)
    acceptance_relationships = _acceptance_relationships(acceptance)
    roadmap_trace = _roadmap_change_trace(roadmap)
    roadmap_changes = sorted(roadmap_trace)

    decisions = {
        "approved": _normalize_decisions(approved_decisions, label="approved"),
        "pending": _normalize_decisions(pending_decisions, label="pending"),
        "blocking": _normalize_decisions(blocking_decisions, label="blocking"),
    }
    memberships = [set(decisions[key]) for key in ("approved", "pending", "blocking")]
    if any(memberships[left] & memberships[right] for left, right in ((0, 1), (0, 2), (1, 2))):
        _fail("planning-bindings-decisions-invalid", "Decision states must be disjoint")
    decision_artifacts, decision_bytes = _bind_decision_documents(project_root, decisions)
    decisions["artifacts"] = decision_artifacts
    decisions["authority_digest"] = _canonical_digest(decision_artifacts)

    exclusions = [
        "delivery-docs/state",
        paths["requirements"],
        paths["acceptance"],
        paths["features"],
        paths["roadmap"],
        "openspec",
    ]
    if len(set(exclusions)) != len(exclusions):
        _fail("planning-bindings-authority-invalid", "Authority paths collide with fixed Git exclusions")
    exclusions = sorted(exclusions)
    git_binding, git_dirty = _capture_git(
        project_root,
        baseline=baseline,
        exclusions=exclusions,
        expected_git_dir=expected_git_dir,
        expected_common_dir=expected_common_dir,
        authority_ref=git_authority_ref,
    )
    _validate_git_baseline(
        project_root,
        git_binding,
        baseline=baseline,
        expected_git_dir=expected_git_dir,
    )

    try:
        tree_audit = openspec_adapter.audit_openspec_tree(project_root)
    except openspec_adapter.OpenSpecAdapterError as exc:
        _openspec_error(
            "planning-bindings-openspec-unsafe",
            "OpenSpec tree could not be audited safely",
            exc,
        )
    if not tree_audit.get("safe"):
        _fail(
            "planning-bindings-openspec-unsafe",
            "OpenSpec tree failed its safety audit",
            blockers=list(tree_audit.get("blockers", [])),
        )
    try:
        config = openspec_adapter.inspect_config(project_root)
    except openspec_adapter.OpenSpecAdapterError as exc:
        _openspec_error(
            "planning-bindings-openspec-config-unsafe",
            "OpenSpec config could not be audited safely",
            exc,
        )
    if config.get("status") == "missing":
        _fail("planning-bindings-openspec-config-missing", "OpenSpec config is required for binding collection")
    if (
        not config.get("cli_allowed")
        or config.get("status") != "present"
        or not isinstance(config.get("digest"), str)
        or SHA256.fullmatch(config["digest"]) is None
        or not isinstance(config.get("schema"), str)
        or not config["schema"].strip()
        or SCHEMA_ID.fullmatch(config["schema"]) is None
    ):
        _fail(
            "planning-bindings-openspec-config-unsafe",
            "OpenSpec config is not safe and complete",
            blockers=list(config.get("blockers", [])),
        )

    initial_tree = _tree_snapshot(project_root)
    config_path = config["path"]
    assert isinstance(config_path, str)
    initial_config = initial_tree["files"].get(config_path)
    if initial_config is None or _sha256(initial_config) != config["digest"]:
        _fail("planning-bindings-openspec-config-raced", "OpenSpec config changed during binding collection")
    active_changes = _active_changes(initial_tree, roadmap_changes)
    actual_set = set(active_changes)
    all_change_ids = sorted(actual_set | set(roadmap_changes))
    if len(all_change_ids) > MAX_CHANGES:
        _fail("planning-bindings-change-budget", "Combined Change count exceeds its budget")

    try:
        pin = openspec_adapter.resolve_openspec_executable(
            project_root,
            explicit=openspec_executable,
        )
    except openspec_adapter.OpenSpecAdapterError as exc:
        _openspec_error(
            "planning-bindings-openspec-cli-unavailable",
            "A safe OpenSpec executable could not be pinned",
            exc,
        )
    try:
        version = openspec_adapter.probe_version(project_root, pin)
    except openspec_adapter.OpenSpecAdapterError as exc:
        _openspec_error(
            "planning-bindings-openspec-version-failed",
            "OpenSpec version could not be verified",
            exc,
        )

    changes: dict[str, dict[str, Any]] = {}
    for change_id in all_change_ids:
        if change_id not in actual_set:
            changes[change_id] = {"state": "reserved", "artifact_digest": None}
            continue
        try:
            status = openspec_adapter.run_openspec(project_root, pin, "status", change_id=change_id)
        except openspec_adapter.OpenSpecAdapterError as exc:
            _openspec_error(
                "planning-bindings-openspec-status-failed",
                "Materialized Change status could not be verified",
                exc,
                change_id=change_id,
            )
        if not _status_is_complete(status.get("result"), change_id=change_id, schema=config["schema"]):
            _fail(
                "planning-bindings-openspec-status-incomplete",
                "Materialized Change artifacts are incomplete or status shape drifted",
                change_id=change_id,
            )
        status_result = status["result"]
        assert isinstance(status_result, dict)
        runtime_artifacts, runtime_instructions_digest = _runtime_artifact_bindings(
            project_root,
            pin,
            status_result,
            initial_tree,
            change_id=change_id,
            schema=config["schema"],
        )
        facts = _change_artifact_facts(
            initial_tree,
            change_id,
            expected_trace=roadmap_trace.get(change_id),
            acceptance_relationships=acceptance_relationships,
            runtime_artifacts=runtime_artifacts,
        )
        try:
            strict = openspec_adapter.run_openspec(project_root, pin, "validate", change_id=change_id)
        except openspec_adapter.OpenSpecAdapterError as exc:
            _openspec_error(
                "planning-bindings-openspec-validation-failed",
                "Materialized Change failed strict OpenSpec validation",
                exc,
                change_id=change_id,
            )
        if not _strict_validation_passed(strict.get("result"), change_id=change_id):
            _fail(
                "planning-bindings-openspec-validation-failed",
                "Materialized Change strict validation shape or result is invalid",
                change_id=change_id,
            )
        changes[change_id] = {
            "state": "materialized",
            "metadata_valid": True,
            "strict_valid": True,
            "tasks_checked": facts["tasks_checked"],
            "artifact_digest": facts["artifact_digest"],
            "requirement_ids": facts["requirement_ids"],
            "acceptance_ids": facts["acceptance_ids"],
            "acceptance_requirement_edges": facts["acceptance_requirement_edges"],
            "runtime_artifacts_digest": facts["runtime_artifacts_digest"],
            "runtime_instructions_digest": runtime_instructions_digest,
        }

    # Re-read every independently bound input after external processes.  This
    # detects a malicious or racy CLI changing files during an otherwise
    # successful read-only collection.
    try:
        final_version = openspec_adapter.probe_version(project_root, pin)
    except openspec_adapter.OpenSpecAdapterError as exc:
        _openspec_error(
            "planning-bindings-openspec-version-failed",
            "OpenSpec executable identity changed during binding collection",
            exc,
        )
    if final_version.get("version") != version.get("version"):
        _fail("planning-bindings-openspec-version-raced", "OpenSpec version changed during binding collection")
    final_tree = _tree_snapshot(project_root)
    if final_tree["digest"] != initial_tree["digest"]:
        _fail("planning-bindings-openspec-tree-raced", "OpenSpec tree changed during binding collection")
    try:
        final_config = openspec_adapter.inspect_config(project_root)
    except openspec_adapter.OpenSpecAdapterError as exc:
        _openspec_error(
            "planning-bindings-openspec-config-raced",
            "OpenSpec config became unsafe during binding collection",
            exc,
        )
    if (
        final_config.get("status") != "present"
        or final_config.get("cli_allowed") is not True
        or final_config.get("path") != config_path
        or final_config.get("digest") != config["digest"]
        or final_config.get("schema") != config["schema"]
    ):
        _fail("planning-bindings-openspec-config-raced", "OpenSpec config changed during binding collection")
    for key, original in authority_bytes.items():
        current = _safe_relative_file(project_root, paths[key], max_bytes=MAX_AUTHORITY_BYTES)
        if current != original:
            _fail("planning-bindings-authority-raced", "Planning authority changed during binding collection", artifact=paths[key])
    for relative, original in decision_bytes.items():
        current = _safe_relative_file(project_root, relative, max_bytes=MAX_DECISION_BYTES)
        if current != original:
            _fail(
                "planning-bindings-decision-raced",
                "Decision authority changed during binding collection",
                artifact=relative,
            )
    final_sources, _ = _registered_sources(project_root, requirements, paths)
    if final_sources != registered_sources:
        _fail("planning-bindings-source-raced", "Registered source changed during binding collection")
    final_git, _ = _capture_git(
        project_root,
        baseline=baseline,
        exclusions=exclusions,
        expected_git_dir=expected_git_dir,
        expected_common_dir=expected_common_dir,
        authority_ref=git_authority_ref,
    )
    if final_git != git_binding:
        _fail("planning-bindings-git-raced", "Ambient Git scope changed during binding collection")
    _validate_git_baseline(
        project_root,
        final_git,
        baseline=baseline,
        expected_git_dir=expected_git_dir,
    )

    external_bindings = {
        "schema_version": BINDINGS_SCHEMA_VERSION,
        "project_mode": project_mode,
        "authority_paths": dict(paths),
        "registered_sources": dict(sorted(registered_sources.items())),
        "decisions": decisions,
        "git": git_binding,
        "openspec": {
            "schema": config["schema"],
            "config_digest": config["digest"],
            "tree_digest": initial_tree["digest"],
            "cli_version": version["version"],
            "executable_digest": pin.sha256,
            "changes": dict(sorted(changes.items())),
        },
    }
    diagnostics = {
        "schema_version": BINDINGS_SCHEMA_VERSION,
        "readiness": readiness,
        "authority_paths": dict(paths),
        "authority_digests": authority_digests,
        "registered_source_count": len(registered_sources),
        "declared_source_digest_mismatch_count": declared_source_mismatches,
        "decision_artifact_count": len(decision_artifacts),
        "decision_authority_digest": decisions["authority_digest"],
        "git": {
            "checkout_kind": git_binding["checkout_kind"],
            "dirty": git_dirty,
            "excluded_paths": exclusions,
            "excluded_paths_digest": git_binding["excluded_paths_digest"],
            "external_authority_used": git_binding["checkout_kind"] == "linked",
        },
        "openspec": {
            "config_path": config["path"],
            "tree_entries": initial_tree["entries"],
            "active_change_count": len(active_changes),
            "materialized_changes": sorted(actual_set),
            "reserved_changes": sorted(set(all_change_ids) - actual_set),
        },
        "writes_performed": False,
        "implementation_authorized": False,
    }
    return {"external_bindings": external_bindings, "diagnostics": diagnostics}


__all__ = ["BINDINGS_SCHEMA_VERSION", "PlanningBindingsError", "collect_external_bindings"]
