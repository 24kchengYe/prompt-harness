from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO / "tests" / "_artifacts"


def retained_workspace(name: str) -> Path:
    path = ARTIFACTS / f"{name}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True)
    return path


def windows_hook_code(event: str = "UserPromptSubmit") -> str:
    hooks = json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    command = hooks["hooks"][event][0]["hooks"][0]["commandWindows"]
    prefix = 'python -c "'
    if not command.startswith(prefix) or not command.endswith('"'):
        raise AssertionError(f"Unexpected hook command: {command}")
    return command[len(prefix) : -1]


def install_fake_plugin(root: Path, *, broken_script: bool = False) -> None:
    for relative in ("hooks", "scripts", "assets"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "hooks" / "run_capture.py", root / "hooks" / "run_capture.py")
    shutil.copy2(REPO / "hooks" / "run_stop_capture.py", root / "hooks" / "run_stop_capture.py")
    if broken_script:
        (root / "scripts" / "prompt_harness.py").write_text(
            "raise RuntimeError('synthetic launcher failure')\n",
            encoding="utf-8",
        )
    else:
        shutil.copy2(REPO / "scripts" / "prompt_harness.py", root / "scripts" / "prompt_harness.py")
        shutil.copy2(REPO / "assets" / "timeline.html", root / "assets" / "timeline.html")


class HookLauncherTests(unittest.TestCase):
    def run_hook(
        self,
        base: Path,
        payload: dict,
        *,
        plugin_root: Path,
        event: str = "UserPromptSubmit",
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "PLUGIN_ROOT": str(plugin_root),
                "CODEX_HOME": str(base / "codex-home"),
                "PROMPT_HARNESS_HOME": str(base / "harness-home"),
                "PROMPT_HARNESS_DISABLE_AUTO_SYNC": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        return subprocess.run(
            [sys.executable, "-c", windows_hook_code(event)],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=base,
            env=env,
            timeout=20,
            check=False,
        )

    def test_deleted_loaded_version_falls_forward_to_current_cache(self) -> None:
        base = retained_workspace("hook-fall-forward")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        current = base / "codex-home" / "plugins" / "cache" / "personal" / "prompt-harness" / "9.9.9"
        install_fake_plugin(current)
        deleted_old_root = base / "codex-home" / "plugins" / "cache" / "personal" / "prompt-harness" / "0.2.0"
        payload = {
            "session_id": "cli-old-version",
            "turn_id": "turn-1",
            "cwd": str(project),
            "hook_event_name": "UserPromptSubmit",
            "model": "gpt-test",
            "prompt": "旧任务在插件升级后仍应写入",
            "timestamp": "2026-07-15T01:00:00.000Z",
        }

        result = self.run_hook(base, payload, plugin_root=deleted_old_root)

        self.assertEqual(result.returncode, 0, result.stderr)
        event_files = list((project / ".prompt-harness" / "events").rglob("*.jsonl"))
        self.assertEqual(len(event_files), 1)
        event = json.loads(event_files[0].read_text(encoding="utf-8").strip())
        self.assertEqual(event["prompt"]["text"], payload["prompt"])
        self.assertEqual(event["source"]["mode"], "hook")

    def test_deleted_loaded_version_stop_falls_forward_to_current_cache(self) -> None:
        base = retained_workspace("stop-hook-fall-forward")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        codex_home = base / "codex-home"
        current = codex_home / "plugins" / "cache" / "personal" / "prompt-harness" / "9.9.9"
        install_fake_plugin(current)
        session_id = "stop-old-version"
        rollout = codex_home / "sessions" / "2026" / "07" / f"rollout-{session_id}.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text(
            "\n".join(
                (
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {"id": session_id, "cwd": str(project)},
                        }
                    ),
                    json.dumps(
                        {
                            "timestamp": "2026-07-16T12:30:00.000Z",
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "Stop 回退检查 😀"}],
                            },
                        },
                        ensure_ascii=False,
                    ),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        deleted_old_root = codex_home / "plugins" / "cache" / "personal" / "prompt-harness" / "0.2.0"

        result = self.run_hook(
            base,
            {"session_id": session_id, "cwd": str(project)},
            plugin_root=deleted_old_root,
            event="Stop",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        event_files = list((project / ".prompt-harness" / "events").rglob("*.jsonl"))
        self.assertEqual(len(event_files), 1)
        captured = json.loads(event_files[0].read_text(encoding="utf-8").strip())
        self.assertEqual(captured["prompt"]["text"], "Stop 回退检查 😀")
        self.assertEqual(captured["source"]["mode"], "stop_recovery")

    def test_missing_old_and_current_plugin_is_a_safe_noop(self) -> None:
        base = retained_workspace("hook-no-candidate")
        payload = {
            "session_id": "missing-plugin",
            "cwd": str(base),
            "hook_event_name": "UserPromptSubmit",
            "prompt": "should not matter",
        }

        result = self.run_hook(base, payload, plugin_root=base / "deleted-plugin")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_launcher_suppresses_runtime_failure_and_logs_no_prompt_body(self) -> None:
        base = retained_workspace("hook-runtime-error")
        current = base / "codex-home" / "plugins" / "cache" / "personal" / "prompt-harness" / "9.9.9"
        install_fake_plugin(current, broken_script=True)
        prompt = "PROMPT_BODY_MUST_NOT_APPEAR_IN_DIAGNOSTICS"
        payload = {
            "session_id": "broken-runtime",
            "cwd": str(base),
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
        }

        result = self.run_hook(base, payload, plugin_root=base / "deleted-plugin")

        self.assertEqual(result.returncode, 0, result.stderr)
        log = base / "harness-home" / "state" / "plugin-hook-errors.jsonl"
        self.assertTrue(log.is_file())
        diagnostic = log.read_text(encoding="utf-8")
        self.assertIn("synthetic launcher failure", diagnostic)
        self.assertNotIn(prompt, diagnostic)


if __name__ == "__main__":
    unittest.main()
