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


if __name__ == "__main__":
    unittest.main()
