#!/usr/bin/env python3
"""Adversarial tests for the M3 read-only Git checkout identity adapter."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import git_scope


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


class GitScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="delivery-git-scope-")
        self.sandbox = Path(self.temporary.name).resolve()
        self.repo = self.sandbox / "primary"
        self.repo.mkdir()
        (self.repo / "app.txt").write_text("baseline\n", encoding="utf-8", newline="")
        self.git(self.repo, "init", "-q")
        self.git(self.repo, "config", "user.name", "M3 Git Scope Fixture")
        self.git(self.repo, "config", "user.email", "m3@example.invalid")
        # Keep checkout bytes independent of the host's global autocrlf so a
        # linked worktree stays clean on Windows hosts.
        self.git(self.repo, "config", "core.autocrlf", "false")
        self.git(self.repo, "add", "--", "app.txt")
        self.git(self.repo, "commit", "-q", "-m", "fixture baseline")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if check:
            self.assertEqual(0, result.returncode, result.stderr)
        return result

    def add_linked_worktree(self, name: str = "linked") -> tuple[Path, Path, Path]:
        linked = self.sandbox / name
        self.git(self.repo, "worktree", "add", "-q", "-b", f"fixture-{name}", str(linked), "HEAD")
        marker = linked / ".git"
        marker_line = marker.read_text(encoding="utf-8").strip()
        self.assertTrue(marker_line.startswith("gitdir: "))
        git_dir = Path(marker_line[len("gitdir: ") :])
        if not git_dir.is_absolute():
            git_dir = marker.parent / git_dir
        git_dir = git_dir.resolve()
        common_dir = (git_dir / (git_dir / "commondir").read_text(encoding="utf-8").strip()).resolve()
        return linked.resolve(), git_dir, common_dir

    def inspect_linked(
        self,
        linked: Path,
        git_dir: Path,
        common_dir: Path,
        *,
        authority_ref: str = "PDR-WORKTREE-001",
        excluded_paths: tuple[str, ...] = (),
    ) -> dict[str, object]:
        return git_scope.inspect_git_scope(
            linked,
            expected_git_dir=git_dir,
            expected_common_dir=common_dir,
            authority_ref=authority_ref,
            excluded_paths=excluded_paths,
        )

    def metadata_inventory(self, root: Path) -> dict[str, tuple[object, ...]]:
        result: dict[str, tuple[object, ...]] = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                result[relative] = ("symlink", os.readlink(path))
            elif stat.S_ISDIR(metadata.st_mode):
                result[relative] = ("directory", stat.S_IMODE(metadata.st_mode))
            elif stat.S_ISREG(metadata.st_mode):
                result[relative] = (
                    "file",
                    stat.S_IMODE(metadata.st_mode),
                    metadata.st_size,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            else:
                result[relative] = ("special", stat.S_IMODE(metadata.st_mode))
        return result

    def assert_error(self, code: str, callable_object: object) -> None:
        with self.assertRaises(git_scope.GitScopeError) as raised:
            callable_object()  # type: ignore[operator]
        self.assertEqual(code, raised.exception.code)

    def test_main_checkout_identity_is_path_free_stable_and_read_only(self) -> None:
        metadata_before = self.metadata_inventory(self.repo / ".git")

        first = git_scope.inspect_git_scope(self.repo.resolve())
        second = git_scope.inspect_git_scope(self.repo.resolve())

        self.assertEqual(first, second)
        self.assertEqual("main", first["checkout_kind"])
        self.assertTrue(first["integration_owner"])
        self.assertFalse(first["detached"])
        self.assertFalse(first["dirty"])
        self.assertRegex(first["head"], r"^[0-9a-f]{40}$")
        self.assertTrue(str(first["ref"]).startswith("refs/heads/"))
        encoded = json.dumps(first, sort_keys=True)
        self.assertNotIn(str(self.repo), encoded)
        self.assertNotIn(str(self.repo / ".git"), encoded)
        self.assertEqual(metadata_before, self.metadata_inventory(self.repo / ".git"))

    def test_checkout_drift_between_git_snapshots_is_rejected(self) -> None:
        original = git_scope._cross_check_git
        calls = 0

        def change_after_first_capture(*args: object, **kwargs: object) -> tuple[object, ...]:
            nonlocal calls
            result = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                (self.repo / "app.txt").write_text("changed during capture\n", encoding="utf-8", newline="")
            return result

        with mock.patch.object(
            git_scope,
            "_cross_check_git",
            side_effect=change_after_first_capture,
        ):
            self.assert_error(
                "git_scope.checkout_changed",
                lambda: git_scope.inspect_git_scope(self.repo.resolve()),
            )
        self.assertEqual(2, calls)

    def test_real_linked_worktree_has_shared_repository_and_distinct_checkout_identity(self) -> None:
        linked, git_dir, common_dir = self.add_linked_worktree()
        metadata_before = self.metadata_inventory(common_dir)

        main = git_scope.inspect_git_scope(self.repo.resolve())
        result = self.inspect_linked(linked, git_dir, common_dir)

        self.assertEqual("linked", result["checkout_kind"])
        self.assertFalse(result["integration_owner"])
        self.assertEqual("PDR-WORKTREE-001", result["authority_ref"])
        self.assertEqual(main["repository_scope_id"], result["repository_scope_id"])
        self.assertNotEqual(main["checkout_scope_id"], result["checkout_scope_id"])
        self.assertFalse(result["dirty"])
        encoded = json.dumps(result, sort_keys=True)
        for local_path in (linked, git_dir, common_dir, self.repo):
            self.assertNotIn(str(local_path), encoded)
        self.assertEqual(metadata_before, self.metadata_inventory(common_dir))

        (linked / "delivery-docs" / "state").mkdir(parents=True)
        (linked / "delivery-docs" / "state" / "state.json").write_text("{}\n", encoding="utf-8", newline="")
        excluded = self.inspect_linked(
            linked,
            git_dir,
            common_dir,
            excluded_paths=("delivery-docs/state/state.json",),
        )
        self.assertFalse(excluded["dirty"])
        self.assertEqual(result["repository_scope_id"], excluded["repository_scope_id"])
        self.assertNotEqual(result["excluded_paths_digest"], excluded["excluded_paths_digest"])

    def test_linked_worktree_without_complete_authority_stops_before_external_metadata(self) -> None:
        linked, git_dir, common_dir = self.add_linked_worktree()
        observed: list[Path] = []
        original = git_scope._lstat_kind

        def recording_lstat(path: Path, label: str) -> os.stat_result:
            observed.append(path)
            return original(path, label)

        with mock.patch.object(git_scope, "_lstat_kind", side_effect=recording_lstat):
            self.assert_error(
                "git_scope.authority_required",
                lambda: git_scope.inspect_git_scope(linked),
            )
        self.assertTrue(observed)
        self.assertTrue(all(path == linked / ".git" for path in observed))

        self.assert_error(
            "git_scope.authority_required",
            lambda: git_scope.inspect_git_scope(
                linked,
                expected_git_dir=git_dir,
                expected_common_dir=common_dir,
            ),
        )

    def test_wrong_root_pointer_is_rejected_before_external_metadata_access(self) -> None:
        linked, git_dir, common_dir = self.add_linked_worktree()
        marker = linked / ".git"
        if os.name == "nt":
            # Git for Windows marks the worktree pointer file hidden, and
            # truncating a hidden file fails with ACCESS_DENIED; replace it.
            marker.unlink()
        marker.write_text(f"gitdir: {git_dir.parent / 'other'}\n", encoding="utf-8", newline="")
        observed: list[Path] = []
        original = git_scope._lstat_kind

        def recording_lstat(path: Path, label: str) -> os.stat_result:
            observed.append(path)
            return original(path, label)

        with mock.patch.object(git_scope, "_lstat_kind", side_effect=recording_lstat):
            self.assert_error(
                "git_scope.authority_mismatch",
                lambda: self.inspect_linked(linked, git_dir, common_dir),
            )
        self.assertTrue(all(path == marker for path in observed))

    def test_reverse_gitdir_mismatch_is_rejected(self) -> None:
        linked, git_dir, common_dir = self.add_linked_worktree()
        backlink = git_dir / "gitdir"
        backlink.write_text(str(self.sandbox / "wrong" / ".git") + "\n", encoding="utf-8", newline="")

        self.assert_error(
            "git_scope.backlink_mismatch",
            lambda: self.inspect_linked(linked, git_dir, common_dir),
        )

    def test_common_dir_mismatch_is_rejected(self) -> None:
        linked, git_dir, common_dir = self.add_linked_worktree()
        (git_dir / "commondir").write_text("../not-the-common-dir\n", encoding="utf-8", newline="")

        self.assert_error(
            "git_scope.common_dir_mismatch",
            lambda: self.inspect_linked(linked, git_dir, common_dir),
        )

    @unittest.skipUnless(SYMLINKS_SUPPORTED, "symlink creation requires elevated privileges on Windows")
    def test_authorized_git_dir_symlink_is_rejected(self) -> None:
        linked, git_dir, common_dir = self.add_linked_worktree()
        alias = git_dir.parent / "linked-alias"
        alias.symlink_to(git_dir, target_is_directory=True)
        (linked / ".git").write_text(f"gitdir: {alias}\n", encoding="utf-8", newline="")

        self.assert_error(
            "git_scope.metadata_symlink",
            lambda: self.inspect_linked(linked, alias, common_dir),
        )

    @unittest.skipUnless(SYMLINKS_SUPPORTED, "symlink creation requires elevated privileges on Windows")
    def test_symlink_inside_git_metadata_is_rejected(self) -> None:
        target = self.sandbox / "outside"
        target.write_text("outside\n", encoding="utf-8", newline="")
        (self.repo / ".git" / "scope-link").symlink_to(target)

        self.assert_error(
            "git_scope.metadata_symlink",
            lambda: git_scope.inspect_git_scope(self.repo.resolve()),
        )

    def test_dirty_digest_changes_when_checkout_becomes_dirty(self) -> None:
        clean = git_scope.inspect_git_scope(self.repo.resolve())
        (self.repo / "app.txt").write_text("changed\n", encoding="utf-8", newline="")

        dirty = git_scope.inspect_git_scope(self.repo.resolve())
        (self.repo / "app.txt").write_text("changed again\n", encoding="utf-8", newline="")
        changed_again = git_scope.inspect_git_scope(self.repo.resolve())
        (self.repo / "untracked.txt").write_text("first\n", encoding="utf-8", newline="")
        untracked_first = git_scope.inspect_git_scope(self.repo.resolve())
        (self.repo / "untracked.txt").write_text("second\n", encoding="utf-8", newline="")
        untracked_second = git_scope.inspect_git_scope(self.repo.resolve())

        self.assertFalse(clean["dirty"])
        self.assertTrue(dirty["dirty"])
        self.assertNotEqual(clean["dirty_digest"], dirty["dirty_digest"])
        self.assertNotEqual(dirty["dirty_digest"], changed_again["dirty_digest"])
        self.assertNotEqual(untracked_first["dirty_digest"], untracked_second["dirty_digest"])
        self.assertNotIn("app.txt", json.dumps(dirty))
        self.assertNotIn("untracked.txt", json.dumps(untracked_second))

    def test_excluded_tracked_and_untracked_authorities_do_not_self_invalidate(self) -> None:
        delivery = self.repo / "delivery-docs" / "state"
        delivery.mkdir(parents=True)
        state = delivery / "state.json"
        manifest = delivery / "planning-manifest.json"
        state.write_text('{"revision": 0}\n', encoding="utf-8", newline="")
        self.git(self.repo, "add", "--", "delivery-docs/state/state.json")
        self.git(self.repo, "commit", "-q", "-m", "add control authority")
        exclusions = ("delivery-docs/state/state.json", "delivery-docs/state/planning-manifest.json")

        baseline = git_scope.inspect_git_scope(self.repo.resolve(), excluded_paths=exclusions)
        state.write_text('{"revision": 1}\n', encoding="utf-8", newline="")
        manifest.write_text('{"seal": 1}\n', encoding="utf-8", newline="")
        first = git_scope.inspect_git_scope(self.repo.resolve(), excluded_paths=exclusions)
        state.write_text('{"revision": 2}\n', encoding="utf-8", newline="")
        manifest.write_text('{"seal": 2}\n', encoding="utf-8", newline="")
        second = git_scope.inspect_git_scope(self.repo.resolve(), excluded_paths=exclusions)

        self.assertFalse(baseline["dirty"])
        self.assertFalse(first["dirty"])
        self.assertFalse(second["dirty"])
        self.assertEqual(baseline["dirty_digest"], first["dirty_digest"])
        self.assertEqual(first["dirty_digest"], second["dirty_digest"])
        self.assertEqual(first["excluded_paths_digest"], second["excluded_paths_digest"])
        encoded = json.dumps(second, sort_keys=True)
        self.assertNotIn("state.json", encoded)
        self.assertNotIn("planning-manifest.json", encoded)

        default_scope = git_scope.inspect_git_scope(self.repo.resolve())
        self.assertTrue(default_scope["dirty"])
        self.assertNotEqual(default_scope["excluded_paths_digest"], second["excluded_paths_digest"])

        (self.repo / "app.txt").write_text("unexcluded first\n", encoding="utf-8", newline="")
        unexcluded_first = git_scope.inspect_git_scope(
            self.repo.resolve(), excluded_paths=exclusions
        )
        (self.repo / "app.txt").write_text("unexcluded second\n", encoding="utf-8", newline="")
        unexcluded_second = git_scope.inspect_git_scope(
            self.repo.resolve(), excluded_paths=exclusions
        )
        self.assertTrue(unexcluded_first["dirty"])
        self.assertNotEqual(
            unexcluded_first["dirty_digest"], unexcluded_second["dirty_digest"]
        )

    def test_excluded_paths_reject_escape_option_and_pathspec_injection(self) -> None:
        invalid = (
            "../escape",
            "/absolute/path",
            "--help",
            ":(glob)**",
            "docs//file.json",
            "docs/../../escape",
            "docs\\windows-path",
            "docs/:(exclude)file",
            "docs/path with spaces",
        )
        for value in invalid:
            with self.subTest(value=value):
                self.assert_error(
                    "git_scope.exclude_invalid",
                    lambda value=value: git_scope.inspect_git_scope(
                        self.repo.resolve(), excluded_paths=(value,)
                    ),
                )

    @unittest.skipUnless(SYMLINKS_SUPPORTED, "symlink creation requires elevated privileges on Windows")
    def test_excluded_paths_reject_link_and_non_directory_parents(self) -> None:
        outside = self.sandbox / "outside-exclusion"
        outside.mkdir()
        linked_parent = self.repo / "linked-parent"
        linked_parent.symlink_to(outside, target_is_directory=True)
        self.assert_error(
            "git_scope.exclude_parent_unsafe",
            lambda: git_scope.inspect_git_scope(
                self.repo.resolve(), excluded_paths=("linked-parent/state.json",)
            ),
        )

        non_directory = self.repo / "not-a-directory"
        non_directory.write_text("file\n", encoding="utf-8", newline="")
        self.assert_error(
            "git_scope.exclude_parent_unsafe",
            lambda: git_scope.inspect_git_scope(
                self.repo.resolve(), excluded_paths=("not-a-directory/state.json",)
            ),
        )

        delivery = self.repo / "delivery-docs" / "state"
        delivery.mkdir(parents=True)
        target = delivery / "state.json"
        target.symlink_to(self.repo / "app.txt")
        self.assert_error(
            "git_scope.exclude_parent_unsafe",
            lambda: git_scope.inspect_git_scope(
                self.repo.resolve(), excluded_paths=("delivery-docs/state/state.json",)
            ),
        )

    def test_detached_head_is_reported_without_fabricating_a_ref(self) -> None:
        self.git(self.repo, "checkout", "-q", "--detach", "HEAD")

        result = git_scope.inspect_git_scope(self.repo.resolve())

        self.assertTrue(result["detached"])
        self.assertIsNone(result["ref"])
        self.assertRegex(result["head"], r"^[0-9a-f]{40}$")

    def test_dangerous_config_is_rejected_before_git_runs(self) -> None:
        config = self.repo / ".git" / "config"
        with config.open("a", encoding="utf-8") as handle:
            handle.write("\n[include]\n\tpath = /tmp/hostile-config\n")

        with mock.patch.object(git_scope, "_run_git") as run:
            self.assert_error(
                "git_scope.config_unsafe",
                lambda: git_scope.inspect_git_scope(self.repo.resolve()),
            )
        run.assert_not_called()

    def test_filter_config_and_attributes_are_rejected(self) -> None:
        config = self.repo / ".git" / "config"
        with config.open("a", encoding="utf-8") as handle:
            handle.write("\n[filter \"hostile\"]\n\tclean = /tmp/hostile-filter\n")
        self.assert_error(
            "git_scope.config_unsafe",
            lambda: git_scope.inspect_git_scope(self.repo.resolve()),
        )

        text = config.read_text(encoding="utf-8")
        config.write_text(text.split("\n[filter ", 1)[0] + "\n", encoding="utf-8", newline="")
        (self.repo / ".gitattributes").write_text("*.txt filter=hostile\n", encoding="utf-8", newline="")
        self.assert_error(
            "git_scope.attribute_unsafe",
            lambda: git_scope.inspect_git_scope(self.repo.resolve()),
        )

    def test_alternates_grafts_and_replace_refs_are_rejected(self) -> None:
        cases = [
            (self.repo / ".git" / "objects" / "info" / "alternates", "../outside\n"),
            (self.repo / ".git" / "info" / "grafts", "deadbeef\n"),
            (self.repo / ".git" / "refs" / "replace" / "deadbeef", "deadbeef\n"),
        ]
        for path, content in cases:
            with self.subTest(path=path.relative_to(self.repo)):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="")
                self.assert_error(
                    "git_scope.metadata_indirection",
                    lambda: git_scope.inspect_git_scope(self.repo.resolve()),
                )
                path.unlink()
                # Remove only empty directories created by this fixture.
                parent = path.parent
                while parent != self.repo / ".git" and parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent

    def test_partial_or_mismatched_authority_is_rejected_for_main_checkout(self) -> None:
        self.assert_error(
            "git_scope.authority_invalid",
            lambda: git_scope.inspect_git_scope(
                self.repo.resolve(), expected_git_dir=self.repo / ".git"
            ),
        )
        self.assert_error(
            "git_scope.authority_mismatch",
            lambda: git_scope.inspect_git_scope(
                self.repo.resolve(),
                expected_git_dir=self.sandbox,
                expected_common_dir=self.sandbox,
                authority_ref="PDR-WRONG",
            ),
        )

    def test_path_like_authority_reference_is_not_returned(self) -> None:
        linked, git_dir, common_dir = self.add_linked_worktree()
        self.assert_error(
            "git_scope.authority_invalid",
            lambda: self.inspect_linked(
                linked,
                git_dir,
                common_dir,
                authority_ref="/private/tmp/local-authority",
            ),
        )

    def test_inherited_trace_and_project_controlled_git_cannot_execute(self) -> None:
        trace = self.sandbox / "inherited-trace.log"
        tools = self.repo / "tools"
        tools.mkdir()
        canary = self.repo / "PROJECT_GIT_EXECUTED"
        fake_git = tools / "git"
        fake_git.write_text(
            f"#!/bin/sh\nprintf canary > '{canary}'\nexit 99\n",
            encoding="utf-8", newline="",
        )
        fake_git.chmod(0o755)
        with mock.patch.dict(
            os.environ,
            {
                "PATH": str(tools) + os.pathsep + os.environ.get("PATH", os.defpath),
                "GIT_TRACE": str(trace),
                "GIT_TRACE2_EVENT": str(trace),
            },
        ):
            result = git_scope.inspect_git_scope(self.repo.resolve())

        self.assertEqual("main", result["checkout_kind"])
        self.assertFalse(trace.exists())
        self.assertFalse(canary.exists())


if __name__ == "__main__":
    unittest.main()
