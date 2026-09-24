#!/usr/bin/env python3
"""Regression tests for read-only M3 external binding collection."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import planning_bindings as bindings
import planning
import test_planning


def _symlinks_supported() -> bool:
    if os.name != "nt":
        return True
    probe_root = Path(tempfile.mkdtemp(prefix="delivery-symlink-probe-"))
    try:
        target = probe_root / "target"
        target.write_text("x", encoding="utf-8", newline="")
        os.symlink(target, probe_root / "link")
        return True
    except OSError:
        return False
    finally:
        shutil.rmtree(probe_root, ignore_errors=True)


SYMLINKS_SUPPORTED = _symlinks_supported()
SYSTEM_OPENSPEC = shutil.which("openspec")


class PlanningBindingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="delivery-planning-bindings-test-")
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.source_path = "delivery-docs/product/source/system-spec.md"
        self.source_text = "# Login system\n\nA visitor can create an authenticated session.\n"
        self.write(self.source_path, self.source_text)
        self.source_digest = hashlib.sha256(self.source_text.encode("utf-8")).hexdigest()
        self.write_authorities(["add-login"])
        self.write("openspec/config.yaml", "schema: spec-driven\n")
        self.materialize_change("add-login")
        self.cli = self.make_cli()
        self.initialize_git()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
        return path

    def write_json(self, relative: str, value: object) -> Path:
        return self.write(relative, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def write_authorities(self, change_ids: list[str], *, requirements_path: str = "delivery-docs/product/requirements.json") -> None:
        requirements = {
            "schema_version": 1,
            "artifact_type": "requirements",
            "requirements": [
                {
                    "id": "REQ-LOGIN",
                    "revision": 1,
                    "title": "Login",
                    "statement": "A visitor can create an authenticated session.",
                    "kind": "functional",
                    "priority": "P0",
                    "status": "confirmed",
                    "scope": "in_scope",
                    "scope_reason": None,
                    "source_refs": [
                        {
                            "path": self.source_path,
                            "sha256": self.source_digest,
                            "locator": "login",
                        }
                    ],
                    "depends_on": [],
                    "decision_refs": [],
                }
            ],
        }
        acceptance = {
            "schema_version": 1,
            "artifact_type": "acceptance-catalog",
            "acceptance_criteria": [
                {
                    "id": "AC-LOGIN",
                    "requirement_ids": ["REQ-LOGIN"],
                }
            ],
        }
        features = {
            "schema_version": 1,
            "artifact_type": "feature-ledger",
            "features": [],
        }
        roadmap = {
            "schema_version": 1,
            "artifact_type": "delivery-roadmap",
            "project_mode": "greenfield",
            "milestones": [],
            "slices": [
                {
                    "id": f"SLICE-{index + 1}",
                    "change_id": change_id,
                    "requirement_ids": ["REQ-LOGIN"],
                    "acceptance_ids": ["AC-LOGIN"],
                    "openspec_state": "materialized" if (self.root / "openspec/changes" / change_id).is_dir() else "reserved",
                }
                for index, change_id in enumerate(change_ids)
            ],
            "next_ready_slice_id": "SLICE-1" if change_ids else None,
            "blocking_questions": [],
            "pending_decisions": [],
        }
        self.write_json(requirements_path, requirements)
        self.write_json("delivery-docs/product/acceptance.json", acceptance)
        self.write_json("delivery-docs/product/feature-ledger.json", features)
        self.write_json("delivery-docs/plans/delivery-roadmap.json", roadmap)
        (self.root / "delivery-docs/state/work-items").mkdir(parents=True, exist_ok=True)

    def materialize_change(
        self,
        change_id: str,
        *,
        checked: bool = False,
        requirement_id: str = "REQ-LOGIN",
        acceptance_id: str = "AC-LOGIN",
    ) -> None:
        self.write(f"openspec/changes/{change_id}/.openspec.yaml", "schema: spec-driven\n")
        self.write(
            f"openspec/changes/{change_id}/proposal.md",
            f"# Proposal\n\nTrace: {requirement_id}, {acceptance_id}\n",
        )
        self.write(
            f"openspec/changes/{change_id}/design.md",
            f"# Design\n\nTrace: {requirement_id}, {acceptance_id}\n",
        )
        self.write(
            f"openspec/changes/{change_id}/specs/session/spec.md",
            "## ADDED Requirements\n\n"
            f"### Requirement: Login [{requirement_id}]\n\n"
            f"#### Scenario: Authenticated [{acceptance_id}]\n"
            "- **WHEN** valid credentials are submitted\n"
            "- **THEN** an authenticated session is observable\n",
        )
        marker = "x" if checked else " "
        self.write(
            f"openspec/changes/{change_id}/tasks.md",
            f"- [{marker}] 1.1 Implement trace {requirement_id} {acceptance_id}\n",
        )

    def replace_default_change(self, change_id: str) -> None:
        change_root = self.root / "openspec/changes/add-login"
        for path in sorted(change_root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            else:
                path.rmdir()
        change_root.rmdir()
        self.materialize_change(change_id)
        self.write_authorities([change_id])

    def make_cli(self) -> Path:
        body = textwrap.dedent(
            """
                import json
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                if args == ["--version"]:
                    print("1.6.0")
                    raise SystemExit(0)
                if len(args) == 4 and args[0] == "status" and args[1] == "--change" and args[3] == "--json":
                    change = args[2]
                    complete = change != "status-incomplete"
                    schema = "custom-review" if change == "custom-review" else "spec-driven"
                    outputs = [
                        ("proposal", "proposal.md"),
                        ("specs", "specs/**/*.md"),
                        ("design", "design.md"),
                    ]
                    if change == "custom-review":
                        outputs.append(("review", "review.md"))
                    outputs.append(("tasks", "tasks.md"))
                    change_dir = (Path.cwd() / "openspec" / "changes" / change).resolve()
                    artifact_paths = {}
                    for artifact, output in outputs:
                        existing = sorted(str(path.resolve()) for path in change_dir.glob(output))
                        artifact_paths[artifact] = {
                            "outputPath": output,
                            "resolvedOutputPath": str(change_dir / output),
                            "existingOutputPaths": existing,
                        }
                    print(json.dumps({
                        "changeName": change,
                        "schemaName": schema,
                        "changeRoot": str(change_dir),
                        "artifactPaths": artifact_paths,
                        "isComplete": complete,
                        "applyRequires": ["tasks"],
                        "artifacts": [
                            {
                                "id": artifact,
                                "outputPath": output,
                                "status": "done" if complete or artifact != "proposal" else "ready",
                            }
                            for artifact, output in outputs
                        ],
                    }))
                    raise SystemExit(0)
                if len(args) == 5 and args[0] == "instructions" and args[2] == "--change" and args[4] == "--json":
                    artifact = args[1]
                    change = args[3]
                    schema = "custom-review" if change == "custom-review" else "spec-driven"
                    change_dir = (Path.cwd() / "openspec" / "changes" / change).resolve()
                    outputs = {
                        "proposal": "proposal.md",
                        "specs": "specs/**/*.md",
                        "design": "design.md",
                        "review": "review.md",
                        "tasks": "tasks.md",
                    }
                    requires = {
                        "proposal": [],
                        "specs": ["proposal"],
                        "design": ["proposal"],
                        "review": ["design"],
                        "tasks": ["specs", "design"] + (["review"] if change == "custom-review" else []),
                    }
                    unlocks = {
                        "proposal": ["specs", "design"],
                        "specs": ["tasks"],
                        "design": ["review", "tasks"] if change == "custom-review" else ["tasks"],
                        "review": ["tasks"],
                        "tasks": [],
                    }
                    print(json.dumps({
                        "changeName": change,
                        "artifactId": artifact,
                        "schemaName": schema,
                        "changeDir": str(change_dir),
                        "outputPath": outputs[artifact],
                        "resolvedOutputPath": (
                            "/outside/change/review.md"
                            if change == "instructions-path-escape" and artifact == "proposal"
                            else str(change_dir / outputs[artifact])
                        ),
                        "existingOutputPaths": (
                            []
                            if change == "instructions-path-drift" and artifact == "proposal"
                            else sorted(str(path.resolve()) for path in change_dir.glob(outputs[artifact]))
                        ),
                        "description": (
                            None
                            if change == "instructions-shape" and artifact == "proposal"
                            else f"Description for {artifact}"
                        ),
                        "instruction": f"Create {artifact} with the approved trace.",
                        "template": f"# {artifact.title()} template",
                        "dependencies": [
                            {
                                "id": dependency,
                                "done": True,
                                "path": outputs[dependency],
                                "description": f"Description for {dependency}",
                            }
                            for dependency in requires[artifact]
                        ],
                        "unlocks": unlocks[artifact],
                    }))
                    raise SystemExit(0)
                if len(args) == 5 and args[0] == "validate" and args[2:] == ["--strict", "--json", "--no-interactive"]:
                    change = args[1]
                    valid = change != "strict-fail"
                    print(json.dumps({
                        "items": [{"id": change, "type": "change", "valid": valid, "issues": []}],
                        "summary": {"totals": {"items": 1, "passed": 1 if valid else 0, "failed": 0 if valid else 1}},
                        "version": "1.0",
                    }))
                    raise SystemExit(0 if valid else 1)
                print(json.dumps({"error": "unexpected arguments"}))
                raise SystemExit(2)
                """
        )
        if os.name == "nt":
            # Windows cannot CreateProcess a bare script; a .cmd launcher
            # forwards arguments to the Python payload instead.
            payload = self.bin / "openspec-payload.py"
            payload.write_text(body, encoding="utf-8", newline="")
            script = self.bin / "openspec.cmd"
            script.write_text(
                f'@echo off\r\n"{sys.executable}" "{payload}" %*\r\n',
                encoding="utf-8", newline="",
            )
        else:
            script = self.bin / "openspec"
            script.write_text(
                "#!/usr/bin/env python3\n" + body,
                encoding="utf-8", newline="",
            )
            script.chmod(0o755)
        return script.resolve()

    def git(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return completed.stdout.strip()

    def initialize_git(self) -> None:
        self.git("init", "-q")
        self.git("config", "user.email", "binding-tests@example.invalid")
        self.git("config", "user.name", "Binding Tests")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "baseline")
        self.baseline = self.git("rev-parse", "HEAD")

    def collect(self, **overrides: object) -> dict[str, object]:
        arguments: dict[str, object] = {
            "project_mode": "greenfield",
            "baseline": self.baseline,
            "approved_decisions": [],
            "pending_decisions": [],
            "blocking_decisions": [],
            "openspec_executable": self.cli,
        }
        arguments.update(overrides)
        return bindings.collect_external_bindings(self.root, **arguments)

    def assert_error(self, code: str, **overrides: object) -> bindings.PlanningBindingsError:
        with self.assertRaises(bindings.PlanningBindingsError) as caught:
            self.collect(**overrides)
        self.assertEqual(code, caught.exception.code)
        serialized = json.dumps(caught.exception.to_dict(), ensure_ascii=False)
        self.assertNotIn(os.fspath(self.base), serialized)
        self.assertNotIn(self.source_text, serialized)
        return caught.exception

    def inventory(self) -> dict[str, tuple[object, ...]]:
        result: dict[str, tuple[object, ...]] = {}
        for path in sorted(self.root.rglob("*")):
            relative = path.relative_to(self.root).as_posix()
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                result[relative] = ("link", os.readlink(path))
            elif stat.S_ISDIR(metadata.st_mode):
                result[relative] = ("directory",)
            elif stat.S_ISREG(metadata.st_mode):
                result[relative] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
            else:
                result[relative] = ("special", stat.S_IFMT(metadata.st_mode))
        return result

    def test_valid_collection_is_path_free_read_only_and_normalized(self) -> None:
        before = self.inventory()
        result = self.collect()
        after = self.inventory()

        self.assertEqual(before, after)
        external = result["external_bindings"]
        git_binding = external["git"]
        change = external["openspec"]["changes"]["add-login"]
        expected_exclusions = sorted(
            [
                "delivery-docs/state",
                "delivery-docs/product/requirements.json",
                "delivery-docs/product/acceptance.json",
                "delivery-docs/product/feature-ledger.json",
                "delivery-docs/plans/delivery-roadmap.json",
                "openspec",
            ]
        )

        self.assertEqual(1, external["schema_version"])
        self.assertEqual(self.baseline, git_binding["head"])
        self.assertEqual(self.baseline, git_binding["baseline"])
        self.assertEqual(expected_exclusions, result["diagnostics"]["git"]["excluded_paths"])
        self.assertEqual(
            bindings._canonical_digest(expected_exclusions),
            git_binding["excluded_paths_digest"],
        )
        self.assertEqual(self.source_digest, external["registered_sources"][self.source_path])
        self.assertEqual("spec-driven", external["openspec"]["schema"])
        self.assertEqual("1.6.0", external["openspec"]["cli_version"])
        self.assertEqual("materialized", change["state"])
        self.assertTrue(change["metadata_valid"])
        self.assertTrue(change["strict_valid"])
        self.assertFalse(change["tasks_checked"])
        self.assertEqual(["REQ-LOGIN"], change["requirement_ids"])
        self.assertEqual(["AC-LOGIN"], change["acceptance_ids"])
        self.assertEqual(
            [{"acceptance_id": "AC-LOGIN", "requirement_id": "REQ-LOGIN"}],
            change["acceptance_requirement_edges"],
        )
        self.assertRegex(change["runtime_artifacts_digest"], r"^[0-9a-f]{64}$")
        self.assertRegex(change["runtime_instructions_digest"], r"^[0-9a-f]{64}$")
        self.assertRegex(change["artifact_digest"], r"^[0-9a-f]{64}$")
        self.assertFalse(result["diagnostics"]["writes_performed"])

        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(os.fspath(self.base), serialized)
        self.assertNotIn(self.source_text, serialized)

    def test_existing_change_is_materialized_for_both_readiness_modes(self) -> None:
        change_ready = self.collect(readiness="change_ready")
        seal = self.collect(readiness="seal")
        self.assertEqual(
            "materialized",
            change_ready["external_bindings"]["openspec"]["changes"]["add-login"]["state"],
        )
        self.assertEqual(
            change_ready["external_bindings"]["openspec"]["changes"],
            seal["external_bindings"]["openspec"]["changes"],
        )

    def test_orphan_is_not_suppressed_and_checked_task_is_reported_as_fact(self) -> None:
        self.materialize_change("orphan-change", requirement_id="REQ-ORPHAN", acceptance_id="AC-ORPHAN")
        tasks = self.root / "openspec/changes/add-login/tasks.md"
        tasks.write_text(
            "- [x] 1.1 Completed too early REQ-LOGIN AC-LOGIN\n"
            "- [ ] 1.2 Still planned REQ-LOGIN AC-LOGIN\n",
            encoding="utf-8", newline="",
        )

        result = self.collect()
        changes = result["external_bindings"]["openspec"]["changes"]
        self.assertEqual({"add-login", "orphan-change"}, set(changes))
        self.assertEqual("materialized", changes["orphan-change"]["state"])
        self.assertTrue(changes["add-login"]["tasks_checked"])

    def test_nested_checked_task_cannot_bypass_m3_lock(self) -> None:
        self.write(
            "openspec/changes/add-login/tasks.md",
            "- [ ] 1.1 Implement REQ-LOGIN and verify AC-LOGIN\n"
            "  - [x] A nested task was already executed\n",
        )

        change = self.collect()["external_bindings"]["openspec"]["changes"]["add-login"]

        self.assertTrue(change["tasks_checked"])

    def test_crlf_tasks_are_parsed_without_platform_drift(self) -> None:
        tasks = self.root / "openspec/changes/add-login/tasks.md"
        tasks.write_bytes(b"- [ ] 1.1 Implement REQ-LOGIN and verify AC-LOGIN\r\n")

        change = self.collect()["external_bindings"]["openspec"]["changes"]["add-login"]

        self.assertFalse(change["tasks_checked"])

    def test_source_drift_binds_actual_digest_and_remains_visible(self) -> None:
        replacement = self.source_text + "The session can also be revoked.\n"
        (self.root / self.source_path).write_text(replacement, encoding="utf-8", newline="")
        actual = hashlib.sha256(replacement.encode("utf-8")).hexdigest()

        result = self.collect()

        self.assertEqual(actual, result["external_bindings"]["registered_sources"][self.source_path])
        self.assertEqual(1, result["diagnostics"]["declared_source_digest_mismatch_count"])
        self.assertTrue(result["diagnostics"]["git"]["dirty"])

    def test_git_exclusions_are_digest_bound_while_authorities_bind_their_own_drift(self) -> None:
        first = self.collect()
        feature_path = self.root / "delivery-docs/product/feature-ledger.json"
        feature = json.loads(feature_path.read_text(encoding="utf-8"))
        feature["note"] = "independently bound planning drift"
        feature_path.write_text(json.dumps(feature, indent=2) + "\n", encoding="utf-8", newline="")
        second = self.collect()

        self.assertEqual(first["external_bindings"]["git"], second["external_bindings"]["git"])
        self.assertNotEqual(
            first["diagnostics"]["authority_digests"]["delivery-docs/product/feature-ledger.json"],
            second["diagnostics"]["authority_digests"]["delivery-docs/product/feature-ledger.json"],
        )

        alternate = "delivery-docs/product/alternate-requirements.json"
        (self.root / alternate).write_bytes((self.root / "delivery-docs/product/requirements.json").read_bytes())
        custom = self.collect(authority_paths={"requirements": alternate})
        self.assertNotEqual(
            second["external_bindings"]["git"]["excluded_paths_digest"],
            custom["external_bindings"]["git"]["excluded_paths_digest"],
        )

    def test_openspec_artifact_and_config_drift_are_not_hidden_by_git_exclusions(self) -> None:
        first = self.collect()
        proposal = self.root / "openspec/changes/add-login/proposal.md"
        proposal.write_text(proposal.read_text(encoding="utf-8") + "\nMore design context.\n", encoding="utf-8", newline="")
        second = self.collect()
        self.assertEqual(first["external_bindings"]["git"], second["external_bindings"]["git"])
        self.assertNotEqual(
            first["external_bindings"]["openspec"]["tree_digest"],
            second["external_bindings"]["openspec"]["tree_digest"],
        )
        self.assertNotEqual(
            first["external_bindings"]["openspec"]["changes"]["add-login"]["artifact_digest"],
            second["external_bindings"]["openspec"]["changes"]["add-login"]["artifact_digest"],
        )

        config = self.root / "openspec/config.yaml"
        config.write_text("schema: spec-driven\nrules: {}\n", encoding="utf-8", newline="")
        third = self.collect()
        self.assertNotEqual(
            second["external_bindings"]["openspec"]["config_digest"],
            third["external_bindings"]["openspec"]["config_digest"],
        )

    def test_invalid_roadmap_and_active_change_ids_fail_before_cli(self) -> None:
        self.write_authorities(["Bad_Change"])
        self.assert_error("planning-bindings-change-id-invalid")

    def test_invalid_active_change_id_is_rejected(self) -> None:
        self.materialize_change("Bad_Change")
        self.assert_error("planning-bindings-change-id-invalid")

    def test_config_missing_unsafe_and_cli_missing_are_structured(self) -> None:
        config = self.root / "openspec/config.yaml"
        config.unlink()
        self.assert_error("planning-bindings-openspec-config-missing")

        config.write_text("schema: spec-driven\nstore: external\n", encoding="utf-8", newline="")
        error = self.assert_error("planning-bindings-openspec-config-unsafe")
        self.assertIn("config-store-pointer-unsupported", error.details["blockers"])

        config.write_text("schema: spec-driven\n", encoding="utf-8", newline="")
        self.assert_error(
            "planning-bindings-openspec-cli-unavailable",
            openspec_executable=self.base / "missing-openspec",
        )

    def test_status_and_strict_validation_must_really_pass(self) -> None:
        self.materialize_change("status-incomplete")
        self.write_authorities(["add-login", "status-incomplete"])
        self.assert_error("planning-bindings-openspec-status-incomplete")

        # A new isolated fixture is unnecessary: remove the incomplete Change
        # and replace the roadmap with a strict-validation failure target.
        for path in sorted((self.root / "openspec/changes/status-incomplete").rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            else:
                path.rmdir()
        (self.root / "openspec/changes/status-incomplete").rmdir()
        self.materialize_change("strict-fail")
        self.write_authorities(["add-login", "strict-fail"])
        self.assert_error("planning-bindings-openspec-validation-failed")

    def test_decision_approval_requires_a_matching_accepted_document_and_binds_digest(self) -> None:
        self.assert_error(
            "planning-bindings-decision-document-missing",
            approved_decisions=["PDR-SLICE-SIZE"],
        )

        decision = self.write(
            "delivery-docs/decisions/product/PDR-SLICE-SIZE.md",
            "# PDR-SLICE-SIZE: Slice budget\n\n- Status: Accepted\n\nApprove the bounded exception.\n",
        )
        first = self.collect(approved_decisions=["PDR-SLICE-SIZE"])
        artifact = first["external_bindings"]["decisions"]["artifacts"]["PDR-SLICE-SIZE"]
        self.assertEqual("approved", artifact["status"])
        self.assertEqual(
            hashlib.sha256(decision.read_bytes()).hexdigest(),
            artifact["sha256"],
        )

        decision.write_text(
            decision.read_text(encoding="utf-8") + "Revision two remains explicitly accepted.\n",
            encoding="utf-8", newline="",
        )
        second = self.collect(approved_decisions=["PDR-SLICE-SIZE"])
        self.assertNotEqual(
            first["external_bindings"]["decisions"]["authority_digest"],
            second["external_bindings"]["decisions"]["authority_digest"],
        )

        decision.write_text(
            "# PDR-SLICE-SIZE: Slice budget\n\n- Status: Draft\n",
            encoding="utf-8", newline="",
        )
        self.assert_error(
            "planning-bindings-decision-status-mismatch",
            approved_decisions=["PDR-SLICE-SIZE"],
        )

    def test_collected_accepted_decision_is_end_to_end_planning_sealable(self) -> None:
        case = test_planning.PlanningValidationTests(
            methodName="test_valid_bundle_is_deterministic_read_only_and_sealable"
        )
        case.setUp()
        try:
            requirements_path = case.root / "delivery-docs/product/requirements.json"
            requirements = json.loads(requirements_path.read_text(encoding="utf-8"))
            requirements["requirements"][0]["decision_refs"] = ["PDR-ACCOUNT"]
            case.write_json("delivery-docs/product/requirements.json", requirements)

            roadmap_path = case.root / "delivery-docs/plans/delivery-roadmap.json"
            roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
            roadmap["slices"][0]["decision_refs"] = ["PDR-ACCOUNT"]
            case.write_json("delivery-docs/plans/delivery-roadmap.json", roadmap)

            account_contract_path = case.root / "delivery-docs/state/work-items/add-account.json"
            account_contract = json.loads(account_contract_path.read_text(encoding="utf-8"))
            account_contract["decision_refs"] = ["PDR-ACCOUNT"]
            case.write_json("delivery-docs/state/work-items/add-account.json", account_contract)
            case.write_text(
                "delivery-docs/decisions/product/PDR-ACCOUNT.md",
                "# PDR-ACCOUNT: Account scope\n\n- Status: Accepted\n\nApprove the account slice.\n",
            )
            case.write_text("openspec/config.yaml", "schema: spec-driven\n")
            case.write_text("openspec/changes/add-account/.openspec.yaml", "schema: spec-driven\n")
            case.write_text(
                "openspec/changes/add-account/proposal.md",
                "# Proposal\n\nREQ-ACCOUNT AC-ACCOUNT\n",
            )
            case.write_text(
                "openspec/changes/add-account/design.md",
                "# Design\n\nREQ-ACCOUNT AC-ACCOUNT\n",
            )
            case.write_text(
                "openspec/changes/add-account/specs/account/spec.md",
                "## ADDED Requirements\n\n"
                "### Requirement: Account creation REQ-ACCOUNT\n"
                "The system SHALL create an account for a valid visitor.\n\n"
                "#### Scenario: Account created AC-ACCOUNT\n"
                "- **WHEN** valid account data is submitted\n"
                "- **THEN** account creation is observable\n",
            )
            case.write_text(
                "openspec/changes/add-account/tasks.md",
                "- [ ] 1.1 Implement REQ-ACCOUNT and verify AC-ACCOUNT\n",
            )

            def case_git(*arguments: str) -> str:
                completed = subprocess.run(
                    ["git", *arguments],
                    cwd=case.root,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                return completed.stdout.strip()

            case_git("init", "-q")
            case_git("config", "user.email", "binding-e2e@example.invalid")
            case_git("config", "user.name", "Binding E2E")
            case_git("add", ".")
            case_git("commit", "-q", "-m", "baseline")
            baseline = case_git("rev-parse", "HEAD")

            def collect_case() -> dict[str, object]:
                return bindings.collect_external_bindings(
                    case.root,
                    project_mode="greenfield",
                    baseline=baseline,
                    approved_decisions=["PDR-ACCOUNT"],
                    pending_decisions=[],
                    blocking_decisions=[],
                    openspec_executable=self.cli,
                )

            first = collect_case()
            external = first["external_bindings"]
            for change_id in ("add-account", "add-profile-view"):
                relative = f"delivery-docs/state/work-items/{change_id}.json"
                contract_path = case.root / relative
                contract = json.loads(contract_path.read_text(encoding="utf-8"))
                contract["git_binding"] = external["git"]
                contract["integration_owner"] = external["git"]["integration_owner"]
                if change_id == "add-account":
                    artifact_digest = external["openspec"]["changes"][change_id]["artifact_digest"]
                    contract["openspec_binding"]["artifact_digest"] = artifact_digest
                case.write_json(relative, contract)

            collected = collect_case()
            report = planning.validate_planning_bundle(
                case.root,
                collected["external_bindings"],
                readiness="seal",
            )

            self.assertTrue(report["valid"], report["errors"])
            self.assertEqual("planning_ready", report["status"])
            self.assertEqual(
                "approved",
                collected["external_bindings"]["decisions"]["artifacts"]["PDR-ACCOUNT"]["status"],
            )
        finally:
            case.tearDown()

    def test_git_baseline_must_exist_be_a_commit_and_ancestor(self) -> None:
        self.assert_error("planning-bindings-baseline-invalid", baseline="f" * 40)

        tree = self.git("write-tree")
        unrelated = self.git("commit-tree", tree, "-m", "unrelated baseline")
        self.assert_error("planning-bindings-baseline-not-ancestor", baseline=unrelated)

    def test_trace_ids_must_live_in_delta_headings_and_tasks_need_unchecked_work(self) -> None:
        spec = self.root / "openspec/changes/add-login/specs/session/spec.md"
        spec.write_text(
            "## ADDED Requirements\n\n"
            "### Requirement: Login without a trace ID\n"
            "The system SHALL authenticate.\n\n"
            "#### Scenario: Success without a trace ID\n"
            "- **WHEN** credentials are valid\n"
            "- **THEN** login succeeds\n",
            encoding="utf-8", newline="",
        )
        self.assert_error("planning-bindings-openspec-trace-invalid")

        self.materialize_change("add-login")
        (self.root / "openspec/changes/add-login/tasks.md").write_text(
            "# Tasks\n\nNo executable task checkbox exists.\n",
            encoding="utf-8", newline="",
        )
        self.assert_error("planning-bindings-tasks-invalid")

    def test_runtime_required_proposal_must_cover_exact_change_trace(self) -> None:
        self.write(
            "openspec/changes/add-login/proposal.md",
            "# Proposal\n\nImplement the approved login outcome.\n",
        )
        self.assert_error("planning-bindings-openspec-trace-invalid")

    def test_runtime_required_design_must_cover_exact_change_trace(self) -> None:
        self.write(
            "openspec/changes/add-login/design.md",
            "# Design\n\nUse the existing session boundary.\n",
        )
        self.assert_error("planning-bindings-openspec-trace-invalid")

    def test_custom_runtime_required_markdown_artifact_cannot_bypass_trace(self) -> None:
        change_root = self.root / "openspec/changes/add-login"
        for path in sorted(change_root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            else:
                path.rmdir()
        change_root.rmdir()

        self.materialize_change("custom-review")
        self.write("openspec/changes/custom-review/.openspec.yaml", "schema: custom-review\n")
        self.write("openspec/config.yaml", "schema: custom-review\n")
        self.write("openspec/changes/custom-review/review.md", "# Review\n\nApproved without trace.\n")
        self.write_authorities(["custom-review"])

        self.assert_error("planning-bindings-openspec-trace-invalid")

        self.write(
            "openspec/changes/custom-review/review.md",
            "# Review\n\nREQ-LOGIN AC-LOGIN\n",
        )
        change = self.collect()["external_bindings"]["openspec"]["changes"]["custom-review"]
        self.assertRegex(change["runtime_artifacts_digest"], r"^[0-9a-f]{64}$")
        self.assertRegex(change["runtime_instructions_digest"], r"^[0-9a-f]{64}$")

    def test_runtime_instructions_shape_drift_fails_closed(self) -> None:
        change_root = self.root / "openspec/changes/add-login"
        for path in sorted(change_root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            else:
                path.rmdir()
        change_root.rmdir()

        self.materialize_change("instructions-shape")
        self.write_authorities(["instructions-shape"])

        self.assert_error("planning-bindings-openspec-instructions-invalid")

    def test_runtime_instruction_resolved_path_escape_fails_closed(self) -> None:
        self.replace_default_change("instructions-path-escape")

        self.assert_error("planning-bindings-openspec-instructions-invalid")

    def test_runtime_instruction_existing_path_drift_fails_closed(self) -> None:
        self.replace_default_change("instructions-path-drift")

        self.assert_error("planning-bindings-openspec-instructions-invalid")

    def test_generic_unchecked_task_without_trace_is_rejected(self) -> None:
        self.write(
            "openspec/changes/add-login/tasks.md",
            "- [ ] 1.1 Implement the change\n",
        )
        self.assert_error("planning-bindings-tasks-invalid")

    def test_delta_acceptance_must_remain_attached_to_catalog_requirements(self) -> None:
        requirements_path = self.root / "delivery-docs/product/requirements.json"
        requirements = json.loads(requirements_path.read_text(encoding="utf-8"))
        second_requirement = dict(requirements["requirements"][0])
        second_requirement.update(
            {
                "id": "REQ-AUDIT",
                "title": "Audit login",
                "statement": "A successful login is audit visible.",
            }
        )
        requirements["requirements"].append(second_requirement)
        self.write_json("delivery-docs/product/requirements.json", requirements)

        acceptance_path = self.root / "delivery-docs/product/acceptance.json"
        acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
        acceptance["acceptance_criteria"].append(
            {"id": "AC-AUDIT", "requirement_ids": ["REQ-AUDIT"]}
        )
        self.write_json("delivery-docs/product/acceptance.json", acceptance)

        roadmap_path = self.root / "delivery-docs/plans/delivery-roadmap.json"
        roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
        roadmap["slices"][0]["requirement_ids"] = ["REQ-LOGIN", "REQ-AUDIT"]
        roadmap["slices"][0]["acceptance_ids"] = ["AC-LOGIN", "AC-AUDIT"]
        self.write_json("delivery-docs/plans/delivery-roadmap.json", roadmap)

        self.write(
            "openspec/changes/add-login/proposal.md",
            "# Proposal\n\nREQ-LOGIN REQ-AUDIT AC-LOGIN AC-AUDIT\n",
        )
        self.write(
            "openspec/changes/add-login/design.md",
            "# Design\n\nREQ-LOGIN REQ-AUDIT AC-LOGIN AC-AUDIT\n",
        )
        self.write(
            "openspec/changes/add-login/tasks.md",
            "- [ ] 1.1 Implement REQ-LOGIN and verify AC-LOGIN\n"
            "- [ ] 1.2 Implement REQ-AUDIT and verify AC-AUDIT\n",
        )
        self.write(
            "openspec/changes/add-login/specs/session/spec.md",
            "## ADDED Requirements\n\n"
            "### Requirement: Login [REQ-LOGIN]\n\n"
            "#### Scenario: Audit is visible [AC-AUDIT]\n"
            "- **WHEN** valid credentials are submitted\n"
            "- **THEN** an audit event is observable\n\n"
            "### Requirement: Audit [REQ-AUDIT]\n\n"
            "#### Scenario: Session exists [AC-LOGIN]\n"
            "- **WHEN** valid credentials are submitted\n"
            "- **THEN** an authenticated session is observable\n",
        )

        self.assert_error("planning-bindings-openspec-trace-invalid")

    def test_archived_change_id_cannot_be_reserved_or_reused(self) -> None:
        self.write(
            "openspec/changes/archive/2026-07-01-add-login/proposal.md",
            "# Archived add-login\n",
        )
        self.assert_error("planning-bindings-openspec-archive-conflict")

    def test_custom_work_items_must_remain_under_delivery_control(self) -> None:
        self.assert_error(
            "planning-bindings-authority-invalid",
            authority_paths={"work_items": "docs/contracts"},
        )
        self.assert_error(
            "planning-bindings-authority-invalid",
            authority_paths={"work_items": "openspec/contracts"},
        )

    def test_directory_fd_open_rejects_intermediate_symlink_swap(self) -> None:
        product = self.root / "delivery-docs/product"
        displaced = self.root / "delivery-docs/product-before-race"
        external = self.base / "outside"
        external.mkdir()
        (external / "requirements.json").write_text(
            '{"secret":"must never be read"}\n',
            encoding="utf-8", newline="",
        )
        swapped = False

        if os.name == "nt":
            # Windows pins directory identity instead of using dirfd; swap the
            # directory right after it is pinned and expect the re-verify to
            # fail closed. A directory rename needs no symlink privilege.
            original_child_open = bindings._open_child_directory

            def racing_child_open(parent_fd, name, *args, **kwargs):
                nonlocal swapped
                handle = original_child_open(parent_fd, name, *args, **kwargs)
                if name == "product" and not swapped:
                    product.rename(displaced)
                    external.rename(product)
                    swapped = True
                return handle

            with mock.patch.object(
                bindings,
                "_open_child_directory",
                side_effect=racing_child_open,
            ):
                self.assert_error("planning-bindings-file-raced")
        else:
            original_open = os.open

            def racing_open(path, flags, *args, **kwargs):
                nonlocal swapped
                if path == "product" and kwargs.get("dir_fd") is not None and not swapped:
                    product.rename(displaced)
                    product.symlink_to(external, target_is_directory=True)
                    swapped = True
                return original_open(path, flags, *args, **kwargs)

            with mock.patch.object(bindings.os, "open", side_effect=racing_open):
                self.assert_error("planning-bindings-file-uninspectable")
        self.assertTrue(swapped)

    @unittest.skipUnless(SYMLINKS_SUPPORTED, "symlink creation requires elevated privileges on Windows")
    def test_metadata_and_tree_links_fail_closed(self) -> None:
        metadata = self.root / "openspec/changes/add-login/.openspec.yaml"
        metadata.unlink()
        metadata.symlink_to(self.root / "openspec/config.yaml")
        self.assert_error("planning-bindings-openspec-unsafe")

    @unittest.skipUnless(SYSTEM_OPENSPEC, "local OpenSpec not installed")
    def test_optional_real_openspec_1_6_status_and_strict_validation(self) -> None:
        self.write("openspec/changes/add-login/design.md", "# Design\n\nREQ-LOGIN AC-LOGIN\n")
        self.write(
            "openspec/changes/add-login/proposal.md",
            "## Why\nNeeded.\n\n"
            "## What Changes\n- Add login. REQ-LOGIN AC-LOGIN\n\n"
            "## Capabilities\n### New Capabilities\n- `session`: Login sessions.\n\n"
            "## Impact\nWeb authentication.\n",
        )
        self.write(
            "openspec/changes/add-login/specs/session/spec.md",
            "## ADDED Requirements\n\n"
            "### Requirement: Authenticated session REQ-LOGIN\n"
            "The system SHALL create an authenticated session for valid credentials.\n\n"
            "#### Scenario: Valid login AC-LOGIN\n"
            "- **WHEN** valid credentials are submitted\n"
            "- **THEN** an authenticated session is observable\n",
        )
        self.write(
            "openspec/changes/add-login/tasks.md",
            "- [ ] 1.1 Implement REQ-LOGIN and verify AC-LOGIN\r\n"
            "  - [x] A nested task was already executed\r\n",
        )

        result = self.collect(openspec_executable=Path(SYSTEM_OPENSPEC))

        change = result["external_bindings"]["openspec"]["changes"]["add-login"]
        self.assertTrue(change["strict_valid"])
        self.assertTrue(change["tasks_checked"])
        self.assertEqual(["REQ-LOGIN"], change["requirement_ids"])
        self.assertEqual(["AC-LOGIN"], change["acceptance_ids"])


if __name__ == "__main__":
    unittest.main()
