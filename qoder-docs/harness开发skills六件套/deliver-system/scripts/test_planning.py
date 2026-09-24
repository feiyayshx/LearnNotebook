#!/usr/bin/env python3
"""M3 machine planning-graph validator regression tests."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import planning


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


class PlanningValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="delivery-planning-test-")
        self.root = Path(self.temporary.name).resolve()
        self.source_path = "delivery-docs/product/source/system-spec.md"
        self.source_text = "# System Spec\n\nUsers create accounts and profiles.\n"
        self.write_text(self.source_path, self.source_text)
        self.source_sha = hashlib.sha256(self.source_text.encode("utf-8")).hexdigest()
        self.documents, self.external = self.make_fixture()
        self.persist_fixture()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_text(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def write_json(self, relative: str, value: object) -> Path:
        return self.write_text(relative, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def make_fixture(self, *, project_mode: str = "greenfield") -> tuple[dict[str, object], dict[str, object]]:
        git_binding = {
            "repository_scope_id": "repo-" + "1" * 64,
            "checkout_scope_id": "checkout-" + "2" * 64,
            "checkout_kind": "main",
            "head": "3" * 40,
            "baseline": "3" * 40,
            "ref": "refs/heads/main",
            "dirty_digest": "4" * 64,
            "excluded_paths_digest": "7" * 64,
            "integration_owner": True,
        }
        source_digest = planning.canonical_json_digest({self.source_path: self.source_sha})
        requirements = {
            "schema_version": 1,
            "artifact_type": "requirements",
            "requirements": [
                {
                    "id": "REQ-ACCOUNT",
                    "revision": 1,
                    "title": "Create account",
                    "statement": "A visitor can create an account and observe a successful result.",
                    "kind": "functional",
                    "priority": "P0",
                    "status": "confirmed",
                    "scope": "in_scope",
                    "scope_reason": None,
                    "source_refs": [{"path": self.source_path, "sha256": self.source_sha, "locator": "accounts"}],
                    "depends_on": [],
                    "decision_refs": [],
                    "sensitivity_assessment": {
                        "categories": [],
                        "rationale": "No sensitive behavior identified in the confirmed scope.",
                    },
                },
                {
                    "id": "REQ-PROFILE",
                    "revision": 1,
                    "title": "View profile",
                    "statement": "An account holder can view the profile created for that account.",
                    "kind": "functional",
                    "priority": "P1",
                    "status": "confirmed",
                    "scope": "in_scope",
                    "scope_reason": None,
                    "source_refs": [{"path": self.source_path, "sha256": self.source_sha, "locator": "profiles"}],
                    "depends_on": ["REQ-ACCOUNT"],
                    "decision_refs": [],
                    "sensitivity_assessment": {
                        "categories": [],
                        "rationale": "No sensitive behavior identified in the confirmed scope.",
                    },
                },
            ],
        }
        acceptance = {
            "schema_version": 1,
            "artifact_type": "acceptance-catalog",
            "acceptance_criteria": [
                self.acceptance("AC-ACCOUNT", "REQ-ACCOUNT", "account-created", "positive"),
                self.acceptance("AC-PROFILE", "REQ-PROFILE", "profile-visible", "positive"),
            ],
        }
        features = {
            "schema_version": 1,
            "artifact_type": "feature-ledger",
            "features": [
                self.feature("FEATURE-ACCOUNT", "account-onboarding", "REQ-ACCOUNT", "AC-ACCOUNT", "SLICE-ACCOUNT"),
                {
                    **self.feature("FEATURE-PROFILE", "profile-view", "REQ-PROFILE", "AC-PROFILE", "SLICE-PROFILE"),
                    "depends_on": ["FEATURE-ACCOUNT"],
                },
            ],
        }
        slices = [
            self.slice(
                "SLICE-ACCOUNT",
                "add-account",
                "account-onboarding",
                "REQ-ACCOUNT",
                "AC-ACCOUNT",
                "FEATURE-ACCOUNT",
                order=1,
                state="materialized",
            ),
            {
                **self.slice(
                    "SLICE-PROFILE",
                    "add-profile-view",
                    "profile-view",
                    "REQ-PROFILE",
                    "AC-PROFILE",
                    "FEATURE-PROFILE",
                    order=2,
                    state="reserved",
                ),
                "depends_on": ["SLICE-ACCOUNT"],
            },
        ]
        roadmap = {
            "schema_version": 1,
            "artifact_type": "delivery-roadmap",
            "project_mode": project_mode,
            "milestones": [{"id": "MILESTONE-ONE", "title": "First delivery", "order": 1, "status": "planned"}],
            "slices": slices,
            "next_ready_slice_id": "SLICE-ACCOUNT",
            "blocking_questions": [],
            "pending_decisions": [],
        }
        materialized_digest = "5" * 64
        openspec_changes = {
            "add-account": {
                "state": "materialized",
                "metadata_valid": True,
                "strict_valid": True,
                "tasks_checked": False,
                "artifact_digest": materialized_digest,
                "runtime_artifacts_digest": "8" * 64,
                "runtime_instructions_digest": "9" * 64,
                "requirement_ids": ["REQ-ACCOUNT"],
                "acceptance_ids": ["AC-ACCOUNT"],
            },
            "add-profile-view": {"state": "reserved", "artifact_digest": None},
        }
        contracts = {
            "add-account": self.contract(
                "add-account",
                "SLICE-ACCOUNT",
                "REQ-ACCOUNT",
                "AC-ACCOUNT",
                "FEATURE-ACCOUNT",
                "account-onboarding",
                source_digest,
                git_binding,
                state="materialized",
                artifact_digest=materialized_digest,
            ),
            "add-profile-view": self.contract(
                "add-profile-view",
                "SLICE-PROFILE",
                "REQ-PROFILE",
                "AC-PROFILE",
                "FEATURE-PROFILE",
                "profile-view",
                source_digest,
                git_binding,
                state="reserved",
                artifact_digest=None,
            ),
        }
        documents: dict[str, object] = {
            "delivery-docs/product/requirements.json": requirements,
            "delivery-docs/product/acceptance.json": acceptance,
            "delivery-docs/product/feature-ledger.json": features,
            "delivery-docs/plans/delivery-roadmap.json": roadmap,
            "delivery-docs/state/work-items/add-account.json": contracts["add-account"],
            "delivery-docs/state/work-items/add-profile-view.json": contracts["add-profile-view"],
        }
        external: dict[str, object] = {
            "schema_version": 1,
            "project_mode": project_mode,
            "registered_sources": {self.source_path: self.source_sha},
            "decisions": {
                "approved": [],
                "pending": [],
                "blocking": [],
                "artifacts": {},
                "authority_digest": planning.canonical_json_digest({}),
            },
            "git": git_binding,
            "openspec": {
                "schema": "spec-driven",
                "config_digest": "8" * 64,
                "tree_digest": "9" * 64,
                "cli_version": "1.6.0",
                "executable_digest": "a" * 64,
                "changes": openspec_changes,
            },
        }
        return documents, external

    @staticmethod
    def acceptance(
        identifier: str,
        requirement: str,
        method: str,
        classification: str,
        risk_coverage: list[str] | None = None,
    ) -> dict[str, object]:
        if risk_coverage is None:
            risk_coverage = ["compatibility"] if classification == "regression" else []
        return {
            "id": identifier,
            "requirement_ids": [requirement],
            "criterion": f"Observe {method} after the user action.",
            "oracle": {"method": method, "expected": "observable success"},
            "preconditions": ["clean test account"],
            "action": "perform the user-visible action",
            "expected_outcome": "the expected behavior is visible",
            "boundary": "e2e",
            "classification": classification,
            "risk_coverage": risk_coverage,
            "automation": "automated",
            "automation_reason": None,
            "status": "planned",
        }

    @staticmethod
    def feature(
        identifier: str,
        outcome_key: str,
        requirement: str,
        acceptance: str,
        slice_id: str,
    ) -> dict[str, object]:
        return {
            "id": identifier,
            "title": identifier.replace("FEATURE-", "").title(),
            "outcome": f"User observes {outcome_key}.",
            "outcome_key": outcome_key,
            "requirement_ids": [requirement],
            "acceptance_ids": [acceptance],
            "slice_ids": [slice_id],
            "depends_on": [],
            "milestone_id": "MILESTONE-ONE",
            "status": "planned",
            "owner": "delivery-team",
        }

    @staticmethod
    def slice(
        identifier: str,
        change_id: str,
        outcome_key: str,
        requirement: str,
        acceptance: str,
        feature: str,
        *,
        order: int,
        state: str,
    ) -> dict[str, object]:
        return {
            "id": identifier,
            "status": "planned",
            "change_id": change_id,
            "outcome": f"User observes {outcome_key}.",
            "outcome_key": outcome_key,
            "non_goals": ["No unrelated administration feature"],
            "requirement_ids": [requirement],
            "acceptance_ids": [acceptance],
            "feature_ids": [feature],
            "preservation_requirement_ids": [],
            "decision_refs": [],
            "depends_on": [],
            "milestone_id": "MILESTONE-ONE",
            "order": order,
            "risk": "R1",
            "affected_boundaries": ["web-ui", "http-api"],
            "migration": "none",
            "rollback": "planned",
            "sensitive_changes": [],
            "qoder_surface": "quest",
            "oversize_exception_pdr": None,
            "openspec_state": state,
        }

    @staticmethod
    def contract(
        change_id: str,
        slice_id: str,
        requirement: str,
        acceptance: str,
        feature: str,
        outcome_key: str,
        source_digest: str,
        git_binding: dict[str, object],
        *,
        state: str,
        artifact_digest: str | None,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "artifact_type": "delivery-contract",
            "change_id": change_id,
            "slice_id": slice_id,
            "source_digest": source_digest,
            "requirement_ids": [requirement],
            "acceptance_ids": [acceptance],
            "feature_ids": [feature],
            "outcome": f"User observes {outcome_key}.",
            "scope": ["Implement only the vertical behavior"],
            "non_goals": ["No unrelated administration feature"],
            "preservation_requirement_ids": [],
            "risk": "R1",
            "affected_components": ["web-ui", "http-api"],
            "compatibility_constraints": [],
            "migration": "none",
            "rollback": "planned",
            "sensitive_changes": [],
            "decision_refs": [],
            "unresolved_questions": [],
            "planned_checks": [
                {"id": f"CHECK-{change_id.upper().replace('-', '-')}", "level": "e2e", "status": "planned", "acceptance_ids": [acceptance]}
            ],
            "required_gates": ["GATE-0", "GATE-1", "GATE-2", "GATE-3"],
            "evidence_classes": ["test-report", "browser-trace"],
            "repair_budget": {"max_attempts": 3, "max_no_progress_attempts": 2},
            "human_approvals": [],
            "integration_owner": git_binding["integration_owner"],
            "git_binding": copy.deepcopy(git_binding),
            "openspec_binding": {"change_id": change_id, "state": state, "artifact_digest": artifact_digest},
            "delivery_state": "planned",
            "implementation_authorized": False,
            "openspec_apply_authorized": False,
            "m4_lock": True,
        }

    def persist_fixture(self) -> None:
        for relative, value in self.documents.items():
            self.write_json(relative, value)

    def bind_decision(
        self,
        identifier: str,
        status: str,
        relative: str,
        text: str,
    ) -> None:
        path = self.write_text(relative, text)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        decisions = self.external["decisions"]
        decisions[status].append(identifier)
        decisions["artifacts"][identifier] = {
            "path": relative,
            "sha256": digest,
            "status": status,
        }
        decisions["authority_digest"] = planning.canonical_json_digest(decisions["artifacts"])

    def inventory(self) -> dict[str, tuple[object, ...]]:
        result: dict[str, tuple[object, ...]] = {}
        for path in sorted(self.root.rglob("*")):
            relative = path.relative_to(self.root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                result[relative] = ("link", os.readlink(path))
            elif stat.S_ISDIR(mode):
                result[relative] = ("directory",)
            elif stat.S_ISREG(mode):
                result[relative] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
            else:
                result[relative] = ("special", stat.S_IFMT(mode))
        return result

    def report(self) -> dict[str, object]:
        return planning.validate_planning_bundle(self.root, copy.deepcopy(self.external))

    def assert_reason(self, report: dict[str, object], code: str) -> None:
        self.assertFalse(report["valid"], report)
        self.assertIn(code, report["reason_codes"], report)

    def test_valid_bundle_is_deterministic_read_only_and_sealable(self) -> None:
        before = self.inventory()
        first = self.report()
        middle = self.inventory()
        second = self.report()
        after = self.inventory()

        self.assertTrue(first["valid"], first)
        self.assertEqual("planning_ready", first["status"])
        self.assertEqual(["SLICE-ACCOUNT", "SLICE-PROFILE"], first["topological_order"])
        self.assertEqual(before, middle)
        self.assertEqual(before, after)
        self.assertEqual(first, second)

        payload = planning.build_planning_seal_payload(
            first,
            approval_ref="PDR-PLAN-APPROVAL",
            approval_revision=7,
        )
        self.assertTrue(payload["m4_lock"])
        self.assertFalse(payload["implementation_authorized"])
        self.assertEqual(planning.planning_seal_digest(payload), planning.planning_seal_digest(copy.deepcopy(payload)))

    def test_content_drift_changes_bundle_and_seal_digest(self) -> None:
        first = self.report()
        first_payload = planning.build_planning_seal_payload(first, approval_ref="PDR-PLAN-APPROVAL", approval_revision=7)
        requirements = self.documents["delivery-docs/product/requirements.json"]
        requirements["requirements"][0]["title"] = "Create a verified account"
        self.persist_fixture()

        second = self.report()
        second_payload = planning.build_planning_seal_payload(second, approval_ref="PDR-PLAN-APPROVAL", approval_revision=7)

        self.assertTrue(second["valid"], second)
        self.assertNotEqual(first["bundle_digest"], second["bundle_digest"])
        self.assertNotEqual(planning.planning_seal_digest(first_payload), planning.planning_seal_digest(second_payload))

    def test_runtime_and_excluded_path_bindings_change_or_block_the_seal(self) -> None:
        first = self.report()
        first_payload = planning.build_planning_seal_payload(
            first,
            approval_ref="PDR-PLAN-APPROVAL",
            approval_revision=7,
        )
        self.external["openspec"]["config_digest"] = "b" * 64
        second = self.report()
        second_payload = planning.build_planning_seal_payload(
            second,
            approval_ref="PDR-PLAN-APPROVAL",
            approval_revision=7,
        )

        self.assertTrue(second["valid"], second)
        self.assertNotEqual(first["external_bindings_digest"], second["external_bindings_digest"])
        self.assertNotEqual(planning.planning_seal_digest(first_payload), planning.planning_seal_digest(second_payload))

        self.external["git"]["excluded_paths_digest"] = "not-a-digest"
        self.assert_reason(self.report(), planning.BINDING_INVALID)

        self.external["git"]["excluded_paths_digest"] = "7" * 64
        del self.external["openspec"]["cli_version"]
        missing_runtime = self.report()
        self.assert_reason(missing_runtime, planning.FIELD_MISSING)

        self.external["openspec"]["cli_version"] = "1.6.0"
        del self.external["openspec"]["changes"]["add-account"]["runtime_instructions_digest"]
        missing_instructions = self.report()
        self.assert_reason(missing_instructions, planning.FIELD_MISSING)

    def test_decision_authority_reloads_documents_and_fails_on_drift(self) -> None:
        relative = "delivery-docs/decisions/adr/0001-preserve-boundary.md"
        original = "# ADR-0001\n\n- Status: Accepted\n\nPreserve the public boundary.\n"
        self.bind_decision("ADR-0001", "approved", relative, original)
        first = self.report()
        self.assertTrue(first["valid"], first)

        self.write_text(relative, original + "Unexpected unapproved edit.\n")
        drifted = self.report()
        self.assert_reason(drifted, planning.DECISION_DIGEST_MISMATCH)

        self.write_text(relative, original)
        self.external["decisions"]["authority_digest"] = "0" * 64
        bad_authority = self.report()
        self.assert_reason(bad_authority, planning.DECISION_AUTHORITY_DIGEST_MISMATCH)

        artifact = self.external["decisions"]["artifacts"]["ADR-0001"]
        artifact["status"] = "pending"
        self.external["decisions"]["authority_digest"] = planning.canonical_json_digest(
            self.external["decisions"]["artifacts"]
        )
        wrong_state = self.report()
        self.assert_reason(wrong_state, planning.DECISION_STATE_MISMATCH)

    def test_decision_artifacts_have_exact_coverage_and_restricted_paths(self) -> None:
        self.external["decisions"]["approved"] = ["PDR-PATH-POLICY"]
        self.external["decisions"]["authority_digest"] = planning.canonical_json_digest({})
        uncovered = self.report()
        self.assert_reason(uncovered, planning.DECISION_ARTIFACT_INVALID)

        text = "# PDR-PATH-POLICY\n\n- Status: Approved\n"
        wrong_path = "delivery-docs/product/PDR-PATH-POLICY.md"
        path = self.write_text(wrong_path, text)
        self.external["decisions"]["artifacts"] = {
            "PDR-PATH-POLICY": {
                "path": wrong_path,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "status": "approved",
            }
        }
        self.external["decisions"]["authority_digest"] = planning.canonical_json_digest(
            self.external["decisions"]["artifacts"]
        )
        restricted = self.report()
        self.assert_reason(restricted, planning.DECISION_ARTIFACT_INVALID)

    def test_duplicate_json_keys_and_duplicate_ids_fail_closed(self) -> None:
        requirements = self.documents["delivery-docs/product/requirements.json"]
        raw = json.dumps(requirements)
        raw = raw.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1)
        self.write_text("delivery-docs/product/requirements.json", raw)
        self.assert_reason(self.report(), planning.JSON_DUPLICATE_KEY)

        self.write_json("delivery-docs/product/requirements.json", requirements)
        requirements["requirements"].append(copy.deepcopy(requirements["requirements"][0]))
        self.write_json("delivery-docs/product/requirements.json", requirements)
        self.assert_reason(self.report(), planning.ID_DUPLICATE)

    def test_unknown_schema_malformed_id_enum_and_missing_oracle_are_rejected(self) -> None:
        requirements = self.documents["delivery-docs/product/requirements.json"]
        requirements["schema_version"] = 99
        requirements["requirements"][0]["id"] = "req-account"
        acceptance = self.documents["delivery-docs/product/acceptance.json"]
        acceptance["acceptance_criteria"][0]["automation"] = "eventually"
        acceptance["acceptance_criteria"][0]["oracle"] = {}
        self.persist_fixture()

        report = self.report()

        self.assert_reason(report, planning.SCHEMA_UNSUPPORTED)
        self.assertIn(planning.ID_INVALID, report["reason_codes"])
        self.assertIn(planning.ENUM_INVALID, report["reason_codes"])
        self.assertIn(planning.ACCEPTANCE_ORACLE_MISSING, report["reason_codes"])

    def test_every_in_scope_requirement_and_feature_need_a_complete_chain(self) -> None:
        features = self.documents["delivery-docs/product/feature-ledger.json"]
        features["features"][1]["requirement_ids"] = []
        features["features"][1]["slice_ids"] = []
        self.write_json("delivery-docs/product/feature-ledger.json", features)

        report = self.report()

        self.assert_reason(report, planning.REQUIREMENT_UNCOVERED)
        self.assertIn(planning.FEATURE_ORPHAN, report["reason_codes"])
        self.assertIn(planning.REFERENCE_MISMATCH, report["reason_codes"])

    def test_orphans_missing_contract_and_source_drift_are_rejected(self) -> None:
        acceptance = self.documents["delivery-docs/product/acceptance.json"]
        acceptance["acceptance_criteria"].append(self.acceptance("AC-ORPHAN", "REQ-ACCOUNT", "orphan", "negative"))
        self.write_json("delivery-docs/product/acceptance.json", acceptance)
        self.assert_reason(self.report(), planning.ACCEPTANCE_ORPHAN)

        self.documents, self.external = self.make_fixture()
        self.persist_fixture()
        (self.root / "delivery-docs/state/work-items/add-profile-view.json").unlink()
        self.assert_reason(self.report(), planning.CONTRACT_MISSING)

        self.persist_fixture()
        self.external["registered_sources"][self.source_path] = "0" * 64
        self.assert_reason(self.report(), planning.SOURCE_DIGEST_MISMATCH)

    def test_cycles_invalid_order_and_scope_leak_are_rejected(self) -> None:
        roadmap = self.documents["delivery-docs/plans/delivery-roadmap.json"]
        roadmap["slices"][0]["depends_on"] = ["SLICE-PROFILE"]
        self.write_json("delivery-docs/plans/delivery-roadmap.json", roadmap)
        self.assert_reason(self.report(), planning.DEPENDENCY_CYCLE)

        self.documents, self.external = self.make_fixture()
        requirements = self.documents["delivery-docs/product/requirements.json"]
        requirements["requirements"][1]["scope"] = "deferred"
        requirements["requirements"][1]["scope_reason"] = "future milestone"
        self.persist_fixture()
        self.assert_reason(self.report(), planning.SCOPE_LEAK)

        self.documents, self.external = self.make_fixture()
        roadmap = self.documents["delivery-docs/plans/delivery-roadmap.json"]
        roadmap["slices"][1]["order"] = 1
        self.persist_fixture()
        self.assert_reason(self.report(), planning.TOPOLOGY_INVALID)

    def add_oversized_slice(self) -> None:
        requirements = self.documents["delivery-docs/product/requirements.json"]["requirements"]
        acceptance = self.documents["delivery-docs/product/acceptance.json"]["acceptance_criteria"]
        features = self.documents["delivery-docs/product/feature-ledger.json"]["features"]
        roadmap_slice = self.documents["delivery-docs/plans/delivery-roadmap.json"]["slices"][0]
        contract = self.documents["delivery-docs/state/work-items/add-account.json"]
        openspec = self.external["openspec"]["changes"]["add-account"]
        for number in range(2, 10):
            req_id = f"REQ-ACCOUNT-{number}"
            ac_id = f"AC-ACCOUNT-{number}"
            feature_id = f"FEATURE-ACCOUNT-{number}"
            requirements.append(
                {
                    **copy.deepcopy(requirements[0]),
                    "id": req_id,
                    "title": f"Account rule {number}",
                    "depends_on": [],
                }
            )
            acceptance.append(self.acceptance(ac_id, req_id, f"account-rule-{number}", "positive"))
            features.append(self.feature(feature_id, "account-onboarding", req_id, ac_id, "SLICE-ACCOUNT"))
            roadmap_slice["requirement_ids"].append(req_id)
            roadmap_slice["acceptance_ids"].append(ac_id)
            roadmap_slice["feature_ids"].append(feature_id)
            contract["requirement_ids"].append(req_id)
            contract["acceptance_ids"].append(ac_id)
            contract["feature_ids"].append(feature_id)
            contract["planned_checks"].append(
                {"id": f"CHECK-ACCOUNT-{number}", "level": "e2e", "status": "planned", "acceptance_ids": [ac_id]}
            )
            openspec["requirement_ids"].append(req_id)
            openspec["acceptance_ids"].append(ac_id)

    def test_oversized_slice_requires_approved_pdr(self) -> None:
        self.add_oversized_slice()
        self.persist_fixture()
        report = self.report()
        self.assert_reason(report, planning.SLICE_OVERSIZED)
        self.assertIn(planning.OVERSIZE_PDR_REQUIRED, report["reason_codes"])

        roadmap_slice = self.documents["delivery-docs/plans/delivery-roadmap.json"]["slices"][0]
        roadmap_slice["oversize_exception_pdr"] = "PDR-OVERSIZE-ACCOUNT"
        roadmap_slice["decision_refs"].append("PDR-OVERSIZE-ACCOUNT")
        self.documents["delivery-docs/state/work-items/add-account.json"]["decision_refs"].append("PDR-OVERSIZE-ACCOUNT")
        self.bind_decision(
            "PDR-OVERSIZE-ACCOUNT",
            "approved",
            "delivery-docs/decisions/product/PDR-OVERSIZE-ACCOUNT.md",
            "# PDR-OVERSIZE-ACCOUNT\n\n- Status: Approved\n",
        )
        self.persist_fixture()
        approved = self.report()
        self.assertTrue(approved["valid"], approved)
        self.assertEqual([planning.SLICE_OVERSIZED], [item["code"] for item in approved["warnings"]])

    def test_brownfield_requires_observable_preservation_with_regression_oracle(self) -> None:
        self.documents, self.external = self.make_fixture(project_mode="brownfield")
        self.persist_fixture()
        self.assert_reason(self.report(), planning.PRESERVATION_REQUIRED)

        requirement = self.documents["delivery-docs/product/requirements.json"]["requirements"][0]
        requirement.update(
            {
                "kind": "preservation",
                "observable_behavior": "An existing account holder remains able to view the public profile.",
                "preservation_boundary": "public browser profile journey",
                "oracle": {"method": "existing-profile-view", "expected": "profile is visible"},
                "evidence_status": "planned",
            }
        )
        preserved_acceptance = self.documents["delivery-docs/product/acceptance.json"]["acceptance_criteria"][0]
        preserved_acceptance["classification"] = "regression"
        preserved_acceptance["risk_coverage"] = ["compatibility"]
        self.documents["delivery-docs/plans/delivery-roadmap.json"]["slices"][0]["preservation_requirement_ids"] = ["REQ-ACCOUNT"]
        self.documents["delivery-docs/state/work-items/add-account.json"]["preservation_requirement_ids"] = ["REQ-ACCOUNT"]
        self.persist_fixture()
        accepted = self.report()
        self.assertTrue(accepted["valid"], accepted)

        requirement["oracle"]["method"] = "file_hash"
        self.persist_fixture()
        self.assert_reason(self.report(), planning.PRESERVATION_NOT_OBSERVABLE)

    def test_sensitive_high_risk_slice_requires_exact_approval_and_risk_coverage(self) -> None:
        roadmap_slice = self.documents["delivery-docs/plans/delivery-roadmap.json"]["slices"][0]
        contract = self.documents["delivery-docs/state/work-items/add-account.json"]
        boundaries = [
            "authentication",
            "authorization-permissions",
            "production-database",
            "external-paid-service",
        ]
        for item in (roadmap_slice, contract):
            item["risk"] = "R4"
            item["migration"] = "required"
            item["rollback"] = "required"
        roadmap_slice["affected_boundaries"] = boundaries
        contract["affected_components"] = boundaries
        self.persist_fixture()

        rejected = self.report()
        self.assert_reason(rejected, planning.SENSITIVE_CHANGE_UNDECLARED)
        self.assertIn(planning.SENSITIVE_APPROVAL_REQUIRED, rejected["reason_codes"])
        self.assertIn(planning.SENSITIVE_ACCEPTANCE_MISSING, rejected["reason_codes"])

        sensitive = [
            "migration",
            "rollback",
            "security",
            "permission",
            "external_cost",
            "production_data",
        ]
        roadmap_slice["sensitive_changes"] = sensitive
        contract["sensitive_changes"] = sensitive
        roadmap_slice["decision_refs"] = ["PDR-HIGH-RISK-ACCOUNT"]
        contract["decision_refs"] = ["PDR-HIGH-RISK-ACCOUNT"]
        contract["human_approvals"] = ["PDR-HIGH-RISK-ACCOUNT"]
        self.bind_decision(
            "PDR-HIGH-RISK-ACCOUNT",
            "approved",
            "delivery-docs/decisions/product/PDR-HIGH-RISK-ACCOUNT.md",
            "# PDR-HIGH-RISK-ACCOUNT\n\n- Status: Approved\n",
        )

        negative = self.acceptance(
            "AC-ACCOUNT-RISK-FAILURE",
            "REQ-ACCOUNT",
            "account-risk-failure",
            "negative",
            ["failure", "security", "permission", "external_cost"],
        )
        regression = self.acceptance(
            "AC-ACCOUNT-RISK-REGRESSION",
            "REQ-ACCOUNT",
            "account-risk-regression",
            "regression",
            ["migration", "rollback", "production_data"],
        )
        acceptance = self.documents["delivery-docs/product/acceptance.json"]["acceptance_criteria"]
        acceptance.extend([negative, regression])
        feature = self.documents["delivery-docs/product/feature-ledger.json"]["features"][0]
        for ac_id in (negative["id"], regression["id"]):
            feature["acceptance_ids"].append(ac_id)
            roadmap_slice["acceptance_ids"].append(ac_id)
            contract["acceptance_ids"].append(ac_id)
            self.external["openspec"]["changes"]["add-account"]["acceptance_ids"].append(ac_id)
        contract["planned_checks"].extend(
            [
                {
                    "id": "CHECK-ACCOUNT-RISK-FAILURE",
                    "level": "e2e",
                    "status": "planned",
                    "acceptance_ids": [negative["id"]],
                },
                {
                    "id": "CHECK-ACCOUNT-RISK-REGRESSION",
                    "level": "integration",
                    "status": "planned",
                    "acceptance_ids": [regression["id"]],
                },
            ]
        )
        self.persist_fixture()
        approved = self.report()
        self.assertTrue(approved["valid"], approved)

        negative["risk_coverage"] = ["failure"]
        self.persist_fixture()
        unrelated_negative = self.report()
        self.assert_reason(unrelated_negative, planning.SENSITIVE_ACCEPTANCE_MISSING)

    def test_authentication_vocabulary_cannot_hide_in_low_risk_prose(self) -> None:
        phrases = (
            "password login",
            "credential sign-in",
            "browser session",
            "OAuth token",
            "SSO authentication",
            "MFA WebAuthn",
            "密码登录和会话认证",
            "角色权限与访问控制",
            "个人信息与隐私",
            "外部服务付费和计费",
            "生产数据库中的生产数据",
            "数据迁移",
            "失败后回滚",
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.documents, self.external = self.make_fixture()
                requirement = self.documents["delivery-docs/product/requirements.json"]["requirements"][0]
                requirement["statement"] = f"A visitor uses {phrase} to enter the system."
                self.persist_fixture()
                hidden = self.report()
                self.assert_reason(hidden, planning.SENSITIVE_CHANGE_UNDECLARED)
                self.assertIn(planning.SENSITIVE_APPROVAL_REQUIRED, hidden["reason_codes"])
                self.assertIn(planning.SENSITIVE_ACCEPTANCE_MISSING, hidden["reason_codes"])

        self.documents, self.external = self.make_fixture()
        requirement = self.documents["delivery-docs/product/requirements.json"]["requirements"][0]
        requirement["sensitivity_assessment"] = {
            "categories": ["privacy"],
            "rationale": "Owner explicitly classified the behavior as privacy-sensitive.",
        }
        self.persist_fixture()
        explicit = self.report()
        self.assert_reason(explicit, planning.SENSITIVE_CHANGE_UNDECLARED)

    def test_m4_claims_and_blocking_decisions_are_rejected(self) -> None:
        contract = self.documents["delivery-docs/state/work-items/add-account.json"]
        contract["implementation_authorized"] = True
        contract["planned_checks"][0]["status"] = "passed"
        self.persist_fixture()
        self.assert_reason(self.report(), planning.M4_LOCK_VIOLATION)

        self.documents, self.external = self.make_fixture()
        self.bind_decision(
            "PDR-UNRESOLVED",
            "pending",
            "delivery-docs/decisions/product/PDR-UNRESOLVED.md",
            "# PDR-UNRESOLVED\n\n- Status: Pending\n",
        )
        self.persist_fixture()
        self.assert_reason(self.report(), planning.BLOCKING_DECISION)

    def test_only_deterministic_next_slice_may_be_materialized(self) -> None:
        roadmap_slice = self.documents["delivery-docs/plans/delivery-roadmap.json"]["slices"][1]
        roadmap_slice["openspec_state"] = "materialized"
        external_change = self.external["openspec"]["changes"]["add-profile-view"]
        external_change.update(
            {
                "state": "materialized",
                "metadata_valid": True,
                "strict_valid": True,
                "tasks_checked": False,
                "artifact_digest": "6" * 64,
                "requirement_ids": ["REQ-PROFILE"],
                "acceptance_ids": ["AC-PROFILE"],
            }
        )
        self.documents["delivery-docs/state/work-items/add-profile-view.json"]["openspec_binding"].update(
            {"state": "materialized", "artifact_digest": "6" * 64}
        )
        self.persist_fixture()
        self.assert_reason(self.report(), planning.OPENSPEC_JIT_VIOLATION)

    def test_change_ready_validates_fully_reserved_graph_before_jit_creation(self) -> None:
        roadmap_slice = self.documents["delivery-docs/plans/delivery-roadmap.json"]["slices"][0]
        roadmap_slice["openspec_state"] = "reserved"
        self.external["openspec"]["changes"]["add-account"] = {
            "state": "reserved",
            "artifact_digest": None,
        }
        self.documents["delivery-docs/state/work-items/add-account.json"]["openspec_binding"].update(
            {"state": "reserved", "artifact_digest": None}
        )
        self.persist_fixture()

        pre_creation = planning.validate_planning_bundle(
            self.root,
            copy.deepcopy(self.external),
            readiness="change_ready",
        )

        self.assertTrue(pre_creation["valid"], pre_creation)
        self.assertEqual("change_ready", pre_creation["status"])
        self.assert_reason(self.report(), planning.OPENSPEC_JIT_VIOLATION)
        with self.assertRaises(ValueError):
            planning.build_planning_seal_payload(
                pre_creation,
                approval_ref="PDR-PLAN-APPROVAL",
                approval_revision=7,
            )

        self.documents, self.external = self.make_fixture()
        self.persist_fixture()
        already_materialized = planning.validate_planning_bundle(
            self.root,
            copy.deepcopy(self.external),
            readiness="change_ready",
        )
        self.assert_reason(already_materialized, planning.OPENSPEC_JIT_VIOLATION)

    @unittest.skipUnless(SYMLINKS_SUPPORTED, "symlink creation requires elevated privileges on Windows")
    def test_authority_and_source_paths_reject_escape_links_and_special_nodes(self) -> None:
        self.external["authority_paths"] = {"requirements": "../requirements.json"}
        self.assert_reason(self.report(), planning.PATH_INVALID)

        self.documents, self.external = self.make_fixture()
        self.persist_fixture()
        requirements_path = self.root / "delivery-docs/product/requirements.json"
        safe_copy = self.root / "delivery-docs/product/requirements-real.json"
        requirements_path.rename(safe_copy)
        requirements_path.symlink_to(safe_copy.name)
        self.assert_reason(self.report(), planning.PATH_LINK)

        requirements_path.unlink()
        safe_copy.rename(requirements_path)
        special = self.root / "delivery-docs/state/work-items/special.json"
        os.mkfifo(special)
        self.assert_reason(self.report(), planning.PATH_SPECIAL)

    def test_descriptor_reads_reject_final_file_race(self) -> None:
        real_open = planning.os.open
        target = self.root / "delivery-docs" / "product" / "requirements.json"
        replacement = target.with_name("requirements.replacement")
        replacement.write_bytes(target.read_bytes())
        raced = False

        def replace_final(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal raced
            if os.name == "nt":
                # Windows opens the file by full path without a dir_fd.
                hit = os.fspath(path) == os.fspath(target)
            else:
                hit = path == "requirements.json" and kwargs.get("dir_fd") is not None
            if hit and not raced:
                raced = True
                os.replace(replacement, target)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(planning.os, "open", side_effect=replace_final):
            report = self.report()
        self.assertTrue(raced)
        self.assert_reason(report, planning.PATH_INVALID)

    def test_descriptor_reads_reject_intermediate_directory_race(self) -> None:
        # A parent swapped to a symlink between no-follow stat and open must
        # also fail before any file can be read through the new target.
        product = self.root / "delivery-docs" / "product"
        moved = self.root / "delivery-docs" / "product-before-race"
        outside = self.root / "outside-product"
        outside.mkdir()
        directory_raced = False

        if os.name == "nt":
            # Windows pins directory identity instead of using dirfd; swap the
            # directory right after it is pinned and expect the re-verify to
            # fail closed. A directory rename needs no symlink privilege.
            real_child_open = planning._open_child_directory

            def replace_parent(parent_fd: object, name: str, *args: object, **kwargs: object) -> object:
                nonlocal directory_raced
                handle = real_child_open(parent_fd, name, *args, **kwargs)
                if name == "product" and not directory_raced:
                    directory_raced = True
                    os.rename(product, moved)
                    os.rename(outside, product)
                return handle

            with mock.patch.object(planning, "_open_child_directory", side_effect=replace_parent):
                report = self.report()
        else:
            real_open = planning.os.open

            def replace_parent(path: object, flags: int, *args: object, **kwargs: object) -> int:
                nonlocal directory_raced
                if path == "product" and kwargs.get("dir_fd") is not None and not directory_raced:
                    directory_raced = True
                    os.rename(product, moved)
                    os.symlink(outside, product)
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(planning.os, "open", side_effect=replace_parent):
                report = self.report()
        self.assertTrue(directory_raced)
        self.assertFalse(report["valid"])
        self.assertTrue(
            {planning.PATH_INVALID, planning.PATH_LINK} & set(report["reason_codes"]),
            report,
        )

    def test_invalid_seal_input_fails_closed(self) -> None:
        report = self.report()
        report["valid"] = False
        with self.assertRaises(ValueError):
            planning.build_planning_seal_payload(report, approval_ref="PDR-PLAN", approval_revision=1)
        with self.assertRaises(ValueError):
            planning.planning_seal_digest({"schema_version": 1, "artifact_type": "planning-seal", "m4_lock": False})


if __name__ == "__main__":
    unittest.main()
