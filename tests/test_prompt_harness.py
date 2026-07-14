from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "prompt_harness.py"
ARTIFACTS = REPO / "tests" / "_artifacts"

spec = importlib.util.spec_from_file_location("prompt_harness", SCRIPT)
assert spec and spec.loader
ph = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ph)


def retained_workspace(name: str) -> Path:
    path = ARTIFACTS / f"{name}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True)
    return path


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class PromptHarnessTests(unittest.TestCase):
    def test_hook_privacy_and_repeated_prompt_identity(self) -> None:
        base = retained_workspace("hook")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        home = base / "harness-home"
        fake_key = "sk-" + "A" * 32
        image_data = "iVBOR" + "A" * 200
        prompt = f"Read G:\\data\\input.md with key {fake_key} and image {image_data}"
        payload = {
            "session_id": "session-one",
            "cwd": str(project),
            "prompt": prompt,
            "hook_event_name": "UserPromptSubmit",
        }
        env = os.environ.copy()
        env["PROMPT_HARNESS_HOME"] = str(home)
        for _ in range(2):
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "capture-hook", "--platform", "claude"],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
        events = list(ph.iter_events(project / ".prompt-harness"))
        self.assertEqual(len(events), 2)
        self.assertEqual(len({event["event_id"] for event in events}), 2)
        for event in events:
            text = event["prompt"]["text"]
            self.assertIn("G:\\data\\input.md", text)
            self.assertNotIn(fake_key, text)
            self.assertIn("[REDACTED_SECRET]", text)
            self.assertNotIn(image_data, text)
            self.assertIn("[ATTACHMENT_DATA_OMITTED]", text)
        result = ph.doctor_store(project / ".prompt-harness", project)
        self.assertTrue(result["ok"], result)

    def test_backfill_merges_branches_and_excludes_import_mirrors(self) -> None:
        base = retained_workspace("backfill")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        claude_home = base / ".claude"
        codex_home = base / ".codex"
        encoded = __import__("re").sub(r"[^A-Za-z0-9]", "-", str(project))
        claude_project = claude_home / "projects" / encoded
        prompt_a = {
            "type": "user",
            "uuid": "native-a",
            "timestamp": "2026-01-01T00:00:00Z",
            "cwd": str(project),
            "message": {"content": [{"type": "text", "text": "first human prompt"}]},
        }
        prompt_repeat = {
            "type": "user",
            "uuid": "native-b",
            "timestamp": "2026-01-01T00:01:00Z",
            "cwd": str(project),
            "message": {"content": [{"type": "text", "text": "first human prompt"}]},
        }
        write_jsonl(claude_project / "branch-one.jsonl", [prompt_a, prompt_repeat])
        branch_copy = dict(prompt_a)
        branch_copy["uuid"] = "native-a-copy"
        write_jsonl(claude_project / "branch-two.jsonl", [branch_copy])

        imported = codex_home / "sessions" / "2026" / "rollout-imported.jsonl"
        write_jsonl(
            imported,
            [
                {
                    "type": "session_meta",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "payload": {
                        "id": "codex-imported",
                        "cwd": str(project),
                        "external_agent_source": "claude",
                        "external_agent_source_path": str(claude_project / "branch-one.jsonl"),
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T00:00:30Z",
                    "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "mirrored prompt"}]},
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T00:02:00Z",
                    "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "native codex continuation"}]},
                },
            ],
        )
        os.environ["PROMPT_HARNESS_HOME"] = str(base / "harness-home")
        args = type(
            "Args",
            (),
            {
                "project": project,
                "platform": "all",
                "claude_home": claude_home,
                "codex_home": codex_home,
                "rebuild_index": True,
            },
        )()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ph.backfill(args), 0)
        events = list(ph.iter_events(project / ".prompt-harness"))
        self.assertEqual(len(events), 3)
        self.assertEqual(sum(event["prompt"]["text"] == "first human prompt" for event in events), 2)
        self.assertNotIn("mirrored prompt", {event["prompt"]["text"] for event in events})
        self.assertIn("native codex continuation", {event["prompt"]["text"] for event in events})
        merged = next(event for event in events if event["source"].get("native_event_id") == "native-a")
        self.assertEqual(len(merged["source"]["refs"]), 2)
        self.assertTrue((project / ".prompt-harness" / "index" / "PROMPTS.md").exists())
        self.assertTrue(ph.doctor_store(project / ".prompt-harness", project)["ok"])

    def test_stable_turn_id_is_idempotent(self) -> None:
        base = retained_workspace("stable-turn")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        payload = {
            "session_id": "codex-session",
            "turn_id": "codex-turn-one",
            "cwd": str(project),
            "prompt": "same hook delivery",
            "hook_event_name": "UserPromptSubmit",
        }
        env = os.environ.copy()
        env["PROMPT_HARNESS_HOME"] = str(base / "harness-home")
        for _ in range(2):
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "capture-hook", "--platform", "codex"],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(list(ph.iter_events(project / ".prompt-harness"))), 1)


if __name__ == "__main__":
    unittest.main()
