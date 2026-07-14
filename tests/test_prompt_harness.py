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
        self.assertTrue((project / ".prompt-harness" / "reports").is_dir())
        self.assertIn("reports/", (project / ".prompt-harness" / ".gitignore").read_text(encoding="utf-8"))
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
        prompt_md = (project / ".prompt-harness" / "index" / "PROMPTS.md").read_text(encoding="utf-8")
        self.assertTrue(prompt_md.startswith("# User prompts\n\n## P00001"))
        self.assertNotIn("Canonical source", prompt_md)
        self.assertIn("- Platform: `claude`", prompt_md)
        self.assertTrue((project / ".prompt-harness" / "reports" / "SESSION_SUMMARIES.md").exists())
        timeline = (project / ".prompt-harness" / "visualizations" / "timeline.html").read_text(encoding="utf-8")
        self.assertIn("const DATA = {", timeline)
        self.assertNotIn("__PROMPT_HARNESS_DATA__", timeline)
        self.assertTrue(ph.doctor_store(project / ".prompt-harness", project)["ok"])

    def test_model_metadata_is_derived_from_source_transcripts(self) -> None:
        base = retained_workspace("model-view")
        claude_path = base / "claude.jsonl"
        rows = [
            (1, {"type": "user", "message": {"role": "user"}}),
            (2, {"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-test"}}),
        ]
        write_jsonl(claude_path, [row for _, row in rows])
        self.assertEqual(ph.source_models_by_line(claude_path, "claude")[1], "claude-opus-test")
        self.assertIsNone(ph.normalize_model("<synthetic>"))

        codex_path = base / "codex.jsonl"
        write_jsonl(
            codex_path,
            [
                {"type": "turn_context", "payload": {"model": "gpt-test"}},
                {"type": "response_item", "payload": {"type": "message", "role": "user"}},
            ],
        )
        self.assertEqual(ph.source_models_by_line(codex_path, "codex")[2], "gpt-test")

    def test_codex_internal_suggestion_prompt_is_excluded(self) -> None:
        internal_prompt = """# Overview
Generate 0 to 3 hyperpersonalized suggestions for the user.

Recent Codex tasks in this project:
- Refactor the prompt ledger.
"""
        self.assertTrue(ph.is_automatic_prompt(internal_prompt))
        self.assertFalse(ph.is_automatic_prompt("帮我整理这个项目里的提示词"))

    def test_codex_wrappers_are_normalized(self) -> None:
        wrapped = """# Files mentioned by the user:

## sample.png: C:/data/sample.png

## My request for Codex:
请检查更新问题。

<image name=[Image #1] path="C:/data/sample.png">
</image>
"""
        normalized, _ = ph.sanitize_prompt(wrapped, backfill=True)
        self.assertEqual(normalized, "请检查更新问题。\n\nReferenced paths:\n- C:/data/sample.png")
        self.assertTrue(ph.is_automatic_prompt("<turn_aborted>stopped</turn_aborted>"))
        self.assertTrue(ph.is_automatic_prompt("[Request interrupted by user for tool use]"))

    def test_clean_store_keeps_one_user_goal_objective(self) -> None:
        base = retained_workspace("clean")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        store, _ = ph.init_store(project)
        goal = """<codex_internal_context source="goal">
<objective>按照顶尖设计来优化</objective>
</codex_internal_context>"""
        for index, text in enumerate((goal, goal, "<turn_aborted>stopped</turn_aborted>", "真正的人类提示")):
            ph.append_event(
                store,
                ph.build_event(
                    root=project,
                    platform="codex",
                    source_mode="backfill",
                    prompt_text=text,
                    session_id="clean-session",
                    native_event_id=f"native-{index}",
                ),
            )
        result = ph.clean_store_events(store)
        ph.rebuild_index_for_store(store)
        self.assertEqual(result["events_dropped"], 2)
        texts = [event["prompt"]["text"] for event in ph.iter_events(store)]
        self.assertEqual(texts, ["按照顶尖设计来优化", "真正的人类提示"])
        metadata = json.loads((store / "sessions" / "codex" / "clean-session.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["event_count"], 2)
        self.assertTrue(ph.doctor_store(store, project)["ok"])

    def test_scrub_store_repairs_new_secret_patterns(self) -> None:
        base = retained_workspace("scrub")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        store, _ = ph.init_store(project)
        fake_pat = "github_pat_" + "A" * 32
        event = ph.build_event(
            root=project,
            platform="claude",
            source_mode="backfill",
            prompt_text=f"token: {fake_pat}",
            session_id="secret-session",
        )
        original_id = event["event_id"]
        self.assertTrue(ph.append_event(store, event))

        result = ph.scrub_store_secrets(store)
        ph.rebuild_index_for_store(store)

        self.assertEqual(result["events_changed"], 1)
        repaired = list(ph.iter_events(store))[0]
        self.assertEqual(repaired["event_id"], original_id)
        self.assertNotIn(fake_pat, repaired["prompt"]["text"])
        self.assertEqual(repaired["prompt"]["text"], "token: [REDACTED_SECRET]")
        self.assertEqual(repaired["prompt"]["secret_redactions"], 1)
        self.assertNotIn(fake_pat, (store / "index" / "PROMPTS.md").read_text(encoding="utf-8"))
        self.assertTrue(ph.doctor_store(store, project)["ok"])

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

    def test_stop_recovery_captures_old_thread_once(self) -> None:
        base = retained_workspace("stop-recovery")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        codex_home = base / ".codex"
        session_id = "old-session"
        turn_id = "old-turn"
        rollout = codex_home / "sessions" / "2026" / "07" / f"rollout-test-{session_id}.jsonl"
        write_jsonl(
            rollout,
            [
                {"type": "session_meta", "payload": {"id": session_id, "cwd": str(project)}},
                {"type": "turn_context", "payload": {"model": "gpt-recovery-test"}},
                {
                    "type": "response_item",
                    "timestamp": "2026-07-14T09:53:30.243Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "old thread human prompt"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
                    },
                },
            ],
        )
        payload = {"session_id": session_id, "cwd": str(project)}
        first = ph.recover_codex_stop(payload, project=project, codex_home=codex_home)
        second = ph.recover_codex_stop(payload, project=project, codex_home=codex_home)
        self.assertTrue(first["captured"])
        self.assertEqual(second["reason"], "already_recorded")
        events = list(ph.iter_events(project / ".prompt-harness"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"]["mode"], "stop_recovery")
        self.assertEqual(events[0]["session"]["turn_id"], turn_id)
        self.assertEqual(events[0]["context"]["model"], "gpt-recovery-test")
        self.assertEqual(events[0]["prompt"]["text"], "old thread human prompt")
        self.assertTrue(ph.doctor_store(project / ".prompt-harness", project)["ok"])

    def test_stop_recovery_reads_utf8_payload_under_gbk_stdio(self) -> None:
        base = retained_workspace("stop-recovery-utf8")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        codex_home = base / ".codex"
        session_id = "old-session-utf8"
        rollout = codex_home / "sessions" / "2026" / "07" / f"rollout-test-{session_id}.jsonl"
        write_jsonl(
            rollout,
            [
                {"type": "session_meta", "payload": {"id": session_id, "cwd": str(project)}},
                {
                    "type": "response_item",
                    "timestamp": "2026-07-14T10:57:05.450Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "utf8 stop recovery"}],
                    },
                },
            ],
        )
        payload = {"session_id": session_id, "cwd": str(project), "encoding_probe": "😀"}
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "cp936"
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "capture-stop-recovery",
                "--project",
                str(project),
                "--codex-home",
                str(codex_home),
            ],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        events = list(ph.iter_events(project / ".prompt-harness"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["prompt"]["text"], "utf8 stop recovery")


if __name__ == "__main__":
    unittest.main()
