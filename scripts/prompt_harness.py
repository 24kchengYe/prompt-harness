#!/usr/bin/env python3
"""Project-local prompt ledger for Codex and Claude Code.

The canonical store is append-only JSONL under ``<project>/.prompt-harness``.
Only user-authored prompt text is recorded. File bodies, tool results, assistant
messages, subagent traffic, injected instructions, and imported mirror rows are
not part of the ledger.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Iterator


SCHEMA_VERSION = "1.0.0"
STORE_NAME = ".prompt-harness"
RECORD_TYPE = "user_prompt"
PROJECT_MARKERS = (
    ".git",
    "AGENTS.md",
    "CLAUDE.md",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
)

DROP_BLOCK_PATTERNS = (
    re.compile(r"<system-reminder>.*?</system-reminder>", re.I | re.S),
    re.compile(r"<local-command-caveat>.*?</local-command-caveat>", re.I | re.S),
    re.compile(r"<local-command-stdout>.*?</local-command-stdout>", re.I | re.S),
    re.compile(r"<task-notification>.*?</task-notification>", re.I | re.S),
)

SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(
        r"(?i)\b(api[_ -]?key|access[_ -]?token|password|passwd|client[_ -]?secret)"
        r"(\s*[:=]\s*)[\"']?([^\s\"']{8,})"
    ),
)

BASE64_PATTERNS = (
    re.compile(r"(data:[\w.+/-]+;base64,)[A-Za-z0-9+/=\r\n]{120,}", re.I),
    re.compile(r"([\"']data[\"']\s*:\s*[\"'])[A-Za-z0-9+/=\r\n]{120,}([\"'])", re.I),
    re.compile(r"(?<![A-Za-z0-9+/=])(?:iVBOR|/9j/)[A-Za-z0-9+/=\r\n]{120,}"),
)

PROJECT_README = """# Prompt Harness project store

This directory is managed by Prompt Harness.

- `events/` is the append-only source of truth for user prompt events.
- `sessions/` contains derived per-session metadata.
- `index/` contains rebuildable catalogs, `PROMPTS.md`, and session views.
- `reports/` contains project-specific narrative analyses and curated exports.
- `visualizations/timeline.html` is a rebuildable, local prompt timeline.
- `state/` contains locks and ingestion state.
- `badcases/` is reserved for the future evaluation harness.

Prompt bodies are private by default. The nested `.gitignore` prevents the
ledger from being committed accidentally. Use the Prompt Harness CLI to search,
rebuild, backfill, or validate the store.
"""

PROJECT_GITIGNORE = """# Prompt bodies may contain private or sensitive context.
config.json
events/
sessions/
index/
reports/
visualizations/
state/
badcases/cases/
badcases/runs/
"""

BADCASE_README = """# Badcase harness (reserved)

The prompt ledger is phase 1. A future badcase record will reference immutable
prompt `event_id` values and add response traces, failure taxonomies, fixtures,
model/run metadata, acceptance tests, and regression status without changing
the v1 prompt-event schema.

Planned layout:

```
badcases/cases/<case-id>/
  case.json
  analysis.md
  fixtures/paths.json
  acceptance.json
  runs/<model>/<run-id>.jsonl
```
"""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_z(value: dt.datetime | None = None) -> str:
    value = (value or utc_now()).astimezone(dt.timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def normalize_path(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("\\\\?\\"):
        text = text[4:]
    try:
        text = str(Path(text).resolve())
    except (OSError, RuntimeError):
        pass
    return os.path.normcase(text.replace("\\", "/").rstrip("/")).lower()


def is_within(value: Any, root: Path) -> bool:
    normalized = normalize_path(value)
    target = normalize_path(root)
    return normalized == target or normalized.startswith(target + "/")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def project_id(root: Path) -> str:
    return "prj_" + sha256_text(normalize_path(root))[:16]


def collapse_blank_lines(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_codex_goal_objective(text: str) -> str | None:
    if not text.lstrip().lower().startswith('<codex_internal_context source="goal">'):
        return None
    match = re.search(r"<objective>\s*(.*?)\s*</objective>", text, re.I | re.S)
    return collapse_blank_lines(match.group(1)) if match else None


def normalize_codex_wrappers(text: str) -> str:
    """Keep human request text and referenced paths, not Codex UI wrappers."""
    references: list[str] = []
    marker = "## My request for Codex:"
    if text.lstrip().startswith("# Files mentioned by the user:") and marker in text:
        header, text = text.split(marker, 1)
        references.extend(
            match.group(1).strip()
            for match in re.finditer(r"^##\s+[^:\n]+:\s*(.+?)\s*$", header, re.M)
        )

    image_pattern = re.compile(
        r"<image\b[^>]*\bpath=([\"'])(.*?)\1[^>]*>\s*</image>",
        re.I | re.S,
    )
    references.extend(match.group(2).strip() for match in image_pattern.finditer(text))
    text = image_pattern.sub("", text)

    unique_references: list[str] = []
    seen: set[str] = set()
    for reference in references:
        key = normalize_path(reference)
        if reference and key not in seen:
            seen.add(key)
            unique_references.append(reference)
    text = collapse_blank_lines(text)
    if unique_references:
        path_block = "Referenced paths:\n" + "\n".join(f"- {path}" for path in unique_references)
        text = f"{text}\n\n{path_block}" if text else path_block
    return text


def redact_secrets(text: str) -> tuple[str, int]:
    count = 0
    for pattern in SECRET_PATTERNS:
        if pattern.groups == 0:
            text, replaced = pattern.subn("[REDACTED_SECRET]", text)
        elif pattern.groups == 1:
            text, replaced = pattern.subn(r"\1[REDACTED_SECRET]", text)
        else:
            text, replaced = pattern.subn(r"\1\2[REDACTED_SECRET]", text)
        count += replaced
    return text, count


def omit_embedded_files(text: str) -> tuple[str, int]:
    count = 0
    for index, pattern in enumerate(BASE64_PATTERNS):
        if index == 0:
            text, replaced = pattern.subn(r"\1[ATTACHMENT_DATA_OMITTED]", text)
        elif index == 1:
            text, replaced = pattern.subn(r"\1[ATTACHMENT_DATA_OMITTED]\2", text)
        else:
            text, replaced = pattern.subn("[ATTACHMENT_DATA_OMITTED]", text)
        count += replaced
    return text, count


def sanitize_prompt(text: str, *, backfill: bool = False) -> tuple[str, dict[str, int]]:
    stats = {"secret_redactions": 0, "attachments_omitted": 0}
    if backfill:
        for pattern in DROP_BLOCK_PATTERNS:
            text = pattern.sub("", text)
        text = extract_codex_goal_objective(text) or text
        text = normalize_codex_wrappers(text)
        text = normalize_slash_command(text)
    text, omitted = omit_embedded_files(text)
    stats["attachments_omitted"] = omitted
    text, redactions = redact_secrets(text)
    stats["secret_redactions"] = redactions
    return collapse_blank_lines(text), stats


def normalize_slash_command(text: str) -> str:
    names = re.findall(r"<command-name>\s*(.*?)\s*</command-name>", text, re.I | re.S)
    args = re.findall(r"<command-args>\s*(.*?)\s*</command-args>", text, re.I | re.S)
    if not names:
        return text
    residue = re.sub(
        r"<command-(?:name|message|args)>.*?</command-(?:name|message|args)>",
        "",
        text,
        flags=re.I | re.S,
    )
    command = names[0].strip()
    argument = args[0].strip() if args else ""
    rendered = " ".join(part for part in (command, argument) if part)
    residue = collapse_blank_lines(residue)
    return f"{rendered}\n\n{residue}" if rendered and residue else rendered or residue


def is_automatic_prompt(text: str) -> bool:
    value = text.strip()
    lowered = value.lower()
    codex_suggestion_prompt = (
        value.startswith("# Overview")
        and "hyperpersonalized suggestions" in lowered
        and "recent codex tasks in this project:" in lowered
    )
    return bool(
        not value
        or re.fullmatch(r"\[Request interrupted by user(?: for tool use)?\]", value, re.I)
        or lowered.startswith("<turn_aborted>")
        or lowered.startswith("<codex_internal_context")
        or lowered.startswith("this session is being continued from a previous conversation")
        or lowered.startswith("caveat: the messages below were generated by the user while running local commands")
        or (value.startswith("# AGENTS.md instructions for ") and "<INSTRUCTIONS>" in value)
        or lowered.startswith("<environment_context>")
        or lowered.startswith("<permissions instructions>")
        or value.startswith("打开 Claude 导入会话归档：")
        or codex_suggestion_prompt
    )


def find_project_root(cwd: Path, explicit: Path | None = None) -> Path:
    if explicit:
        return explicit.expanduser().resolve()
    env_root = os.environ.get("PROMPT_HARNESS_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    current = cwd.expanduser().resolve()
    parents = (current, *current.parents)
    for parent in parents:
        home_catchall = parent == Path.home().resolve() and current != parent
        if (parent / STORE_NAME / "config.json").exists() and not home_catchall:
            return parent
        if (parent / ".git").exists():
            return parent
        if any((parent / marker).exists() for marker in PROJECT_MARKERS[1:]):
            return parent
    return current


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 8.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.monotonic() + timeout
    locked = False
    try:
        while not locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out acquiring lock: {path}")
                time.sleep(0.05)
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def registry_path() -> Path:
    harness_home = os.environ.get("PROMPT_HARNESS_HOME")
    base = Path(harness_home).expanduser() if harness_home else Path.home() / ".prompt-harness"
    return base / "projects.json"


def register_project(root: Path, store: Path) -> None:
    path = registry_path()
    lock = path.with_suffix(".lock")
    with file_lock(lock):
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        else:
            data = {}
        data.setdefault("schema_version", SCHEMA_VERSION)
        projects = data.setdefault("projects", {})
        projects[project_id(root)] = {
            "project_id": project_id(root),
            "name": root.name,
            "root": str(root),
            "store": str(store),
            "last_seen_at": iso_z(),
        }
        write_json(path, data)


def init_store(root: Path) -> tuple[Path, dict[str, Any]]:
    root = root.resolve()
    store = root / STORE_NAME
    for relative in (
        "events",
        "sessions/claude",
        "sessions/codex",
        "index",
        "reports",
        "visualizations",
        "state",
        "badcases/cases",
        "badcases/runs",
    ):
        (store / relative).mkdir(parents=True, exist_ok=True)
    config_path = store / "config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            config = {}
    else:
        config = {}
    if not config:
        config = {
            "schema_version": SCHEMA_VERSION,
            "project_id": project_id(root),
            "project_name": root.name,
            "project_root": str(root),
            "created_at": iso_z(),
            "privacy": {
                "store_prompt_text": True,
                "store_assistant_messages": False,
                "store_file_bodies": False,
                "redact_obvious_secrets": True,
                "git_private_by_default": True,
            },
            "future": {"badcase_schema": "reserved"},
        }
        write_json(config_path, config)
    if not (store / "README.md").exists():
        atomic_write(store / "README.md", PROJECT_README)
    if not (store / ".gitignore").exists():
        atomic_write(store / ".gitignore", PROJECT_GITIGNORE)
    if not (store / "badcases" / "README.md").exists():
        atomic_write(store / "badcases" / "README.md", BADCASE_README)
    register_project(root, store)
    return store, config


def event_file(store: Path, occurred_at: str) -> Path:
    parsed = parse_iso(occurred_at) or utc_now()
    return store / "events" / f"{parsed:%Y}" / f"{parsed:%m}" / f"prompts-{parsed:%Y-%m-%d}.jsonl"


def iter_event_files(store: Path) -> Iterator[Path]:
    events = store / "events"
    if events.exists():
        yield from sorted(events.rglob("prompts-*.jsonl"))


def iter_events(store: Path) -> Iterator[dict[str, Any]]:
    for path in iter_event_files(store):
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    yield value


def event_identity(
    platform: str,
    session_id: str,
    prompt_hash: str,
    occurred_at: str,
    native_event_id: str | None,
    turn_id: str | None,
    source_mode: str,
) -> str:
    stable = native_event_id or turn_id
    if source_mode == "hook" and not stable:
        stable = str(uuid.uuid4())
    stable = stable or occurred_at
    material = "|".join((SCHEMA_VERSION, platform, session_id, stable, prompt_hash))
    return "phe_" + sha256_text(material)[:32]


def build_event(
    *,
    root: Path,
    platform: str,
    source_mode: str,
    prompt_text: str,
    session_id: str,
    occurred_at: str | None = None,
    turn_id: str | None = None,
    transcript_path: str | None = None,
    native_event_id: str | None = None,
    source_path: str | None = None,
    source_line: int | None = None,
    source_refs: list[dict[str, Any]] | None = None,
    alias_session_ids: list[str] | None = None,
    cwd: str | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    sanitation: dict[str, int] | None = None,
) -> dict[str, Any]:
    occurred_at = occurred_at or iso_z()
    prompt_hash = sha256_text(prompt_text)
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": RECORD_TYPE,
        "event_id": event_identity(
            platform,
            session_id,
            prompt_hash,
            occurred_at,
            native_event_id,
            turn_id,
            source_mode,
        ),
        "captured_at": iso_z(),
        "occurred_at": occurred_at,
        "source": {
            "mode": source_mode,
            "platform": platform,
            "path": source_path,
            "line": source_line,
            "native_event_id": native_event_id,
            "refs": source_refs or [],
        },
        "project": {
            "id": project_id(root),
            "name": root.name,
            "root": str(root),
        },
        "session": {
            "id": session_id,
            "alias_ids": sorted(set(alias_session_ids or [])),
            "turn_id": turn_id,
            "transcript_path": transcript_path,
        },
        "prompt": {
            "text": prompt_text,
            "sha256": prompt_hash,
            "chars": len(prompt_text),
            "secret_redactions": int((sanitation or {}).get("secret_redactions", 0)),
            "attachments_omitted": int((sanitation or {}).get("attachments_omitted", 0)),
        },
        "context": {
            "cwd": cwd or str(root),
            "model": model,
            "permission_mode": permission_mode,
        },
        "links": {
            "response_event_id": None,
            "badcase_ids": [],
        },
    }


def update_session_metadata(store: Path, event: dict[str, Any]) -> None:
    platform = event["source"]["platform"]
    session_id = event["session"]["id"] or "unknown"
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id)
    path = store / "sessions" / platform / f"{safe_id}.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}
    data.setdefault("schema_version", SCHEMA_VERSION)
    data["platform"] = platform
    data["session_id"] = session_id
    data["event_count"] = int(data.get("event_count", 0)) + 1
    data["first_occurred_at"] = min(
        filter(None, (data.get("first_occurred_at"), event.get("occurred_at")))
    )
    data["last_occurred_at"] = max(
        filter(None, (data.get("last_occurred_at"), event.get("occurred_at")))
    )
    data["last_event_id"] = event["event_id"]
    data["last_cwd"] = event.get("context", {}).get("cwd")
    data["last_model"] = event.get("context", {}).get("model")
    data["updated_at"] = iso_z()
    write_json(path, data)


def append_event(store: Path, event: dict[str, Any], existing_ids: set[str] | None = None) -> bool:
    path = event_file(store, event["occurred_at"])
    lock = store / "state" / "write.lock"
    with file_lock(lock):
        if existing_ids is not None and event["event_id"] in existing_ids:
            return False
        if path.exists():
            for _, prior in read_jsonl(path):
                if prior.get("event_id") == event["event_id"]:
                    if existing_ids is not None:
                        existing_ids.add(event["event_id"])
                    return False
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        update_session_metadata(store, event)
        if existing_ids is not None:
            existing_ids.add(event["event_id"])
    return True


def text_from_blocks(content: Any, *, include_attachments: bool = True) -> tuple[str, bool]:
    if isinstance(content, str):
        return content, False
    if not isinstance(content, list):
        return "", False
    parts: list[str] = []
    saw_tool = False
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        block_type = block.get("type")
        if block_type == "tool_result":
            saw_tool = True
        elif block_type in {"text", "input_text", "output_text"}:
            parts.append(str(block.get("text", "")))
        elif include_attachments and block_type in {"image", "document", "file"}:
            source = block.get("source") if isinstance(block.get("source"), dict) else {}
            path = block.get("path") or source.get("path")
            suffix = f": {path}" if path else ""
            parts.append(f"[{block_type} attachment omitted{suffix}]")
    return "\n".join(part for part in parts if part), saw_tool


def read_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield line_no, value


def latest_user_from_transcript(path: Path, platform: str) -> str:
    latest = ""
    for _, obj in read_jsonl(path):
        if platform == "claude" and obj.get("type") == "user" and isinstance(obj.get("message"), dict):
            text, saw_tool = text_from_blocks(obj["message"].get("content"))
            if text.strip() and not saw_tool:
                latest = text
        elif platform == "codex" and obj.get("type") == "response_item":
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            if payload.get("type") == "message" and payload.get("role") == "user":
                text, _ = text_from_blocks(payload.get("content"))
                if text.strip():
                    latest = text
    return latest


def prompt_from_hook_payload(payload: dict[str, Any]) -> str:
    for key in ("prompt", "user_prompt", "userPrompt", "prompt_text", "promptText", "input"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        text, _ = text_from_blocks(value)
        if text.strip():
            return text
    message = payload.get("message")
    if isinstance(message, dict):
        text, _ = text_from_blocks(message.get("content"))
        if text.strip():
            return text
    specific = payload.get("hookSpecificInput")
    if isinstance(specific, dict):
        return prompt_from_hook_payload(specific)
    return ""


def record_hook_miss(store: Path, payload: dict[str, Any]) -> None:
    preview: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            cleaned, _ = sanitize_prompt(value)
            preview[key] = cleaned[:500]
        else:
            preview[key] = f"<{type(value).__name__}>"
    entry = {
        "recorded_at": iso_z(),
        "reason": "no user prompt field and transcript fallback was empty",
        "payload_preview": preview,
    }
    path = store / "state" / "hook-misses.jsonl"
    with file_lock(store / "state" / "write.lock"):
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")


def capture_hook(args: argparse.Namespace) -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    platform = args.platform
    if platform == "auto":
        platform = "codex" if payload.get("turn_id") or payload.get("model") else "claude"
    cwd = Path(str(payload.get("cwd") or os.getcwd()))
    root = find_project_root(cwd, args.project)
    store, _ = init_store(root)
    prompt = prompt_from_hook_payload(payload)
    transcript_path = payload.get("transcript_path")
    if not prompt.strip() and transcript_path and Path(str(transcript_path)).exists():
        prompt = latest_user_from_transcript(Path(str(transcript_path)), platform)
    if not prompt.strip():
        record_hook_miss(store, payload)
        return 0
    prompt, sanitation = sanitize_prompt(prompt)
    if not prompt or is_automatic_prompt(prompt):
        return 0
    session_id = str(payload.get("session_id") or "unknown")
    occurred_at = str(payload.get("timestamp") or iso_z())
    event = build_event(
        root=root,
        platform=platform,
        source_mode="hook",
        prompt_text=prompt,
        session_id=session_id,
        occurred_at=occurred_at,
        turn_id=str(payload.get("turn_id")) if payload.get("turn_id") else None,
        transcript_path=str(transcript_path) if transcript_path else None,
        cwd=str(cwd),
        model=str(payload.get("model")) if payload.get("model") else None,
        permission_mode=str(payload.get("permission_mode")) if payload.get("permission_mode") else None,
        sanitation=sanitation,
    )
    append_event(store, event)
    return 0


def claude_project_dir(claude_home: Path, root: Path) -> Path | None:
    projects = claude_home / "projects"
    if not projects.exists():
        return None
    sanitized = re.sub(r"[^A-Za-z0-9]", "-", str(root))
    direct = projects / sanitized
    if direct.exists():
        return direct
    normalized = normalize_path(root)
    best_folder: Path | None = None
    best_matches = 0
    for folder in projects.iterdir():
        if not folder.is_dir():
            continue
        matches = 0
        for sample in folder.glob("*.jsonl"):
            for _, obj in read_jsonl(sample):
                cwd = obj.get("cwd")
                if cwd:
                    if normalize_path(cwd) == normalized:
                        matches += 1
                    break
        if matches > best_matches:
            best_folder = folder
            best_matches = matches
    return best_folder


def collect_claude_candidates(claude_home: Path, root: Path) -> list[dict[str, Any]]:
    folder = claude_project_dir(claude_home, root)
    if not folder:
        return []
    raw: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.jsonl")):
        session_id = path.stem
        rows = list(read_jsonl(path))
        models = source_models_by_line(path, "claude", rows=rows)
        for line_no, obj in rows:
            if obj.get("type") != "user" or not isinstance(obj.get("message"), dict):
                continue
            if obj.get("isSidechain") or obj.get("isMeta"):
                continue
            text, saw_tool = text_from_blocks(obj["message"].get("content"))
            if saw_tool and not text.strip():
                continue
            text, sanitation = sanitize_prompt(text, backfill=True)
            if is_automatic_prompt(text):
                continue
            occurred = parse_iso(obj.get("timestamp"))
            raw.append(
                {
                    "platform": "claude",
                    "session_id": session_id,
                    "timestamp": iso_z(occurred) if occurred else iso_z(),
                    "text": text,
                    "sanitation": sanitation,
                    "native_event_id": str(obj.get("uuid") or obj.get("promptId") or "") or None,
                    "path": str(path),
                    "line": line_no,
                    "cwd": str(obj.get("cwd") or root),
                    "model": models.get(line_no),
                }
            )
    return merge_branch_copies(raw)


def max_jsonl_timestamp(path: Path | None) -> dt.datetime | None:
    if not path or not path.exists():
        return None
    maximum = None
    for _, obj in read_jsonl(path):
        timestamp = parse_iso(obj.get("timestamp"))
        if timestamp and (maximum is None or timestamp > maximum):
            maximum = timestamp
    return maximum


def collect_codex_candidates(codex_home: Path, root: Path) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    seen_goal_objectives: set[tuple[str, str]] = set()
    paths: set[Path] = set()
    for folder_name in ("sessions", "archived_sessions"):
        folder = codex_home / folder_name
        if folder.exists():
            paths.update(path for path in folder.rglob("rollout-*.jsonl") if "subagents" not in path.parts)
    for path in sorted(paths):
        rows = list(read_jsonl(path))
        models = source_models_by_line(path, "codex", rows=rows)
        meta = next(
            (
                obj.get("payload")
                for _, obj in rows[:16]
                if obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict)
            ),
            None,
        )
        if not meta or not is_within(meta.get("cwd"), root):
            continue
        if meta.get("thread_source") == "subagent" or isinstance(meta.get("source"), dict):
            continue
        session_id = str(meta.get("id") or meta.get("session_id") or path.stem)
        imported = str(meta.get("external_agent_source") or "") == "claude"
        external_path = Path(str(meta.get("external_agent_source_path"))) if meta.get("external_agent_source_path") else None
        original_max = max_jsonl_timestamp(external_path) if imported else None
        for line_no, obj in rows:
            if obj.get("type") != "response_item" or not isinstance(obj.get("payload"), dict):
                continue
            payload = obj["payload"]
            if payload.get("type") != "message" or payload.get("role") != "user":
                continue
            timestamp = parse_iso(obj.get("timestamp"))
            if imported and original_max and timestamp and timestamp <= original_max:
                continue
            text, _ = text_from_blocks(payload.get("content"))
            goal_objective = extract_codex_goal_objective(text)
            if goal_objective:
                goal_key = (session_id, sha256_text(goal_objective))
                if goal_key in seen_goal_objectives:
                    continue
                seen_goal_objectives.add(goal_key)
                text = goal_objective
            text, sanitation = sanitize_prompt(text, backfill=True)
            if is_automatic_prompt(text):
                continue
            native_event_id = str(payload.get("id") or obj.get("id") or "") or None
            raw.append(
                {
                    "platform": "codex",
                    "session_id": session_id,
                    "timestamp": iso_z(timestamp) if timestamp else iso_z(),
                    "text": text,
                    "sanitation": sanitation,
                    "native_event_id": native_event_id,
                    "turn_id": str(payload.get("turn_id") or "") or None,
                    "path": str(path),
                    "line": line_no,
                    "cwd": str(meta.get("cwd") or root),
                    "model": models.get(line_no) or str(meta.get("model") or "") or None,
                }
            )
    return merge_branch_copies(raw)


def merge_branch_copies(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for item in items:
        prompt_hash = sha256_text(item["text"])
        native = item.get("native_event_id")
        key = f"time:{item['timestamp']}:{prompt_hash}" if item.get("timestamp") else f"native:{native}"
        if key not in unique:
            merged = dict(item)
            merged["source_refs"] = []
            merged["alias_session_ids"] = []
            unique[key] = merged
        merged = unique[key]
        merged["source_refs"].append(
            {
                "session_id": item["session_id"],
                "path": item["path"],
                "line": item["line"],
                "native_event_id": item.get("native_event_id"),
            }
        )
        merged["alias_session_ids"].append(item["session_id"])
    return sorted(unique.values(), key=lambda item: (item["timestamp"], item["platform"], item["session_id"]))


def backfill(args: argparse.Namespace) -> int:
    root = find_project_root(Path(args.project or os.getcwd()), Path(args.project) if args.project else None)
    store, _ = init_store(root)
    existing = list(iter_events(store))
    existing_ids = {str(event.get("event_id")) for event in existing}
    existing_counts = collections.Counter(
        (
            event.get("source", {}).get("platform"),
            event.get("session", {}).get("id"),
            event.get("prompt", {}).get("sha256"),
        )
        for event in existing
        if event.get("record_type") == RECORD_TYPE
    )
    candidates: list[dict[str, Any]] = []
    if args.platform in {"all", "claude"}:
        candidates.extend(collect_claude_candidates(Path(args.claude_home), root))
    if args.platform in {"all", "codex"}:
        candidates.extend(collect_codex_candidates(Path(args.codex_home), root))
    candidates.sort(key=lambda item: (item["timestamp"], item["platform"], item["session_id"]))
    source_counts: collections.Counter = collections.Counter()
    added = skipped = 0
    for item in candidates:
        prompt_hash = sha256_text(item["text"])
        count_key = (item["platform"], item["session_id"], prompt_hash)
        source_counts[count_key] += 1
        if source_counts[count_key] <= existing_counts[count_key]:
            skipped += 1
            continue
        event = build_event(
            root=root,
            platform=item["platform"],
            source_mode="backfill",
            prompt_text=item["text"],
            session_id=item["session_id"],
            occurred_at=item["timestamp"],
            turn_id=item.get("turn_id"),
            native_event_id=item.get("native_event_id"),
            source_path=item.get("path"),
            source_line=item.get("line"),
            source_refs=item.get("source_refs"),
            alias_session_ids=item.get("alias_session_ids"),
            cwd=item.get("cwd"),
            model=item.get("model"),
            sanitation=item.get("sanitation"),
        )
        if append_event(store, event, existing_ids):
            added += 1
            existing_counts[count_key] += 1
        else:
            skipped += 1
    if args.rebuild_index:
        rebuild_index_for_store(store)
    print(json.dumps({"project": str(root), "candidates": len(candidates), "added": added, "skipped": skipped}, ensure_ascii=False))
    return 0


def max_backtick_run(text: str) -> int:
    return max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)


def fenced(text: str) -> str:
    mark = "`" * max(3, max_backtick_run(text) + 1)
    return f"{mark}text\n{text}\n{mark}"


def source_models_by_line(
    path: Path,
    platform: str,
    *,
    rows: list[tuple[int, dict[str, Any]]] | None = None,
) -> dict[int, str]:
    """Resolve the serving model without changing canonical prompt events."""
    if not path.exists():
        return {}
    rows = rows if rows is not None else list(read_jsonl(path))
    resolved: dict[int, str] = {}
    if platform == "claude":
        next_model: str | None = None
        for line_no, obj in reversed(rows):
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            candidate = normalize_model(message.get("model"))
            if obj.get("type") == "assistant" and candidate:
                next_model = candidate
            elif obj.get("type") == "user" and next_model:
                resolved[line_no] = next_model
        return resolved

    active_model: str | None = None
    for line_no, obj in rows:
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if obj.get("type") == "session_meta" and normalize_model(payload.get("model")):
            active_model = normalize_model(payload.get("model"))
        elif obj.get("type") == "turn_context" and normalize_model(payload.get("model")):
            active_model = normalize_model(payload.get("model"))
        if active_model:
            resolved[line_no] = active_model
    return resolved


def normalize_model(value: Any) -> str | None:
    model = str(value or "").strip()
    if not model or model.startswith("<") or model.lower() in {"unknown", "synthetic", "none", "null"}:
        return None
    return model


def title_from_prompt(text: str, limit: int = 88) -> str:
    for raw in text.splitlines():
        line = re.sub(r"^[#>*\-\d.\s]+", "", raw).strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
    return "Untitled session"


def title_from_prompts(prompts: list[str]) -> str:
    fallback = "Untitled session"
    for index, text in enumerate(prompts):
        title = title_from_prompt(text)
        if index == 0:
            fallback = title
        if not text.lstrip().startswith("/"):
            return title
    return fallback


def build_derived_views(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_cache: dict[tuple[str, str], dict[int, str]] = {}
    views: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for index, event in enumerate(events, 1):
        source = event.get("source", {})
        context = event.get("context", {})
        session = event.get("session", {})
        platform = str(source.get("platform") or "unknown").lower()
        model = normalize_model(context.get("model")) or ""
        model_source = ("captured" if source.get("mode") == "hook" else "derived") if model else None
        source_path = str(source.get("path") or "")
        source_line = source.get("line")
        if not model and source_path and source_line:
            cache_key = (platform, source_path)
            if cache_key not in source_cache:
                source_cache[cache_key] = source_models_by_line(Path(source_path), platform)
            model = source_cache[cache_key].get(int(source_line), "")
            model_source = "derived" if model else None
        session_id = str(session.get("id") or "unknown")
        session_key = f"{platform}:{session_id}"
        view = {
            "number": index,
            "event_id": event.get("event_id"),
            "occurred_at": event.get("occurred_at"),
            "platform": platform,
            "source_mode": source.get("mode"),
            "session_id": session_id,
            "session_key": session_key,
            "turn_id": session.get("turn_id"),
            "model": model or None,
            "model_source": model_source,
            "permission_mode": context.get("permission_mode"),
            "chars": event.get("prompt", {}).get("chars"),
            "sha256": event.get("prompt", {}).get("sha256"),
            "prompt": str(event.get("prompt", {}).get("text") or ""),
        }
        views.append(view)
        grouped[session_key].append(view)

    sessions: list[dict[str, Any]] = []
    for key, items in grouped.items():
        models = sorted({str(item["model"]) for item in items if item.get("model")})
        sessions.append(
            {
                "session_key": key,
                "session_id": items[0]["session_id"],
                "platform": items[0]["platform"],
                "title": title_from_prompts([str(item["prompt"]) for item in items]),
                "prompt_count": len(items),
                "first_occurred_at": items[0]["occurred_at"],
                "last_occurred_at": items[-1]["occurred_at"],
                "models": models,
                "event_ids": [item["event_id"] for item in items],
            }
        )
    sessions.sort(key=lambda item: (item["first_occurred_at"] or "", item["session_key"]))
    return views, sessions


def render_timeline(store: Path, payload: dict[str, Any]) -> None:
    template_path = Path(__file__).resolve().parents[1] / "assets" / "timeline.html"
    template = template_path.read_text(encoding="utf-8")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    atomic_write(store / "visualizations" / "timeline.html", template.replace("__PROMPT_HARNESS_DATA__", encoded))


def rebuild_session_metadata_for_store(store: Path, events: list[dict[str, Any]]) -> None:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for event in events:
        platform = str(event.get("source", {}).get("platform") or "unknown")
        session_id = str(event.get("session", {}).get("id") or "unknown")
        grouped[(platform, session_id)].append(event)
    for (platform, session_id), items in grouped.items():
        items.sort(key=lambda event: (event.get("occurred_at") or "", event.get("event_id") or ""))
        first, last = items[0], items[-1]
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id)
        write_json(
            store / "sessions" / platform / f"{safe_id}.json",
            {
                "schema_version": SCHEMA_VERSION,
                "platform": platform,
                "session_id": session_id,
                "event_count": len(items),
                "first_occurred_at": first.get("occurred_at"),
                "last_occurred_at": last.get("occurred_at"),
                "last_event_id": last.get("event_id"),
                "last_cwd": last.get("context", {}).get("cwd"),
                "last_model": last.get("context", {}).get("model"),
                "updated_at": iso_z(),
            },
        )


def rebuild_index_for_store(store: Path) -> dict[str, Any]:
    events = sorted(iter_events(store), key=lambda event: (event.get("occurred_at") or "", event.get("event_id") or ""))
    rebuild_session_metadata_for_store(store, events)
    event_views, sessions = build_derived_views(events)
    by_platform = collections.Counter(event.get("source", {}).get("platform") for event in events)
    by_session = collections.Counter(
        f"{event.get('source', {}).get('platform')}:{event.get('session', {}).get('id')}" for event in events
    )
    redactions = sum(int(event.get("prompt", {}).get("secret_redactions", 0)) for event in events)
    omissions = sum(int(event.get("prompt", {}).get("attachments_omitted", 0)) for event in events)
    catalog = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_z(),
        "event_count": len(events),
        "platform_counts": dict(sorted(by_platform.items())),
        "session_counts": dict(sorted(by_session.items())),
        "secret_redactions": redactions,
        "attachments_omitted": omissions,
        "first_occurred_at": events[0].get("occurred_at") if events else None,
        "last_occurred_at": events[-1].get("occurred_at") if events else None,
    }
    write_json(store / "index" / "catalog.json", catalog)
    write_json(store / "index" / "sessions.json", sessions)
    lines = ["# User prompts", ""]
    for view in event_views:
        model = view.get("model") or "unavailable"
        model_note = " (derived from source transcript)" if view.get("model_source") == "derived" else ""
        lines.extend(
            [
                f"## P{view['number']:05d}",
                "",
                f"- Time: `{view.get('occurred_at')}`",
                f"- Platform: `{view.get('platform')}`",
                f"- Model: `{model}`{model_note}",
                f"- Session: `{view.get('session_id')}`",
                f"- Event: `{view.get('event_id')}`",
                f"- Source mode: `{view.get('source_mode')}`",
                "",
                fenced(view["prompt"]),
                "",
            ]
        )
    atomic_write(store / "index" / "PROMPTS.md", "\n".join(lines))
    summary_lines = [
        "# Session summaries",
        "",
        "> Mutable derived view. Titles come only from the first human prompt in each session.",
        "",
    ]
    for session in sessions:
        models = ", ".join(session["models"]) if session["models"] else "unavailable"
        summary_lines.extend(
            [
                f"## {session['title']}",
                "",
                f"- Platform: `{session['platform']}`",
                f"- Model: `{models}`",
                f"- Session: `{session['session_id']}`",
                f"- Time: `{session['first_occurred_at']}` → `{session['last_occurred_at']}`",
                f"- Human prompts: `{session['prompt_count']}`",
                "",
            ]
        )
    atomic_write(store / "reports" / "SESSION_SUMMARIES.md", "\n".join(summary_lines))
    config_path = store / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    render_timeline(
        store,
        {
            "generated_at": catalog["generated_at"],
            "project": {"name": config.get("project_name") or store.parent.name, "root": config.get("project_root")},
            "catalog": catalog,
            "sessions": sessions,
            "events": event_views,
        },
    )
    return catalog


def rebuild_index(args: argparse.Namespace) -> int:
    root = find_project_root(Path(args.project or os.getcwd()), Path(args.project) if args.project else None)
    store, _ = init_store(root)
    print(json.dumps(rebuild_index_for_store(store), ensure_ascii=False, indent=2))
    return 0


def scrub_store_secrets(store: Path) -> dict[str, int]:
    """Repair historical event rows after secret-pattern coverage improves.

    Event IDs stay stable so any future badcase references remain valid. Prompt
    hashes and derived views are rebuilt from the redacted text.
    """
    files_changed = events_changed = redactions = 0
    events_root = store / "events"
    if not events_root.exists():
        return {"files_changed": 0, "events_changed": 0, "secret_redactions": 0}
    for path in sorted(events_root.rglob("*.jsonl")):
        rendered: list[str] = []
        changed = False
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                rendered.append(raw)
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                rendered.append(raw)
                continue
            prompt = event.get("prompt") if isinstance(event.get("prompt"), dict) else None
            if prompt is not None:
                text = str(prompt.get("text") or "")
                redacted, count = redact_secrets(text)
                if count:
                    prompt["text"] = redacted
                    prompt["sha256"] = sha256_text(redacted)
                    prompt["chars"] = len(redacted)
                    prompt["secret_redactions"] = int(prompt.get("secret_redactions", 0)) + count
                    events_changed += 1
                    redactions += count
                    changed = True
            rendered.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        if changed:
            atomic_write(path, "\n".join(rendered) + "\n")
            files_changed += 1
    return {
        "files_changed": files_changed,
        "events_changed": events_changed,
        "secret_redactions": redactions,
    }


def scrub_secrets_command(args: argparse.Namespace) -> int:
    root = find_project_root(Path(args.project or os.getcwd()), Path(args.project) if args.project else None)
    store, _ = init_store(root)
    result = scrub_store_secrets(store)
    rebuild_index_for_store(store)
    print(json.dumps({"project": str(root), **result}, ensure_ascii=False))
    return 0


def clean_store_events(store: Path) -> dict[str, int]:
    """Remove non-human rows and re-apply current prompt normalization rules."""
    files_changed = events_changed = events_dropped = 0
    seen_goal_objectives: set[tuple[str, str, str]] = set()
    events_root = store / "events"
    if not events_root.exists():
        return {"files_changed": 0, "events_changed": 0, "events_dropped": 0}
    for path in sorted(events_root.rglob("*.jsonl")):
        rendered: list[str] = []
        changed = False
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                rendered.append(raw)
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                rendered.append(raw)
                continue
            prompt = event.get("prompt") if isinstance(event.get("prompt"), dict) else None
            if prompt is None:
                rendered.append(raw)
                continue
            text = str(prompt.get("text") or "")
            goal_objective = extract_codex_goal_objective(text)
            if goal_objective:
                goal_key = (
                    str(event.get("source", {}).get("platform") or ""),
                    str(event.get("session", {}).get("id") or ""),
                    sha256_text(goal_objective),
                )
                if goal_key in seen_goal_objectives:
                    events_dropped += 1
                    changed = True
                    continue
                seen_goal_objectives.add(goal_key)
            normalized, sanitation = sanitize_prompt(text, backfill=True)
            if is_automatic_prompt(normalized):
                events_dropped += 1
                changed = True
                continue
            if normalized != text or any(sanitation.values()):
                prompt["text"] = normalized
                prompt["sha256"] = sha256_text(normalized)
                prompt["chars"] = len(normalized)
                prompt["secret_redactions"] = int(prompt.get("secret_redactions", 0)) + sanitation["secret_redactions"]
                prompt["attachments_omitted"] = int(prompt.get("attachments_omitted", 0)) + sanitation["attachments_omitted"]
                events_changed += 1
                changed = True
            rendered.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        if changed:
            atomic_write(path, ("\n".join(rendered) + "\n") if rendered else "")
            files_changed += 1
    return {
        "files_changed": files_changed,
        "events_changed": events_changed,
        "events_dropped": events_dropped,
    }


def clean_store_command(args: argparse.Namespace) -> int:
    root = find_project_root(Path(args.project or os.getcwd()), Path(args.project) if args.project else None)
    store, _ = init_store(root)
    result = clean_store_events(store)
    rebuild_index_for_store(store)
    print(json.dumps({"project": str(root), **result}, ensure_ascii=False))
    return 0


def search(args: argparse.Namespace) -> int:
    root = find_project_root(Path(args.project or os.getcwd()), Path(args.project) if args.project else None)
    store, _ = init_store(root)
    terms = [term.lower() for term in re.findall(r"\S+", args.query)]
    matches: list[tuple[int, dict[str, Any]]] = []
    for event in iter_events(store):
        haystack = " ".join(
            (
                str(event.get("prompt", {}).get("text") or ""),
                str(event.get("session", {}).get("id") or ""),
                str(event.get("source", {}).get("platform") or ""),
                str(event.get("occurred_at") or ""),
            )
        ).lower()
        score = sum(haystack.count(term) for term in terms)
        if score:
            matches.append((score, event))
    matches.sort(key=lambda pair: (-pair[0], pair[1].get("occurred_at") or ""))
    selected = [event for _, event in matches[: args.limit]]
    if args.format == "json":
        print(json.dumps(selected, ensure_ascii=False, indent=2))
    else:
        for event in selected:
            print(f"## {event.get('occurred_at')} · {event.get('source', {}).get('platform')} · {event.get('event_id')}")
            print()
            print(event.get("prompt", {}).get("text") or "")
            print()
    return 0


def doctor_store(store: Path, root: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    ids: set[str] = set()
    count = 0
    for event in iter_events(store):
        count += 1
        for field in ("schema_version", "record_type", "event_id", "captured_at", "occurred_at"):
            if not event.get(field):
                errors.append(f"missing {field} in event #{count}")
        event_id = str(event.get("event_id") or "")
        if event_id in ids:
            errors.append(f"duplicate event_id {event_id}")
        ids.add(event_id)
        text = str(event.get("prompt", {}).get("text") or "")
        digest = str(event.get("prompt", {}).get("sha256") or "")
        if digest != sha256_text(text):
            errors.append(f"prompt hash mismatch for {event_id}")
        if any(pattern.search(text) for pattern in BASE64_PATTERNS):
            errors.append(f"embedded attachment data remains in {event_id}")
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            errors.append(f"obvious secret pattern remains in {event_id}")
        if not is_within(event.get("project", {}).get("root"), root):
            errors.append(f"project root mismatch in {event_id}")
    config_path = store / "config.json"
    if not config_path.exists():
        errors.append("config.json is missing")
    if not (store / ".gitignore").exists():
        warnings.append("nested .gitignore is missing")
    misses = store / "state" / "hook-misses.jsonl"
    if misses.exists():
        miss_count = sum(1 for _ in read_jsonl(misses))
        if miss_count:
            warnings.append(f"{miss_count} hook payloads contained no recoverable user prompt; inspect {misses}")
    return {"ok": not errors, "event_count": count, "errors": errors, "warnings": warnings}


def doctor(args: argparse.Namespace) -> int:
    root = find_project_root(Path(args.project or os.getcwd()), Path(args.project) if args.project else None)
    store, _ = init_store(root)
    result = doctor_store(store, root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def list_projects(_: argparse.Namespace) -> int:
    path = registry_path()
    if not path.exists():
        print(json.dumps({"schema_version": SCHEMA_VERSION, "projects": {}}, ensure_ascii=False, indent=2))
        return 0
    print(path.read_text(encoding="utf-8"))
    return 0


def init_command(args: argparse.Namespace) -> int:
    root = find_project_root(Path(args.project or os.getcwd()), Path(args.project) if args.project else None)
    store, config = init_store(root)
    print(json.dumps({"store": str(store), "config": config}, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Project-local prompt ledger for Codex and Claude Code")
    sub = root.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="initialize .prompt-harness in a project")
    init_parser.add_argument("--project", type=Path)
    init_parser.set_defaults(func=init_command)

    capture = sub.add_parser("capture-hook", help="capture one UserPromptSubmit JSON payload from stdin")
    capture.add_argument("--platform", choices=("auto", "codex", "claude"), default="auto")
    capture.add_argument("--project", type=Path)
    capture.set_defaults(func=capture_hook)

    fill = sub.add_parser("backfill", help="backfill historical Claude/Codex prompts for one project")
    fill.add_argument("--project", type=Path)
    fill.add_argument("--platform", choices=("all", "claude", "codex"), default="all")
    fill.add_argument("--claude-home", type=Path, default=Path.home() / ".claude")
    fill.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    fill.add_argument("--rebuild-index", action="store_true")
    fill.set_defaults(func=backfill)

    rebuild = sub.add_parser("rebuild-index", help="rebuild catalog.json and PROMPTS.md")
    rebuild.add_argument("--project", type=Path)
    rebuild.set_defaults(func=rebuild_index)

    scrub = sub.add_parser("scrub-secrets", help="redact newly recognized secrets from an existing store")
    scrub.add_argument("--project", type=Path)
    scrub.set_defaults(func=scrub_secrets_command)

    clean = sub.add_parser("clean-store", help="remove non-human rows and reapply prompt normalization")
    clean.add_argument("--project", type=Path)
    clean.set_defaults(func=clean_store_command)

    find = sub.add_parser("search", help="search prompt text and metadata")
    find.add_argument("query")
    find.add_argument("--project", type=Path)
    find.add_argument("--limit", type=int, default=20)
    find.add_argument("--format", choices=("json", "md"), default="md")
    find.set_defaults(func=search)

    check = sub.add_parser("doctor", help="validate schema, hashes, privacy, and duplicate IDs")
    check.add_argument("--project", type=Path)
    check.set_defaults(func=doctor)

    projects = sub.add_parser("list-projects", help="list known project prompt stores")
    projects.set_defaults(func=list_projects)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
