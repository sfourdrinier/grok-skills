# wrapper/scripts/tests/test_worktree_escape.py

import json
import os
import unittest

from groklib import GrokWrapperError, runstate
from groklib.worktree import create_external_worktree
from groklib.worktree_escape import (
    assert_changes_within,
    assert_original_checkout_unmodified,
    capture_original_checkout_baseline,
    repo_change_fingerprint,
)

from tests.worktree_test_base import WorktreeTestBase, _git


class RepoChangeFingerprintTests(WorktreeTestBase):
    def test_fingerprint_detects_rewrite_of_already_dirty_file(self) -> None:
        # Grok dogfood-4 #2 review-fs-content: a rewrite of an ALREADY-dirty file
        # changes its content fingerprint, so the before/after set difference is
        # non-empty even though the PATH set is unchanged (a path-only diff missed it).
        dirty = self.repo_root / "a.txt"
        with dirty.open("a", encoding="utf-8") as handle:
            handle.write("operator's own edit\n")
        before = repo_change_fingerprint(self.repo_root)
        self.assertIn("a.txt", {path for path, _fp in before})

        # A path-only diff would see the same {a.txt, dirty.txt} set before/after.
        with dirty.open("a", encoding="utf-8") as handle:
            handle.write("a run-attributable rewrite\n")
        after = repo_change_fingerprint(self.repo_root)
        changed = {path for path, _fp in (after - before)}
        self.assertIn("a.txt", changed, "a rewrite of an already-dirty file must be detected")
        self.assertNotIn("dirty.txt", changed, "the operator's untouched pre-existing dirt is not flagged")

    def test_fingerprint_uses_real_non_ascii_path_under_default_quotepath(self) -> None:
        # Default core.quotePath C-quotes non-ASCII names from non-z path listers
        # (e.g. "caf\303\251.txt"). Fingerprints must key on the real relative path
        # so rewrite detection and dirty-overlap never miss café.txt.
        _git(self.repo_root, "config", "core.quotePath", "true")
        dirty = self.repo_root / "café.txt"
        dirty.write_text("operator dirt v1\n", encoding="utf-8")
        before = repo_change_fingerprint(self.repo_root)
        before_paths = {path for path, _fp in before}
        self.assertIn("café.txt", before_paths)
        self.assertFalse(
            any("\\303" in path or path.startswith('"') for path in before_paths),
            "must not store C-quoted phantom keys from non-z path listing: {!r}".format(
                sorted(before_paths)
            ),
        )

        dirty.write_text("run-attributable rewrite\n", encoding="utf-8")
        after = repo_change_fingerprint(self.repo_root)
        changed = {path for path, _fp in (after - before)}
        self.assertIn(
            "café.txt",
            changed,
            "rewrite of non-ASCII dirty path must be detected under default quotePath",
        )

    def test_fingerprint_detects_same_size_mtime_rewrite_of_ignored_env(self) -> None:
        # Protected gitignored credentials cannot use size:mtime:mode alone: a
        # same-length rewrite that restores mtime is invisible to stat signatures
        # and would leave a silently-leaked .env. Content-hash + mode is required
        # for deny/snapshot-scope ignored paths; bulk caches stay stat-only.
        _git(self.repo_root, "config", "core.quotePath", "true")
        (self.repo_root / ".gitignore").write_text(".env\nnode_modules/\n", encoding="utf-8")
        _git(self.repo_root, "add", ".gitignore")
        _git(self.repo_root, "commit", "-q", "-m", "ignore env and caches")
        env = self.repo_root / ".env"
        original = b"SECRET=keep-me-xx\n"  # fixed length
        env.write_bytes(original)
        os.chmod(str(env), 0o600)
        st = env.stat()
        before = repo_change_fingerprint(self.repo_root)
        self.assertIn(".env", {path for path, _fp in before})

        # Same size, different bytes, restore mtime so a stat-only sig is unchanged.
        env.write_bytes(b"SECRET=leaked-now\n")
        os.chmod(str(env), 0o600)
        os.utime(str(env), ns=(st.st_atime_ns, st.st_mtime_ns))
        after = repo_change_fingerprint(self.repo_root)
        changed = {path for path, _fp in (after - before)}
        self.assertIn(
            ".env",
            changed,
            "same-size+mtime rewrite of protected gitignored .env must be detected",
        )

        # Bulk ignored caches may still use bounded stat-only signatures.
        cache_dir = self.repo_root / "node_modules" / "pkg"
        cache_dir.mkdir(parents=True)
        cache = cache_dir / "bundle.js"
        cache.write_bytes(b"// cache v1\n")
        st_cache = cache.stat()
        mid = repo_change_fingerprint(self.repo_root)
        cache.write_bytes(b"// cache v2\n")  # same length
        os.utime(str(cache), ns=(st_cache.st_atime_ns, st_cache.st_mtime_ns))
        late = repo_change_fingerprint(self.repo_root)
        cache_changed = {path for path, _fp in (late - mid)}
        self.assertNotIn(
            "node_modules/pkg/bundle.js",
            cache_changed,
            "bulk ignored cache same-stat rewrite may stay undetected (stat-only)",
        )

class AssertChangesWithinTests(WorktreeTestBase):
    def test_assert_changes_within_flags_outside_writes(self) -> None:
        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        # Production always passes an entry baseline (pre-existing tracked + untracked
        # dirt), so the operator's untracked dirty.txt is exempt while a run write is
        # flagged. Capture it before any escape.
        baseline = capture_original_checkout_baseline(self.repo_root)

        # In-worktree change confined to the worktree passes.
        (wt.path / "pkg" / "generated.txt").write_text("ok\n", encoding="utf-8")
        assert_changes_within(wt, (wt.path,), original_baseline=baseline)

        # A write to a TRACKED file in the ORIGINAL checkout (Grok editing real
        # source) must be flagged, while the pre-existing untracked dirty.txt is
        # tolerated (it is the operator's, not run-introduced).
        with (self.repo_root / "a.txt").open("a", encoding="utf-8") as handle:
            handle.write("escaped-write\n")
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(wt, (wt.path,), original_baseline=baseline)
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")

    def test_assert_changes_within_flags_planted_untracked_file_in_original_checkout(self) -> None:
        # original-checkout-scan-misses-untracked-new-files: a sandbox bypass that
        # PLANTS a brand-new untracked file into the operator's real checkout (not a
        # tracked edit) must be flagged; a path-only tracked-diff scan missed it.
        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        baseline = capture_original_checkout_baseline(self.repo_root)

        # Pre-existing operator dirt (dirty.txt) stays exempt; only the newly
        # planted file is a violation.
        (self.repo_root / "exfil.env").write_text("SECRET=planted\n", encoding="utf-8")
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(wt, (wt.path,), original_baseline=baseline)
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")
        self.assertTrue(any("exfil.env" in v for v in ctx.exception.detail["violations"]))
        self.assertFalse(any("dirty.txt" in v for v in ctx.exception.detail["violations"]))

    def test_worktree_change_outside_allowed_subroot_flagged(self) -> None:
        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        baseline = capture_original_checkout_baseline(self.repo_root)
        # Change lands at the worktree root, but only pkg/ is allowed.
        (wt.path / "outside.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(wt, (wt.path / "pkg",), original_baseline=baseline)
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")

    def test_assert_changes_within_tolerates_gitignored_build_artifacts(self) -> None:
        # Grok dogfood #7: a multi-package workspace build writes into per-package
        # packages/<pkg>/dist and .next, NESTED below the worktree root. These
        # are tolerated ONLY when git genuinely ignores them (a real disposable
        # build root), not merely because a same-named component appears.
        wt = self._create()
        self.addCleanup(self._force_remove, wt)

        baseline = capture_original_checkout_baseline(self.repo_root)
        (wt.path / ".gitignore").write_text("dist/\n.next/\n", encoding="utf-8")
        nested_dist = wt.path / "packages" / "foo" / "dist"
        nested_dist.mkdir(parents=True)
        (nested_dist / "bundle.js").write_text("// built\n", encoding="utf-8")
        nested_next = wt.path / "packages" / "foo" / ".next"
        nested_next.mkdir(parents=True)
        (nested_next / "manifest.json").write_text("{}\n", encoding="utf-8")

        # The gitignored artifact paths are tolerated even with a narrow root; the
        # operator's pre-existing untracked dirt is exempted by the entry baseline.
        assert_changes_within(
            wt,
            (wt.path / "pkg",),
            worktree_changed=[
                "packages/foo/dist/bundle.js",
                "packages/foo/.next/manifest.json",
            ],
            original_baseline=baseline,
        )

    def test_assert_changes_within_flags_dist_under_src_not_ignored(self) -> None:
        # Grok dogfood #5 attack: writing to packages/foo/src/dist/backdoor.ts
        # carries a "dist" component but is NOT gitignored -- it is a source-tree
        # escape and must still be flagged closed, never exempted by name.
        wt = self._create()
        self.addCleanup(self._force_remove, wt)

        baseline = capture_original_checkout_baseline(self.repo_root)
        # Only dist/ and .next/ at any level are ignored; src/dist is NOT.
        (wt.path / ".gitignore").write_text("/dist/\n", encoding="utf-8")
        backdoor = wt.path / "packages" / "foo" / "src" / "dist"
        backdoor.mkdir(parents=True)
        (backdoor / "backdoor.ts").write_text("export const pwn = 1\n", encoding="utf-8")
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(
                wt,
                (wt.path / "pkg",),
                worktree_changed=["packages/foo/src/dist/backdoor.ts"],
                original_baseline=baseline,
            )
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")

        # A plain nested SOURCE edit (no artifact component) is also flagged.
        nested_src = wt.path / "packages" / "foo" / "src"
        (nested_src / "code.ts").write_text("export const x = 1\n", encoding="utf-8")
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(
                wt, (wt.path / "pkg",), worktree_changed=["packages/foo/src/code.ts"], original_baseline=baseline
            )
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")

    def test_pre_dirty_original_file_rewritten_during_run_is_flagged(self) -> None:
        # Round5 original-checkout-baseline-path-only-exclusion: a tracked file
        # already dirty at run entry must be exempt ONLY while its content is
        # unchanged. If the run REWRITES it (an escape), it must be flagged, not
        # permanently blessed by a path-only exemption.
        pre_dirty = self.repo_root / "a.txt"
        with pre_dirty.open("a", encoding="utf-8") as handle:
            handle.write("operator's own pre-existing edit\n")
        # Baseline captured at run entry, with a.txt already dirty.
        baseline = capture_original_checkout_baseline(self.repo_root)
        self.assertIn("a.txt", baseline)

        wt = self._create()
        self.addCleanup(self._force_remove, wt)

        # Unchanged since entry -> still exempt (no false positive on operator dirt).
        assert_changes_within(wt, (wt.path,), original_baseline=baseline)

        # The run now REWRITES the already-dirty file in the REAL checkout.
        with pre_dirty.open("a", encoding="utf-8") as handle:
            handle.write("// run-attributable escape into an already-dirty file\n")
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(wt, (wt.path,), original_baseline=baseline)
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")
        self.assertTrue(any("a.txt" in v for v in ctx.exception.detail["violations"]))

    def test_mode_only_change_to_pre_dirty_original_file_is_flagged(self) -> None:
        # Round7 mode-only-change-baseline-exemption-bypass: a tracked file already
        # dirty at run entry must be exempt only while BOTH its content and its mode
        # are unchanged. A mode-only mutation (chmod +x -- content byte-identical, so
        # git hash-object is unchanged) is still a run-attributable change and must be
        # flagged, not silently blessed as pre-existing operator dirt.
        pre_dirty = self.repo_root / "a.txt"
        os.chmod(str(pre_dirty), 0o644)
        with pre_dirty.open("a", encoding="utf-8") as handle:
            handle.write("operator's own pre-existing edit\n")
        baseline = capture_original_checkout_baseline(self.repo_root)
        self.assertIn("a.txt", baseline)

        wt = self._create()
        self.addCleanup(self._force_remove, wt)

        # Unchanged content AND mode since entry -> still exempt.
        assert_changes_within(wt, (wt.path,), original_baseline=baseline)

        # A MODE-ONLY mutation with NO content change (chmod +x).
        os.chmod(str(pre_dirty), 0o755)
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(wt, (wt.path,), original_baseline=baseline)
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")
        self.assertTrue(any("a.txt" in v for v in ctx.exception.detail["violations"]))

    def test_assert_changes_within_flags_planted_gitignored_file_in_original_checkout(self) -> None:
        # Grok r5 #4: the original-checkout escape scan must flag ANY modification
        # including gitignored files -- a sandbox bypass that PLANTS a gitignored file
        # (repo/.env.local, repo/node_modules/.bin/evil) in the operator's real
        # checkout was invisible to the --exclude-standard scan. The .gitignore is
        # captured into the baseline first (exempt operator dirt); only the newly
        # planted ignored file is a violation.
        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        (self.repo_root / ".gitignore").write_text("secret/\n*.local\n", encoding="utf-8")
        baseline = capture_original_checkout_baseline(self.repo_root)

        # Plant a gitignored tree (deep file) AND a root ignored file. Inventory
        # collapses ignored directories to one entry (issue #7 --directory); the
        # planted tree still appears as a NEW path vs baseline (secret/), and
        # the root file still lists individually.
        (self.repo_root / "secret").mkdir()
        (self.repo_root / "secret" / "evil.bin").write_text("exfil\n", encoding="utf-8")
        (self.repo_root / "creds.local").write_text("SECRET=planted\n", encoding="utf-8")
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(wt, (wt.path,), original_baseline=baseline)
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")
        violations = ctx.exception.detail["violations"]
        self.assertTrue(
            any(
                "secret/evil.bin" in v or v.rstrip("/").endswith("secret") or "/secret" in v
                for v in violations
            ),
            violations,
        )
        self.assertTrue(any("creds.local" in v for v in violations), violations)
        # Pre-existing operator dirt (dirty.txt) stays exempt.
        self.assertFalse(any("dirty.txt" in v for v in violations))

    def test_original_checkout_artifact_named_edit_always_flagged(self) -> None:
        # Escape detection of edits in the operator's REAL checkout must NEVER be
        # softened by artifact tolerance: a tracked file under a "build"-named
        # directory, newly diverged during the run, is still an escape.
        artifact_dir = self.repo_root / "build"
        artifact_dir.mkdir()
        tracked = artifact_dir / "orchestrate.ts"
        tracked.write_text("export const v = 1\n", encoding="utf-8")
        _git(self.repo_root, "add", "-A")
        _git(self.repo_root, "commit", "-q", "-m", "add tracked build file")
        base = _git(self.repo_root, "rev-parse", "HEAD").strip()

        run_id = runstate.new_run_id()
        wt = create_external_worktree(repo_root=self.repo_root, base=base, run_id=run_id)
        self.addCleanup(self._force_remove, wt)
        baseline = capture_original_checkout_baseline(self.repo_root)

        # Grok "escapes" and edits the tracked build file in the REAL checkout.
        with tracked.open("a", encoding="utf-8") as handle:
            handle.write("// escaped edit\n")
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_changes_within(wt, (wt.path,), original_baseline=baseline)
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")

    def test_original_checkout_unmodified_flags_post_gate_write_but_not_worktree_output(self) -> None:
        # PR968 codex post-build-gate: the re-scan run AFTER the build gate must flag a
        # write into the operator's REAL checkout (a Grok-modified build script escaping)
        # while ignoring the isolated worktree's OWN build outputs, so a legitimate gate
        # that only writes inside the worktree never false-positives.
        wt = self._create()
        self.addCleanup(self._force_remove, wt)
        baseline = capture_original_checkout_baseline(self.repo_root)

        # A legitimate build writes only inside the worktree (its own outputs) -- the
        # original-checkout re-scan must ignore it entirely.
        (wt.path / "pkg" / "built.js").write_text("// built\n", encoding="utf-8")
        assert_original_checkout_unmodified(wt, (wt.path,), original_baseline=baseline)

        # A Grok-modified build step escaping into the REAL checkout is flagged, while
        # the operator's pre-existing untracked dirty.txt stays exempt.
        (self.repo_root / "gate-escaped.txt").write_text("planted by build\n", encoding="utf-8")
        with self.assertRaises(GrokWrapperError) as ctx:
            assert_original_checkout_unmodified(wt, (wt.path,), original_baseline=baseline)
        self.assertEqual(ctx.exception.error_class, "unexpected-edits")
        self.assertEqual(ctx.exception.detail.get("phase"), "post-build-gate")
        self.assertTrue(any("gate-escaped.txt" in v for v in ctx.exception.detail["violations"]))
        self.assertFalse(any("dirty.txt" in v for v in ctx.exception.detail["violations"]))
