#!/usr/bin/env python3
"""Validate the Qoder-native execution and acceptance loop.

This standard-library-only helper never executes project tests, browsers,
OpenSpec lifecycle commands, Worktree operations, deployment, or production
actions.  Qoder and project-native tools produce facts; loopctl binds and
validates the Stage B execution authority, attempts, traceability, evidence,
and quality scorecard.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
HEX64 = re.compile(r"^[0-9a-f]{64}$")
GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
CHANGE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
STABLE_ID = re.compile(r"^[A-Z][A-Z0-9._-]{1,127}$")
RUN_ID = re.compile(r"^RUN-[A-Za-z0-9._-]+$")

ROW_STATUSES = {"planned", "implemented", "code_verified", "acceptance_verified", "integrated"}
TEST_STATUSES = {"not_run", "passed", "failed", "blocked", "skipped", "flaky"}
TEST_LEVELS = {"static", "unit", "component", "contract", "integration", "e2e", "system", "manual"}
DEFECT_SEVERITIES = {"critical", "high", "medium", "low"}
DEFECT_STATUSES = {"open", "fixed", "verified", "accepted"}
ATTEMPT_OUTCOMES = {"passed", "failed", "blocked", "paused"}
CHECK_STATUSES = {"passed", "failed", "blocked", "skipped", "flaky", "not_run"}
PROTOTYPE_KINDS = {"screen", "component", "interaction", "state", "journey"}
PROTOTYPE_STATUSES = {"planned", "verified", "deviation_approved"}

HARD_GATES = {
    "requirements_complete",
    "core_acceptance_passed",
    "no_unapproved_spec_or_prototype_deviation",
    "no_critical_or_high_defects",
    "no_hidden_skips_or_critical_flaky",
    "integration_commit_bound",
    "evidence_fresh",
    "required_risk_gates_passed",
    "independent_evaluation_passed",
    "docs_and_recovery_complete",
}

QUALITY_DEFAULTS: dict[str, tuple[int, int]] = {
    "requirements-and-functionality": (30, 27),
    "prototype-ux-and-states": (15, 12),
    "automated-tests-and-regression": (15, 13),
    "architecture-and-maintainability": (10, 8),
    "security-data-and-reliability": (10, 9),
    "performance-accessibility-compatibility": (10, 8),
    "documentation-operations-and-recovery": (10, 8),
}


class LoopError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LoopError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def project_root(raw: str | Path) -> Path:
    root = Path(raw).absolute()
    try:
        info = root.lstat()
    except OSError as exc:
        raise LoopError(f"cannot inspect project root: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise LoopError("project root must be a real directory, not a symlink or special node")
    return root


def relative_path(raw: Any, label: str) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise LoopError(f"{label} must be a canonical project-relative path")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or path.as_posix() != raw:
        raise LoopError(f"{label} must be a canonical project-relative path")
    if path.parts[0].casefold() == ".git":
        raise LoopError(f"{label} cannot enter .git")
    return path


def checked_path(root: Path, raw: Any, label: str, *, regular: bool = True) -> Path:
    relative = relative_path(raw, label)
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise LoopError(f"{label} is missing or unreadable: {relative.as_posix()}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise LoopError(f"{label} cannot traverse a symlink: {relative.as_posix()}")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise LoopError(f"{label} parent is not a directory: {relative.as_posix()}")
    if regular and not stat.S_ISREG(current.lstat().st_mode):
        raise LoopError(f"{label} must be a regular file: {relative.as_posix()}")
    return current


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object_no_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError, LoopError) as exc:
        raise LoopError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise LoopError(f"{label} must be a JSON object")
    return value


def read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise LoopError(f"cannot read {label}: {exc}") from exc
    result: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            raise LoopError(f"{label} line {index} is empty")
        try:
            value = json.loads(line, object_pairs_hook=_object_no_duplicates)
        except (json.JSONDecodeError, LoopError) as exc:
            raise LoopError(f"{label} line {index} is invalid: {exc}") from exc
        if not isinstance(value, dict):
            raise LoopError(f"{label} line {index} must be an object")
        result.append(value)
    return result


def ensure_directory(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise LoopError("execution directory escaped the project root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise LoopError(f"directory path is unsafe: {relative.as_posix()}")
        else:
            current.mkdir()


def exclusive_write(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise LoopError(f"refusing to overwrite {path}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def atomic_rewrite(path: Path, content: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise LoopError(f"refusing to rewrite unsafe attempts ledger: {path}")
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def git_output(root: Path, args: Sequence[str]) -> str:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise LoopError(f"Git query failed: {' '.join(args)}: {exc}") from exc
    output = completed.stdout.strip()
    if len(output) > 1024 * 1024:
        raise LoopError("Git output exceeded 1 MiB")
    return output


def current_commit(root: Path) -> str:
    commit = git_output(root, ["rev-parse", "--verify", "HEAD"])
    if not GIT_OID.fullmatch(commit):
        raise LoopError("Git HEAD is not a full object ID")
    return commit


def commit_exists(root: Path, commit: str) -> bool:
    if not isinstance(commit, str) or not GIT_OID.fullmatch(commit):
        return False
    try:
        git_output(root, ["cat-file", "-e", f"{commit}^{{commit}}"])
    except LoopError:
        return False
    return True


def string_list(value: Any, label: str, *, nonempty: bool = False, pattern: re.Pattern[str] | None = None) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise LoopError(f"{label} must be a list of non-empty strings")
    result = [item.strip() for item in value]
    if nonempty and not result:
        raise LoopError(f"{label} cannot be empty")
    if len(result) != len(set(result)):
        raise LoopError(f"{label} cannot contain duplicates")
    if pattern is not None and any(not pattern.fullmatch(item) for item in result):
        raise LoopError(f"{label} contains an invalid stable ID")
    return result


def require_fields(value: Mapping[str, Any], fields: Iterable[str], label: str) -> None:
    missing = sorted(field for field in fields if field not in value)
    if missing:
        raise LoopError(f"{label} is missing: {', '.join(missing)}")


def validate_version(value: Mapping[str, Any], artifact_type: str, label: str) -> None:
    if value.get("schema_version") != SCHEMA_VERSION:
        raise LoopError(f"{label}.schema_version must be {SCHEMA_VERSION}")
    if value.get("artifact_type") != artifact_type:
        raise LoopError(f"{label}.artifact_type must be {artifact_type!r}")


def execution_paths(root: Path, change_id: str) -> dict[str, Path]:
    if not CHANGE_ID.fullmatch(change_id):
        raise LoopError("change ID must be lowercase kebab-case")
    directory = root / "delivery-docs" / "state" / "execution" / change_id
    return {
        "directory": directory,
        "manifest": directory / "execution-manifest.json",
        "matrix": directory / "verification-matrix.json",
        "attempts": directory / "attempts.jsonl",
        "scorecard": directory / "quality-scorecard.json",
    }


def planning_seal(root: Path, mode: str) -> dict[str, str] | None:
    if mode == "quick":
        return None
    path = checked_path(root, "delivery-docs/state/planning-manifest.json", "planning seal")
    return {"path": "delivery-docs/state/planning-manifest.json", "sha256": file_digest(path)}


def prototype_binding(root: Path, raw_path: str | None, required: bool, reason: str | None) -> tuple[dict[str, Any], list[str]]:
    item_ids: list[str] = []
    if raw_path:
        path = checked_path(root, raw_path, "prototype contract")
        contract = read_json(path, "prototype contract")
        validate_version(contract, "prototype-contract", "prototype contract")
        applicable = contract.get("applicable")
        if required and applicable is not True:
            raise LoopError("a required prototype contract must be applicable")
        items = contract.get("items")
        if applicable is True:
            validate_prototype_source(root, contract)
            if not isinstance(items, list) or not items:
                raise LoopError("an applicable prototype contract needs at least one item")
            for index, item in enumerate(items):
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not STABLE_ID.fullmatch(item["id"]):
                    raise LoopError(f"prototype contract item {index} has an invalid ID")
                if item["id"] in item_ids:
                    raise LoopError(f"prototype contract item {index} has a duplicated ID")
                item_ids.append(item["id"])
        elif not isinstance(contract.get("not_applicable_reason"), str) or not contract["not_applicable_reason"].strip():
            raise LoopError("a non-applicable prototype contract needs a reason")
        return {
            "required": required,
            "path": raw_path,
            "sha256": file_digest(path),
            "not_applicable_reason": None if applicable is True else contract.get("not_applicable_reason"),
        }, item_ids
    if required:
        raise LoopError("prototype-required needs --prototype-contract")
    if not isinstance(reason, str) or not reason.strip():
        raise LoopError("non-prototype work needs --prototype-not-applicable-reason")
    return {"required": False, "path": None, "sha256": None, "not_applicable_reason": reason.strip()}, []


def validate_prototype_source(root: Path, contract: Mapping[str, Any]) -> None:
    source = contract.get("source")
    if not isinstance(source, dict):
        raise LoopError("applicable prototype contract needs a source object")
    require_fields(source, {"path", "sha256"}, "prototype source")
    if not isinstance(source["sha256"], str) or not HEX64.fullmatch(source["sha256"]):
        raise LoopError("prototype source sha256 is invalid")
    path = checked_path(root, source["path"], "prototype source")
    if file_digest(path) != source["sha256"]:
        raise LoopError("prototype source digest does not match its contract")


def default_scorecard(change_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "quality-scorecard",
        "change_id": change_id,
        "subject_commit": None,
        "hard_gates": {key: False for key in sorted(HARD_GATES)},
        "dimensions": [
            {"id": key, "weight": weight, "minimum": minimum, "score": 0, "evidence_refs": []}
            for key, (weight, minimum) in QUALITY_DEFAULTS.items()
        ],
        "redistribution_approval_ref": None,
        "total_score": 0,
        "independent_evaluator": {"identity": None, "report_path": None, "report_sha256": None},
        "status": "draft",
    }


def command_init(args: argparse.Namespace) -> dict[str, Any]:
    root = project_root(args.project_root)
    paths = execution_paths(root, args.change_id)
    requirements = string_list(args.requirement, "requirements", nonempty=True, pattern=STABLE_ID)
    acceptance = string_list(args.acceptance, "acceptance", nonempty=True, pattern=STABLE_ID)
    source_bindings: list[dict[str, str]] = []
    seen_sources: set[str] = set()
    for index, raw_path in enumerate(args.source):
        normalized = str(relative_path(raw_path, f"sources[{index}]"))
        if normalized in seen_sources:
            raise LoopError(f"source path is duplicated: {normalized}")
        seen_sources.add(normalized)
        path = checked_path(root, normalized, f"sources[{index}]")
        source_bindings.append({"path": normalized, "sha256": file_digest(path)})
    if args.path == "quick" and not source_bindings:
        raise LoopError("quick execution needs at least one --source binding")
    if args.path == "quick":
        source_digest = canonical_digest(source_bindings)
    else:
        if not isinstance(args.source_digest, str) or not HEX64.fullmatch(args.source_digest):
            raise LoopError("enhanced execution needs a 64-character lowercase source digest")
        source_digest = args.source_digest
    if not isinstance(args.approval_ref, str) or not args.approval_ref.strip():
        raise LoopError("execution needs a durable approval reference")
    if not args.scope:
        raise LoopError("execution needs at least one scope item")
    if not args.non_goal:
        raise LoopError("execution needs at least one explicit non-goal")
    if not args.integration_owner.strip():
        raise LoopError("integration owner cannot be empty")
    if not (1 <= args.max_same_failure <= args.max_attempts):
        raise LoopError("max-same-failure must be between 1 and max-attempts")
    if not (1 <= args.max_no_progress <= args.max_attempts):
        raise LoopError("max-no-progress must be between 1 and max-attempts")
    prototype, prototype_ids = prototype_binding(
        root,
        args.prototype_contract,
        args.prototype_required,
        args.prototype_not_applicable_reason,
    )
    seal = planning_seal(root, args.path)
    starting = current_commit(root)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "execution-manifest",
        "change_id": args.change_id,
        "route": args.route,
        "path": args.path,
        "source_digest": source_digest,
        "source_bindings": source_bindings,
        "planning_seal": seal,
        "starting_commit": starting,
        "requirement_ids": requirements,
        "acceptance_ids": acceptance,
        "prototype_item_ids": prototype_ids,
        "scope": list(dict.fromkeys(args.scope)),
        "non_goals": list(dict.fromkeys(args.non_goal)),
        "authorization": {
            "approval_ref": args.approval_ref.strip(),
            "approved_at": utc_now(),
            "implementation_authorized": True,
            "openspec_apply_authorized": True,
        },
        "environment": {"mode": args.environment, "integration_owner": args.integration_owner.strip()},
        "repair_budget": {
            "max_attempts": args.max_attempts,
            "max_same_failure": args.max_same_failure,
            "max_no_progress_attempts": args.max_no_progress,
        },
        "prototype_contract": prototype,
        "status": "authorized",
        "created_at": utc_now(),
    }
    rows = [
        {
            "id": f"CHECK-{index:03d}",
            "requirement_ids": [],
            "acceptance_ids": [acceptance_id],
            "prototype_item_ids": [],
            "task_refs": [],
            "code_refs": [],
            "tests": [],
            "evidence": [],
            "commit": None,
            "environment": None,
            "independent_review": {"status": "not_run", "reviewer": None, "evidence_refs": []},
            "status": "planned",
        }
        for index, acceptance_id in enumerate(acceptance, start=1)
    ]
    matrix = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "verification-matrix",
        "change_id": args.change_id,
        "source_digest": source_digest,
        "subject_commit": None,
        "rows": rows,
        "defects": [],
    }
    ensure_directory(root, paths["directory"])
    for path in (paths["manifest"], paths["matrix"], paths["attempts"], paths["scorecard"]):
        if path.exists() or path.is_symlink():
            raise LoopError(f"refusing to overwrite existing execution artifact: {path}")
    exclusive_write(paths["manifest"], json_text(manifest))
    exclusive_write(paths["matrix"], json_text(matrix))
    exclusive_write(paths["attempts"], "")
    exclusive_write(paths["scorecard"], json_text(default_scorecard(args.change_id)))
    return {
        "created": True,
        "change_id": args.change_id,
        "execution_manifest_digest": canonical_digest(manifest),
        "starting_commit": starting,
        "paths": {key: str(path.relative_to(root)) for key, path in paths.items() if key != "directory"},
    }


def validate_manifest(value: Mapping[str, Any], change_id: str) -> None:
    validate_version(value, "execution-manifest", "execution manifest")
    require_fields(
        value,
        {
            "change_id", "route", "path", "source_digest", "source_bindings", "planning_seal", "starting_commit",
            "requirement_ids", "acceptance_ids", "prototype_item_ids", "scope", "non_goals",
            "authorization", "environment", "repair_budget", "prototype_contract", "status", "created_at",
        },
        "execution manifest",
    )
    if value["change_id"] != change_id:
        raise LoopError("execution manifest change_id mismatch")
    if value["route"] not in {"greenfield", "brownfield", "resume"}:
        raise LoopError("execution manifest route is invalid")
    if value["path"] not in {"quick", "enhanced"}:
        raise LoopError("execution manifest path is invalid")
    if not isinstance(value["source_digest"], str) or not HEX64.fullmatch(value["source_digest"]):
        raise LoopError("execution manifest source_digest is invalid")
    bindings = value["source_bindings"]
    if not isinstance(bindings, list) or (value["path"] == "quick" and not bindings):
        raise LoopError("quick execution manifest needs source bindings")
    seen_sources: set[str] = set()
    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict):
            raise LoopError(f"source_bindings[{index}] must be an object")
        require_fields(binding, {"path", "sha256"}, f"source_bindings[{index}]")
        normalized = str(relative_path(binding["path"], f"source_bindings[{index}].path"))
        if normalized in seen_sources:
            raise LoopError("execution manifest source bindings contain a duplicate path")
        seen_sources.add(normalized)
        if not isinstance(binding["sha256"], str) or not HEX64.fullmatch(binding["sha256"]):
            raise LoopError(f"source_bindings[{index}].sha256 is invalid")
    if not isinstance(value["starting_commit"], str) or not GIT_OID.fullmatch(value["starting_commit"]):
        raise LoopError("execution manifest starting_commit is invalid")
    string_list(value["requirement_ids"], "execution manifest requirement_ids", nonempty=True, pattern=STABLE_ID)
    string_list(value["acceptance_ids"], "execution manifest acceptance_ids", nonempty=True, pattern=STABLE_ID)
    string_list(value["prototype_item_ids"], "execution manifest prototype_item_ids", pattern=STABLE_ID)
    string_list(value["scope"], "execution manifest scope", nonempty=True)
    string_list(value["non_goals"], "execution manifest non_goals", nonempty=True)
    authorization = value["authorization"]
    if not isinstance(authorization, dict) or not isinstance(authorization.get("approval_ref"), str) or not authorization["approval_ref"].strip():
        raise LoopError("execution authorization needs an approval_ref")
    if authorization.get("implementation_authorized") is not True or authorization.get("openspec_apply_authorized") is not True:
        raise LoopError("execution authorization must explicitly authorize implementation and OpenSpec Apply")
    if value["status"] not in {"authorized", "paused", "blocked", "complete"}:
        raise LoopError("execution manifest status is invalid")
    budget = value["repair_budget"]
    if not isinstance(budget, dict):
        raise LoopError("repair_budget must be an object")
    for key in ("max_attempts", "max_same_failure", "max_no_progress_attempts"):
        if not isinstance(budget.get(key), int) or budget[key] < 1:
            raise LoopError(f"repair_budget.{key} must be a positive integer")
    if budget["max_same_failure"] > budget["max_attempts"] or budget["max_no_progress_attempts"] > budget["max_attempts"]:
        raise LoopError("repair sub-budgets cannot exceed max_attempts")


def _evidence(root: Path, value: Any, label: str) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise LoopError(f"{label} must be an object")
    require_fields(value, {"id", "kind", "path", "sha256"}, label)
    if not isinstance(value["id"], str) or not STABLE_ID.fullmatch(value["id"]):
        raise LoopError(f"{label}.id is invalid")
    if not isinstance(value["kind"], str) or not value["kind"].strip():
        raise LoopError(f"{label}.kind is required")
    if not isinstance(value["sha256"], str) or not HEX64.fullmatch(value["sha256"]):
        raise LoopError(f"{label}.sha256 is invalid")
    path = checked_path(root, value["path"], label)
    if file_digest(path) != value["sha256"]:
        raise LoopError(f"{label} digest does not match {value['path']}")
    return value["id"], value["path"]


def validate_prototype(root: Path, manifest: Mapping[str, Any], final: bool) -> set[str]:
    binding = manifest.get("prototype_contract")
    if not isinstance(binding, dict):
        raise LoopError("execution manifest prototype_contract must be an object")
    required = binding.get("required") is True
    path_raw = binding.get("path")
    expected_ids = set(string_list(manifest.get("prototype_item_ids"), "prototype item IDs", pattern=STABLE_ID))
    if path_raw is None:
        if required or expected_ids:
            raise LoopError("required prototype contract is missing")
        if not isinstance(binding.get("not_applicable_reason"), str) or not binding["not_applicable_reason"].strip():
            raise LoopError("prototype not-applicable reason is missing")
        return set()
    path = checked_path(root, path_raw, "prototype contract")
    if file_digest(path) != binding.get("sha256"):
        raise LoopError("prototype contract changed after execution authorization")
    contract = read_json(path, "prototype contract")
    validate_version(contract, "prototype-contract", "prototype contract")
    if contract.get("applicable") is not True:
        if required:
            raise LoopError("required prototype contract cannot be not applicable")
        if not isinstance(contract.get("not_applicable_reason"), str) or not contract["not_applicable_reason"].strip():
            raise LoopError("non-applicable prototype contract needs a reason")
        return set()
    validate_prototype_source(root, contract)
    items = contract.get("items")
    if not isinstance(items, list) or not items:
        raise LoopError("applicable prototype contract needs items")
    requirement_ids = set(manifest["requirement_ids"])
    acceptance_ids = set(manifest["acceptance_ids"])
    seen: set[str] = set()
    deviations = contract.get("deviations")
    if not isinstance(deviations, list):
        raise LoopError("prototype deviations must be a list")
    approved_deviations = {
        item.get("item_id")
        for item in deviations
        if isinstance(item, dict)
        and item.get("status") == "approved"
        and isinstance(item.get("approval_ref"), str)
        and item["approval_ref"].strip()
    }
    for index, item in enumerate(items):
        label = f"prototype items[{index}]"
        if not isinstance(item, dict):
            raise LoopError(f"{label} must be an object")
        require_fields(
            item,
            {"id", "kind", "locator", "requirement_ids", "acceptance_ids", "states", "breakpoints", "accessibility_expectations", "visual_oracle", "status"},
            label,
        )
        identifier = item["id"]
        if not isinstance(identifier, str) or not STABLE_ID.fullmatch(identifier) or identifier in seen:
            raise LoopError(f"{label}.id is invalid or duplicated")
        seen.add(identifier)
        if item["kind"] not in PROTOTYPE_KINDS:
            raise LoopError(f"{label}.kind is invalid")
        if not isinstance(item["locator"], str) or not item["locator"].strip():
            raise LoopError(f"{label}.locator is required")
        requirements = set(string_list(item["requirement_ids"], f"{label}.requirement_ids", nonempty=True, pattern=STABLE_ID))
        acceptance = set(string_list(item["acceptance_ids"], f"{label}.acceptance_ids", nonempty=True, pattern=STABLE_ID))
        if not requirements <= requirement_ids or not acceptance <= acceptance_ids:
            raise LoopError(f"{label} references IDs outside the execution authority")
        string_list(item["states"], f"{label}.states", nonempty=True)
        string_list(item["breakpoints"], f"{label}.breakpoints")
        string_list(item["accessibility_expectations"], f"{label}.accessibility_expectations")
        if not isinstance(item["visual_oracle"], str) or not item["visual_oracle"].strip():
            raise LoopError(f"{label}.visual_oracle is required")
        if item["status"] not in PROTOTYPE_STATUSES:
            raise LoopError(f"{label}.status is invalid")
        if final and item["status"] == "planned":
            raise LoopError(f"{label} is not verified")
        if final and item["status"] == "deviation_approved" and identifier not in approved_deviations:
            raise LoopError(f"{label} has no approved deviation")
    if seen != expected_ids:
        raise LoopError("prototype contract items do not match the authorized prototype item IDs")
    return seen


def validate_matrix(root: Path, matrix: Mapping[str, Any], manifest: Mapping[str, Any], prototype_ids: set[str], final: bool) -> tuple[set[str], str | None]:
    validate_version(matrix, "verification-matrix", "verification matrix")
    if matrix.get("change_id") != manifest["change_id"] or matrix.get("source_digest") != manifest["source_digest"]:
        raise LoopError("verification matrix authority binding mismatch")
    rows = matrix.get("rows")
    defects = matrix.get("defects")
    if not isinstance(rows, list) or not rows:
        raise LoopError("verification matrix needs at least one row")
    if not isinstance(defects, list):
        raise LoopError("verification matrix defects must be a list")
    allowed_requirements = set(manifest["requirement_ids"])
    allowed_acceptance = set(manifest["acceptance_ids"])
    covered_requirements: set[str] = set()
    covered_acceptance: set[str] = set()
    covered_prototype: set[str] = set()
    acceptance_owners: dict[str, str] = {}
    prototype_owners: dict[str, str] = {}
    row_ids: set[str] = set()
    evidence_ids: set[str] = set()
    subject_commit = matrix.get("subject_commit")
    if final and (not isinstance(subject_commit, str) or not commit_exists(root, subject_commit)):
        raise LoopError("final verification matrix needs an existing subject_commit")
    for index, row in enumerate(rows):
        label = f"verification rows[{index}]"
        if not isinstance(row, dict):
            raise LoopError(f"{label} must be an object")
        require_fields(
            row,
            {"id", "requirement_ids", "acceptance_ids", "prototype_item_ids", "task_refs", "code_refs", "tests", "evidence", "commit", "environment", "independent_review", "status"},
            label,
        )
        identifier = row["id"]
        if not isinstance(identifier, str) or not STABLE_ID.fullmatch(identifier) or identifier in row_ids:
            raise LoopError(f"{label}.id is invalid or duplicated")
        row_ids.add(identifier)
        requirements = set(string_list(row["requirement_ids"], f"{label}.requirement_ids", nonempty=final, pattern=STABLE_ID))
        acceptance = set(string_list(row["acceptance_ids"], f"{label}.acceptance_ids", nonempty=True, pattern=STABLE_ID))
        prototypes = set(string_list(row["prototype_item_ids"], f"{label}.prototype_item_ids", pattern=STABLE_ID))
        if not requirements <= allowed_requirements or not acceptance <= allowed_acceptance or not prototypes <= prototype_ids:
            raise LoopError(f"{label} references IDs outside the execution authority")
        for acceptance_id in acceptance:
            previous = acceptance_owners.get(acceptance_id)
            if previous is not None:
                raise LoopError(
                    f"Acceptance ID {acceptance_id} is covered by both {previous} and {identifier}; "
                    "combine many-to-one coverage in one row"
                )
            acceptance_owners[acceptance_id] = identifier
        for prototype_id in prototypes:
            previous = prototype_owners.get(prototype_id)
            if previous is not None:
                raise LoopError(
                    f"prototype item {prototype_id} is covered by both {previous} and {identifier}; "
                    "keep one verification owner"
                )
            prototype_owners[prototype_id] = identifier
        covered_requirements.update(requirements)
        covered_acceptance.update(acceptance)
        covered_prototype.update(prototypes)
        task_refs = string_list(row["task_refs"], f"{label}.task_refs", nonempty=final)
        code_refs = string_list(row["code_refs"], f"{label}.code_refs", nonempty=final)
        for code_index, raw_path in enumerate(code_refs):
            checked_path(root, raw_path, f"{label}.code_refs[{code_index}]")
        evidence = row["evidence"]
        if not isinstance(evidence, list):
            raise LoopError(f"{label}.evidence must be a list")
        row_evidence: set[str] = set()
        for evidence_index, entry in enumerate(evidence):
            evidence_id, _ = _evidence(root, entry, f"{label}.evidence[{evidence_index}]")
            if evidence_id in evidence_ids:
                raise LoopError(f"evidence ID {evidence_id} is duplicated")
            evidence_ids.add(evidence_id)
            row_evidence.add(evidence_id)
        tests = row["tests"]
        if not isinstance(tests, list) or (final and not tests):
            raise LoopError(f"{label}.tests must be a non-empty list for final validation")
        test_ids: set[str] = set()
        for test_index, test in enumerate(tests):
            test_label = f"{label}.tests[{test_index}]"
            if not isinstance(test, dict):
                raise LoopError(f"{test_label} must be an object")
            require_fields(test, {"id", "level", "command", "status", "evidence_refs"}, test_label)
            if not isinstance(test["id"], str) or not STABLE_ID.fullmatch(test["id"]) or test["id"] in test_ids:
                raise LoopError(f"{test_label}.id is invalid or duplicated")
            test_ids.add(test["id"])
            if test["level"] not in TEST_LEVELS or test["status"] not in TEST_STATUSES:
                raise LoopError(f"{test_label} level or status is invalid")
            if not isinstance(test["command"], str) or not test["command"].strip():
                raise LoopError(f"{test_label}.command is required")
            refs = set(string_list(test["evidence_refs"], f"{test_label}.evidence_refs", nonempty=final, pattern=STABLE_ID))
            if not refs <= row_evidence:
                raise LoopError(f"{test_label} references unknown evidence")
            if final and test["status"] != "passed":
                raise LoopError(f"{test_label} is not passed")
        if row["status"] not in ROW_STATUSES:
            raise LoopError(f"{label}.status is invalid")
        if final and row["status"] != "integrated":
            raise LoopError(f"{label} is not integrated")
        if final and row["commit"] != subject_commit:
            raise LoopError(f"{label}.commit does not match subject_commit")
        if final and (not isinstance(row["environment"], str) or not row["environment"].strip()):
            raise LoopError(f"{label}.environment is required")
        review = row["independent_review"]
        if not isinstance(review, dict):
            raise LoopError(f"{label}.independent_review must be an object")
        require_fields(review, {"status", "reviewer", "evidence_refs"}, f"{label}.independent_review")
        review_refs = set(string_list(review["evidence_refs"], f"{label}.independent_review.evidence_refs", nonempty=final, pattern=STABLE_ID))
        if not review_refs <= row_evidence:
            raise LoopError(f"{label}.independent_review references unknown evidence")
        if final and (review["status"] != "passed" or not isinstance(review["reviewer"], str) or not review["reviewer"].strip()):
            raise LoopError(f"{label} has no passed independent review")
    if covered_acceptance != allowed_acceptance:
        raise LoopError("verification matrix does not exactly cover authorized Acceptance IDs")
    if final and covered_requirements != allowed_requirements:
        raise LoopError("verification matrix does not cover every authorized Requirement ID")
    if final and covered_prototype != prototype_ids:
        raise LoopError("verification matrix does not cover every prototype item")
    defect_ids: set[str] = set()
    for index, defect in enumerate(defects):
        label = f"defects[{index}]"
        if not isinstance(defect, dict):
            raise LoopError(f"{label} must be an object")
        require_fields(defect, {"id", "severity", "status", "acceptance_ids", "task_refs", "evidence_refs"}, label)
        if not isinstance(defect["id"], str) or not STABLE_ID.fullmatch(defect["id"]) or defect["id"] in defect_ids:
            raise LoopError(f"{label}.id is invalid or duplicated")
        defect_ids.add(defect["id"])
        if defect["severity"] not in DEFECT_SEVERITIES or defect["status"] not in DEFECT_STATUSES:
            raise LoopError(f"{label} severity or status is invalid")
        if not set(string_list(defect["acceptance_ids"], f"{label}.acceptance_ids", nonempty=True, pattern=STABLE_ID)) <= allowed_acceptance:
            raise LoopError(f"{label} references unknown Acceptance IDs")
        string_list(defect["task_refs"], f"{label}.task_refs", nonempty=defect["status"] == "open")
        string_list(defect["evidence_refs"], f"{label}.evidence_refs", pattern=STABLE_ID)
        if final and defect["severity"] in {"critical", "high"} and defect["status"] != "verified":
            raise LoopError(f"{label} is an unresolved critical/high defect")
    return evidence_ids, subject_commit if isinstance(subject_commit, str) else None


def attempt_progress(attempt: Mapping[str, Any]) -> bool:
    progress = attempt.get("progress")
    if not isinstance(progress, dict):
        return False
    return bool(
        attempt.get("outcome") == "passed"
        or (isinstance(progress.get("new_passes"), int) and progress["new_passes"] > 0)
        or (isinstance(progress.get("removed_failures"), int) and progress["removed_failures"] > 0)
        or progress.get("meaningful_diff") is True
        or progress.get("state_advance") is True
    )


def validate_attempt(value: Mapping[str, Any], change_id: str, *, recorded: bool) -> None:
    validate_version(value, "execution-attempt", "execution attempt")
    require_fields(
        value,
        {"change_id", "run_id", "objective", "oracle", "starting_commit", "ending_commit", "changed_digest", "checks", "failure_fingerprint", "progress", "plan_changed", "outcome", "next_action"},
        "execution attempt",
    )
    if value["change_id"] != change_id:
        raise LoopError("execution attempt change_id mismatch")
    if not isinstance(value["run_id"], str) or not RUN_ID.fullmatch(value["run_id"]):
        raise LoopError("execution attempt run_id is invalid")
    for field in ("objective", "oracle", "next_action"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise LoopError(f"execution attempt {field} is required")
    for field in ("starting_commit", "ending_commit"):
        if not isinstance(value[field], str) or not GIT_OID.fullmatch(value[field]):
            raise LoopError(f"execution attempt {field} is invalid")
    if not isinstance(value["changed_digest"], str) or not HEX64.fullmatch(value["changed_digest"]):
        raise LoopError("execution attempt changed_digest is invalid")
    if value["outcome"] not in ATTEMPT_OUTCOMES:
        raise LoopError("execution attempt outcome is invalid")
    if value["outcome"] == "failed" and (not isinstance(value["failure_fingerprint"], str) or not value["failure_fingerprint"].strip()):
        raise LoopError("failed attempt needs a failure_fingerprint")
    checks = value["checks"]
    if not isinstance(checks, list) or not checks:
        raise LoopError("execution attempt needs at least one check")
    for index, check in enumerate(checks):
        if not isinstance(check, dict) or not isinstance(check.get("id"), str) or not check["id"].strip() or check.get("status") not in CHECK_STATUSES:
            raise LoopError(f"execution attempt checks[{index}] is invalid")
    progress = value["progress"]
    if not isinstance(progress, dict):
        raise LoopError("execution attempt progress must be an object")
    require_fields(progress, {"new_passes", "removed_failures", "meaningful_diff", "state_advance"}, "execution attempt progress")
    if not isinstance(progress["new_passes"], int) or progress["new_passes"] < 0 or not isinstance(progress["removed_failures"], int) or progress["removed_failures"] < 0:
        raise LoopError("execution attempt progress counts must be non-negative integers")
    if not isinstance(progress["meaningful_diff"], bool) or not isinstance(progress["state_advance"], bool):
        raise LoopError("execution attempt progress flags must be booleans")
    if not isinstance(value["plan_changed"], bool):
        raise LoopError("execution attempt plan_changed must be boolean")
    if recorded:
        if not isinstance(value.get("round"), int) or value["round"] < 1 or not isinstance(value.get("recorded_at"), str):
            raise LoopError("recorded execution attempt needs round and recorded_at")
        if value.get("effective_progress") is not attempt_progress(value):
            raise LoopError("recorded execution attempt effective_progress mismatch")


def attempt_stop(attempts: Sequence[Mapping[str, Any]], budget: Mapping[str, int]) -> dict[str, Any]:
    if not attempts:
        return {"stop": False, "reasons": [], "attempt_count": 0, "same_failure_count": 0, "no_progress_count": 0}
    last = attempts[-1]
    same_failure = 0
    fingerprint = last.get("failure_fingerprint")
    if isinstance(fingerprint, str) and fingerprint:
        for attempt in reversed(attempts):
            if attempt.get("failure_fingerprint") != fingerprint:
                break
            same_failure += 1
    no_progress = 0
    for attempt in reversed(attempts):
        if attempt.get("effective_progress") is True:
            break
        no_progress += 1
    reasons: list[str] = []
    if last.get("outcome") != "passed":
        if len(attempts) >= budget["max_attempts"]:
            reasons.append("max-attempts")
        if same_failure >= budget["max_same_failure"]:
            reasons.append("same-failure-limit")
        if no_progress >= budget["max_no_progress_attempts"]:
            reasons.append("no-progress-limit")
    return {
        "stop": bool(reasons),
        "reasons": reasons,
        "attempt_count": len(attempts),
        "same_failure_count": same_failure,
        "no_progress_count": no_progress,
        "last_outcome": last.get("outcome"),
    }


def load_execution(root: Path, change_id: str) -> tuple[dict[str, Path], dict[str, Any], list[dict[str, Any]]]:
    paths = execution_paths(root, change_id)
    manifest = read_json(paths["manifest"], "execution manifest")
    validate_manifest(manifest, change_id)
    attempts = read_jsonl(paths["attempts"], "attempt ledger")
    for index, attempt in enumerate(attempts, start=1):
        validate_attempt(attempt, change_id, recorded=True)
        if attempt["round"] != index:
            raise LoopError("attempt rounds must be contiguous and start at 1")
    return paths, manifest, attempts


def command_record(args: argparse.Namespace) -> dict[str, Any]:
    root = project_root(args.project_root)
    paths, manifest, attempts = load_execution(root, args.change_id)
    prior = attempt_stop(attempts, manifest["repair_budget"])
    if prior["stop"]:
        raise LoopError(f"repair loop is already stopped: {prior['reasons']}")
    attempt_path = checked_path(root, args.attempt_file, "attempt input")
    attempt = read_json(attempt_path, "attempt input")
    validate_attempt(attempt, args.change_id, recorded=False)
    attempt["round"] = len(attempts) + 1
    attempt["recorded_at"] = utc_now()
    attempt["effective_progress"] = attempt_progress(attempt)
    attempts.append(attempt)
    content = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for item in attempts)
    atomic_rewrite(paths["attempts"], content)
    status = attempt_stop(attempts, manifest["repair_budget"])
    return {"recorded": True, "change_id": args.change_id, "round": attempt["round"], **status}


def validate_scorecard(root: Path, scorecard: Mapping[str, Any], manifest: Mapping[str, Any], evidence_ids: set[str], subject_commit: str | None, final: bool) -> int:
    validate_version(scorecard, "quality-scorecard", "quality scorecard")
    if scorecard.get("change_id") != manifest["change_id"]:
        raise LoopError("quality scorecard change_id mismatch")
    if final and scorecard.get("subject_commit") != subject_commit:
        raise LoopError("quality scorecard subject_commit does not match verification matrix")
    if final and not commit_exists(root, scorecard.get("subject_commit")):
        raise LoopError("quality scorecard subject_commit does not exist")
    gates = scorecard.get("hard_gates")
    if not isinstance(gates, dict) or set(gates) != HARD_GATES or any(not isinstance(value, bool) for value in gates.values()):
        raise LoopError("quality scorecard hard_gates are incomplete or invalid")
    if final and not all(gates.values()):
        raise LoopError("all quality hard gates must pass")
    dimensions = scorecard.get("dimensions")
    if not isinstance(dimensions, list) or len(dimensions) != len(QUALITY_DEFAULTS):
        raise LoopError("quality scorecard dimensions are incomplete")
    seen: set[str] = set()
    total_weight = 0
    total_score = 0
    redistributed = False
    for index, dimension in enumerate(dimensions):
        label = f"quality dimensions[{index}]"
        if not isinstance(dimension, dict):
            raise LoopError(f"{label} must be an object")
        require_fields(dimension, {"id", "weight", "minimum", "score", "evidence_refs"}, label)
        identifier = dimension["id"]
        if identifier not in QUALITY_DEFAULTS or identifier in seen:
            raise LoopError(f"{label}.id is unknown or duplicated")
        seen.add(identifier)
        weight, minimum, score = dimension["weight"], dimension["minimum"], dimension["score"]
        if not all(isinstance(item, int) for item in (weight, minimum, score)) or weight <= 0 or minimum < 0 or score < 0 or minimum > weight or score > weight:
            raise LoopError(f"{label} weight/minimum/score is invalid")
        if (weight, minimum) != QUALITY_DEFAULTS[identifier]:
            redistributed = True
            floor = math.ceil(weight * (0.9 if identifier in {"requirements-and-functionality", "security-data-and-reliability"} else 0.8))
            if minimum < floor:
                raise LoopError(f"{label}.minimum is below the allowed redistributed floor")
        refs = set(string_list(dimension["evidence_refs"], f"{label}.evidence_refs", nonempty=final, pattern=STABLE_ID))
        if not refs <= evidence_ids:
            raise LoopError(f"{label} references unknown evidence")
        if final and score < minimum:
            raise LoopError(f"{label} is below its minimum")
        total_weight += weight
        total_score += score
    if seen != set(QUALITY_DEFAULTS) or total_weight != 100:
        raise LoopError("quality dimensions must contain all IDs and total weight 100")
    if redistributed and (not isinstance(scorecard.get("redistribution_approval_ref"), str) or not scorecard["redistribution_approval_ref"].strip()):
        raise LoopError("quality weight redistribution needs an approval reference")
    if scorecard.get("total_score") != total_score:
        raise LoopError("quality scorecard total_score does not equal the dimension sum")
    if final and total_score < 90:
        raise LoopError("quality score must be at least 90")
    evaluator = scorecard.get("independent_evaluator")
    if not isinstance(evaluator, dict):
        raise LoopError("quality scorecard independent_evaluator must be an object")
    if final:
        if not isinstance(evaluator.get("identity"), str) or not evaluator["identity"].strip():
            raise LoopError("final quality scorecard needs an independent evaluator identity")
        report_id, _ = _evidence(
            root,
            {"id": "EVID-EVALUATOR", "kind": "independent-evaluation", "path": evaluator.get("report_path"), "sha256": evaluator.get("report_sha256")},
            "independent evaluator report",
        )
        del report_id
        if scorecard.get("status") != "passed":
            raise LoopError("final quality scorecard status must be passed")
    elif scorecard.get("status") not in {"draft", "passed", "failed"}:
        raise LoopError("quality scorecard status is invalid")
    return total_score


def command_validate(args: argparse.Namespace) -> dict[str, Any]:
    root = project_root(args.project_root)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        paths, manifest, attempts = load_execution(root, args.change_id)
        if not commit_exists(root, manifest["starting_commit"]):
            raise LoopError("execution starting_commit does not exist")
        for index, binding in enumerate(manifest["source_bindings"]):
            path = checked_path(root, binding["path"], f"source_bindings[{index}]")
            if file_digest(path) != binding["sha256"]:
                raise LoopError(f"authorized source changed after execution authorization: {binding['path']}")
        if manifest["path"] == "enhanced":
            seal = manifest.get("planning_seal")
            if not isinstance(seal, dict):
                raise LoopError("enhanced execution needs a Planning Seal binding")
            seal_path = checked_path(root, seal.get("path"), "bound Planning Seal")
            if file_digest(seal_path) != seal.get("sha256"):
                raise LoopError("bound Planning Seal changed after execution authorization")
        prototype_ids = validate_prototype(root, manifest, args.final)
        matrix = read_json(paths["matrix"], "verification matrix")
        evidence_ids, subject = validate_matrix(root, matrix, manifest, prototype_ids, args.final)
        scorecard = read_json(paths["scorecard"], "quality scorecard")
        score = validate_scorecard(root, scorecard, manifest, evidence_ids, subject, args.final)
        stop = attempt_stop(attempts, manifest["repair_budget"])
        if args.final:
            if not attempts:
                raise LoopError("final validation needs at least one recorded execution attempt")
            if attempts[-1].get("outcome") != "passed":
                raise LoopError("the last execution attempt is not passed")
            if stop["stop"]:
                raise LoopError(f"repair loop stopped without a passing result: {stop['reasons']}")
            if manifest["status"] != "complete":
                raise LoopError("final execution manifest status must be complete")
        elif not attempts:
            warnings.append("no execution attempt has been recorded")
        return {
            "valid": True,
            "acceptance_ready": bool(args.final),
            "change_id": args.change_id,
            "quality_score": score,
            "attempts": stop,
            "warnings": warnings,
            "errors": errors,
        }
    except LoopError as exc:
        errors.append(str(exc))
        return {
            "valid": False,
            "acceptance_ready": False,
            "change_id": args.change_id,
            "quality_score": None,
            "attempts": None,
            "warnings": warnings,
            "errors": errors,
        }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="loopctl", description="Validate the Qoder-native delivery loop")
    subparsers = root.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init-execution", help="create a non-overwriting Stage B execution bundle")
    init.add_argument("--project-root", required=True)
    init.add_argument("--change-id", required=True)
    init.add_argument("--route", choices=("greenfield", "brownfield", "resume"), required=True)
    init.add_argument("--path", choices=("quick", "enhanced"), required=True)
    init.add_argument("--source-digest")
    init.add_argument("--source", action="append", default=[])
    init.add_argument("--approval-ref", required=True)
    init.add_argument("--requirement", action="append", default=[])
    init.add_argument("--acceptance", action="append", default=[])
    init.add_argument("--scope", action="append", default=[])
    init.add_argument("--non-goal", action="append", default=[])
    init.add_argument("--environment", choices=("local", "worktree"), default="local")
    init.add_argument("--integration-owner", required=True)
    init.add_argument("--prototype-contract")
    init.add_argument("--prototype-required", action="store_true")
    init.add_argument("--prototype-not-applicable-reason")
    init.add_argument("--max-attempts", type=int, default=3)
    init.add_argument("--max-same-failure", type=int, default=2)
    init.add_argument("--max-no-progress", type=int, default=2)

    record = subparsers.add_parser("record-attempt", help="append one validated execution attempt")
    record.add_argument("--project-root", required=True)
    record.add_argument("--change-id", required=True)
    record.add_argument("--attempt-file", required=True)

    validate = subparsers.add_parser("validate", help="validate Stage B authority, trace, evidence, attempts, and quality")
    validate.add_argument("--project-root", required=True)
    validate.add_argument("--change-id", required=True)
    validate.add_argument("--final", action="store_true")
    return root


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "init-execution":
            result = command_init(arguments)
        elif arguments.command == "record-attempt":
            result = command_record(arguments)
        else:
            result = command_validate(arguments)
        sys.stdout.write(json_text(result))
        return 0 if result.get("valid", True) else 1
    except LoopError as exc:
        sys.stderr.write(f"loopctl: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
