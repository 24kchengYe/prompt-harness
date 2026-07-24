from __future__ import annotations

import importlib.util
import base64
import io
import json
import os
import subprocess
import sys
import unittest
import uuid
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "prompt_harness.py"
ARTIFACTS = REPO / "tests" / "_artifacts"

spec = importlib.util.spec_from_file_location("prompt_harness", SCRIPT)
assert spec and spec.loader
ph = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ph)

ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
os.environ["PROMPT_HARNESS_DISABLE_AUTO_SYNC"] = "1"


def retained_workspace(name: str) -> Path:
    path = ARTIFACTS / f"{name}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True)
    return path


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class PromptHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prior_harness_home = os.environ.get("PROMPT_HARNESS_HOME")
        home = retained_workspace("harness-home")
        os.environ["PROMPT_HARNESS_HOME"] = str(home)

    def tearDown(self) -> None:
        if self._prior_harness_home is None:
            os.environ.pop("PROMPT_HARNESS_HOME", None)
        else:
            os.environ["PROMPT_HARNESS_HOME"] = self._prior_harness_home

    def test_windows_drive_unc_and_extended_paths_normalize_portably(self) -> None:
        self.assertEqual(ph.normalize_path("C:\\Work\\Repo\\"), "c:/work/repo")
        self.assertEqual(ph.normalize_path(r"\\?\C:\Work\Repo"), "c:/work/repo")
        self.assertEqual(ph.normalize_path("\\\\Server\\Share\\Repo\\"), "//server/share/repo")
        self.assertTrue(ph.is_within(r"C:\Work\Repo\child", Path(r"C:\Work\Repo")))
        self.assertFalse(ph.is_within(r"C:\Work\Other", Path(r"C:\Work\Repo")))

    def test_backfill_matches_legacy_image_prompt_by_native_identity(self) -> None:
        base = retained_workspace("legacy-image-identity")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        claude_home = base / ".claude"
        codex_home = base / ".codex"
        encoded = __import__("re").sub(r"[^A-Za-z0-9]", "-", str(project))
        transcript = claude_home / "projects" / encoded / "legacy.jsonl"
        image_b64 = base64.b64encode(ONE_PIXEL_PNG).decode("ascii")
        write_jsonl(
            transcript,
            [
                {
                    "type": "user",
                    "uuid": "same-native-id",
                    "timestamp": "2026-07-14T00:00:00Z",
                    "cwd": str(project),
                    "message": {
                        "content": [
                            {"type": "text", "text": "legacy image prompt"},
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/png", "data": image_b64},
                            },
                        ]
                    },
                }
            ],
        )
        store, _ = ph.init_store(project)
        legacy = ph.build_event(
            root=project,
            platform="claude",
            source_mode="backfill",
            prompt_text="legacy image prompt\n[image attachment omitted]",
            session_id="legacy",
            occurred_at="2026-07-14T00:00:00.000Z",
            native_event_id="same-native-id",
            source_path=str(transcript),
            source_line=1,
        )
        self.assertTrue(ph.append_event(store, legacy))
        result = ph.backfill_project(
            project,
            platform="all",
            claude_home=claude_home,
            codex_home=codex_home,
            rebuild_index=True,
        )
        self.assertEqual(result["added"], 0)
        self.assertEqual(len(list(ph.iter_events(store))), 1)
        self.assertEqual(len(list(ph.iter_prompt_images(store))), 1)
        self.assertEqual(list(ph.iter_prompt_images(store))[0]["event_id"], legacy["event_id"])

    def test_append_only_supersession_hides_migrated_legacy_duplicate(self) -> None:
        base = retained_workspace("legacy-image-supersession")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        store, _ = ph.init_store(project)
        common = {
            "root": project,
            "platform": "claude",
            "source_mode": "backfill",
            "session_id": "legacy",
            "occurred_at": "2026-07-14T00:00:00.000Z",
            "native_event_id": "same-native-id",
            "source_path": str(base / "legacy.jsonl"),
            "source_line": 1,
        }
        old = ph.build_event(prompt_text="image prompt\n[image attachment omitted]", **common)
        clean = ph.build_event(prompt_text="image prompt", **common)
        self.assertTrue(ph.append_event(store, old))
        self.assertTrue(ph.append_event(store, clean))
        ph.persist_prompt_images(
            store,
            clean["event_id"],
            [{"kind": "base64", "value": base64.b64encode(ONE_PIXEL_PNG).decode("ascii"), "media_type": "image/png"}],
        )
        self.assertEqual(ph.repair_legacy_image_duplicates(store), 1)
        self.assertEqual([event["event_id"] for event in ph.iter_active_events(store)], [clean["event_id"]])
        catalog = ph.rebuild_index_for_store(store)
        self.assertEqual(catalog["raw_event_count"], 2)
        self.assertEqual(catalog["event_count"], 1)
        self.assertEqual(catalog["superseded_event_count"], 1)
        doctor = ph.doctor_store(store, project)
        self.assertTrue(doctor["ok"], doctor)
        self.assertEqual(doctor["active_event_count"], 1)
        self.assertEqual(doctor["superseded_event_count"], 1)

    def test_auto_sync_runs_one_full_scan_then_incremental_source_tails(self) -> None:
        base = retained_workspace("auto-sync")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        claude_home = base / ".claude"
        codex_home = base / ".codex"
        encoded = __import__("re").sub(r"[^A-Za-z0-9]", "-", str(project))
        image_b64 = base64.b64encode(ONE_PIXEL_PNG).decode("ascii")
        history = claude_home / "projects" / encoded / "history.jsonl"
        write_jsonl(
            history,
            [
                {
                    "type": "user",
                    "uuid": "auto-claude-one",
                    "timestamp": "2026-07-14T00:00:00Z",
                    "cwd": str(project),
                    "message": {
                        "content": [
                            {"type": "text", "text": "historical Claude prompt"},
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/png", "data": image_b64},
                            },
                        ]
                    },
                }
            ],
        )
        first = ph.auto_sync_project(
            project,
            source_platform="claude",
            session_id="opened-claude-session",
            trigger="test",
            source_path=history,
            claude_home=claude_home,
            codex_home=codex_home,
        )
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["reason"], "first_full_scan")
        self.assertEqual(first["mode"], "full")
        store = project / ".prompt-harness"
        self.assertTrue(store.is_dir())
        self.assertEqual(len(list(ph.iter_events(store))), 1)
        self.assertEqual(len(list(ph.iter_prompt_images(store))), 1)

        repeated = ph.auto_sync_project(
            project,
            source_platform="claude",
            session_id="opened-claude-session",
            trigger="test",
            source_path=history,
            claude_home=claude_home,
            codex_home=codex_home,
        )
        self.assertEqual(repeated["status"], "completed")
        self.assertEqual(repeated["reason"], "incremental")
        self.assertEqual(repeated["mode"], "incremental")
        self.assertEqual(repeated["sources_changed"], 0)
        self.assertFalse(repeated["index_rebuilt"])

        with history.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "auto-claude-two",
                        "timestamp": "2026-07-14T00:01:00Z",
                        "cwd": str(project),
                        "message": {"content": "new Claude prompt"},
                    }
                )
                + "\n"
            )
        with mock.patch.object(ph, "codex_project_paths", side_effect=AssertionError("no global scan")):
            next_session = ph.auto_sync_project(
                project,
                source_platform="claude",
                session_id="opened-claude-session",
                trigger="test",
                source_path=history,
                claude_home=claude_home,
                codex_home=codex_home,
            )
        self.assertEqual(next_session["status"], "completed")
        self.assertEqual(next_session["mode"], "incremental")
        self.assertEqual(next_session["added"], 1)
        self.assertTrue(next_session["index_rebuilt"])
        self.assertGreater(next_session["bytes_read"], 0)
        self.assertEqual(len(list(ph.iter_events(store))), 2)
        state = json.loads((store / "state" / "auto-sync.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "completed")
        self.assertIn("claude:opened-claude-session", state["sessions"])
        self.assertEqual(ph.doctor_store(store, project)["auto_sync"]["status"], "completed")

    def test_backfill_archives_claude_and_codex_model_outputs(self) -> None:
        base = retained_workspace("model-outputs")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        claude_home = base / ".claude"
        codex_home = base / ".codex"
        encoded = __import__("re").sub(r"[^A-Za-z0-9]", "-", str(project))
        claude_path = claude_home / "projects" / encoded / "claude-session.jsonl"
        write_jsonl(
            claude_path,
            [
                {
                    "type": "user",
                    "uuid": "claude-user",
                    "promptId": "claude-turn-one",
                    "timestamp": "2026-07-16T00:00:00Z",
                    "cwd": str(project),
                    "message": {"role": "user", "content": "Claude prompt"},
                },
                {
                    "type": "assistant",
                    "uuid": "claude-output",
                    "parentUuid": "claude-user",
                    "timestamp": "2026-07-16T00:00:01Z",
                    "cwd": str(project),
                    "message": {
                        "role": "assistant",
                        "model": "claude-test",
                        "stop_reason": "end_turn",
                        "content": [
                            {"type": "thinking", "thinking": "private reasoning"},
                            {"type": "text", "text": "Claude visible answer api_key=secret-value-123"},
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "private.txt"}},
                        ],
                    },
                },
                {
                    "type": "user",
                    "uuid": "claude-tool-result",
                    "parentUuid": "claude-output",
                    "timestamp": "2026-07-16T00:00:02Z",
                    "cwd": str(project),
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "claude-tool",
                                "content": "tool returned password=secret-value-456",
                                "is_error": False
                            }
                        ]
                    }
                },
            ],
        )
        claude_subagent_path = (
            claude_path.parent
            / "claude-session"
            / "subagents"
            / "agent-worker.jsonl"
        )
        write_jsonl(
            claude_subagent_path,
            [
                {
                    "type": "assistant",
                    "uuid": "claude-subagent-output",
                    "timestamp": "2026-07-16T00:00:03Z",
                    "cwd": str(project),
                    "isSidechain": True,
                    "agentId": "worker",
                    "message": {
                        "role": "assistant",
                        "model": "claude-test",
                        "content": [{"type": "text", "text": "subagent answer"}]
                    }
                }
            ],
        )
        codex_path = codex_home / "sessions" / "2026" / "rollout-codex-session.jsonl"
        write_jsonl(
            codex_path,
            [
                {
                    "type": "session_meta",
                    "payload": {"id": "codex-session", "cwd": str(project), "model": "codex-test"},
                },
                {
                    "type": "turn_context",
                    "timestamp": "2026-07-16T00:00:59Z",
                    "payload": {
                        "turn_id": "turn-one",
                        "cwd": str(project),
                        "model": "codex-test",
                        "summary": "auto"
                    }
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-16T00:00:59Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "<recommended_plugins>plugin metadata</recommended_plugins><environment_context><cwd>project</cwd></environment_context>"
                            }
                        ],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-one"}
                    }
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-16T00:00:59Z",
                    "payload": {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": "developer injected instruction"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-one"}
                    }
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-16T00:01:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Codex prompt"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-one"},
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-16T00:01:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Codex follow-up in same turn"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-one"},
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-16T00:01:00Z",
                    "payload": {
                        "type": "reasoning",
                        "id": "codex-reasoning",
                        "summary": [{"type": "summary_text", "text": "Codex reasoning"}],
                        "encrypted_content": "opaque-reasoning",
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-one"}
                    }
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-16T00:01:00Z",
                    "payload": {
                        "type": "function_call",
                        "id": "codex-call",
                        "name": "shell",
                        "arguments": "{\"cmd\":\"pwd\"}",
                        "call_id": "call-one",
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-one"}
                    }
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-16T00:01:00Z",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-one",
                        "output": "tool output",
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-one"}
                    }
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-16T00:01:01Z",
                    "payload": {
                        "type": "message",
                        "id": "codex-output",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "Codex visible answer"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-one"},
                    },
                },
            ],
        )

        first = ph.backfill_project(
            project,
            platform="all",
            claude_home=claude_home,
            codex_home=codex_home,
            rebuild_index=True,
        )
        second = ph.backfill_project(
            project,
            platform="all",
            claude_home=claude_home,
            codex_home=codex_home,
            rebuild_index=True,
        )
        store = project / ".prompt-harness"
        outputs = list(ph.iter_model_outputs(store))
        prompts = list(ph.iter_active_events(store))
        self.assertEqual(first["model_outputs_added"], 12)
        self.assertEqual(second["model_outputs_added"], 0)
        self.assertEqual(len(outputs), 12)
        self.assertEqual(len(prompts), 3)
        event_types = {output["event_type"] for output in outputs}
        self.assertTrue(
            {
                "assistant_text",
                "reasoning",
                "tool_call",
                "tool_result",
                "developer_instruction",
                "system_instruction",
            }.issubset(event_types)
        )
        self.assertTrue(any(output["session"]["is_subagent"] for output in outputs))
        rendered = (store / "index" / "MODELOUT.md").read_text(encoding="utf-8")
        self.assertIn("Lightweight project-wide index", rendered)
        trajectory = (store / "index" / "TRAJECTORY.md").read_text(encoding="utf-8")
        self.assertIn("# Project trajectories", trajectory)
        self.assertIn("- Total sessions: `3`", trajectory)
        self.assertIn("- Claude sessions: `2`", trajectory)
        self.assertIn("- Codex sessions: `1`", trajectory)
        self.assertIn("- Total turns: `3`", trajectory)
        self.assertIn(
            "| `S00001` | `claude` | `claude-session` | `closed` | 1 | 1 |",
            trajectory,
        )
        prompt_files = sorted((store / "index" / "prompt").glob("*.md"))
        modelout_files = sorted((store / "index" / "modelout").glob("*.md"))
        trajectory_files = sorted((store / "index" / "trajectory").glob("*.md"))
        self.assertEqual(len(prompt_files), 3)
        self.assertEqual(
            [path.name for path in prompt_files],
            [path.name for path in modelout_files],
        )
        self.assertEqual(
            [path.name for path in prompt_files],
            [path.name for path in trajectory_files],
        )
        self.assertTrue(all("claude" in path.name or "codex" in path.name for path in prompt_files))
        full_modelout = "\n".join(path.read_text(encoding="utf-8") for path in modelout_files)
        self.assertIn("Claude visible answer api_key=[REDACTED_SECRET]", full_modelout)
        self.assertIn("Codex visible answer", full_modelout)
        self.assertIn("private reasoning", full_modelout)
        self.assertIn("private.txt", full_modelout)
        self.assertIn("Codex reasoning", full_modelout)
        self.assertIn("developer injected instruction", full_modelout)
        self.assertIn("plugin metadata", full_modelout)
        self.assertIn("subagent answer", full_modelout)
        self.assertIn("password=[REDACTED_SECRET]", full_modelout)
        self.assertIn("Claude visible answer api_key=[REDACTED_SECRET]", rendered)
        self.assertIn("Codex visible answer", trajectory)
        self.assertNotIn("private reasoning", rendered)
        self.assertNotIn("private.txt", trajectory)
        self.assertNotIn("[truncated;", rendered)
        self.assertNotIn("[truncated;", trajectory)
        self.assertLess(trajectory.index("Claude prompt"), trajectory.index("Claude visible answer"))
        self.assertLess(trajectory.index("Codex prompt"), trajectory.index("Codex visible answer"))
        self.assertFalse((store / "index" / "MODELOUTEASY.md").exists())
        self.assertFalse((store / "index" / "TRAJECTORYEASY.md").exists())
        claude_trajectory = next(path for path in trajectory_files if "Claude-prompt" in path.name)
        self.assertIn(
            "- Native turn ID: `claude-turn-one`",
            claude_trajectory.read_text(encoding="utf-8"),
        )
        subagent_prompt = next(path for path in prompt_files if "subagent-worker" in path.name)
        self.assertIn(
            "No human prompt event was recorded",
            subagent_prompt.read_text(encoding="utf-8"),
        )
        codex_trajectory = next(path for path in trajectory_files if "Codex-prompt" in path.name)
        codex_text = codex_trajectory.read_text(encoding="utf-8")
        self.assertIn("## Turn 00001", codex_text)
        self.assertIn("- Human messages: `2`", codex_text)
        self.assertLess(codex_text.index("Codex prompt"), codex_text.index("Codex visible answer"))
        self.assertLess(
            codex_text.index("Codex follow-up in same turn"),
            codex_text.index("Codex visible answer"),
        )
        self.assertLess(codex_text.index("Codex prompt"), codex_text.index("developer injected instruction"))
        doctor = ph.doctor_store(store, project)
        self.assertTrue(doctor["ok"], doctor)
        self.assertEqual(doctor["model_output_count"], 12)

    def test_incremental_tail_adds_assistant_output_without_new_prompt(self) -> None:
        base = retained_workspace("model-output-tail")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        claude_home = base / ".claude"
        codex_home = base / ".codex"
        rollout = codex_home / "sessions" / "2026" / "rollout-output-tail.jsonl"
        write_jsonl(
            rollout,
            [
                {
                    "type": "session_meta",
                    "payload": {"id": "output-tail", "cwd": str(project), "model": "codex-test"},
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-16T00:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "wait for output"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "tail-turn"},
                    },
                },
            ],
        )
        first = ph.auto_sync_project(
            project,
            source_platform="codex",
            session_id="output-tail",
            trigger="test",
            source_path=rollout,
            claude_home=claude_home,
            codex_home=codex_home,
        )
        self.assertEqual(first["model_outputs_added"], 0)
        with rollout.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "timestamp": "2026-07-16T00:00:01Z",
                        "payload": {
                            "type": "message",
                            "id": "tail-output",
                            "role": "assistant",
                            "phase": "final_answer",
                            "content": [{"type": "output_text", "text": "tail answer"}],
                            "internal_chat_message_metadata_passthrough": {"turn_id": "tail-turn"},
                        },
                    }
                )
                + "\n"
            )
        second = ph.auto_sync_project(
            project,
            source_platform="codex",
            session_id="output-tail",
            trigger="test",
            source_path=rollout,
            claude_home=claude_home,
            codex_home=codex_home,
        )
        store = project / ".prompt-harness"
        self.assertEqual(second["mode"], "incremental")
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["model_outputs_added"], 1)
        self.assertTrue(second["index_rebuilt"])
        output = list(ph.iter_model_outputs(store))[0]
        prompt = list(ph.iter_active_events(store))[0]
        self.assertEqual(output["links"]["prompt_event_id"], prompt["event_id"])
        self.assertIn(
            "tail answer",
            (store / "index" / "MODELOUT.md").read_text(encoding="utf-8"),
        )

    def test_incremental_sync_discovers_new_codex_sibling_session(self) -> None:
        base = retained_workspace("codex-sibling-discovery")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        claude_home = base / ".claude"
        codex_home = base / ".codex"
        first_rollout = codex_home / "sessions" / "2026" / "07" / "rollout-first.jsonl"
        write_jsonl(
            first_rollout,
            [
                {"type": "session_meta", "payload": {"id": "first-session", "cwd": str(project)}},
                {
                    "type": "response_item",
                    "timestamp": "2026-07-16T00:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "first prompt"}],
                    },
                },
            ],
        )
        first = ph.auto_sync_project(
            project,
            source_platform="codex",
            session_id="first-session",
            trigger="test",
            source_path=first_rollout,
            claude_home=claude_home,
            codex_home=codex_home,
        )
        self.assertEqual(first["mode"], "full")

        sibling_rollout = codex_home / "sessions" / "2026" / "07" / "rollout-sibling.jsonl"
        write_jsonl(
            sibling_rollout,
            [
                {"type": "session_meta", "payload": {"id": "sibling-session", "cwd": str(project)}},
                {
                    "type": "response_item",
                    "timestamp": "2026-07-16T00:01:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "sibling prompt"}],
                    },
                },
            ],
        )
        second = ph.auto_sync_project(
            project,
            source_platform="codex",
            session_id="first-session",
            trigger="test",
            source_path=first_rollout,
            claude_home=claude_home,
            codex_home=codex_home,
        )
        self.assertEqual(second["mode"], "incremental")
        self.assertEqual(second["added"], 1)
        self.assertEqual(len(list(ph.iter_active_events(project / ".prompt-harness"))), 2)
        self.assertEqual(len(list((project / ".prompt-harness" / "index" / "trajectory").glob("*.md"))), 2)
        trajectory = (project / ".prompt-harness" / "index" / "TRAJECTORY.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("| Session | Platform | Session ID | Latest turn |", trajectory)
        self.assertIn("`open_or_interrupted`", trajectory)

    def test_codex_desktop_index_avoids_opening_unrelated_rollouts(self) -> None:
        base = retained_workspace("codex-state-index")
        project = base / "project"
        project.mkdir()
        codex_home = base / ".codex"
        target_id = "019f0000-0000-7000-8000-000000000001"
        unrelated_id = "019f0000-0000-7000-8000-000000000002"
        state = codex_home / "state_5.sqlite"
        state.parent.mkdir(parents=True)
        sqlite = __import__("sqlite3")
        with sqlite.connect(state) as connection:
            connection.execute("CREATE TABLE threads (id TEXT, cwd TEXT)")
            connection.executemany(
                "INSERT INTO threads (id, cwd) VALUES (?, ?)",
                (
                    (target_id, str(project)),
                    (unrelated_id, str(base / "other")),
                ),
            )
        target = codex_home / "sessions" / "2026" / "07" / f"rollout-{target_id}.jsonl"
        unrelated = codex_home / "sessions" / "2026" / "07" / f"rollout-{unrelated_id}.jsonl"
        write_jsonl(
            target,
            [{"type": "session_meta", "payload": {"id": target_id, "cwd": str(project)}}],
        )
        unrelated.parent.mkdir(parents=True, exist_ok=True)
        unrelated.write_text("not json and must not be opened\n", encoding="utf-8")

        paths = ph.codex_project_paths(codex_home, project)

        self.assertEqual(paths, [target])

    def test_bulk_model_output_append_deduplicates_without_rescanning_per_event(self) -> None:
        base = retained_workspace("bulk-model-output")
        project = base / "project"
        project.mkdir()
        store, _ = ph.init_store(project)
        events = [
            ph.build_model_output_event(
                root=project,
                platform="codex",
                session_id="bulk-session",
                occurred_at=f"2026-07-16T00:00:0{index}Z",
                event_type="assistant_text",
                actor_role="assistant",
                output_text=f"output {index}",
                structured=None,
                source_path=str(base / "rollout.jsonl"),
                source_line=index + 1,
                block_index=0,
            )
            for index in range(3)
        ]
        first = ph.append_model_outputs_bulk(store, events)
        second = ph.append_model_outputs_bulk(store, events)
        self.assertEqual(first, (3, 0))
        self.assertEqual(second, (0, 3))
        self.assertEqual(len(list(ph.iter_model_outputs(store))), 3)

    def test_full_backfill_requires_exact_session_root_unless_bound(self) -> None:
        base = retained_workspace("exact-root-backfill")
        project = base / "project"
        child = project / "child"
        child.mkdir(parents=True)
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        claude_home = base / ".claude"
        codex_home = base / ".codex"
        encoded = __import__("re").sub(r"[^A-Za-z0-9]", "-", str(project))
        claude_folder = claude_home / "projects" / encoded
        claude_exact = claude_folder / "claude-exact.jsonl"
        claude_child = claude_folder / "claude-child.jsonl"
        write_jsonl(
            claude_exact,
            [{"type": "user", "timestamp": "2026-07-15T00:00:00Z", "cwd": str(project), "message": {"content": "claude exact"}}],
        )
        write_jsonl(
            claude_child,
            [{"type": "user", "timestamp": "2026-07-15T00:01:00Z", "cwd": str(child), "message": {"content": "claude child"}}],
        )

        codex_exact = codex_home / "sessions" / "2026" / "rollout-codex-exact.jsonl"
        codex_child = codex_home / "sessions" / "2026" / "rollout-codex-child.jsonl"
        write_jsonl(
            codex_exact,
            [
                {"type": "session_meta", "payload": {"id": "codex-exact", "cwd": str(project)}},
                {"type": "response_item", "timestamp": "2026-07-15T00:02:00Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "codex exact"}]}},
            ],
        )
        write_jsonl(
            codex_child,
            [
                {"type": "session_meta", "payload": {"id": "codex-child", "cwd": str(child)}},
                {"type": "response_item", "timestamp": "2026-07-15T00:03:00Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "codex child"}]}},
            ],
        )

        first = ph.backfill_project(
            project,
            platform="all",
            claude_home=claude_home,
            codex_home=codex_home,
        )
        store = project / ".prompt-harness"
        self.assertEqual(first["sources_scanned"], 2)
        self.assertEqual({event["prompt"]["text"] for event in ph.iter_active_events(store)}, {"claude exact", "codex exact"})

        ph.append_session_binding(
            platform="claude",
            session_id="claude-child",
            project_root=project,
            source_path=claude_child,
        )
        ph.append_session_binding(
            platform="codex",
            session_id="codex-child",
            project_root=project,
            source_path=codex_child,
        )
        second = ph.backfill_project(
            project,
            platform="all",
            claude_home=claude_home,
            codex_home=codex_home,
        )
        self.assertEqual(second["sources_scanned"], 4)
        self.assertEqual(second["added"], 2)
        self.assertEqual(
            {event["prompt"]["text"] for event in ph.iter_active_events(store)},
            {"claude exact", "claude child", "codex exact", "codex child"},
        )

    def test_live_hook_skips_descendant_session_until_explicitly_bound(self) -> None:
        base = retained_workspace("exact-root-live-hook")
        project = base / "project"
        child = project / "child"
        child.mkdir(parents=True)
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        args = type("Args", (), {"platform": "codex", "project": None})()
        session_id = "child-live-session"
        ph.capture_hook_payload(
            args,
            {"session_id": session_id, "cwd": str(child), "prompt": "must stay outside parent"},
        )
        self.assertFalse((project / ".prompt-harness").exists())

        ph.append_session_binding(platform="codex", session_id=session_id, project_root=project)
        ph.capture_hook_payload(
            args,
            {"session_id": session_id, "cwd": str(child), "prompt": "explicitly routed to parent"},
        )
        events = list(ph.iter_active_events(project / ".prompt-harness"))
        self.assertEqual([event["prompt"]["text"] for event in events], ["explicitly routed to parent"])

    def test_full_cursor_snapshot_prunes_stale_descendant_sources(self) -> None:
        base = retained_workspace("exact-root-cursors")
        project = base / "project"
        project.mkdir()
        store, _ = ph.init_store(project)
        stale = {"path": str(project / "child" / "stale.jsonl"), "platform": "codex", "session_id": "stale"}
        current = {"path": str(project / "current.jsonl"), "platform": "codex", "session_id": "current"}
        ph.write_cursor_snapshots(store, {"stale": stale}, full_scan=False)
        state = ph.write_cursor_snapshots(store, {"current": current}, full_scan=True)
        self.assertEqual(state["sources"], {"current": current})

        claude = {"path": str(project / "claude.jsonl"), "platform": "claude", "session_id": "claude"}
        stale_codex = {"path": str(project / "stale-codex.jsonl"), "platform": "codex", "session_id": "old"}
        ph.write_cursor_snapshots(store, {"claude": claude, "stale-codex": stale_codex}, full_scan=False)
        codex_only = ph.write_cursor_snapshots(
            store,
            {"current": current},
            full_scan=True,
            scanned_platforms={"codex"},
        )
        self.assertEqual(codex_only["sources"], {"claude": claude, "current": current})

    def test_incremental_sync_prunes_missing_selected_source_cursor(self) -> None:
        base = retained_workspace("missing-source-cursor")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        codex_home = base / ".codex"
        store, _ = ph.init_store(project)
        missing = codex_home / "archived_sessions" / "rollout-missing.jsonl"
        ph.write_cursor_snapshots(
            store,
            {
                ph.normalize_path(missing): {
                    "path": str(missing),
                    "platform": "codex",
                    "session_id": "missing-session",
                    "size": 100,
                    "mtime_ns": 1,
                    "byte_offset": 100,
                    "line_count": 1,
                    "trailing_newline": True,
                }
            },
            full_scan=False,
        )

        result = ph.incremental_backfill_project(
            project,
            platform="codex",
            source_platform="codex",
            session_id="current-session",
            codex_home=codex_home,
        )

        self.assertEqual(result["mode"], "incremental")
        self.assertEqual(result["sources_known"], 0)
        self.assertEqual(ph.read_source_cursors(store)["sources"], {})

    def test_backfill_keeps_distinct_messages_in_the_same_codex_turn(self) -> None:
        base = retained_workspace("same-turn")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        codex_home = base / ".codex"
        rollout = codex_home / "sessions" / "2026" / "rollout-same-turn.jsonl"
        write_jsonl(
            rollout,
            [
                {"type": "session_meta", "payload": {"id": "same-turn-session", "cwd": str(project)}},
                {
                    "type": "response_item",
                    "timestamp": "2026-07-14T00:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "first human message"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "shared-turn"},
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-14T00:00:01Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "second human message"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "shared-turn"},
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-14T00:00:02Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "first human message"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "shared-turn"},
                    },
                },
            ],
        )
        first = ph.backfill_project(
            project,
            platform="codex",
            claude_home=base / ".claude",
            codex_home=codex_home,
            rebuild_index=True,
        )
        second = ph.backfill_project(
            project,
            platform="codex",
            claude_home=base / ".claude",
            codex_home=codex_home,
            rebuild_index=True,
        )
        self.assertEqual(first["added"], 3)
        self.assertEqual(second["added"], 0)
        self.assertEqual(
            [event["prompt"]["text"] for event in ph.iter_active_events(project / ".prompt-harness")],
            ["first human message", "second human message", "first human message"],
        )

    def test_automatic_project_context_is_append_only_excluded(self) -> None:
        base = retained_workspace("automatic-context")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        store, _ = ph.init_store(project)
        automatic = "# AGENTS.md instructions for X\n<INSTRUCTIONS>rules</INSTRUCTIONS>\n<environment_context>ctx</environment_context>"
        event = ph.build_event(
            root=project,
            platform="codex",
            source_mode="backfill",
            prompt_text=automatic,
            session_id="context-session",
        )
        self.assertTrue(ph.append_event(store, event))
        self.assertEqual(ph.repair_automatic_context_events(store), 1)
        self.assertEqual(len(list(ph.iter_events(store))), 1)
        self.assertEqual(len(list(ph.iter_active_events(store))), 0)
        catalog = ph.rebuild_index_for_store(store)
        self.assertEqual(catalog["excluded_event_count"], 1)
        doctor = ph.doctor_store(store, project)
        self.assertTrue(doctor["ok"], doctor)
        self.assertEqual(doctor["excluded_event_count"], 1)

    def test_hook_scheduler_launches_detached_auto_sync_command(self) -> None:
        base = retained_workspace("auto-sync-schedule")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        store, _ = ph.init_store(project)
        fake_process = type("Process", (), {"pid": 4321})()
        with mock.patch.dict(os.environ, {"PROMPT_HARNESS_DISABLE_AUTO_SYNC": ""}):
            with mock.patch.object(ph.subprocess, "Popen", return_value=fake_process) as popen:
                result = ph.schedule_auto_sync(
                    project,
                    store,
                    source_platform="codex",
                    session_id="existing-task",
                    trigger="user_prompt_submit",
                )
        self.assertTrue(result["scheduled"])
        command = popen.call_args.args[0]
        self.assertIn("auto-sync", command)
        self.assertIn("existing-task", command)
        self.assertIn(str(project), command)

    def test_first_hook_materializes_indexes_before_background_sync(self) -> None:
        base = retained_workspace("initial-hook-index")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        args = type("Args", (), {"platform": "codex", "project": None})()
        payload = {
            "session_id": "initial-index-session",
            "turn_id": "turn-one",
            "cwd": str(project),
            "prompt": "首次对话应立即出现索引",
            "timestamp": "2026-07-16T12:30:00.000Z",
        }

        with mock.patch.dict(os.environ, {"PROMPT_HARNESS_DISABLE_AUTO_SYNC": "1"}):
            self.assertEqual(ph.capture_hook_payload(args, payload), 0)

        store = project / ".prompt-harness"
        self.assertTrue((store / "index" / "PROMPTS.md").is_file())
        self.assertTrue((store / "index" / "MODELOUT.md").is_file())
        trajectory = (store / "index" / "TRAJECTORY.md").read_text(encoding="utf-8")
        self.assertIn("首次对话应立即出现索引", trajectory)

    def test_hook_scheduler_uses_windows_detached_process_flags(self) -> None:
        base = retained_workspace("auto-sync-windows-schedule")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        store, _ = ph.init_store(project)
        fake_process = type("Process", (), {"pid": 4321})()
        fake_os = mock.Mock(wraps=os)
        fake_os.name = "nt"
        with mock.patch.dict(os.environ, {"PROMPT_HARNESS_DISABLE_AUTO_SYNC": ""}):
            with mock.patch.object(ph, "os", fake_os):
                with mock.patch.object(ph, "file_lock", side_effect=lambda *args, **kwargs: nullcontext()):
                    with mock.patch.object(ph.subprocess, "Popen", return_value=fake_process) as popen:
                        result = ph.schedule_auto_sync(
                            project,
                            store,
                            source_platform="codex",
                            session_id="windows-task",
                            trigger="user_prompt_submit",
                        )
        self.assertTrue(result["scheduled"])
        kwargs = popen.call_args.kwargs
        expected = getattr(ph.subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(
            ph.subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            0x00000200,
        )
        self.assertEqual(kwargs["creationflags"], expected)
        self.assertNotIn("start_new_session", kwargs)
        self.assertIs(kwargs["stdin"], ph.subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], ph.subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], ph.subprocess.DEVNULL)

    def test_pending_sync_requests_are_coalesced_without_deletion(self) -> None:
        base = retained_workspace("pending-sync")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        store, _ = ph.init_store(project)
        first = ph.sync_request(
            source_platform="codex",
            session_id="same-session",
            trigger="test",
            source_path=base / "rollout-a.jsonl",
        )
        second = ph.sync_request(
            source_platform="claude",
            session_id="other-session",
            trigger="test",
            source_path=base / "claude-b.jsonl",
        )
        self.assertEqual(ph.mark_pending_sync(store, first), 1)
        self.assertEqual(ph.mark_pending_sync(store, first), 1)
        self.assertEqual(ph.mark_pending_sync(store, second), 2)
        self.assertEqual(len(ph.pop_pending_sync(store)), 2)
        self.assertEqual(ph.pop_pending_sync(store), [])
        state = json.loads(ph.auto_sync_pending_file(store).read_text(encoding="utf-8"))
        self.assertFalse(state["pending"])
        self.assertEqual(state["request_count"], 3)

    def test_user_home_is_allowed_but_filesystem_root_is_rejected(self) -> None:
        self.assertFalse(ph.is_unsafe_broad_project_root(Path.home()))
        filesystem_root = Path(Path.home().anchor)
        self.assertTrue(ph.is_unsafe_broad_project_root(filesystem_root))
        with self.assertRaisesRegex(ValueError, "broad project root"):
            ph.init_store(filesystem_root)

    def test_user_home_ledger_captures_exact_home_without_absorbing_child(self) -> None:
        base = retained_workspace("user-home-exact-root")
        fake_home = base / "ASUS"
        child = fake_home / "child-project"
        child.mkdir(parents=True)
        (child / "AGENTS.md").write_text("child project", encoding="utf-8")
        args = type("Args", (), {"platform": "codex", "project": None})()

        with mock.patch.object(ph.Path, "home", return_value=fake_home):
            ph.init_store(fake_home)
            ph.capture_hook_payload(
                args,
                {"session_id": "home-session", "cwd": str(fake_home), "prompt": "exact home prompt"},
            )
            ph.capture_hook_payload(
                args,
                {"session_id": "child-session", "cwd": str(child), "prompt": "child prompt"},
            )

        home_events = list(ph.iter_active_events(fake_home / ".prompt-harness"))
        child_events = list(ph.iter_active_events(child / ".prompt-harness"))
        self.assertEqual([event["prompt"]["text"] for event in home_events], ["exact home prompt"])
        self.assertEqual([event["prompt"]["text"] for event in child_events], ["child prompt"])

    def test_hook_archives_user_image_and_keeps_only_file_path(self) -> None:
        base = retained_workspace("hook-image")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        image_path = base / "sent.png"
        image_path.write_bytes(ONE_PIXEL_PNG)
        document_path = base / "brief.pdf"
        payload = {
            "session_id": "image-session",
            "turn_id": "image-turn",
            "cwd": str(project),
            "prompt": "请分析我发送的材料。",
            "local_images": [str(image_path)],
            "attachments": [{"type": "file", "path": str(document_path)}],
        }
        env = os.environ.copy()
        env["PROMPT_HARNESS_HOME"] = str(base / "harness-home")
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "capture-hook", "--platform", "codex"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        store = project / ".prompt-harness"
        event = list(ph.iter_events(store))[0]
        self.assertIn(f"[attached file: {document_path}]", event["prompt"]["text"])
        self.assertFalse(document_path.exists())
        images = list(ph.iter_prompt_images(store))
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["event_id"], event["event_id"])
        asset_path = store / images[0]["asset"]["path"]
        self.assertEqual(asset_path.read_bytes(), ONE_PIXEL_PNG)
        self.assertFalse((store / "state" / "image-misses.jsonl").exists())
        ph.rebuild_index_for_store(store)
        prompts = (store / "index" / "PROMPTS.md").read_text(encoding="utf-8")
        self.assertIn(f"![P00001 image 1](../{images[0]['asset']['path']})", prompts)
        self.assertIn("assets/", (store / ".gitignore").read_text(encoding="utf-8"))
        self.assertIn("badcases/", (store / ".gitignore").read_text(encoding="utf-8"))
        self.assertTrue(ph.doctor_store(store, project)["ok"])

    def test_backfill_images_is_idempotent_for_claude_and_codex(self) -> None:
        base = retained_workspace("backfill-images")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        claude_home = base / ".claude"
        codex_home = base / ".codex"
        encoded = __import__("re").sub(r"[^A-Za-z0-9]", "-", str(project))
        claude_path = claude_home / "projects" / encoded / "claude-image.jsonl"
        image_b64 = base64.b64encode(ONE_PIXEL_PNG).decode("ascii")
        write_jsonl(
            claude_path,
            [
                {
                    "type": "user",
                    "uuid": "claude-image-native",
                    "timestamp": "2026-07-14T01:00:00Z",
                    "cwd": str(project),
                    "message": {
                        "content": [
                            {"type": "text", "text": "Claude image prompt"},
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/png", "data": image_b64},
                            },
                        ]
                    },
                }
            ],
        )
        codex_path = codex_home / "sessions" / "2026" / "rollout-codex-image.jsonl"
        write_jsonl(
            codex_path,
            [
                {"type": "session_meta", "payload": {"id": "codex-image-session", "cwd": str(project)}},
                {
                    "type": "response_item",
                    "timestamp": "2026-07-14T01:01:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Codex image prompt"},
                            {"type": "input_image", "image_url": f"data:image/png;base64,{image_b64}"},
                        ],
                    },
                },
            ],
        )
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
        store = project / ".prompt-harness"
        first_events = list(ph.iter_events(store))
        first_images = list(ph.iter_prompt_images(store))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ph.backfill(args), 0)
        self.assertEqual(len(list(ph.iter_events(store))), len(first_events))
        self.assertEqual(len(list(ph.iter_prompt_images(store))), len(first_images))
        self.assertEqual(len(first_events), 2)
        self.assertEqual(len(first_images), 2)
        self.assertEqual(len(list((store / "assets" / "images").glob("*"))), 1)
        prompts = (store / "index" / "PROMPTS.md").read_text(encoding="utf-8")
        self.assertEqual(prompts.count("../assets/images/"), 2)
        self.assertTrue(ph.doctor_store(store, project)["ok"])

    def test_image_only_prompt_keeps_valid_local_raster_and_omits_unsafe_sources(self) -> None:
        base = retained_workspace("image-only")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        png = base / "ok.png"
        png.write_bytes(ONE_PIXEL_PNG)
        svg = base / "unsafe.svg"
        svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
        payload = {
            "session_id": "image-only-session",
            "turn_id": "image-only-turn",
            "cwd": str(project),
            "images": [str(png), str(svg), "https://example.invalid/remote.png"],
        }
        args = type("Args", (), {"platform": "codex", "project": project})()
        stdin = io.StringIO(json.dumps(payload))
        original_stdin = sys.stdin
        try:
            sys.stdin = stdin
            self.assertEqual(ph.capture_hook(args), 0)
        finally:
            sys.stdin = original_stdin
        store = project / ".prompt-harness"
        events = list(ph.iter_events(store))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["prompt"]["text"], "")
        self.assertEqual(len(list(ph.iter_prompt_images(store))), 1)
        result = ph.doctor_store(store, project)
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("image attachments were omitted" in warning for warning in result["warnings"]))

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
                        "timestamp": "2026-01-01T00:00:00Z",
                        "cwd": str(project),
                        "external_agent_source": "claude",
                        "external_agent_source_path": str(claude_project / "branch-one.jsonl"),
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "打开 Claude 导入会话归档：Imported task",
                            }
                        ],
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
        self.assertEqual(len(events), 4)
        self.assertEqual(sum(event["prompt"]["text"] == "first human prompt" for event in events), 2)
        self.assertNotIn("mirrored prompt", {event["prompt"]["text"] for event in events})
        self.assertIn(
            "打开 Claude 导入会话归档：Imported task",
            {event["prompt"]["text"] for event in events},
        )
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

    def test_rebuild_reuses_model_cache_for_unchanged_sources(self) -> None:
        base = retained_workspace("model-cache")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        source = base / "rollout-model-cache.jsonl"
        write_jsonl(
            source,
            [
                {"type": "turn_context", "payload": {"model": "gpt-cache-test"}},
                {"type": "response_item", "payload": {"type": "message", "role": "user"}},
            ],
        )
        store, _ = ph.init_store(project)
        event = ph.build_event(
            root=project,
            platform="codex",
            source_mode="backfill",
            prompt_text="derive cached model",
            session_id="model-cache-session",
            source_path=str(source),
            source_line=2,
        )
        self.assertTrue(ph.append_event(store, event))
        ph.rebuild_index_for_store(store)
        with mock.patch.object(ph, "source_models_by_line", side_effect=AssertionError("cache miss")):
            ph.rebuild_index_for_store(store)
        self.assertTrue(ph.source_model_cache_file(store).is_file())
        self.assertIn("gpt-cache-test", (store / "index" / "PROMPTS.md").read_text(encoding="utf-8"))

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

    def test_scrub_store_repairs_structured_agent_trace_secrets(self) -> None:
        base = retained_workspace("scrub-trace-secrets")
        project = base / "project"
        project.mkdir()
        store, _ = ph.init_store(project)
        event = ph.build_model_output_event(
            root=project,
            platform="codex",
            session_id="trace-secret-session",
            occurred_at="2026-07-16T00:00:00Z",
            event_type="tool_result",
            actor_role="tool",
            output_text="safe summary",
            structured={"output": "api_key = previously-unrecognized-secret-value"},
            source_path=str(base / "rollout.jsonl"),
            source_line=1,
            block_index=0,
        )
        event["content"]["structured"]["output"] = "api_key = newly-recognized-secret-value"
        event["content"]["sha256"] = ph.trace_content_hash(
            event["content"]["text"],
            event["content"]["structured"],
        )
        self.assertTrue(ph.append_model_output(store, event))
        result = ph.scrub_store_secrets(store)
        self.assertEqual(result["events_changed"], 1)
        repaired = list(ph.iter_model_outputs(store))[0]
        self.assertIn(
            "[REDACTED_SECRET]",
            repaired["content"]["structured"]["output"],
        )
        self.assertTrue(ph.doctor_store(store, project)["ok"])

    def test_doctor_does_not_match_trace_secrets_across_json_escaped_newlines(self) -> None:
        base = retained_workspace("doctor-trace-newlines")
        project = base / "project"
        project.mkdir()
        store, _ = ph.init_store(project)
        event = ph.build_model_output_event(
            root=project,
            platform="codex",
            session_id="trace-newline-session",
            occurred_at="2026-07-16T00:00:00Z",
            event_type="tool_result",
            actor_role="tool",
            output_text="api_key =\nfrom client.model_config import get_api_key_for_model",
            structured={
                "output": "api_key =\nfrom client.model_config import get_api_key_for_model"
            },
            source_path=str(base / "rollout.jsonl"),
            source_line=1,
            block_index=0,
        )
        self.assertTrue(ph.append_model_output(store, event))
        ph.rebuild_index_for_store(store)
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

    def test_prompt_numbers_reorder_when_earlier_history_is_backfilled(self) -> None:
        base = retained_workspace("chronological-numbers")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        store, _ = ph.init_store(project)
        later = ph.build_event(
            root=project,
            platform="codex",
            source_mode="hook",
            prompt_text="later prompt",
            session_id="ordering-session",
            occurred_at="2026-07-14T10:00:00.000Z",
            turn_id="later-turn",
        )
        newest = ph.build_event(
            root=project,
            platform="claude",
            source_mode="backfill",
            prompt_text="newest prompt",
            session_id="ordering-session-two",
            occurred_at="2026-07-14T11:00:00.000Z",
            native_event_id="newest-message",
        )
        self.assertTrue(ph.append_event(store, later))
        self.assertTrue(ph.append_event(store, newest))
        ph.rebuild_index_for_store(store)
        first_markdown = (store / "index" / "PROMPTS.md").read_text(encoding="utf-8")
        self.assertLess(first_markdown.index("## P00001"), first_markdown.index("## P00002"))
        self.assertIn(f"- Event ID: `{later['event_id']}`", first_markdown)

        earlier = ph.build_event(
            root=project,
            platform="claude",
            source_mode="backfill",
            prompt_text="earlier prompt",
            session_id="ordering-session-three",
            occurred_at="2026-07-14T09:00:00.000Z",
            native_event_id="earlier-message",
        )
        self.assertTrue(ph.append_event(store, earlier))
        ph.rebuild_index_for_store(store)
        rebuilt = (store / "index" / "PROMPTS.md").read_text(encoding="utf-8")
        self.assertLess(rebuilt.index("earlier prompt"), rebuilt.index("later prompt"))
        self.assertLess(rebuilt.index("later prompt"), rebuilt.index("newest prompt"))
        self.assertIn(f"## P00001\n\n- Time: `{earlier['occurred_at']}`", rebuilt)
        self.assertIn(f"## P00002\n\n- Time: `{later['occurred_at']}`", rebuilt)
        self.assertIn(f"- Event ID: `{later['event_id']}`", rebuilt)

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "search",
                "later prompt",
                "--project",
                str(project),
                "--format",
                "json",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        search_result = json.loads(completed.stdout)
        self.assertEqual(search_result[0]["prompt_number"], 2)
        self.assertEqual(search_result[0]["event_id"], later["event_id"])

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
        with mock.patch.object(
            ph,
            "source_models_by_line",
            side_effect=AssertionError("Stop recovery must tail from the end"),
        ):
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

    def test_stop_recovery_skips_descendant_session_until_explicitly_bound(self) -> None:
        base = retained_workspace("exact-root-stop")
        project = base / "project"
        child = project / "child"
        child.mkdir(parents=True)
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        codex_home = base / ".codex"
        session_id = "child-stop-session"
        rollout = codex_home / "sessions" / "2026" / "07" / f"rollout-test-{session_id}.jsonl"
        write_jsonl(
            rollout,
            [
                {"type": "session_meta", "payload": {"id": session_id, "cwd": str(child)}},
                {
                    "type": "response_item",
                    "timestamp": "2026-07-15T01:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "child stop prompt"}],
                    },
                },
            ],
        )
        payload = {"session_id": session_id, "cwd": str(child)}
        skipped = ph.recover_codex_stop(payload, codex_home=codex_home)
        self.assertEqual(skipped["reason"], "cwd_not_exact_project_root")
        self.assertFalse((project / ".prompt-harness").exists())

        ph.append_session_binding(
            platform="codex",
            session_id=session_id,
            project_root=project,
            source_path=rollout,
        )
        captured = ph.recover_codex_stop(payload, codex_home=codex_home)
        self.assertTrue(captured["captured"])
        self.assertEqual(Path(captured["project"]), project.resolve())

    def test_reconcile_hides_legacy_descendant_events_but_binding_reenables_them(self) -> None:
        base = retained_workspace("exact-root-repair")
        project = base / "project"
        child = project / "child"
        other = base / "other"
        child.mkdir(parents=True)
        other.mkdir()
        store, _ = ph.init_store(project)
        exact = ph.build_event(
            root=project,
            platform="codex",
            source_mode="hook",
            prompt_text="exact legacy event",
            session_id="exact-session",
            cwd=str(project),
        )
        descendant = ph.build_event(
            root=project,
            platform="codex",
            source_mode="hook",
            prompt_text="descendant legacy event",
            session_id="descendant-session",
            cwd=str(child),
        )
        self.assertTrue(ph.append_event(store, exact))
        self.assertTrue(ph.append_event(store, descendant))
        result = ph.reconcile_candidates(project, store, [], rebuild_index=True, full_dataset=True)
        self.assertEqual(result["excluded_out_of_scope_events"], 1)
        self.assertEqual(len(list(ph.iter_events(store))), 2)
        self.assertEqual([event["event_id"] for event in ph.iter_active_events(store)], [exact["event_id"]])

        ph.append_session_binding(
            platform="codex",
            session_id="descendant-session",
            project_root=project,
        )
        self.assertEqual(len(list(ph.iter_active_events(store))), 2)
        self.assertEqual(ph.doctor_store(store, project)["active_event_count"], 2)
        ph.append_session_binding(
            platform="codex",
            session_id="descendant-session",
            project_root=other,
        )
        self.assertEqual([event["event_id"] for event in ph.iter_active_events(store)], [exact["event_id"]])
        self.assertEqual(ph.doctor_store(store, project)["active_event_count"], 1)

    def test_stop_recovery_missing_session_does_not_select_latest_unrelated_rollout(self) -> None:
        base = retained_workspace("stop-recovery-missing-session")
        project = base / "project"
        project.mkdir()
        codex_home = base / ".codex"
        rollout = codex_home / "sessions" / "2026" / "07" / "rollout-unrelated.jsonl"
        write_jsonl(
            rollout,
            [
                {"type": "session_meta", "payload": {"id": "unrelated", "cwd": str(project)}},
                {
                    "type": "response_item",
                    "timestamp": "2026-07-15T04:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "must not be captured"}],
                    },
                },
            ],
        )
        result = ph.recover_codex_stop({"cwd": str(project)}, codex_home=codex_home)
        self.assertEqual(result["reason"], "missing_session_id")
        self.assertEqual(len(list(ph.iter_events(project / ".prompt-harness"))), 0)

    def test_stop_recovery_attaches_image_to_existing_hook_event(self) -> None:
        base = retained_workspace("stop-recovery-image")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        codex_home = base / ".codex"
        session_id = "existing-image-session"
        turn_id = "existing-image-turn"
        image_b64 = base64.b64encode(ONE_PIXEL_PNG).decode("ascii")
        rollout = codex_home / "sessions" / "2026" / "07" / f"rollout-test-{session_id}.jsonl"
        write_jsonl(
            rollout,
            [
                {"type": "session_meta", "payload": {"id": session_id, "cwd": str(project)}},
                {
                    "type": "response_item",
                    "timestamp": "2026-07-14T11:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "hook event missing its image"},
                            {"type": "input_image", "image_url": f"data:image/png;base64,{image_b64}"},
                        ],
                        "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
                    },
                },
            ],
        )
        store, _ = ph.init_store(project)
        existing = ph.build_event(
            root=project,
            platform="codex",
            source_mode="hook",
            prompt_text="hook event missing its image",
            session_id=session_id,
            occurred_at="2026-07-14T11:00:00.000Z",
            turn_id=turn_id,
        )
        self.assertTrue(ph.append_event(store, existing))
        result = ph.recover_codex_stop(
            {"session_id": session_id, "cwd": str(project)},
            project=project,
            codex_home=codex_home,
        )
        self.assertEqual(result["reason"], "already_recorded")
        self.assertEqual(result["event_id"], existing["event_id"])
        self.assertEqual(result["images"]["saved"], 1)
        self.assertEqual(len(list(ph.iter_events(store))), 1)
        self.assertEqual(len(list(ph.iter_prompt_images(store))), 1)
        self.assertTrue(ph.doctor_store(store, project)["ok"])

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

    def test_bound_codex_session_routes_stop_recovery_and_full_backfill_to_destination(self) -> None:
        base = retained_workspace("bound-session-route")
        original = base / "original"
        destination = base / "destination"
        original.mkdir()
        destination.mkdir()
        (original / "AGENTS.md").write_text("original", encoding="utf-8")
        (destination / "AGENTS.md").write_text("destination", encoding="utf-8")
        codex_home = base / ".codex"
        session_id = "bound-codex-session"
        rollout = codex_home / "sessions" / "2026" / "07" / f"rollout-test-{session_id}.jsonl"
        write_jsonl(
            rollout,
            [
                {"type": "session_meta", "payload": {"id": session_id, "cwd": str(original)}},
                {
                    "type": "response_item",
                    "timestamp": "2026-07-15T01:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "route me to destination"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "bound-turn"},
                    },
                },
            ],
        )
        binding, appended = ph.append_session_binding(
            platform="codex",
            session_id=session_id,
            project_root=destination,
            source_path=rollout,
        )
        self.assertTrue(appended)
        self.assertEqual(ph.bound_project_root("codex", session_id), destination.resolve())
        result = ph.recover_codex_stop(
            {"session_id": session_id, "cwd": str(original)},
            codex_home=codex_home,
        )
        self.assertTrue(result["captured"])
        self.assertEqual(Path(result["project"]), destination.resolve())
        self.assertFalse((original / ".prompt-harness").exists())
        self.assertEqual(len(list(ph.iter_events(destination / ".prompt-harness"))), 1)

        backfill = ph.backfill_project(destination, platform="codex", codex_home=codex_home)
        self.assertEqual(backfill["added"], 0)
        self.assertEqual(backfill["sources_scanned"], 1)
        self.assertEqual(binding["source_path"], str(rollout.resolve()))

    def test_rebinding_is_append_only_and_latest_project_wins(self) -> None:
        base = retained_workspace("session-rebinding")
        first = base / "first"
        second = base / "second"
        first.mkdir()
        second.mkdir()
        one, one_added = ph.append_session_binding(
            platform="codex",
            session_id="switch-session",
            project_root=first,
        )
        two, two_added = ph.append_session_binding(
            platform="codex",
            session_id="switch-session",
            project_root=second,
        )
        repeated, repeated_added = ph.append_session_binding(
            platform="codex",
            session_id="switch-session",
            project_root=second,
        )
        self.assertTrue(one_added)
        self.assertTrue(two_added)
        self.assertFalse(repeated_added)
        self.assertEqual(two["replaces_binding_id"], one["binding_id"])
        self.assertEqual(repeated["binding_id"], two["binding_id"])
        self.assertEqual(len(list(ph.iter_session_bindings())), 2)
        self.assertEqual(ph.bound_project_root("codex", "switch-session"), second.resolve())

    def test_session_migration_copies_images_and_excludes_source_without_deleting_raw_events(self) -> None:
        base = retained_workspace("session-migration")
        source_root = base / "source"
        destination = base / "destination"
        source_root.mkdir()
        destination.mkdir()
        (source_root / "AGENTS.md").write_text("source", encoding="utf-8")
        (destination / "AGENTS.md").write_text("destination", encoding="utf-8")
        codex_home = base / ".codex"
        session_id = "migrate-session"
        turn_id = "migrate-turn"
        rollout = codex_home / "sessions" / "2026" / "07" / f"rollout-test-{session_id}.jsonl"
        write_jsonl(
            rollout,
            [
                {"type": "session_meta", "payload": {"id": session_id, "cwd": str(source_root)}},
                {
                    "type": "response_item",
                    "timestamp": "2026-07-15T02:00:00Z",
                    "payload": {
                        "id": "native-migrate-message",
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "move prompt and archived image"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
                    },
                },
            ],
        )
        source_store, _ = ph.init_store(source_root)
        source_event = ph.build_event(
            root=source_root,
            platform="codex",
            source_mode="hook",
            prompt_text="move prompt and archived image",
            session_id=session_id,
            occurred_at="2026-07-15T02:00:00.000Z",
            turn_id=turn_id,
        )
        self.assertTrue(ph.append_event(source_store, source_event))
        image_path = base / "sent.png"
        image_path.write_bytes(ONE_PIXEL_PNG)
        self.assertEqual(
            ph.persist_prompt_images(
                source_store,
                source_event["event_id"],
                [{"kind": "local_path", "value": str(image_path)}],
            )["saved"],
            1,
        )
        ph.rebuild_index_for_store(source_store)
        ph.append_session_binding(
            platform="codex",
            session_id=session_id,
            project_root=destination,
            source_path=rollout,
        )
        result = ph.migrate_bound_session(
            platform="codex",
            session_id=session_id,
            project_root=destination,
            source_path=rollout,
            claude_home=base / ".claude",
            codex_home=codex_home,
        )
        destination_store = destination / ".prompt-harness"
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["source_exclusions_added"], 1)
        self.assertEqual(result["source_images_copied"], 1)
        self.assertEqual(len(list(ph.iter_events(source_store))), 1)
        self.assertEqual(len(list(ph.iter_active_events(source_store))), 0)
        self.assertEqual(len(list(ph.iter_active_events(destination_store))), 1)
        self.assertEqual(len(list(ph.iter_prompt_images(destination_store))), 1)
        self.assertTrue(ph.doctor_store(source_store, source_root)["ok"])
        self.assertTrue(ph.doctor_store(destination_store, destination)["ok"])

        repeated = ph.migrate_bound_session(
            platform="codex",
            session_id=session_id,
            project_root=destination,
            source_path=rollout,
            claude_home=base / ".claude",
            codex_home=codex_home,
        )
        self.assertEqual(repeated["added"], 0)
        self.assertEqual(repeated["source_exclusions_added"], 0)
        self.assertEqual(len(list(ph.iter_prompt_images(destination_store))), 1)

        ph.append_session_binding(
            platform="codex",
            session_id=session_id,
            project_root=source_root,
            source_path=rollout,
        )
        switched_back = ph.migrate_bound_session(
            platform="codex",
            session_id=session_id,
            project_root=source_root,
            source_path=rollout,
            claude_home=base / ".claude",
            codex_home=codex_home,
        )
        self.assertEqual(switched_back["added"], 0)
        self.assertEqual(switched_back["source_exclusions_added"], 1)
        self.assertEqual(len(list(ph.iter_active_events(source_store))), 1)
        self.assertEqual(len(list(ph.iter_active_events(destination_store))), 0)
        self.assertTrue(ph.doctor_store(source_store, source_root)["ok"])
        self.assertTrue(ph.doctor_store(destination_store, destination)["ok"])

    def test_stop_recovery_prefers_explicit_transcript_metadata_over_stale_payload(self) -> None:
        base = retained_workspace("stop-transcript-identity")
        project = base / "real-project"
        stale = base / "stale-project"
        project.mkdir()
        stale.mkdir()
        (project / "AGENTS.md").write_text("real", encoding="utf-8")
        codex_home = base / ".codex"
        session_id = "real-session"
        rollout = codex_home / "sessions" / "2026" / "07" / f"rollout-test-{session_id}.jsonl"
        write_jsonl(
            rollout,
            [
                {"type": "session_meta", "payload": {"id": session_id, "cwd": str(project)}},
                {
                    "type": "response_item",
                    "timestamp": "2026-07-15T03:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "trust transcript identity"}],
                    },
                },
            ],
        )
        result = ph.recover_codex_stop(
            {
                "session_id": "stale-session",
                "cwd": str(stale),
                "transcript_path": str(rollout),
            },
            codex_home=codex_home,
        )
        self.assertTrue(result["captured"])
        self.assertEqual(result["session_id"], session_id)
        self.assertEqual(Path(result["project"]), project.resolve())
        self.assertFalse((stale / ".prompt-harness").exists())

    def test_cli_json_output_is_utf8_when_windows_stdio_requests_gbk(self) -> None:
        base = retained_workspace("utf8-cli-output")
        project = base / "project"
        project.mkdir()
        store, _ = ph.init_store(project)
        event = ph.build_event(
            root=project,
            platform="codex",
            source_mode="backfill",
            prompt_text="编码检查 ⚠",
            session_id="utf8-cli-session",
        )
        self.assertTrue(ph.append_event(store, event))
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "cp936"
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "search",
                "编码检查",
                "--project",
                str(project),
                "--format",
                "json",
            ],
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        payload = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(payload[0]["prompt"]["text"], "编码检查 ⚠")

    def test_badcase_detector_creates_review_only_candidate_with_trace_evidence(self) -> None:
        base = retained_workspace("badcase-detect")
        project = base / "project"
        project.mkdir()
        store, _ = ph.init_store(project)
        first = ph.build_event(
            root=project,
            platform="codex",
            source_mode="backfill",
            prompt_text="更新 Windows Hook 并验证",
            session_id="badcase-session",
            occurred_at="2026-07-18T01:00:00.000Z",
            turn_id="turn-one",
        )
        correction = ph.build_event(
            root=project,
            platform="codex",
            source_mode="backfill",
            prompt_text="还是没有生成事件，Windows Hook 仍然不工作",
            session_id="badcase-session",
            occurred_at="2026-07-18T01:02:00.000Z",
            turn_id="turn-two",
        )
        self.assertTrue(ph.append_event(store, first))
        trace = ph.build_model_output_event(
            root=project,
            platform="codex",
            session_id="badcase-session",
            occurred_at="2026-07-18T01:01:00.000Z",
            event_type="assistant_text",
            actor_role="assistant",
            output_text="已经修复 Windows Hook。",
            structured=None,
            source_path=str(base / "rollout.jsonl"),
            source_line=10,
            turn_id="turn-one",
            model="gpt-test",
            phase="final_answer",
            prompt_event_id=first["event_id"],
        )
        self.assertTrue(ph.append_model_output(store, trace))
        self.assertTrue(ph.append_event(store, correction))

        detected = ph.detect_badcase_candidates(store)
        self.assertEqual(detected["added"], 1)
        repeated = ph.detect_badcase_candidates(store)
        self.assertEqual(repeated["added"], 0)
        self.assertFalse(repeated["trace_scan"])
        candidate = list(ph.iter_badcase_candidates(store))[0]
        self.assertFalse(candidate["detector"]["asserts_failure"])
        self.assertEqual(
            candidate["evidence"]["prompt_event_ids"],
            [first["event_id"], correction["event_id"]],
        )
        self.assertIn(trace["trace_event_id"], candidate["evidence"]["trace_event_ids"])
        self.assertEqual(candidate["session"]["models"], ["gpt-test"])

        catalog = ph.rebuild_index_for_store(store)
        self.assertEqual(catalog["badcases"]["candidate_count"], 1)
        index = (store / "index" / "BADCASES.md").read_text(encoding="utf-8")
        self.assertIn("Pending review: `1`", index)
        self.assertTrue(ph.doctor_store(store, project)["ok"])

    def test_badcase_confirmation_merge_dismiss_and_lifecycle_are_append_only(self) -> None:
        base = retained_workspace("badcase-lifecycle")
        project = base / "project"
        project.mkdir()
        store, _ = ph.init_store(project)

        prompts = []
        for number, text in enumerate(
            (
                "实现自动刷新",
                "不对，自动刷新后还是没有内容",
                "修复会话顺序",
                "顺序还是不对，会话混在一起",
                "更新说明",
                "你漏了验证步骤",
            ),
            1,
        ):
            event = ph.build_event(
                root=project,
                platform="codex",
                source_mode="backfill",
                prompt_text=text,
                session_id="lifecycle-session",
                occurred_at=f"2026-07-18T02:{number:02d}:00.000Z",
                turn_id=f"turn-{number}",
            )
            self.assertTrue(ph.append_event(store, event))
            prompts.append(event)

        result = ph.detect_badcase_candidates(store)
        self.assertEqual(result["added"], 3)
        candidates = list(ph.iter_badcase_candidates(store))
        first_candidate, merge_candidate, dismiss_candidate = candidates
        self.assertEqual(len({item["source_prompt_event_id"] for item in candidates}), 3)
        self.assertEqual(len({item["fingerprint"] for item in candidates}), 3)
        self.assertEqual(
            [item["proposal"]["title"] for item in candidates],
            [
                "不对，自动刷新后还是没有内容",
                "顺序还是不对，会话混在一起",
                "你漏了验证步骤",
            ],
        )
        confirmed = ph.confirm_badcase_candidate(
            store,
            candidate_id=first_candidate["candidate_id"],
            title="自动刷新未写入内容",
            phenomenon="刷新完成但索引没有新增内容",
            red_condition="同步结束后索引仍缺少新事件",
            green_condition="同步结束后索引包含对应事件 ID",
            expected_failure_reason="旧实现只更新原始事件文件而没有重建索引",
            category="index_consistency Authorization: Bearer test-secret-value",
            severity="high",
            guard_type="integration password=test-guard-secret",
            verification="运行一次增量同步并检查 BADCASES 索引",
            root_cause="索引刷新遗漏",
            fix_method=None,
        )
        case_id = confirmed["case_id"]
        merged = ph.decide_badcase_candidate(
            store,
            candidate_id=merge_candidate["candidate_id"],
            action="merged",
            reason="属于同一个索引一致性工作流",
            target_case_id=case_id,
        )
        self.assertTrue(merged["changed"])
        merge_event_count = len(list(ph.iter_badcase_case_events(store)))
        repeated_merge = ph.decide_badcase_candidate(
            store,
            candidate_id=merge_candidate["candidate_id"],
            action="merged",
            reason="重复调用不应追加第二个 merge update",
            target_case_id=case_id,
        )
        self.assertFalse(repeated_merge["changed"])
        self.assertEqual(len(list(ph.iter_badcase_case_events(store))), merge_event_count)
        dismissed = ph.decide_badcase_candidate(
            store,
            candidate_id=dismiss_candidate["candidate_id"],
            action="dismissed",
            reason="只是要求补充验证，不足以证明发生 badcase",
        )
        self.assertTrue(dismissed["changed"])
        updated = ph.update_badcase_case(
            store,
            case_id=case_id,
            patch={
                "status": "resolved",
                "last_checked_at": "2026-07-18T03:00:00.000Z",
                "harness": {"lifecycle": "probation"},
            },
            note="新模型在不注入补偿规则时连续通过，进入观察期",
        )
        self.assertTrue(updated["changed"])

        cases = ph.active_badcase_cases(store)
        self.assertEqual(cases[case_id]["status"], "resolved")
        self.assertEqual(cases[case_id]["harness"]["lifecycle"], "probation")
        self.assertNotIn("test-secret-value", cases[case_id]["category"])
        self.assertNotIn("test-guard-secret", cases[case_id]["acceptance"]["guard_type"])
        self.assertEqual(len(cases[case_id]["source_candidate_ids"]), 2)
        decisions = ph.badcase_candidate_decisions(store)
        self.assertEqual(decisions[first_candidate["candidate_id"]]["action"], "confirmed")
        self.assertEqual(decisions[merge_candidate["candidate_id"]]["action"], "merged")
        self.assertEqual(decisions[dismiss_candidate["candidate_id"]]["action"], "dismissed")

        ph.rebuild_index_for_store(store)
        detail = (store / "index" / "badcase" / f"{case_id}.md").read_text(encoding="utf-8")
        self.assertIn("Red condition: 同步结束后索引仍缺少新事件", detail)
        self.assertIn("Harness lifecycle: `probation`", detail)
        doctor = ph.doctor_store(store, project)
        self.assertTrue(doctor["ok"], doctor)
        self.assertEqual(doctor["badcase_candidate_count"], 3)
        self.assertEqual(doctor["badcase_case_count"], 1)
        ph.update_badcase_case(
            store,
            case_id=case_id,
            patch={"status": "recurred", "recurrence_analysis": None},
            note="corruption fixture: recurrence lacks analysis",
        )
        ph.rebuild_index_for_store(store)
        invalid = ph.doctor_store(store, project)
        self.assertFalse(invalid["ok"])
        self.assertTrue(any("requires checked recurrence analysis" in value for value in invalid["errors"]))

    def test_feature_chain_red_green_approval_and_test_hub_evidence_lifecycle(self) -> None:
        base = retained_workspace("feature-chain-red-green")
        project = base / "project"
        project.mkdir()
        store, _ = ph.init_store(project)
        first = ph.build_event(
            root=project,
            platform="codex",
            source_mode="backfill",
            prompt_text="实现状态保存",
            session_id="feature-chain-session",
            occurred_at="2026-07-18T05:00:00.000Z",
            turn_id="turn-one",
        )
        correction = ph.build_event(
            root=project,
            platform="codex",
            source_mode="backfill",
            prompt_text="还是没有保存，刷新后状态又丢了",
            session_id="feature-chain-session",
            occurred_at="2026-07-18T05:01:00.000Z",
            turn_id="turn-two",
        )
        self.assertTrue(ph.append_event(store, first))
        self.assertTrue(ph.append_event(store, correction))
        ph.detect_badcase_candidates(store)
        candidate = next(ph.iter_badcase_candidates(store))
        case_id = ph.confirm_badcase_candidate(
            store,
            candidate_id=candidate["candidate_id"],
            title="状态刷新后丢失",
            phenomenon="刷新后恢复旧状态",
            red_condition="刷新后状态丢失",
            green_condition="刷新后保留新状态",
            expected_failure_reason="旧实现没有持久化状态",
            category="persistence",
            severity="high",
            guard_type="feature-chain",
            verification="运行保存和刷新流程",
            root_cause="状态只保存在内存",
            fix_method="写入持久化存储",
        )["case_id"]
        proposed = ph.create_feature_chain(
            store,
            title="状态保存与刷新",
            entry="修改状态并刷新",
            exit_check="刷新后仍显示新状态",
            checkpoint_title="状态已持久化",
            checkpoint_check="刷新后必须恢复新状态",
            case_ids=[case_id],
        )
        chain_id = proposed["chain_id"]
        ph.attach_feature_chain_case(
            store,
            chain_id=chain_id,
            checkpoint_title="页面恢复正确状态",
            checkpoint_check="页面必须显示持久化后的状态",
            case_id=case_id,
        )

        runner = project / "feature_chain_runner.py"
        runner.write_text(
            "import os, pathlib, sys, time\n"
            "mode = sys.argv[1]\n"
            "if mode == 'red':\n"
            "    print('PH_CHECKPOINT:状态已持久化:FAIL:old symptom returned')\n"
            "    print('PH_CHECKPOINT:页面恢复正确状态:PASS')\n"
            "elif mode == 'fail-green':\n"
            "    print('PH_CHECKPOINT:状态已持久化:PASS')\n"
            "    print('PH_CHECKPOINT:页面恢复正确状态:FAIL:refresh lost state')\n"
            "    pathlib.Path(os.environ['PROMPT_HARNESS_RUN_DIR'], 'state.txt').write_text('lost', encoding='utf-8')\n"
            "elif mode == 'missing':\n"
            "    print('PH_CHECKPOINT:状态已持久化:PASS')\n"
            "elif mode == 'unknown':\n"
            "    print('PH_CHECKPOINT:状态已持久化:PASS')\n"
            "    print('PH_CHECKPOINT:页面恢复正确状态:PASS')\n"
            "    print('PH_CHECKPOINT:未注册步骤:PASS')\n"
            "elif mode == 'timeout':\n"
            "    time.sleep(2)\n"
            "else:\n"
            "    print('PH_CHECKPOINT:状态已持久化:PASS')\n"
            "    print('PH_CHECKPOINT:页面恢复正确状态:PASS')\n"
            "    pathlib.Path(os.environ['PROMPT_HARNESS_RUN_DIR'], 'temporary.txt').write_text('ok', encoding='utf-8')\n"
            "",
            encoding="utf-8",
        )

        command = lambda mode: {"argv": [sys.executable, str(runner), mode]}
        rejected = ph.approve_feature_chain(
            store,
            root=project,
            chain_id=chain_id,
            red_command=command("green"),
            green_command=command("green"),
            expected_red_reason="old symptom returned",
        )
        self.assertFalse(rejected["changed"])
        self.assertEqual(rejected["reason"], "approval_preflight_failed")
        self.assertEqual(ph.active_feature_chains(store)[chain_id]["status"], "proposed")

        approved = ph.approve_feature_chain(
            store,
            root=project,
            chain_id=chain_id,
            red_command=command("red"),
            green_command=command("green"),
            expected_red_reason="old symptom returned",
        )
        self.assertTrue(approved["changed"])
        self.assertTrue(approved["dry_run"]["passed"])
        chain = ph.active_feature_chains(store)[chain_id]
        self.assertEqual(chain["status"], "approved")
        self.assertFalse(any((store / "badcases" / "runs" / run_id).exists() for run_id in approved["dry_run"]["run_ids"]))
        duplicate_chain_id = ph.create_feature_chain(
            store,
            title="刷新后状态保存",
            entry="修改状态并刷新页面",
            exit_check="页面恢复已保存状态",
            checkpoint_title="状态已持久化",
            checkpoint_check="刷新后必须恢复新状态",
            case_ids=[case_id],
        )["chain_id"]
        duplicate_approval = ph.approve_feature_chain(
            store,
            root=project,
            chain_id=duplicate_chain_id,
            red_command=command("red"),
            green_command=command("green"),
            expected_red_reason="old symptom returned",
        )
        self.assertEqual(duplicate_approval["reason"], "duplicate_feature_chain")
        self.assertEqual(ph.active_feature_chains(store)[duplicate_chain_id]["status"], "proposed")

        config = ph.read_json_object(store / "config.json")
        self.assertFalse(config["badcases"]["automation_enabled"])
        config["badcases"]["automation_enabled"] = True
        ph.write_json(store / "config.json", config)
        passed_sync = ph.auto_sync_project(
            project,
            source_platform="codex",
            session_id="feature-chain-session",
            trigger="stop",
            force=True,
            claude_home=base / ".claude",
            codex_home=base / ".codex",
        )
        self.assertEqual(passed_sync["status"], "completed")
        passed = passed_sync["completion_tests"]
        self.assertEqual((passed["passed"], passed["failed"], passed["blocked"]), (1, 0, 0))
        self.assertIsNone(passed["results"][0]["evidence_path"])
        ph.set_feature_chain_run_policy(
            store, chain_id=chain_id, run_policy="manual", reason="manual selection fixture"
        )
        self.assertEqual(ph.test_hub_dev_complete(store, root=project)["selected_count"], 0)
        explicit = ph.test_hub_dev_complete(
            store, root=project, selected_target_ids={chain_id}
        )
        self.assertEqual((explicit["selected_count"], explicit["passed"]), (1, 1))
        ph.set_feature_chain_run_policy(
            store, chain_id=chain_id, run_policy="every-dev-completion", reason="restore scheduled completion"
        )

        # Canonical records remain immutable; this local folded-chain override
        # exercises the runner's failure path without rewriting approval state.
        failed = ph.run_feature_chain_command(
            store,
            root=project,
            chain={**ph.active_feature_chains(store)[chain_id], "green_command": command("fail-green")},
            command=command("fail-green"),
            mode="dev_complete",
            expectation="green",
        )
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(failed["expectation_met"])
        evidence = Path(failed["evidence_path"])
        self.assertTrue((evidence / "state.txt").is_file())

        missing = ph.run_feature_chain_command(
            store,
            root=project,
            chain=ph.active_feature_chains(store)[chain_id],
            command=command("missing"),
            mode="dev_complete",
            expectation="green",
        )
        self.assertEqual(missing["reason"], "missing required checkpoint: 页面恢复正确状态")
        unknown = ph.run_feature_chain_command(
            store,
            root=project,
            chain=ph.active_feature_chains(store)[chain_id],
            command=command("unknown"),
            mode="dev_complete",
            expectation="green",
        )
        self.assertEqual(unknown["reason"], "unknown checkpoint marker: 未注册步骤")
        blocked = ph.run_feature_chain_command(
            store,
            root=project,
            chain=ph.active_feature_chains(store)[chain_id],
            command={"argv": [sys.executable, str(runner), "timeout"], "timeout_seconds": 1},
            mode="dev_complete",
            expectation="green",
        )
        self.assertEqual((blocked["status"], blocked["reason"]), ("blocked", "command timed out"))
        self.assertTrue(Path(blocked["evidence_path"]).is_dir())

        optional = ph.set_feature_chain_checkpoint_policy(
            store,
            chain_id=chain_id,
            checkpoint_title="页面恢复正确状态",
            required=False,
            reason="该步骤只在浏览器集成环境运行",
        )
        self.assertTrue(optional["changed"])
        optional_run = ph.run_feature_chain_command(
            store,
            root=project,
            chain=ph.active_feature_chains(store)[chain_id],
            command=command("missing"),
            mode="dev_complete",
            expectation="green",
        )
        self.assertTrue(optional_run["expectation_met"])

        rerun = ph.run_feature_chain_command(
            store,
            root=project,
            chain=ph.active_feature_chains(store)[chain_id],
            command=command("green"),
            mode="dev_complete",
            expectation="green",
        )
        self.assertTrue(rerun["expectation_met"])
        self.assertFalse((store / "badcases" / "runs" / rerun["run_id"]).exists())
        self.assertEqual(len(ph.active_harness_runs(store)), 12)
        catalog = ph.rebuild_index_for_store(store)
        self.assertEqual(catalog["test_hub"]["feature_chain_count"], 2)
        self.assertIn("状态保存与刷新", (store / "index" / "TEST_HUB.md").read_text(encoding="utf-8"))
        self.assertIn("Prompt Harness Test Hub", (store / "index" / "test-hub" / "index.html").read_text(encoding="utf-8"))
        doctor = ph.doctor_store(store, project)
        self.assertTrue(doctor["ok"], doctor)
        self.assertEqual(doctor["feature_chain_count"], 2)
        self.assertEqual(doctor["harness_run_count"], 12)

    def test_feature_chain_planning_overlap_coverage_and_candidate_groups_are_read_only(self) -> None:
        base = retained_workspace("feature-chain-planning")
        project = base / "project"
        project.mkdir()
        store, _ = ph.init_store(project)

        specifications = (
            ("实现 Markdown 公式预览", "还是不对，公式预览没有渲染", "公式预览未渲染", ["markdown", "preview"]),
            ("保存 Markdown 草稿", "仍然没有保存，刷新后草稿丢了", "Markdown 草稿丢失", ["markdown", "persistence"]),
            ("实现游戏暂停恢复", "还是不行，暂停后无法恢复", "游戏暂停无法恢复", ["game", "state"]),
            ("实现游戏关卡重试", "仍然错误，重试后状态混在一起", "游戏重试状态串线", ["game", "state"]),
        )
        case_ids = []
        minute = 0
        for index, (request, correction_text, title, tags) in enumerate(specifications, 1):
            request_event = ph.build_event(
                root=project,
                platform="codex",
                source_mode="backfill",
                prompt_text=request,
                session_id="planning-session",
                occurred_at=f"2026-07-18T06:{minute:02d}:00.000Z",
                turn_id=f"turn-{index}-request",
            )
            minute += 1
            correction_event = ph.build_event(
                root=project,
                platform="codex",
                source_mode="backfill",
                prompt_text=correction_text,
                session_id="planning-session",
                occurred_at=f"2026-07-18T06:{minute:02d}:00.000Z",
                turn_id=f"turn-{index}-correction",
            )
            minute += 1
            self.assertTrue(ph.append_event(store, request_event))
            self.assertTrue(ph.append_event(store, correction_event))
        ph.detect_badcase_candidates(store)
        candidates = list(ph.iter_badcase_candidates(store))
        self.assertEqual(len(candidates), 4)
        for candidate, (_, _, title, tags) in zip(candidates, specifications):
            case_ids.append(
                ph.confirm_badcase_candidate(
                    store,
                    candidate_id=candidate["candidate_id"],
                    title=title,
                    phenomenon=title,
                    red_condition=f"{title}再次发生",
                    green_condition=f"{title}不再发生",
                    expected_failure_reason=f"旧症状会触发 {title} checkpoint",
                    category=tags[0],
                    severity="medium",
                    guard_type="feature-chain",
                    verification=f"运行 {title} 用户流程",
                    root_cause="unknown",
                    fix_method=None,
                    tags=tags,
                    trigger_reproduction=f"执行 {title} 对应入口",
                )["case_id"]
            )

        primary = ph.create_feature_chain(
            store,
            title="Markdown 编辑与预览",
            entry="输入 Markdown 并刷新预览",
            exit_check="预览和草稿状态正确",
            checkpoint_title="公式预览完成",
            checkpoint_check="公式必须完成渲染",
            case_ids=[case_ids[0]],
        )["chain_id"]
        ph.attach_feature_chain_case(
            store,
            chain_id=primary,
            checkpoint_title="草稿保存完成",
            checkpoint_check="刷新后必须恢复 Markdown 草稿",
            case_id=case_ids[1],
        )
        duplicate = ph.create_feature_chain(
            store,
            title="Markdown 公式预览与草稿",
            entry="编辑 Markdown 后查看预览",
            exit_check="公式和草稿保持正确",
            checkpoint_title="Markdown 预览稳定",
            checkpoint_check="公式预览不能显示原始文本",
            case_ids=[case_ids[0]],
        )["chain_id"]

        coverage = ph.feature_chain_coverage_report(store)
        self.assertEqual(coverage["covered_case_count"], 2)
        self.assertEqual({item["case_id"] for item in coverage["unassigned"]}, set(case_ids[2:]))
        groups = ph.feature_chain_candidate_groups(store)
        self.assertEqual(groups["candidate_group_count"], 1)
        self.assertEqual(set(groups["groups"][0]["case_ids"]), set(case_ids[2:]))
        ph.attach_feature_chain_case(
            store,
            chain_id=primary,
            checkpoint_title="预览错误定位完成",
            checkpoint_check="错误必须定位到对应 Markdown 块",
            case_id=case_ids[2],
        )
        primary_case_ids = {
            case_id
            for checkpoint in ph.active_feature_chains(store)[primary]["checkpoints"]
            for case_id in checkpoint.get("case_ids", [])
        }
        self.assertEqual(len(primary_case_ids), 3)
        event_count = len(list(ph.iter_feature_chain_events(store)))
        overlap = ph.feature_chain_overlap_report(store)
        self.assertTrue(
            any(
                {item["left_chain_id"], item["right_chain_id"]} == {primary, duplicate}
                for item in overlap["pairs"]
            )
        )
        plan = ph.feature_chain_plan(store, query=case_ids[1])
        self.assertEqual(plan["action"], "review-existing-chain")
        self.assertEqual(plan["match"]["chain_id"], primary)
        self.assertFalse(plan["mutated"])
        self.assertEqual(len(list(ph.iter_feature_chain_events(store))), event_count)

        ph.rebuild_index_for_store(store)
        doctor = ph.doctor_store(store, project)
        self.assertTrue(doctor["ok"], doctor)

    def test_test_hub_runs_two_chains_preserves_one_failure_and_recovers(self) -> None:
        base = retained_workspace("test-hub-multi-chain")
        project = base / "project"
        project.mkdir()
        store, _ = ph.init_store(project)
        case_ids = []
        for index, (request, correction, title) in enumerate(
            (
                ("实现清单保存", "还是没有保存，刷新后丢了", "清单刷新后丢失"),
                ("实现故事卡生成", "仍然不对，重复生成结果变了", "故事卡重复生成漂移"),
            ),
            1,
        ):
            first = ph.build_event(
                root=project,
                platform="codex",
                source_mode="backfill",
                prompt_text=request,
                session_id=f"hub-session-{index}",
                occurred_at=f"2026-07-18T07:0{index}:00.000Z",
                turn_id="request",
            )
            second = ph.build_event(
                root=project,
                platform="codex",
                source_mode="backfill",
                prompt_text=correction,
                session_id=f"hub-session-{index}",
                occurred_at=f"2026-07-18T07:1{index}:00.000Z",
                turn_id="correction",
            )
            ph.append_event(store, first)
            ph.append_event(store, second)
        ph.detect_badcase_candidates(store)
        for candidate, title in zip(
            ph.iter_badcase_candidates(store),
            ("清单刷新后丢失", "故事卡重复生成漂移"),
        ):
            case_ids.append(
                ph.confirm_badcase_candidate(
                    store,
                    candidate_id=candidate["candidate_id"],
                    title=title,
                    phenomenon=title,
                    red_condition=f"{title}再次发生",
                    green_condition=f"{title}不再发生",
                    expected_failure_reason="old symptom",
                    category="workflow",
                    severity="high",
                    guard_type="feature-chain",
                    verification=f"执行 {title} 流程",
                    root_cause="unknown",
                    fix_method=None,
                )["case_id"]
            )

        runner = project / "multi_chain_runner.py"
        runner.write_text(
            "import os, pathlib, sys\n"
            "label, mode = sys.argv[1], sys.argv[2]\n"
            "flag = pathlib.Path(sys.argv[3])\n"
            "if mode == 'red':\n"
            "    print(f'PH_CHECKPOINT:{label}:FAIL:old symptom')\n"
            "elif flag.exists():\n"
            "    print(f'PH_CHECKPOINT:{label}:FAIL:forced regression')\n"
            "    pathlib.Path(os.environ['PROMPT_HARNESS_RUN_DIR'], 'failure.txt').write_text(label, encoding='utf-8')\n"
            "else:\n"
            "    print(f'PH_CHECKPOINT:{label}:PASS')\n",
            encoding="utf-8",
        )
        flags = [project / "fail-one.flag", project / "fail-two.flag"]
        labels = ["清单状态已保存", "故事卡输出稳定"]
        chain_ids = []
        for index, (case_id, label, flag) in enumerate(zip(case_ids, labels, flags), 1):
            chain_id = ph.create_feature_chain(
                store,
                title=f"独立工作流 {index}",
                entry=f"触发工作流 {index}",
                exit_check=f"工作流 {index} 正确完成",
                checkpoint_title=label,
                checkpoint_check=f"{label} 必须通过",
                case_ids=[case_id],
            )["chain_id"]
            chain_ids.append(chain_id)
            command = lambda mode, label=label, flag=flag: {
                "argv": [sys.executable, str(runner), label, mode, str(flag)]
            }
            approved = ph.approve_feature_chain(
                store,
                root=project,
                chain_id=chain_id,
                red_command=command("red"),
                green_command=command("green"),
                expected_red_reason="old symptom",
            )
            self.assertTrue(approved["changed"])

        flags[1].write_text("fail", encoding="utf-8")
        mixed = ph.test_hub_dev_complete(store, root=project, jobs=2)
        self.assertEqual((mixed["passed"], mixed["failed"], mixed["blocked"]), (1, 1, 0))
        results = {item["target_id"]: item for item in mixed["results"]}
        self.assertEqual(results[chain_ids[0]]["status"], "passed")
        self.assertEqual(results[chain_ids[1]]["status"], "failed")
        failed_evidence = Path(results[chain_ids[1]]["evidence_path"])
        self.assertTrue((failed_evidence / "failure.txt").is_file())

        flags[1].unlink()
        recovered = ph.test_hub_dev_complete(store, root=project, jobs=2)
        self.assertEqual((recovered["passed"], recovered["failed"], recovered["blocked"]), (2, 0, 0))
        self.assertTrue(failed_evidence.is_dir())
        self.assertTrue(all(item["evidence_path"] is None for item in recovered["results"]))
        last_run = json.loads(
            (store / "index" / "test-hub" / "last-run.json").read_text(encoding="utf-8")
        )
        self.assertEqual(last_run["passed"], 2)

        ph.rebuild_index_for_store(store)
        doctor = ph.doctor_store(store, project)
        self.assertTrue(doctor["ok"], doctor)

    def test_snapshot_and_two_adapter_replay_matrix_are_stable_private_and_auditable(self) -> None:
        base = retained_workspace("snapshot-replay-matrix")
        project = base / "project"
        project.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        source = project / "app.txt"
        source.write_text("initial\n", encoding="utf-8")
        (project / ".env").write_text("API_KEY=should-never-be-copied\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(project), "add", "app.txt", ".env"], check=True)
        subprocess.run(
            [
                "git", "-C", str(project), "-c", "user.name=Prompt Harness Test",
                "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture",
            ],
            check=True,
        )
        store, _ = ph.init_store(project)
        request = ph.build_event(
            root=project,
            platform="codex",
            source_mode="backfill",
            prompt_text="实现稳定输出",
            session_id="replay-session",
            occurred_at="2026-07-18T08:00:00.000Z",
            turn_id="request",
        )
        correction = ph.build_event(
            root=project,
            platform="codex",
            source_mode="backfill",
            prompt_text="还是不对，输出又发生漂移",
            session_id="replay-session",
            occurred_at="2026-07-18T08:01:00.000Z",
            turn_id="correction",
        )
        ph.append_event(store, request)
        ph.append_event(store, correction)
        ph.detect_badcase_candidates(store)
        candidate = next(ph.iter_badcase_candidates(store))
        case_id = ph.confirm_badcase_candidate(
            store,
            candidate_id=candidate["candidate_id"],
            title="输出漂移",
            phenomenon="相同输入产生不兼容输出",
            red_condition="输出结构漂移",
            green_condition="输出结构稳定",
            expected_failure_reason="historical output mismatch",
            category="model-behavior",
            severity="high",
            guard_type="replay",
            verification="固定快照重放",
            root_cause="historical root cause must stay hidden",
            fix_method="historical fix must stay hidden",
        )["case_id"]

        first = ph.create_project_snapshot(
            store,
            root=project,
            case_id=case_id,
            tools=["fake-tool"],
            skills=["fake-skill"],
            configuration={"token": "sk-abcdefghijklmnopqrstuvwxyz123456"},
        )
        second = ph.create_project_snapshot(store, root=project, case_id=case_id, tools=["fake-tool"], skills=["fake-skill"], configuration={"token": "sk-abcdefghijklmnopqrstuvwxyz123456"})
        self.assertEqual(first["snapshot"]["snapshot_id"], second["snapshot"]["snapshot_id"])
        self.assertFalse(second["changed"])
        manifest = first["snapshot"]["manifest"]
        self.assertNotIn(".env", {item["path"] for item in manifest["files"]})
        self.assertIn(".env", {item["path"] for item in manifest["excluded"]})
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", json.dumps(manifest))
        self.assertEqual(source.read_text(encoding="utf-8"), "initial\n")
        source.write_text("dirty\n", encoding="utf-8")
        dirty = ph.create_project_snapshot(store, root=project, case_id=case_id)
        self.assertNotEqual(first["snapshot"]["snapshot_id"], dirty["snapshot"]["snapshot_id"])
        materialized_parent = base / "materialized"
        materialized_parent.mkdir()
        with self.assertRaises(ValueError):
            ph.materialize_project_snapshot(
                store, root=project, snapshot_id=dirty["snapshot"]["snapshot_id"]
            )
        ph.approve_snapshot_materialization(
            store,
            root=project,
            snapshot_id=dirty["snapshot"]["snapshot_id"],
            destination_parent=materialized_parent,
            reason="isolated replay workspace",
        )
        materialized = ph.materialize_project_snapshot(
            store, root=project, snapshot_id=dirty["snapshot"]["snapshot_id"]
        )
        materialized_root = Path(materialized["destination"])
        self.assertEqual((materialized_root / "app.txt").read_text(encoding="utf-8"), "dirty\n")
        self.assertFalse((materialized_root / ".env").exists())
        self.assertEqual(source.read_text(encoding="utf-8"), "dirty\n")
        outside = base / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        (project / "escape-link").symlink_to(outside)
        with self.assertRaises(ValueError):
            ph.create_project_snapshot(store, root=project, case_id=case_id)
        (project / "escape-link").unlink()

        adapter_script = project / "fake_adapter.py"
        adapter_script.write_text(
            "import json, os, pathlib, sys\n"
            "data=json.loads(pathlib.Path(os.environ['PROMPT_HARNESS_INPUT_PATH']).read_text(encoding='utf-8'))\n"
            "blob=json.dumps(data, ensure_ascii=False)\n"
            "assert 'historical root cause' not in blob and 'historical fix' not in blob\n"
            "name=sys.argv[1]\n"
            "result={'schema_version':'1.0.0','status':'completed','answer':name+' ok','metrics':{'score':1}}\n"
            "pathlib.Path(os.environ['PROMPT_HARNESS_RESULT_PATH']).write_text(json.dumps(result), encoding='utf-8')\n",
            encoding="utf-8",
        )
        adapter_ids = []
        for name in ("model-a", "model-b"):
            proposed = ph.propose_model_adapter(
                store,
                root=project,
                name=name,
                platform="fake",
                model=name,
                command={"argv": [sys.executable, str(adapter_script), name]},
            )
            adapter_ids.append(proposed["adapter_id"])
            approved = ph.approve_model_adapter(store, root=project, adapter_id=proposed["adapter_id"])
            self.assertTrue(approved["changed"], approved)

        matrix = ph.run_replay_matrix(
            store,
            root=project,
            case_id=case_id,
            snapshot_id=dirty["snapshot"]["snapshot_id"],
            adapter_ids=adapter_ids,
        )
        self.assertEqual((matrix["passed"], matrix["failed"], matrix["blocked"]), (2, 0, 0))
        self.assertEqual({run["result"]["answer"] for run in matrix["runs"]}, {"model-a ok", "model-b ok"})
        self.assertEqual(
            {run["execution_isolation"] for run in matrix["runs"]},
            {"approved-materialized-snapshot"},
        )
        ph.rebuild_index_for_store(store)
        doctor = ph.doctor_store(store, project)
        self.assertTrue(doctor["ok"], doctor)
        self.assertEqual(doctor["snapshot_count"], 2)
        self.assertEqual(doctor["model_adapter_count"], 2)

    def test_adapter_preflight_rejects_timeout_nonzero_and_malformed_results(self) -> None:
        base = retained_workspace("adapter-failures")
        project = base / "project"
        project.mkdir()
        store, _ = ph.init_store(project)
        script = project / "bad_adapter.py"
        script.write_text(
            "import os, pathlib, sys, time\n"
            "mode=sys.argv[1]\n"
            "if mode=='timeout': time.sleep(2)\n"
            "elif mode=='nonzero': raise SystemExit(7)\n"
            "elif mode=='malformed': pathlib.Path(os.environ['PROMPT_HARNESS_RESULT_PATH']).write_text('{bad json', encoding='utf-8')\n"
            "elif mode in {'task','policy'}:\n"
            " import json\n"
            " result={'schema_version':'1.0.0','status':'failed' if mode=='task' else 'blocked','answer':'classified','reason':mode,'metrics':{}}\n"
            " pathlib.Path(os.environ['PROMPT_HARNESS_RESULT_PATH']).write_text(json.dumps(result), encoding='utf-8')\n",
            encoding="utf-8",
        )
        expected = {
            "timeout": ("blocked", "environment"),
            "nonzero": ("failed", "tool-runtime"),
            "malformed": ("failed", "adapter-protocol"),
            "task": ("failed", "task"),
            "policy": ("blocked", "policy-blocker"),
        }
        for mode, (status, attribution) in expected.items():
            adapter_id = ph.propose_model_adapter(
                store, root=project, name=mode, platform="fake", model=mode,
                command={
                    "argv": [sys.executable, str(script), mode],
                    "timeout_seconds": 1,
                },
            )["adapter_id"]
            approval = ph.approve_model_adapter(store, root=project, adapter_id=adapter_id)
            self.assertEqual(approval["reason"], "approval_preflight_failed")
            self.assertEqual(approval["run"]["status"], status)
            self.assertEqual(approval["run"]["attribution"], attribution)
            self.assertTrue(Path(approval["run"]["evidence_path"]).is_dir())
            self.assertEqual(ph.active_model_adapters(store)[adapter_id]["status"], "proposed")

    def test_doctor_rejects_corruption_in_every_adaptive_ledger(self) -> None:
        fixtures = [
            (
                ph.task_case_event_file,
                {"record_type": ph.TASK_CASE_EVENT_RECORD_TYPE, "event_id": "bad", "task_case_id": "bad", "action": "approved"},
                "task-case event_id",
            ),
            (
                ph.adapter_event_file,
                {"record_type": ph.ADAPTER_EVENT_RECORD_TYPE, "event_id": "bad", "adapter_id": "bad", "action": "approved"},
                "model adapter event_id",
            ),
            (
                ph.judge_event_file,
                {"record_type": ph.JUDGE_EVENT_RECORD_TYPE, "event_id": "bad", "judge_id": "deterministic", "action": "evaluated", "replay_run_id": "missing"},
                "judge event_id",
            ),
            (
                ph.compensation_event_file,
                {"record_type": ph.COMPENSATION_EVENT_RECORD_TYPE, "event_id": "bad", "compensation_id": "bad", "action": "approved"},
                "compensation event_id",
            ),
            (
                ph.attribution_event_file,
                {"record_type": ph.ATTRIBUTION_EVENT_RECORD_TYPE, "event_id": "bad", "run_id": "missing", "action": "overridden", "attribution": "unknown"},
                "attribution event_id",
            ),
            (
                ph.policy_event_file,
                {"record_type": ph.POLICY_EVENT_RECORD_TYPE, "event_id": "bad", "target_type": "project", "target_id": "bad", "action": "set", "policy": {}},
                "policy event_id",
            ),
            (
                ph.subagent_event_file,
                {"record_type": ph.SUBAGENT_EVENT_RECORD_TYPE, "event_id": "bad", "binding_id": "bad", "action": "completed"},
                "subagent event_id",
            ),
            (
                ph.snapshot_event_file,
                {"record_type": ph.SNAPSHOT_EVENT_RECORD_TYPE, "snapshot_id": "bad", "case_id": "missing", "project": {}, "manifest": {}, "manifest_sha256": "bad"},
                "snapshot_id",
            ),
            (
                ph.harness_run_event_file,
                {"record_type": ph.HARNESS_RUN_EVENT_RECORD_TYPE, "run_event_id": "bad", "run_id": "bad", "action": "completed"},
                "run_event_id",
            ),
        ]
        for index, (path_getter, row, expected) in enumerate(fixtures):
            with self.subTest(ledger=path_getter.__name__):
                project = retained_workspace(f"doctor-ledger-{index}") / "project"
                project.mkdir()
                store, _ = ph.init_store(project)
                ph.rebuild_index_for_store(store)
                write_jsonl(path_getter(store), [{"schema_version": "1.0.0", **row}])
                result = ph.doctor_store(store, project)
                self.assertFalse(result["ok"])
                self.assertIn(expected, "\n".join(result["errors"]))

    def test_context_view_tracks_task_switch_and_resume_without_copying_trajectory(self) -> None:
        project = retained_workspace("context-switch") / "project"
        project.mkdir()
        store, _ = ph.init_store(project)
        for session_id, minute, text in (
            ("task-a", 0, "必须保留原始证据，开始任务 A"),
            ("task-b", 1, "切换到任务 B，只做只读检查"),
            ("task-c", 2, "现在处理任务 C"),
        ):
            ph.append_event(
                store,
                ph.build_event(
                    root=project, platform="codex", source_mode="backfill",
                    prompt_text=text, session_id=session_id, turn_id="start",
                    occurred_at=f"2026-07-18T10:0{minute}:00.000Z",
                ),
            )
        catalog = ph.rebuild_index_for_store(store)
        self.assertEqual(catalog["context"]["active_session"]["session_id"], "task-c")
        self.assertEqual(catalog["context"]["parked_session_count"], 2)
        ph.append_event(
            store,
            ph.build_event(
                root=project, platform="codex", source_mode="backfill",
                prompt_text="恢复任务 A，从上次下一步继续", session_id="task-a", turn_id="resume",
                occurred_at="2026-07-18T10:04:00.000Z",
            ),
        )
        resumed = ph.rebuild_index_for_store(store)
        self.assertEqual(resumed["context"]["active_session"]["session_id"], "task-a")
        context = (store / "index" / "CONTEXT.md").read_text(encoding="utf-8")
        self.assertIn("task-b", context)
        self.assertIn("必须保留原始证据", context)
        self.assertNotIn("# Complete trajectory", context)

    def test_task_judge_compensation_policy_and_subagent_lifecycles(self) -> None:
        base = retained_workspace("adaptive-lifecycles")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("fixture", encoding="utf-8")
        store, _ = ph.init_store(project)
        first = ph.build_event(
            root=project, platform="codex", source_mode="backfill",
            prompt_text="实现多阶段恢复", session_id="adaptive", turn_id="one",
            occurred_at="2026-07-18T09:00:00.000Z",
        )
        correction = ph.build_event(
            root=project, platform="codex", source_mode="backfill",
            prompt_text="还是失败，恢复后没有清理临时状态", session_id="adaptive", turn_id="two",
            occurred_at="2026-07-18T09:01:00.000Z",
        )
        ph.append_event(store, first)
        ph.append_event(store, correction)
        ph.detect_badcase_candidates(store)
        candidate = next(ph.iter_badcase_candidates(store))
        case_id = ph.confirm_badcase_candidate(
            store, candidate_id=candidate["candidate_id"], title="恢复后未清理",
            phenomenon="恢复完成后残留临时状态", red_condition="临时状态仍存在",
            green_condition="恢复后临时状态已清理", expected_failure_reason="cleanup missing",
            category="workflow", severity="high", guard_type="task-case",
            verification="运行恢复和清理阶段", root_cause="cleanup phase omitted", fix_method="add cleanup",
        )["case_id"]
        snapshot_id = ph.create_project_snapshot(store, root=project, case_id=case_id)["snapshot"]["snapshot_id"]

        adapter_script = project / "adaptive_adapter.py"
        adapter_script.write_text(
            "import json, os, pathlib, sys\n"
            "data=json.loads(pathlib.Path(os.environ['PROMPT_HARNESS_INPUT_PATH']).read_text(encoding='utf-8'))\n"
            "mode=sys.argv[1]\n"
            "comp=bool(data.get('compensation'))\n"
            "metrics={} if mode=='judge-path' and data.get('purpose')=='badcase-replay' else {'passed': mode=='modern' or comp or data.get('purpose')=='approval-self-test','tokens':2,'cost_usd':0.5}\n"
            "answer='good result' if mode=='judge-path' else ('fixed' if comp else 'old failure')\n"
            "result={'schema_version':'1.0.0','status':'completed','answer':answer,'metrics':metrics}\n"
            "pathlib.Path(os.environ['PROMPT_HARNESS_RESULT_PATH']).write_text(json.dumps(result), encoding='utf-8')\n",
            encoding="utf-8",
        )
        proposed = ph.propose_model_adapter(
            store, root=project, name="adaptive", platform="fake", model="adaptive-v1",
            command={"argv": [sys.executable, str(adapter_script), "adaptive"]},
        )
        adapter_id = proposed["adapter_id"]
        self.assertTrue(ph.approve_model_adapter(store, root=project, adapter_id=adapter_id)["changed"])
        baseline = ph.run_replay_matrix(
            store, root=project, case_id=case_id, snapshot_id=snapshot_id, adapter_ids=[adapter_id]
        )["runs"][0]
        evaluation = ph.evaluate_replay_outcome(store, root=project, run_id=baseline["run_id"])
        self.assertFalse(evaluation["evaluation"]["passed"])
        proposal = ph.propose_compensation(
            store, replay_run_id=baseline["run_id"], compensation_type="instruction",
            content="Always execute the cleanup phase after recovery.", scope="recovery workflow",
            rationale="The judged baseline reproduces the missing cleanup.",
        )
        compensation_id = proposal["compensation_id"]
        approved = ph.approve_compensation(store, root=project, compensation_id=compensation_id)
        self.assertTrue(approved["changed"], approved)
        ph.transition_compensation(
            store, compensation_id=compensation_id, action="activated", reason="Red/Green replay passed"
        )

        task_runner = project / "task_runner.py"
        task_runner.write_text(
            "import sys\n"
            "mode=sys.argv[1]\n"
            "if mode=='red':\n"
            " print('PH_CHECKPOINT:恢复:PASS')\n"
            " print('PH_CHECKPOINT:清理:FAIL:cleanup missing')\n"
            "else:\n"
            " print('PH_CHECKPOINT:恢复:PASS')\n"
            " print('PH_CHECKPOINT:清理:PASS')\n",
            encoding="utf-8",
        )
        task_case_id = ph.propose_task_case(
            store, root=project, title="恢复与清理",
            phases=[{"name": "恢复", "check": "恢复成功"}, {"name": "清理", "check": "临时状态已删除"}],
            linked_case_ids=[case_id], stop_condition="清理完成", cleanup="删除测试状态",
            exclusions=["不调用生产服务"], blocker_policy=["缺少权限时停止"],
        )["task_case_id"]
        task_approval = ph.approve_task_case(
            store, root=project, task_case_id=task_case_id,
            red_command={"argv": [sys.executable, str(task_runner), "red"]},
            green_command={"argv": [sys.executable, str(task_runner), "green"]},
            expected_red_reason="cleanup missing",
        )
        self.assertTrue(task_approval["changed"], task_approval)
        task_approval_runs = [
            ph.active_harness_runs(store)[run_id]
            for run_id in ph.active_task_cases(store)[task_case_id]["approval_run_ids"]
        ]
        self.assertIn("清理", task_approval_runs[0]["reason"])
        hub = ph.test_hub_dev_complete(store, root=project, jobs=2)
        self.assertEqual((hub["task_case_count"], hub["passed"]), (1, 1))

        ph.set_harness_policy(
            store, target_type="badcase", target_id=case_id,
            policy={"max_attempts": 1, "max_tokens": 1}, reason="bounded regression budget",
        )
        bounded = ph.run_replay_matrix(
            store, root=project, case_id=case_id, snapshot_id=snapshot_id,
            adapter_ids=[adapter_id], compensation_ids=[compensation_id],
        )
        self.assertEqual(bounded["budget_stop"]["reason"], "max_attempts")
        ph.set_harness_policy(
            store, target_type="badcase", target_id=case_id,
            policy={"max_attempts": 8, "max_tokens": 1}, reason="token budget fixture",
        )
        token_bounded = ph.run_replay_matrix(
            store, root=project, case_id=case_id, snapshot_id=snapshot_id,
            adapter_ids=[adapter_id, adapter_id],
        )
        self.assertEqual(token_bounded["budget_stop"]["reason"], "max_tokens")
        ph.set_harness_policy(
            store, target_type="badcase", target_id=case_id,
            policy={"max_attempts": 8, "max_tokens": 0, "max_cost_usd": 0.1},
            reason="cost budget fixture",
        )
        cost_bounded = ph.run_replay_matrix(
            store, root=project, case_id=case_id, snapshot_id=snapshot_id,
            adapter_ids=[adapter_id, adapter_id],
        )
        self.assertEqual(cost_bounded["budget_stop"]["reason"], "max_cost_usd")
        ph.set_harness_policy(
            store, target_type="badcase", target_id=case_id,
            policy={"max_attempts": 8, "max_tokens": 0, "max_cost_usd": 0}, reason="restore normal replay budget",
        )
        binding = ph.bind_subagent(
            store, root=project, platform="codex", session_id="child-session",
            agent_id="worker-1", child_root=project, parent_session_id="adaptive",
        )["binding"]
        completion = ph.record_subagent_completion(
            store, binding_id=binding["binding_id"], evidence_ids=[first["event_id"]], summary="child completed fixture"
        )
        repeated = ph.record_subagent_completion(
            store, binding_id=binding["binding_id"], evidence_ids=[first["event_id"]], summary="child completed fixture"
        )
        self.assertTrue(completion["changed"])
        self.assertFalse(repeated["changed"])
        with self.assertRaises(ValueError):
            ph.bind_subagent(
                store, root=project, platform="codex", session_id="remote", agent_id="remote",
                child_root="https://example.com/repo",
            )

        judge_adapter = ph.propose_model_adapter(
            store, root=project, name="judge-path", platform="fake", model="judge-model",
            command={"argv": [sys.executable, str(adapter_script), "judge-path"]},
        )
        ph.approve_model_adapter(store, root=project, adapter_id=judge_adapter["adapter_id"])
        judge_replay = ph.run_replay_matrix(
            store, root=project, case_id=case_id, snapshot_id=snapshot_id,
            adapter_ids=[judge_adapter["adapter_id"]],
        )["runs"][0]
        self.assertFalse(ph.evaluate_replay_outcome(store, root=project, run_id=judge_replay["run_id"])["decided"])
        judge_script = project / "judge_adapter.py"
        judge_script.write_text(
            "import json, os, pathlib\n"
            "data=json.loads(pathlib.Path(os.environ['PROMPT_HARNESS_INPUT_PATH']).read_text(encoding='utf-8'))\n"
            "passed=data.get('purpose')=='approval-self-test' or 'good' in str(data.get('candidate',{}).get('answer',''))\n"
            "result={'schema_version':'1.0.0','status':'completed','answer':'narrow outcome decision','metrics':{'passed':passed}}\n"
            "pathlib.Path(os.environ['PROMPT_HARNESS_RESULT_PATH']).write_text(json.dumps(result), encoding='utf-8')\n",
            encoding="utf-8",
        )
        judge_id = ph.propose_judge_adapter(
            store, root=project, name="narrow-judge",
            command={"argv": [sys.executable, str(judge_script)]},
        )["judge_id"]
        self.assertTrue(ph.approve_judge_adapter(store, root=project, judge_id=judge_id)["changed"])
        judged = ph.evaluate_replay_outcome(
            store, root=project, run_id=judge_replay["run_id"], judge_id=judge_id,
        )
        self.assertTrue(judged["evaluation"]["passed"])
        modern_ids = []
        for model in ("modern-a", "modern-b"):
            modern_id = ph.propose_model_adapter(
                store, root=project, name=model, platform="fake", model=model,
                command={"argv": [sys.executable, str(adapter_script), "modern"]},
            )["adapter_id"]
            ph.approve_model_adapter(store, root=project, adapter_id=modern_id)
            modern_ids.append(modern_id)
        for _ in range(2):
            modern_matrix = ph.run_replay_matrix(
                store, root=project, case_id=case_id, snapshot_id=snapshot_id,
                adapter_ids=modern_ids if _ == 0 else modern_ids[:1],
            )
            for run in modern_matrix["runs"]:
                ph.evaluate_replay_outcome(store, root=project, run_id=run["run_id"])
        recommendation = ph.compensation_lifecycle_recommendation(
            store, compensation_id=compensation_id, required_consecutive_passes=3,
            distinct_model_minimum=2,
        )
        self.assertEqual(recommendation["recommendation"], "enter-probation")
        ph.transition_compensation(
            store, compensation_id=compensation_id, action="probation",
            reason="three uncompensated passes across two models",
        )
        with self.assertRaises(ValueError):
            ph.run_replay_matrix(
                store, root=project, case_id=case_id, snapshot_id=snapshot_id,
                adapter_ids=[modern_ids[0]], compensation_ids=[compensation_id],
            )
        probation_runs = []
        for selected in (modern_ids, modern_ids[:1]):
            probation_runs.extend(
                ph.run_replay_matrix(
                    store, root=project, case_id=case_id, snapshot_id=snapshot_id,
                    adapter_ids=selected,
                )["runs"]
            )
        for run in probation_runs:
            ph.evaluate_replay_outcome(store, root=project, run_id=run["run_id"])
        self.assertEqual(
            ph.compensation_lifecycle_recommendation(
                store, compensation_id=compensation_id,
                required_consecutive_passes=3, distinct_model_minimum=2,
            )["recommendation"],
            "retire",
        )
        recurrence = ph.run_replay_matrix(
            store, root=project, case_id=case_id, snapshot_id=snapshot_id,
            adapter_ids=[adapter_id],
        )["runs"][0]
        ph.evaluate_replay_outcome(store, root=project, run_id=recurrence["run_id"])
        self.assertEqual(
            ph.compensation_lifecycle_recommendation(
                store, compensation_id=compensation_id,
                required_consecutive_passes=3, distinct_model_minimum=2,
            )["recommendation"],
            "reactivate",
        )
        ph.transition_compensation(
            store, compensation_id=compensation_id, action="reactivated",
            reason="post-probation recurrence",
        )
        ph.append_attribution_override(
            store, run_id=baseline["run_id"], attribution="changed-intent", reason="manual audit override"
        )
        for entity_type, entity_id in (
            ("task_case", task_case_id),
            ("adapter", adapter_id),
            ("judge", judge_id),
        ):
            ph.transition_workflow_entity(
                store, entity_type=entity_type, entity_id=entity_id,
                action="disabled", reason="transition audit fixture",
            )
            ph.transition_workflow_entity(
                store, entity_type=entity_type, entity_id=entity_id,
                action="reactivated", reason="transition audit fixture recovered",
            )
        ph.rebuild_index_for_store(store)
        doctor = ph.doctor_store(store, project)
        self.assertTrue(doctor["ok"], doctor)
        self.assertEqual(doctor["task_case_count"], 1)
        self.assertEqual(doctor["compensation_count"], 1)
        self.assertTrue((store / "index" / "CONTEXT.md").is_file())
        stable_paths = [
            store / "index" / "BADCASES.md",
            store / "index" / "TEST_HUB.md",
            store / "index" / "CONTEXT.md",
            store / "index" / "test-hub" / "index.html",
        ]
        before = {path: path.read_bytes() for path in stable_paths}
        ph.rebuild_index_for_store(store)
        self.assertEqual(before, {path: path.read_bytes() for path in stable_paths})

    def test_auto_sync_detects_badcase_candidate_after_trace_reconciliation(self) -> None:
        base = retained_workspace("badcase-auto-sync")
        project = base / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("project", encoding="utf-8")
        claude_home = base / ".claude"
        encoded = __import__("re").sub(r"[^A-Za-z0-9]", "-", str(project))
        transcript = claude_home / "projects" / encoded / "badcase-session.jsonl"
        write_jsonl(
            transcript,
            [
                {
                    "type": "user",
                    "uuid": "badcase-user-one",
                    "promptId": "badcase-turn-one",
                    "timestamp": "2026-07-18T04:00:00Z",
                    "cwd": str(project),
                    "message": {"role": "user", "content": "实现 Windows 自动刷新"},
                },
                {
                    "type": "assistant",
                    "uuid": "badcase-assistant-one",
                    "parentUuid": "badcase-user-one",
                    "timestamp": "2026-07-18T04:01:00Z",
                    "cwd": str(project),
                    "message": {
                        "role": "assistant",
                        "model": "claude-test",
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "已经实现自动刷新。"}],
                    },
                },
                {
                    "type": "user",
                    "uuid": "badcase-user-two",
                    "promptId": "badcase-turn-two",
                    "parentUuid": "badcase-assistant-one",
                    "timestamp": "2026-07-18T04:02:00Z",
                    "cwd": str(project),
                    "message": {"role": "user", "content": "还是没有更新，Windows 上不工作"},
                },
            ],
        )
        result = ph.auto_sync_project(
            project,
            source_platform="claude",
            session_id="badcase-session",
            trigger="test",
            source_path=transcript,
            claude_home=claude_home,
            codex_home=base / ".codex",
        )
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["badcase_automation_enabled"])
        self.assertEqual(result["badcase_candidates_added"], 0)
        self.assertTrue(result["index_rebuilt"])
        store = project / ".prompt-harness"
        self.assertEqual(list(ph.iter_badcase_candidates(store)), [])

        config = ph.read_json_object(store / "config.json")
        self.assertFalse(config["badcases"]["automation_enabled"])
        config["badcases"]["automation_enabled"] = True
        ph.write_json(store / "config.json", config)
        enabled = ph.auto_sync_project(
            project,
            source_platform="claude",
            session_id="badcase-session",
            trigger="test",
            source_path=transcript,
            force=True,
            claude_home=claude_home,
            codex_home=base / ".codex",
        )
        self.assertEqual(enabled["status"], "completed")
        self.assertTrue(enabled["badcase_automation_enabled"])
        self.assertEqual(enabled["badcase_candidates_added"], 1)
        candidate = list(ph.iter_badcase_candidates(store))[0]
        self.assertEqual(candidate["session"]["models"], ["claude-test"])
        self.assertTrue((store / "index" / "BADCASES.md").is_file())
        self.assertTrue(ph.doctor_store(store, project)["ok"])


if __name__ == "__main__":
    unittest.main()
