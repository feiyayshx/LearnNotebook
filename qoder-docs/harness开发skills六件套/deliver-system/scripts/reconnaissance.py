#!/usr/bin/env python3
"""Bounded, read-only reconnaissance for the deliver-system Skill.

The module never writes project files and never executes discovered commands.
Only an injected Git runner may execute read-only Git queries for drift checks.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


INSPECTION_SCHEMA_VERSION = 1
IMPLEMENTED_MILESTONE = 2
DRIFT_STATUSES = {"unchanged", "changed", "missing", "unknown", "not_applicable"}

DEFAULT_LIMITS = {
    "max_files": 10_000,
    "max_directories": 2_000,
    "max_depth": 12,
    "max_marker_bytes": 1_048_576,
    "max_results": 200,
}
HARD_LIMITS = {
    "max_files": 100_000,
    "max_directories": 20_000,
    "max_depth": 32,
    "max_marker_bytes": 4_194_304,
    "max_results": 1_000,
}
SKIP_DIRECTORY_NAMES = {
    ".git",
    ".delivery",
    ".venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "coverage",
    "htmlcov",
    "playwright-report",
    "test-results",
    ".next",
    ".nuxt",
    ".turbo",
    "target",
}
# Path-based skips relative to the project root (posix form).  The machine
# state subtree lives under delivery-docs/, whose other subtrees still carry
# product/planning signals and therefore must remain scannable.
SKIP_RELATIVE_PATHS = {
    "delivery-docs/state",
}
CODE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
    ".svelte",
}
STATIC_WEB_SUFFIXES = {".css", ".htm", ".html", ".less", ".scss"}
NON_PRODUCT_ROOTS = {
    ".qoder",
    "architecture",
    "delivery-docs",
    "docs",
    "examples",
    "fixtures",
    "openspec",
    "prototype",
    "prototypes",
    "scripts",
    "spec",
    "test",
    "tests",
}
NON_PRODUCT_COMPONENTS = {"__tests__", "e2e", "fixtures", "spec", "test", "tests", "python-tests"}
COMMON_NODE_SCRIPTS = {
    "build",
    "check",
    "dev",
    "lint",
    "start",
    "test",
    "test:e2e",
    "typecheck",
}

GitRunner = Callable[[Path, Sequence[str]], Any]


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _limits(overrides: Mapping[str, Any] | None) -> dict[str, int]:
    result = dict(DEFAULT_LIMITS)
    for key, hard_max in HARD_LIMITS.items():
        if not overrides or key not in overrides:
            continue
        value = overrides[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{key} must be a positive integer")
        result[key] = min(value, hard_max)
    return result


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _safe_regular_file(root: Path, path: Path) -> tuple[bool, str | None]:
    """Check a file lexically and reject every symlink component."""

    root = root.resolve()
    lexical = Path(os.path.abspath(os.fspath(path)))
    try:
        relative = lexical.relative_to(root)
    except ValueError:
        return False, "path escapes project root"
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return False, "path is missing"
        except OSError as exc:
            return False, f"path cannot be inspected: {exc}"
        if stat.S_ISLNK(mode):
            return False, "path crosses a symbolic link"
    try:
        mode = lexical.lstat().st_mode
    except OSError as exc:
        return False, f"path cannot be inspected: {exc}"
    if not stat.S_ISREG(mode):
        return False, "path is not a regular file"
    return True, None


def _read_marker_text(
    root: Path,
    path: Path,
    *,
    max_bytes: int,
    warnings: list[str],
) -> str | None:
    safe, reason = _safe_regular_file(root, path)
    label = _relative(root, path) if path.is_absolute() and root in path.parents else str(path)
    if not safe:
        warnings.append(f"{label}: {reason}")
        return None
    try:
        size = path.stat().st_size
        if size > max_bytes:
            warnings.append(f"{label}: marker exceeds {max_bytes} bytes and was not read")
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        warnings.append(f"{label}: marker cannot be read as UTF-8: {exc}")
        return None


def _read_marker_json(
    root: Path,
    path: Path,
    *,
    max_bytes: int,
    warnings: list[str],
) -> dict[str, Any] | None:
    text = _read_marker_text(root, path, max_bytes=max_bytes, warnings=warnings)
    if text is None:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        warnings.append(f"{_relative(root, path)}: invalid JSON at line {exc.lineno}")
        return None
    if not isinstance(value, dict):
        warnings.append(f"{_relative(root, path)}: expected a JSON object")
        return None
    return value


def scan_project(root: Path, limits: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a deterministic bounded inventory without following symlinks."""

    root = root.resolve()
    budget = _limits(limits)
    files: list[str] = []
    directories: list[str] = []
    symlinks: list[str] = []
    specials: list[str] = []
    skipped: set[str] = set()
    warnings: list[str] = []
    truncated_reasons: set[str] = set()
    stop = False
    observed_entries = 0
    # Files, directories, links, skipped directories, and special entries all
    # consume the same traversal budget. This prevents a link-heavy tree from
    # bypassing the separate regular-file and directory caps.
    max_entries = budget["max_files"] + budget["max_directories"] + (2 * budget["max_results"])
    budget["max_entries"] = max_entries

    def consume_entry() -> bool:
        nonlocal observed_entries, stop
        if observed_entries >= max_entries:
            truncated_reasons.add("max_entries")
            stop = True
            return False
        observed_entries += 1
        return True

    def onerror(error: OSError) -> None:
        filename = Path(error.filename) if error.filename else root
        try:
            label = _relative(root, filename)
        except ValueError:
            label = "<outside-root>"
        warnings.append(f"cannot scan {label}: {error}")

    for current_text, dirnames, filenames in os.walk(
        root,
        topdown=True,
        followlinks=False,
        onerror=onerror,
    ):
        current = Path(current_text)
        depth = len(current.relative_to(root).parts)
        dirnames[:] = sorted(dirnames)
        filenames = sorted(filenames)
        kept_dirs: list[str] = []
        for name in dirnames:
            if not consume_entry():
                break
            path = current / name
            relative = _relative(root, path)
            if name in SKIP_DIRECTORY_NAMES or relative in SKIP_RELATIVE_PATHS:
                skipped.add(relative)
                continue
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                warnings.append(f"cannot inspect {relative}: {exc}")
                continue
            if stat.S_ISLNK(mode):
                symlinks.append(relative)
                continue
            if depth >= budget["max_depth"]:
                truncated_reasons.add("max_depth")
                continue
            if len(directories) >= budget["max_directories"]:
                truncated_reasons.add("max_directories")
                stop = True
                break
            directories.append(relative)
            kept_dirs.append(name)
        dirnames[:] = [] if stop else kept_dirs
        if stop:
            break
        for name in filenames:
            if not consume_entry():
                break
            path = current / name
            relative = _relative(root, path)
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                warnings.append(f"cannot inspect {relative}: {exc}")
                continue
            if stat.S_ISLNK(mode):
                symlinks.append(relative)
                continue
            if not stat.S_ISREG(mode):
                specials.append(relative)
                continue
            if len(files) >= budget["max_files"]:
                truncated_reasons.add("max_files")
                stop = True
                break
            files.append(relative)
        if stop:
            break

    files.sort()
    directories.sort()
    symlinks.sort()
    specials.sort()
    cap = budget["max_results"]
    # The scanner intentionally never follows links. Until an explicit path
    # authority adapter exists, every uninspected link can hide product,
    # configuration, test, or evidence inputs and therefore blocks routing.
    critical_symlinks = symlinks[:cap]
    return {
        "status": "partial" if truncated_reasons or warnings else "complete",
        "truncated": bool(truncated_reasons),
        "truncated_reasons": sorted(truncated_reasons),
        "limits": budget,
        "counts": {
            "files": len(files),
            "directories": len(directories),
            "symlinks": len(symlinks),
            "special_entries": len(specials),
            "observed_entries": observed_entries,
        },
        "symlinks": symlinks[:cap],
        # Safety-critical paths are deliberately not hidden by the display cap.
        # The scan budget still bounds how many entries can be observed.
        "critical_symlinks": critical_symlinks,
        "critical_symlink_count": len(symlinks),
        "special_entries": specials[:cap],
        "skipped_directories": sorted(skipped)[:cap],
        "warnings": warnings[:cap],
        "_files": files,
        "_directories": directories,
    }


def _node_command(manager: str | None, script: str) -> str | None:
    if manager is None:
        return None
    if manager == "pnpm":
        return f"pnpm {script}"
    if manager == "yarn":
        return f"yarn {script}"
    if script == "test":
        return "npm test"
    return f"npm run {script}"


def _detect_node(
    root: Path,
    files: set[str],
    limits: Mapping[str, int],
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    commands: list[dict[str, Any]] = []
    frameworks: set[str] = set()
    managers: set[str] = set()
    for marker, manager in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("package-lock.json", "npm"),
        ("bun.lockb", "bun"),
    ):
        if marker in files:
            managers.add(manager)
    package_files = sorted(path for path in files if Path(path).name == "package.json")[:50]
    for relative in package_files:
        package = _read_marker_json(
            root,
            root / relative,
            max_bytes=limits["max_marker_bytes"],
            warnings=warnings,
        )
        if package is None:
            continue
        scripts = package.get("scripts")
        if isinstance(scripts, dict):
            # A lock-file conflict is an authority question, not permission to
            # choose a package manager by alphabetical accident.
            manager = next(iter(managers)) if len(managers) == 1 else None
            for name in sorted(scripts):
                if name in COMMON_NODE_SCRIPTS:
                    commands.append(
                        {
                            "id": name.replace(":", "-"),
                            "command": _node_command(manager, name),
                            "source": f"{relative}#scripts.{name}",
                            "declared_only": True,
                            "discovery": "declared",
                            "execution_status": "not_run",
                            "requires_authority_choice": manager is None,
                        }
                    )
        dependency_names: set[str] = set()
        for field in ("dependencies", "devDependencies", "peerDependencies"):
            value = package.get(field)
            if isinstance(value, dict):
                dependency_names.update(str(name) for name in value)
        known = {
            "@nestjs/core": "nestjs",
            "@playwright/test": "playwright",
            "cypress": "cypress",
            "express": "express",
            "jest": "jest",
            "next": "nextjs",
            "react": "react",
            "svelte": "svelte",
            "vite": "vite",
            "vitest": "vitest",
            "vue": "vue",
        }
        for dependency, framework in known.items():
            if dependency in dependency_names:
                frameworks.add(framework)
    return commands, sorted(frameworks), sorted(managers)


def detect_capabilities(root: Path, scan: Mapping[str, Any]) -> dict[str, Any]:
    files = set(scan["_files"])
    directories = set(scan["_directories"])
    limits = scan["limits"]
    warnings = scan["warnings"]
    stacks: list[dict[str, Any]] = []

    stack_markers = {
        "node": {"package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json"},
        "python": {"pyproject.toml", "requirements.txt", "Pipfile", "setup.py"},
        "go": {"go.mod"},
        "rust": {"Cargo.toml"},
        "java": {"pom.xml", "build.gradle", "build.gradle.kts"},
        "dotnet": {".sln", ".csproj"},
    }
    for stack, markers in stack_markers.items():
        evidence = sorted(
            path
            for path in files
            if Path(path).name in markers
            or any(path.endswith(suffix) for suffix in markers if suffix.startswith("."))
        )
        if evidence:
            stacks.append({"id": stack, "evidence": evidence[: limits["max_results"]]})

    node_commands, frameworks, package_managers = _detect_node(root, files, limits, warnings)
    commands = list(node_commands)
    if any(Path(path).name in {"pyproject.toml", "pytest.ini", "tox.ini"} for path in files) or any(
        path.startswith(("tests/", "python-tests/")) and path.endswith(".py") for path in files
    ):
        commands.append(
            {
                "id": "python-test",
                "command": "python -m pytest",
                "source": "python test/config markers",
                "declared_only": False,
                "discovery": "inferred_candidate",
                "execution_status": "not_run",
                "requires_authority_choice": True,
            }
        )
    commands = sorted(commands, key=lambda item: (item["id"], item["source"]))[: limits["max_results"]]

    ci_patterns = {
        "github-actions": ".github/workflows/",
        "gitlab-ci": ".gitlab-ci.yml",
        "jenkins": "Jenkinsfile",
        "circleci": ".circleci/",
        "buildkite": ".buildkite/",
        "azure-pipelines": "azure-pipelines.yml",
    }
    ci: list[dict[str, Any]] = []
    for provider, pattern in ci_patterns.items():
        evidence = sorted(
            path for path in files if path == pattern or path.startswith(pattern)
        )
        if evidence:
            ci.append({"provider": provider, "evidence": evidence[: limits["max_results"]]})

    doc_categories = {
        "agents": ("AGENTS.md",),
        "architecture": ("delivery-docs/architecture/", "docs/architecture/", "architecture/"),
        "decisions": (
            "delivery-docs/decisions/",
            "docs/adr/",
            "architecture/decisions/",
            "docs/decisions/",
            "rfcs/",
        ),
        "plans": ("delivery-docs/plans/", "docs/plans/"),
        "product": ("delivery-docs/product/", "docs/product/"),
        "runbooks": ("docs/runbooks/", "runbooks/"),
    }
    documentation: dict[str, list[str]] = {}
    for category, prefixes in doc_categories.items():
        matched = sorted(
            path
            for path in files
            if any(path == prefix or path.startswith(prefix) for prefix in prefixes)
        )
        if matched:
            documentation[category] = matched[: limits["max_results"]]

    openspec = _detect_openspec(root, files, directories, limits, warnings)
    qoder = _detect_qoder(root, files, directories, limits, warnings)

    source_directories = sorted(
        directory
        for directory in directories
        if Path(directory).name in {"app", "api", "backend", "client", "frontend", "lib", "server", "src"}
    )[: limits["max_results"]]
    test_directories = sorted(
        directory
        for directory in directories
        if Path(directory).name in {"__tests__", "e2e", "spec", "test", "tests", "python-tests"}
    )[: limits["max_results"]]
    test_files = sorted(
        path
        for path in files
        if any(part in {"__tests__", "e2e", "spec", "test", "tests", "python-tests"} for part in Path(path).parts[:-1])
        or re.search(r"(?:^|[._-])(?:test|spec)(?:[._-]|$)", Path(path).name, flags=re.IGNORECASE)
    )[: limits["max_results"]]
    spec_candidates = sorted(
        path
        for path in files
        if path.lower().endswith((".md", ".txt"))
        and any(word in Path(path).name.lower() for word in ("spec", "prd", "requirement"))
    )[: limits["max_results"]]
    prototype_candidates = sorted(
        path
        for path in files
        if path.lower().endswith(".html")
        and (
            path.startswith(("prototype/", "prototypes/", "docs/", "delivery-docs/"))
            or "prototype" in path.lower()
        )
    )[: limits["max_results"]]
    product_sources = sorted(
        path
        for path in files
        if Path(path).suffix.lower() in CODE_SUFFIXES | STATIC_WEB_SUFFIXES
        and Path(path).parts
        and Path(path).parts[0] not in NON_PRODUCT_ROOTS
        and not any(part in NON_PRODUCT_COMPONENTS for part in Path(path).parts[:-1])
        and path not in prototype_candidates
    )[: limits["max_results"]]

    test_frameworks = sorted(
        set(frameworks) & {"playwright", "cypress", "jest", "vitest"}
        | ({"pytest"} if any(item["id"] == "python-test" for item in commands) else set())
    )
    return {
        "stacks": stacks,
        "frameworks": frameworks,
        "package_managers": package_managers,
        "commands": commands,
        "testing": {
            "frameworks": test_frameworks,
            "directories": test_directories,
            "files": test_files,
            "declared_commands": [
                item["id"] for item in commands if "test" in item["id"] and item.get("discovery") == "declared"
            ],
            "candidate_commands": [
                item["id"] for item in commands if "test" in item["id"] and item.get("discovery") != "declared"
            ],
            "executed": False,
        },
        "ci": ci,
        "documentation": documentation,
        "openspec": openspec,
        "qoder": qoder,
        "source_directories": source_directories,
        "product_sources": product_sources,
        "spec_candidates": spec_candidates,
        "prototype_candidates": prototype_candidates,
    }


def _detect_openspec(
    root: Path,
    files: set[str],
    directories: set[str],
    limits: Mapping[str, int],
    warnings: list[str],
) -> dict[str, Any]:
    present = "openspec" in directories or any(path.startswith("openspec/") for path in files)
    config_path = root / "openspec" / "config.yaml"
    config: dict[str, Any] = {
        "present": "openspec/config.yaml" in files,
        "path": "openspec/config.yaml" if "openspec/config.yaml" in files else None,
        "schema": None,
        "sha256": None,
        "parse_status": "not_present",
    }
    if config["present"]:
        text = _read_marker_text(
            root,
            config_path,
            max_bytes=limits["max_marker_bytes"],
            warnings=warnings,
        )
        if text is not None:
            match = re.search(r"(?m)^schema\s*:\s*([A-Za-z0-9._-]+)\s*$", text)
            config["schema"] = match.group(1) if match else None
            config["sha256"] = _file_digest(config_path)
            config["parse_status"] = "marker_parsed" if match else "schema_unknown"
    active_changes = sorted(
        {
            Path(path).parts[2]
            for path in files | directories
            if len(Path(path).parts) >= 3
            and Path(path).parts[:2] == ("openspec", "changes")
            and Path(path).parts[2] != "archive"
        }
    )
    archived = sorted(
        {
            Path(path).parts[3]
            for path in files | directories
            if len(Path(path).parts) >= 4
            and Path(path).parts[:3] == ("openspec", "changes", "archive")
        }
    )
    spec_files = sorted(
        path for path in files if path.startswith("openspec/specs/") and path.endswith(".md")
    )
    return {
        "present": present,
        "config": config,
        "spec_files": spec_files[: limits["max_results"]],
        "active_changes": active_changes[: limits["max_results"]],
        "active_change_count": len(active_changes),
        "_active_changes": active_changes,
        "archived_change_count": len(archived),
        "cli_executed": False,
    }


def _detect_qoder(
    root: Path,
    files: set[str],
    directories: set[str],
    limits: Mapping[str, int],
    warnings: list[str],
) -> dict[str, Any]:
    rules = sorted(path for path in files if path.startswith(".qoder/rules/"))
    hooks = sorted(path for path in files if path.startswith(".qoder/hooks/"))
    skill_paths = sorted(
        path
        for path in files
        if len(Path(path).parts) >= 4
        and Path(path).parts[:2] == (".qoder", "skills")
        and Path(path).name == "SKILL.md"
    )
    skills = sorted({Path(path).parts[2] for path in skill_paths})
    settings_files = [
        path for path in (".qoder/settings.json", ".qoder/settings.local.json", ".mcp.json") if path in files
    ]
    known_hook_events = {
        "PreToolUse",
        "PostToolUse",
        "Notification",
        "UserPromptSubmit",
        "Stop",
        "SubagentStop",
        "PreCompact",
        "SessionStart",
        "SessionEnd",
    }
    hook_events: set[str] = set()
    unknown_hook_event_count = 0
    mcp_server_refs: set[str] = set()
    for relative in settings_files:
        value = _read_marker_json(
            root,
            root / relative,
            max_bytes=limits["max_marker_bytes"],
            warnings=warnings,
        )
        if value is None:
            continue
        configured_hooks = value.get("hooks")
        if isinstance(configured_hooks, dict):
            for key in configured_hooks:
                if not isinstance(key, str):
                    continue
                if key in known_hook_events:
                    hook_events.add(key)
                else:
                    unknown_hook_event_count += 1
        for key in ("mcpServers", "mcp_servers"):
            configured_servers = value.get(key)
            if isinstance(configured_servers, dict):
                for name in configured_servers:
                    if isinstance(name, str):
                        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
                        mcp_server_refs.add(f"mcp-server-{digest}")
    return {
        "present": ".qoder" in directories or any(path.startswith(".qoder/") for path in files),
        "rules": rules[: limits["max_results"]],
        "hooks": hooks[: limits["max_results"]],
        "hook_events": sorted(hook_events)[: limits["max_results"]],
        "unknown_hook_event_count": unknown_hook_event_count,
        "skills": skills[: limits["max_results"]],
        "skill_paths": skill_paths[: limits["max_results"]],
        "settings_files": settings_files,
        "mcp_servers": sorted(mcp_server_refs)[: limits["max_results"]],
        "mcp_server_count": len(mcp_server_refs),
        "mcp_identifiers_disclosed": False,
        "configuration_values_exposed": False,
        "runtime_capabilities": {
            "quest": "unknown",
            "goal": "unknown",
            "experts": "unknown",
            "worktree": "unknown",
            "mcp_connections": "unknown",
        },
    }


def _build_adoption(capabilities: Mapping[str, Any]) -> dict[str, Any]:
    assets: list[dict[str, Any]] = []
    requirements: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    def add(kind: str, path: str, statement: str) -> None:
        if any(item["kind"] == kind and item["path"] == path for item in assets):
            return
        assets.append({"kind": kind, "path": path, "action": "adopt"})
        code = re.sub(r"[^A-Z0-9]+", "-", kind.upper()).strip("-")[:12] or "ASSET"
        stable_key = f"{kind}\0{path}".encode("utf-8")
        stable_suffix = hashlib.sha256(stable_key).hexdigest()[:10].upper()
        requirements.append(
            {
                "id": f"PR-{code}-{stable_suffix}",
                "status": "draft",
                "statement": statement,
                "evidence": [path],
                "verification_status": "not_run",
            }
        )

    for path in capabilities.get("source_directories", []):
        add("source", path, f"Preserve and extend the existing source organization at `{path}`.")
    for path in capabilities.get("product_sources", [])[:10]:
        add("product-source", path, f"Preserve existing externally observable behavior implemented by `{path}` until M3 traceability defines an approved delta.")
    for command in capabilities.get("commands", []):
        if command.get("discovery") == "declared":
            statement = f"Retain the existing `{command['id']}` command contract and verify it before changing behavior."
        else:
            statement = f"Confirm whether inferred candidate `{command['command']}` is authoritative before adopting or executing it."
        add(
            "command",
            command["source"],
            statement,
        )
    for path in capabilities.get("testing", {}).get("files", [])[:20]:
        add("characterization-test", path, f"Preserve behavior covered by `{path}` and run it as characterization evidence before implementation.")
    for item in capabilities.get("ci", []):
        for path in item.get("evidence", []):
            add("ci", path, f"Adopt the existing {item['provider']} pipeline at `{path}`.")
    for category, paths in capabilities.get("documentation", {}).items():
        for path in paths[:3]:
            add("documentation", path, f"Maintain the existing {category} documentation at `{path}`.")
    openspec = capabilities.get("openspec", {})
    if openspec.get("config", {}).get("path"):
        add("openspec", openspec["config"]["path"], "Preserve and extend the existing OpenSpec configuration.")
    for path in openspec.get("spec_files", [])[:20]:
        add("openspec-spec", path, f"Keep `{path}` as an existing OpenSpec specification authority; do not create a parallel spec.")
    for change in openspec.get("active_changes", [])[:20]:
        path = f"openspec/changes/{change}"
        add("openspec-change", path, f"Preserve and reconcile the active OpenSpec Change `{change}` before planning another overlapping Change.")
    qoder = capabilities.get("qoder", {})
    for path in qoder.get("rules", [])[:3]:
        add("qoder-rule", path, f"Continue applying the existing Qoder project rule at `{path}`.")
    for path in qoder.get("hooks", [])[:10]:
        add("qoder-hook", path, f"Preserve the Qoder Hook at `{path}`; do not execute or rewrite it during reconnaissance.")
    for path in qoder.get("skill_paths", [])[:10]:
        add("qoder-skill", path, f"Preserve the project-local Qoder Skill at `{path}` and review its authority before use.")
    for path in qoder.get("settings_files", [])[:10]:
        add("qoder-config", path, f"Preserve existing Qoder/MCP configuration at `{path}` without exposing values or creating a parallel authority.")

    managers = capabilities.get("package_managers", [])
    if len(managers) > 1:
        conflicts.append(
            {
                "id": "CONFLICT-PACKAGE-MANAGERS",
                "summary": "Multiple package-manager lock files were detected; select the authoritative manager before execution.",
                "evidence": managers,
            }
        )
    assets.sort(key=lambda item: (item["kind"], item["path"]))
    requirements.sort(key=lambda item: item["id"])
    return {
        "strategy": "adopt-not-replace",
        "semantic_extraction_status": "deferred_to_m3",
        "coverage_notice": "M2 drafts protect discovered assets and test/spec authorities; M3 must extract behavior-level Preservation Requirements before implementation.",
        "existing_assets": assets,
        "conflicts": conflicts,
        "preservation_requirements": requirements,
    }


def _route(
    capabilities: Mapping[str, Any],
    scan: Mapping[str, Any],
    git: Mapping[str, Any],
    delivery: Mapping[str, Any],
    override: str,
) -> dict[str, Any]:
    reasons: list[str] = []
    blocked = False
    control_exists = bool(delivery.get("directory_exists") or delivery.get("manifest_present"))
    control_valid = bool(delivery.get("valid"))
    phase = delivery.get("phase")
    terminal = phase == "CLOSED" and control_valid

    if control_exists and not control_valid:
        recommended = "resume"
        reasons.append("control-plane-invalid-or-partial")
        blocked = True
    elif control_valid and not terminal:
        recommended = "resume"
        reasons.append("valid-nonterminal-control-plane")
    elif capabilities.get("product_sources"):
        recommended = "brownfield"
        reasons.append("existing-product-source")
    else:
        recommended = "greenfield"
        reasons.append("no-product-implementation")
        if capabilities.get("spec_candidates") or capabilities.get("prototype_candidates"):
            reasons.append("spec-or-prototype-input")
    if terminal:
        reasons.append("terminal-control-plane")

    if git.get("status") in {"unknown", "error", "nested"} or git.get("nested_project"):
        blocked = True
        reasons.append("git-scope-unknown")
    if scan.get("truncated"):
        blocked = True
        reasons.append("scan-incomplete")
    elif scan.get("status") != "complete":
        blocked = True
        reasons.append("scan-warnings")
    critical_symlinks = list(scan.get("critical_symlinks", []))
    if scan.get("critical_symlink_count", len(critical_symlinks)):
        blocked = True
        reasons.append("critical-path-symlink")
    special_entry_count = scan.get("counts", {}).get("special_entries", 0)
    if special_entry_count:
        blocked = True
        reasons.append("unsupported-special-entry")

    override_requested = None if override == "auto" else override
    selected = recommended
    override_applied = False
    if override_requested:
        if recommended == "resume" and control_exists and override_requested != "resume":
            blocked = True
            reasons.append("override-cannot-bypass-resume")
        elif override_requested == "resume" and not control_exists:
            blocked = True
            reasons.append("override-resume-without-control-plane")
        elif override_requested != recommended:
            selected = override_requested
            override_applied = True
            reasons.append("explicit-route-override")

    confidence = "low" if blocked or override_applied else "high" if reasons[0] != "no-product-implementation" else "medium"
    return {
        "recommended": recommended,
        "selected": selected,
        "override_requested": override_requested,
        "override_applied": override_applied,
        "confidence": confidence,
        "reason_codes": list(dict.fromkeys(reasons)),
        "blocked": blocked,
        "critical_symlinks": critical_symlinks,
    }


def _safe_source_digest(root: Path, source_paths: Sequence[str]) -> dict[str, Any]:
    if not source_paths:
        return {"status": "unknown", "paths": [], "actual_digest": None, "reason": "source paths were not supplied"}
    entries: list[dict[str, str]] = []
    missing: list[str] = []
    unsafe: list[str] = []
    for raw in sorted(set(source_paths)):
        path = Path(raw)
        if path.is_absolute():
            lexical = path
            try:
                relative = lexical.relative_to(root)
            except ValueError:
                unsafe.append(raw)
                continue
        else:
            relative = path
            lexical = root / path
        safe, reason = _safe_regular_file(root, lexical)
        if not safe:
            if reason == "path is missing":
                missing.append(relative.as_posix())
            else:
                unsafe.append(relative.as_posix())
            continue
        entries.append({"path": relative.as_posix(), "sha256": _file_digest(lexical)})
    if unsafe:
        return {"status": "unknown", "paths": sorted(set(source_paths)), "unsafe_paths": unsafe, "actual_digest": None}
    if missing:
        return {"status": "missing", "paths": sorted(set(source_paths)), "missing_paths": missing, "actual_digest": None}
    actual = entries[0]["sha256"] if len(entries) == 1 else _canonical_digest(entries)
    return {"status": "unknown", "paths": [item["path"] for item in entries], "actual_digest": actual}


def _git_diff_paths(
    root: Path,
    baseline: str,
    pathspec: str | None,
    runner: GitRunner | None,
) -> tuple[list[str] | None, str | None]:
    if runner is None:
        return None, "a Git runner was not provided"
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", baseline):
        return None, "registered Git baseline is not a full object ID"
    arguments = ["diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "--end-of-options", baseline, "--"]
    if pathspec:
        arguments.append(pathspec)
    try:
        result = runner(root, arguments)
    except Exception as exc:  # callback errors are converted into unknown evidence
        return None, f"Git comparison failed: {exc}"
    if getattr(result, "returncode", 1) != 0:
        detail = (getattr(result, "stderr", "") or "").strip() or f"exit status {getattr(result, 'returncode', '?')}"
        return None, f"Git comparison failed: {detail}"
    return sorted(path for path in (getattr(result, "stdout", "") or "").split("\0") if path), None


def _control_only_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return (
        normalized in {"delivery-docs/state", ".delivery"}
        or normalized.startswith("delivery-docs/state/")
        or normalized.startswith("delivery-docs/verification/runs/")
        # Legacy layouts kept for backward compatibility.
        or normalized.startswith(".delivery/")
        or normalized.startswith("docs/verification/runs/")
    )


def _public_git_info(git: Mapping[str, Any], cap: int) -> dict[str, Any]:
    public = dict(git)
    for key in ("dirty_paths", "gate_relevant_dirty_paths"):
        paths = list(git.get(key) or [])
        public[f"{key[:-1]}_count"] = len(paths)
        public[f"{key[:-1]}_truncated"] = len(paths) > cap
        public[key] = paths[:cap]
    return public


def _drift(
    root: Path,
    route: Mapping[str, Any],
    git: Mapping[str, Any],
    delivery: Mapping[str, Any],
    capabilities: Mapping[str, Any],
    scan: Mapping[str, Any],
    source_paths: Sequence[str],
    runner: GitRunner | None,
) -> dict[str, Any]:
    if route["recommended"] != "resume":
        empty = {"status": "not_applicable"}
        return {"source": dict(empty), "git": dict(empty), "openspec": dict(empty), "evidence": dict(empty), "overall": "not_applicable"}
    state = delivery.get("state") if isinstance(delivery.get("state"), dict) else {}
    if not delivery.get("valid"):
        unknown = {"status": "unknown", "reason": "control plane is invalid or partial"}
        return {"source": dict(unknown), "git": dict(unknown), "openspec": dict(unknown), "evidence": dict(unknown), "overall": "unknown"}

    source = _safe_source_digest(root, source_paths)
    expected_source = state.get("source_digest")
    source["expected_digest"] = expected_source
    if expected_source is None:
        source["status"] = "unknown"
        source["reason"] = "state.source_digest is not registered"
    elif source["status"] not in {"missing", "unknown"}:
        pass
    elif source.get("actual_digest"):
        source["status"] = "unchanged" if source["actual_digest"] == expected_source else "changed"

    baseline = state.get("baseline_commit")
    current = git.get("commit")
    relevant_dirty = list(git.get("gate_relevant_dirty_paths") or [])
    git_drift: dict[str, Any] = {
        "status": "unknown",
        "baseline_commit": baseline,
        "current_commit": current,
        "dirty_paths": relevant_dirty,
    }
    all_diff_paths: list[str] | None = None
    git_status = git.get("status")
    if not baseline:
        git_drift["reason"] = "state.baseline_commit is not registered"
    elif git.get("repository") is not True or not current or git_status not in {"clean", "dirty"}:
        git_drift["reason"] = "current Git identity is unknown"
    else:
        all_diff_paths, error = _git_diff_paths(root, baseline, None, runner)
        if all_diff_paths is None and baseline != current:
            git_drift["status"] = "changed"
            git_drift["reason"] = error
        elif all_diff_paths is None:
            git_drift["reason"] = error
        else:
            all_diff_paths = [path for path in all_diff_paths if not _control_only_path(path)]
            changed = baseline != current or bool(all_diff_paths) or bool(relevant_dirty)
            git_drift["status"] = "changed" if changed else "unchanged"
            git_drift["changed_paths"] = sorted(set(all_diff_paths + relevant_dirty))[:200]

    active_change = state.get("active_change")
    openspec_capability = capabilities.get("openspec", {})
    active_changes = openspec_capability.get("active_changes", [])
    active_change_index = openspec_capability.get("_active_changes", active_changes)
    openspec_drift: dict[str, Any] = {
        "status": "unknown",
        "active_change": active_change,
        "detected_active_changes": active_changes,
    }
    scan_complete = scan.get("status") == "complete" and not scan.get("truncated")
    if active_change and active_change not in active_change_index and not scan_complete:
        openspec_drift["reason"] = "the bounded scan did not prove whether the registered active OpenSpec Change exists"
    elif active_change and active_change not in active_change_index:
        openspec_drift["status"] = "missing"
        openspec_drift["reason"] = "the registered active OpenSpec Change is missing"
    elif not baseline or git.get("repository") is not True or git_status not in {"clean", "dirty"}:
        openspec_drift["reason"] = "OpenSpec baseline cannot be compared without registered Git evidence"
    else:
        openspec_paths, error = _git_diff_paths(root, baseline, "openspec", runner)
        dirty_openspec = sorted(path for path in relevant_dirty if path == "openspec" or path.startswith("openspec/"))
        if openspec_paths is None:
            openspec_drift["reason"] = error
        else:
            changed = bool(openspec_paths or dirty_openspec)
            openspec_drift["status"] = "changed" if changed else "unchanged"
            openspec_drift["changed_paths"] = sorted(set(openspec_paths + dirty_openspec))[:200]

    gate = delivery.get("gate") if isinstance(delivery.get("gate"), dict) else {}
    gate_status = gate.get("status")
    if gate_status in {None, "not_run"}:
        evidence = {"status": "not_applicable", "gate_status": gate_status or "not_run"}
    elif gate_status == "passed":
        binding_mismatches: list[str] = []
        if gate.get("change_id") != state.get("active_change"):
            binding_mismatches.append("active_change")
        if gate.get("source_digest") != state.get("source_digest"):
            binding_mismatches.append("source_digest")
        if gate.get("commit") != git.get("commit"):
            binding_mismatches.append("commit")
        verified_local = any(
            item.get("kind") == "file" and item.get("verified") is True
            for item in gate.get("artifacts", [])
            if isinstance(item, dict)
        )
        if not verified_local:
            binding_mismatches.append("verified_local_artifact")
        if binding_mismatches or gate.get("effective_status") == "stale":
            evidence = {
                "status": "changed",
                "gate_status": gate_status,
                "reason": "prior evidence is stale or bound to different inputs",
                "binding_mismatches": binding_mismatches,
            }
        else:
            # M2 can bind a gate to source/change/commit and verify local bytes,
            # but cannot yet reproduce its environment, configuration, or test
            # suite. It must not promote that partial check to `unchanged`.
            evidence = {
                "status": "unknown",
                "gate_status": gate_status,
                "reason": "M2 cannot revalidate gate environment, configuration, and test-suite identity",
            }
    else:
        evidence = {"status": "unknown", "gate_status": gate_status}

    statuses = [source["status"], git_drift["status"], openspec_drift["status"], evidence["status"]]
    if "missing" in statuses:
        overall = "missing"
    elif "changed" in statuses:
        overall = "changed"
    elif "unknown" in statuses:
        overall = "unknown"
    elif any(value == "unchanged" for value in statuses):
        overall = "unchanged"
    else:
        overall = "not_applicable"
    assert all(value in DRIFT_STATUSES for value in statuses + [overall])
    return {
        "source": source,
        "git": git_drift,
        "openspec": openspec_drift,
        "evidence": evidence,
        "overall": overall,
    }


def _risks(
    scan: Mapping[str, Any],
    route: Mapping[str, Any],
    capabilities: Mapping[str, Any],
    adoption: Mapping[str, Any],
    drift: Mapping[str, Any],
    git: Mapping[str, Any],
) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []

    def add(identifier: str, severity: str, summary: str, evidence: Sequence[str] = ()) -> None:
        risks.append({"id": identifier, "severity": severity, "summary": summary, "evidence": list(evidence)})

    if scan.get("truncated"):
        add("RISK-SCAN-INCOMPLETE", "high", "The repository scan reached a deterministic budget.", scan.get("truncated_reasons", []))
    if route.get("critical_symlinks"):
        add("RISK-CRITICAL-SYMLINK", "high", "A critical configuration or OpenSpec path is a symbolic link.", route["critical_symlinks"])
    if scan.get("counts", {}).get("special_entries", 0):
        add(
            "RISK-UNSUPPORTED-SPECIAL-ENTRY",
            "high",
            "The project contains a non-regular filesystem entry that M2 cannot inspect safely.",
            scan.get("special_entries", []),
        )
    if git.get("status") in {"unknown", "error", "nested"}:
        add("RISK-GIT-SCOPE", "high", "Git identity or project scope is not reliable.")
    if adoption.get("conflicts"):
        add("RISK-CONFLICTING-ASSETS", "medium", "Existing engineering assets require an explicit authority choice.")
    if not capabilities.get("testing", {}).get("declared_commands"):
        add("RISK-NO-TEST-COMMAND", "medium", "No declared automated test command was detected.")
    if route.get("recommended") == "resume" and drift.get("overall") in {"changed", "missing", "unknown"}:
        add("RISK-RESUME-DRIFT", "high", f"Resume evidence is {drift.get('overall')} and must be resolved before continuation.")
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(risks, key=lambda item: (order[item["severity"]], item["id"]))


def build_reconnaissance(
    root: Path,
    *,
    git: Mapping[str, Any],
    delivery: Mapping[str, Any],
    source_paths: Sequence[str] = (),
    route_override: str = "auto",
    git_runner: GitRunner | None = None,
    limits: Mapping[str, Any] | None = None,
    inspected_at: str | None = None,
    harness_version: str = "0.1.0",
) -> dict[str, Any]:
    """Build the M2 report. The target tree is never modified."""

    root = root.resolve()
    if route_override not in {"auto", "greenfield", "brownfield", "resume"}:
        raise ValueError("route_override must be auto, greenfield, brownfield, or resume")
    scan_internal = scan_project(root, limits)
    capabilities = detect_capabilities(root, scan_internal)
    if scan_internal["warnings"] and scan_internal["status"] == "complete":
        scan_internal["status"] = "partial"
    scan = {key: value for key, value in scan_internal.items() if not key.startswith("_")}
    capabilities = {"git": _public_git_info(git, scan["limits"]["max_results"]), **capabilities}
    adoption = _build_adoption(capabilities)
    route = _route(capabilities, scan, git, delivery, route_override)
    drift = _drift(root, route, git, delivery, capabilities, scan, source_paths, git_runner)
    capabilities.get("openspec", {}).pop("_active_changes", None)
    if route["recommended"] == "resume" and drift["overall"] in {"changed", "missing", "unknown"}:
        route["blocked"] = True
        route["reason_codes"] = list(dict.fromkeys([*route["reason_codes"], f"resume-drift-{drift['overall']}"]))
    risks = _risks(scan, route, capabilities, adoption, drift, git)
    if route["blocked"]:
        if "control-plane-invalid-or-partial" in route["reason_codes"]:
            next_action = "repair-or-recover-control-plane-with-user-review"
        elif route["recommended"] == "resume":
            next_action = "resolve-resume-drift-or-unknown-evidence"
        else:
            next_action = "resolve-reconnaissance-blockers"
    elif route["selected"] == "greenfield":
        next_action = "confirm-inputs-before-m3-clarification"
    elif route["selected"] == "brownfield":
        next_action = "review-preservation-drafts-before-m3-planning"
    else:
        next_action = "review-resume-audit-before-any-state-transition"

    report: dict[str, Any] = {
        "inspection_schema_version": INSPECTION_SCHEMA_VERSION,
        "harness_version": harness_version,
        "implemented_milestone": IMPLEMENTED_MILESTONE,
        "inspected_at": inspected_at or _iso_now(),
        "project_root": str(root),
        "read_only": True,
        "persistence": "stdout-only",
        "scan": scan,
        "route": route,
        "capabilities": capabilities,
        "adoption": adoption,
        "drift": drift,
        "risks": risks,
        "next_action": next_action,
    }
    stable = json.loads(json.dumps(report, ensure_ascii=False))
    stable.pop("inspected_at", None)
    stable.pop("project_root", None)
    stable.get("capabilities", {}).get("git", {}).pop("root", None)
    report["report_digest"] = _canonical_digest(stable)
    return report
