#!/usr/bin/env python3
"""Standard-library regression and hardening tests for deliveryctl."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import deliveryctl


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


def _remove_tree(path: Path) -> None:
    # Git marks pack and object files read-only on Windows; make them writable
    # before the removal callback retries.
    def make_writable(function, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        function(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable)
    else:
        shutil.rmtree(path, onerror=make_writable)


SOURCE_DIGEST = "a" * 64
CONFIG_DIGEST = "b" * 64
TEST_SUITE_DIGEST = "c" * 64
CHANGE_ID = "fixture-slice"


class DeliveryCtlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="deliveryctl-test-")
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def init(self, *, mode: str = "greenfield", openspec: bool = False) -> dict:
        return deliveryctl.initialize_project(
            self.root,
            mode=mode,
            project_id="fixture",
            with_openspec_config=openspec,
        )

    @unittest.skipUnless(SYMLINKS_SUPPORTED, "symlink creation requires elevated privileges on Windows")
    def test_canonical_root_rejects_symbolic_root_and_symbolic_parent(self) -> None:
        real_parent = self.root / "real-parent"
        real_root = real_parent / "project"
        real_root.mkdir(parents=True)
        self.assertEqual(real_root, deliveryctl.canonical_root(real_root))

        root_link = self.root / "project-link"
        root_link.symlink_to(real_root, target_is_directory=True)
        with self.assertRaisesRegex(deliveryctl.DeliveryError, "non-symbolic directory"):
            deliveryctl.canonical_root(root_link)

        parent_link = self.root / "parent-link"
        parent_link.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaisesRegex(deliveryctl.DeliveryError, "symbolic-link parent"):
            deliveryctl.canonical_root(parent_link / "project")

    def read_state(self) -> dict:
        return deliveryctl.read_json(self.root / "delivery-docs" / "state" / "state.json")

    def set_context(self, **updates: object) -> dict:
        return deliveryctl.set_context(
            self.root,
            expected_revision=self.read_state()["revision"],
            actor="test",
            reason="fixture context",
            **updates,
        )

    def prepare_context(self) -> None:
        self.set_context(source_digest=SOURCE_DIGEST, active_change=CHANGE_ID)

    def enable_quality_transitions(self) -> None:
        path = self.root / "delivery-docs" / "state" / "manifest.json"
        manifest = deliveryctl.read_json(path)
        manifest["policies"]["quality_transitions_enabled"] = True
        deliveryctl.atomic_write_json(path, manifest)

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode:
            self.fail(f"git {' '.join(arguments)} failed: {result.stderr}")
        return result.stdout.strip()

    def initialize_git(self) -> str:
        if shutil.which("git") is None:
            self.skipTest("git is required for evidence tests")
        self.git("init", "-q")
        self.git("config", "user.name", "Delivery Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.root / "app.txt").write_text("version 1\n", encoding="utf-8", newline="")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "fixture baseline")
        return self.git("rev-parse", "HEAD")

    def initialize_git_at(self, root: Path) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is required for evidence tests")
        result = subprocess.run(
            ["git", "init", "-q", str(root)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode:
            self.fail(f"git init failed: {result.stderr}")

    def write_passed_gate(
        self,
        commit: str,
        *,
        change_id: str = CHANGE_ID,
        source_digest: str = SOURCE_DIGEST,
        artifacts: list | None = None,
        config_digest: str = CONFIG_DIGEST,
        test_suite_digest: str = TEST_SUITE_DIGEST,
    ) -> dict:
        if artifacts is None:
            artifact = self.root / "delivery-docs" / "verification" / "runs" / "FIXTURE" / "evidence.txt"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("verified\n", encoding="utf-8", newline="")
            artifacts = [
                {
                    "path": str(artifact.relative_to(self.root)),
                    "sha256": deliveryctl.sha256_file(artifact),
                    "kind": "test-report",
                }
            ]
        now = deliveryctl.isoformat()
        gate = {
            "schema_version": deliveryctl.SCHEMA_VERSION,
            "harness_version": deliveryctl.HARNESS_VERSION,
            "change_id": change_id,
            "source_digest": source_digest,
            "commit": commit,
            "environment": "test",
            "config_digest": config_digest,
            "test_suite_digest": test_suite_digest,
            "started_at": now,
            "finished_at": now,
            "results": {"lint": "passed", "unit": "passed", "targeted_e2e": "passed"},
            "artifacts": artifacts,
            "status": "passed",
        }
        deliveryctl.atomic_write_json(self.root / "delivery-docs" / "state" / "gate-manifest.json", gate)
        return gate

    def transition(self, target: str, reason: str = "fixture") -> dict:
        return deliveryctl.transition_state(
            self.root,
            target=target,
            reason=reason,
            actor="test",
            expected_revision=None,
            next_action=None,
            milestone=None,
            change=None,
            approval_ref="APPROVAL-1" if target.replace("-", "_").upper() in deliveryctl.HUMAN_APPROVAL_PHASES else None,
        )

    def advance_to_system_verifying(self) -> None:
        milestone_patch = mock.patch.object(deliveryctl, "IMPLEMENTED_MILESTONE", 5)
        milestone_patch.start()
        self.addCleanup(milestone_patch.stop)
        # These tests isolate future M4/M5 quality-gate semantics. M3 planning
        # has its own integration suite and must not be bypassed in production.
        planning_patch = mock.patch.object(deliveryctl, "PLANNING_GATED_PHASES", set())
        planning_patch.start()
        self.addCleanup(planning_patch.stop)
        for phase in ("BASELINING", "READY", "EXECUTING", "SYSTEM_VERIFYING"):
            self.transition(phase)

    def prepare_quality_fixture(self) -> str:
        self.init()
        commit = self.initialize_git()
        self.prepare_context()
        self.advance_to_system_verifying()
        self.write_passed_gate(commit)
        return commit

    def test_init_is_idempotent_and_never_overwrites(self) -> None:
        first = self.init()
        self.assertTrue(first["initialized"])
        event_path = self.root / "delivery-docs" / "state" / "events.jsonl"
        before = event_path.read_bytes()
        (self.root / "openspec").mkdir()
        config = self.root / "openspec" / "config.yaml"
        config.write_text("user-owned: true\n", encoding="utf-8", newline="")

        second = self.init()

        self.assertEqual(before, event_path.read_bytes())
        self.assertEqual("user-owned: true\n", config.read_text(encoding="utf-8"))
        self.assertEqual([], second["created"])
        with self.assertRaisesRegex(deliveryctl.DeliveryError, "no longer writes OpenSpec config"):
            self.init(openspec=True)

    def test_all_recoverable_templates_have_harness_version(self) -> None:
        self.init()
        for name in (
            "manifest.json",
            "state.json",
            "questions.json",
            "assumptions.json",
            "gate-manifest.json",
        ):
            self.assertEqual(
                deliveryctl.HARNESS_VERSION,
                deliveryctl.read_json(self.root / "delivery-docs" / "state" / name)["harness_version"],
            )
        event = deliveryctl.read_jsonl(self.root / "delivery-docs" / "state" / "events.jsonl")[0]
        self.assertEqual(deliveryctl.HARNESS_VERSION, event["harness_version"])

    def test_inspect_routes_greenfield_brownfield_and_resume(self) -> None:
        self.assertEqual("greenfield", deliveryctl.inspect_project(self.root)["recommended_mode"])
        (self.root / "package.json").write_text("{}\n", encoding="utf-8", newline="")
        (self.root / "src").mkdir()
        (self.root / "src" / "app.js").write_text("export const app = true;\n", encoding="utf-8", newline="")
        self.assertEqual("brownfield", deliveryctl.inspect_project(self.root)["recommended_mode"])
        self.init(mode="brownfield")
        inspection = deliveryctl.inspect_project(self.root)
        self.assertEqual("resume", inspection["recommended_mode"])
        self.assertTrue(inspection["delivery"]["validation"]["valid"])

    def test_invalid_transition_changes_nothing(self) -> None:
        self.init()
        state_path = self.root / "delivery-docs" / "state" / "state.json"
        events_path = self.root / "delivery-docs" / "state" / "events.jsonl"
        state_before = state_path.read_bytes()
        events_before = events_path.read_bytes()

        with self.assertRaises(deliveryctl.DeliveryError):
            self.transition("ACCEPTANCE_READY")

        self.assertEqual(state_before, state_path.read_bytes())
        self.assertEqual(events_before, events_path.read_bytes())

    def test_pause_and_resume_are_bounded_to_prior_phase(self) -> None:
        self.init()
        self.transition("BASELINING")
        self.transition("PAUSED")
        self.assertEqual("BASELINING", self.read_state()["resume_phase"])
        with self.assertRaises(deliveryctl.DeliveryError):
            self.transition("EXECUTING")
        self.transition("BASELINING")
        self.assertIsNone(self.read_state()["resume_phase"])

    def test_m3_phase_ceiling_blocks_future_transition_and_replayed_state(self) -> None:
        self.init()
        self.transition("BASELINING")
        self.transition("CLARIFYING")
        state_before = (self.root / "delivery-docs" / "state" / "state.json").read_bytes()
        events_before = (self.root / "delivery-docs" / "state" / "events.jsonl").read_bytes()

        with self.assertRaisesRegex(deliveryctl.DeliveryError, "milestone M4"):
            self.transition("EXECUTING")

        self.assertEqual(state_before, (self.root / "delivery-docs" / "state" / "state.json").read_bytes())
        self.assertEqual(events_before, (self.root / "delivery-docs" / "state" / "events.jsonl").read_bytes())

        state = self.read_state()
        state["phase"] = "EXECUTING"
        deliveryctl.atomic_write_json(self.root / "delivery-docs" / "state" / "state.json", state)
        validation = deliveryctl.validate_project(self.root, check_fresh_gate=False)
        self.assertFalse(validation["valid"])
        self.assertTrue(any("milestone M4" in item for item in validation["errors"]))

    def test_set_context_is_evented_locked_and_requires_a_change(self) -> None:
        self.init()
        with self.assertRaisesRegex(deliveryctl.DeliveryError, "at least one field"):
            deliveryctl.set_context(
                self.root,
                expected_revision=0,
                actor="test",
                reason="empty",
            )
        result = deliveryctl.set_context(
            self.root,
            expected_revision=0,
            actor="test",
            reason="register inputs",
            source_digest=SOURCE_DIGEST,
            active_milestone="M1",
            active_change=CHANGE_ID,
            next_action="baseline",
            blocking_questions=["Q-INPUT"],
            pending_decisions=["D-ARCH"],
        )
        self.assertEqual(1, result["revision"])
        self.assertEqual("INTAKE", result["phase"])
        event = deliveryctl.read_jsonl(self.root / "delivery-docs" / "state" / "events.jsonl")[-1]
        self.assertEqual("state.context_updated", event["event_type"])
        self.assertEqual(event["details"]["from"], event["details"]["to"])
        self.assertEqual(deliveryctl.canonical_sha256(result["state"]), event["state_digest"])
        with self.assertRaisesRegex(deliveryctl.DeliveryError, "revision changed"):
            deliveryctl.set_context(
                self.root,
                expected_revision=0,
                actor="test",
                reason="stale writer",
                next_action="wrong",
            )

    def test_ready_and_executing_require_resolved_context(self) -> None:
        self.init()
        with mock.patch.object(deliveryctl, "IMPLEMENTED_MILESTONE", 4), mock.patch.object(
            deliveryctl, "PLANNING_GATED_PHASES", set()
        ):
            self.set_context(blocking_questions=["Q-BLOCK"], source_digest=SOURCE_DIGEST)
            self.transition("BASELINING")
            with self.assertRaisesRegex(deliveryctl.DeliveryError, "blocking questions"):
                self.transition("READY")
            self.set_context(blocking_questions=[])
            self.transition("READY")
            with self.assertRaisesRegex(deliveryctl.DeliveryError, "active_change"):
                self.transition("EXECUTING")
            self.set_context(active_change=CHANGE_ID)
            self.transition("EXECUTING")

    def test_ready_requires_source_digest(self) -> None:
        self.init()
        self.transition("BASELINING")
        with mock.patch.object(deliveryctl, "IMPLEMENTED_MILESTONE", 3), mock.patch.object(
            deliveryctl, "PLANNING_GATED_PHASES", set()
        ):
            with self.assertRaisesRegex(deliveryctl.DeliveryError, "source_digest"):
                self.transition("READY")

    def test_state_event_replay_detects_phase_tampering_without_revision_change(self) -> None:
        self.init()
        state_path = self.root / "delivery-docs" / "state" / "state.json"
        state = deliveryctl.read_json(state_path)
        state["phase"] = "CLARIFYING"
        deliveryctl.atomic_write_json(state_path, state)

        result = deliveryctl.validate_project(self.root, check_fresh_gate=False)

        self.assertFalse(result["valid"])
        self.assertTrue(any("state digest does not match" in item for item in result["errors"]))

    def test_event_chain_detects_revision_and_from_to_tampering(self) -> None:
        self.init()
        self.transition("BASELINING")
        path = self.root / "delivery-docs" / "state" / "events.jsonl"
        events = deliveryctl.read_jsonl(path)
        events[-1]["details"]["from"] = "CLARIFYING"
        deliveryctl.atomic_write_text(
            path,
            "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in events),
        )
        result = deliveryctl.validate_project(self.root, check_fresh_gate=False)
        self.assertFalse(result["valid"])
        self.assertTrue(any("details.from" in item for item in result["errors"]))

    def test_event_replay_rejects_self_consistent_illegal_transition(self) -> None:
        self.init()
        self.transition("BASELINING")
        state_path = self.root / "delivery-docs" / "state" / "state.json"
        events_path = self.root / "delivery-docs" / "state" / "events.jsonl"
        events = deliveryctl.read_jsonl(events_path)
        snapshot = dict(events[-1]["state_snapshot"])
        snapshot["phase"] = "SYSTEM_VERIFYING"
        events[-1]["state_snapshot"] = snapshot
        events[-1]["state_digest"] = deliveryctl.canonical_sha256(snapshot)
        events[-1]["details"]["to"] = "SYSTEM_VERIFYING"
        deliveryctl.atomic_write_json(state_path, snapshot)
        deliveryctl.atomic_write_text(
            events_path,
            "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in events),
        )

        result = deliveryctl.validate_project(self.root, check_fresh_gate=False)

        self.assertFalse(result["valid"])
        self.assertTrue(any("unavailable milestone M5" in item for item in result["errors"]))

    def test_quality_transitions_are_disabled_before_gate_evaluation(self) -> None:
        self.prepare_quality_fixture()
        with self.assertRaisesRegex(deliveryctl.DeliveryError, "not implemented"):
            self.transition("ACCEPTANCE_READY")

        self.enable_quality_transitions()
        validation = deliveryctl.validate_project(self.root, check_fresh_gate=False)
        self.assertFalse(validation["valid"])
        self.assertTrue(any("cannot unlock" in item for item in validation["errors"]))

    def test_enabled_quality_transition_accepts_strict_bound_gate(self) -> None:
        commit = self.prepare_quality_fixture()
        with mock.patch.object(deliveryctl, "QUALITY_TRANSITIONS_IMPLEMENTED", True):
            self.enable_quality_transitions()
            checked = deliveryctl.check_gate_file(self.root, None, allow_dirty=False, max_age=None)
            self.assertTrue(checked["passed"], checked)
            outcome = self.transition("acceptance-ready")
        self.assertEqual("ACCEPTANCE_READY", outcome["to"])
        self.assertEqual(commit, outcome["state"]["last_verified_commit"])

    def test_gate_rejects_bad_digests_change_source_and_url_only_evidence(self) -> None:
        commit = self.prepare_quality_fixture()
        cases = [
            {"config_digest": "not-a-digest"},
            {"change_id": "other-change"},
            {"source_digest": "d" * 64},
            {
                "artifacts": [
                    {"url": "https://example.invalid/report", "sha256": "e" * 64}
                ]
            },
        ]
        for case in cases:
            with self.subTest(case=case):
                self.write_passed_gate(commit, **case)
                checked = deliveryctl.check_gate_file(self.root, None, allow_dirty=False, max_age=None)
                self.assertFalse(checked["passed"])
        self.write_passed_gate(commit)
        self.assertTrue(
            deliveryctl.check_gate_file(self.root, None, allow_dirty=False, max_age=None)["passed"]
        )

    def test_gate_requires_project_state_change_and_source_context(self) -> None:
        self.init()
        commit = self.initialize_git()
        self.write_passed_gate(commit)

        checked = deliveryctl.check_gate_file(self.root, None, allow_dirty=False, max_age=None)

        self.assertFalse(checked["passed"])
        self.assertTrue(any("active Change" in item for item in checked["errors"]))
        self.assertTrue(any("source digest" in item for item in checked["errors"]))

    def test_stale_or_dirty_gate_is_rejected(self) -> None:
        commit = self.prepare_quality_fixture()
        (self.root / "app.txt").write_text("uncommitted behavior\n", encoding="utf-8", newline="")

        dirty = deliveryctl.check_gate_file(self.root, None, allow_dirty=False, max_age=None)
        self.assertFalse(dirty["passed"])
        self.assertTrue(any("worktree has gate-relevant changes" in item for item in dirty["errors"]))

        self.git("add", "app.txt")
        self.git("commit", "-q", "-m", "change behavior")
        stale = deliveryctl.check_gate_file(self.root, None, allow_dirty=False, max_age=None)
        self.assertFalse(stale["passed"])
        self.assertTrue(any("gate commit is stale" in item for item in stale["errors"]))

    def test_git_status_failure_fails_gate_closed(self) -> None:
        self.prepare_quality_fixture()
        real_run_git = deliveryctl.run_git

        def failing_status(root: Path, arguments: list[str]) -> subprocess.CompletedProcess:
            if arguments and arguments[0] == "status":
                return subprocess.CompletedProcess(
                    ["git", *arguments],
                    128,
                    stdout="",
                    stderr="simulated status failure",
                )
            return real_run_git(root, arguments)

        with mock.patch.object(deliveryctl, "run_git", side_effect=failing_status):
            checked = deliveryctl.check_gate_file(self.root, None, allow_dirty=False, max_age=None)
        self.assertFalse(checked["passed"])
        self.assertTrue(any("git status failed" in item for item in checked["errors"]))

    def test_git_status_uses_nul_safe_paths(self) -> None:
        self.initialize_git()
        names = ["space name.txt", "测试.txt"]
        if os.name != "nt":
            # Windows forbids < > : " | ? * and newline characters in file names.
            names.extend(['quote"name.txt', "line\nbreak.txt", "a -> b.txt"])
        for name in names:
            (self.root / name).write_text("dirty\n", encoding="utf-8", newline="")

        paths = deliveryctl._git_dirty_paths(self.root)

        for name in names:
            self.assertIn(name, paths)

    def test_corrupt_git_metadata_is_unknown_not_non_repository(self) -> None:
        (self.root / ".git").mkdir()

        result = deliveryctl.git_info(self.root)

        self.assertIsNone(result["repository"])
        self.assertEqual("error", result["status"])
        self.assertFalse(result["clean_for_gate"])
        self.assertIn("cannot be inspected", result["error"])

    def test_normal_internal_git_metadata_passes_preflight(self) -> None:
        self.initialize_git()

        risks = deliveryctl._git_metadata_safety_risks(self.root)
        result = deliveryctl.git_info(self.root)

        self.assertEqual([], risks)
        self.assertTrue(result["repository"])
        self.assertEqual("clean", result["status"])
        self.assertTrue(result["clean_for_gate"])

    def test_relative_path_cannot_select_a_project_local_git_executable(self) -> None:
        self.initialize_git()
        actual_git = shutil.which("git")
        if actual_git is None:
            self.skipTest("git is required for evidence tests")
        tripwire = self.root / "M2_PROJECT_GIT_TRIPWIRE"
        local_git = self.root / "git"
        local_git.write_text(
            "#!/bin/sh\nprintf invoked > M2_PROJECT_GIT_TRIPWIRE\nexit 99\n",
            encoding="utf-8", newline="",
        )
        local_git.chmod(0o755)
        supplied_path = os.pathsep.join((".", str(Path(actual_git).resolve().parent)))

        with mock.patch.dict(os.environ, {"PATH": supplied_path}, clear=False):
            result = deliveryctl.git_info(self.root)

        self.assertTrue(result["repository"])
        self.assertFalse(tripwire.exists())

    @unittest.skipUnless(SYMLINKS_SUPPORTED, "symlink creation requires elevated privileges on Windows")
    def test_external_path_symlink_cannot_resolve_to_project_git(self) -> None:
        self.initialize_git()
        local_git = self.root / "project-git"
        tripwire = self.root / "M2_SYMLINKED_GIT_TRIPWIRE"
        local_git.write_text(
            "#!/bin/sh\nprintf invoked > M2_SYMLINKED_GIT_TRIPWIRE\nexit 99\n",
            encoding="utf-8", newline="",
        )
        local_git.chmod(0o755)
        with tempfile.TemporaryDirectory(prefix="deliveryctl-external-path-") as external_text:
            external = Path(external_text).resolve()
            (external / "git").symlink_to(local_git)
            with mock.patch.dict(os.environ, {"PATH": str(external)}, clear=False):
                with self.assertRaisesRegex(deliveryctl.DeliveryError, "resolves inside the project"):
                    deliveryctl.run_git(self.root, ["rev-parse", "--show-toplevel"])

        self.assertFalse(tripwire.exists())

    def test_git_scope_mismatch_stops_after_single_top_level_probe(self) -> None:
        self.initialize_git()
        outside = self.root.parent / "outside-git-root"
        probe = subprocess.CompletedProcess(
            ["git", "rev-parse", "--show-toplevel"],
            0,
            stdout=str(outside) + "\n",
            stderr="",
        )

        with mock.patch.object(deliveryctl, "run_git", return_value=probe) as run_git, mock.patch.object(
            deliveryctl, "_git_attribute_filter_risks"
        ) as attributes, mock.patch.object(deliveryctl, "_git_dirty_paths") as dirty, mock.patch.object(
            deliveryctl, "_git_commit"
        ) as commit:
            result = deliveryctl.git_info(self.root)

        run_git.assert_called_once_with(self.root, ["rev-parse", "--show-toplevel"])
        attributes.assert_not_called()
        dirty.assert_not_called()
        commit.assert_not_called()
        self.assertEqual("nested", result["status"])
        self.assertTrue(result["nested_project"])
        self.assertIsNone(result["commit"])
        self.assertFalse(result["clean_for_gate"])

    def test_unsafe_git_metadata_blocks_before_any_git_subprocess(self) -> None:
        outside = self.root / "outside-metadata"
        outside.write_text("must not be consulted\n", encoding="utf-8", newline="")

        def append_config(root: Path, text: str) -> None:
            config = root / ".git" / "config"
            config.write_text(config.read_text(encoding="utf-8") + text, encoding="utf-8", newline="")

        def add_alternates(root: Path) -> None:
            path = root / ".git" / "objects" / "info" / "alternates"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(outside) + "\n", encoding="utf-8", newline="")

        def add_replace_ref(root: Path) -> None:
            path = root / ".git" / "refs" / "replace" / ("a" * 40)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("b" * 40 + "\n", encoding="utf-8", newline="")

        def add_internal_symlink(root: Path) -> None:
            os.symlink(outside, root / ".git" / "hooks" / "outside-link")

        cases = {
            "config-include": lambda root: append_config(
                root, '\n[include]\n\tpath = "../outside-metadata"\n'
            ),
            "config-includeif": lambda root: append_config(
                root, '\n[includeIf "gitdir:/**"]\n\tpath = "../outside-metadata"\n'
            ),
            "core-worktree": lambda root: append_config(
                root, "\n[core]\n\tworktree = ../outside-metadata\n"
            ),
            "core-bare": lambda root: append_config(root, "\n[core]\n\tbare = true\n"),
            "extensions-worktree-config": lambda root: append_config(
                root, "\n[extensions]\n\tworktreeConfig = true\n"
            ),
            "commondir": lambda root: (root / ".git" / "commondir").write_text(
                "../outside-metadata\n", encoding="utf-8"
            ),
            "alternates": add_alternates,
            "internal-symlink": add_internal_symlink,
            "replace-ref": add_replace_ref,
            "packed-replace-ref": lambda root: (root / ".git" / "packed-refs").write_text(
                f"{'b' * 40} refs/replace/{'a' * 40}\n", encoding="utf-8"
            ),
            "grafts": lambda root: (root / ".git" / "info" / "grafts").write_text(
                "a" * 40 + "\n", encoding="utf-8"
            ),
        }
        if not SYMLINKS_SUPPORTED:
            # Symlink creation requires elevated privileges on Windows.
            del cases["internal-symlink"]
        for name, mutate in cases.items():
            with self.subTest(name=name):
                root = self.root / name
                root.mkdir()
                self.initialize_git_at(root)
                mutate(root)
                with mock.patch.object(deliveryctl.subprocess, "Popen") as popen:
                    result = deliveryctl.git_info(root.resolve())
                    with self.assertRaisesRegex(deliveryctl.DeliveryError, "unsafe Git metadata"):
                        deliveryctl.run_git(root.resolve(), ["status", "--porcelain=v1"])
                popen.assert_not_called()
                self.assertIsNone(result["repository"])
                self.assertEqual("error", result["status"])
                self.assertFalse(result["clean_for_gate"])
                self.assertTrue(result["safety_risks"])

        self.assertEqual("must not be consulted\n", outside.read_text(encoding="utf-8"))

    @unittest.skipUnless(SYMLINKS_SUPPORTED, "symlink creation requires elevated privileges on Windows")
    def test_metadata_parent_symlink_stops_before_deeper_path_checks(self) -> None:
        self.initialize_git()
        outside = self.root / "outside-objects"
        (outside / "info").mkdir(parents=True)
        (outside / "info" / "alternates").write_text("must-not-be-inspected\n", encoding="utf-8", newline="")
        _remove_tree(self.root / ".git" / "objects")
        (self.root / ".git" / "objects").symlink_to(outside, target_is_directory=True)

        risks = deliveryctl._git_metadata_safety_risks(self.root)

        self.assertTrue(any("symbolic link" in item for item in risks))
        self.assertFalse(any("object alternates" in item for item in risks))

    def test_closed_requires_fresh_gate_except_after_aborted(self) -> None:
        self.prepare_quality_fixture()
        with mock.patch.object(deliveryctl, "QUALITY_TRANSITIONS_IMPLEMENTED", True), mock.patch.object(
            deliveryctl, "IMPLEMENTED_MILESTONE", 7
        ):
            self.enable_quality_transitions()
            self.transition("ACCEPTANCE_READY")
            self.transition("ACCEPTED")
            (self.root / "app.txt").write_text("new version\n", encoding="utf-8", newline="")
            self.git("add", "app.txt")
            self.git("commit", "-q", "-m", "stale accepted evidence")
            with self.assertRaisesRegex(deliveryctl.DeliveryError, "quality gate rejected"):
                self.transition("CLOSED")

        other = tempfile.TemporaryDirectory(prefix="deliveryctl-abort-")
        try:
            root = Path(other.name).resolve()
            deliveryctl.initialize_project(
                root,
                mode="greenfield",
                project_id="aborted",
                with_openspec_config=False,
            )
            deliveryctl.transition_state(
                root,
                target="ABORTED",
                reason="cancel",
                actor="test",
                expected_revision=None,
                next_action=None,
                milestone=None,
                change=None,
                approval_ref=None,
            )
            closed = deliveryctl.transition_state(
                root,
                target="CLOSED",
                reason="close aborted work",
                actor="test",
                expected_revision=None,
                next_action=None,
                milestone=None,
                change=None,
                approval_ref=None,
            )
            self.assertEqual("CLOSED", closed["to"])
        finally:
            other.cleanup()

    def test_report_prerequisites_and_stale_status(self) -> None:
        self.init()
        with self.assertRaisesRegex(deliveryctl.DeliveryError, "requires phase"):
            deliveryctl.build_report(
                self.root,
                run_id="RUN-EARLY",
                kind="change-complete",
                summary="too early",
                actor="test",
            )

        commit = self.initialize_git()
        self.prepare_context()
        self.advance_to_system_verifying()
        self.write_passed_gate(commit)
        completed = deliveryctl.build_report(
            self.root,
            run_id="RUN-CHANGE",
            kind="change-complete",
            summary="verified change",
            actor="test",
        )
        self.assertEqual("passed", completed["gate_status"])
        with self.assertRaisesRegex(deliveryctl.DeliveryError, "requires phase"):
            deliveryctl.build_report(
                self.root,
                run_id="RUN-MILESTONE",
                kind="milestone-complete",
                summary="too early",
                actor="test",
            )

        (self.root / "app.txt").write_text("version 2\n", encoding="utf-8", newline="")
        self.git("add", "app.txt")
        self.git("commit", "-q", "-m", "stale report gate")
        checkpoint = deliveryctl.build_report(
            self.root,
            run_id="RUN-STALE",
            kind="checkpoint",
            summary="progress",
            actor="test",
        )
        self.assertEqual("stale", checkpoint["gate_status"])
        result = deliveryctl.read_json(Path(checkpoint["result"]))
        self.assertEqual("stale", result["gate"]["status"])
        self.assertEqual("passed", result["gate"]["manifest_status"])
        report_text = Path(checkpoint["report"]).read_text(encoding="utf-8")
        self.assertIn("stale", report_text)

    def test_checkpoint_and_blocked_reports_remain_available_and_non_overwriting(self) -> None:
        self.init()
        checkpoint = deliveryctl.build_report(
            self.root,
            run_id="RUN-CHECKPOINT",
            kind="checkpoint",
            summary="checkpoint",
            actor="test",
        )
        blocked = deliveryctl.build_report(
            self.root,
            run_id="RUN-BLOCKED",
            kind="blocked",
            summary="needs input",
            actor="test",
        )
        self.assertTrue(Path(checkpoint["result"]).is_file())
        self.assertTrue(Path(blocked["report"]).is_file())
        with self.assertRaises(deliveryctl.DeliveryError):
            deliveryctl.build_report(
                self.root,
                run_id="RUN-CHECKPOINT",
                kind="checkpoint",
                summary="overwrite",
                actor="test",
            )
        self.assertEqual(
            "report.generated",
            deliveryctl.read_jsonl(self.root / "delivery-docs" / "state" / "events.jsonl")[-1]["event_type"],
        )

    @unittest.skipUnless(SYMLINKS_SUPPORTED, "symlink creation requires elevated privileges on Windows")
    def test_controlled_writes_reject_symlink_paths(self) -> None:
        outside = tempfile.TemporaryDirectory(prefix="deliveryctl-outside-")
        try:
            outside_root = Path(outside.name).resolve()
            (self.root / "delivery-docs").mkdir(exist_ok=True)
            (self.root / "delivery-docs" / "state").symlink_to(outside_root, target_is_directory=True)
            with self.assertRaisesRegex(deliveryctl.DeliveryError, "symbolic link"):
                self.init()
            self.assertEqual([], list(outside_root.iterdir()))
        finally:
            outside.cleanup()

    @unittest.skipUnless(SYMLINKS_SUPPORTED, "symlink creation requires elevated privileges on Windows")
    def test_report_and_openspec_paths_reject_symlink_components(self) -> None:
        outside = tempfile.TemporaryDirectory(prefix="deliveryctl-outside-")
        try:
            outside_root = Path(outside.name).resolve()
            self.init()
            (self.root / "delivery-docs").mkdir(exist_ok=True)
            (self.root / "delivery-docs" / "verification").symlink_to(outside_root, target_is_directory=True)
            with self.assertRaisesRegex(deliveryctl.DeliveryError, "symbolic link"):
                deliveryctl.build_report(
                    self.root,
                    run_id="RUN-SYMLINK",
                    kind="checkpoint",
                    summary="unsafe",
                    actor="test",
                )
        finally:
            outside.cleanup()

        fresh = tempfile.TemporaryDirectory(prefix="deliveryctl-openspec-")
        outside_two = tempfile.TemporaryDirectory(prefix="deliveryctl-outside-")
        try:
            root = Path(fresh.name).resolve()
            (root / "openspec").symlink_to(Path(outside_two.name), target_is_directory=True)
            with self.assertRaisesRegex(deliveryctl.DeliveryError, "unsafe"):
                deliveryctl.plan_openspec_config(root, candidate_path=None)
            self.assertFalse((root / "delivery-docs" / "state").exists())
        finally:
            fresh.cleanup()
            outside_two.cleanup()

    def test_interrupted_state_event_write_recovers_exactly_once(self) -> None:
        self.init()
        real_append = deliveryctl.controlled_append_jsonl
        with mock.patch.object(
            deliveryctl,
            "controlled_append_jsonl",
            side_effect=OSError("simulated interrupted append"),
        ):
            with self.assertRaisesRegex(deliveryctl.DeliveryError, "run deliveryctl recover"):
                self.set_context(source_digest=SOURCE_DIGEST)
        self.assertTrue((self.root / "delivery-docs" / "state" / "transaction.json").is_file())
        self.assertFalse(deliveryctl.validate_project(self.root, check_fresh_gate=False)["valid"])

        with mock.patch.object(deliveryctl, "controlled_append_jsonl", side_effect=real_append):
            recovered = deliveryctl.recover_project(self.root)
        self.assertTrue(recovered["recovered"])
        self.assertTrue(recovered["validation"]["valid"])
        events = deliveryctl.read_jsonl(self.root / "delivery-docs" / "state" / "events.jsonl")
        context_events = [event for event in events if event["event_type"] == "state.context_updated"]
        self.assertEqual(1, len(context_events))
        self.assertEqual(SOURCE_DIGEST, self.read_state()["source_digest"])

    def test_recovery_repairs_only_matching_partial_final_event(self) -> None:
        self.init()
        with mock.patch.object(
            deliveryctl,
            "controlled_append_jsonl",
            side_effect=OSError("simulated interrupted append"),
        ):
            with self.assertRaises(deliveryctl.DeliveryError):
                self.set_context(source_digest=SOURCE_DIGEST)
        journal = deliveryctl.read_json(self.root / "delivery-docs" / "state" / "transaction.json")
        encoded = json.dumps(journal["event"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with (self.root / "delivery-docs" / "state" / "events.jsonl").open("ab") as handle:
            handle.write(encoded[: len(encoded) // 2])
        recovered = deliveryctl.recover_project(self.root)
        self.assertTrue(recovered["validation"]["valid"])
        event_ids = [
            event["event_id"]
            for event in deliveryctl.read_jsonl(self.root / "delivery-docs" / "state" / "events.jsonl")
        ]
        self.assertEqual(1, event_ids.count(journal["event"]["event_id"]))

    def test_recovery_rejects_future_phase_before_any_write(self) -> None:
        self.init()
        previous = self.read_state()
        next_state = dict(previous)
        next_state.update(
            {
                "phase": "CLARIFYING",
                "revision": previous["revision"] + 1,
                "updated_at": deliveryctl.isoformat(),
                "next_action": "future milestone",
            }
        )
        events = deliveryctl.read_jsonl(self.root / "delivery-docs" / "state" / "events.jsonl")
        event = deliveryctl.event_record(
            sequence=deliveryctl.next_event_sequence(events),
            event_type="state.transitioned",
            actor="test",
            state_revision=next_state["revision"],
            details={"from": previous["phase"], "to": next_state["phase"], "reason": "forged"},
            state_snapshot=next_state,
        )
        journal = deliveryctl._transaction_journal(previous, next_state, event)
        deliveryctl.atomic_write_json(self.root / "delivery-docs" / "state" / "transaction.json", journal)
        paths = [
            self.root / "delivery-docs" / "state" / "state.json",
            self.root / "delivery-docs" / "state" / "events.jsonl",
            self.root / "delivery-docs" / "state" / "transaction.json",
        ]
        before = {path: path.read_bytes() for path in paths}

        with mock.patch.dict(deliveryctl.PHASE_MINIMUM_MILESTONE, {"CLARIFYING": 4}):
            with self.assertRaisesRegex(deliveryctl.DeliveryError, "unimplemented phase CLARIFYING"):
                deliveryctl.recover_project(self.root)

        self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_recovery_rejects_illegal_implemented_transition_before_write(self) -> None:
        self.init()
        previous = self.read_state()
        next_state = dict(previous)
        next_state.update(
            {
                "phase": "CLOSED",
                "revision": previous["revision"] + 1,
                "updated_at": deliveryctl.isoformat(),
                "next_action": "forged close",
            }
        )
        event = deliveryctl.event_record(
            sequence=deliveryctl.next_event_sequence(
                deliveryctl.read_jsonl(self.root / "delivery-docs" / "state" / "events.jsonl")
            ),
            event_type="state.transitioned",
            actor="test",
            state_revision=next_state["revision"],
            details={"from": "INTAKE", "to": "CLOSED", "reason": "forged"},
            state_snapshot=next_state,
        )
        deliveryctl.atomic_write_json(
            self.root / "delivery-docs" / "state" / "transaction.json",
            deliveryctl._transaction_journal(previous, next_state, event),
        )
        paths = [
            self.root / "delivery-docs" / "state" / "state.json",
            self.root / "delivery-docs" / "state" / "events.jsonl",
            self.root / "delivery-docs" / "state" / "transaction.json",
        ]
        before = {path: path.read_bytes() for path in paths}

        with self.assertRaisesRegex(deliveryctl.DeliveryError, "illegal transition INTAKE -> CLOSED"):
            deliveryctl.recover_project(self.root)

        self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_baseline_commit_rejects_git_option_injection(self) -> None:
        self.init()
        output = self.root / "M2_GIT_OPTION_OUTPUT"

        with self.assertRaisesRegex(deliveryctl.DeliveryError, "full 40- or 64-character"):
            self.set_context(baseline_commit=f"--output={output}")

        self.assertFalse(output.exists())
        self.assertIsNone(self.read_state()["baseline_commit"])

    def test_write_all_handles_short_os_writes(self) -> None:
        with mock.patch.object(deliveryctl.os, "write", side_effect=[2, 3]) as patched:
            deliveryctl._write_all(99, b"abcde")
        self.assertEqual(2, patched.call_count)
        self.assertEqual(b"cde", bytes(patched.call_args_list[1].args[1]))

    def test_stale_process_lock_is_recovered_without_manual_deletion(self) -> None:
        self.init()
        lock = self.root / "delivery-docs" / "state" / ".deliveryctl.lock"
        lock.write_text("pid=99999999 created_at=2026-01-01T00:00:00Z\n", encoding="utf-8", newline="")
        result = self.set_context(source_digest=SOURCE_DIGEST)
        self.assertTrue(result["updated"])
        self.assertFalse(lock.exists())

    def test_cli_normalizes_kebab_case_quality_phase(self) -> None:
        self.prepare_quality_fixture()
        output = io.StringIO()
        with mock.patch.object(deliveryctl, "QUALITY_TRANSITIONS_IMPLEMENTED", True):
            self.enable_quality_transitions()
            with contextlib.redirect_stdout(output):
                exit_code = deliveryctl.main(
                    [
                        "transition",
                        "--project-root",
                        str(self.root),
                        "--to",
                        "acceptance-ready",
                        "--reason",
                        "verified",
                        "--actor",
                        "test",
                    ]
                )
        self.assertEqual(0, exit_code, output.getvalue())
        self.assertEqual("ACCEPTANCE_READY", json.loads(output.getvalue())["to"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
