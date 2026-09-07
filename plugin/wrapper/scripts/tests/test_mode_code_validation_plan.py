# wrapper/scripts/tests/test_mode_code_validation_plan.py
#
# F10: production _run_build_gate must call validation_plan and honor targeted.

import json
import pathlib
import tempfile
import types
import unittest
from unittest import mock

from groklib.modes import code as code_mod
from groklib.projectconfig import validation_plan as real_plan


class BuildGateValidationPlanTests(unittest.TestCase):
    def test_run_build_gate_calls_validation_plan_and_targeted_skips_build(self) -> None:
        import inspect
        import shutil

        src = inspect.getsource(code_mod._run_build_gate)
        self.assertIn(
            "validation_plan(",
            src,
            "production _run_build_gate must call validation_plan",
        )

        tmp = pathlib.Path(tempfile.mkdtemp(prefix="grok-gate-plan-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, True))
        (tmp / "package.json").write_text(
            json.dumps({"name": "pkg", "scripts": {"build": "true", "test": "true"}}),
            encoding="utf-8",
        )
        seen = []

        def spy(level, commands=None, pinned=None):
            seen.append(level)
            return real_plan(level, commands, pinned=pinned)

        recorded = []

        def fake_run(argv, cwd, purpose):
            recorded.append(purpose)
            return {"purpose": purpose, "exitStatus": 0, "argv": argv}

        stage = types.SimpleNamespace(
            worktree=types.SimpleNamespace(path=tmp),
            acc=types.SimpleNamespace(commands=[], warnings=[]),
            progress=types.SimpleNamespace(safe_emit=lambda *a, **k: None),
        )
        with mock.patch.object(code_mod, "validation_plan", spy), mock.patch.object(
            code_mod, "_run_recorded_command", fake_run
        ):
            code_mod._run_build_gate(
                stage,
                "",
                "npm",
                None,
                {},
                "pkg",
                {"build": "true", "test": "true"},
                validation_level="targeted",
            )
        self.assertEqual(seen, ["targeted"])
        self.assertEqual(recorded, ["build-gate:test"])

    def test_direct_build_gate_forwards_validation_level(self) -> None:
        import inspect
        import shutil

        from groklib.modes import direct_finalize

        src = inspect.getsource(direct_finalize._run_build_gate_for_direct)
        self.assertIn(
            "validation_level",
            src,
            "direct finalize must forward --validation into _run_build_gate",
        )
        fin_src = inspect.getsource(direct_finalize.finalize_direct)
        self.assertIn(
            "validation_level",
            fin_src,
            "finalize_direct must pass validation_level to the build gate",
        )
        from groklib.modes import direct as direct_mod

        run_src = inspect.getsource(direct_mod.run)
        self.assertIn(
            "validation",
            run_src,
            "direct.run must pass args.validation into finalize_direct",
        )

        tmp = pathlib.Path(tempfile.mkdtemp(prefix="grok-direct-gate-plan-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, True))
        (tmp / "package.json").write_text(
            json.dumps({"name": "pkg", "scripts": {"build": "true", "test": "true"}}),
            encoding="utf-8",
        )
        seen_levels = []

        def spy_gate(stage, *args, **kwargs):
            seen_levels.append(kwargs.get("validation_level", args[6] if len(args) > 6 else "missing"))

        stage = types.SimpleNamespace(
            repo_root=tmp,
            acc=types.SimpleNamespace(commands=[], warnings=[]),
            progress=types.SimpleNamespace(safe_emit=lambda *a, **k: None),
        )
        with mock.patch.object(code_mod, "_run_build_gate", spy_gate):
            direct_finalize._run_build_gate_for_direct(
                stage,
                target_relative="",
                package_manager="npm",
                pm_binary=None,
                never_build_workspaces={},
                original_workspace_name="pkg",
                pristine_scripts={"build": "true", "test": "true"},
                validation_level="targeted",
            )
        self.assertEqual(seen_levels, ["targeted"])
