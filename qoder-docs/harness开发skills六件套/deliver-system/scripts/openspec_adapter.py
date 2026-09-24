#!/usr/bin/env python3
"""Fail-closed OpenSpec v1 adapter used by the M3 planning harness.

The adapter deliberately exposes only the small JSON workflow surface needed
for planning.  It is not a YAML parser, an OpenSpec lifecycle wrapper, or a
general command runner.  Existing configuration bytes remain authoritative.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import selectors
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


ADAPTER_SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 256 * 1024
MAX_CONTEXT_BYTES = 50 * 1024
MAX_EXECUTABLE_BYTES = 16 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_TIMEOUT_SECONDS = 120.0
DEFAULT_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_TREE_ENTRIES = 20_000
MAX_TREE_DEPTH = 32
SUPPORTED_VERSION_MIN = (1, 0, 0)
SUPPORTED_VERSION_MAX_EXCLUSIVE = (2, 0, 0)

RECOGNIZED_CONFIG_FIELDS = {"schema", "context", "rules", "references", "store"}
READ_OPERATIONS = {"schemas", "status", "instructions", "validate"}
PUBLIC_OPERATIONS = {"version", *READ_OPERATIONS, "new-change"}
FORBIDDEN_LIFECYCLE_WORDS = {
    "apply",
    "archive",
    "sync",
    "verify",
    "update",
    "continue",
    "ff",
    "fast-forward",
}
KEBAB_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
ARTIFACT_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
APPROVAL_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,255}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TOP_LEVEL_KEY = re.compile(
    r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*|'(?:[^']|'')*'|\"(?:[^\"\\]|\\.)*\")\s*:(?P<value>.*)$"
)


class OpenSpecAdapterError(RuntimeError):
    """Structured, stable adapter failure."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


@dataclass(frozen=True)
class ExecutablePin:
    """Identity checked immediately before every OpenSpec process."""

    path: Path
    sha256: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    safe_path: str


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _canonical_project_root(project_root: Path | str) -> Path:
    lexical = Path(os.path.abspath(os.fspath(project_root)))
    try:
        mode = lexical.lstat().st_mode
    except OSError as exc:
        raise OpenSpecAdapterError(
            "project-root-unavailable",
            "Project root cannot be inspected",
            reason=str(exc),
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise OpenSpecAdapterError(
            "project-root-unsafe",
            "Project root must be a real directory, not a link or special entry",
        )
    return lexical.resolve(strict=True)


def _strip_yaml_comment(line: str) -> str:
    """Strip a YAML comment without interpreting quoted content."""

    result: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote == "'":
            result.append(char)
            if char == "'" and index + 1 < len(line) and line[index + 1] == "'":
                result.append("'")
                index += 2
                continue
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            result.append(char)
            if char == "\\" and index + 1 < len(line):
                result.append(line[index + 1])
                index += 2
                continue
            if char == '"':
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            result.append(char)
            index += 1
            continue
        if char == "#" and (index == 0 or line[index - 1].isspace()):
            break
        result.append(char)
        index += 1
    return "".join(result)


def _yaml_unsafe_tokens(line: str) -> set[str]:
    """Find YAML features intentionally outside this conservative adapter."""

    clean = _strip_yaml_comment(line)
    outside: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(clean):
        char = clean[index]
        if quote == "'":
            if char == "'" and index + 1 < len(clean) and clean[index + 1] == "'":
                index += 2
                continue
            if char == "'":
                quote = None
            outside.append(" ")
            index += 1
            continue
        if quote == '"':
            if char == "\\" and index + 1 < len(clean):
                outside.extend((" ", " "))
                index += 2
                continue
            if char == '"':
                quote = None
            outside.append(" ")
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            outside.append(" ")
        else:
            outside.append(char)
        index += 1

    value = "".join(outside)
    found: set[str] = set()
    if re.search(r"(?:^|[\s\[{,:])&[A-Za-z0-9_-]+", value):
        found.add("yaml-anchor")
    if re.search(r"(?:^|[\s\[{,:])\*[A-Za-z0-9_-]+", value):
        found.add("yaml-alias")
    if re.search(r"(?:^|[\s\[{,:])![^\s,}\]]+", value):
        found.add("yaml-tag")
    if re.search(r"(?:^|\s)<<\s*:", value):
        found.add("yaml-merge-key")
    return found


def _decode_yaml_key(token: str) -> str:
    if token.startswith("'"):
        return token[1:-1].replace("''", "'")
    if token.startswith('"'):
        try:
            value = json.loads(token)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid quoted top-level key") from exc
        if not isinstance(value, str):
            raise ValueError("top-level key is not a string")
        return value
    return token


def _simple_yaml_scalar(value: str) -> str | None:
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.startswith("'") and stripped.endswith("'") and len(stripped) >= 2:
        return stripped[1:-1].replace("''", "'")
    if stripped.startswith('"') and stripped.endswith('"') and len(stripped) >= 2:
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, str) else None
    if stripped[0] in "[{&*!|>" or stripped in {"null", "Null", "NULL", "~"}:
        return None
    return stripped


def _field_child_lines(lines: list[str], start: int, end: int) -> list[str]:
    return lines[start + 1 : end]


def _has_yaml_value(value: str, children: Sequence[str]) -> bool:
    stripped = value.strip()
    if stripped in {"", "[]", "{}", "null", "Null", "NULL", "~"}:
        return any(_strip_yaml_comment(line).strip() for line in children)
    return True


def _audit_config_bytes(data: bytes, label: str) -> dict[str, Any]:
    blockers: set[str] = set()
    warnings: set[str] = set()
    if len(data) > MAX_CONFIG_BYTES:
        blockers.add("config-too-large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
        blockers.add("config-not-utf8")
    if "\x00" in text:
        blockers.add("config-nul-byte")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
        blockers.add("config-control-character")

    lines = text.splitlines()
    top_fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(lines, 1):
        if not line.strip() or not _strip_yaml_comment(line).strip():
            continue
        prefix = line[: len(line) - len(line.lstrip(" \t"))]
        if "\t" in prefix:
            blockers.add("yaml-tab-indentation")
        blockers.update(_yaml_unsafe_tokens(line))
        if prefix:
            continue
        clean = _strip_yaml_comment(line).rstrip()
        if clean in {"---", "..."} or clean.startswith("%YAML") or clean.startswith("%TAG"):
            blockers.add("yaml-multiple-documents-or-directive")
            continue
        match = TOP_LEVEL_KEY.match(clean)
        if not match:
            blockers.add("yaml-unsupported-top-level-syntax")
            continue
        try:
            key = _decode_yaml_key(match.group("key"))
        except ValueError:
            blockers.add("yaml-invalid-top-level-key")
            continue
        if key in seen:
            blockers.add("config-duplicate-top-level-key")
        seen.add(key)
        top_fields.append(
            {
                "key": key,
                "value": match.group("value").strip(),
                "line": line_number,
                "index": line_number - 1,
            }
        )

    for index, field in enumerate(top_fields):
        field["end_index"] = (
            top_fields[index + 1]["index"] if index + 1 < len(top_fields) else len(lines)
        )

    by_key = {field["key"]: field for field in top_fields}
    unknown = sorted(set(by_key) - RECOGNIZED_CONFIG_FIELDS)
    if unknown:
        warnings.add("config-unknown-fields-preserved")

    schema: str | None = None
    if "schema" in by_key:
        schema = _simple_yaml_scalar(by_key["schema"]["value"])
        if not schema or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", schema):
            blockers.add("config-schema-invalid")
    elif text.strip():
        blockers.add("config-schema-missing")

    context_bytes = 0
    if "context" in by_key:
        field = by_key["context"]
        value = field["value"]
        children = _field_child_lines(lines, field["index"], field["end_index"])
        if re.fullmatch(r"[|>][1-9]?[+-]?", value):
            context_bytes = len("\n".join(children).encode("utf-8"))
        else:
            scalar = _simple_yaml_scalar(value)
            if scalar is None:
                blockers.add("config-context-invalid")
            else:
                context_bytes = len(scalar.encode("utf-8"))
        if context_bytes > MAX_CONTEXT_BYTES:
            blockers.add("config-context-too-large")

    store_present = False
    if "store" in by_key:
        field = by_key["store"]
        children = _field_child_lines(lines, field["index"], field["end_index"])
        store = _simple_yaml_scalar(field["value"])
        if store is None or any(_strip_yaml_comment(line).strip() for line in children):
            blockers.add("config-store-malformed")
        else:
            store_present = True
            blockers.add("config-store-pointer-unsupported")

    references_present = False
    if "references" in by_key:
        field = by_key["references"]
        children = _field_child_lines(lines, field["index"], field["end_index"])
        references_present = _has_yaml_value(field["value"], children)
        if references_present:
            blockers.add("config-external-references-unsupported")

    if "rules" in by_key:
        warnings.add("config-rules-require-runtime-schema-check")

    empty = not any(_strip_yaml_comment(line).strip() for line in lines)
    if empty:
        blockers.add("config-empty")

    return {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "path": label,
        "status": "empty" if empty else ("unsafe" if blockers else "present"),
        "size": len(data),
        "digest": _sha256_bytes(data),
        "recognized_fields": sorted(set(by_key) & RECOGNIZED_CONFIG_FIELDS),
        "unknown_fields": unknown,
        "schema": schema,
        "context_bytes": context_bytes,
        "has_rules": "rules" in by_key,
        "has_references": references_present,
        "has_store": store_present,
        "blockers": sorted(blockers),
        "warnings": sorted(warnings),
        "cli_allowed": not blockers,
    }


def inspect_config(project_root: Path | str) -> dict[str, Any]:
    """Audit config.yaml/config.yml without parsing or rewriting general YAML."""

    root = _canonical_project_root(project_root)
    openspec = root / "openspec"
    try:
        openspec_mode = openspec.lstat().st_mode
    except FileNotFoundError:
        openspec_mode = None
    except OSError:
        openspec_mode = -1
    if openspec_mode is not None and (
        openspec_mode == -1 or stat.S_ISLNK(openspec_mode) or not stat.S_ISDIR(openspec_mode)
    ):
        return {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "path": None,
            "status": "unsafe",
            "size": None,
            "digest": None,
            "recognized_fields": [],
            "unknown_fields": [],
            "schema": None,
            "context_bytes": 0,
            "has_rules": False,
            "has_references": False,
            "has_store": False,
            "blockers": ["openspec-root-link-or-special"],
            "warnings": [],
            "cli_allowed": False,
        }
    candidates = [openspec / "config.yaml", openspec / "config.yml"]
    present: list[Path] = []
    entry_blockers: set[str] = set()
    for candidate in candidates:
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError:
            entry_blockers.add("config-uninspectable")
            continue
        present.append(candidate)
        if stat.S_ISLNK(mode):
            entry_blockers.add("config-symbolic-link")
        elif not stat.S_ISREG(mode):
            entry_blockers.add("config-special-entry")

    if len(present) > 1:
        return {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "path": None,
            "status": "conflict",
            "size": None,
            "digest": None,
            "recognized_fields": [],
            "unknown_fields": [],
            "schema": None,
            "context_bytes": 0,
            "has_rules": False,
            "has_references": False,
            "has_store": False,
            "blockers": ["config-dual-files", *sorted(entry_blockers)],
            "warnings": [],
            "cli_allowed": False,
        }
    if not present:
        return {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "path": None,
            "status": "missing",
            "size": None,
            "digest": None,
            "recognized_fields": [],
            "unknown_fields": [],
            "schema": None,
            "context_bytes": 0,
            "has_rules": False,
            "has_references": False,
            "has_store": False,
            "blockers": sorted(entry_blockers),
            "warnings": ["config-missing"],
            "cli_allowed": not entry_blockers,
        }
    path = present[0]
    if entry_blockers:
        return {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "path": path.relative_to(root).as_posix(),
            "status": "unsafe",
            "size": None,
            "digest": None,
            "recognized_fields": [],
            "unknown_fields": [],
            "schema": None,
            "context_bytes": 0,
            "has_rules": False,
            "has_references": False,
            "has_store": False,
            "blockers": sorted(entry_blockers),
            "warnings": [],
            "cli_allowed": False,
        }
    try:
        size = path.stat().st_size
        if size > MAX_CONFIG_BYTES:
            data = b""
            result = _audit_config_bytes(data, path.relative_to(root).as_posix())
            result.update(size=size, digest=None, status="unsafe", cli_allowed=False)
            result["blockers"] = sorted(set(result["blockers"]) | {"config-too-large"})
            return result
        data = path.read_bytes()
    except OSError as exc:
        raise OpenSpecAdapterError(
            "config-read-failed",
            "OpenSpec config could not be read",
            reason=str(exc),
        ) from exc
    return _audit_config_bytes(data, path.relative_to(root).as_posix())


def audit_openspec_tree(project_root: Path | str) -> dict[str, Any]:
    """Boundedly reject links, special nodes, and traversal ambiguity."""

    root = _canonical_project_root(project_root)
    openspec = root / "openspec"
    try:
        root_mode = openspec.lstat().st_mode
    except FileNotFoundError:
        return {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "status": "missing",
            "entries": 0,
            "blockers": [],
            "safe": True,
        }
    except OSError as exc:
        return {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "status": "unsafe",
            "entries": 0,
            "blockers": [f"openspec-root-uninspectable:{type(exc).__name__}"],
            "safe": False,
        }
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        return {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "status": "unsafe",
            "entries": 1,
            "blockers": ["openspec-root-link-or-special"],
            "safe": False,
        }

    blockers: set[str] = set()
    entries = 0
    stack: list[tuple[Path, int]] = [(openspec, 0)]
    while stack and not blockers:
        directory, depth = stack.pop()
        if depth > MAX_TREE_DEPTH:
            blockers.add("openspec-tree-depth-exceeded")
            break
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            blockers.add("openspec-tree-unreadable")
            break
        for child in children:
            entries += 1
            if entries > MAX_TREE_ENTRIES:
                blockers.add("openspec-tree-entry-budget-exceeded")
                break
            child_path = Path(child.path)
            try:
                mode = child.stat(follow_symlinks=False).st_mode
            except OSError:
                blockers.add("openspec-tree-entry-uninspectable")
                break
            if stat.S_ISLNK(mode):
                blockers.add("openspec-tree-symbolic-link")
                break
            if stat.S_ISDIR(mode):
                stack.append((child_path, depth + 1))
            elif not stat.S_ISREG(mode):
                blockers.add("openspec-tree-special-entry")
                break

    return {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "status": "unsafe" if blockers else "present",
        "entries": entries,
        "blockers": sorted(blockers),
        "safe": not blockers,
    }


def _safe_path_directories(root: Path, path_value: str) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for raw in path_value.split(os.pathsep):
        if not raw:
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_dir() or _is_within(resolved, root) or resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def _pin_executable(candidate: Path, root: Path, safe_dirs: list[Path]) -> ExecutablePin:
    lexical = Path(os.path.abspath(os.fspath(candidate)))
    if _is_within(lexical, root):
        raise OpenSpecAdapterError(
            "openspec-executable-inside-project",
            "OpenSpec executable must not be controlled by the target project",
        )
    try:
        resolved = lexical.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise OpenSpecAdapterError(
            "openspec-executable-unavailable",
            "OpenSpec executable could not be resolved",
            reason=str(exc),
        ) from exc
    if _is_within(resolved, root):
        raise OpenSpecAdapterError(
            "openspec-executable-inside-project",
            "Resolved OpenSpec executable points inside the target project",
        )
    if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        raise OpenSpecAdapterError(
            "openspec-executable-invalid",
            "OpenSpec executable must be an executable regular file",
        )
    if info.st_size > MAX_EXECUTABLE_BYTES:
        raise OpenSpecAdapterError(
            "openspec-executable-too-large",
            "OpenSpec executable exceeds the pinning budget",
        )
    try:
        digest = _sha256_bytes(resolved.read_bytes())
    except OSError as exc:
        raise OpenSpecAdapterError(
            "openspec-executable-read-failed",
            "OpenSpec executable could not be pinned",
            reason=str(exc),
        ) from exc
    if resolved.parent not in safe_dirs:
        safe_dirs.insert(0, resolved.parent)
    return ExecutablePin(
        path=resolved,
        sha256=digest,
        device=info.st_dev,
        inode=info.st_ino,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        safe_path=os.pathsep.join(os.fspath(path) for path in safe_dirs),
    )


def resolve_openspec_executable(
    project_root: Path | str,
    *,
    explicit: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ExecutablePin:
    """Resolve and pin one project-external executable without changing cwd."""

    root = _canonical_project_root(project_root)
    source_env = os.environ if environ is None else environ
    safe_dirs = _safe_path_directories(root, source_env.get("PATH", ""))
    if explicit is not None:
        candidate = Path(explicit)
        if not candidate.is_absolute():
            raise OpenSpecAdapterError(
                "openspec-executable-not-absolute",
                "Explicit OpenSpec executable path must be absolute",
            )
        return _pin_executable(candidate, root, safe_dirs)
    candidate_names = ["openspec"]
    if os.name == "nt":
        # Windows executables carry an extension listed in PATHEXT.
        extensions = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
        candidate_names = [f"openspec{ext}" for ext in extensions] + ["openspec"]
    for directory in safe_dirs:
        for name in candidate_names:
            candidate = directory / name
            try:
                candidate.lstat()
            except OSError:
                continue
            return _pin_executable(candidate, root, safe_dirs)
    raise OpenSpecAdapterError(
        "openspec-executable-not-found",
        "No project-external OpenSpec executable was found on the safe PATH",
    )


def _verify_pin(pin: ExecutablePin) -> None:
    try:
        info = pin.path.stat()
    except OSError as exc:
        raise OpenSpecAdapterError(
            "openspec-executable-pin-stale",
            "Pinned OpenSpec executable is no longer available",
            reason=str(exc),
        ) from exc
    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    expected = (pin.device, pin.inode, pin.size, pin.mtime_ns)
    if identity != expected or not stat.S_ISREG(info.st_mode) or not os.access(pin.path, os.X_OK):
        raise OpenSpecAdapterError(
            "openspec-executable-pin-stale",
            "Pinned OpenSpec executable identity changed",
        )
    try:
        digest = _sha256_bytes(pin.path.read_bytes())
    except OSError as exc:
        raise OpenSpecAdapterError(
            "openspec-executable-pin-stale",
            "Pinned OpenSpec executable cannot be re-read",
            reason=str(exc),
        ) from exc
    if digest != pin.sha256:
        raise OpenSpecAdapterError(
            "openspec-executable-pin-stale",
            "Pinned OpenSpec executable content changed",
        )


def _minimal_environment(pin: ExecutablePin) -> dict[str, str]:
    return {
        "PATH": pin.safe_path,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "CI": "1",
        "OPENSPEC_TELEMETRY": "0",
        "DO_NOT_TRACK": "1",
    }


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _execute_bounded(
    root: Path,
    pin: ExecutablePin,
    arguments: Sequence[str],
    *,
    timeout_seconds: float,
    max_output_bytes: int,
) -> tuple[int, bytes, bytes]:
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
        raise OpenSpecAdapterError("openspec-timeout-invalid", "Timeout must be numeric")
    if timeout_seconds <= 0 or timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise OpenSpecAdapterError(
            "openspec-timeout-invalid",
            f"Timeout must be between 0 and {MAX_TIMEOUT_SECONDS} seconds",
        )
    if not isinstance(max_output_bytes, int) or isinstance(max_output_bytes, bool):
        raise OpenSpecAdapterError("openspec-output-limit-invalid", "Output limit must be an integer")
    if max_output_bytes <= 0 or max_output_bytes > MAX_OUTPUT_BYTES:
        raise OpenSpecAdapterError(
            "openspec-output-limit-invalid",
            f"Output limit must be between 1 and {MAX_OUTPUT_BYTES} bytes",
        )
    _verify_pin(pin)
    argv = [os.fspath(pin.path), *arguments]
    try:
        process = subprocess.Popen(
            argv,
            cwd=root,
            env=_minimal_environment(pin),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            start_new_session=(os.name == "posix"),
        )
    except OSError as exc:
        raise OpenSpecAdapterError(
            "openspec-process-start-failed",
            "OpenSpec process could not be started",
            reason=str(exc),
        ) from exc

    try:
        _verify_pin(pin)
    except OpenSpecAdapterError:
        _terminate_process(process)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        raise

    assert process.stdout is not None and process.stderr is not None
    if os.name == "nt":
        # Windows select() only supports sockets, not pipe descriptors.
        # communicate() reads both pipes on reader threads and provides the
        # same bounded wait; the output budget is enforced after collection.
        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=float(timeout_seconds))
        except subprocess.TimeoutExpired as exc:
            _terminate_process(process)
            process.stdout.close()
            process.stderr.close()
            raise OpenSpecAdapterError(
                "openspec-process-timeout",
                "OpenSpec process exceeded its time budget",
            ) from exc
        process.stdout.close()
        process.stderr.close()
        if len(stdout_bytes) + len(stderr_bytes) > max_output_bytes:
            raise OpenSpecAdapterError(
                "openspec-output-limit-exceeded",
                "OpenSpec output exceeded its byte budget",
            )
        _verify_pin(pin)
        return process.returncode, stdout_bytes, stderr_bytes
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    deadline = time.monotonic() + float(timeout_seconds)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process(process)
                raise OpenSpecAdapterError(
                    "openspec-process-timeout",
                    "OpenSpec process exceeded its time budget",
                )
            events = selector.select(min(remaining, 0.1))
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > max_output_bytes:
                    _terminate_process(process)
                    raise OpenSpecAdapterError(
                        "openspec-output-limit-exceeded",
                        "OpenSpec output exceeded its byte budget",
                    )
                buffers[key.data].extend(chunk)
        remaining = max(0.0, deadline - time.monotonic())
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            _terminate_process(process)
            raise OpenSpecAdapterError(
                "openspec-process-timeout",
                "OpenSpec process exceeded its time budget",
            ) from exc
    finally:
        selector.close()
        if process.poll() is None:
            _terminate_process(process)
        process.stdout.close()
        process.stderr.close()
    _verify_pin(pin)
    return return_code, bytes(buffers["stdout"]), bytes(buffers["stderr"])


def _decode_process_output(data: bytes, stream: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OpenSpecAdapterError(
            "openspec-output-not-utf8",
            f"OpenSpec {stream} was not valid UTF-8",
        ) from exc


def probe_version(
    project_root: Path | str,
    pin: ExecutablePin,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_OUTPUT_BYTES,
) -> dict[str, Any]:
    root = _canonical_project_root(project_root)
    code, stdout_bytes, stderr_bytes = _execute_bounded(
        root,
        pin,
        ["--version"],
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    stdout = _decode_process_output(stdout_bytes, "stdout").strip()
    stderr = _decode_process_output(stderr_bytes, "stderr").strip()
    if code != 0:
        raise OpenSpecAdapterError(
            "openspec-version-failed",
            "OpenSpec version probe failed",
            returncode=code,
            stderr=stderr,
        )
    match = re.fullmatch(r"(?:OpenSpec\s+)?v?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?", stdout)
    if not match:
        raise OpenSpecAdapterError(
            "openspec-version-invalid",
            "OpenSpec returned an unrecognized version",
            stdout=stdout,
        )
    version_tuple = tuple(int(part) for part in match.groups())
    if not (SUPPORTED_VERSION_MIN <= version_tuple < SUPPORTED_VERSION_MAX_EXCLUSIVE):
        raise OpenSpecAdapterError(
            "openspec-version-incompatible",
            "OpenSpec version is outside the supported 1.x adapter range",
            version=".".join(str(part) for part in version_tuple),
        )
    return {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "version": ".".join(str(part) for part in version_tuple),
        "compatible": True,
    }


def _validate_change_id(change_id: str | None) -> str:
    if not isinstance(change_id, str) or len(change_id) > 100 or not KEBAB_ID.fullmatch(change_id):
        raise OpenSpecAdapterError(
            "openspec-change-id-invalid",
            "Change ID must be a kebab-case identifier of at most 100 characters",
        )
    if change_id in FORBIDDEN_LIFECYCLE_WORDS:
        raise OpenSpecAdapterError(
            "openspec-change-id-reserved",
            "Change ID collides with a forbidden lifecycle word",
        )
    return change_id


def _project_cli_preflight(root: Path) -> dict[str, Any]:
    tree = audit_openspec_tree(root)
    if not tree["safe"]:
        raise OpenSpecAdapterError(
            "openspec-tree-unsafe",
            "OpenSpec tree failed link/special-entry preflight",
            blockers=tree["blockers"],
        )
    config = inspect_config(root)
    if not config["cli_allowed"]:
        raise OpenSpecAdapterError(
            "openspec-config-unsafe",
            "OpenSpec config failed conservative preflight",
            blockers=config["blockers"],
        )
    return config


def _json_command(
    root: Path,
    pin: ExecutablePin,
    arguments: Sequence[str],
    *,
    timeout_seconds: float,
    max_output_bytes: int,
) -> Any:
    code, stdout_bytes, stderr_bytes = _execute_bounded(
        root,
        pin,
        arguments,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    stdout = _decode_process_output(stdout_bytes, "stdout")
    stderr = _decode_process_output(stderr_bytes, "stderr").strip()
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise OpenSpecAdapterError(
            "openspec-output-not-json",
            "OpenSpec did not return one JSON document on stdout",
            returncode=code,
            stderr=stderr,
        ) from exc
    if not isinstance(payload, (dict, list)):
        raise OpenSpecAdapterError(
            "openspec-output-shape-invalid",
            "OpenSpec JSON output must be an object or array",
        )
    if code != 0:
        raise OpenSpecAdapterError(
            "openspec-command-failed",
            "OpenSpec command returned a failure status",
            returncode=code,
            stderr=stderr,
            payload=payload,
        )
    return payload


def run_openspec(
    project_root: Path | str,
    pin: ExecutablePin,
    operation: str,
    *,
    change_id: str | None = None,
    artifact_id: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_OUTPUT_BYTES,
) -> dict[str, Any]:
    """Run one enumerated read operation; arbitrary argv is impossible."""

    if operation not in PUBLIC_OPERATIONS:
        raise OpenSpecAdapterError(
            "openspec-operation-forbidden",
            "Operation is outside the M3 OpenSpec allowlist",
            operation=operation,
        )
    if operation == "new-change":
        raise OpenSpecAdapterError(
            "openspec-new-change-requires-authorized-api",
            "Use create_new_change with approval and config CAS binding",
        )
    if operation == "version":
        return {"operation": "version", **probe_version(project_root, pin, timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes)}

    root = _canonical_project_root(project_root)
    _project_cli_preflight(root)
    version = probe_version(root, pin, timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes)
    if operation == "schemas":
        arguments = ["schemas", "--json"]
    elif operation == "status":
        change = _validate_change_id(change_id)
        arguments = ["status", "--change", change, "--json"]
    elif operation == "instructions":
        change = _validate_change_id(change_id)
        if (
            not isinstance(artifact_id, str)
            or len(artifact_id) > 100
            or not ARTIFACT_ID.fullmatch(artifact_id)
            or artifact_id in FORBIDDEN_LIFECYCLE_WORDS
        ):
            raise OpenSpecAdapterError(
                "openspec-artifact-id-invalid",
                "Artifact ID is invalid or names a lifecycle command",
            )
        arguments = ["instructions", artifact_id, "--change", change, "--json"]
    else:
        change = _validate_change_id(change_id)
        arguments = ["validate", change, "--strict", "--json", "--no-interactive"]
    payload = _json_command(
        root,
        pin,
        arguments,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    return {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "operation": operation,
        "openspec_version": version["version"],
        "result": payload,
    }


def _validate_approval(approval_ref: str) -> str:
    if not isinstance(approval_ref, str) or not APPROVAL_REF.fullmatch(approval_ref):
        raise OpenSpecAdapterError(
            "approval-ref-invalid",
            "Approval reference must be a durable, whitespace-free identifier",
        )
    return approval_ref


def create_new_change(
    project_root: Path | str,
    pin: ExecutablePin,
    change_id: str,
    *,
    approval_ref: str,
    expected_config_digest: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_OUTPUT_BYTES,
) -> dict[str, Any]:
    """Create exactly one official OpenSpec scaffold after approval and CAS."""

    root = _canonical_project_root(project_root)
    change = _validate_change_id(change_id)
    approval = _validate_approval(approval_ref)
    if not isinstance(expected_config_digest, str) or not SHA256.fullmatch(expected_config_digest):
        raise OpenSpecAdapterError(
            "config-cas-invalid",
            "New Change requires the expected SHA-256 of a present config",
        )
    config = _project_cli_preflight(root)
    if config["status"] == "missing" or config["digest"] != expected_config_digest:
        raise OpenSpecAdapterError(
            "config-cas-mismatch",
            "OpenSpec config changed or is missing since approval",
            actual=config["digest"],
        )
    target = root / "openspec" / "changes" / change
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise OpenSpecAdapterError(
            "openspec-change-target-uninspectable",
            "Change target cannot be inspected",
            reason=str(exc),
        ) from exc
    else:
        raise OpenSpecAdapterError(
            "openspec-change-already-exists",
            "OpenSpec Change already exists",
            change_id=change,
        )

    version = probe_version(root, pin, timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes)
    payload = _json_command(
        root,
        pin,
        ["new", "change", change, "--json"],
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    post_tree = audit_openspec_tree(root)
    post_config = inspect_config(root)
    if not post_tree["safe"]:
        raise OpenSpecAdapterError(
            "openspec-new-change-postcondition-failed",
            "OpenSpec created an unsafe tree",
            blockers=post_tree["blockers"],
        )
    if post_config["digest"] != expected_config_digest:
        raise OpenSpecAdapterError(
            "openspec-new-change-config-drift",
            "OpenSpec config changed while creating the Change",
            actual=post_config["digest"],
        )
    metadata = target / ".openspec.yaml"
    try:
        target_mode = target.lstat().st_mode
        metadata_mode = metadata.lstat().st_mode
    except OSError as exc:
        raise OpenSpecAdapterError(
            "openspec-new-change-metadata-missing",
            "Official Change scaffold or .openspec.yaml metadata is missing",
            reason=str(exc),
        ) from exc
    if not stat.S_ISDIR(target_mode) or stat.S_ISLNK(target_mode) or not stat.S_ISREG(metadata_mode) or stat.S_ISLNK(metadata_mode):
        raise OpenSpecAdapterError(
            "openspec-new-change-metadata-unsafe",
            "Official Change scaffold metadata is not a regular local file",
        )
    return {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "operation": "new-change",
        "openspec_version": version["version"],
        "change_id": change,
        "approval_ref": approval,
        "config_digest": expected_config_digest,
        "result": payload,
    }


def default_config_candidate() -> bytes:
    return (
        b"# Managed only through a reviewed byte-exact delivery proposal.\n"
        b"schema: spec-driven\n"
    )


def _candidate_bytes(candidate: bytes | str | None) -> bytes:
    if candidate is None:
        data = default_config_candidate()
    elif isinstance(candidate, bytes):
        data = candidate
    elif isinstance(candidate, str):
        data = candidate.encode("utf-8")
    else:
        raise OpenSpecAdapterError(
            "config-candidate-type-invalid",
            "Config candidate must be UTF-8 text or bytes",
        )
    audit = _audit_config_bytes(data, "candidate")
    if not audit["cli_allowed"]:
        raise OpenSpecAdapterError(
            "config-candidate-unsafe",
            "Config candidate failed conservative audit",
            blockers=audit["blockers"],
        )
    if audit["has_store"] or audit["has_references"]:
        raise OpenSpecAdapterError(
            "config-candidate-external-root-forbidden",
            "M3 candidates cannot introduce store or references",
        )
    return data


def plan_config_candidate(
    project_root: Path | str,
    candidate: bytes | str | None = None,
) -> dict[str, Any]:
    """Return a stdout-ready, deterministic proposal without writing files."""

    root = _canonical_project_root(project_root)
    data = _candidate_bytes(candidate)
    current = inspect_config(root)
    if current["status"] == "conflict" or any(
        blocker
        in {
            "config-symbolic-link",
            "config-special-entry",
            "config-uninspectable",
            "openspec-root-link-or-special",
            "config-store-pointer-unsupported",
            "config-external-references-unsupported",
        }
        for blocker in current["blockers"]
    ):
        raise OpenSpecAdapterError(
            "config-current-unsafe",
            "Existing config location is unsafe for a proposal",
            blockers=current["blockers"],
        )
    candidate_digest = _sha256_bytes(data)
    if current["digest"] == candidate_digest:
        action = "noop"
    elif current["status"] == "missing":
        action = "create"
    else:
        action = "replace-exact-bytes-requires-explicit-opt-in"
    return {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "operation": "config-plan",
        "action": action,
        "candidate": data.decode("utf-8"),
        "candidate_digest": candidate_digest,
        "expected_config_digest": "absent" if current["status"] == "missing" else current["digest"],
        "current_path": current["path"],
        "current_unknown_fields": current["unknown_fields"],
        "preserves_existing_by_default": current["status"] != "missing",
    }


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


class _DirectoryHandle:
    """Windows substitute for an open directory descriptor.

    POSIX CAS operations anchor every step in an open directory fd. Windows
    cannot open directories, so the handle pins the verified (dev, ino, type)
    identity of the directory and re-validates it before every operation.
    """

    __slots__ = ("path", "identity")

    def __init__(self, path: Path, metadata: os.stat_result) -> None:
        self.path = path
        self.identity = (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))

    def verify(self, code: str, message: str) -> None:
        try:
            current = self.path.lstat()
        except OSError as exc:
            raise OpenSpecAdapterError(code, message, reason=str(exc)) from exc
        identity = (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode))
        if identity != self.identity:
            raise OpenSpecAdapterError(code, message)


def _close_directory(handle: "int | _DirectoryHandle") -> None:
    if isinstance(handle, _DirectoryHandle):
        return
    os.close(handle)


def _fsync_directory(handle: "int | _DirectoryHandle") -> None:
    if isinstance(handle, _DirectoryHandle):
        # Windows cannot fsync a directory descriptor; NTFS commits the
        # metadata update as part of the create/rename itself.
        return
    os.fsync(handle)


def _open_directory_handle(path: Path, *, code: str, message: str) -> "int | _DirectoryHandle":
    if os.name == "nt":
        try:
            info = path.lstat()
        except OSError as exc:
            raise OpenSpecAdapterError(code, message, reason=str(exc)) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OpenSpecAdapterError(code, message)
        return _DirectoryHandle(path, info)
    return os.open(path, _directory_flags())


def _open_subdirectory(
    directory_fd: "int | _DirectoryHandle",
    name: str,
    *,
    code: str,
    message: str,
) -> "int | _DirectoryHandle":
    if isinstance(directory_fd, _DirectoryHandle):
        directory_fd.verify(code, message)
        return _open_directory_handle(directory_fd.path / name, code=code, message=message)
    try:
        return os.open(name, _directory_flags(), dir_fd=directory_fd)
    except OSError as exc:
        raise OpenSpecAdapterError(code, message, reason=str(exc)) from exc


def _stat_entry(directory_fd: "int | _DirectoryHandle", name: str) -> os.stat_result:
    if isinstance(directory_fd, _DirectoryHandle):
        directory_fd.verify("config-cas-race", "Config location changed while applying approved bytes")
        return (directory_fd.path / name).lstat()
    return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)


def _mkdir_entry(directory_fd: "int | _DirectoryHandle", name: str) -> None:
    if isinstance(directory_fd, _DirectoryHandle):
        os.mkdir(directory_fd.path / name, 0o755)
        return
    os.mkdir(name, 0o755, dir_fd=directory_fd)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short config write")
        view = view[written:]


def _exclusive_create_at(directory_fd: "int | _DirectoryHandle", name: str, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    if isinstance(directory_fd, _DirectoryHandle):
        directory_fd.verify("config-cas-race", "Config directory changed while applying approved bytes")
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(directory_fd.path / name, flags, 0o644)
    else:
        descriptor = os.open(name, flags, 0o644, dir_fd=directory_fd)
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(directory_fd)


def _read_regular_at(directory_fd: "int | _DirectoryHandle", name: str) -> tuple[bytes, int]:
    windows_handle = directory_fd if isinstance(directory_fd, _DirectoryHandle) else None
    try:
        if windows_handle is not None:
            windows_handle.verify(
                "config-cas-race",
                "Config location changed while applying approved bytes",
            )
            info = (windows_handle.path / name).lstat()
        else:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise OpenSpecAdapterError(
            "config-cas-race",
            "Config location changed while applying approved bytes",
            reason=str(exc),
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise OpenSpecAdapterError(
            "config-cas-race",
            "Config became a link or special entry while applying approved bytes",
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if windows_handle is not None:
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(windows_handle.path / name, flags)
    else:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, 65_536)
            if not block:
                break
            total += len(block)
            if total > MAX_CONFIG_BYTES:
                raise OpenSpecAdapterError(
                    "config-cas-race",
                    "Config exceeded the size budget during CAS verification",
                )
            chunks.append(block)
        return b"".join(chunks), stat.S_IMODE(info.st_mode)
    finally:
        os.close(descriptor)


def _atomic_exact_replace_at(directory_fd: "int | _DirectoryHandle", name: str, data: bytes, current_mode: int) -> None:
    temporary = f".{name}.delivery-{os.getpid()}-{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    windows_handle = directory_fd if isinstance(directory_fd, _DirectoryHandle) else None
    if windows_handle is not None:
        windows_handle.verify("config-cas-race", "Config directory changed while applying approved bytes")
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        temporary_path = windows_handle.path / temporary
        descriptor = os.open(temporary_path, flags, 0o600)
    else:
        temporary_path = None
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
    try:
        _write_all(descriptor, data)
        if windows_handle is None:
            os.fchmod(descriptor, current_mode)
        os.fsync(descriptor)
    except BaseException:
        try:
            if temporary_path is not None:
                os.unlink(temporary_path)
            else:
                os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)
    if windows_handle is not None:
        os.replace(temporary_path, windows_handle.path / name)
    else:
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    _fsync_directory(directory_fd)


def apply_config_candidate(
    project_root: Path | str,
    candidate: bytes | str,
    *,
    candidate_digest: str,
    expected_config_digest: str,
    approval_ref: str,
    allow_existing_replacement: bool = False,
) -> dict[str, Any]:
    """CAS-create or explicitly replace with the exact reviewed bytes."""

    root = _canonical_project_root(project_root)
    approval = _validate_approval(approval_ref)
    data = _candidate_bytes(candidate)
    actual_candidate_digest = _sha256_bytes(data)
    if candidate_digest != actual_candidate_digest or not SHA256.fullmatch(candidate_digest):
        raise OpenSpecAdapterError(
            "config-candidate-digest-mismatch",
            "Candidate bytes differ from the reviewed digest",
        )
    current = inspect_config(root)
    if current["status"] == "conflict" or any(
        blocker
        in {
            "config-symbolic-link",
            "config-special-entry",
            "config-uninspectable",
            "openspec-root-link-or-special",
            "config-store-pointer-unsupported",
            "config-external-references-unsupported",
        }
        for blocker in current["blockers"]
    ):
        raise OpenSpecAdapterError(
            "config-current-unsafe",
            "Existing config location is unsafe to mutate",
            blockers=current["blockers"],
        )

    root_fd = _open_directory_handle(
        root,
        code="openspec-root-unsafe",
        message="Project root cannot be opened safely",
    )
    openspec_fd: "int | _DirectoryHandle | None" = None
    try:
        if current["status"] == "missing":
            if expected_config_digest != "absent":
                raise OpenSpecAdapterError(
                    "config-cas-mismatch",
                    "Config absence does not match the approved expectation",
                )
            try:
                _mkdir_entry(root_fd, "openspec")
                _fsync_directory(root_fd)
            except FileExistsError:
                pass
            openspec_fd = _open_subdirectory(
                root_fd,
                "openspec",
                code="openspec-root-unsafe",
                message="OpenSpec root changed to a link or special entry",
            )
            for name in ("config.yaml", "config.yml"):
                try:
                    _stat_entry(openspec_fd, name)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise OpenSpecAdapterError(
                        "config-cas-race",
                        "Config location became uninspectable",
                        reason=str(exc),
                    ) from exc
                raise OpenSpecAdapterError(
                    "config-cas-race",
                    "Config appeared after approval; no bytes were overwritten",
                )
            _exclusive_create_at(openspec_fd, "config.yaml", data)
            action = "created"
        else:
            if expected_config_digest != current["digest"]:
                raise OpenSpecAdapterError(
                    "config-cas-mismatch",
                    "Existing config differs from the approved bytes",
                    actual=current["digest"],
                )
            openspec_fd = _open_subdirectory(
                root_fd,
                "openspec",
                code="config-cas-race",
                message="OpenSpec root changed while applying approved bytes",
            )
            assert current["path"] is not None
            name = Path(current["path"]).name
            current_bytes, current_mode = _read_regular_at(openspec_fd, name)
            if _sha256_bytes(current_bytes) != expected_config_digest:
                raise OpenSpecAdapterError(
                    "config-cas-race",
                    "Config bytes changed during CAS verification",
                )
            other_name = "config.yml" if name == "config.yaml" else "config.yaml"
            try:
                _stat_entry(openspec_fd, other_name)
            except FileNotFoundError:
                pass
            else:
                raise OpenSpecAdapterError(
                    "config-cas-race",
                    "A second config appeared during CAS verification",
                )
            if actual_candidate_digest == current["digest"]:
                action = "unchanged"
            else:
                if not allow_existing_replacement:
                    raise OpenSpecAdapterError(
                        "config-existing-preserved",
                        "Existing config is byte-preserved by default; exact replacement needs explicit opt-in",
                    )
                _atomic_exact_replace_at(openspec_fd, name, data, current_mode)
                action = "replaced-exact-bytes"
    finally:
        if openspec_fd is not None:
            _close_directory(openspec_fd)
        _close_directory(root_fd)

    post = inspect_config(root)
    if post["digest"] != actual_candidate_digest or not post["cli_allowed"]:
        raise OpenSpecAdapterError(
            "config-postcondition-failed",
            "Applied config bytes failed post-write audit",
            blockers=post["blockers"],
        )
    return {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "operation": "config-apply",
        "action": action,
        "approval_ref": approval,
        "config_digest": actual_candidate_digest,
        "path": post["path"],
        "requires_openspec_post_write_verification": action != "unchanged",
    }
