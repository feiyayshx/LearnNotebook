#!/usr/bin/env python3
"""M2 read-only reconnaissance regression tests."""

from __future__ import annotations

import hashlib
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


class ReconnaissanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="delivery-recon-test-")
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative: str, text: str = "fixture\n") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def initialize_git(self) -> str:
        self.git("init", "-q")
        self.git("config", "user.name", "M2 Fixture")
        self.git("config", "user.email", "m2@example.invalid")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "fixture baseline")
        return self.git("rev-parse", "HEAD")

    def inventory(self) -> dict[str, tuple]:
        result: dict[str, tuple] = {}
        for path in sorted(self.root.rglob("*")):
            relative = path.relative_to(self.root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                result[relative] = ("symlink", stat.S_IMODE(mode), os.readlink(path))
            elif stat.S_ISDIR(mode):
                result[relative] = ("directory", stat.S_IMODE(mode))
            elif stat.S_ISREG(mode):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                result[relative] = ("file", stat.S_IMODE(mode), path.stat().st_size, digest)
            else:
                result[relative] = ("special", stat.S_IMODE(mode))
        return result

    def initialize_resume(self) -> tuple[str, Path]:
        source = self.write("delivery-docs/product/source/spec.md", "# Registered source\n")
        self.write("src/app.js", "export const value = 1;\n")
        self.write("openspec/config.yaml", "schema: spec-driven\n")
        self.write("openspec/changes/active-slice/proposal.md", "# Active slice\n")
        deliveryctl.initialize_project(
            self.root,
            mode="brownfield",
            project_id="resume-fixture",
            with_openspec_config=False,
        )
        commit = self.initialize_git()
        deliveryctl.set_context(
            self.root,
            expected_revision=0,
            actor="test",
            reason="register M2 baseline",
            source_digest=deliveryctl.sha256_file(source),
            baseline_commit=commit,
            active_change="active-slice",
        )
        return commit, source

    def test_inspect_is_read_only_and_deterministic(self) -> None:
        self.write("delivery-docs/product/source/spec.md", "# Spec\n")
        self.write("prototype/index.html", "<main>prototype</main>\n")
        self.initialize_git()
        before = self.inventory()

        first = deliveryctl.inspect_project(self.root)
        middle = self.inventory()
        second = deliveryctl.inspect_project(self.root)
        after = self.inventory()

        self.assertEqual(before, middle)
        self.assertEqual(before, after)
        self.assertEqual(first["report_digest"], second["report_digest"])
        first.pop("inspected_at")
        second.pop("inspected_at")
        self.assertEqual(first, second)
        self.assertTrue(first["read_only"])

    def test_committed_spec_and_prototype_are_greenfield(self) -> None:
        self.write("delivery-docs/product/source/system-spec.md", "# System\n")
        self.write("prototype/index.html", "<main>Prototype</main>\n")
        self.initialize_git()

        result = deliveryctl.inspect_project(self.root)

        self.assertEqual("greenfield", result["route"]["recommended"])
        self.assertFalse(result["route"]["blocked"])
        self.assertIn("spec-or-prototype-input", result["route"]["reason_codes"])

    def test_existing_static_web_site_is_brownfield_not_prototype(self) -> None:
        self.write("index.html", "<!doctype html><title>Existing product</title>\n")
        self.write("styles.css", "body { color: black; }\n")

        result = deliveryctl.inspect_project(self.root)

        self.assertEqual(["index.html", "styles.css"], result["capabilities"]["product_sources"])
        self.assertEqual("brownfield", result["route"]["recommended"])
        self.assertIn("existing-product-source", result["route"]["reason_codes"])

    def test_explicit_html_prototype_remains_greenfield_input(self) -> None:
        self.write("prototype.html", "<!doctype html><title>Prototype only</title>\n")

        result = deliveryctl.inspect_project(self.root)

        self.assertEqual(["prototype.html"], result["capabilities"]["prototype_candidates"])
        self.assertFalse(result["capabilities"]["product_sources"])
        self.assertEqual("greenfield", result["route"]["recommended"])

    def test_brownfield_matrix_is_metadata_only_and_secret_safe(self) -> None:
        self.write(
            "package.json",
            json.dumps(
                {
                    "scripts": {"test": "touch SHOULD_NOT_EXIST", "build": "echo SECRET_BUILD_VALUE"},
                    "devDependencies": {"@playwright/test": "fixture"},
                }
            ),
        )
        self.write("pnpm-lock.yaml")
        self.write("pyproject.toml", "[tool.pytest.ini_options]\n")
        self.write("src/app.js", "export const app = true;\n")
        self.write("tests/app.test.js")
        self.write(".github/workflows/ci.yml")
        self.write("architecture/decisions/0001.md")
        self.write("openspec/config.yaml", "schema: spec-driven\n")
        self.write("openspec/specs/app/spec.md")
        self.write("openspec/changes/add-app/proposal.md")
        self.write(".qoder/rules/project.md")
        self.write(".qoder/hooks/audit.sh")
        self.write(".qoder/skills/local/SKILL.md")
        self.write(".qoder/settings.json", json.dumps({"hooks": {"PostToolUse": []}}))
        self.write(
            ".mcp.json",
            json.dumps(
                {
                    "mcpServers": {
                        "browser": {
                            "command": "MUST_NOT_EXECUTE",
                            "env": {"TOKEN": "M2_SECRET_CANARY_DO_NOT_EMIT"},
                        },
                        "https://user:M2_KEYNAME_CANARY@example.invalid/mcp": {},
                    }
                }
            ),
        )
        self.write(
            ".qoder/settings.local.json",
            json.dumps({"hooks": {"https://user:M2_HOOK_CANARY@example.invalid": []}}),
        )
        self.initialize_git()

        result = deliveryctl.inspect_project(self.root)
        encoded = json.dumps(result, ensure_ascii=False)

        self.assertEqual("brownfield", result["route"]["recommended"])
        self.assertEqual(["node", "python"], [item["id"] for item in result["capabilities"]["stacks"]])
        browser_ref = f"mcp-server-{hashlib.sha256(b'browser').hexdigest()[:12]}"
        self.assertIn(browser_ref, result["capabilities"]["qoder"]["mcp_servers"])
        self.assertFalse(result["capabilities"]["qoder"]["mcp_identifiers_disclosed"])
        self.assertNotIn("M2_SECRET_CANARY_DO_NOT_EMIT", encoded)
        self.assertNotIn("MUST_NOT_EXECUTE", encoded)
        self.assertNotIn("SECRET_BUILD_VALUE", encoded)
        self.assertNotIn("M2_KEYNAME_CANARY", encoded)
        self.assertNotIn("M2_HOOK_CANARY", encoded)
        self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())
        requirements = result["adoption"]["preservation_requirements"]
        self.assertTrue(requirements)
        self.assertTrue(all(item["status"] == "draft" for item in requirements))
        self.assertTrue(all(item["verification_status"] == "not_run" for item in requirements))
        requirement_evidence = {evidence for item in requirements for evidence in item["evidence"]}
        self.assertIn("tests/app.test.js", requirement_evidence)
        self.assertIn("openspec/specs/app/spec.md", requirement_evidence)
        self.assertIn("openspec/changes/add-app", requirement_evidence)
        self.assertIn(".qoder/hooks/audit.sh", requirement_evidence)
        self.assertIn(".qoder/skills/local/SKILL.md", requirement_evidence)
        self.assertIn(".mcp.json", requirement_evidence)
        self.assertEqual("deferred_to_m3", result["adoption"]["semantic_extraction_status"])
        python_command = next(item for item in result["capabilities"]["commands"] if item["id"] == "python-test")
        self.assertEqual("inferred_candidate", python_command["discovery"])
        self.assertTrue(python_command["requires_authority_choice"])

    def test_cli_inspect_does_not_create_python_bytecode_in_project_skill(self) -> None:
        tool_dir = self.root / ".qoder" / "skills" / "deliver-system" / "scripts"
        tool_dir.mkdir(parents=True)
        source_dir = Path(deliveryctl.__file__).resolve().parent
        # deliveryctl imports every sibling module at module scope, so the copy
        # must include the whole local dependency set.
        for module in (
            "deliveryctl.py",
            "reconnaissance.py",
            "git_scope.py",
            "openspec_adapter.py",
            "planning.py",
            "planning_bindings.py",
        ):
            shutil.copy2(source_dir / module, tool_dir / module)
        self.write("delivery-docs/product/source/spec.md", "# Bytecode-safe inspection\n")
        before = self.inventory()

        completed = subprocess.run(
            [
                sys.executable,
                str(tool_dir / "deliveryctl.py"),
                "inspect",
                "--project-root",
                str(self.root),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(before, self.inventory())
        self.assertFalse((tool_dir / "__pycache__").exists())

    def test_partial_control_plane_blocks_route_override_and_init(self) -> None:
        state = self.write("delivery-docs/state/state.json", "{}\n")
        before = self.inventory()

        result = deliveryctl.inspect_project(self.root, route_override="greenfield")

        self.assertEqual("resume", result["route"]["recommended"])
        self.assertEqual("resume", result["route"]["selected"])
        self.assertTrue(result["route"]["blocked"])
        self.assertIn("override-cannot-bypass-resume", result["route"]["reason_codes"])
        with self.assertRaisesRegex(deliveryctl.DeliveryError, "partial delivery-docs/state"):
            deliveryctl.initialize_project(
                self.root,
                mode="greenfield",
                project_id="unsafe",
                with_openspec_config=False,
            )
        self.assertEqual("{}\n", state.read_text(encoding="utf-8"))
        self.assertEqual(before, self.inventory())

    def test_route_override_is_explicit_but_resume_requires_control(self) -> None:
        overridden = deliveryctl.inspect_project(self.root, route_override="brownfield")
        invalid_resume = deliveryctl.inspect_project(self.root, route_override="resume")

        self.assertEqual("greenfield", overridden["route"]["recommended"])
        self.assertEqual("brownfield", overridden["route"]["selected"])
        self.assertTrue(overridden["route"]["override_applied"])
        self.assertEqual("greenfield", invalid_resume["route"]["selected"])
        self.assertTrue(invalid_resume["route"]["blocked"])
        self.assertFalse(invalid_resume["route"]["override_applied"])

    def test_valid_resume_cannot_be_overridden_away(self) -> None:
        self.initialize_resume()

        result = deliveryctl.inspect_project(self.root, route_override="greenfield")

        self.assertEqual("resume", result["route"]["recommended"])
        self.assertEqual("resume", result["route"]["selected"])
        self.assertFalse(result["route"]["override_applied"])
        self.assertIn("override-cannot-bypass-resume", result["route"]["reason_codes"])

    @unittest.skipUnless(SYMLINKS_SUPPORTED, "symlink creation requires elevated privileges on Windows")
    def test_symlink_is_not_followed_and_critical_path_blocks(self) -> None:
        outside = Path(self.temporary.name).parent / f"{self.root.name}-outside"
        outside.mkdir()
        try:
            (outside / "M2_EXTERNAL_CANARY").write_text("outside secret\n", encoding="utf-8", newline="")
            (self.root / "openspec").symlink_to(outside, target_is_directory=True)
            result = deliveryctl.inspect_project(self.root)
            encoded = json.dumps(result, ensure_ascii=False)
            self.assertTrue(result["route"]["blocked"])
            self.assertIn("critical-path-symlink", result["route"]["reason_codes"])
            self.assertNotIn("M2_EXTERNAL_CANARY", encoded)
        finally:
            for path in outside.iterdir():
                path.unlink()
            outside.rmdir()

    @unittest.skipUnless(SYMLINKS_SUPPORTED, "symlink creation requires elevated privileges on Windows")
    def test_source_directory_symlink_blocks_instead_of_claiming_greenfield(self) -> None:
        outside = Path(self.temporary.name).parent / f"{self.root.name}-source-outside"
        outside.mkdir()
        (outside / "app.js").write_text("export const hidden = true;\n", encoding="utf-8", newline="")
        (self.root / "src").symlink_to(outside, target_is_directory=True)
        try:
            result = deliveryctl.inspect_project(self.root)
            self.assertTrue(result["route"]["blocked"])
            self.assertIn("critical-path-symlink", result["route"]["reason_codes"])
            self.assertEqual(1, result["scan"]["critical_symlink_count"])
            self.assertFalse(result["capabilities"]["product_sources"])
        finally:
            (self.root / "src").unlink()
            (outside / "app.js").unlink()
            outside.rmdir()

    @unittest.skipUnless(SYMLINKS_SUPPORTED, "symlink creation requires elevated privileges on Windows")
    def test_symlink_blocker_is_not_hidden_by_result_cap(self) -> None:
        outside = Path(self.temporary.name).parent / f"{self.root.name}-cap-outside"
        outside.mkdir()
        try:
            for index in range(5):
                (self.root / f"a-link-{index}").symlink_to(outside, target_is_directory=True)
            (self.root / "openspec").symlink_to(outside, target_is_directory=True)

            result = deliveryctl.inspect_project(self.root, scan_limits={"max_results": 1})

            self.assertEqual(["a-link-0"], result["scan"]["symlinks"])
            self.assertEqual(["a-link-0"], result["scan"]["critical_symlinks"])
            self.assertEqual(6, result["scan"]["critical_symlink_count"])
            self.assertIn("critical-path-symlink", result["route"]["reason_codes"])
            self.assertTrue(result["route"]["blocked"])
        finally:
            for path in self.root.glob("a-link-*"):
                path.unlink()
            (self.root / "openspec").unlink()
            outside.rmdir()

    @unittest.skipUnless(SYMLINKS_SUPPORTED, "symlink creation requires elevated privileges on Windows")
    def test_delivery_directory_symlink_is_blocked_without_reading_or_repair(self) -> None:
        outside = Path(self.temporary.name).parent / f"{self.root.name}-delivery-outside"
        outside.mkdir()
        marker = outside / "manifest.json"
        marker.write_text('{"secret":"M2_OUTSIDE_DELIVERY_CANARY"}\n', encoding="utf-8", newline="")
        (self.root / "delivery-docs").mkdir()
        (self.root / "delivery-docs" / "state").symlink_to(outside, target_is_directory=True)
        try:
            result = deliveryctl.inspect_project(self.root)
            encoded = json.dumps(result, ensure_ascii=False)
            self.assertEqual("resume", result["route"]["recommended"])
            self.assertTrue(result["route"]["blocked"])
            self.assertNotIn("M2_OUTSIDE_DELIVERY_CANARY", encoded)
            with self.assertRaisesRegex(deliveryctl.DeliveryError, "symbolic link"):
                deliveryctl.initialize_project(
                    self.root,
                    mode="greenfield",
                    project_id="unsafe",
                    with_openspec_config=False,
                )
            self.assertEqual('{"secret":"M2_OUTSIDE_DELIVERY_CANARY"}\n', marker.read_text(encoding="utf-8"))
        finally:
            (self.root / "delivery-docs" / "state").unlink()
            marker.unlink()
            outside.rmdir()

    def test_scan_budget_is_enforced_before_full_walk(self) -> None:
        for index in range(20):
            self.write(f"src/file-{index:02d}.js")
        for index in range(20):
            self.write(f"node_modules/pkg/file-{index:02d}.js")

        result = deliveryctl.inspect_project(self.root, scan_limits={"max_files": 3})

        self.assertTrue(result["scan"]["truncated"])
        self.assertEqual(3, result["scan"]["counts"]["files"])
        self.assertIn("max_files", result["scan"]["truncated_reasons"])
        self.assertTrue(result["route"]["blocked"])
        self.assertTrue(any(path == "node_modules" for path in result["scan"]["skipped_directories"]))

    @unittest.skipUnless(SYMLINKS_SUPPORTED, "symlink creation requires elevated privileges on Windows")
    def test_symlinks_and_special_entries_consume_scan_budget(self) -> None:
        outside = Path(self.temporary.name).parent / f"{self.root.name}-budget-outside"
        outside.mkdir()
        try:
            for index in range(100):
                (self.root / f"link-{index:03d}").symlink_to(outside, target_is_directory=True)

            result = deliveryctl.inspect_project(
                self.root,
                scan_limits={"max_files": 1, "max_directories": 1, "max_results": 1},
            )

            self.assertTrue(result["scan"]["truncated"])
            self.assertIn("max_entries", result["scan"]["truncated_reasons"])
            self.assertLessEqual(result["scan"]["counts"]["observed_entries"], 4)
            self.assertTrue(result["route"]["blocked"])
        finally:
            for path in self.root.glob("link-*"):
                path.unlink()
            outside.rmdir()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_fifo_is_never_opened_and_blocks_routing(self) -> None:
        fifo = self.root / "package.json"
        extra_fifo = self.root / "z-extra.fifo"
        os.mkfifo(fifo)
        os.mkfifo(extra_fifo)
        command = [
            sys.executable,
            str(Path(deliveryctl.__file__).resolve()),
            "inspect",
            "--project-root",
            str(self.root),
            "--max-results",
            "1",
        ]

        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(2, result["scan"]["counts"]["special_entries"])
        self.assertEqual(["package.json"], result["scan"]["special_entries"])
        self.assertTrue(result["route"]["blocked"])
        self.assertIn("unsupported-special-entry", result["route"]["reason_codes"])
        risk = next(
            risk
            for risk in result["risks"]
            if risk["id"] == "RISK-UNSUPPORTED-SPECIAL-ENTRY"
        )
        self.assertEqual(["package.json"], risk["evidence"])

    def test_malformed_and_oversized_markers_report_partial(self) -> None:
        self.write("package.json", "{broken")
        self.write(".qoder/settings.json", "{broken")
        self.write("openspec/config.yaml", "x" * 200)

        result = deliveryctl.inspect_project(self.root, scan_limits={"max_marker_bytes": 64})

        self.assertEqual("partial", result["scan"]["status"])
        self.assertTrue(result["scan"]["warnings"])
        self.assertTrue(result["route"]["blocked"])

    def test_multiple_package_managers_are_an_explicit_conflict(self) -> None:
        self.write("package.json", json.dumps({"scripts": {"test": "node --test"}}))
        self.write("package-lock.json", "{}\n")
        self.write("pnpm-lock.yaml")
        self.write("src/app.js")

        result = deliveryctl.inspect_project(self.root)

        self.assertEqual(["npm", "pnpm"], result["capabilities"]["package_managers"])
        self.assertEqual("CONFLICT-PACKAGE-MANAGERS", result["adoption"]["conflicts"][0]["id"])
        self.assertIsNone(result["capabilities"]["commands"][0]["command"])
        self.assertTrue(result["capabilities"]["commands"][0]["requires_authority_choice"])

    def test_preservation_ids_do_not_shift_when_an_asset_is_added(self) -> None:
        self.write("package.json", json.dumps({"scripts": {"test": "node --test"}}))
        self.write("package-lock.json", "{}\n")
        self.write("src/app.js")
        first = deliveryctl.inspect_project(self.root)
        command_id = next(
            item["id"]
            for item in first["adoption"]["preservation_requirements"]
            if item["evidence"] == ["package.json#scripts.test"]
        )

        self.write("delivery-docs/decisions/adr/0001-added.md")
        second = deliveryctl.inspect_project(self.root)
        self.assertIn(
            command_id,
            {item["id"] for item in second["adoption"]["preservation_requirements"]},
        )

    def test_resume_direct_evidence_reports_unchanged(self) -> None:
        _, source = self.initialize_resume()

        result = deliveryctl.inspect_project(
            self.root,
            source_paths=[str(source.relative_to(self.root))],
        )

        self.assertEqual("resume", result["route"]["selected"])
        self.assertEqual("unchanged", result["drift"]["source"]["status"])
        self.assertEqual("unchanged", result["drift"]["git"]["status"])
        self.assertEqual("unchanged", result["drift"]["openspec"]["status"])
        self.assertEqual("unchanged", result["drift"]["overall"])
        self.assertFalse(result["route"]["blocked"])

    def test_resume_source_drift_changed_missing_and_unknown(self) -> None:
        _, source = self.initialize_resume()
        relative = str(source.relative_to(self.root))
        source.write_text("changed\n", encoding="utf-8", newline="")
        changed = deliveryctl.inspect_project(self.root, source_paths=[relative])
        source.unlink()
        missing = deliveryctl.inspect_project(self.root, source_paths=[relative])
        unknown = deliveryctl.inspect_project(self.root)

        self.assertEqual("changed", changed["drift"]["source"]["status"])
        self.assertEqual("missing", missing["drift"]["source"]["status"])
        self.assertEqual("unknown", unknown["drift"]["source"]["status"])
        self.assertTrue(changed["route"]["blocked"])
        self.assertTrue(missing["route"]["blocked"])
        self.assertTrue(unknown["route"]["blocked"])

    def test_resume_missing_active_change_is_blocked(self) -> None:
        _, source = self.initialize_resume()
        for path in sorted((self.root / "openspec" / "changes" / "active-slice").rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        (self.root / "openspec" / "changes" / "active-slice").rmdir()

        result = deliveryctl.inspect_project(
            self.root,
            source_paths=[str(source.relative_to(self.root))],
        )

        self.assertEqual("missing", result["drift"]["openspec"]["status"])
        self.assertEqual("missing", result["drift"]["overall"])
        self.assertTrue(result["route"]["blocked"])

    def test_partial_scan_cannot_claim_active_change_is_missing(self) -> None:
        _, source = self.initialize_resume()

        result = deliveryctl.inspect_project(
            self.root,
            source_paths=[str(source.relative_to(self.root))],
            scan_limits={"max_directories": 1},
        )

        self.assertTrue(result["scan"]["truncated"])
        self.assertEqual("unknown", result["drift"]["openspec"]["status"])
        self.assertNotEqual("missing", result["drift"]["overall"])
        self.assertTrue(result["route"]["blocked"])

    def test_result_display_cap_cannot_hide_registered_active_change(self) -> None:
        _, source = self.initialize_resume()
        self.write("openspec/changes/a-earlier/proposal.md", "# Earlier\n")
        self.git("add", "openspec/changes/a-earlier/proposal.md")
        self.git("commit", "-q", "-m", "add earlier change")
        deliveryctl.set_context(
            self.root,
            expected_revision=deliveryctl.read_json(self.root / "delivery-docs" / "state" / "state.json")["revision"],
            actor="test",
            reason="refresh fixture baseline",
            baseline_commit=self.git("rev-parse", "HEAD"),
        )

        result = deliveryctl.inspect_project(
            self.root,
            source_paths=[str(source.relative_to(self.root))],
            scan_limits={"max_results": 1},
        )

        self.assertEqual(["a-earlier"], result["capabilities"]["openspec"]["active_changes"])
        self.assertEqual(2, result["capabilities"]["openspec"]["active_change_count"])
        self.assertEqual("unchanged", result["drift"]["openspec"]["status"])
        self.assertNotIn("_active_changes", result["capabilities"]["openspec"])

    def test_git_status_error_cannot_claim_resume_is_unchanged(self) -> None:
        _, source = self.initialize_resume()
        git = deliveryctl.git_info(self.root)
        git.update({"status": "error", "clean_for_gate": False, "error": "simulated status failure"})
        _, delivery_context = deliveryctl._inspect_delivery_control(self.root)

        def successful_diff(root: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(arguments, 0, "", "")

        result = deliveryctl.reconnaissance.build_reconnaissance(
            self.root,
            git=git,
            delivery=delivery_context,
            source_paths=[str(source.relative_to(self.root))],
            git_runner=successful_diff,
            harness_version=deliveryctl.HARNESS_VERSION,
        )

        self.assertEqual("unknown", result["drift"]["git"]["status"])
        self.assertEqual("unknown", result["drift"]["openspec"]["status"])
        self.assertEqual("unknown", result["drift"]["overall"])
        self.assertTrue(result["route"]["blocked"])

    def test_passed_gate_bound_to_other_inputs_is_changed_not_unchanged(self) -> None:
        _, source = self.initialize_resume()
        state = deliveryctl.read_json(self.root / "delivery-docs" / "state" / "state.json")
        now = deliveryctl.isoformat()
        gate = deliveryctl.read_json(self.root / "delivery-docs" / "state" / "gate-manifest.json")
        gate.update(
            {
                "change_id": "other-change",
                "source_digest": "f" * 64,
                "commit": self.git("rev-parse", "HEAD"),
                "environment": "fixture",
                "config_digest": "c" * 64,
                "test_suite_digest": "d" * 64,
                "started_at": now,
                "finished_at": now,
                "results": {"unit": "passed"},
                "artifacts": ["https://example.invalid/unverified"],
                "status": "passed",
            }
        )
        deliveryctl.atomic_write_json(self.root / "delivery-docs" / "state" / "gate-manifest.json", gate)

        result = deliveryctl.inspect_project(
            self.root,
            source_paths=[str(source.relative_to(self.root))],
        )

        self.assertEqual(state["active_change"], result["drift"]["openspec"]["active_change"])
        self.assertEqual("changed", result["drift"]["evidence"]["status"])
        self.assertIn("active_change", result["drift"]["evidence"]["binding_mismatches"])
        self.assertIn("verified_local_artifact", result["drift"]["evidence"]["binding_mismatches"])
        self.assertTrue(result["route"]["blocked"])

    def test_matching_passed_gate_remains_unknown_until_m3_revalidation(self) -> None:
        _, source = self.initialize_resume()
        evidence_path = self.write("delivery-docs/state/runs/evidence.txt", "passed fixture evidence\n")
        state = deliveryctl.read_json(self.root / "delivery-docs" / "state" / "state.json")
        now = deliveryctl.isoformat()
        gate = deliveryctl.read_json(self.root / "delivery-docs" / "state" / "gate-manifest.json")
        gate.update(
            {
                "change_id": state["active_change"],
                "source_digest": state["source_digest"],
                "commit": self.git("rev-parse", "HEAD"),
                "environment": "fixture",
                "config_digest": "c" * 64,
                "test_suite_digest": "d" * 64,
                "started_at": now,
                "finished_at": now,
                "results": {"unit": "passed"},
                "artifacts": [
                    {
                        "path": "delivery-docs/state/runs/evidence.txt",
                        "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                    }
                ],
                "status": "passed",
            }
        )
        deliveryctl.atomic_write_json(self.root / "delivery-docs" / "state" / "gate-manifest.json", gate)

        result = deliveryctl.inspect_project(
            self.root,
            source_paths=[str(source.relative_to(self.root))],
        )

        self.assertEqual("unknown", result["drift"]["evidence"]["status"])
        self.assertEqual("unknown", result["drift"]["overall"])
        self.assertTrue(result["route"]["blocked"])

    def test_git_trace_environment_cannot_write_during_inspect(self) -> None:
        self.write("src/app.js", "export const value = 1;\n")
        self.initialize_git()
        trace = self.root / "M2_GIT_TRACE_CANARY"

        with mock.patch.dict(os.environ, {"GIT_TRACE": str(trace)}, clear=False):
            result = deliveryctl.inspect_project(self.root)

        self.assertFalse(trace.exists())
        self.assertEqual("clean", result["git"]["status"])

    def test_git_fsmonitor_is_disabled_during_inspect(self) -> None:
        self.write("src/app.js", "export const value = 1;\n")
        self.initialize_git()
        canary = self.root / "M2_FSMONITOR_CANARY"
        hook = self.root / "fsmonitor-tripwire.sh"
        hook.write_text(f"#!/bin/sh\ntouch '{canary}'\nexit 0\n", encoding="utf-8", newline="")
        hook.chmod(0o755)
        self.git("config", "core.fsmonitor", str(hook))

        result = deliveryctl.inspect_project(self.root)

        self.assertFalse(canary.exists())
        self.assertIn(result["git"]["status"], {"clean", "dirty"})

    def test_git_clean_filter_is_detected_without_execution(self) -> None:
        self.write(".gitattributes", "src/app.js filter=evil\n")
        app = self.write("src/app.js", "export const value = 1;\n")
        self.initialize_git()
        canary = self.root / "M2_FILTER_CANARY"
        outside = Path(self.temporary.name).parent / f"{self.root.name}-filter.sh"
        outside.write_text(f"#!/bin/sh\ntouch '{canary}'\ncat\n", encoding="utf-8", newline="")
        outside.chmod(0o755)
        try:
            self.git("config", "filter.evil.clean", str(outside))
            app.write_text("export const value = 2;\n", encoding="utf-8", newline="")

            result = deliveryctl.inspect_project(self.root)

            self.assertFalse(canary.exists())
            self.assertEqual("error", result["git"]["status"])
            self.assertTrue(result["route"]["blocked"])
            self.assertTrue(result["git"]["safety_risks"])
        finally:
            outside.unlink()

    def test_git_output_is_bounded_and_failure_blocks(self) -> None:
        self.write("src/app.js", "export const value = 1;\n")
        self.initialize_git()
        for index in range(20):
            self.write(f"untracked-{index:02d}-with-a-long-name.txt")

        with mock.patch.object(deliveryctl, "GIT_OUTPUT_LIMIT_BYTES", 64):
            result = deliveryctl.inspect_project(self.root)

        self.assertIn(result["git"]["status"], {"error", "unknown"})
        self.assertIn("exceeded", result["git"]["error"])
        self.assertTrue(result["route"]["blocked"])

    def test_public_git_paths_respect_result_cap(self) -> None:
        self.write("src/app.js", "export const value = 1;\n")
        self.initialize_git()
        for index in range(12):
            self.write(f"untracked-{index:02d}.txt")

        result = deliveryctl.inspect_project(self.root, scan_limits={"max_results": 2})

        self.assertEqual(12, result["git"]["dirty_path_count"])
        self.assertEqual(2, len(result["git"]["dirty_paths"]))
        self.assertTrue(result["git"]["dirty_path_truncated"])
        self.assertEqual(result["git"], result["capabilities"]["git"])

    def test_resume_advanced_git_identity_is_changed_even_without_product_diff(self) -> None:
        _, source = self.initialize_resume()
        self.git("commit", "--allow-empty", "-q", "-m", "advance identity")

        result = deliveryctl.inspect_project(
            self.root,
            source_paths=[str(source.relative_to(self.root))],
        )

        self.assertEqual("changed", result["drift"]["git"]["status"])
        self.assertTrue(result["route"]["blocked"])

    def test_closed_control_plane_routes_existing_product_as_brownfield(self) -> None:
        self.write("src/app.js")
        deliveryctl.initialize_project(
            self.root,
            mode="brownfield",
            project_id="closed-fixture",
            with_openspec_config=False,
        )
        deliveryctl.transition_state(
            self.root,
            target="ABORTED",
            reason="cancel",
            actor="test",
            expected_revision=0,
            next_action=None,
            milestone=None,
            change=None,
            approval_ref=None,
        )
        deliveryctl.transition_state(
            self.root,
            target="CLOSED",
            reason="close",
            actor="test",
            expected_revision=1,
            next_action=None,
            milestone=None,
            change=None,
            approval_ref=None,
        )

        result = deliveryctl.inspect_project(self.root)

        self.assertEqual("brownfield", result["route"]["recommended"])
        self.assertIn("terminal-control-plane", result["route"]["reason_codes"])

    def test_git_failure_is_structured_and_blocks(self) -> None:
        self.write("src/app.js")
        (self.root / ".git").mkdir()

        result = deliveryctl.inspect_project(self.root)

        self.assertIsNone(result["git"]["repository"])
        self.assertEqual("error", result["git"]["status"])
        self.assertTrue(result["route"]["blocked"])
        self.assertIn("git-scope-unknown", result["route"]["reason_codes"])

    def test_inspect_never_repairs_or_transitions_resume_state(self) -> None:
        _, source = self.initialize_resume()
        before = self.inventory()

        deliveryctl.inspect_project(self.root, source_paths=[str(source.relative_to(self.root))])

        self.assertEqual(before, self.inventory())
        state = deliveryctl.read_json(self.root / "delivery-docs" / "state" / "state.json")
        self.assertEqual("INTAKE", state["phase"])
        self.assertEqual(1, state["revision"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
