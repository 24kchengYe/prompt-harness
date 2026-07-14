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
from contextlib import redirect_stdout
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

    def test_home_directory_is_rejected_as_a_project_root(self) -> None:
        self.assertTrue(ph.is_unsafe_broad_project_root(Path.home()))
        with self.assertRaisesRegex(ValueError, "broad project root"):
            ph.init_store(Path.home())

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
        ph.append_session_binding(
            platform="codex",
            session_id="descendant-session",
            project_root=other,
        )
        self.assertEqual([event["event_id"] for event in ph.iter_active_events(store)], [exact["event_id"]])

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


if __name__ == "__main__":
    unittest.main()
