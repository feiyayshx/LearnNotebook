#!/usr/bin/env python3
"""Focused tests for the Qoder-native Stage B closure validator."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import loopctl


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class LoopctlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        hooks = self.root / ".empty-hooks"
        hooks.mkdir()
        subprocess.run(["git", "config", "core.hooksPath", str(hooks)], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "loopctl@example.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Loopctl Test"], cwd=self.root, check=True)
        (self.root / "README.md").write_text("test\n", encoding="utf-8", newline="")
        subprocess.run(["git", "add", "README.md"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=self.root, check=True)
        self.source_digest = digest_bytes(b"source")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_json(self, relative: str, value: object) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="")
        return path

    def read_json(self, relative: str) -> dict:
        return json.loads((self.root / relative).read_text(encoding="utf-8"))

    def init_args(self, **overrides: object) -> argparse.Namespace:
        values = {
            "project_root": str(self.root),
            "change_id": "change-login",
            "route": "brownfield",
            "path": "quick",
            "source_digest": self.source_digest,
            "source": ["README.md"],
            "approval_ref": "APPROVAL-2026-07-22",
            "requirement": ["REQ-LOGIN"],
            "acceptance": ["AC-LOGIN"],
            "scope": ["login flow"],
            "non_goal": ["production release"],
            "environment": "local",
            "integration_owner": "main checkout",
            "prototype_contract": None,
            "prototype_required": False,
            "prototype_not_applicable_reason": "API-only test fixture",
            "max_attempts": 3,
            "max_same_failure": 2,
            "max_no_progress": 2,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def attempt(self, *, outcome: str, fingerprint: str | None, progress: bool = False) -> dict:
        commit = loopctl.current_commit(self.root)
        return {
            "schema_version": 1,
            "artifact_type": "execution-attempt",
            "change_id": "change-login",
            "run_id": "RUN-LOGIN-001",
            "objective": "verify login",
            "oracle": "AC-LOGIN passes",
            "starting_commit": commit,
            "ending_commit": commit,
            "changed_digest": digest_bytes(b"diff"),
            "checks": [{"id": "TEST-LOGIN", "status": "passed" if outcome == "passed" else "failed"}],
            "failure_fingerprint": fingerprint,
            "progress": {
                "new_passes": 1 if progress else 0,
                "removed_failures": 0,
                "meaningful_diff": progress,
                "state_advance": outcome == "passed",
            },
            "plan_changed": False,
            "outcome": outcome,
            "next_action": "complete" if outcome == "passed" else "repair",
        }

    def record(self, value: dict, name: str) -> dict:
        path = self.write_json(name, value)
        return loopctl.command_record(
            argparse.Namespace(project_root=str(self.root), change_id="change-login", attempt_file=path.name)
        )

    def test_init_creates_non_overwriting_bundle(self) -> None:
        result = loopctl.command_init(self.init_args())
        self.assertTrue(result["created"])
        self.assertTrue((self.root / "delivery-docs/state/execution/change-login/execution-manifest.json").is_file())
        manifest = self.read_json("delivery-docs/state/execution/change-login/execution-manifest.json")
        expected = loopctl.canonical_digest(
            [{"path": "README.md", "sha256": loopctl.file_digest(self.root / "README.md")}]
        )
        self.assertEqual(expected, manifest["source_digest"])
        with self.assertRaisesRegex(loopctl.LoopError, "refusing to overwrite"):
            loopctl.command_init(self.init_args())

    def test_enhanced_path_requires_planning_seal(self) -> None:
        with self.assertRaisesRegex(loopctl.LoopError, "planning-manifest"):
            loopctl.command_init(self.init_args(path="enhanced", source_digest=self.source_digest))

    def test_quick_source_drift_invalidates_execution(self) -> None:
        loopctl.command_init(self.init_args())
        (self.root / "README.md").write_text("changed approved source\n", encoding="utf-8", newline="")
        result = loopctl.command_validate(
            argparse.Namespace(project_root=str(self.root), change_id="change-login", final=False)
        )
        self.assertFalse(result["valid"])
        self.assertIn("authorized source changed", result["errors"][0])

    def test_repeated_failure_stops_at_declared_budget(self) -> None:
        loopctl.command_init(self.init_args())
        first = self.record(self.attempt(outcome="failed", fingerprint="same"), "attempt-1.json")
        second = self.record(self.attempt(outcome="failed", fingerprint="same"), "attempt-2.json")
        self.assertFalse(first["stop"])
        self.assertTrue(second["stop"])
        self.assertIn("same-failure-limit", second["reasons"])
        with self.assertRaisesRegex(loopctl.LoopError, "already stopped"):
            self.record(self.attempt(outcome="failed", fingerprint="same"), "attempt-3.json")

    def test_prototype_source_drift_invalidates_execution(self) -> None:
        prototype = self.root / "prototype.html"
        prototype.write_text("<main>Login</main>\n", encoding="utf-8", newline="")
        contract = {
            "schema_version": 1,
            "artifact_type": "prototype-contract",
            "applicable": True,
            "not_applicable_reason": None,
            "source": {"path": "prototype.html", "sha256": loopctl.file_digest(prototype)},
            "items": [
                {
                    "id": "PROTO-LOGIN",
                    "kind": "screen",
                    "locator": "main",
                    "requirement_ids": ["REQ-LOGIN"],
                    "acceptance_ids": ["AC-LOGIN"],
                    "states": ["default", "error", "success"],
                    "breakpoints": ["desktop", "mobile"],
                    "accessibility_expectations": ["keyboard reachable"],
                    "visual_oracle": "matches the approved login composition",
                    "status": "planned",
                }
            ],
            "deviations": [],
        }
        self.write_json("delivery-docs/product/prototype-contract.json", contract)
        loopctl.command_init(
            self.init_args(
                prototype_contract="delivery-docs/product/prototype-contract.json",
                prototype_required=True,
                prototype_not_applicable_reason=None,
            )
        )
        prototype.write_text("<main>Unexpected replacement</main>\n", encoding="utf-8", newline="")
        result = loopctl.command_validate(
            argparse.Namespace(project_root=str(self.root), change_id="change-login", final=False)
        )
        self.assertFalse(result["valid"])
        self.assertIn("prototype source digest", result["errors"][0])

    def test_duplicate_acceptance_owner_is_rejected(self) -> None:
        loopctl.command_init(self.init_args())
        path = "delivery-docs/state/execution/change-login/verification-matrix.json"
        matrix = self.read_json(path)
        duplicate = dict(matrix["rows"][0])
        duplicate["id"] = "CHECK-002"
        matrix["rows"].append(duplicate)
        self.write_json(path, matrix)
        result = loopctl.command_validate(
            argparse.Namespace(project_root=str(self.root), change_id="change-login", final=False)
        )
        self.assertFalse(result["valid"])
        self.assertIn("covered by both", result["errors"][0])

    def complete_bundle(self) -> Path:
        loopctl.command_init(self.init_args())
        code = self.root / "src/app.py"
        code.parent.mkdir(parents=True)
        code.write_text("def login():\n    return True\n", encoding="utf-8", newline="")
        evidence = self.root / "delivery-docs/verification/login.txt"
        evidence.parent.mkdir(parents=True)
        evidence.write_text("login acceptance passed\n", encoding="utf-8", newline="")
        evaluator = self.root / "delivery-docs/verification/evaluator.md"
        evaluator.write_text("Independent evaluator: passed\n", encoding="utf-8", newline="")
        subprocess.run(["git", "add", "src/app.py", "delivery-docs/verification/login.txt", "delivery-docs/verification/evaluator.md"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "implement login"], cwd=self.root, check=True)
        commit = loopctl.current_commit(self.root)

        manifest_path = "delivery-docs/state/execution/change-login/execution-manifest.json"
        manifest = self.read_json(manifest_path)
        manifest["status"] = "complete"
        self.write_json(manifest_path, manifest)

        matrix_path = "delivery-docs/state/execution/change-login/verification-matrix.json"
        matrix = self.read_json(matrix_path)
        evidence_entry = {
            "id": "EVID-LOGIN",
            "kind": "test-report",
            "path": "delivery-docs/verification/login.txt",
            "sha256": loopctl.file_digest(evidence),
        }
        matrix["subject_commit"] = commit
        matrix["rows"][0].update(
            {
                "requirement_ids": ["REQ-LOGIN"],
                "task_refs": ["openspec/changes/change-login/tasks.md#task-1"],
                "code_refs": ["src/app.py"],
                "tests": [
                    {
                        "id": "TEST-LOGIN",
                        "level": "e2e",
                        "command": "python -m unittest test_login",
                        "status": "passed",
                        "evidence_refs": ["EVID-LOGIN"],
                    }
                ],
                "evidence": [evidence_entry],
                "commit": commit,
                "environment": "local-test",
                "independent_review": {
                    "status": "passed",
                    "reviewer": "qoder-independent-evaluator",
                    "evidence_refs": ["EVID-LOGIN"],
                },
                "status": "integrated",
            }
        )
        self.write_json(matrix_path, matrix)

        score_path = "delivery-docs/state/execution/change-login/quality-scorecard.json"
        score = self.read_json(score_path)
        score["subject_commit"] = commit
        score["hard_gates"] = {key: True for key in score["hard_gates"]}
        for dimension in score["dimensions"]:
            dimension["score"] = dimension["weight"]
            dimension["evidence_refs"] = ["EVID-LOGIN"]
        score["total_score"] = 100
        score["independent_evaluator"] = {
            "identity": "qoder-independent-evaluator",
            "report_path": "delivery-docs/verification/evaluator.md",
            "report_sha256": loopctl.file_digest(evaluator),
        }
        score["status"] = "passed"
        self.write_json(score_path, score)
        self.record(self.attempt(outcome="passed", fingerprint=None, progress=True), "attempt-final.json")
        return evidence

    def test_final_validation_accepts_complete_90_plus_delivery(self) -> None:
        self.complete_bundle()
        result = loopctl.command_validate(
            argparse.Namespace(project_root=str(self.root), change_id="change-login", final=True)
        )
        self.assertTrue(result["valid"], result)
        self.assertTrue(result["acceptance_ready"])
        self.assertEqual(result["quality_score"], 100)

    def test_final_validation_rejects_stale_evidence(self) -> None:
        evidence = self.complete_bundle()
        evidence.write_text("evidence changed after scoring\n", encoding="utf-8", newline="")
        result = loopctl.command_validate(
            argparse.Namespace(project_root=str(self.root), change_id="change-login", final=True)
        )
        self.assertFalse(result["valid"])
        self.assertIn("digest does not match", result["errors"][0])

    def test_final_validation_rejects_sub_90_dimension(self) -> None:
        self.complete_bundle()
        path = "delivery-docs/state/execution/change-login/quality-scorecard.json"
        score = self.read_json(path)
        score["dimensions"][0]["score"] = 26
        score["total_score"] = 96
        self.write_json(path, score)
        result = loopctl.command_validate(
            argparse.Namespace(project_root=str(self.root), change_id="change-login", final=True)
        )
        self.assertFalse(result["valid"])
        self.assertIn("below its minimum", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
