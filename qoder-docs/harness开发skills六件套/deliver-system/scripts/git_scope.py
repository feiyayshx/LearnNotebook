#!/usr/bin/env python3
"""Read-only Git checkout identity for the deliver-system M3 planner.

The adapter supports a conventional main checkout and a conventional linked
Git worktree.  It never manages worktrees and never exposes local paths in its
returned identity.  A linked checkout's external metadata is not accessed
until the caller supplies an exact git-dir, common-dir, and authority reference.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence


SCHEMA_VERSION = 1
METADATA_ENTRY_LIMIT = 200_000
METADATA_FILE_LIMIT_BYTES = 4 * 1024 * 1024
POINTER_FILE_LIMIT_BYTES = 4 * 1024
GIT_OUTPUT_LIMIT_BYTES = 4 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 15
ATTRIBUTE_ENTRY_LIMIT = 50_000
ATTRIBUTE_FILE_LIMIT_BYTES = 1024 * 1024
AUTHORITY_REF_LIMIT = 256
DIRTY_CONTENT_LIMIT_BYTES = 64 * 1024 * 1024
EXCLUDED_PATH_LIMIT = 128
EXCLUDED_PATH_BYTES_LIMIT = 16 * 1024
EXCLUDED_PATH_COMPONENT = re.compile(r"[A-Za-z0-9._+-]+\Z")

_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SECTION = re.compile(
    r'^\[\s*([A-Za-z0-9.-]+)(?:\s+(?:"(?:[^"\\]|\\.)*"|[^\]]+))?\s*\]'
    r"(?:\s*[#;].*)?\Z"
)
_VARIABLE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)\s*(?:=\s*(.*))?\Z")
_AUTHORITY_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


class GitScopeError(RuntimeError):
    """Fail-closed checkout validation error with a stable reason code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise GitScopeError(code, detail)


def _canonical_json_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _opaque_id(prefix: str, value: Any) -> str:
    return f"{prefix}-{_canonical_json_digest(value)}"


def _canonical_root(root: Path) -> Path:
    supplied = Path(os.path.abspath(os.fspath(root)))
    try:
        canonical = supplied.resolve(strict=True)
    except OSError as exc:
        _fail("git_scope.root_unavailable", f"project root cannot be resolved ({exc})")
    if supplied != canonical or not canonical.is_dir():
        _fail("git_scope.root_not_canonical", "project root must be an existing canonical directory")
    return canonical


def _validated_excluded_paths(root: Path, excluded_paths: Sequence[str]) -> tuple[str, ...]:
    """Validate a bounded, project-relative set before constructing pathspecs."""

    if isinstance(excluded_paths, (str, bytes, bytearray)):
        _fail("git_scope.exclude_invalid", "excluded_paths must be a sequence of relative paths")
    try:
        declared_length = len(excluded_paths)
    except (TypeError, ValueError, OverflowError):
        _fail("git_scope.exclude_invalid", "excluded_paths must be a sized sequence")
    if declared_length > EXCLUDED_PATH_LIMIT:
        _fail("git_scope.exclude_budget", "excluded_paths exceeds its entry budget")
    try:
        values = list(excluded_paths)
    except (TypeError, ValueError):
        _fail("git_scope.exclude_invalid", "excluded_paths must be a bounded sequence")
    if len(values) != declared_length or len(values) > EXCLUDED_PATH_LIMIT:
        _fail("git_scope.exclude_budget", "excluded_paths exceeds its entry budget")
    total_bytes = 0
    validated: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            _fail("git_scope.exclude_invalid", "every excluded path must be a string")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            _fail("git_scope.exclude_invalid", "an excluded path is not valid UTF-8")
        total_bytes += len(encoded)
        if total_bytes > EXCLUDED_PATH_BYTES_LIMIT:
            _fail("git_scope.exclude_budget", "excluded_paths exceeds its byte budget")
        if (
            not value
            or value.startswith(("/", "-", ":"))
            or "\\" in value
            or "\x00" in value
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        ):
            _fail("git_scope.exclude_invalid", "an excluded path is not a safe project-relative path")
        components = value.split("/")
        if any(
            not component
            or component in {".", ".."}
            or not EXCLUDED_PATH_COMPONENT.fullmatch(component)
            for component in components
        ):
            _fail("git_scope.exclude_invalid", "an excluded path is not canonical or literal-safe")
        if components[0] == ".git":
            _fail("git_scope.exclude_invalid", "Git metadata cannot be an excluded worktree path")
        canonical = "/".join(components)
        if canonical in seen:
            _fail("git_scope.exclude_invalid", "excluded_paths contains a duplicate path")
        seen.add(canonical)

        current = root
        for index, component in enumerate(components):
            current = current / component
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                if index != len(components) - 1:
                    _fail("git_scope.exclude_parent_unsafe", "an excluded path has a missing parent")
                break
            except OSError as exc:
                _fail(
                    "git_scope.exclude_parent_unsafe",
                    f"an excluded path cannot be inspected ({exc})",
                )
            if stat.S_ISLNK(metadata.st_mode):
                _fail("git_scope.exclude_parent_unsafe", "an excluded path crosses a symbolic link")
            if index != len(components) - 1 and not stat.S_ISDIR(metadata.st_mode):
                _fail("git_scope.exclude_parent_unsafe", "an excluded path has a non-directory parent")
            if index == len(components) - 1 and not (
                stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
            ):
                _fail("git_scope.exclude_target_unsafe", "an excluded path is a special node")
        validated.append(canonical)
    return tuple(sorted(validated))


def _excluded_pathspecs(excluded_paths: Sequence[str]) -> list[str]:
    """Return only fixed top-level literal exclusions; callers add ``--``."""

    return [f":(top,exclude,literal){path}" for path in excluded_paths]


def _normalize_expected(value: Path | str | None, label: str) -> Path:
    if value is None:
        _fail("git_scope.authority_required", f"linked checkout requires explicit {label}")
    raw = os.fspath(value)
    if not raw or "\x00" in raw or any(character in raw for character in "\r\n"):
        _fail("git_scope.authority_invalid", f"{label} is not a valid absolute path")
    path = Path(raw)
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        _fail("git_scope.authority_invalid", f"{label} must be an absolute normalized path")
    normalized = Path(os.path.normpath(raw))
    if normalized != path:
        _fail("git_scope.authority_invalid", f"{label} must be an absolute normalized path")
    return normalized


def _validate_authority_ref(authority_ref: str | None) -> str:
    if authority_ref is None:
        _fail("git_scope.authority_required", "linked checkout requires an authority_ref")
    if not isinstance(authority_ref, str):
        _fail("git_scope.authority_invalid", "authority_ref must be a string")
    if not authority_ref or len(authority_ref.encode("utf-8")) > AUTHORITY_REF_LIMIT:
        _fail("git_scope.authority_invalid", "authority_ref is empty or exceeds its bounded size")
    if not _AUTHORITY_REF.fullmatch(authority_ref):
        _fail("git_scope.authority_invalid", "authority_ref must be an opaque decision identifier")
    return authority_ref


def _lstat_kind(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        _fail("git_scope.metadata_missing", f"{label} is missing or stale")
    except OSError as exc:
        _fail("git_scope.metadata_unreadable", f"{label} cannot be inspected ({exc})")
    return metadata


def _assert_no_symlink_components(path: Path, label: str) -> None:
    """Reject every symbolic or special parent component of an authorized path."""

    if not path.is_absolute():
        _fail("git_scope.metadata_escape", f"{label} is not absolute")
    anchor = Path(path.anchor)
    current = anchor
    for index, part in enumerate(path.parts[1:]):
        current = current / part
        metadata = _lstat_kind(current, label)
        if stat.S_ISLNK(metadata.st_mode):
            _fail("git_scope.metadata_symlink", f"{label} crosses a symbolic link")
        if index < len(path.parts[1:]) - 1 and not stat.S_ISDIR(metadata.st_mode):
            _fail("git_scope.metadata_special", f"{label} has a non-directory parent component")


def _read_bounded_regular(path: Path, *, label: str, limit: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        _fail("git_scope.metadata_unreadable", f"{label} cannot be safely opened ({exc})")
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail("git_scope.metadata_special", f"{label} is not a regular file")
        if metadata.st_size > limit:
            _fail("git_scope.metadata_oversized", f"{label} exceeds its bounded size")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) > limit:
            _fail("git_scope.metadata_oversized", f"{label} exceeds its bounded size")
        return value
    finally:
        os.close(descriptor)


def _read_text(path: Path, *, label: str, limit: int = METADATA_FILE_LIMIT_BYTES) -> str:
    value = _read_bounded_regular(path, label=label, limit=limit)
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        _fail("git_scope.metadata_encoding", f"{label} is not valid UTF-8")


def _single_line(text: str, label: str) -> str:
    if "\x00" in text or "\r" in text:
        _fail("git_scope.pointer_malformed", f"{label} contains an invalid control character")
    lines = text.splitlines()
    if len(lines) != 1 or not lines[0].strip():
        _fail("git_scope.pointer_malformed", f"{label} must contain exactly one non-empty line")
    return lines[0]


def _pointer_target(text: str, *, base: Path, label: str, prefix: str | None = None) -> Path:
    line = _single_line(text, label)
    if prefix is not None:
        marker = f"{prefix}: "
        if not line.startswith(marker) or not line[len(marker) :]:
            _fail("git_scope.pointer_malformed", f"{label} must use the exact '{marker}' form")
        raw = line[len(marker) :]
    else:
        raw = line
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw):
        _fail("git_scope.pointer_malformed", f"{label} contains a control character")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    return Path(os.path.abspath(os.path.normpath(os.fspath(candidate))))


def _scan_metadata_tree(root: Path, *, label: str) -> None:
    root_metadata = _lstat_kind(root, label)
    if stat.S_ISLNK(root_metadata.st_mode):
        _fail("git_scope.metadata_symlink", f"{label} is a symbolic link")
    if not stat.S_ISDIR(root_metadata.st_mode):
        _fail("git_scope.metadata_special", f"{label} is not a directory")
    pending = [root]
    observed = 0
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            _fail("git_scope.metadata_unreadable", f"{label} contains an unreadable directory ({exc})")
        for entry in entries:
            observed += 1
            if observed > METADATA_ENTRY_LIMIT:
                _fail("git_scope.metadata_oversized", f"{label} exceeds the metadata entry budget")
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                _fail("git_scope.metadata_unreadable", f"{label} contains an unreadable entry ({exc})")
            if stat.S_ISLNK(metadata.st_mode):
                _fail("git_scope.metadata_symlink", f"{label} contains a symbolic link")
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(Path(entry.path))
            elif not stat.S_ISREG(metadata.st_mode):
                _fail("git_scope.metadata_special", f"{label} contains a non-regular metadata entry")
            elif metadata.st_size > METADATA_FILE_LIMIT_BYTES:
                # Object and pack payloads are intentionally allowed to exceed
                # the text-file budget; only files that this adapter parses are
                # bounded when opened.
                continue


def _config_value(value: str | None) -> str:
    if value is None:
        return "true"
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == '"':
        stripped = stripped[1:-1]
    return stripped.strip().lower()


def _inspect_config(config_text: str) -> None:
    if "\x00" in config_text:
        _fail("git_scope.config_unsafe", "local Git config contains NUL data")
    section = ""
    for raw_line in config_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if raw_line.rstrip().endswith("\\"):
            _fail("git_scope.config_unsafe", "local Git config uses an ambiguous continuation")
        section_match = _SECTION.fullmatch(stripped)
        if section_match:
            section = section_match.group(1).lower()
            if section in {"include", "includeif"}:
                _fail("git_scope.config_unsafe", "local Git config uses include indirection")
            if section == "filter":
                _fail("git_scope.config_unsafe", "local Git config defines a content filter")
            continue
        variable_match = _VARIABLE.fullmatch(stripped)
        if not variable_match or not section:
            _fail("git_scope.config_unsafe", "local Git config contains unsupported syntax")
        key = variable_match.group(1).lower()
        value = _config_value(variable_match.group(2))
        if section == "core" and key in {
            "worktree",
            "hookspath",
            "attributesfile",
            "excludesfile",
            "alternaterefscommand",
        }:
            _fail("git_scope.config_unsafe", f"local Git config sets core.{key}")
        if section == "core" and key == "bare" and value not in {"false", "no", "off", "0"}:
            _fail("git_scope.config_unsafe", "local Git config enables or ambiguously defines core.bare")
        if section == "core" and key == "fsmonitor" and value not in {"false", "no", "off", "0", ""}:
            _fail("git_scope.config_unsafe", "local Git config enables core.fsmonitor")
        if section == "extensions" and key == "worktreeconfig" and value not in {
            "false",
            "no",
            "off",
            "0",
            "",
        }:
            _fail("git_scope.config_unsafe", "local Git config enables extensions.worktreeConfig")


def _inspect_replacement_and_indirection(common_dir: Path) -> None:
    forbidden = {
        common_dir / "objects" / "info" / "alternates": "Git object alternates",
        common_dir / "objects" / "info" / "http-alternates": "Git HTTP object alternates",
        common_dir / "info" / "grafts": "Git graft metadata",
        common_dir / "refs" / "replace": "Git replacement refs",
    }
    for path, description in forbidden.items():
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            _fail("git_scope.metadata_unreadable", f"{description} cannot be inspected")
        _fail("git_scope.metadata_indirection", f"{description} is not allowed")

    packed_refs = common_dir / "packed-refs"
    try:
        packed_refs.lstat()
    except FileNotFoundError:
        return
    except OSError:
        _fail("git_scope.metadata_unreadable", "packed refs cannot be inspected")
    packed_text = _read_text(packed_refs, label="packed-refs")
    for line in packed_text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", "^")) and " refs/replace/" in f" {stripped}":
            _fail("git_scope.metadata_indirection", "packed replacement refs are not allowed")


def _inspect_config_files(git_dir: Path, common_dir: Path) -> None:
    config = common_dir / "config"
    _inspect_config(_read_text(config, label="common Git config"))
    candidates = {git_dir / "config.worktree", common_dir / "config.worktree"}
    for candidate in candidates:
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            _fail("git_scope.metadata_unreadable", "per-worktree Git config cannot be inspected")
        _fail("git_scope.config_unsafe", "per-worktree Git config is not allowed")


def _attribute_filter_line(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and re.search(
            r"(?:^|\s)(?:-?filter)(?:=|\s|$)", stripped
        ):
            return True
    return False


def _inspect_attribute_file(path: Path, label: str) -> None:
    metadata = _lstat_kind(path, label)
    if stat.S_ISLNK(metadata.st_mode):
        _fail("git_scope.attribute_unsafe", f"{label} is a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        _fail("git_scope.attribute_unsafe", f"{label} is not a regular file")
    text = _read_text(path, label=label, limit=ATTRIBUTE_FILE_LIMIT_BYTES)
    if _attribute_filter_line(text):
        _fail("git_scope.attribute_unsafe", f"{label} selects a Git content filter")


def _inspect_attributes(root: Path, common_dir: Path) -> None:
    observed = 0

    def onerror(error: OSError) -> None:
        _fail("git_scope.attribute_unsafe", f"attribute preflight cannot scan the checkout ({error})")

    for current_text, dirnames, filenames in os.walk(
        root, topdown=True, followlinks=False, onerror=onerror
    ):
        current = Path(current_text)
        if current == root:
            dirnames[:] = sorted(name for name in dirnames if name != ".git")
        else:
            dirnames[:] = sorted(dirnames)
        filenames = sorted(filenames)
        observed += len(dirnames) + len(filenames)
        if observed > ATTRIBUTE_ENTRY_LIMIT:
            _fail("git_scope.attribute_unsafe", "attribute preflight exceeded its entry budget")
        if ".gitattributes" in filenames:
            _inspect_attribute_file(current / ".gitattributes", "checkout .gitattributes")
    info_attributes = common_dir / "info" / "attributes"
    try:
        info_attributes.lstat()
    except FileNotFoundError:
        return
    except OSError:
        _fail("git_scope.attribute_unsafe", "Git info attributes cannot be inspected")
    _inspect_attribute_file(info_attributes, "Git info attributes")


def _valid_ref(value: str) -> bool:
    if not value.startswith("refs/") or len(value.encode("utf-8")) > 1024:
        return False
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return False
    if any(token in value for token in ("..", "@{", "\\", "//")) or any(
        character in value for character in " ~^:?*["
    ):
        return False
    if value.endswith(("/", ".")):
        return False
    return all(
        component
        and not component.startswith(".")
        and not component.endswith(".lock")
        for component in value.split("/")
    )


def _inspect_head(git_dir: Path) -> tuple[str, str | None, str | None]:
    text = _read_text(git_dir / "HEAD", label="checkout HEAD", limit=POINTER_FILE_LIMIT_BYTES)
    line = _single_line(text, "checkout HEAD")
    if line.startswith("ref: "):
        ref = line[5:]
        if not _valid_ref(ref):
            _fail("git_scope.head_invalid", "checkout HEAD contains an invalid symbolic ref")
        return "symbolic", ref, None
    if not _OBJECT_ID.fullmatch(line):
        _fail("git_scope.head_invalid", "checkout HEAD is neither a symbolic ref nor a full object ID")
    return "detached", None, line


def _resolve_git_executable(root: Path) -> tuple[str, str]:
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
        _fail("git_scope.git_unavailable", "no trusted Git executable is available")
    try:
        resolved = Path(executable).resolve(strict=True)
    except OSError:
        _fail("git_scope.git_unavailable", "the selected Git executable cannot be resolved")
    try:
        resolved.relative_to(root)
    except ValueError:
        pass
    else:
        _fail("git_scope.git_untrusted", "the selected Git executable resolves inside the project")
    return str(resolved), safe_path


def _run_git(
    root: Path,
    git_dir: Path,
    arguments: Sequence[str],
) -> subprocess.CompletedProcess[bytes]:
    executable, safe_path = _resolve_git_executable(root)
    environment: dict[str, str] = {"PATH": safe_path}
    if os.environ.get("SYSTEMROOT"):
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
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
        executable,
        f"--git-dir={git_dir}",
        f"--work-tree={root}",
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
        "core.untrackedCache=false",
        "-c",
        "submodule.recurse=false",
        "-c",
        "diff.external=",
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
        _fail("git_scope.git_failed", f"Git could not be started ({exc})")
    assert process.stdout is not None and process.stderr is not None
    if os.name == "nt":
        # Windows select() only supports sockets, not pipe descriptors.
        # communicate() reads both pipes on reader threads and provides the
        # same bounded wait; the output budget is enforced after collection.
        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=GIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            process.stdout.close()
            process.stderr.close()
            _fail("git_scope.git_timeout", "Git command exceeded its time budget")
        process.stdout.close()
        process.stderr.close()
        if len(stdout_bytes) + len(stderr_bytes) > GIT_OUTPUT_LIMIT_BYTES:
            _fail("git_scope.git_output", "Git output exceeded its bounded size")
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout_bytes,
            stderr_bytes,
        )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                _fail("git_scope.git_timeout", "Git command exceeded its time budget")
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
                    _fail("git_scope.git_output", "Git output exceeded its bounded size")
                buffers[key.data].extend(chunk)
        try:
            return_code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            _fail("git_scope.git_timeout", "Git command exceeded its time budget")
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    return subprocess.CompletedProcess(
        command,
        return_code,
        bytes(buffers["stdout"]),
        bytes(buffers["stderr"]),
    )


def _git_text(
    root: Path,
    git_dir: Path,
    arguments: Sequence[str],
    *,
    allowed_returncodes: set[int] | None = None,
) -> tuple[int, str]:
    result = _run_git(root, git_dir, arguments)
    allowed = {0} if allowed_returncodes is None else allowed_returncodes
    if result.returncode not in allowed:
        _fail("git_scope.git_failed", "a bounded Git identity query failed")
    try:
        value = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        _fail("git_scope.git_output", "a Git identity query returned non-UTF-8 output")
    if "\x00" in value:
        _fail("git_scope.git_output", "a Git identity query returned malformed output")
    return result.returncode, value.strip()


def _untracked_paths(status: bytes) -> list[bytes]:
    """Extract untracked paths from NUL-delimited porcelain without decoding."""

    records = status.split(b"\0")
    result: list[bytes] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            _fail("git_scope.git_output", "Git status returned a malformed porcelain record")
        status_code = record[:2]
        path = record[3:]
        if not path:
            _fail("git_scope.git_output", "Git status returned an empty path")
        if status_code == b"??":
            result.append(path)
        if b"R" in status_code or b"C" in status_code:
            if index >= len(records) or not records[index]:
                _fail("git_scope.git_output", "Git status returned an incomplete rename record")
            index += 1
    return result


def _hash_untracked(root: Path, status: bytes) -> bytes:
    """Hash bounded untracked content without following links or opening specials."""

    digest = hashlib.sha256()
    consumed = 0
    for raw_path in _untracked_paths(status):
        decoded = os.fsdecode(raw_path)
        relative = Path(decoded)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            _fail("git_scope.dirty_path_unsafe", "Git status returned a non-canonical untracked path")
        target = root / relative
        try:
            target.relative_to(root)
        except ValueError:
            _fail("git_scope.dirty_path_unsafe", "an untracked path escapes the checkout")
        current = root
        for part in relative.parts[:-1]:
            current = current / part
            metadata = _lstat_kind(current, "untracked path parent")
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                _fail("git_scope.dirty_path_unsafe", "an untracked path crosses a non-directory or link")
        metadata = _lstat_kind(target, "untracked path")
        digest.update(len(raw_path).to_bytes(8, "big"))
        digest.update(raw_path)
        if stat.S_ISREG(metadata.st_mode):
            if metadata.st_size > DIRTY_CONTENT_LIMIT_BYTES - consumed:
                _fail("git_scope.dirty_content_oversized", "untracked content exceeds its digest budget")
            consumed += metadata.st_size
            digest.update(b"file\0")
            flags = os.O_RDONLY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(target, flags)
            except OSError as exc:
                _fail("git_scope.dirty_path_unsafe", f"an untracked file cannot be opened ({exc})")
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or opened.st_size != metadata.st_size:
                    _fail("git_scope.checkout_changed", "an untracked file changed during identity capture")
                remaining = metadata.st_size
                while remaining:
                    block = os.read(descriptor, min(1024 * 1024, remaining))
                    if not block:
                        _fail("git_scope.checkout_changed", "an untracked file changed during identity capture")
                    digest.update(block)
                    remaining -= len(block)
                if os.read(descriptor, 1):
                    _fail("git_scope.checkout_changed", "an untracked file changed during identity capture")
            finally:
                os.close(descriptor)
        elif stat.S_ISLNK(metadata.st_mode):
            try:
                target_value = os.fsencode(os.readlink(target))
            except OSError as exc:
                _fail("git_scope.dirty_path_unsafe", f"an untracked link cannot be read ({exc})")
            digest.update(b"link\0")
            digest.update(target_value)
        else:
            _fail("git_scope.dirty_path_unsafe", "an untracked path is a special node")
    return digest.digest()


def _capture_dirty_digest(
    root: Path,
    git_dir: Path,
    status: bytes,
    excluded_paths: Sequence[str],
) -> str:
    if not status:
        return hashlib.sha256(b"").hexdigest()
    diff_pathspecs = _excluded_pathspecs(excluded_paths)
    diff_arguments = [
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--binary",
        "--full-index",
        "--no-renames",
        "HEAD",
        "--",
    ]
    if diff_pathspecs:
        diff_arguments.extend([".", *diff_pathspecs])
    diff = _run_git(
        root,
        git_dir,
        diff_arguments,
    )
    if diff.returncode != 0:
        _fail("git_scope.git_failed", "Git diff failed; dirty content identity is unknown")
    digest = hashlib.sha256()
    for label, value in (
        (b"status", status),
        (b"tracked-diff", diff.stdout),
        (b"untracked", _hash_untracked(root, status)),
    ):
        digest.update(label)
        digest.update(b"\0")
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


def _cross_check_git(
    root: Path,
    git_dir: Path,
    common_dir: Path,
    head_kind: str,
    metadata_ref: str | None,
    metadata_oid: str | None,
    excluded_paths: Sequence[str],
) -> tuple[str, str | None, bool, str]:
    _, top_level_text = _git_text(root, git_dir, ["rev-parse", "--show-toplevel"])
    top_level = Path(os.path.abspath(os.path.normpath(top_level_text)))
    if top_level != root:
        _fail("git_scope.top_level_mismatch", "Git top-level differs from the authorized checkout root")

    _, git_dir_text = _git_text(root, git_dir, ["rev-parse", "--absolute-git-dir"])
    observed_git_dir = Path(os.path.abspath(os.path.normpath(git_dir_text)))
    if observed_git_dir != git_dir:
        _fail("git_scope.git_dir_mismatch", "Git resolved a different git-dir")

    _, common_text = _git_text(root, git_dir, ["rev-parse", "--git-common-dir"])
    common_candidate = Path(common_text)
    if not common_candidate.is_absolute():
        common_candidate = root / common_candidate
    observed_common = Path(os.path.abspath(os.path.normpath(os.fspath(common_candidate))))
    if observed_common != common_dir:
        _fail("git_scope.common_dir_mismatch", "Git resolved a different common-dir")

    _, head = _git_text(root, git_dir, ["rev-parse", "--verify", "HEAD"])
    if not _OBJECT_ID.fullmatch(head):
        _fail("git_scope.head_invalid", "Git did not return a full canonical HEAD object ID")
    if head_kind == "detached" and head != metadata_oid:
        _fail("git_scope.head_mismatch", "Git HEAD differs from detached checkout metadata")

    ref_returncode, ref_text = _git_text(
        root,
        git_dir,
        ["symbolic-ref", "-q", "HEAD"],
        allowed_returncodes={0, 1},
    )
    observed_ref: str | None
    if ref_returncode == 0:
        if not _valid_ref(ref_text):
            _fail("git_scope.head_invalid", "Git returned an invalid symbolic ref")
        observed_ref = ref_text
    else:
        if ref_text:
            _fail("git_scope.git_output", "detached HEAD query returned unexpected output")
        observed_ref = None
    if head_kind == "symbolic" and observed_ref != metadata_ref:
        _fail("git_scope.head_mismatch", "Git symbolic ref differs from checkout HEAD metadata")
    if head_kind == "detached" and observed_ref is not None:
        _fail("git_scope.head_mismatch", "Git reports a symbolic ref for detached HEAD metadata")

    status_pathspecs = _excluded_pathspecs(excluded_paths)
    status_arguments = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
    if status_pathspecs:
        status_arguments.extend(["--", ".", *status_pathspecs])
    status = _run_git(root, git_dir, status_arguments)
    if status.returncode != 0:
        _fail("git_scope.git_failed", "Git status failed; checkout dirtiness is unknown")
    dirty_digest = _capture_dirty_digest(root, git_dir, status.stdout, excluded_paths)
    # Repeat the status and digest capture.  A stable porcelain code alone is
    # insufficient because modified content can change while remaining "M".
    repeated_status = _run_git(root, git_dir, status_arguments)
    if repeated_status.returncode != 0 or repeated_status.stdout != status.stdout:
        _fail("git_scope.checkout_changed", "checkout status changed during identity capture")
    if _capture_dirty_digest(root, git_dir, repeated_status.stdout, excluded_paths) != dirty_digest:
        _fail("git_scope.checkout_changed", "dirty checkout content changed during identity capture")
    return head, observed_ref, bool(status.stdout), dirty_digest


def inspect_git_scope(
    root: Path,
    *,
    expected_git_dir: Path | str | None = None,
    expected_common_dir: Path | str | None = None,
    authority_ref: str | None = None,
    excluded_paths: Sequence[str] = (),
) -> dict[str, Any]:
    """Return a shareable, path-free identity for one validated checkout.

    Main checkouts require no external authority.  Linked checkouts fail before
    touching their external metadata unless all three authority inputs are
    present and the root ``.git`` pointer exactly matches ``expected_git_dir``.
    """

    root = _canonical_root(Path(root))
    validated_exclusions = _validated_excluded_paths(root, excluded_paths)
    marker = root / ".git"
    marker_metadata = _lstat_kind(marker, ".git marker")
    marker_text: str | None = None
    commondir_text: str | None = None
    backlink_text: str | None = None
    if stat.S_ISLNK(marker_metadata.st_mode):
        _fail("git_scope.marker_symlink", ".git marker must not be a symbolic link")

    if stat.S_ISDIR(marker_metadata.st_mode):
        checkout_kind = "main"
        git_dir = marker
        common_dir = marker
        if any(value is not None for value in (expected_git_dir, expected_common_dir, authority_ref)):
            if expected_git_dir is None or expected_common_dir is None or authority_ref is None:
                _fail("git_scope.authority_invalid", "partial authority is not accepted")
            supplied_git = _normalize_expected(expected_git_dir, "expected_git_dir")
            supplied_common = _normalize_expected(expected_common_dir, "expected_common_dir")
            _validate_authority_ref(authority_ref)
            if supplied_git != git_dir or supplied_common != common_dir:
                _fail("git_scope.authority_mismatch", "authority paths do not match the main checkout")
        validated_authority: str | None = None
        git_dir_metadata = marker_metadata
        common_metadata = marker_metadata
        _assert_no_symlink_components(git_dir, "main git-dir")
        _scan_metadata_tree(common_dir, label="main Git metadata")
    elif stat.S_ISREG(marker_metadata.st_mode):
        checkout_kind = "linked"
        # Read only the in-checkout marker first.  No external path is touched
        # before all authority values are validated and the pointer matches.
        marker_text = _read_text(marker, label=".git pointer", limit=POINTER_FILE_LIMIT_BYTES)
        pointed_git_dir = _pointer_target(marker_text, base=root, label=".git pointer", prefix="gitdir")
        supplied_git = _normalize_expected(expected_git_dir, "expected_git_dir")
        supplied_common = _normalize_expected(expected_common_dir, "expected_common_dir")
        validated_authority = _validate_authority_ref(authority_ref)
        if pointed_git_dir != supplied_git:
            _fail("git_scope.authority_mismatch", ".git pointer does not match expected_git_dir")
        git_dir = supplied_git
        common_dir = supplied_common
        _assert_no_symlink_components(git_dir, "authorized linked git-dir")
        git_dir_metadata = _lstat_kind(git_dir, "authorized linked git-dir")
        if not stat.S_ISDIR(git_dir_metadata.st_mode):
            _fail("git_scope.metadata_special", "authorized linked git-dir is not a directory")

        commondir_text = _read_text(
            git_dir / "commondir", label="linked commondir", limit=POINTER_FILE_LIMIT_BYTES
        )
        pointed_common = _pointer_target(
            commondir_text, base=git_dir, label="linked commondir"
        )
        if pointed_common != common_dir:
            _fail("git_scope.common_dir_mismatch", "linked commondir does not match expected_common_dir")
        if git_dir.parent != common_dir / "worktrees" or git_dir.parent.parent != common_dir:
            _fail("git_scope.metadata_escape", "linked git-dir is outside common-dir/worktrees/<id>")

        backlink_text = _read_text(
            git_dir / "gitdir", label="linked reverse gitdir", limit=POINTER_FILE_LIMIT_BYTES
        )
        backlink = _pointer_target(backlink_text, base=git_dir, label="linked reverse gitdir")
        if backlink != marker:
            _fail("git_scope.backlink_mismatch", "linked reverse gitdir does not point to the checkout marker")

        _assert_no_symlink_components(common_dir, "authorized common-dir")
        common_metadata = _lstat_kind(common_dir, "authorized common-dir")
        if not stat.S_ISDIR(common_metadata.st_mode):
            _fail("git_scope.metadata_special", "authorized common-dir is not a directory")
        # Scanning the common directory also covers the selected worktree
        # registry entry because it is required to be a direct child.
        _scan_metadata_tree(common_dir, label="authorized common Git metadata")
    else:
        _fail("git_scope.marker_special", ".git marker is neither a directory nor a regular pointer file")

    _inspect_replacement_and_indirection(common_dir)
    _inspect_config_files(git_dir, common_dir)
    _inspect_attributes(root, common_dir)
    head_kind, metadata_ref, metadata_oid = _inspect_head(git_dir)
    head, ref, dirty, dirty_digest = _cross_check_git(
        root,
        git_dir,
        common_dir,
        head_kind,
        metadata_ref,
        metadata_oid,
        validated_exclusions,
    )

    # Revalidate the complete ambient checkout after the Git subprocesses.
    # This catches ordinary checkout/config replacement and metadata drift that
    # occurs during capture; an intentionally coordinated same-account process
    # is outside the local Harness security boundary (ADR-0008).
    final_marker = _lstat_kind(marker, ".git marker")
    initial_marker_identity = (
        stat.S_IFMT(marker_metadata.st_mode),
        marker_metadata.st_dev,
        marker_metadata.st_ino,
    )
    final_marker_identity = (
        stat.S_IFMT(final_marker.st_mode),
        final_marker.st_dev,
        final_marker.st_ino,
    )
    if final_marker_identity != initial_marker_identity or stat.S_ISLNK(final_marker.st_mode):
        _fail("git_scope.checkout_changed", ".git marker changed during identity capture")
    for label, path, initial in (
        ("git-dir", git_dir, git_dir_metadata),
        ("common-dir", common_dir, common_metadata),
    ):
        final = _lstat_kind(path, label)
        if (
            stat.S_ISLNK(final.st_mode)
            or not stat.S_ISDIR(final.st_mode)
            or (final.st_dev, final.st_ino) != (initial.st_dev, initial.st_ino)
        ):
            _fail("git_scope.checkout_changed", f"{label} changed during identity capture")
    if checkout_kind == "linked":
        if _read_text(marker, label=".git pointer", limit=POINTER_FILE_LIMIT_BYTES) != marker_text:
            _fail("git_scope.checkout_changed", ".git pointer changed during identity capture")
        if _read_text(git_dir / "commondir", label="linked commondir", limit=POINTER_FILE_LIMIT_BYTES) != commondir_text:
            _fail("git_scope.checkout_changed", "linked commondir changed during identity capture")
        if _read_text(git_dir / "gitdir", label="linked reverse gitdir", limit=POINTER_FILE_LIMIT_BYTES) != backlink_text:
            _fail("git_scope.checkout_changed", "linked reverse gitdir changed during identity capture")
    _scan_metadata_tree(common_dir, label="final Git metadata")
    _inspect_replacement_and_indirection(common_dir)
    _inspect_config_files(git_dir, common_dir)
    _inspect_attributes(root, common_dir)
    final_head_metadata = _inspect_head(git_dir)
    if final_head_metadata != (head_kind, metadata_ref, metadata_oid):
        _fail("git_scope.checkout_changed", "checkout HEAD metadata changed during identity capture")
    final_identity = _cross_check_git(
        root,
        git_dir,
        common_dir,
        head_kind,
        metadata_ref,
        metadata_oid,
        validated_exclusions,
    )
    if final_identity != (head, ref, dirty, dirty_digest):
        _fail("git_scope.checkout_changed", "Git identity changed during capture")

    repository_scope_id = _opaque_id("repo", {"common_dir": os.fspath(common_dir)})
    checkout_scope_id = _opaque_id(
        "checkout",
        {
            "repository_scope_id": repository_scope_id,
            "root": os.fspath(root),
            "git_dir": os.fspath(git_dir),
        },
    )
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "repository_scope_id": repository_scope_id,
        "checkout_scope_id": checkout_scope_id,
        "checkout_kind": checkout_kind,
        "head": head,
        "ref": ref,
        "detached": ref is None,
        "dirty": dirty,
        "dirty_digest": dirty_digest,
        "excluded_paths_digest": _canonical_json_digest(list(validated_exclusions)),
        "integration_owner": checkout_kind == "main",
    }
    if validated_authority is not None:
        result["authority_ref"] = validated_authority
    return result


__all__ = ["GitScopeError", "SCHEMA_VERSION", "inspect_git_scope"]
