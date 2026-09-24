#!/usr/bin/env python3
"""M3 OpenSpec/config safety adapter regression tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import openspec_adapter as adapter


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


class OpenSpecAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="delivery-openspec-test-")
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
        return path

    @staticmethod
    def cli_script_name() -> str:
        return "openspec.cmd" if os.name == "nt" else "openspec"

    def make_cli(self, body: str) -> adapter.ExecutablePin:
        if os.name == "nt":
            # Windows cannot CreateProcess a bare script; a .cmd launcher
            # forwards arguments to the Python payload instead.
            payload = self.bin / "openspec-payload.py"
            payload.write_text(textwrap.dedent(body), encoding="utf-8", newline="")
            script = self.bin / self.cli_script_name()
            script.write_text(
                f'@echo off\r\n"{sys.executable}" "{payload}" %*\r\n',
                encoding="utf-8", newline="",
            )
        else:
            script = self.bin / self.cli_script_name()
            script.write_text(
                "#!/usr/bin/env python3\n" + textwrap.dedent(body),
                encoding="utf-8", newline="",
            )
            script.chmod(0o755)
        path = os.pathsep.join((os.fspath(self.bin), "/usr/bin", "/bin"))
        return adapter.resolve_openspec_executable(
            self.root,
            environ={"PATH": path},
        )

    def valid_cli(self) -> adapter.ExecutablePin:
        return self.make_cli(
            """
            import json
            import os
            import pathlib
            import sys

            if sys.argv[1:] == ["--version"]:
                print("1.6.0")
                raise SystemExit(0)
            args = sys.argv[1:]
            if args[:2] == ["new", "change"]:
                change = args[2]
                target = pathlib.Path.cwd() / "openspec" / "changes" / change
                target.mkdir(parents=True)
                (target / ".openspec.yaml").write_text("schema: spec-driven\\n", encoding="utf-8", newline="")
                print(json.dumps({"change": change, "created": True}))
                raise SystemExit(0)
            print(json.dumps({"argv": args, "telemetry": os.environ.get("OPENSPEC_TELEMETRY")}))
            """
        )

    def assert_error(self, code: str, function, *args, **kwargs) -> adapter.OpenSpecAdapterError:
        with self.assertRaises(adapter.OpenSpecAdapterError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def test_config_audit_reports_known_unknown_and_preserves_comments(self) -> None:
        original = (
            "# keep this comment\n"
            "schema: spec-driven\n"
            "context: |\n"
            "  Web service\n"
            "rules:\n"
            "  proposal:\n"
            "    - Include REQ IDs\n"
            "custom_future_field: keep-me\n"
        )
        path = self.write("openspec/config.yaml", original)

        audit = adapter.inspect_config(self.root)
        plan = adapter.plan_config_candidate(self.root)

        self.assertTrue(audit["cli_allowed"])
        self.assertEqual("spec-driven", audit["schema"])
        self.assertEqual(["custom_future_field"], audit["unknown_fields"])
        self.assertEqual("replace-exact-bytes-requires-explicit-opt-in", plan["action"])
        self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_dual_empty_duplicate_and_unsafe_yaml_are_blocked(self) -> None:
        self.write("openspec/config.yaml", "schema: spec-driven\n")
        self.write("openspec/config.yml", "schema: spec-driven\n")
        self.assertIn("config-dual-files", adapter.inspect_config(self.root)["blockers"])

        (self.root / "openspec/config.yml").unlink()
        (self.root / "openspec/config.yaml").write_text("# only comment\n", encoding="utf-8", newline="")
        self.assertIn("config-empty", adapter.inspect_config(self.root)["blockers"])

        (self.root / "openspec/config.yaml").write_text(
            "schema: spec-driven\nschema: other\n",
            encoding="utf-8", newline="",
        )
        self.assertIn("config-duplicate-top-level-key", adapter.inspect_config(self.root)["blockers"])

        for unsafe, reason in (
            ("schema: &base spec-driven\n", "yaml-anchor"),
            ("schema: *base\n", "yaml-alias"),
            ("schema: !thing spec-driven\n", "yaml-tag"),
            ("schema: spec-driven\nrules:\n  <<: *defaults\n", "yaml-merge-key"),
        ):
            (self.root / "openspec/config.yaml").write_text(unsafe, encoding="utf-8", newline="")
            self.assertIn(reason, adapter.inspect_config(self.root)["blockers"])

    def test_store_and_external_references_block_cli(self) -> None:
        pin = self.valid_cli()
        for content, reason in (
            ("schema: spec-driven\nstore: shared-plans\n", "config-store-pointer-unsupported"),
            ("schema: spec-driven\nreferences:\n  - shared-plans\n", "config-external-references-unsupported"),
        ):
            self.write("openspec/config.yaml", content)
            audit = adapter.inspect_config(self.root)
            self.assertIn(reason, audit["blockers"])
            error = self.assert_error(
                "openspec-config-unsafe",
                adapter.run_openspec,
                self.root,
                pin,
                "schemas",
            )
            self.assertIn(reason, error.details["blockers"])
            self.assert_error(
                "config-current-unsafe",
                adapter.plan_config_candidate,
                self.root,
            )

    def test_empty_references_do_not_claim_an_external_store(self) -> None:
        self.write("openspec/config.yaml", "schema: spec-driven\nreferences: []\n")
        audit = adapter.inspect_config(self.root)
        self.assertTrue(audit["cli_allowed"])
        self.assertFalse(audit["has_references"])

    def test_single_yml_config_and_size_budgets(self) -> None:
        path = self.write("openspec/config.yml", "schema: spec-driven\n")
        audit = adapter.inspect_config(self.root)
        self.assertEqual("openspec/config.yml", audit["path"])
        self.assertTrue(audit["cli_allowed"])

        path.write_text(
            "schema: spec-driven\ncontext: |\n  " + ("x" * (adapter.MAX_CONTEXT_BYTES + 1)) + "\n",
            encoding="utf-8", newline="",
        )
        self.assertIn("config-context-too-large", adapter.inspect_config(self.root)["blockers"])

        path.write_bytes(b"schema: spec-driven\n#" + b"x" * adapter.MAX_CONFIG_BYTES)
        oversized = adapter.inspect_config(self.root)
        self.assertIn("config-too-large", oversized["blockers"])
        self.assertIsNone(oversized["digest"])

    def test_project_controlled_executable_is_rejected(self) -> None:
        script = self.write("tools/openspec", "#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        self.assert_error(
            "openspec-executable-inside-project",
            adapter.resolve_openspec_executable,
            self.root,
            explicit=script,
        )

    def test_version_gate_and_exact_argument_mapping(self) -> None:
        self.write("openspec/config.yaml", "schema: spec-driven\n")
        pin = self.valid_cli()

        status = adapter.run_openspec(self.root, pin, "status", change_id="user-login")
        validate = adapter.run_openspec(self.root, pin, "validate", change_id="user-login")
        instructions = adapter.run_openspec(
            self.root,
            pin,
            "instructions",
            change_id="user-login",
            artifact_id="proposal",
        )

        self.assertEqual(["status", "--change", "user-login", "--json"], status["result"]["argv"])
        self.assertEqual(
            ["validate", "user-login", "--strict", "--json", "--no-interactive"],
            validate["result"]["argv"],
        )
        self.assertEqual(
            ["instructions", "proposal", "--change", "user-login", "--json"],
            instructions["result"]["argv"],
        )
        self.assertEqual("0", status["result"]["telemetry"])

    def test_forbidden_lifecycle_operations_and_apply_instruction_are_unreachable(self) -> None:
        self.write("openspec/config.yaml", "schema: spec-driven\n")
        pin = self.valid_cli()
        for operation in ("apply", "verify", "sync", "archive", "anything"):
            self.assert_error(
                "openspec-operation-forbidden",
                adapter.run_openspec,
                self.root,
                pin,
                operation,
            )
        self.assert_error(
            "openspec-artifact-id-invalid",
            adapter.run_openspec,
            self.root,
            pin,
            "instructions",
            change_id="user-login",
            artifact_id="apply",
        )
        self.assert_error(
            "openspec-new-change-requires-authorized-api",
            adapter.run_openspec,
            self.root,
            pin,
            "new-change",
        )

    def test_timeout_output_limit_and_non_json_fail_closed(self) -> None:
        self.write("openspec/config.yaml", "schema: spec-driven\n")
        timeout_pin = self.make_cli(
            """
            import sys
            import time
            if sys.argv[1:] == ["--version"]:
                print("1.6.0")
            else:
                time.sleep(5)
                print("{}")
            """
        )
        self.assert_error(
            "openspec-process-timeout",
            adapter.run_openspec,
            self.root,
            timeout_pin,
            "schemas",
            timeout_seconds=0.1,
        )

        output_pin = self.make_cli(
            """
            import sys
            if sys.argv[1:] == ["--version"]:
                print("1.6.0")
            else:
                print("x" * 10000)
            """
        )
        self.assert_error(
            "openspec-output-limit-exceeded",
            adapter.run_openspec,
            self.root,
            output_pin,
            "schemas",
            max_output_bytes=1024,
        )

        json_pin = self.make_cli(
            """
            import sys
            if sys.argv[1:] == ["--version"]:
                print("1.6.0")
            else:
                print("not json")
            """
        )
        self.assert_error(
            "openspec-output-not-json",
            adapter.run_openspec,
            self.root,
            json_pin,
            "schemas",
        )

    def test_incompatible_version_fails_before_project_command(self) -> None:
        self.write("openspec/config.yaml", "schema: spec-driven\n")
        pin = self.make_cli(
            """
            import sys
            print("2.0.0" if sys.argv[1:] == ["--version"] else "{}")
            """
        )
        self.assert_error(
            "openspec-version-incompatible",
            adapter.run_openspec,
            self.root,
            pin,
            "schemas",
        )

    @unittest.skipUnless(SYMLINKS_SUPPORTED, "symlink creation requires elevated privileges on Windows")
    def test_tree_links_and_special_entries_block_process(self) -> None:
        self.write("openspec/config.yaml", "schema: spec-driven\n")
        outside = self.base / "outside.md"
        outside.write_text("outside\n", encoding="utf-8", newline="")
        (self.root / "openspec/link.md").symlink_to(outside)
        pin = self.valid_cli()

        audit = adapter.audit_openspec_tree(self.root)
        self.assertIn("openspec-tree-symbolic-link", audit["blockers"])
        self.assert_error(
            "openspec-tree-unsafe",
            adapter.run_openspec,
            self.root,
            pin,
            "schemas",
        )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO unavailable on this platform")
    def test_openspec_tree_special_entry_is_blocked(self) -> None:
        self.write("openspec/config.yaml", "schema: spec-driven\n")
        os.mkfifo(self.root / "openspec/untrusted.pipe")

        audit = adapter.audit_openspec_tree(self.root)

        self.assertIn("openspec-tree-special-entry", audit["blockers"])
        self.assertFalse(audit["safe"])

    @unittest.skipUnless(SYMLINKS_SUPPORTED, "symlink creation requires elevated privileges on Windows")
    def test_config_link_is_never_read_or_replaced(self) -> None:
        outside = self.base / "outside-config.yaml"
        outside.write_text("schema: outside\n", encoding="utf-8", newline="")
        openspec = self.root / "openspec"
        openspec.mkdir()
        (openspec / "config.yaml").symlink_to(outside)

        audit = adapter.inspect_config(self.root)

        self.assertIn("config-symbolic-link", audit["blockers"])
        self.assertIsNone(audit["digest"])
        self.assertEqual("schema: outside\n", outside.read_text(encoding="utf-8"))

    @unittest.skipUnless(SYMLINKS_SUPPORTED, "symlink creation requires elevated privileges on Windows")
    def test_openspec_root_link_and_special_entry_are_blocked(self) -> None:
        outside = self.base / "outside-openspec"
        outside.mkdir()
        (self.root / "openspec").symlink_to(outside, target_is_directory=True)
        audit = adapter.inspect_config(self.root)
        self.assertIn("openspec-root-link-or-special", audit["blockers"])
        self.assert_error(
            "config-current-unsafe",
            adapter.plan_config_candidate,
            self.root,
        )

    def test_config_plan_create_and_cas_exclusive_apply(self) -> None:
        plan = adapter.plan_config_candidate(self.root)
        self.assertEqual("create", plan["action"])
        self.assertEqual("absent", plan["expected_config_digest"])

        result = adapter.apply_config_candidate(
            self.root,
            plan["candidate"],
            candidate_digest=plan["candidate_digest"],
            expected_config_digest="absent",
            approval_ref="APPROVAL-123",
        )

        self.assertEqual("created", result["action"])
        self.assertEqual(plan["candidate_digest"], adapter.inspect_config(self.root)["digest"])
        self.assert_error(
            "config-cas-mismatch",
            adapter.apply_config_candidate,
            self.root,
            plan["candidate"],
            candidate_digest=plan["candidate_digest"],
            expected_config_digest="absent",
            approval_ref="APPROVAL-123",
        )

    def test_executable_pin_detects_replacement(self) -> None:
        pin = self.valid_cli()
        script = self.bin / self.cli_script_name()
        replacement = self.bin / "replacement"
        replacement.write_text("#!/bin/sh\necho 1.6.0\n", encoding="utf-8", newline="")
        replacement.chmod(0o755)
        os.replace(replacement, script)

        self.assert_error(
            "openspec-executable-pin-stale",
            adapter.run_openspec,
            self.root,
            pin,
            "version",
        )

    def test_executable_replacement_during_process_start_is_detected(self) -> None:
        pin = self.valid_cli()
        script = self.bin / self.cli_script_name()
        replacement = self.bin / "replacement"
        replacement.write_text("#!/bin/sh\necho 1.6.0\n", encoding="utf-8", newline="")
        replacement.chmod(0o755)
        real_popen = adapter.subprocess.Popen

        def replace_then_start(*args: object, **kwargs: object):
            os.replace(replacement, script)
            return real_popen(*args, **kwargs)

        with mock.patch.object(
            adapter.subprocess,
            "Popen",
            side_effect=replace_then_start,
        ):
            self.assert_error(
                "openspec-executable-pin-stale",
                adapter.run_openspec,
                self.root,
                pin,
                "version",
            )

    def test_existing_config_is_preserved_unless_exact_replacement_opted_in(self) -> None:
        original = "# user comment\nschema: spec-driven\ncustom: yes\n"
        path = self.write("openspec/config.yaml", original)
        old_digest = hashlib.sha256(original.encode()).hexdigest()
        candidate = "schema: spec-driven\ncontext: reviewed\n"
        candidate_digest = hashlib.sha256(candidate.encode()).hexdigest()

        self.assert_error(
            "config-existing-preserved",
            adapter.apply_config_candidate,
            self.root,
            candidate,
            candidate_digest=candidate_digest,
            expected_config_digest=old_digest,
            approval_ref="PDR-004",
        )
        self.assertEqual(original, path.read_text(encoding="utf-8"))

        result = adapter.apply_config_candidate(
            self.root,
            candidate,
            candidate_digest=candidate_digest,
            expected_config_digest=old_digest,
            approval_ref="PDR-004",
            allow_existing_replacement=True,
        )
        self.assertEqual("replaced-exact-bytes", result["action"])
        self.assertEqual(candidate, path.read_text(encoding="utf-8"))

    def test_new_change_requires_approval_config_cas_and_official_metadata(self) -> None:
        config = "schema: spec-driven\n"
        self.write("openspec/config.yaml", config)
        digest = hashlib.sha256(config.encode()).hexdigest()
        pin = self.valid_cli()

        self.assert_error(
            "config-cas-mismatch",
            adapter.create_new_change,
            self.root,
            pin,
            "user-login",
            approval_ref="PDR-1",
            expected_config_digest="0" * 64,
        )
        result = adapter.create_new_change(
            self.root,
            pin,
            "user-login",
            approval_ref="PDR-1",
            expected_config_digest=digest,
        )
        self.assertEqual("user-login", result["change_id"])
        self.assertTrue((self.root / "openspec/changes/user-login/.openspec.yaml").is_file())
        self.assert_error(
            "openspec-change-already-exists",
            adapter.create_new_change,
            self.root,
            pin,
            "user-login",
            approval_ref="PDR-1",
            expected_config_digest=digest,
        )

    @unittest.skipUnless(SYSTEM_OPENSPEC, "local OpenSpec not installed")
    def test_optional_local_openspec_1_6_integration(self) -> None:
        pin = adapter.resolve_openspec_executable(
            self.root,
            explicit=Path(SYSTEM_OPENSPEC),
        )
        version = adapter.run_openspec(self.root, pin, "version")
        schemas = adapter.run_openspec(self.root, pin, "schemas")
        self.assertTrue(version["compatible"])
        self.assertEqual("schemas", schemas["operation"])


if __name__ == "__main__":
    unittest.main()
