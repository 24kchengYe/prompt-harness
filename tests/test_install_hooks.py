from __future__ import annotations

import importlib.util
import json
import uuid
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO / "tests" / "_artifacts"
SCRIPT = REPO / "scripts" / "prompt_harness.py"

spec = importlib.util.spec_from_file_location("install_hooks", REPO / "scripts" / "install_hooks.py")
assert spec and spec.loader
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class HookInstallerTests(unittest.TestCase):
    def test_preserves_unrelated_hooks_backs_up_and_is_idempotent(self) -> None:
        base = ARTIFACTS / f"installer-{uuid.uuid4().hex[:8]}"
        base.mkdir(parents=True)
        settings = base / "settings.json"
        original = {
            "theme": "dark",
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": "python unrelated.py"}]}
                ]
            },
        }
        settings.write_text(json.dumps(original), encoding="utf-8")
        first = installer.update_file(settings, SCRIPT, "claude", remove=False, dry_run=False)
        self.assertTrue(first["changed"])
        self.assertTrue(Path(first["backup"]).exists())
        current = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(current["theme"], "dark")
        self.assertEqual(len(current["hooks"]["UserPromptSubmit"]), 2)
        second = installer.update_file(settings, SCRIPT, "claude", remove=False, dry_run=False)
        self.assertFalse(second["changed"])

    def test_stop_recovery_preserves_existing_stop_hook(self) -> None:
        base = ARTIFACTS / f"stop-installer-{uuid.uuid4().hex[:8]}"
        base.mkdir(parents=True)
        hooks_path = base / "hooks.json"
        hooks_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {"hooks": [{"type": "command", "command": "python existing_stop.py"}]}
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        first = installer.update_stop_recovery_file(
            hooks_path,
            SCRIPT,
            remove=False,
            dry_run=False,
        )
        self.assertTrue(first["changed"])
        current = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(len(current["hooks"]["Stop"]), 2)
        commands = [entry["hooks"][0]["command"] for entry in current["hooks"]["Stop"]]
        self.assertIn("python existing_stop.py", commands)
        self.assertTrue(any("capture-stop-recovery" in command for command in commands))
        second = installer.update_stop_recovery_file(
            hooks_path,
            SCRIPT,
            remove=False,
            dry_run=False,
        )
        self.assertFalse(second["changed"])


if __name__ == "__main__":
    unittest.main()
