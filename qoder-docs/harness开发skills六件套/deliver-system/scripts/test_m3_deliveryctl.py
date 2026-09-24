#!/usr/bin/env python3
"""Integration tests for the M3 planning seal and phase ceiling.

The narrow validators have their own adversarial suites.  These tests prove
that their sanitized result is bound into the durable control plane and that
state transitions, reports, drift handling, and the M4 lock agree end to end.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import deliveryctl
import planning
import test_planning as planning_fixture


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


class M3DeliveryCtlIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="deliveryctl-m3-integration-")
        self.root = Path(self.temporary.name).resolve()
        deliveryctl.initialize_project(
            self.root,
            mode="greenfield",
            project_id="m3-integration",
            with_openspec_config=False,
        )

        # Reuse the canonical graph fixture exercised by planning.py itself;
        # this suite is responsible for orchestration, not a second schema
        # implementation hidden in test code.
        fixture = planning_fixture.PlanningValidationTests(
            methodName="test_valid_bundle_is_deterministic_read_only_and_sealable"
        )
        fixture.root = self.root
        fixture.source_path = "delivery-docs/product/source/system-spec.md"
        fixture.source_text = "# System Spec\n\nUsers create accounts and profiles.\n"
        fixture.write_text(fixture.source_path, fixture.source_text)
        fixture.source_sha = deliveryctl.sha256_file(self.root / fixture.source_path)
        documents, external = fixture.make_fixture()
        for relative, value in documents.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="")
        self.external = external
        self.source_digest = planning.canonical_json_digest(external["registered_sources"])
        self.baseline = external["git"]["baseline"]

        self._transition("BASELINING")
        self._transition("CLARIFYING")
        deliveryctl.set_context(
            self.root,
            expected_revision=self._state()["revision"],
            actor="integration-test",
            reason="bind fixture sources and baseline",
            source_digest=self.source_digest,
            baseline_commit=self.baseline,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _state(self) -> dict:
        return deliveryctl.read_json(self.root / "delivery-docs" / "state" / "state.json")

    def _collected(self) -> dict:
        return {
            "external_bindings": copy.deepcopy(self.external),
            "diagnostics": {
                "writes_performed": False,
                "implementation_authorized": False,
            },
        }

    def _transition(self, target: str, *, live: bool = False) -> dict:
        return deliveryctl.transition_state(
            self.root,
            target=target,
            reason=f"advance to {target}",
            actor="integration-test",
            expected_revision=self._state()["revision"] if (self.root / "delivery-docs" / "state" / "state.json").exists() else None,
            next_action=None,
            milestone=None,
            change=None,
            approval_ref=None,
            planning_external_bindings=copy.deepcopy(self.external) if live else None,
        )

    def _seal(self) -> dict:
        with mock.patch.object(deliveryctl, "collect_planning_context", return_value=self._collected()):
            return deliveryctl.seal_planning(
                self.root,
                expected_revision=self._state()["revision"],
                approval_ref="APPROVAL-M3-PLAN",
                actor="integration-test",
                approved_decisions=[],
                pending_decisions=[],
                blocking_decisions=[],
                expected_git_dir=None,
                expected_common_dir=None,
                git_authority_ref=None,
                openspec_executable=None,
            )

    def test_seal_binds_state_supports_planning_report_and_stops_before_m4(self) -> None:
        sealed = self._seal()

        state = self._state()
        self.assertEqual("plan_ready", state["planning_stage"])
        self.assertEqual(sealed["seal_digest"], state["planning_manifest_digest"])
        self.assertEqual("add-account", state["active_change"])
        self.assertFalse(sealed["implementation_authorized"])

        self._transition("READY", live=True)
        self._transition("PLANNING", live=True)
        self.assertEqual("await-m4-before-apply", self._state()["next_action"])
        with self.assertRaisesRegex(deliveryctl.DeliveryError, "current live"):
            deliveryctl.build_report(
                self.root,
                run_id="RUN-M3-NO-LIVE-BINDINGS",
                kind="planning-complete",
                summary="This must not be emitted from stored bindings alone.",
                actor="integration-test",
            )
        report = deliveryctl.build_report(
            self.root,
            run_id="RUN-M3-INTEGRATION",
            kind="planning-complete",
            summary="M3 plan is sealed; execution remains locked.",
            actor="integration-test",
            planning_external_bindings=copy.deepcopy(self.external),
        )
        report_result = deliveryctl.read_json(Path(report["result"]))
        self.assertEqual("planning_ready", report_result["planning"]["status"])
        self.assertFalse(
            report_result["planning"]["report"]["implementation_authorized"]
        )

        with self.assertRaisesRegex(deliveryctl.DeliveryError, "milestone M4"):
            self._transition("EXECUTING", live=True)

    def test_local_drift_fails_closed_without_rewriting_the_seal(self) -> None:
        sealed = self._seal()
        seal_path = self.root / "delivery-docs" / "state" / "planning-manifest.json"
        seal_before = seal_path.read_bytes()
        requirements_path = self.root / "delivery-docs" / "product" / "requirements.json"
        requirements = json.loads(requirements_path.read_text(encoding="utf-8"))
        requirements["requirements"][0]["title"] = "Changed after approval"
        requirements_path.write_text(json.dumps(requirements, indent=2) + "\n", encoding="utf-8", newline="")

        validation = deliveryctl.validate_project(self.root, check_fresh_gate=False)
        self.assertFalse(validation["valid"])
        self.assertEqual(sealed["seal_digest"], self._state()["planning_manifest_digest"])
        self.assertEqual(seal_before, seal_path.read_bytes())
        with self.assertRaisesRegex(deliveryctl.DeliveryError, "invalid"):
            self._transition("READY", live=True)

    def test_sealed_context_is_locked_and_return_to_clarifying_marks_it_stale(self) -> None:
        self._seal()
        candidate = self.root / "candidate-config.yaml"
        candidate.write_text("schema: spec-driven\n", encoding="utf-8", newline="")
        with mock.patch.object(deliveryctl.openspec_adapter, "apply_config_candidate") as apply:
            with self.assertRaisesRegex(deliveryctl.DeliveryError, "bound by a current planning seal"):
                deliveryctl.apply_openspec_config(
                    self.root,
                    candidate_path="candidate-config.yaml",
                    candidate_digest="a" * 64,
                    expected_config_digest="b" * 64,
                    approval_ref="APPROVAL-CONFIG",
                    allow_existing_replacement=False,
                )
            apply.assert_not_called()
        self._transition("READY", live=True)

        with self.assertRaisesRegex(deliveryctl.DeliveryError, "planning context is sealed"):
            deliveryctl.set_context(
                self.root,
                expected_revision=self._state()["revision"],
                actor="integration-test",
                reason="attempt hidden source swap",
                source_digest="f" * 64,
            )

        self._transition("ARCHITECTING", live=True)
        self._transition("CLARIFYING")
        state = self._state()
        self.assertEqual("stale", state["planning_stage"])
        self.assertIsNone(state["planning_manifest_digest"])

    def test_plan_init_is_non_overwriting_and_deprecated_config_flag_is_rejected(self) -> None:
        before = (self.root / "delivery-docs" / "product" / "requirements.json").read_bytes()
        events_before = (self.root / "delivery-docs" / "state" / "events.jsonl").read_bytes()
        initialized = deliveryctl.initialize_planning_bundle(self.root)
        self.assertIn("delivery-docs/product/requirements.json", initialized["preserved"])
        self.assertFalse(initialized["event_written"])
        self.assertEqual(before, (self.root / "delivery-docs" / "product" / "requirements.json").read_bytes())
        self.assertEqual(events_before, (self.root / "delivery-docs" / "state" / "events.jsonl").read_bytes())

        with self.assertRaisesRegex(deliveryctl.DeliveryError, "no longer writes OpenSpec config"):
            deliveryctl.initialize_project(
                self.root,
                mode="greenfield",
                project_id="m3-integration",
                with_openspec_config=True,
            )

    @unittest.skipUnless(SYMLINKS_SUPPORTED, "symlink creation requires elevated privileges on Windows")
    def test_plan_init_is_idempotent_and_preflights_every_destination(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deliveryctl-plan-init-") as temporary:
            fresh = Path(temporary).resolve()
            deliveryctl.initialize_project(
                fresh,
                mode="greenfield",
                project_id="plan-init",
                with_openspec_config=False,
            )
            first = deliveryctl.initialize_planning_bundle(fresh)
            self.assertTrue(first["event_written"])
            self.assertEqual(
                {
                    "delivery-docs/product/requirements.json",
                    "delivery-docs/product/acceptance.json",
                    "delivery-docs/product/feature-ledger.json",
                    "delivery-docs/plans/delivery-roadmap.json",
                },
                set(first["created"]),
            )
            events_after_first = (fresh / "delivery-docs" / "state" / "events.jsonl").read_bytes()
            second = deliveryctl.initialize_planning_bundle(fresh)
            self.assertFalse(second["event_written"])
            self.assertEqual(events_after_first, (fresh / "delivery-docs" / "state" / "events.jsonl").read_bytes())

        with tempfile.TemporaryDirectory(prefix="deliveryctl-plan-preflight-") as temporary:
            unsafe = Path(temporary).resolve()
            deliveryctl.initialize_project(
                unsafe,
                mode="greenfield",
                project_id="plan-preflight",
                with_openspec_config=False,
            )
            (unsafe / "delivery-docs" / "plans").mkdir(parents=True)
            target = unsafe / "unsafe-roadmap.json"
            target.write_text("{}\n", encoding="utf-8", newline="")
            (unsafe / "delivery-docs" / "plans" / "delivery-roadmap.json").symlink_to(target)
            with self.assertRaisesRegex(deliveryctl.DeliveryError, "symbolic link"):
                deliveryctl.initialize_planning_bundle(unsafe)
            self.assertFalse((unsafe / "delivery-docs" / "product" / "requirements.json").exists())
            self.assertFalse((unsafe / "delivery-docs" / "product" / "acceptance.json").exists())
            self.assertFalse((unsafe / "delivery-docs" / "product" / "feature-ledger.json").exists())

        with tempfile.TemporaryDirectory(prefix="deliveryctl-plan-protected-") as temporary:
            protected = Path(temporary).resolve()
            deliveryctl.initialize_project(
                protected,
                mode="greenfield",
                project_id="plan-protected",
                with_openspec_config=False,
            )
            (protected / ".git" / "hooks").mkdir(parents=True)
            manifest_path = protected / "delivery-docs" / "state" / "manifest.json"
            manifest = deliveryctl.read_json(manifest_path)
            manifest["paths"]["requirements"] = ".GIT/hooks/deliver-system-created.json"
            deliveryctl.atomic_write_json(manifest_path, manifest)
            validation = deliveryctl.validate_project(protected, check_fresh_gate=False)
            self.assertFalse(validation["valid"])
            with self.assertRaisesRegex(deliveryctl.DeliveryError, "invalid control plane"):
                deliveryctl.initialize_planning_bundle(protected)
            self.assertFalse(
                (protected / ".git" / "hooks" / "deliver-system-created.json").exists()
            )

        with tempfile.TemporaryDirectory(prefix="deliveryctl-plan-overlap-") as temporary:
            overlap = Path(temporary).resolve()
            deliveryctl.initialize_project(
                overlap,
                mode="greenfield",
                project_id="plan-overlap",
                with_openspec_config=False,
            )
            manifest_path = overlap / "delivery-docs" / "state" / "manifest.json"
            manifest = deliveryctl.read_json(manifest_path)
            manifest["paths"]["requirements"] = "delivery-docs/product"
            manifest["paths"]["acceptance"] = "delivery-docs/product/acceptance.json"
            deliveryctl.atomic_write_json(manifest_path, manifest)
            with self.assertRaisesRegex(deliveryctl.DeliveryError, "invalid control plane"):
                deliveryctl.initialize_planning_bundle(overlap)
            self.assertFalse((overlap / "delivery-docs" / "product").exists())

        with tempfile.TemporaryDirectory(prefix="deliveryctl-plan-concurrent-") as temporary:
            concurrent = Path(temporary).resolve()
            deliveryctl.initialize_project(
                concurrent,
                mode="greenfield",
                project_id="plan-concurrent",
                with_openspec_config=False,
            )
            events_path = concurrent / "delivery-docs" / "state" / "events.jsonl"
            events_before = events_path.read_bytes()
            real_lock = deliveryctl.ProjectLock

            class MutatingLock:
                def __init__(self, root: Path) -> None:
                    self.root = root
                    self.inner = real_lock(root)

                def __enter__(self):
                    entered = self.inner.__enter__()
                    manifest_path = self.root / "delivery-docs" / "state" / "manifest.json"
                    manifest = deliveryctl.read_json(manifest_path)
                    manifest["paths"]["requirements"] = "custom/requirements.json"
                    deliveryctl.atomic_write_json(manifest_path, manifest)
                    return entered

                def __exit__(self, *args: object) -> None:
                    self.inner.__exit__(*args)

            with mock.patch.object(deliveryctl, "ProjectLock", MutatingLock):
                with self.assertRaisesRegex(deliveryctl.DeliveryError, "manifest changed"):
                    deliveryctl.initialize_planning_bundle(concurrent)
            self.assertEqual(events_before, events_path.read_bytes())
            self.assertFalse((concurrent / "custom" / "requirements.json").exists())
            self.assertFalse((concurrent / "delivery-docs" / "product" / "requirements.json").exists())

    def test_bad_seal_digest_is_rejected(self) -> None:
        self._seal()
        seal_path = self.root / "delivery-docs" / "state" / "planning-manifest.json"
        seal = json.loads(seal_path.read_text(encoding="utf-8"))
        seal["seal_digest"] = "0" * 64
        seal_path.write_text(json.dumps(seal, indent=2) + "\n", encoding="utf-8", newline="")
        invalid = deliveryctl.validate_project(self.root, check_fresh_gate=False)
        self.assertFalse(invalid["valid"])
        self.assertTrue(any("seal_digest" in error for error in invalid["errors"]))

    def test_live_binding_drift_is_rejected(self) -> None:
        # Even valid local artifacts cannot be entered with different ambient
        # Git or OpenSpec bindings.
        self._seal()
        changed = copy.deepcopy(self.external)
        changed["openspec"]["tree_digest"] = "b" * 64
        with self.assertRaisesRegex(deliveryctl.DeliveryError, "bindings differ"):
            deliveryctl.transition_state(
                self.root,
                target="READY",
                reason="attempt with drifted runtime",
                actor="integration-test",
                expected_revision=self._state()["revision"],
                next_action=None,
                milestone=None,
                change=None,
                approval_ref=None,
                planning_external_bindings=changed,
            )

    def test_recovery_into_planning_gated_phase_recaptures_live_bindings(self) -> None:
        self._seal()

        def leave_pending_journal(
            root: Path,
            previous_state: dict,
            next_state: dict,
            event: dict,
        ) -> None:
            deliveryctl.controlled_atomic_write_json(
                root,
                root / "delivery-docs" / "state" / "transaction.json",
                deliveryctl._transaction_journal(previous_state, next_state, event),
            )

        with mock.patch.object(deliveryctl, "commit_state_event", side_effect=leave_pending_journal):
            with self.assertRaisesRegex(deliveryctl.DeliveryError, "pending state transaction"):
                self._transition("READY", live=True)
        self.assertEqual("CLARIFYING", self._state()["phase"])

        transaction = self.root / "delivery-docs" / "state" / "transaction.json"
        state_before = (self.root / "delivery-docs" / "state" / "state.json").read_bytes()
        events_before = (self.root / "delivery-docs" / "state" / "events.jsonl").read_bytes()
        changed = copy.deepcopy(self.external)
        changed["openspec"]["tree_digest"] = "b" * 64
        with mock.patch.object(
            deliveryctl,
            "collect_live_bindings_for_sealed_plan",
            return_value={"external_bindings": changed},
        ):
            with self.assertRaisesRegex(deliveryctl.DeliveryError, "bindings differ"):
                deliveryctl.recover_project(self.root)

        self.assertTrue(transaction.exists())
        self.assertEqual(state_before, (self.root / "delivery-docs" / "state" / "state.json").read_bytes())
        self.assertEqual(events_before, (self.root / "delivery-docs" / "state" / "events.jsonl").read_bytes())

        with mock.patch.object(
            deliveryctl,
            "collect_live_bindings_for_sealed_plan",
            return_value=self._collected(),
        ) as collect:
            recovered = deliveryctl.recover_project(self.root)
        collect.assert_called_once()
        self.assertEqual("READY", recovered["phase"])
        self.assertTrue(recovered["planning"]["live_bindings_checked"])
        self.assertFalse(transaction.exists())

    def test_planning_report_rejects_concurrent_state_transition_before_writing(self) -> None:
        self._seal()
        self._transition("READY", live=True)
        self._transition("PLANNING", live=True)
        original_loader = deliveryctl.load_text_template
        transitioned = False

        def transition_while_rendering(name: str) -> str:
            nonlocal transitioned
            template = original_loader(name)
            if not transitioned:
                transitioned = True
                self._transition("CLARIFYING")
            return template

        run_id = "RUN-M3-CONCURRENT-STALE"
        with mock.patch.object(
            deliveryctl,
            "load_text_template",
            side_effect=transition_while_rendering,
        ):
            with self.assertRaisesRegex(deliveryctl.DeliveryError, "changed while the report"):
                deliveryctl.build_report(
                    self.root,
                    run_id=run_id,
                    kind="planning-complete",
                    summary="Must not report a stale planning state.",
                    actor="integration-test",
                    planning_external_bindings=copy.deepcopy(self.external),
                )

        self.assertEqual("CLARIFYING", self._state()["phase"])
        self.assertFalse((self.root / "delivery-docs" / "state" / "runs" / run_id).exists())
        self.assertFalse(
            (self.root / "delivery-docs" / "verification" / "runs" / run_id).exists()
        )


if __name__ == "__main__":
    unittest.main()
