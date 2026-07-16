#!/usr/bin/env python3
"""Project-local prompt and agent-trace ledger for Codex and Claude Code.

The canonical store is append-only JSONL under ``<project>/.prompt-harness``.
User-authored prompts, user-sent raster images, and structured agent trace
events are recorded. Trace events include assistant text, reasoning/thinking,
tool calls/results, injected instructions, and subagent traffic.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import collections
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Iterable, Iterator


SCHEMA_VERSION = "1.0.0"
STORE_NAME = ".prompt-harness"
RECORD_TYPE = "user_prompt"
MODEL_OUTPUT_RECORD_TYPE = "agent_trace"
LEGACY_MODEL_OUTPUT_RECORD_TYPE = "model_output"
IMAGE_RECORD_TYPE = "prompt_image"
EXCLUSION_RECORD_TYPE = "event_exclusion"
SESSION_BINDING_RECORD_TYPE = "session_project_binding"
EXACT_ROOT_EXCLUSION_PREFIX = "cwd_outside_exact_project_root_"
MAX_IMAGES_PER_EVENT = 20
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 50 * 1024 * 1024
MAX_AUTO_SYNC_SESSION_KEYS = 200
MAX_PENDING_SYNC_PASSES = 8
IMAGE_MEDIA = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}
IMAGE_SUFFIXES = set(IMAGE_MEDIA.values()) | {".jpeg"}
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
        r"(\s*[:=]\s*)[\"']?(?!\[REDACTED_SECRET\])([^\s\"']{8,})"
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
- `model-events/` is the append-only source of truth for structured agent trace events.
- `sessions/` contains derived per-session metadata.
- `index/` contains rebuildable catalogs, `PROMPTS.md`, `MODELOUT.md`, `TRAJECTORY.md`, and session views.
- `index/prompt/`, `index/modelout/`, and `index/trajectory/` contain matching per-session Markdown files.
- `reports/` contains project-specific narrative analyses and curated exports.
- `assets/images/` contains content-addressed copies of user-sent raster images.
- `assets/manifest.jsonl` links those images to immutable prompt `event_id` values.
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
model-events/
sessions/
index/
reports/
assets/
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
    codex_plugin_environment_injection = (
        lowered.startswith("<recommended_plugins>")
        and "</recommended_plugins>" in lowered
        and "<environment_context>" in lowered
    )
    return bool(
        not value
        or re.fullmatch(r"\[Request interrupted by user(?: for tool use)?\]", value, re.I)
        or lowered.startswith("<turn_aborted>")
        or lowered.startswith("<codex_internal_context")
        or lowered.startswith("this session is being continued from a previous conversation")
        or lowered.startswith("caveat: the messages below were generated by the user while running local commands")
        or (
            value.startswith("# AGENTS.md instructions")
            and "<INSTRUCTIONS>" in value
            and "<environment_context>" in value
        )
        or lowered.startswith("<environment_context>")
        or lowered.startswith("<permissions instructions>")
        or codex_plugin_environment_injection
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


def is_unsafe_broad_project_root(root: Path) -> bool:
    """Reject filesystem roots while allowing exact-cwd user-home ledgers."""
    resolved = root.expanduser().resolve()
    return resolved == Path(resolved.anchor)


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


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def index_dirty_file(store: Path) -> Path:
    return store / "state" / "index-dirty.json"


def mark_index_dirty(store: Path, reason: str) -> None:
    path = index_dirty_file(store)
    state = read_json_object(path)
    if state.get("dirty"):
        return
    write_json(
        path,
        {
            "schema_version": "1.0.0",
            "dirty": True,
            "reason": reason,
            "updated_at": iso_z(),
        },
    )


def clear_index_dirty(store: Path) -> None:
    write_json(
        index_dirty_file(store),
        {
            "schema_version": "1.0.0",
            "dirty": False,
            "reason": None,
            "updated_at": iso_z(),
        },
    )


def index_is_dirty(store: Path) -> bool:
    state = read_json_object(index_dirty_file(store))
    return bool(state.get("dirty")) or not (store / "index" / "catalog.json").exists()


def harness_home() -> Path:
    harness_home = os.environ.get("PROMPT_HARNESS_HOME")
    return Path(harness_home).expanduser() if harness_home else Path.home() / ".prompt-harness"


def record_hook_runtime_error(payload: dict[str, Any], error: Exception) -> None:
    """Persist hook diagnostics without copying the submitted prompt body."""

    entry = {
        "timestamp": iso_z(),
        "component": "capture_hook",
        "error_type": type(error).__name__,
        "message": str(error)[:1000],
        "session_id": payload.get("session_id"),
        "turn_id": payload.get("turn_id"),
        "hook_event_name": payload.get("hook_event_name"),
        "cwd": payload.get("cwd"),
        "transcript_path": payload.get("transcript_path"),
    }
    with contextlib.suppress(Exception):
        path = harness_home() / "state" / "hook-errors.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path.with_suffix(".lock")):
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")


def registry_path() -> Path:
    return harness_home() / "projects.json"


def native_agent_home(environment_variable: str, default_name: str) -> Path:
    configured = os.environ.get(environment_variable)
    return Path(configured).expanduser() if configured else Path.home() / default_name


def session_binding_file() -> Path:
    return harness_home() / "session-bindings.jsonl"


def session_binding_key(platform: str, session_id: str) -> str:
    return f"{platform.strip().lower()}:{session_id.strip()}"


def iter_session_bindings() -> Iterator[dict[str, Any]]:
    path = session_binding_file()
    if not path.exists():
        return
    for _, value in read_jsonl(path):
        if value.get("record_type") == SESSION_BINDING_RECORD_TYPE:
            yield value


def active_session_bindings() -> dict[str, dict[str, Any]]:
    active: dict[str, dict[str, Any]] = {}
    for record in iter_session_bindings():
        platform = str(record.get("platform") or "").lower()
        session_id = str(record.get("session_id") or "")
        if platform and session_id and record.get("project_root"):
            active[session_binding_key(platform, session_id)] = record
    return active


def session_binding(platform: str, session_id: str) -> dict[str, Any] | None:
    if not platform or not session_id or session_id == "unknown":
        return None
    return active_session_bindings().get(session_binding_key(platform, session_id))


def bound_project_root(platform: str, session_id: str) -> Path | None:
    binding = session_binding(platform, session_id)
    if not binding:
        return None
    try:
        root = Path(str(binding["project_root"])).expanduser().resolve()
    except (KeyError, OSError, RuntimeError):
        return None
    return None if is_unsafe_broad_project_root(root) else root


def session_belongs_to_project_root(
    *,
    platform: str,
    session_id: str,
    cwd: Any,
    root: Path,
    bindings: dict[str, dict[str, Any]] | None = None,
) -> bool:
    """Route a session only to its exact launch root unless explicitly bound.

    Active bindings are authoritative. In their absence, parent/child path
    containment is intentionally insufficient: the normalized session cwd must
    equal the normalized project root.
    """

    binding_map = bindings if bindings is not None else active_session_bindings()
    binding = binding_map.get(session_binding_key(platform, session_id))
    if binding:
        return normalize_path(binding.get("project_root")) == normalize_path(root)
    return bool(cwd) and normalize_path(cwd) == normalize_path(root)


def bound_source_paths(platform: str, root: Path) -> list[Path]:
    target = normalize_path(root)
    paths: list[Path] = []
    for binding in active_session_bindings().values():
        if str(binding.get("platform") or "").lower() != platform:
            continue
        if normalize_path(binding.get("project_root")) != target or not binding.get("source_path"):
            continue
        path = Path(str(binding["source_path"])).expanduser()
        if path.is_file():
            paths.append(path)
    return paths


def append_session_binding(
    *,
    platform: str,
    session_id: str,
    project_root: Path,
    source_path: Path | None = None,
    reason: str = "explicit",
) -> tuple[dict[str, Any], bool]:
    platform = platform.strip().lower()
    session_id = session_id.strip()
    root = project_root.expanduser().resolve()
    if platform not in {"claude", "codex"}:
        raise ValueError(f"Unsupported platform: {platform}")
    if not session_id or session_id == "unknown":
        raise ValueError("A native session ID is required")
    if is_unsafe_broad_project_root(root):
        raise ValueError(f"Refusing broad project root: {root}")
    path = session_binding_file()
    lock = path.with_suffix(".lock")
    with file_lock(lock):
        prior = session_binding(platform, session_id)
        normalized_source = str(source_path.expanduser().resolve()) if source_path else None
        if (
            prior
            and normalize_path(prior.get("project_root")) == normalize_path(root)
            and normalize_path(prior.get("source_path")) == normalize_path(normalized_source)
        ):
            return prior, False
        recorded_at = iso_z()
        material = "|".join(
            (platform, session_id, normalize_path(root), normalize_path(normalized_source), recorded_at)
        )
        record = {
            "schema_version": "1.0.0",
            "record_type": SESSION_BINDING_RECORD_TYPE,
            "binding_id": "phb_" + sha256_text(material)[:32],
            "platform": platform,
            "session_id": session_id,
            "project_id": project_id(root),
            "project_root": str(root),
            "source_path": normalized_source,
            "reason": reason,
            "replaces_binding_id": prior.get("binding_id") if prior else None,
            "recorded_at": recorded_at,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return record, True


def resolve_project_root(
    cwd: Path,
    *,
    explicit: Path | None,
    platform: str,
    session_id: str,
) -> Path:
    if explicit or os.environ.get("PROMPT_HARNESS_PROJECT_ROOT"):
        return find_project_root(cwd, explicit)
    return bound_project_root(platform, session_id) or find_project_root(cwd)


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
    if is_unsafe_broad_project_root(root):
        raise ValueError(f"Refusing broad project root: {root}")
    store = root / STORE_NAME
    for relative in (
        "events",
        "model-events",
        "sessions/claude",
        "sessions/codex",
        "index",
        "reports",
        "assets/images",
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
    changed_config = False
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
                "store_model_outputs": True,
                "store_agent_trace": True,
                "store_file_bodies": False,
                "store_user_images": True,
                "redact_obvious_secrets": True,
                "git_private_by_default": True,
            },
            "future": {"badcase_schema": "reserved"},
            "auto_sync": {
                "enabled": True,
                "platform": "all",
                "background": True,
                "strategy": "incremental",
                "full_scan": "first_use_or_force",
                "rebuild_index": True,
            },
        }
        changed_config = True
    else:
        privacy = config.setdefault("privacy", {})
        if "store_model_outputs" not in privacy:
            privacy["store_model_outputs"] = True
            changed_config = True
        if "store_agent_trace" not in privacy:
            privacy["store_agent_trace"] = bool(privacy.get("store_model_outputs", True))
            changed_config = True
        if "store_user_images" not in privacy:
            privacy["store_user_images"] = True
            changed_config = True
        auto_sync = config.setdefault("auto_sync", {})
        auto_sync_defaults = {
            "enabled": True,
            "platform": "all",
            "background": True,
            "strategy": "incremental",
            "full_scan": "first_use_or_force",
            "rebuild_index": True,
        }
        if "min_interval_seconds" in auto_sync:
            del auto_sync["min_interval_seconds"]
            changed_config = True
        for key, value in auto_sync_defaults.items():
            if key not in auto_sync:
                auto_sync[key] = value
                changed_config = True
    if changed_config:
        write_json(config_path, config)
    if not (store / "README.md").exists():
        atomic_write(store / "README.md", PROJECT_README)
    gitignore_path = store / ".gitignore"
    if not gitignore_path.exists():
        atomic_write(gitignore_path, PROJECT_GITIGNORE)
    else:
        gitignore_text = gitignore_path.read_text(encoding="utf-8", errors="replace")
        if not re.search(r"(?m)^assets/?$", gitignore_text):
            atomic_write(gitignore_path, gitignore_text.rstrip() + "\nassets/\n")
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


def model_output_file(store: Path, occurred_at: str) -> Path:
    parsed = parse_iso(occurred_at) or utc_now()
    return (
        store
        / "model-events"
        / f"{parsed:%Y}"
        / f"{parsed:%m}"
        / f"model-outputs-{parsed:%Y-%m-%d}.jsonl"
    )


def iter_model_output_files(store: Path) -> Iterator[Path]:
    root = store / "model-events"
    if root.exists():
        yield from sorted(root.rglob("model-outputs-*.jsonl"))


def iter_model_outputs(store: Path) -> Iterator[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for path in iter_model_output_files(store):
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict) and value.get("record_type") in {
                    MODEL_OUTPUT_RECORD_TYPE,
                    LEGACY_MODEL_OUTPUT_RECORD_TYPE,
                }:
                    values.append(value)
    upgraded_sources = {
        (
            str(value.get("source", {}).get("platform") or ""),
            str(value.get("session", {}).get("id") or ""),
            normalize_path(value.get("source", {}).get("path")),
            int(value.get("source", {}).get("line") or 0),
        )
        for value in values
        if value.get("record_type") == MODEL_OUTPUT_RECORD_TYPE
        and str(value.get("event_type") or "") == "assistant_text"
    }
    for value in values:
        if value.get("record_type") == LEGACY_MODEL_OUTPUT_RECORD_TYPE:
            source_key = (
                str(value.get("source", {}).get("platform") or ""),
                str(value.get("session", {}).get("id") or ""),
                normalize_path(value.get("source", {}).get("path")),
                int(value.get("source", {}).get("line") or 0),
            )
            if source_key in upgraded_sources:
                continue
        yield value


def event_supersession_file(store: Path) -> Path:
    return store / "state" / "event-supersessions.jsonl"


def iter_event_supersessions(store: Path) -> Iterator[dict[str, Any]]:
    path = event_supersession_file(store)
    if path.exists():
        for _, value in read_jsonl(path):
            if value.get("record_type") == "event_supersession":
                yield value


def superseded_event_ids(store: Path) -> set[str]:
    return {str(item.get("event_id") or "") for item in iter_event_supersessions(store)}


def event_exclusion_file(store: Path) -> Path:
    return store / "state" / "event-exclusions.jsonl"


def iter_event_exclusions(store: Path) -> Iterator[dict[str, Any]]:
    path = event_exclusion_file(store)
    if path.exists():
        for _, value in read_jsonl(path):
            if value.get("record_type") == EXCLUSION_RECORD_TYPE:
                yield value


def excluded_event_ids(store: Path) -> set[str]:
    events_by_id: dict[str, dict[str, Any]] | None = None
    excluded: set[str] = set()
    for item in iter_event_exclusions(store):
        event_id = str(item.get("event_id") or "")
        reason = str(item.get("reason") or "")
        reassignment = re.fullmatch(r"session_reassigned_to_(prj_[0-9a-f]+)", reason)
        if reassignment:
            if events_by_id is None:
                events_by_id = {str(event.get("event_id") or ""): event for event in iter_events(store)}
            event = events_by_id.get(event_id)
            if not event:
                excluded.add(event_id)
                continue
            platform = str(event.get("source", {}).get("platform") or "")
            session_id = str(event.get("session", {}).get("id") or "")
            binding = session_binding(platform, session_id)
            if binding and str(binding.get("project_id") or "") == reassignment.group(1):
                excluded.add(event_id)
            continue
        exact_root_scope = re.fullmatch(
            rf"{re.escape(EXACT_ROOT_EXCLUSION_PREFIX)}(prj_[0-9a-f]+)",
            reason,
        )
        if exact_root_scope:
            if events_by_id is None:
                events_by_id = {str(event.get("event_id") or ""): event for event in iter_events(store)}
            event = events_by_id.get(event_id)
            if not event:
                excluded.add(event_id)
                continue
            platform = str(event.get("source", {}).get("platform") or "")
            session_id = str(event.get("session", {}).get("id") or "")
            binding = session_binding(platform, session_id)
            if binding and str(binding.get("project_id") or "") == exact_root_scope.group(1):
                continue
            excluded.add(event_id)
            continue
        excluded.add(event_id)
    return excluded


def iter_active_events(store: Path) -> Iterator[dict[str, Any]]:
    inactive = superseded_event_ids(store) | excluded_event_ids(store)
    for event in iter_events(store):
        if str(event.get("event_id") or "") not in inactive:
            yield event


def event_order_key(event: dict[str, Any]) -> tuple[Any, ...]:
    """Order active prompts chronologically, with deterministic transcript-aware ties."""
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    session = event.get("session") if isinstance(event.get("session"), dict) else {}
    source_line = source.get("line")
    try:
        line_number = int(source_line)
    except (TypeError, ValueError):
        line_number = 2**63 - 1
    source_path = str(source.get("path") or session.get("transcript_path") or "")
    return (
        str(event.get("occurred_at") or ""),
        source_path.lower(),
        line_number,
        str(source.get("platform") or ""),
        str(session.get("id") or ""),
        str(source.get("native_event_id") or session.get("turn_id") or ""),
        str(event.get("event_id") or ""),
    )


def model_output_order_key(event: dict[str, Any]) -> tuple[Any, ...]:
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    session = event.get("session") if isinstance(event.get("session"), dict) else {}
    try:
        line_number = int(source.get("line"))
    except (TypeError, ValueError):
        line_number = 2**63 - 1
    return (
        str(event.get("occurred_at") or ""),
        str(source.get("path") or session.get("transcript_path") or "").lower(),
        line_number,
        str(source.get("platform") or ""),
        str(session.get("id") or ""),
        int(source.get("block_index") or 0),
        str(event.get("trace_event_id") or event.get("model_output_id") or ""),
    )


def trajectory_item_order_key(item: tuple[str, dict[str, Any]]) -> tuple[Any, ...]:
    kind, event = item
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    session = event.get("session") if isinstance(event.get("session"), dict) else {}
    try:
        line_number = int(source.get("line"))
    except (TypeError, ValueError):
        line_number = 2**63 - 1
    try:
        block_index = int(source.get("block_index") or 0)
    except (TypeError, ValueError):
        block_index = 0
    return (
        str(event.get("occurred_at") or ""),
        str(source.get("path") or session.get("transcript_path") or "").lower(),
        line_number,
        0 if kind == "prompt" else 1,
        block_index,
        str(event.get("event_id") or event.get("trace_event_id") or ""),
    )


def render_trajectory_event(
    label: str,
    kind: str,
    event: dict[str, Any],
    prompt_numbers: dict[str, int],
    *,
    heading_level: int = 4,
) -> list[str]:
    heading = "#" * max(1, min(6, heading_level))
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    session = event.get("session") if isinstance(event.get("session"), dict) else {}
    context = event.get("context") if isinstance(event.get("context"), dict) else {}
    if kind == "prompt":
        prompt = event.get("prompt") if isinstance(event.get("prompt"), dict) else {}
        return [
            f"{heading} {label} · PROMPT",
            "",
            f"- Time: `{event.get('occurred_at')}`",
            f"- Turn: `{session.get('turn_id') or 'none'}`",
            f"- Prompt event ID: `{event.get('event_id')}`",
            f"- Source: `{source.get('path') or 'hook'}:{source.get('line') or '-'}`",
            f"- Model: `{context.get('model') or 'unavailable'}`",
            "",
            fenced(str(prompt.get("text") or "")),
            "",
        ]
    content = event.get("content")
    if not isinstance(content, dict):
        content = event.get("output", {})
    links = event.get("links") if isinstance(event.get("links"), dict) else {}
    prompt_event_id = str(links.get("prompt_event_id") or "")
    prompt_number = prompt_numbers.get(prompt_event_id)
    prompt_label = f"P{prompt_number:05d}" if prompt_number else "unlinked"
    lines = [
        f"{heading} {label} · {str(event.get('event_type') or 'assistant_text').upper()}",
        "",
        f"- Time: `{event.get('occurred_at')}`",
        f"- Actor: `{event.get('actor', {}).get('role') or 'assistant'}`",
        f"- Turn: `{session.get('turn_id') or 'none'}`",
        f"- Trace event ID: `{event.get('trace_event_id') or event.get('model_output_id')}`",
        f"- Linked prompt: `{prompt_label}` (`{prompt_event_id or 'unlinked'}`)",
        f"- Tool call ID: `{links.get('tool_call_id') or 'none'}`",
        f"- Source: `{source.get('path') or 'unknown'}:{source.get('line') or '-'}#{source.get('block_index') or 0}`",
        f"- Model: `{context.get('model') or 'unavailable'}`",
        "",
        fenced(str(content.get("text") or "")),
        "",
    ]
    if content.get("structured") is not None:
        lines.extend(
            [
                "```json",
                json.dumps(content.get("structured"), ensure_ascii=False, indent=2, sort_keys=True),
                "```",
                "",
            ]
        )
    return lines


def render_conversation_turns(
    items: list[tuple[str, dict[str, Any]]],
    prompt_by_id: dict[str, dict[str, Any]],
    prompt_numbers: dict[str, int],
    *,
    turn_heading_level: int,
    event_heading_level: int,
) -> list[str]:
    """Render native turns with every human message before the model trace."""

    local_prompt_events = [
        event for kind, event in items if kind == "prompt" and event.get("event_id")
    ]
    local_turn_ids = {
        str(event.get("session", {}).get("turn_id") or "")
        for event in local_prompt_events
        if event.get("session", {}).get("turn_id")
    }
    prompts_by_turn: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    traces_by_turn: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    external_prompt_by_turn: dict[str, dict[str, Any]] = {}
    for event in local_prompt_events:
        event_id = str(event.get("event_id") or "")
        native_turn_id = str(event.get("session", {}).get("turn_id") or "")
        key = f"native:{native_turn_id}" if native_turn_id else f"prompt:{event_id}"
        prompts_by_turn[key].append(event)
    unlinked_traces: list[dict[str, Any]] = []
    for kind, event in items:
        if kind != "trace":
            continue
        prompt_event_id = str(event.get("links", {}).get("prompt_event_id") or "")
        native_turn_id = str(event.get("session", {}).get("turn_id") or "")
        if native_turn_id and native_turn_id in local_turn_ids:
            traces_by_turn[f"native:{native_turn_id}"].append(event)
        elif prompt_event_id and prompt_event_id in prompt_by_id:
            linked_prompt = prompt_by_id[prompt_event_id]
            linked_turn_id = str(linked_prompt.get("session", {}).get("turn_id") or "")
            if linked_turn_id and linked_turn_id in local_turn_ids:
                traces_by_turn[f"native:{linked_turn_id}"].append(event)
            else:
                key = f"prompt:{prompt_event_id}"
                traces_by_turn[key].append(event)
                external_prompt_by_turn[key] = linked_prompt
        else:
            unlinked_traces.append(event)

    turn_keys = set(prompts_by_turn) | set(traces_by_turn)

    def turn_order_key(key: str) -> tuple[Any, ...]:
        prompt_items = prompts_by_turn.get(key)
        if prompt_items:
            return event_order_key(min(prompt_items, key=event_order_key))
        if key in external_prompt_by_turn:
            return event_order_key(external_prompt_by_turn[key])
        trace_items = traces_by_turn.get(key, [])
        return model_output_order_key(min(trace_items, key=model_output_order_key))

    ordered_turn_keys = sorted(
        turn_keys,
        key=turn_order_key,
    )
    turn_heading = "#" * max(1, min(6, turn_heading_level))
    lines: list[str] = []
    for turn_number, turn_key in enumerate(ordered_turn_keys, 1):
        prompt_events = sorted(prompts_by_turn.get(turn_key, []), key=event_order_key)
        if not prompt_events and turn_key in external_prompt_by_turn:
            prompt_events = [external_prompt_by_turn[turn_key]]
        native_turn_id = turn_key.removeprefix("native:") if turn_key.startswith("native:") else ""
        lines.extend(
            [
                f"{turn_heading} Turn {turn_number:05d}",
                "",
                f"- Native turn ID: `{native_turn_id or 'unavailable'}`",
                f"- Human messages: `{len(prompt_events)}`",
                "",
            ]
        )
        for message_number, prompt_event in enumerate(prompt_events, 1):
            prompt_event_id = str(prompt_event.get("event_id") or "")
            prompt_number = prompt_numbers.get(prompt_event_id)
            lines.extend(
                render_trajectory_event(
                    (
                        f"P{prompt_number:05d}"
                        if prompt_number
                        else f"P-external-{message_number:02d}"
                    ),
                    "prompt",
                    prompt_event,
                    prompt_numbers,
                    heading_level=event_heading_level,
                )
            )
        for trace_number, trace_event in enumerate(
            sorted(traces_by_turn.get(turn_key, []), key=model_output_order_key),
            1,
        ):
            lines.extend(
                render_trajectory_event(
                    f"R{trace_number:05d}",
                    "trace",
                    trace_event,
                    prompt_numbers,
                    heading_level=event_heading_level,
                )
            )

    if unlinked_traces:
        label = "Trace-only session" if not ordered_turn_keys else "Unlinked session events"
        lines.extend([f"{turn_heading} {label}", ""])
        for trace_number, trace_event in enumerate(
            sorted(unlinked_traces, key=model_output_order_key),
            1,
        ):
            lines.extend(
                render_trajectory_event(
                    f"U{trace_number:05d}",
                    "trace",
                    trace_event,
                    prompt_numbers,
                    heading_level=event_heading_level,
                )
            )
    return lines


def session_projection_fingerprint(
    items: list[tuple[str, dict[str, Any]]],
    prompt_numbers: dict[str, int],
    output_numbers: dict[str, int],
) -> str:
    payload = []
    for kind, event in sorted(items, key=trajectory_item_order_key):
        if kind == "prompt":
            event_id = str(event.get("event_id") or "")
            payload.append(
                (
                    kind,
                    event_id,
                    prompt_numbers.get(event_id),
                    event.get("prompt", {}).get("sha256"),
                    event.get("occurred_at"),
                )
            )
        else:
            output_id = str(event.get("trace_event_id") or event.get("model_output_id") or "")
            content = event.get("content") if isinstance(event.get("content"), dict) else event.get("output", {})
            payload.append(
                (
                    kind,
                    output_id,
                    output_numbers.get(output_id),
                    content.get("sha256"),
                    event.get("occurred_at"),
                    event.get("context", {}).get("phase"),
                )
            )
    return sha256_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def append_event_supersession(
    store: Path,
    *,
    event_id: str,
    canonical_event_id: str,
    reason: str,
) -> bool:
    if not event_id or not canonical_event_id or event_id == canonical_event_id:
        return False
    record_id = "phs_" + sha256_text(f"{event_id}|{canonical_event_id}|{reason}")[:32]
    path = event_supersession_file(store)
    with file_lock(store / "state" / "supersession.lock"):
        existing = {str(item.get("supersession_id") or "") for item in iter_event_supersessions(store)}
        if record_id in existing:
            return False
        record = {
            "schema_version": "1.0.0",
            "record_type": "event_supersession",
            "supersession_id": record_id,
            "event_id": event_id,
            "canonical_event_id": canonical_event_id,
            "reason": reason,
            "recorded_at": iso_z(),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        mark_index_dirty(store, "event_supersession_appended")
    return True


def append_event_exclusion(store: Path, *, event_id: str, reason: str) -> bool:
    if not event_id or not reason:
        return False
    exclusion_id = "phx_" + sha256_text(f"{event_id}|{reason}")[:32]
    path = event_exclusion_file(store)
    with file_lock(store / "state" / "exclusion.lock"):
        existing = {str(item.get("exclusion_id") or "") for item in iter_event_exclusions(store)}
        if exclusion_id in existing:
            return False
        record = {
            "schema_version": "1.0.0",
            "record_type": EXCLUSION_RECORD_TYPE,
            "exclusion_id": exclusion_id,
            "event_id": event_id,
            "reason": reason,
            "recorded_at": iso_z(),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        mark_index_dirty(store, "event_exclusion_appended")
    return True


def repair_automatic_context_events(store: Path) -> int:
    excluded = excluded_event_ids(store)
    repaired = 0
    for event in iter_events(store):
        event_id = str(event.get("event_id") or "")
        text = str(event.get("prompt", {}).get("text") or "")
        if event_id and event_id not in excluded and is_automatic_prompt(text):
            if append_event_exclusion(
                store,
                event_id=event_id,
                reason="automatic_context_not_human_input",
            ):
                repaired += 1
                excluded.add(event_id)
    return repaired


def repair_out_of_scope_events(root: Path, store: Path) -> int:
    """Hide legacy events automatically captured from a non-root session cwd.

    The immutable event remains in the canonical ledger. An active explicit
    binding to this project dynamically re-enables it.
    """

    excluded = excluded_event_ids(store)
    bindings = active_session_bindings()
    reason = EXACT_ROOT_EXCLUSION_PREFIX + project_id(root)
    repaired = 0
    for event in iter_events(store):
        event_id = str(event.get("event_id") or "")
        if not event_id or event_id in excluded:
            continue
        platform = str(event.get("source", {}).get("platform") or "").lower()
        session_id = str(event.get("session", {}).get("id") or "")
        cwd = event.get("context", {}).get("cwd")
        if platform not in {"claude", "codex"} or not cwd:
            continue
        if session_belongs_to_project_root(
            platform=platform,
            session_id=session_id,
            cwd=cwd,
            root=root,
            bindings=bindings,
        ):
            continue
        if append_event_exclusion(store, event_id=event_id, reason=reason):
            repaired += 1
            excluded.add(event_id)
    return repaired


LEGACY_IMAGE_OMISSION_RE = re.compile(
    r"(?im)^\s*\[image attachment omitted(?:\s*:[^\]]*)?\]\s*(?:\r?\n)?"
)


def without_legacy_image_omissions(text: str) -> str:
    return LEGACY_IMAGE_OMISSION_RE.sub("", text).strip()


def repair_legacy_image_duplicates(store: Path) -> int:
    events = list(iter_events(store))
    already_superseded = superseded_event_ids(store)
    image_event_ids = {str(item.get("event_id") or "") for item in iter_prompt_images(store)}
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = collections.defaultdict(list)
    for event in events:
        source = event.get("source", {})
        native = str(source.get("native_event_id") or "")
        if native:
            key = ("native", source.get("platform"), native)
        elif source.get("path") and source.get("line"):
            key = ("source", source.get("platform"), source.get("path"), source.get("line"))
        else:
            continue
        groups[key].append(event)
    repaired = 0
    for group in groups.values():
        active = [event for event in group if str(event.get("event_id") or "") not in already_superseded]
        legacy = [
            event
            for event in active
            if LEGACY_IMAGE_OMISSION_RE.search(str(event.get("prompt", {}).get("text") or ""))
        ]
        clean = [event for event in active if event not in legacy]
        if not legacy or not clean:
            continue
        canonical = max(
            clean,
            key=lambda event: (
                str(event.get("event_id") or "") in image_event_ids,
                event.get("captured_at") or "",
            ),
        )
        canonical_text = str(canonical.get("prompt", {}).get("text") or "").strip()
        canonical_id = str(canonical.get("event_id") or "")
        for old in legacy:
            old_text = str(old.get("prompt", {}).get("text") or "")
            if without_legacy_image_omissions(old_text) != canonical_text:
                continue
            old_id = str(old.get("event_id") or "")
            if append_event_supersession(
                store,
                event_id=old_id,
                canonical_event_id=canonical_id,
                reason="legacy_image_omission_migrated_to_image_manifest",
            ):
                repaired += 1
                already_superseded.add(old_id)
    return repaired


def event_identity(
    platform: str,
    session_id: str,
    prompt_hash: str,
    occurred_at: str,
    native_event_id: str | None,
    turn_id: str | None,
    source_mode: str,
    source_path: str | None = None,
    source_line: int | None = None,
) -> str:
    source_identity = None
    if source_path and source_line:
        source_identity = f"source:{normalize_path(source_path)}:{int(source_line)}"
    stable = native_event_id or source_identity or turn_id
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
            source_path,
            source_line,
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
        mark_index_dirty(store, "prompt_event_appended")
        if existing_ids is not None:
            existing_ids.add(event["event_id"])
    return True


def model_output_identity(
    platform: str,
    session_id: str,
    event_type: str,
    content_hash: str,
    source_path: str,
    source_line: int,
    block_index: int,
) -> str:
    material = "|".join(
        (
            SCHEMA_VERSION,
            platform,
            session_id,
            normalize_path(source_path),
            str(source_line),
            str(block_index),
            event_type,
            content_hash,
        )
    )
    return "ate_" + sha256_text(material)[:32]


def sanitize_trace_value(value: Any) -> tuple[Any, dict[str, int]]:
    """Recursively sanitize a trace payload without dropping any event category."""

    stats = {"secret_redactions": 0, "attachments_omitted": 0}
    if isinstance(value, str):
        value, omitted = omit_embedded_files(value)
        value, redactions = redact_secrets(value)
        stats["attachments_omitted"] += omitted
        stats["secret_redactions"] += redactions
        return value, stats
    if isinstance(value, list):
        rendered = []
        for item in value:
            clean, item_stats = sanitize_trace_value(item)
            rendered.append(clean)
            for key in stats:
                stats[key] += item_stats[key]
        return rendered, stats
    if isinstance(value, dict):
        rendered = {}
        for key, item in value.items():
            clean, item_stats = sanitize_trace_value(item)
            rendered[str(key)] = clean
            for stat_key in stats:
                stats[stat_key] += item_stats[stat_key]
        return rendered, stats
    return value, stats


def trace_value_matches_patterns(value: Any, patterns: Iterable[re.Pattern[str]]) -> bool:
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in patterns)
    if isinstance(value, list):
        return any(trace_value_matches_patterns(item, patterns) for item in value)
    if isinstance(value, dict):
        return any(trace_value_matches_patterns(item, patterns) for item in value.values())
    return False


def trace_content_hash(text: str, structured: Any) -> str:
    return sha256_text(
        json.dumps(
            {"text": text, "structured": structured},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def build_model_output_event(
    *,
    root: Path,
    platform: str,
    session_id: str,
    occurred_at: str,
    event_type: str,
    actor_role: str,
    output_text: str,
    structured: Any,
    source_path: str,
    source_line: int,
    block_index: int = 0,
    raw_type: str | None = None,
    native_event_id: str | None = None,
    turn_id: str | None = None,
    cwd: str | None = None,
    model: str | None = None,
    phase: str | None = None,
    prompt_event_id: str | None = None,
    sanitation: dict[str, int] | None = None,
    actor_name: str | None = None,
    tool_call_id: str | None = None,
    parent_session_id: str | None = None,
    agent_id: str | None = None,
    is_subagent: bool = False,
) -> dict[str, Any]:
    content_hash = trace_content_hash(output_text, structured)
    trace_event_id = model_output_identity(
        platform,
        session_id,
        event_type,
        content_hash,
        source_path,
        source_line,
        block_index,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": MODEL_OUTPUT_RECORD_TYPE,
        "trace_event_id": trace_event_id,
        "model_output_id": trace_event_id,
        "captured_at": iso_z(),
        "occurred_at": occurred_at,
        "event_type": event_type,
        "source": {
            "mode": "backfill",
            "platform": platform,
            "path": source_path,
            "line": source_line,
            "block_index": block_index,
            "raw_type": raw_type,
            "native_event_id": native_event_id,
        },
        "project": {
            "id": project_id(root),
            "name": root.name,
            "root": str(root),
        },
        "session": {
            "id": session_id,
            "turn_id": turn_id,
            "transcript_path": source_path,
            "parent_session_id": parent_session_id,
            "agent_id": agent_id,
            "is_subagent": is_subagent,
        },
        "actor": {
            "role": actor_role,
            "name": actor_name,
        },
        "content": {
            "text": output_text,
            "structured": structured,
            "sha256": content_hash,
            "chars": len(output_text),
            "secret_redactions": int((sanitation or {}).get("secret_redactions", 0)),
            "attachments_omitted": int((sanitation or {}).get("attachments_omitted", 0)),
        },
        "context": {
            "cwd": cwd or str(root),
            "model": model,
            "phase": phase,
        },
        "links": {
            "prompt_event_id": prompt_event_id,
            "tool_call_id": tool_call_id,
            "parent_trace_event_id": None,
        },
    }


def append_model_output(
    store: Path,
    event: dict[str, Any],
    existing_ids: set[str] | None = None,
) -> bool:
    path = model_output_file(store, str(event["occurred_at"]))
    output_id = str(event.get("trace_event_id") or event["model_output_id"])
    with file_lock(store / "state" / "write.lock"):
        if existing_ids is not None and output_id in existing_ids:
            return False
        if path.exists():
            for _, prior in read_jsonl(path):
                if str(prior.get("trace_event_id") or prior.get("model_output_id") or "") == output_id:
                    if existing_ids is not None:
                        existing_ids.add(output_id)
                    return False
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        mark_index_dirty(store, "model_output_appended")
        if existing_ids is not None:
            existing_ids.add(output_id)
    return True


def append_model_outputs_bulk(
    store: Path,
    events: Iterable[dict[str, Any]],
) -> tuple[int, int]:
    pending = list(events)
    if not pending:
        return 0, 0
    added = skipped = 0
    with file_lock(store / "state" / "write.lock"):
        existing_ids: set[str] = set()
        for path in iter_model_output_files(store):
            for _, prior in read_jsonl(path):
                output_id = str(
                    prior.get("trace_event_id") or prior.get("model_output_id") or ""
                )
                if output_id:
                    existing_ids.add(output_id)
        grouped: dict[Path, list[dict[str, Any]]] = collections.defaultdict(list)
        for event in pending:
            output_id = str(event.get("trace_event_id") or event.get("model_output_id") or "")
            if not output_id or output_id in existing_ids:
                skipped += 1
                continue
            existing_ids.add(output_id)
            grouped[model_output_file(store, str(event["occurred_at"]))].append(event)
            added += 1
        for path, path_events in grouped.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                for event in path_events:
                    handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
    if added:
        mark_index_dirty(store, "model_outputs_appended")
    return added, skipped


def image_manifest_file(store: Path) -> Path:
    return store / "assets" / "manifest.jsonl"


def iter_prompt_images(store: Path) -> Iterator[dict[str, Any]]:
    path = image_manifest_file(store)
    if path.exists():
        for _, value in read_jsonl(path):
            if value.get("record_type") == IMAGE_RECORD_TYPE:
                yield value


def image_candidate_from_value(
    value: Any,
    *,
    media_type: str | None = None,
    name: str | None = None,
    base_dir: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.lower().startswith("data:image/"):
        return {"kind": "data_url", "value": raw, "media_type": media_type, "name": name}
    if raw.lower().startswith(("http://", "https://")):
        return {"kind": "remote_url", "value": raw, "media_type": media_type, "name": name}
    if raw.lower().startswith("file://"):
        parsed = urllib.parse.urlparse(raw)
        path = urllib.parse.unquote(parsed.path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        raw = path
    return {
        "kind": "local_path",
        "value": raw,
        "media_type": media_type,
        "name": name,
        "base_dir": base_dir,
    }


def image_candidates_from_blocks(content: Any, *, base_dir: str | None = None) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    candidates: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in {"image", "input_image", "local_image"}:
            continue
        source = block.get("source") if isinstance(block.get("source"), dict) else {}
        media_type = str(source.get("media_type") or block.get("media_type") or "") or None
        name = str(block.get("name") or source.get("name") or "") or None
        if source.get("type") == "base64" and isinstance(source.get("data"), str):
            candidates.append(
                {
                    "kind": "base64",
                    "value": source["data"],
                    "media_type": media_type,
                    "name": name,
                }
            )
            continue
        direct = (
            block.get("image_url")
            or block.get("path")
            or block.get("url")
            or source.get("path")
            or source.get("url")
        )
        candidate = image_candidate_from_value(direct, media_type=media_type, name=name, base_dir=base_dir)
        if candidate:
            candidates.append(candidate)
    return candidates


def image_candidates_from_hook_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    base_dir = str(payload.get("cwd") or "") or None
    for key in ("local_images", "images", "attachments"):
        values = payload.get(key)
        if values is None:
            continue
        for value in values if isinstance(values, list) else [values]:
            if isinstance(value, dict):
                before = len(candidates)
                candidates.extend(image_candidates_from_blocks([value], base_dir=base_dir))
                if len(candidates) == before:
                    direct = value.get("path") or value.get("url") or value.get("image_url")
                    media_type = str(value.get("media_type") or "").lower()
                    block_type = str(value.get("type") or "").lower()
                    direct_suffix = Path(str(direct or "")).suffix.lower()
                    looks_like_image = (
                        key in {"local_images", "images"}
                        or media_type.startswith("image/")
                        or block_type in {"image", "input_image", "local_image"}
                        or direct_suffix in IMAGE_SUFFIXES
                    )
                    if looks_like_image:
                        candidate = image_candidate_from_value(
                            direct,
                            media_type=media_type or None,
                            name=str(value.get("name") or "") or None,
                            base_dir=base_dir,
                        )
                        if candidate:
                            candidates.append(candidate)
            else:
                value_suffix = Path(str(value or "")).suffix.lower()
                if key in {"local_images", "images"} or value_suffix in IMAGE_SUFFIXES:
                    candidate = image_candidate_from_value(value, base_dir=base_dir)
                    if candidate:
                        candidates.append(candidate)
    for key in ("prompt", "user_prompt", "userPrompt", "input", "content"):
        candidates.extend(image_candidates_from_blocks(payload.get(key), base_dir=base_dir))
    message = payload.get("message")
    if isinstance(message, dict):
        candidates.extend(image_candidates_from_blocks(message.get("content"), base_dir=base_dir))
    specific = payload.get("hookSpecificInput")
    if isinstance(specific, dict):
        candidates.extend(image_candidates_from_hook_payload(specific))
    return candidates


def attachment_path_from_block(block: dict[str, Any]) -> str | None:
    source = block.get("source") if isinstance(block.get("source"), dict) else {}
    value = (
        block.get("path")
        or block.get("file_path")
        or source.get("path")
        or source.get("file_path")
    )
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def file_path_notes_from_hook_payload(payload: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("attachments", "files"):
        values = payload.get(key)
        if values is None:
            continue
        for value in values if isinstance(values, list) else [values]:
            if isinstance(value, dict):
                media_type = str(value.get("media_type") or "").lower()
                block_type = str(value.get("type") or "").lower()
                path = attachment_path_from_block(value)
                if (
                    path
                    and not media_type.startswith("image/")
                    and block_type not in {"image", "input_image", "local_image"}
                    and Path(path).suffix.lower() not in IMAGE_SUFFIXES
                ):
                    paths.append(path)
            elif isinstance(value, str):
                stripped = value.strip()
                if stripped and not stripped.lower().startswith(("http://", "https://", "data:image/")):
                    if Path(stripped).suffix.lower() not in IMAGE_SUFFIXES:
                        paths.append(stripped)
    message = payload.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), list):
        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") in {"document", "file", "input_file"}:
                path = attachment_path_from_block(block)
                if path:
                    paths.append(path)
    specific = payload.get("hookSpecificInput")
    if isinstance(specific, dict):
        paths.extend(file_path_notes_from_hook_payload(specific))
    return list(dict.fromkeys(paths))


def append_file_path_notes(prompt: str, paths: list[str]) -> str:
    missing = [path for path in paths if path not in prompt]
    if not missing:
        return prompt
    notes = "\n".join(f"[attached file: {path}]" for path in missing)
    return f"{prompt.rstrip()}\n{notes}".lstrip()


def sniff_raster_image(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data.startswith(b"BM"):
        return "image/bmp", ".bmp"
    return None


def candidate_display_name(candidate: dict[str, Any]) -> str | None:
    if candidate.get("name"):
        return Path(str(candidate["name"])).name
    value = str(candidate.get("value") or "")
    if candidate.get("kind") == "local_path":
        return Path(value).name
    if candidate.get("kind") == "remote_url":
        return Path(urllib.parse.urlparse(value).path).name or None
    return None


def decode_image_candidate(candidate: dict[str, Any]) -> tuple[bytes, str, str, str | None]:
    kind = str(candidate.get("kind") or "")
    value = str(candidate.get("value") or "")
    declared_media = str(candidate.get("media_type") or "").lower()
    if kind == "remote_url":
        raise ValueError("remote image URLs are not downloaded")
    if kind == "local_path":
        path = Path(value).expanduser()
        if not path.is_absolute() and candidate.get("base_dir"):
            path = Path(str(candidate["base_dir"])) / path
        if not path.is_file():
            raise ValueError("local image path is missing")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
        data = path.read_bytes()
    elif kind == "data_url":
        header, separator, encoded = value.partition(",")
        if not separator or ";base64" not in header.lower():
            raise ValueError("unsupported non-base64 image data URL")
        declared_media = header[5:].split(";", 1)[0].lower()
        compact = re.sub(r"\s+", "", encoded)
        if len(compact) * 3 // 4 > MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
        try:
            data = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("invalid base64 image") from exc
    elif kind == "base64":
        compact = re.sub(r"\s+", "", value)
        if len(compact) * 3 // 4 > MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
        try:
            data = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("invalid base64 image") from exc
    else:
        raise ValueError("unsupported image source")
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes or is empty")
    detected = sniff_raster_image(data)
    if not detected:
        raise ValueError("unsupported or unrecognized raster image")
    media_type, extension = detected
    if declared_media and declared_media.startswith("image/") and declared_media in IMAGE_MEDIA:
        extension = IMAGE_MEDIA[declared_media] if declared_media == media_type else extension
    return data, media_type, extension, candidate_display_name(candidate)


def persist_prompt_images(
    store: Path,
    event_id: str,
    candidates: list[dict[str, Any]],
    *,
    source_path: str | None = None,
    source_line: int | None = None,
) -> dict[str, int]:
    if not candidates:
        return {"seen": 0, "saved": 0, "omitted": 0}
    decoded: list[tuple[dict[str, Any], bytes, str, str, str | None, str]] = []
    failures: list[dict[str, Any]] = []
    total_bytes = 0
    seen_hashes: set[str] = set()
    for candidate in candidates[:MAX_IMAGES_PER_EVENT]:
        try:
            data, media_type, extension, original_name = decode_image_candidate(candidate)
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen_hashes:
                continue
            if total_bytes + len(data) > MAX_IMAGE_TOTAL_BYTES:
                raise ValueError(f"event images exceed {MAX_IMAGE_TOTAL_BYTES} bytes")
            seen_hashes.add(digest)
            total_bytes += len(data)
            decoded.append((candidate, data, media_type, extension, original_name, digest))
        except ValueError as exc:
            failures.append(
                {
                    "source_kind": str(candidate.get("kind") or "unknown"),
                    "original_name": candidate_display_name(candidate),
                    "reason": str(exc),
                }
            )
    if len(candidates) > MAX_IMAGES_PER_EVENT:
        failures.append(
            {
                "source_kind": "limit",
                "original_name": None,
                "reason": f"more than {MAX_IMAGES_PER_EVENT} images were attached",
            }
        )

    manifest = image_manifest_file(store)
    lock = store / "state" / "write.lock"
    saved = 0
    with file_lock(lock):
        existing = {str(item.get("attachment_id") or "") for item in iter_prompt_images(store)}
        manifest.parent.mkdir(parents=True, exist_ok=True)
        rendered: list[str] = []
        for candidate, data, media_type, extension, original_name, digest in decoded:
            attachment_id = "phi_" + sha256_text(f"{event_id}|{digest}")[:32]
            relative_path = f"assets/images/{digest}{extension}"
            asset_path = store / Path(relative_path)
            if asset_path.exists():
                current = asset_path.read_bytes()
                if hashlib.sha256(current).hexdigest() != digest:
                    failures.append(
                        {
                            "source_kind": str(candidate.get("kind") or "unknown"),
                            "original_name": original_name,
                            "reason": "content-addressed image path is corrupt",
                        }
                    )
                    continue
            else:
                atomic_write_bytes(asset_path, data)
            if attachment_id in existing:
                continue
            record = {
                "schema_version": "1.0.0",
                "record_type": IMAGE_RECORD_TYPE,
                "attachment_id": attachment_id,
                "event_id": event_id,
                "captured_at": iso_z(),
                "asset": {
                    "path": relative_path,
                    "sha256": digest,
                    "bytes": len(data),
                    "media_type": media_type,
                },
                "source": {
                    "kind": str(candidate.get("kind") or "unknown"),
                    "original_name": original_name,
                    "transcript_path": source_path,
                    "line": source_line,
                },
            }
            rendered.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            existing.add(attachment_id)
            saved += 1
        if rendered:
            with manifest.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(rendered) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            mark_index_dirty(store, "prompt_image_appended")
        if failures:
            miss_path = store / "state" / "image-misses.jsonl"
            with miss_path.open("a", encoding="utf-8", newline="\n") as handle:
                for failure in failures:
                    handle.write(
                        json.dumps(
                            {"recorded_at": iso_z(), "event_id": event_id, **failure},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                handle.flush()
                os.fsync(handle.fileno())
    return {"seen": len(candidates), "saved": saved, "omitted": len(failures)}


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
        elif include_attachments and block_type in {"document", "file", "input_file"}:
            path = attachment_path_from_block(block)
            if path:
                parts.append(f"[attached file: {path}]")
            else:
                parts.append(f"[{block_type} attachment omitted]")
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


def read_jsonl_reverse(path: Path, *, chunk_size: int = 64 * 1024) -> Iterator[dict[str, Any]]:
    """Read JSONL objects newest-first without parsing the entire file."""

    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        remainder = b""
        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            block = handle.read(read_size) + remainder
            parts = block.split(b"\n")
            remainder = parts[0]
            for raw in reversed(parts[1:]):
                if not raw.strip():
                    continue
                try:
                    value = json.loads(raw.decode("utf-8-sig"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(value, dict):
                    yield value
        if remainder.strip():
            try:
                value = json.loads(remainder.decode("utf-8-sig"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            if isinstance(value, dict):
                yield value


def latest_user_record_from_transcript(path: Path, platform: str) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    for line_no, obj in read_jsonl(path):
        if platform == "claude" and obj.get("type") == "user" and isinstance(obj.get("message"), dict):
            content = obj["message"].get("content")
            text, saw_tool = text_from_blocks(content)
            images = image_candidates_from_blocks(content, base_dir=str(path.parent))
            if (text.strip() or images) and not (saw_tool and not text.strip() and not images):
                latest = {"text": text, "images": images, "line": line_no}
        elif platform == "codex" and obj.get("type") == "response_item":
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            if payload.get("type") == "message" and payload.get("role") == "user":
                content = payload.get("content")
                text, _ = text_from_blocks(content)
                images = image_candidates_from_blocks(content, base_dir=str(path.parent))
                if text.strip() or images:
                    latest = {"text": text, "images": images, "line": line_no}
    return latest


def latest_user_from_transcript(path: Path, platform: str) -> str:
    record = latest_user_record_from_transcript(path, platform)
    return str(record.get("text") or "") if record else ""


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


def codex_turn_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("turn_id")
    if value:
        return str(value)
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if isinstance(metadata, dict) and metadata.get("turn_id"):
        return str(metadata["turn_id"])
    return None


def find_codex_rollout(codex_home: Path, session_id: str, transcript_path: Any = None) -> Path | None:
    if transcript_path:
        hinted = Path(str(transcript_path))
        if hinted.exists():
            return hinted
    if not session_id.strip():
        return None
    candidates: list[Path] = []
    for folder_name in ("sessions", "archived_sessions"):
        folder = codex_home / folder_name
        if folder.exists():
            candidates.extend(folder.rglob(f"*{session_id}.jsonl"))
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def latest_codex_prompt_event(path: Path) -> dict[str, Any] | None:
    candidate: dict[str, Any] | None = None
    fallback_model: str | None = None
    for obj in read_jsonl_reverse(path):
        if candidate is not None:
            if obj.get("type") == "turn_context" and isinstance(obj.get("payload"), dict):
                model = normalize_model(obj["payload"].get("model"))
                if model:
                    candidate["model"] = model
                    return candidate
            if obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
                fallback_model = normalize_model(obj["payload"].get("model"))
                candidate["model"] = fallback_model
                return candidate
            continue
        if obj.get("type") != "response_item" or not isinstance(obj.get("payload"), dict):
            continue
        payload = obj["payload"]
        if payload.get("type") != "message" or payload.get("role") != "user":
            continue
        content = payload.get("content")
        raw_text, _ = text_from_blocks(content)
        images = image_candidates_from_blocks(content, base_dir=str(path.parent))
        if raw_text.strip() and is_automatic_prompt(raw_text) and not images:
            continue
        text, sanitation = sanitize_prompt(raw_text, backfill=True)
        if (not text and not images) or (text and is_automatic_prompt(text) and not images):
            continue
        occurred = parse_iso(obj.get("timestamp"))
        candidate = {
            "text": text,
            "images": images,
            "sanitation": sanitation,
            "timestamp": iso_z(occurred) if occurred else iso_z(),
            "line": None,
            "model": None,
            "turn_id": codex_turn_id(payload),
            "native_event_id": str(payload.get("id") or obj.get("id") or "") or None,
        }
    return candidate


def find_equivalent_event(
    store: Path,
    *,
    platform: str,
    session_id: str,
    turn_id: str | None,
    prompt_hash: str,
    occurred_at: str,
) -> dict[str, Any] | None:
    for event in iter_active_events(store):
        if event.get("source", {}).get("platform") != platform:
            continue
        if str(event.get("session", {}).get("id") or "") != session_id:
            continue
        if (
            turn_id
            and str(event.get("session", {}).get("turn_id") or "") == turn_id
            and event.get("prompt", {}).get("sha256") == prompt_hash
        ):
            return event
        if (
            event.get("prompt", {}).get("sha256") == prompt_hash
            and event.get("occurred_at") == occurred_at
        ):
            return event
    return None


def equivalent_event_exists(
    store: Path,
    *,
    platform: str,
    session_id: str,
    turn_id: str | None,
    prompt_hash: str,
    occurred_at: str,
) -> bool:
    return find_equivalent_event(
        store,
        platform=platform,
        session_id=session_id,
        turn_id=turn_id,
        prompt_hash=prompt_hash,
        occurred_at=occurred_at,
    ) is not None


def recover_codex_stop(
    payload: dict[str, Any],
    *,
    project: Path | None = None,
    codex_home: Path | None = None,
) -> dict[str, Any]:
    payload_session_id = str(payload.get("session_id") or payload.get("conversation_id") or "").strip()
    rollout = find_codex_rollout(
        codex_home or (Path.home() / ".codex"),
        payload_session_id,
        payload.get("transcript_path"),
    )
    meta = codex_meta_from_path(rollout) if rollout else None
    session_id = str((meta or {}).get("id") or (meta or {}).get("session_id") or payload_session_id).strip()
    cwd = Path(str((meta or {}).get("cwd") or payload.get("cwd") or os.getcwd()))
    root = resolve_project_root(
        cwd,
        explicit=project,
        platform="codex",
        session_id=session_id,
    )
    if is_unsafe_broad_project_root(root):
        return {"captured": False, "reason": "unsafe_broad_project_root", "project": str(root)}
    if not session_id:
        return {"captured": False, "reason": "missing_session_id", "project": str(root)}
    if not session_belongs_to_project_root(
        platform="codex",
        session_id=session_id,
        cwd=cwd,
        root=root,
    ):
        return {
            "captured": False,
            "reason": "cwd_not_exact_project_root",
            "project": str(root),
            "session_id": session_id,
            "session_cwd": str(cwd),
            "source_path": str(rollout) if rollout else None,
        }
    store, _ = init_store(root)
    if not rollout:
        return {
            "captured": False,
            "reason": "rollout_not_found",
            "project": str(root),
            "session_id": session_id,
            "source_path": str(rollout),
        }
    candidate = latest_codex_prompt_event(rollout)
    if not candidate:
        return {
            "captured": False,
            "reason": "human_prompt_not_found",
            "project": str(root),
            "session_id": session_id,
        }
    prompt_hash = sha256_text(candidate["text"])
    existing_event = find_equivalent_event(
        store,
        platform="codex",
        session_id=session_id,
        turn_id=candidate.get("turn_id"),
        prompt_hash=prompt_hash,
        occurred_at=candidate["timestamp"],
    )
    if existing_event:
        image_result = persist_prompt_images(
            store,
            str(existing_event.get("event_id") or ""),
            candidate.get("images") or [],
            source_path=str(rollout),
            source_line=candidate.get("line"),
        )
        return {
            "captured": False,
            "reason": "already_recorded",
            "project": str(root),
            "event_id": existing_event.get("event_id"),
            "session_id": session_id,
            "turn_id": candidate.get("turn_id"),
            "source_path": str(rollout),
            "model": normalize_model(existing_event.get("context", {}).get("model")) or candidate.get("model"),
            "images": image_result,
        }
    event = build_event(
        root=root,
        platform="codex",
        source_mode="stop_recovery",
        prompt_text=candidate["text"],
        session_id=session_id,
        occurred_at=candidate["timestamp"],
        turn_id=candidate.get("turn_id"),
        transcript_path=str(rollout),
        native_event_id=candidate.get("native_event_id"),
        source_path=str(rollout),
        source_line=candidate.get("line"),
        cwd=str(cwd),
        model=candidate.get("model"),
        permission_mode=str(payload.get("permission_mode")) if payload.get("permission_mode") else None,
        sanitation=candidate.get("sanitation"),
    )
    captured = append_event(store, event)
    image_result = persist_prompt_images(
        store,
        event["event_id"],
        candidate.get("images") or [],
        source_path=str(rollout),
        source_line=candidate.get("line"),
    )
    return {
        "captured": captured,
        "reason": "captured" if captured else "duplicate_event_id",
        "project": str(root),
        "event_id": event["event_id"],
        "session_id": session_id,
        "turn_id": candidate.get("turn_id"),
        "source_path": str(rollout),
        "model": candidate.get("model"),
        "images": image_result,
    }


def read_hook_payload() -> dict[str, Any] | None:
    """Read hook JSON as UTF-8 bytes so Windows console code pages cannot corrupt it."""
    try:
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.read() if stream is not None else sys.stdin.read()
    except OSError:
        return None
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode(sys.stdin.encoding or "utf-8", errors="replace")
    else:
        text = raw
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, UnicodeError):
        return None
    return payload if isinstance(payload, dict) else None


def capture_stop_recovery(args: argparse.Namespace) -> int:
    payload = read_hook_payload()
    if payload is None:
        return 0
    result = recover_codex_stop(payload, project=args.project, codex_home=args.codex_home)
    if result.get("project") and result.get("reason") != "cwd_not_exact_project_root":
        root = Path(str(result["project"]))
        ensure_initial_index(root / STORE_NAME)
        result["auto_sync"] = schedule_auto_sync(
            root,
            root / STORE_NAME,
            source_platform="codex",
            session_id=str(result.get("session_id") or payload.get("session_id") or "unknown"),
            trigger="stop_recovery",
            source_path=result.get("source_path") or payload.get("transcript_path"),
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


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


def capture_hook_payload(args: argparse.Namespace, payload: dict[str, Any]) -> int:
    platform = args.platform
    if platform == "auto":
        platform = "codex" if payload.get("turn_id") or payload.get("model") else "claude"
    transcript_path = payload.get("transcript_path")
    session_id = str(payload.get("session_id") or "unknown")
    transcript_meta = None
    if platform == "codex" and transcript_path and Path(str(transcript_path)).is_file():
        transcript_meta = codex_meta_from_path(Path(str(transcript_path)))
        session_id = str(
            (transcript_meta or {}).get("id")
            or (transcript_meta or {}).get("session_id")
            or session_id
        )
    cwd = Path(str((transcript_meta or {}).get("cwd") or payload.get("cwd") or os.getcwd()))
    root = resolve_project_root(
        cwd,
        explicit=args.project,
        platform=platform,
        session_id=session_id,
    )
    if is_unsafe_broad_project_root(root):
        return 0
    if not session_belongs_to_project_root(
        platform=platform,
        session_id=session_id,
        cwd=cwd,
        root=root,
    ):
        return 0
    store, _ = init_store(root)
    prompt = prompt_from_hook_payload(payload)
    prompt = append_file_path_notes(prompt, file_path_notes_from_hook_payload(payload))
    images = image_candidates_from_hook_payload(payload)
    source_line = None
    if not prompt.strip() and not images and transcript_path and Path(str(transcript_path)).exists():
        latest = latest_user_record_from_transcript(Path(str(transcript_path)), platform)
        if latest:
            prompt = str(latest.get("text") or "")
            images = latest.get("images") or []
            source_line = latest.get("line")
    if not prompt.strip() and not images:
        record_hook_miss(store, payload)
        schedule_auto_sync(
            root,
            store,
            source_platform=platform,
            session_id=session_id,
            trigger="user_prompt_submit_miss",
            source_path=transcript_path,
        )
        return 0
    prompt, sanitation = sanitize_prompt(prompt)
    if (not prompt and not images) or (prompt and is_automatic_prompt(prompt) and not images):
        return 0
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
        source_path=str(transcript_path) if transcript_path and source_line else None,
        source_line=source_line,
        cwd=str(cwd),
        model=str(payload.get("model")) if payload.get("model") else None,
        permission_mode=str(payload.get("permission_mode")) if payload.get("permission_mode") else None,
        sanitation=sanitation,
    )
    append_event(store, event)
    persist_prompt_images(
        store,
        event["event_id"],
        images,
        source_path=str(transcript_path) if transcript_path else None,
        source_line=source_line,
    )
    ensure_initial_index(store)
    schedule_auto_sync(
        root,
        store,
        source_platform=platform,
        session_id=session_id,
        trigger="user_prompt_submit",
        source_path=transcript_path,
    )
    return 0


def ensure_initial_index(store: Path) -> bool:
    """Materialize small first-use views before historical reconciliation ends."""

    required = (
        store / "index" / "PROMPTS.md",
        store / "index" / "MODELOUT.md",
        store / "index" / "TRAJECTORY.md",
    )
    if all(path.is_file() for path in required):
        return False
    try:
        rebuild_index_for_store(store)
    except Exception:
        return False
    return True


def capture_hook(args: argparse.Namespace) -> int:
    payload = read_hook_payload()
    if payload is None:
        return 0
    try:
        return capture_hook_payload(args, payload)
    except Exception as exc:  # Hook capture is best-effort and must not interrupt the active task.
        record_hook_runtime_error(payload, exc)
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


def claude_session_cwd(path: Path) -> str | None:
    """Read the first recorded cwd, which represents the Claude session root."""

    for _, obj in read_jsonl(path):
        cwd = obj.get("cwd")
        if cwd:
            return str(cwd)
    return None


def claude_source_belongs_to_root(
    path: Path,
    root: Path,
    *,
    session_id: str | None = None,
    bindings: dict[str, dict[str, Any]] | None = None,
) -> bool:
    return session_belongs_to_project_root(
        platform="claude",
        session_id=session_id or path.stem,
        cwd=claude_session_cwd(path),
        root=root,
        bindings=bindings,
    )


def collect_claude_candidates_from_rows(
    path: Path,
    rows: list[tuple[int, dict[str, Any]]],
    root: Path,
) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    session_id = path.stem
    models = source_models_by_line(path, "claude", rows=rows)
    for line_no, obj in rows:
        if obj.get("type") != "user" or not isinstance(obj.get("message"), dict):
            continue
        if obj.get("isSidechain") or obj.get("isMeta"):
            continue
        content = obj["message"].get("content")
        text, saw_tool = text_from_blocks(content)
        images = image_candidates_from_blocks(content, base_dir=str(path.parent))
        if saw_tool and not text.strip() and not images:
            continue
        text, sanitation = sanitize_prompt(text, backfill=True)
        if (not text and not images) or (text and is_automatic_prompt(text) and not images):
            continue
        occurred = parse_iso(obj.get("timestamp"))
        raw.append(
            {
                "platform": "claude",
                "session_id": session_id,
                "timestamp": iso_z(occurred) if occurred else iso_z(),
                "text": text,
                "images": images,
                "sanitation": sanitation,
                "native_event_id": str(obj.get("uuid") or obj.get("promptId") or "") or None,
                "turn_id": str(obj.get("promptId") or obj.get("uuid") or "") or None,
                "path": str(path),
                "line": line_no,
                "cwd": str(obj.get("cwd") or root),
                "model": models.get(line_no),
            }
        )
    return raw


def collect_claude_candidates_from_paths(
    paths: Iterable[Path],
    root: Path,
    *,
    rows_by_path: dict[str, list[tuple[int, dict[str, Any]]]] | None = None,
) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    bindings = active_session_bindings()
    for path in sorted(set(paths)):
        if not path.is_file() or not claude_source_belongs_to_root(path, root, bindings=bindings):
            continue
        rows = (rows_by_path or {}).get(normalize_path(path))
        rows = rows if rows is not None else list(read_jsonl(path))
        raw.extend(collect_claude_candidates_from_rows(path, rows, root))
    return merge_branch_copies(raw)


def collect_claude_candidates(claude_home: Path, root: Path) -> list[dict[str, Any]]:
    folder = claude_project_dir(claude_home, root)
    if not folder:
        return []
    return collect_claude_candidates_from_paths(folder.glob("*.jsonl"), root)


def max_jsonl_timestamp(path: Path | None) -> dt.datetime | None:
    if not path or not path.exists():
        return None
    maximum = None
    for _, obj in read_jsonl(path):
        timestamp = parse_iso(obj.get("timestamp"))
        if timestamp and (maximum is None or timestamp > maximum):
            maximum = timestamp
    return maximum


def codex_meta_from_path(
    path: Path,
    rows: list[tuple[int, dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    sample = rows if rows is not None and any(obj.get("type") == "session_meta" for _, obj in rows[:16]) else []
    if not sample:
        sample = []
        for line_no, obj in read_jsonl(path):
            sample.append((line_no, obj))
            if line_no >= 16:
                break
    return next(
        (
            obj.get("payload")
            for _, obj in sample[:16]
            if obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict)
        ),
        None,
    )


def codex_meta_belongs_to_root(
    meta: dict[str, Any],
    root: Path,
    *,
    force_session_ids: set[str] | None = None,
    bindings: dict[str, dict[str, Any]] | None = None,
) -> bool:
    session_id = str(meta.get("id") or meta.get("session_id") or "")
    if session_id and force_session_ids and session_id in force_session_ids:
        return True
    binding_map = bindings if bindings is not None else active_session_bindings()
    return session_belongs_to_project_root(
        platform="codex",
        session_id=session_id,
        cwd=meta.get("cwd"),
        root=root,
        bindings=binding_map,
    )


def transcript_source_belongs_to_root(
    path: Path,
    platform: str,
    root: Path,
    *,
    session_id: str | None = None,
    bindings: dict[str, dict[str, Any]] | None = None,
) -> bool:
    if platform == "claude":
        return claude_source_belongs_to_root(
            path,
            root,
            session_id=session_id,
            bindings=bindings,
        )
    if platform == "codex":
        meta = codex_meta_from_path(path)
        return bool(meta and codex_meta_belongs_to_root(meta, root, bindings=bindings))
    return False


def collect_codex_candidates_from_paths(
    paths: Iterable[Path],
    root: Path,
    *,
    rows_by_path: dict[str, list[tuple[int, dict[str, Any]]]] | None = None,
    force_session_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    seen_goal_objectives: set[tuple[str, str]] = set()
    bindings = active_session_bindings()
    for path in sorted(set(paths)):
        if "subagents" in path.parts or not path.is_file():
            continue
        rows = (rows_by_path or {}).get(normalize_path(path))
        meta = codex_meta_from_path(path, rows)
        if not meta or not codex_meta_belongs_to_root(
            meta,
            root,
            force_session_ids=force_session_ids,
            bindings=bindings,
        ):
            continue
        if meta.get("thread_source") == "subagent" or isinstance(meta.get("source"), dict):
            continue
        rows = rows if rows is not None else list(read_jsonl(path))
        models = source_models_by_line(path, "codex", rows=rows)
        session_id = str(meta.get("id") or meta.get("session_id") or path.stem)
        imported = str(meta.get("external_agent_source") or "") == "claude"
        external_path = Path(str(meta.get("external_agent_source_path"))) if meta.get("external_agent_source_path") else None
        original_max = max_jsonl_timestamp(external_path) if imported else None
        import_bootstrap_time = parse_iso(meta.get("timestamp")) if imported else None
        for line_no, obj in rows:
            if obj.get("type") != "response_item" or not isinstance(obj.get("payload"), dict):
                continue
            payload = obj["payload"]
            if payload.get("type") != "message" or payload.get("role") != "user":
                continue
            timestamp = parse_iso(obj.get("timestamp"))
            is_import_bootstrap = bool(
                imported
                and timestamp
                and import_bootstrap_time
                and timestamp == import_bootstrap_time
            )
            if imported and original_max and timestamp and timestamp <= original_max and not is_import_bootstrap:
                continue
            content = payload.get("content")
            text, _ = text_from_blocks(content)
            images = image_candidates_from_blocks(content, base_dir=str(path.parent))
            goal_objective = extract_codex_goal_objective(text)
            if goal_objective:
                goal_key = (session_id, sha256_text(goal_objective))
                if goal_key in seen_goal_objectives:
                    continue
                seen_goal_objectives.add(goal_key)
                text = goal_objective
            text, sanitation = sanitize_prompt(text, backfill=True)
            if (not text and not images) or (
                text
                and is_automatic_prompt(text)
                and not images
                and not is_import_bootstrap
            ):
                continue
            native_event_id = str(payload.get("id") or obj.get("id") or "") or None
            raw.append(
                {
                    "platform": "codex",
                    "session_id": session_id,
                    "timestamp": iso_z(timestamp) if timestamp else iso_z(),
                    "text": text,
                    "images": images,
                    "sanitation": sanitation,
                    "native_event_id": native_event_id,
                    "turn_id": codex_turn_id(payload),
                    "path": str(path),
                    "line": line_no,
                    "cwd": str(meta.get("cwd") or root),
                    "model": models.get(line_no) or str(meta.get("model") or "") or None,
                }
            )
    return merge_branch_copies(raw)


def codex_project_paths(codex_home: Path, root: Path) -> list[Path]:
    paths: set[Path] = set(bound_source_paths("codex", root))
    bindings = active_session_bindings()
    indexed_session_ids = codex_session_ids_from_state(codex_home, root)
    for folder_name in ("sessions", "archived_sessions"):
        folder = codex_home / folder_name
        if not folder.exists():
            continue
        for path in folder.rglob("rollout-*.jsonl"):
            if indexed_session_ids is not None and not any(
                session_id in path.name for session_id in indexed_session_ids
            ):
                continue
            meta = codex_meta_from_path(path)
            if not meta or not codex_meta_belongs_to_root(meta, root, bindings=bindings):
                continue
            paths.add(path)
    return sorted(paths)


def codex_session_ids_from_state(codex_home: Path, root: Path) -> set[str] | None:
    """Use the Codex desktop index to avoid opening every global rollout.

    The database is an optional accelerator. Codex CLI-only installations and
    temporarily unreadable databases fall back to transcript metadata scanning.
    """

    state_path = codex_home / "state_5.sqlite"
    if not state_path.is_file():
        return None
    try:
        uri = f"{state_path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=0.25) as connection:
            rows = connection.execute(
                "SELECT id, cwd FROM threads WHERE id IS NOT NULL AND cwd IS NOT NULL"
            )
            return {
                str(session_id)
                for session_id, cwd in rows
                if session_id and normalize_path(cwd) == normalize_path(root)
            }
    except (OSError, sqlite3.Error):
        return None


def collect_codex_candidates(codex_home: Path, root: Path) -> list[dict[str, Any]]:
    return collect_codex_candidates_from_paths(codex_project_paths(codex_home, root), root)


def trace_text_from_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := trace_text_from_value(item)))
    if isinstance(value, dict):
        for key in ("text", "thinking", "output", "content", "message", "summary"):
            if key in value and (text := trace_text_from_value(value[key])):
                return text
    return ""


def trace_candidate(
    *,
    platform: str,
    session_id: str,
    timestamp: str,
    event_type: str,
    actor_role: str,
    structured: Any,
    path: Path,
    line: int,
    block_index: int = 0,
    raw_type: str | None = None,
    native_event_id: str | None = None,
    turn_id: str | None = None,
    cwd: str | None = None,
    model: str | None = None,
    phase: str | None = None,
    actor_name: str | None = None,
    tool_call_id: str | None = None,
    parent_session_id: str | None = None,
    agent_id: str | None = None,
    is_subagent: bool = False,
    text: str | None = None,
) -> dict[str, Any]:
    clean, sanitation = sanitize_trace_value(structured)
    clean_text = trace_text_from_value(clean) if text is None else sanitize_trace_value(text)[0]
    return {
        "platform": platform,
        "session_id": session_id,
        "timestamp": timestamp,
        "event_type": event_type,
        "actor_role": actor_role,
        "actor_name": actor_name,
        "text": str(clean_text or ""),
        "structured": clean,
        "sanitation": sanitation,
        "native_event_id": native_event_id,
        "turn_id": turn_id,
        "path": str(path),
        "line": line,
        "block_index": block_index,
        "raw_type": raw_type,
        "cwd": cwd,
        "model": model,
        "phase": phase,
        "tool_call_id": tool_call_id,
        "parent_session_id": parent_session_id,
        "agent_id": agent_id,
        "is_subagent": is_subagent,
    }


def collect_claude_model_outputs_from_paths(
    paths: Iterable[Path],
    root: Path,
    *,
    rows_by_path: dict[str, list[tuple[int, dict[str, Any]]]] | None = None,
) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    bindings = active_session_bindings()
    for path in sorted(set(paths)):
        if not path.is_file() or not claude_source_belongs_to_root(path, root, bindings=bindings):
            continue
        rows = (rows_by_path or {}).get(normalize_path(path))
        rows = rows if rows is not None else list(read_jsonl(path))
        rows_by_uuid = {
            str(obj.get("uuid")): obj
            for _, obj in rows
            if obj.get("uuid")
        }

        def claude_turn_anchor(obj: dict[str, Any]) -> str | None:
            current = obj
            seen: set[str] = set()
            while isinstance(current, dict):
                current_uuid = str(current.get("uuid") or "")
                if current_uuid:
                    if current_uuid in seen:
                        break
                    seen.add(current_uuid)
                message = current.get("message") if isinstance(current.get("message"), dict) else {}
                if current.get("type") == "user" and message.get("role") == "user":
                    content = message.get("content")
                    blocks = content if isinstance(content, list) else []
                    tool_only = bool(blocks) and all(
                        isinstance(block, dict) and block.get("type") == "tool_result"
                        for block in blocks
                    )
                    if not tool_only:
                        return str(current.get("promptId") or current.get("uuid") or "") or None
                parent_uuid = str(current.get("parentUuid") or "")
                if not parent_uuid:
                    break
                current = rows_by_uuid.get(parent_uuid, {})
            return None

        session_id = path.stem
        path_is_subagent = path.parent.name == "subagents"
        path_parent_session = path.parent.parent.name if path_is_subagent else None
        for line_no, obj in rows:
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            occurred = parse_iso(obj.get("timestamp"))
            timestamp = iso_z(occurred) if occurred else iso_z()
            native_id = str(obj.get("uuid") or message.get("id") or "") or None
            is_subagent = bool(obj.get("isSidechain") or obj.get("agentId") or path_is_subagent)
            agent_id = str(obj.get("agentId") or path.stem) if is_subagent else None
            parent_session_id = path_parent_session
            if obj.get("type") == "assistant" and message.get("role") == "assistant":
                content = message.get("content")
                blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
                for block_index, block in enumerate(blocks):
                    if not isinstance(block, dict):
                        block = {"type": "text", "text": str(block)}
                    block_type = str(block.get("type") or "assistant_content")
                    event_type = {
                        "thinking": "reasoning",
                        "text": "assistant_text",
                        "tool_use": "tool_call",
                    }.get(block_type, "assistant_content")
                    outputs.append(
                        trace_candidate(
                            platform="claude",
                            session_id=session_id,
                            timestamp=timestamp,
                            event_type=event_type,
                            actor_role="assistant",
                            structured=block,
                            path=path,
                            line=line_no,
                            block_index=block_index,
                            raw_type=f"assistant.{block_type}",
                            native_event_id=native_id,
                            turn_id=claude_turn_anchor(obj),
                            cwd=str(obj.get("cwd") or root),
                            model=normalize_model(message.get("model")),
                            phase="final_answer" if message.get("stop_reason") == "end_turn" else "commentary",
                            tool_call_id=str(block.get("id") or "") or None,
                            parent_session_id=parent_session_id,
                            agent_id=agent_id,
                            is_subagent=is_subagent,
                        )
                    )
            elif obj.get("type") == "user" and message.get("role") == "user":
                content = message.get("content")
                blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
                for block_index, block in enumerate(blocks):
                    if not isinstance(block, dict):
                        block = {"type": "text", "text": str(block)}
                    block_type = str(block.get("type") or "user_content")
                    if block_type == "tool_result":
                        outputs.append(
                            trace_candidate(
                                platform="claude",
                                session_id=session_id,
                                timestamp=timestamp,
                                event_type="tool_result",
                                actor_role="tool",
                                structured=block,
                                path=path,
                                line=line_no,
                                block_index=block_index,
                                raw_type="user.tool_result",
                                native_event_id=native_id,
                                turn_id=claude_turn_anchor(obj),
                                cwd=str(obj.get("cwd") or root),
                                tool_call_id=str(block.get("tool_use_id") or "") or None,
                                parent_session_id=parent_session_id,
                                agent_id=agent_id,
                                is_subagent=is_subagent,
                            )
                        )
                        continue
                    text = trace_text_from_value(block)
                    injections = [
                        match.group(0)
                        for pattern in DROP_BLOCK_PATTERNS
                        for match in pattern.finditer(text)
                    ]
                    for injection_index, injection in enumerate(injections):
                        outputs.append(
                            trace_candidate(
                                platform="claude",
                                session_id=session_id,
                                timestamp=timestamp,
                                event_type="system_instruction",
                                actor_role="system",
                                structured={"type": "injected_instruction", "text": injection},
                                path=path,
                                line=line_no,
                                block_index=block_index * 1000 + injection_index,
                                raw_type="user.injected_instruction",
                                native_event_id=native_id,
                                turn_id=claude_turn_anchor(obj),
                                cwd=str(obj.get("cwd") or root),
                                parent_session_id=parent_session_id,
                                agent_id=agent_id,
                                is_subagent=is_subagent,
                            )
                        )
            elif obj.get("type") == "system":
                outputs.append(
                    trace_candidate(
                        platform="claude",
                        session_id=session_id,
                        timestamp=timestamp,
                        event_type="system_event",
                        actor_role="system",
                        structured=obj,
                        path=path,
                        line=line_no,
                        raw_type="system",
                        native_event_id=native_id,
                        turn_id=claude_turn_anchor(obj),
                        cwd=str(obj.get("cwd") or root),
                        parent_session_id=parent_session_id,
                        agent_id=agent_id,
                        is_subagent=is_subagent,
                    )
                )
    return merge_model_output_copies(outputs)


def collect_codex_model_outputs_from_paths(
    paths: Iterable[Path],
    root: Path,
    *,
    rows_by_path: dict[str, list[tuple[int, dict[str, Any]]]] | None = None,
    force_session_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    bindings = active_session_bindings()
    for path in sorted(set(paths)):
        if not path.is_file():
            continue
        rows = (rows_by_path or {}).get(normalize_path(path))
        meta = codex_meta_from_path(path, rows)
        if not meta or not codex_meta_belongs_to_root(
            meta,
            root,
            force_session_ids=force_session_ids,
            bindings=bindings,
        ):
            continue
        rows = rows if rows is not None else list(read_jsonl(path))
        models = source_models_by_line(path, "codex", rows=rows)
        session_id = str(meta.get("id") or meta.get("session_id") or path.stem)
        imported = str(meta.get("external_agent_source") or "") == "claude"
        external_path = Path(str(meta.get("external_agent_source_path"))) if meta.get("external_agent_source_path") else None
        original_max = max_jsonl_timestamp(external_path) if imported else None
        source_meta = meta.get("source") if isinstance(meta.get("source"), dict) else {}
        spawn = (
            source_meta.get("subagent", {}).get("thread_spawn", {})
            if isinstance(source_meta.get("subagent"), dict)
            else {}
        )
        is_subagent = bool(meta.get("thread_source") == "subagent" or source_meta)
        parent_session_id = str(spawn.get("parent_thread_id") or "") or None
        agent_id = str(spawn.get("agent_nickname") or session_id) if is_subagent else None
        for line_no, obj in rows:
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            timestamp = parse_iso(obj.get("timestamp"))
            if imported and original_max and timestamp and timestamp <= original_max:
                continue
            occurred_at = iso_z(timestamp) if timestamp else iso_z()
            if obj.get("type") == "turn_context":
                outputs.append(
                    trace_candidate(
                        platform="codex",
                        session_id=session_id,
                        timestamp=occurred_at,
                        event_type="system_instruction",
                        actor_role="system",
                        structured=payload,
                        path=path,
                        line=line_no,
                        raw_type="turn_context",
                        turn_id=str(payload.get("turn_id") or "") or None,
                        cwd=str(payload.get("cwd") or meta.get("cwd") or root),
                        model=normalize_model(payload.get("model")),
                        parent_session_id=parent_session_id,
                        agent_id=agent_id,
                        is_subagent=is_subagent,
                    )
                )
                continue
            if obj.get("type") != "response_item":
                continue
            payload_type = str(payload.get("type") or "response_item")
            native_id = str(payload.get("id") or obj.get("id") or "") or None
            common = {
                "platform": "codex",
                "session_id": session_id,
                "timestamp": occurred_at,
                "path": path,
                "line": line_no,
                "native_event_id": native_id,
                "turn_id": codex_turn_id(payload),
                "cwd": str(meta.get("cwd") or root),
                "model": models.get(line_no) or normalize_model(meta.get("model")),
                "phase": str(payload.get("phase") or "") or None,
                "parent_session_id": parent_session_id,
                "agent_id": agent_id,
                "is_subagent": is_subagent,
            }
            if payload_type == "message":
                role = str(payload.get("role") or "unknown")
                content = payload.get("content")
                blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
                for block_index, block in enumerate(blocks):
                    if not isinstance(block, dict):
                        block = {"type": "text", "text": str(block)}
                    block_type = str(block.get("type") or "message_content")
                    text = trace_text_from_value(block)
                    if role == "user" and not is_automatic_prompt(text):
                        continue
                    event_type = {
                        "assistant": "assistant_text",
                        "developer": "developer_instruction",
                        "system": "system_instruction",
                        "user": "system_instruction",
                    }.get(role, "message")
                    outputs.append(
                        trace_candidate(
                            event_type=event_type,
                            actor_role=role,
                            structured=block,
                            block_index=block_index,
                            raw_type=f"message.{role}.{block_type}",
                            **common,
                        )
                    )
                continue
            if payload_type == "reasoning":
                event_type, actor_role = "reasoning", "assistant"
            elif payload_type.endswith("_output") or payload_type in {"function_call_output"}:
                event_type, actor_role = "tool_result", "tool"
            elif payload_type.endswith("_call") or payload_type in {"function_call"}:
                event_type, actor_role = "tool_call", "assistant"
            else:
                event_type, actor_role = "agent_event", "assistant"
            outputs.append(
                trace_candidate(
                    event_type=event_type,
                    actor_role=actor_role,
                    structured=payload,
                    raw_type=payload_type,
                    tool_call_id=str(payload.get("call_id") or "") or None,
                    actor_name=str(payload.get("name") or "") or None,
                    **common,
                )
            )
    return merge_model_output_copies(outputs)


def merge_model_output_copies(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in items:
        key = (
            str(item.get("platform") or ""),
            str(item.get("session_id") or ""),
            str(item.get("native_event_id") or ""),
            str(item.get("event_type") or ""),
            str(item.get("block_index") or 0),
            trace_content_hash(str(item.get("text") or ""), item.get("structured")),
        )
        unique.setdefault(key, item)
    return sorted(
        unique.values(),
        key=lambda item: (
            str(item.get("timestamp") or ""),
            str(item.get("platform") or ""),
            str(item.get("session_id") or ""),
            str(item.get("path") or ""),
            int(item.get("line") or 0),
        ),
    )


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
        known_images = {
            (str(candidate.get("kind") or ""), str(candidate.get("value") or ""))
            for candidate in merged.get("images", [])
        }
        for candidate in item.get("images", []):
            image_key = (str(candidate.get("kind") or ""), str(candidate.get("value") or ""))
            if image_key not in known_images:
                merged.setdefault("images", []).append(candidate)
                known_images.add(image_key)
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


def reconcile_candidates(
    root: Path,
    store: Path,
    candidates: list[dict[str, Any]],
    *,
    rebuild_index: bool,
    full_dataset: bool,
) -> dict[str, Any]:
    """Merge source candidates into the append-only ledger.

    A Codex turn may contain more than one distinct human message, so turn
    identity always includes the prompt hash. Occurrence-count fallback is safe
    only for a complete historical candidate set, never for an incremental
    tail containing just the newest repeated prompt.
    """
    superseded = repair_legacy_image_duplicates(store)
    excluded = repair_automatic_context_events(store)
    excluded_out_of_scope = repair_out_of_scope_events(root, store)
    existing = list(iter_active_events(store))
    existing_ids = {str(event.get("event_id")) for event in existing}
    existing_by_key: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = collections.defaultdict(list)
    existing_by_native: dict[tuple[str, str], dict[str, Any]] = {}
    existing_by_turn: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    existing_by_source: dict[tuple[str, str, int], dict[str, Any]] = {}
    existing_by_time: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for event in existing:
        if event.get("record_type") != RECORD_TYPE:
            continue
        source = event.get("source", {})
        session = event.get("session", {})
        platform_name = str(source.get("platform") or "")
        session_id = str(session.get("id") or "")
        prompt_hash = str(event.get("prompt", {}).get("sha256") or "")
        event_key = (platform_name, session_id, prompt_hash)
        existing_by_key[event_key].append(event)
        native = str(source.get("native_event_id") or "")
        turn = str(session.get("turn_id") or "")
        source_path = str(source.get("path") or "")
        source_line = source.get("line")
        if native:
            existing_by_native.setdefault((platform_name, native), event)
        if turn and prompt_hash:
            existing_by_turn.setdefault((platform_name, session_id, turn, prompt_hash), event)
        if source_path and source_line:
            existing_by_source.setdefault((platform_name, normalize_path(source_path), int(source_line)), event)
        if prompt_hash and event.get("occurred_at"):
            existing_by_time.setdefault(
                (platform_name, session_id, prompt_hash, str(event.get("occurred_at"))),
                event,
            )
    for items in existing_by_key.values():
        items.sort(key=event_order_key)

    candidates.sort(key=lambda item: (item["timestamp"], item["platform"], item["session_id"]))
    claimed_event_ids: set[str] = set()
    added = skipped = image_seen = image_saved = image_omitted = 0
    for item in candidates:
        prompt_hash = sha256_text(item["text"])
        platform_name = str(item["platform"])
        session_id = str(item["session_id"])
        prior_identity = None
        if item.get("native_event_id"):
            prior_identity = existing_by_native.get((platform_name, str(item["native_event_id"])))
            if prior_identity and str(prior_identity.get("event_id") or "") in claimed_event_ids:
                prior_identity = None
        if prior_identity is None and item.get("path") and item.get("line"):
            prior_identity = existing_by_source.get(
                (platform_name, normalize_path(item["path"]), int(item["line"]))
            )
            if prior_identity and str(prior_identity.get("event_id") or "") in claimed_event_ids:
                prior_identity = None
        if prior_identity is None and item.get("turn_id"):
            turn_match = existing_by_turn.get(
                (platform_name, session_id, str(item["turn_id"]), prompt_hash)
            )
            if turn_match and str(turn_match.get("event_id") or "") not in claimed_event_ids:
                turn_source = turn_match.get("source", {})
                candidate_has_source = bool(item.get("path") and item.get("line"))
                turn_has_source = bool(turn_source.get("path") and turn_source.get("line"))
                if not candidate_has_source or not turn_has_source:
                    prior_identity = turn_match
        if prior_identity is None:
            prior_identity = existing_by_time.get(
                (platform_name, session_id, prompt_hash, str(item["timestamp"]))
            )
            if prior_identity and str(prior_identity.get("event_id") or "") in claimed_event_ids:
                prior_identity = None
        count_key = (platform_name, session_id, prompt_hash)
        if prior_identity is None and full_dataset:
            prior_identity = next(
                (
                    event
                    for event in existing_by_key[count_key]
                    if str(event.get("event_id") or "") not in claimed_event_ids
                ),
                None,
            )
        if prior_identity is not None:
            claimed_event_ids.add(str(prior_identity.get("event_id") or ""))
            image_result = persist_prompt_images(
                store,
                str(prior_identity.get("event_id") or ""),
                item.get("images") or [],
                source_path=item.get("path"),
                source_line=item.get("line"),
            )
            image_seen += image_result["seen"]
            image_saved += image_result["saved"]
            image_omitted += image_result["omitted"]
            skipped += 1
            continue
        event = build_event(
            root=root,
            platform=platform_name,
            source_mode="backfill",
            prompt_text=item["text"],
            session_id=session_id,
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
            existing_by_key[count_key].append(event)
            claimed_event_ids.add(str(event.get("event_id") or ""))
            if item.get("native_event_id"):
                existing_by_native[(platform_name, str(item["native_event_id"]))] = event
            if item.get("turn_id"):
                existing_by_turn[(platform_name, session_id, str(item["turn_id"]), prompt_hash)] = event
            if item.get("path") and item.get("line"):
                existing_by_source[(platform_name, normalize_path(item["path"]), int(item["line"]))] = event
            existing_by_time[(platform_name, session_id, prompt_hash, str(item["timestamp"]))] = event
            image_result = persist_prompt_images(
                store,
                event["event_id"],
                item.get("images") or [],
                source_path=item.get("path"),
                source_line=item.get("line"),
            )
            image_seen += image_result["seen"]
            image_saved += image_result["saved"]
            image_omitted += image_result["omitted"]
        else:
            skipped += 1
    index_rebuilt = bool(rebuild_index and (full_dataset or index_is_dirty(store)))
    if index_rebuilt:
        rebuild_index_for_store(store)
    return {
        "project": str(root),
        "candidates": len(candidates),
        "added": added,
        "skipped": skipped,
        "images": {"seen": image_seen, "saved": image_saved, "omitted": image_omitted},
        "superseded_legacy_events": superseded,
        "excluded_automatic_events": excluded,
        "excluded_out_of_scope_events": excluded_out_of_scope,
        "index_rebuilt": index_rebuilt,
    }


def prompt_event_for_model_output(
    prompts: list[dict[str, Any]],
    candidate: dict[str, Any],
) -> str | None:
    platform = str(candidate.get("platform") or "")
    session_id = str(candidate.get("session_id") or "")
    parent_session_id = str(candidate.get("parent_session_id") or "")
    candidate_sessions = {value for value in (session_id, parent_session_id) if value}
    turn_id = str(candidate.get("turn_id") or "")
    source_path = normalize_path(candidate.get("path"))
    source_line = int(candidate.get("line") or 0)
    turn_matches: list[dict[str, Any]] = []
    preceding: list[tuple[int, dict[str, Any]]] = []
    temporal_preceding: list[dict[str, Any]] = []
    for event in prompts:
        source = event.get("source") if isinstance(event.get("source"), dict) else {}
        session = event.get("session") if isinstance(event.get("session"), dict) else {}
        if str(source.get("platform") or "") != platform:
            continue
        event_session = str(session.get("id") or "")
        aliases = {str(value) for value in session.get("alias_ids", []) if value}
        if not (candidate_sessions & ({event_session} | aliases)):
            continue
        if turn_id and str(session.get("turn_id") or "") == turn_id:
            turn_matches.append(event)
        references = [
            (str(source.get("path") or ""), source.get("line")),
            *[
                (str(item.get("path") or ""), item.get("line"))
                for item in source.get("refs", [])
                if isinstance(item, dict)
            ],
        ]
        for path_value, line_value in references:
            try:
                line_number = int(line_value)
            except (TypeError, ValueError):
                continue
            if source_path and normalize_path(path_value) == source_path and line_number < source_line:
                preceding.append((line_number, event))
        if str(event.get("occurred_at") or "") <= str(candidate.get("timestamp") or ""):
            temporal_preceding.append(event)
    if turn_matches:
        return str(max(turn_matches, key=event_order_key).get("event_id") or "") or None
    if preceding:
        return str(max(preceding, key=lambda item: item[0])[1].get("event_id") or "") or None
    if temporal_preceding:
        return str(max(temporal_preceding, key=event_order_key).get("event_id") or "") or None
    return None


def reconcile_model_outputs(
    root: Path,
    store: Path,
    candidates: list[dict[str, Any]],
) -> dict[str, int]:
    config = read_json_object(store / "config.json")
    privacy = config.get("privacy") if isinstance(config.get("privacy"), dict) else {}
    if not bool(privacy.get("store_agent_trace", privacy.get("store_model_outputs", True))):
        return {
            "model_output_candidates": len(candidates),
            "model_outputs_added": 0,
            "model_outputs_skipped": len(candidates),
        }
    prompts = list(iter_active_events(store))
    events: list[dict[str, Any]] = []
    for candidate in candidates:
        events.append(
            build_model_output_event(
                root=root,
                platform=str(candidate["platform"]),
                session_id=str(candidate["session_id"]),
                occurred_at=str(candidate["timestamp"]),
                event_type=str(candidate["event_type"]),
                actor_role=str(candidate["actor_role"]),
                output_text=str(candidate["text"]),
                structured=candidate.get("structured"),
                source_path=str(candidate["path"]),
                source_line=int(candidate["line"]),
                block_index=int(candidate.get("block_index") or 0),
                raw_type=candidate.get("raw_type"),
                native_event_id=candidate.get("native_event_id"),
                turn_id=candidate.get("turn_id"),
                cwd=candidate.get("cwd"),
                model=candidate.get("model"),
                phase=candidate.get("phase"),
                prompt_event_id=prompt_event_for_model_output(prompts, candidate),
                sanitation=candidate.get("sanitation"),
                actor_name=candidate.get("actor_name"),
                tool_call_id=candidate.get("tool_call_id"),
                parent_session_id=candidate.get("parent_session_id"),
                agent_id=candidate.get("agent_id"),
                is_subagent=bool(candidate.get("is_subagent")),
            )
        )
    added, skipped = append_model_outputs_bulk(store, events)
    return {
        "model_output_candidates": len(candidates),
        "model_outputs_added": added,
        "model_outputs_skipped": skipped,
    }


def source_cursor_file(store: Path) -> Path:
    return store / "state" / "source-cursors.json"


def read_source_cursors(store: Path) -> dict[str, Any]:
    state = read_json_object(source_cursor_file(store))
    if not isinstance(state.get("sources"), dict):
        state["sources"] = {}
    return state


def source_snapshot(path: Path, platform: str, session_id: str | None = None) -> dict[str, Any] | None:
    try:
        stat = path.stat()
        if not path.is_file():
            return None
        with path.open("rb") as handle:
            newline_count = 0
            last_byte = b""
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                newline_count += chunk.count(b"\n")
                last_byte = chunk[-1:]
    except OSError:
        return None
    trailing_newline = stat.st_size == 0 or last_byte == b"\n"
    return {
        "path": str(path.resolve()),
        "platform": platform,
        "session_id": session_id or path.stem,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "byte_offset": stat.st_size,
        "line_count": newline_count + (1 if stat.st_size and not trailing_newline else 0),
        "trailing_newline": trailing_newline,
    }


def source_paths_from_candidates(candidates: Iterable[dict[str, Any]]) -> dict[str, tuple[Path, str, str]]:
    paths: dict[str, tuple[Path, str, str]] = {}
    for item in candidates:
        platform = str(item.get("platform") or "unknown")
        session_id = str(item.get("session_id") or "unknown")
        references = list(item.get("source_refs") or [])
        if item.get("path"):
            references.append({"path": item["path"], "session_id": session_id})
        for reference in references:
            value = reference.get("path") if isinstance(reference, dict) else None
            if value:
                path = Path(str(value))
                paths[normalize_path(path)] = (
                    path,
                    platform,
                    str(reference.get("session_id") or session_id),
                )
    return paths


def write_source_cursors(
    store: Path,
    sources: dict[str, tuple[Path, str, str]],
    *,
    previous: dict[str, Any] | None = None,
    full_scan: bool = False,
) -> dict[str, Any]:
    state = dict(previous or {})
    snapshots = dict(state.get("sources") or {})
    for key, (path, platform, session_id) in sources.items():
        snapshot = source_snapshot(path, platform, session_id)
        if snapshot:
            snapshots[key] = snapshot
    now = iso_z()
    state.update(
        {
            "schema_version": "1.0.0",
            "initialized": True,
            "updated_at": now,
            "sources": snapshots,
        }
    )
    if full_scan:
        state["last_full_scan_at"] = now
    write_json(source_cursor_file(store), state)
    return state


def write_cursor_snapshots(
    store: Path,
    snapshots: dict[str, dict[str, Any]],
    *,
    full_scan: bool,
    scanned_platforms: set[str] | None = None,
) -> dict[str, Any]:
    state = read_source_cursors(store)
    prior_sources = dict(state.get("sources") or {})
    if full_scan and scanned_platforms:
        sources = {
            key: value
            for key, value in prior_sources.items()
            if not isinstance(value, dict) or str(value.get("platform") or "unknown") not in scanned_platforms
        }
        sources.update(snapshots)
    elif full_scan:
        sources = dict(snapshots)
    else:
        sources = prior_sources
        sources.update(snapshots)
    now = iso_z()
    state.update(
        {
            "schema_version": "1.0.0",
            "initialized": True,
            "updated_at": now,
            "sources": sources,
        }
    )
    if full_scan:
        state["last_full_scan_at"] = now
    write_json(source_cursor_file(store), state)
    return state


def read_jsonl_tail(
    path: Path,
    cursor: dict[str, Any] | None,
) -> tuple[list[tuple[int, dict[str, Any]]], int, dict[str, Any] | None]:
    """Read only complete UTF-8 JSONL records appended after a saved cursor."""
    prior = cursor or {}
    try:
        stat = path.stat()
    except OSError:
        return [], 0, None
    offset = int(prior.get("byte_offset") or 0)
    prior_lines = int(prior.get("line_count") or 0)
    if stat.st_size < offset or (offset and not bool(prior.get("trailing_newline", True))):
        snapshot = source_snapshot(
            path,
            str(prior.get("platform") or "unknown"),
            str(prior.get("session_id") or path.stem),
        )
        rows = list(read_jsonl(path))
        return rows, int((snapshot or {}).get("size") or stat.st_size), snapshot
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            raw = handle.read()
    except OSError:
        return [], 0, None
    if not raw:
        snapshot = dict(prior)
        snapshot.update({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        return [], 0, snapshot
    complete_size = raw.rfind(b"\n") + 1
    complete = raw[:complete_size]
    rows: list[tuple[int, dict[str, Any]]] = []
    for index, line in enumerate(complete.splitlines(), 1):
        try:
            value = json.loads(line.decode("utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(value, dict):
            rows.append((prior_lines + index, value))
    consumed_lines = complete.count(b"\n")
    new_offset = offset + complete_size
    snapshot = {
        "path": str(path.resolve()),
        "platform": str(prior.get("platform") or "unknown"),
        "session_id": str(prior.get("session_id") or path.stem),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "byte_offset": new_offset,
        "line_count": prior_lines + consumed_lines,
        "trailing_newline": new_offset == stat.st_size,
    }
    return rows, complete_size, snapshot


def infer_source_platform(path: Path, fallback: str = "unknown") -> str:
    normalized = normalize_path(path)
    if normalize_path(Path.home() / ".claude") in normalized:
        return "claude"
    if normalize_path(Path.home() / ".codex") in normalized:
        return "codex"
    return fallback if fallback in {"claude", "codex"} else "unknown"


def incremental_backfill_project(
    root: Path,
    *,
    platform: str,
    source_platform: str,
    session_id: str,
    source_paths: Iterable[Path] = (),
    claude_home: Path | None = None,
    codex_home: Path | None = None,
    rebuild_index: bool = True,
) -> dict[str, Any]:
    root = root.resolve()
    if is_unsafe_broad_project_root(root):
        raise ValueError(f"Refusing broad project root: {root}")
    store, _ = init_store(root)
    cursor_state = read_source_cursors(store)
    if not bool(cursor_state.get("initialized")):
        return backfill_project(
            root,
            platform=platform,
            claude_home=claude_home,
            codex_home=codex_home,
            rebuild_index=rebuild_index,
        )

    known = dict(cursor_state.get("sources") or {})
    paths: dict[str, tuple[Path, str, str]] = {}
    eligible_known: dict[str, dict[str, Any]] = {}
    preserved_unselected: dict[str, dict[str, Any]] = {}
    bindings = active_session_bindings()
    for key, cursor in known.items():
        if not isinstance(cursor, dict) or not cursor.get("path"):
            continue
        source_kind = str(cursor.get("platform") or "unknown")
        if platform != "all" and source_kind != platform:
            preserved_unselected[key] = cursor
            continue
        path = Path(str(cursor["path"]))
        source_session_id = str(cursor.get("session_id") or path.stem)
        if not transcript_source_belongs_to_root(
            path,
            source_kind,
            root,
            session_id=source_session_id,
            bindings=bindings,
        ):
            continue
        eligible_known[key] = cursor
        paths[key] = (path, source_kind, source_session_id)

    for value in source_paths:
        path = Path(value).expanduser()
        kind = infer_source_platform(path, source_platform)
        if kind == "unknown" or (platform != "all" and kind != platform):
            continue
        if not transcript_source_belongs_to_root(
            path,
            kind,
            root,
            session_id=session_id or path.stem,
            bindings=bindings,
        ):
            continue
        paths[normalize_path(path)] = (path, kind, session_id or path.stem)

    if source_platform == "codex" and not any(kind == "codex" and sid == session_id for _, kind, sid in paths.values()):
        rollout = find_codex_rollout(codex_home or (Path.home() / ".codex"), session_id)
        if rollout and transcript_source_belongs_to_root(
            rollout,
            "codex",
            root,
            session_id=session_id,
            bindings=bindings,
        ):
            paths[normalize_path(rollout)] = (rollout, "codex", session_id)

    # Claude keeps all project sessions in one small direct folder. Listing that
    # folder discovers new sessions without walking the global transcript tree.
    if platform in {"all", "claude"}:
        folder = claude_project_dir(claude_home or (Path.home() / ".claude"), root)
        if folder:
            for path in folder.rglob("*.jsonl"):
                if not claude_source_belongs_to_root(path, root, bindings=bindings):
                    continue
                paths[normalize_path(path)] = (path, "claude", path.stem)

    # Codex stores every task in a global dated rollout tree rather than a
    # project-specific folder. Prefer its optional desktop thread index to
    # select exact-root session IDs before opening transcript metadata. CLI-only
    # installations fall back to metadata scanning.
    if platform in {"all", "codex"} and source_platform in {"codex", "unknown"}:
        codex_base = codex_home or (Path.home() / ".codex")
        for path in codex_project_paths(codex_base, root):
            key = normalize_path(path)
            if key in known or key in paths:
                continue
            meta = codex_meta_from_path(path)
            if not meta:
                continue
            source_session_id = str(meta.get("id") or meta.get("session_id") or path.stem)
            paths[key] = (path, "codex", source_session_id)

    rows_by_platform: dict[str, dict[str, list[tuple[int, dict[str, Any]]]]] = {
        "claude": {},
        "codex": {},
    }
    changed_paths: dict[str, tuple[Path, str, str]] = {}
    updated_sources = {**preserved_unselected, **eligible_known}
    bytes_read = 0
    for key, (path, kind, source_session_id) in paths.items():
        try:
            stat = path.stat()
        except OSError:
            continue
        prior = known.get(key) if isinstance(known.get(key), dict) else {}
        prior = dict(prior)
        prior.update({"platform": kind, "session_id": source_session_id})
        unchanged = (
            int(prior.get("size") or -1) == stat.st_size
            and int(prior.get("mtime_ns") or -1) == stat.st_mtime_ns
            and int(prior.get("byte_offset") or 0) == stat.st_size
        )
        if unchanged:
            continue
        if prior and stat.st_size == int(prior.get("size") or -1):
            # Same-size rewrites cannot be represented as an append cursor.
            prior.update({"byte_offset": 0, "line_count": 0, "trailing_newline": True})
        rows, read_bytes, snapshot = read_jsonl_tail(path, prior)
        bytes_read += read_bytes
        if snapshot:
            snapshot.update({"platform": kind, "session_id": source_session_id})
            updated_sources[key] = snapshot
        if rows:
            rows_by_platform[kind][key] = rows
            changed_paths[key] = (path, kind, source_session_id)

    candidates: list[dict[str, Any]] = []
    output_candidates: list[dict[str, Any]] = []
    claude_paths = [path for key, (path, kind, _) in changed_paths.items() if kind == "claude"]
    codex_paths = [path for key, (path, kind, _) in changed_paths.items() if kind == "codex"]
    if claude_paths:
        candidates.extend(
            collect_claude_candidates_from_paths(
                claude_paths,
                root,
                rows_by_path=rows_by_platform["claude"],
            )
        )
        output_candidates.extend(
            collect_claude_model_outputs_from_paths(
                claude_paths,
                root,
                rows_by_path=rows_by_platform["claude"],
            )
        )
    if codex_paths:
        candidates.extend(
            collect_codex_candidates_from_paths(
                codex_paths,
                root,
                rows_by_path=rows_by_platform["codex"],
            )
        )
        output_candidates.extend(
            collect_codex_model_outputs_from_paths(
                codex_paths,
                root,
                rows_by_path=rows_by_platform["codex"],
            )
        )
    result = reconcile_candidates(
        root,
        store,
        candidates,
        rebuild_index=False,
        full_dataset=False,
    )
    output_result = reconcile_model_outputs(root, store, output_candidates)
    index_rebuilt = bool(rebuild_index and index_is_dirty(store))
    if index_rebuilt:
        rebuild_index_for_store(store)
    cursor_state.update(
        {
            "schema_version": "1.0.0",
            "initialized": True,
            "updated_at": iso_z(),
            "sources": updated_sources,
        }
    )
    write_json(source_cursor_file(store), cursor_state)
    return {
        "mode": "incremental",
        "sources_known": len(paths),
        "sources_changed": len(changed_paths),
        "bytes_read": bytes_read,
        **result,
        **output_result,
        "index_rebuilt": index_rebuilt,
    }


def backfill_project(
    root: Path,
    *,
    platform: str = "all",
    claude_home: Path | None = None,
    codex_home: Path | None = None,
    rebuild_index: bool = True,
) -> dict[str, Any]:
    root = root.resolve()
    if is_unsafe_broad_project_root(root):
        raise ValueError(f"Refusing broad project root: {root}")
    store, _ = init_store(root)
    candidates: list[dict[str, Any]] = []
    output_candidates: list[dict[str, Any]] = []
    sources: dict[str, tuple[Path, str, str]] = {}
    claude_base = claude_home or (Path.home() / ".claude")
    codex_base = codex_home or (Path.home() / ".codex")
    claude_paths: list[Path] = []
    codex_paths: list[Path] = []
    if platform in {"all", "claude"}:
        folder = claude_project_dir(claude_base, root)
        bindings = active_session_bindings()
        claude_paths = sorted(
            path
            for path in set((list(folder.rglob("*.jsonl")) if folder else []) + bound_source_paths("claude", root))
            if claude_source_belongs_to_root(path, root, bindings=bindings)
        )
        for path in claude_paths:
            sources[normalize_path(path)] = (path, "claude", path.stem)
    if platform in {"all", "codex"}:
        codex_paths = codex_project_paths(codex_base, root)
        for path in codex_paths:
            meta = codex_meta_from_path(path) or {}
            sources[normalize_path(path)] = (
                path,
                "codex",
                str(meta.get("id") or meta.get("session_id") or path.stem),
            )
    # Snapshot before reconciliation completes. If a source grows concurrently,
    # the next incremental pass starts from this earlier safe offset and may
    # reread an already-reconciled row, but it cannot skip an unseen row.
    snapshots = {
        key: snapshot
        for key, (path, source_kind, source_session_id) in sources.items()
        if (snapshot := source_snapshot(path, source_kind, source_session_id)) is not None
    }
    if claude_paths:
        candidates.extend(collect_claude_candidates_from_paths(claude_paths, root))
        output_candidates.extend(collect_claude_model_outputs_from_paths(claude_paths, root))
    if codex_paths:
        candidates.extend(collect_codex_candidates_from_paths(codex_paths, root))
        output_candidates.extend(collect_codex_model_outputs_from_paths(codex_paths, root))
    result = reconcile_candidates(
        root,
        store,
        candidates,
        rebuild_index=False,
        full_dataset=True,
    )
    output_result = reconcile_model_outputs(root, store, output_candidates)
    index_rebuilt = bool(rebuild_index)
    if index_rebuilt:
        rebuild_index_for_store(store)
    write_cursor_snapshots(
        store,
        snapshots,
        full_scan=True,
        scanned_platforms={"claude", "codex"} if platform == "all" else {platform},
    )
    return {
        "mode": "full",
        "sources_scanned": len(sources),
        **result,
        **output_result,
        "index_rebuilt": index_rebuilt,
    }


def backfill(args: argparse.Namespace) -> int:
    root = find_project_root(Path(args.project or os.getcwd()), Path(args.project) if args.project else None)
    result = backfill_project(
        root,
        platform=args.platform,
        claude_home=Path(args.claude_home),
        codex_home=Path(args.codex_home),
        rebuild_index=args.rebuild_index,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def find_claude_transcript(claude_home: Path, session_id: str, hinted: Path | None = None) -> Path | None:
    if hinted and hinted.is_file():
        return hinted
    projects = claude_home / "projects"
    if not projects.exists():
        return None
    candidates = list(projects.glob(f"*/{session_id}.jsonl"))
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def resolve_session_source(
    *,
    platform: str,
    session_id: str,
    source_path: Path | None,
    claude_home: Path,
    codex_home: Path,
) -> Path | None:
    hinted = source_path.expanduser() if source_path else None
    if platform == "codex":
        return find_codex_rollout(codex_home, session_id, hinted)
    return find_claude_transcript(claude_home, session_id, hinted)


def event_match_indexes(events: Iterable[dict[str, Any]]) -> dict[str, dict[Any, dict[str, Any]]]:
    indexes: dict[str, dict[Any, dict[str, Any]]] = {
        "event": {},
        "native": {},
        "source": {},
        "turn": {},
        "time": {},
    }
    for event in events:
        event_id = str(event.get("event_id") or "")
        source = event.get("source") if isinstance(event.get("source"), dict) else {}
        session = event.get("session") if isinstance(event.get("session"), dict) else {}
        platform = str(source.get("platform") or "")
        session_id = str(session.get("id") or "")
        prompt_hash = str(event.get("prompt", {}).get("sha256") or "")
        native = str(source.get("native_event_id") or "")
        source_path = str(source.get("path") or "")
        source_line = source.get("line")
        turn_id = str(session.get("turn_id") or "")
        occurred_at = str(event.get("occurred_at") or "")
        if event_id:
            indexes["event"].setdefault(event_id, event)
        if native:
            indexes["native"].setdefault((platform, native), event)
        if source_path and source_line:
            indexes["source"].setdefault((platform, normalize_path(source_path), int(source_line)), event)
        if turn_id and prompt_hash:
            indexes["turn"].setdefault((platform, session_id, turn_id, prompt_hash), event)
        if occurred_at and prompt_hash:
            indexes["time"].setdefault((platform, session_id, occurred_at, prompt_hash), event)
    return indexes


def matching_event(
    event: dict[str, Any],
    indexes: dict[str, dict[Any, dict[str, Any]]],
) -> dict[str, Any] | None:
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    session = event.get("session") if isinstance(event.get("session"), dict) else {}
    platform = str(source.get("platform") or "")
    session_id = str(session.get("id") or "")
    prompt_hash = str(event.get("prompt", {}).get("sha256") or "")
    native = str(source.get("native_event_id") or "")
    source_path = str(source.get("path") or "")
    source_line = source.get("line")
    turn_id = str(session.get("turn_id") or "")
    occurred_at = str(event.get("occurred_at") or "")
    probes = [indexes["event"].get(str(event.get("event_id") or ""))]
    if native:
        probes.append(indexes["native"].get((platform, native)))
    if source_path and source_line:
        probes.append(indexes["source"].get((platform, normalize_path(source_path), int(source_line))))
    if turn_id and prompt_hash:
        probes.append(indexes["turn"].get((platform, session_id, turn_id, prompt_hash)))
    if occurred_at and prompt_hash:
        probes.append(indexes["time"].get((platform, session_id, occurred_at, prompt_hash)))
    return next((candidate for candidate in probes if candidate), None)


def registered_project_stores() -> list[tuple[Path, Path]]:
    data = read_json_object(registry_path())
    projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
    stores: list[tuple[Path, Path]] = []
    for item in projects.values():
        if not isinstance(item, dict) or not item.get("root") or not item.get("store"):
            continue
        root = Path(str(item["root"])).expanduser()
        store = Path(str(item["store"])).expanduser()
        if store.is_dir():
            stores.append((root, store))
    return stores


def reassign_session_events(
    *,
    platform: str,
    session_id: str,
    destination_root: Path,
    destination_store: Path,
) -> dict[str, int]:
    destination_events = [
        event
        for event in iter_active_events(destination_store)
        if str(event.get("source", {}).get("platform") or "") == platform
        and str(event.get("session", {}).get("id") or "") == session_id
    ]
    indexes = event_match_indexes(destination_events)
    images_copied = exclusions_added = stores_changed = 0
    reason = f"session_reassigned_to_{project_id(destination_root)}"
    for source_root, source_store in registered_project_stores():
        if normalize_path(source_root) == normalize_path(destination_root):
            continue
        source_images: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for image in iter_prompt_images(source_store):
            source_images[str(image.get("event_id") or "")].append(image)
        changed = False
        for source_event in list(iter_active_events(source_store)):
            if str(source_event.get("source", {}).get("platform") or "") != platform:
                continue
            if str(source_event.get("session", {}).get("id") or "") != session_id:
                continue
            destination_event = matching_event(source_event, indexes)
            if not destination_event:
                continue
            for image in source_images.get(str(source_event.get("event_id") or ""), []):
                asset_path = source_store / str(image.get("asset", {}).get("path") or "")
                if not asset_path.is_file():
                    continue
                copied = persist_prompt_images(
                    destination_store,
                    str(destination_event.get("event_id") or ""),
                    [{"kind": "local_path", "value": str(asset_path), "name": asset_path.name}],
                    source_path=str(source_event.get("source", {}).get("path") or "") or None,
                    source_line=source_event.get("source", {}).get("line"),
                )
                images_copied += copied["saved"]
            if append_event_exclusion(
                source_store,
                event_id=str(source_event.get("event_id") or ""),
                reason=reason,
            ):
                exclusions_added += 1
                changed = True
        if changed:
            rebuild_index_for_store(source_store)
            stores_changed += 1
    if images_copied:
        rebuild_index_for_store(destination_store)
    return {
        "source_stores_changed": stores_changed,
        "source_exclusions_added": exclusions_added,
        "source_images_copied": images_copied,
    }


def migrate_bound_session(
    *,
    platform: str,
    session_id: str,
    project_root: Path,
    source_path: Path,
    claude_home: Path,
    codex_home: Path,
) -> dict[str, Any]:
    root = project_root.expanduser().resolve()
    store, _ = init_store(root)
    if platform == "codex":
        candidates = collect_codex_candidates_from_paths(
            [source_path],
            root,
            force_session_ids={session_id},
        )
        output_candidates = collect_codex_model_outputs_from_paths(
            [source_path],
            root,
            force_session_ids={session_id},
        )
    else:
        candidates = collect_claude_candidates_from_paths([source_path], root)
        output_candidates = collect_claude_model_outputs_from_paths([source_path], root)
    candidates = [item for item in candidates if str(item.get("session_id") or "") == session_id]
    output_candidates = [
        item for item in output_candidates if str(item.get("session_id") or "") == session_id
    ]
    result = reconcile_candidates(
        root,
        store,
        candidates,
        rebuild_index=False,
        full_dataset=True,
    )
    output_result = reconcile_model_outputs(root, store, output_candidates)
    rebuild_index_for_store(store)
    snapshot = source_snapshot(source_path, platform, session_id)
    if snapshot:
        write_cursor_snapshots(
            store,
            {normalize_path(source_path): snapshot},
            full_scan=False,
        )
    reassignment = reassign_session_events(
        platform=platform,
        session_id=session_id,
        destination_root=root,
        destination_store=store,
    )
    return {
        **result,
        **output_result,
        **reassignment,
        "index_rebuilt": True,
        "source_path": str(source_path),
    }


def bind_session_command(args: argparse.Namespace) -> int:
    root = find_project_root(Path(args.project), Path(args.project))
    if is_unsafe_broad_project_root(root):
        raise ValueError(f"Refusing broad project root: {root}")
    claude_home = Path(args.claude_home)
    codex_home = Path(args.codex_home)
    source = resolve_session_source(
        platform=args.platform,
        session_id=args.session_id,
        source_path=args.source_path,
        claude_home=claude_home,
        codex_home=codex_home,
    )
    binding, appended = append_session_binding(
        platform=args.platform,
        session_id=args.session_id,
        project_root=root,
        source_path=source,
        reason=args.reason,
    )
    store, _ = init_store(root)
    migration = None
    if args.migrate:
        if not source:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "binding": binding,
                        "binding_appended": appended,
                        "error": "source_transcript_not_found",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        migration = migrate_bound_session(
            platform=args.platform,
            session_id=args.session_id,
            project_root=root,
            source_path=source,
            claude_home=claude_home,
            codex_home=codex_home,
        )
    print(
        json.dumps(
            {
                "ok": True,
                "binding": binding,
                "binding_appended": appended,
                "store": str(store),
                "migration": migration,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def list_bindings(_: argparse.Namespace) -> int:
    print(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "bindings": sorted(
                    active_session_bindings().values(),
                    key=lambda item: (str(item.get("platform") or ""), str(item.get("session_id") or "")),
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def auto_sync_state_file(store: Path) -> Path:
    return store / "state" / "auto-sync.json"


def read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def auto_sync_session_key(source_platform: str, session_id: str) -> str:
    return f"{source_platform or 'unknown'}:{session_id or 'unknown'}"


def trimmed_auto_sync_sessions(sessions: dict[str, Any]) -> dict[str, Any]:
    ordered = sorted(
        sessions.items(),
        key=lambda pair: parse_iso(pair[1]) or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        reverse=True,
    )
    return dict(ordered[:MAX_AUTO_SYNC_SESSION_KEYS])


def auto_sync_pending_file(store: Path) -> Path:
    return store / "state" / "auto-sync-pending.json"


def sync_request(
    *,
    source_platform: str,
    session_id: str,
    trigger: str,
    source_path: Path | str | None,
) -> dict[str, Any]:
    return {
        "source_platform": source_platform or "unknown",
        "session_id": session_id or "unknown",
        "trigger": trigger or "unknown",
        "source_path": str(source_path) if source_path else None,
        "requested_at": iso_z(),
    }


def request_key(request: dict[str, Any]) -> str:
    return "|".join(
        (
            str(request.get("source_platform") or "unknown"),
            str(request.get("session_id") or "unknown"),
            normalize_path(request.get("source_path")) if request.get("source_path") else "",
        )
    )


def mark_pending_sync(store: Path, request: dict[str, Any]) -> int:
    path = auto_sync_pending_file(store)
    with file_lock(store / "state" / "auto-sync-pending.lock"):
        state = read_json_object(path)
        requests = {
            request_key(item): item
            for item in state.get("requests", [])
            if isinstance(item, dict)
        }
        requests[request_key(request)] = request
        payload = {
            "schema_version": "1.0.0",
            "pending": True,
            "updated_at": iso_z(),
            "request_count": int(state.get("request_count") or 0) + 1,
            "requests": list(requests.values())[-200:],
        }
        write_json(path, payload)
        return len(payload["requests"])


def pop_pending_sync(store: Path) -> list[dict[str, Any]]:
    path = auto_sync_pending_file(store)
    with file_lock(store / "state" / "auto-sync-pending.lock"):
        state = read_json_object(path)
        requests = [item for item in state.get("requests", []) if isinstance(item, dict)]
        write_json(
            path,
            {
                "schema_version": "1.0.0",
                "pending": False,
                "updated_at": iso_z(),
                "request_count": int(state.get("request_count") or 0),
                "requests": [],
            },
        )
    return requests


def global_auto_sync_lock_file() -> Path:
    return registry_path().parent / "global-auto-sync.lock"


def auto_sync_project(
    root: Path,
    *,
    source_platform: str,
    session_id: str,
    trigger: str,
    source_path: Path | None = None,
    force: bool = False,
    claude_home: Path | None = None,
    codex_home: Path | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    if is_unsafe_broad_project_root(root):
        return {"status": "skipped", "reason": "unsafe_broad_project_root", "project": str(root)}
    store, config = init_store(root)
    auto_config = config.get("auto_sync") if isinstance(config.get("auto_sync"), dict) else {}
    if not bool(auto_config.get("enabled", True)) and not force:
        return {"status": "skipped", "reason": "disabled", "project": str(root)}
    platform = str(auto_config.get("platform") or "all")
    if platform not in {"all", "claude", "codex"}:
        platform = "all"
    rebuild = bool(auto_config.get("rebuild_index", True))
    initial_request = sync_request(
        source_platform=source_platform,
        session_id=session_id,
        trigger=trigger,
        source_path=source_path,
    )
    state_path = auto_sync_state_file(store)
    lock_path = store / "state" / "auto-sync.lock"
    try:
        with file_lock(lock_path, timeout=2.0):
            state = read_json_object(state_path)
            started_at = iso_z()
            running = {
                **state,
                "schema_version": "1.0.0",
                "status": "running",
                "last_started_at": started_at,
                "last_trigger": trigger,
                "last_trigger_platform": source_platform,
                "last_trigger_session_id": session_id,
                "last_source_path": str(source_path) if source_path else None,
            }
            write_json(state_path, running)
            pending_requests = [initial_request, *pop_pending_sync(store)]
            pending_requests = list({request_key(item): item for item in pending_requests}.values())
            processed_requests: list[dict[str, Any]] = []
            results: list[dict[str, Any]] = []
            try:
                for pass_number in range(1, MAX_PENDING_SYNC_PASSES + 1):
                    paths = [Path(str(item["source_path"])) for item in pending_requests if item.get("source_path")]
                    primary = pending_requests[-1]
                    processed_requests.extend(pending_requests)
                    cursors = read_source_cursors(store)
                    full_scan = force and pass_number == 1 or not bool(cursors.get("initialized"))
                    with file_lock(global_auto_sync_lock_file(), timeout=120.0):
                        if full_scan:
                            result = backfill_project(
                                root,
                                platform=platform,
                                claude_home=claude_home,
                                codex_home=codex_home,
                                rebuild_index=rebuild,
                            )
                            extra_sources: dict[str, tuple[Path, str, str]] = {}
                            known_after_full = read_source_cursors(store).get("sources") or {}
                            bindings = active_session_bindings()
                            for request in pending_requests:
                                if not request.get("source_path"):
                                    continue
                                path = Path(str(request["source_path"]))
                                kind = infer_source_platform(path, str(request.get("source_platform") or "unknown"))
                                request_session_id = str(request.get("session_id") or path.stem)
                                if (
                                    kind in {"claude", "codex"}
                                    and normalize_path(path) not in known_after_full
                                    and transcript_source_belongs_to_root(
                                        path,
                                        kind,
                                        root,
                                        session_id=request_session_id,
                                        bindings=bindings,
                                    )
                                ):
                                    extra_sources[normalize_path(path)] = (
                                        path,
                                        kind,
                                        request_session_id,
                                    )
                            if extra_sources:
                                write_source_cursors(
                                    store,
                                    extra_sources,
                                    previous=read_source_cursors(store),
                                )
                        else:
                            result = incremental_backfill_project(
                                root,
                                platform=platform,
                                source_platform=str(primary.get("source_platform") or "unknown"),
                                session_id=str(primary.get("session_id") or "unknown"),
                                source_paths=paths,
                                claude_home=claude_home,
                                codex_home=codex_home,
                                rebuild_index=rebuild,
                            )
                    results.append(result)
                    pending_requests = pop_pending_sync(store)
                    if not pending_requests:
                        break
                else:
                    for request in pending_requests:
                        mark_pending_sync(store, request)
            except Exception as exc:
                failed = {
                    **running,
                    "status": "failed",
                    "last_failed_at": iso_z(),
                    "last_error": f"{type(exc).__name__}: {exc}",
                }
                write_json(state_path, failed)
                return {
                    "status": "failed",
                    "reason": "reconciliation_error",
                    "project": str(root),
                    "error": failed["last_error"],
                }
            completed_at = iso_z()
            sessions = running.get("sessions") if isinstance(running.get("sessions"), dict) else {}
            for request in processed_requests or [initial_request]:
                sessions[
                    auto_sync_session_key(
                        str(request.get("source_platform") or "unknown"),
                        str(request.get("session_id") or "unknown"),
                    )
                ] = completed_at
            last_result = results[-1] if results else {"project": str(root), "mode": "incremental"}
            aggregate = {
                **last_result,
                "added": sum(int(item.get("added") or 0) for item in results),
                "sync_passes": len(results),
            }
            completed = {
                **running,
                "status": "completed",
                "last_completed_at": completed_at,
                "last_error": None,
                "sessions": trimmed_auto_sync_sessions(sessions),
                "last_result": aggregate,
            }
            write_json(state_path, completed)
            return {
                "status": "completed",
                "reason": "forced" if force else ("first_full_scan" if results and results[0].get("mode") == "full" else "incremental"),
                **aggregate,
            }
    except TimeoutError:
        pending_count = mark_pending_sync(store, initial_request)
        return {
            "status": "queued",
            "reason": "sync_already_running",
            "project": str(root),
            "pending_sources": pending_count,
        }


def auto_sync_command(args: argparse.Namespace) -> int:
    root = find_project_root(Path(args.project or os.getcwd()), Path(args.project) if args.project else None)
    result = auto_sync_project(
        root,
        source_platform=args.source_platform,
        session_id=args.session_id,
        trigger=args.trigger,
        source_path=args.source_path,
        force=args.force,
        claude_home=Path(args.claude_home),
        codex_home=Path(args.codex_home),
    )
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("status") == "failed" else 0


def schedule_auto_sync(
    root: Path,
    store: Path,
    *,
    source_platform: str,
    session_id: str,
    trigger: str,
    source_path: Path | str | None = None,
) -> dict[str, Any]:
    if is_unsafe_broad_project_root(root):
        return {"scheduled": False, "reason": "unsafe_broad_project_root"}
    disabled = str(os.environ.get("PROMPT_HARNESS_DISABLE_AUTO_SYNC") or "").lower()
    if disabled in {"1", "true", "yes", "on"}:
        return {"scheduled": False, "reason": "disabled_by_environment"}
    config = read_json_object(store / "config.json")
    auto_config = config.get("auto_sync") if isinstance(config.get("auto_sync"), dict) else {}
    if not bool(auto_config.get("enabled", True)):
        return {"scheduled": False, "reason": "disabled_by_config"}
    if not bool(auto_config.get("background", True)):
        return {"scheduled": False, "reason": "background_disabled"}
    queued = mark_pending_sync(
        store,
        sync_request(
            source_platform=source_platform,
            session_id=session_id,
            trigger=trigger,
            source_path=source_path,
        ),
    )
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "auto-sync",
        "--project",
        str(root),
        "--source-platform",
        source_platform,
        "--session-id",
        session_id or "unknown",
        "--trigger",
        trigger,
    ]
    if source_path:
        command.extend(("--source-path", str(source_path)))
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": os.environ.copy(),
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **kwargs)
    except OSError as exc:
        failure_path = store / "state" / "auto-sync-errors.jsonl"
        with failure_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    {"recorded_at": iso_z(), "trigger": trigger, "error": f"{type(exc).__name__}: {exc}"},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
        return {"scheduled": False, "reason": "spawn_failed", "error": str(exc)}
    return {
        "scheduled": True,
        "pid": process.pid,
        "reason": "background_check_started",
        "pending_sources": queued,
    }


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
    target_lines: set[int] | None = None,
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
            elif obj.get("type") == "user" and next_model and (target_lines is None or line_no in target_lines):
                resolved[line_no] = next_model
        return resolved

    active_model: str | None = None
    for line_no, obj in rows:
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if obj.get("type") == "session_meta" and normalize_model(payload.get("model")):
            active_model = normalize_model(payload.get("model"))
        elif obj.get("type") == "turn_context" and normalize_model(payload.get("model")):
            active_model = normalize_model(payload.get("model"))
        if active_model and (target_lines is None or line_no in target_lines):
            resolved[line_no] = active_model
    return resolved


def source_model_cache_file(store: Path) -> Path:
    return store / "state" / "source-models.json"


def cached_source_models(
    store: Path,
    requests: dict[tuple[str, str], set[int]],
) -> dict[tuple[str, str], dict[int, str]]:
    state = read_json_object(source_model_cache_file(store))
    entries = state.get("sources") if isinstance(state.get("sources"), dict) else {}
    resolved: dict[tuple[str, str], dict[int, str]] = {}
    changed = False
    for (platform, source_path), requested_lines in requests.items():
        path = Path(source_path)
        cache_key = f"{platform}|{normalize_path(path)}"
        entry = entries.get(cache_key) if isinstance(entries.get(cache_key), dict) else {}
        try:
            stat = path.stat()
        except OSError:
            resolved[(platform, source_path)] = {}
            continue
        covered = {int(value) for value in entry.get("covered_lines", []) if str(value).isdigit()}
        fingerprint_matches = (
            int(entry.get("size") or -1) == stat.st_size
            and int(entry.get("mtime_ns") or -1) == stat.st_mtime_ns
        )
        if fingerprint_matches and requested_lines.issubset(covered):
            cached_models = entry.get("models") if isinstance(entry.get("models"), dict) else {}
            models = {
                int(line): str(model)
                for line, model in cached_models.items()
                if str(line).isdigit() and model
            }
            resolved[(platform, source_path)] = models
            continue
        target_lines = requested_lines | covered
        models = source_models_by_line(path, platform, target_lines=target_lines)
        entries[cache_key] = {
            "path": str(path),
            "platform": platform,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "covered_lines": sorted(target_lines),
            "models": {str(line): model for line, model in sorted(models.items())},
            "updated_at": iso_z(),
        }
        resolved[(platform, source_path)] = models
        changed = True
    if changed:
        write_json(
            source_model_cache_file(store),
            {
                "schema_version": "1.0.0",
                "updated_at": iso_z(),
                "sources": entries,
            },
        )
    return resolved


def normalize_model(value: Any) -> str | None:
    model = str(value or "").strip()
    if not model or model.startswith("<") or model.lower() in {"unknown", "synthetic", "none", "null"}:
        return None
    return model


def title_from_prompt(text: str, limit: int = 88) -> str:
    for raw in text.splitlines():
        line = re.sub(r"^[#>*\-\d.\s]+", "", raw).strip()
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
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


def filename_component(value: Any, *, limit: int = 64, fallback: str = "unknown") -> str:
    text = collapse_blank_lines(str(value or "")).replace("\n", " ").strip()
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[\[\](){}]", "-", text)
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip(" .-_")
    if not text:
        text = fallback
    return text[:limit].rstrip(" .-_") or fallback


def session_markdown_filename(
    key: tuple[str, str],
    items: list[tuple[str, dict[str, Any]]],
    used: set[str],
) -> str:
    ordered = sorted(items, key=trajectory_item_order_key)
    first_event = ordered[0][1] if ordered else {}
    occurred = parse_iso(first_event.get("occurred_at"))
    timestamp = occurred.astimezone(dt.timezone.utc).strftime("%Y%m%d-%H%M%S") if occurred else "unknown-time"
    platform_name = filename_component(key[0], limit=24)
    models = [
        normalize_model(event.get("context", {}).get("model"))
        for _, event in ordered
        if isinstance(event.get("context"), dict)
    ]
    model = filename_component(next((value for value in models if value), "unknown-model"), limit=40)
    prompt_texts = [
        str(event.get("prompt", {}).get("text") or "")
        for kind, event in ordered
        if kind == "prompt"
    ]
    if prompt_texts:
        topic = title_from_prompts(prompt_texts)
    else:
        agent_name = next(
            (
                str(event.get("session", {}).get("agent_id") or "")
                for kind, event in ordered
                if kind == "trace" and event.get("session", {}).get("agent_id")
            ),
            "",
        )
        readable = next(
            (
                str(
                    (
                        event.get("content")
                        if isinstance(event.get("content"), dict)
                        else event.get("output", {})
                    ).get("text")
                    or ""
                )
                for kind, event in ordered
                if kind == "trace"
            ),
            "",
        )
        topic = f"subagent-{agent_name}" if agent_name else title_from_prompt(readable)
    topic_part = filename_component(topic, limit=72, fallback="untitled-session")
    stem = f"{timestamp}-{platform_name}-{model}-{topic_part}"
    candidate = stem
    if candidate.casefold() in used:
        candidate = f"{stem}-{sha256_text('|'.join(key))[:8]}"
    used.add(candidate.casefold())
    return candidate + ".md"


def build_derived_views(
    events: list[dict[str, Any]],
    images_by_event: dict[str, list[dict[str, Any]]] | None = None,
    *,
    store: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    images_by_event = images_by_event or {}
    model_requests: dict[tuple[str, str], set[int]] = collections.defaultdict(set)
    for event in events:
        source = event.get("source", {})
        context = event.get("context", {})
        source_path = str(source.get("path") or "")
        source_line = source.get("line")
        if normalize_model(context.get("model")) or not source_path or not source_line:
            continue
        model_requests[(str(source.get("platform") or "unknown").lower(), source_path)].add(int(source_line))
    source_cache = cached_source_models(store, model_requests) if store else {}
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
            if cache_key not in source_cache and not store:
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
            "images": images_by_event.get(str(event.get("event_id") or ""), []),
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
        items.sort(key=event_order_key)
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
    raw_event_count = sum(1 for _ in iter_events(store))
    events = sorted(iter_active_events(store), key=event_order_key)
    model_outputs = sorted(iter_model_outputs(store), key=model_output_order_key)
    event_ids = {str(event.get("event_id") or "") for event in iter_events(store)}
    superseded_ids = superseded_event_ids(store) & event_ids
    excluded_ids = excluded_event_ids(store) & event_ids
    prompt_images = sorted(
        iter_prompt_images(store),
        key=lambda item: (str(item.get("event_id") or ""), str(item.get("attachment_id") or "")),
    )
    images_by_event: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for image in prompt_images:
        images_by_event[str(image.get("event_id") or "")].append(image)
    rebuild_session_metadata_for_store(store, events)
    event_views, sessions = build_derived_views(events, images_by_event, store=store)
    by_platform = collections.Counter(event.get("source", {}).get("platform") for event in events)
    model_outputs_by_platform = collections.Counter(
        event.get("source", {}).get("platform") for event in model_outputs
    )
    by_session = collections.Counter(
        f"{event.get('source', {}).get('platform')}:{event.get('session', {}).get('id')}" for event in events
    )
    redactions = sum(int(event.get("prompt", {}).get("secret_redactions", 0)) for event in events)
    omissions = sum(int(event.get("prompt", {}).get("attachments_omitted", 0)) for event in events)
    catalog = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_z(),
        "event_count": len(events),
        "raw_event_count": raw_event_count,
        "inactive_event_count": raw_event_count - len(events),
        "superseded_event_count": len(superseded_ids),
        "excluded_event_count": len(excluded_ids),
        "platform_counts": dict(sorted(by_platform.items())),
        "session_counts": dict(sorted(by_session.items())),
        "secret_redactions": redactions,
        "attachments_omitted": omissions,
        "image_count": len(prompt_images),
        "image_event_count": len(images_by_event),
        "model_output_count": len(model_outputs),
        "model_output_platform_counts": dict(sorted(model_outputs_by_platform.items())),
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
                f"- Event ID: `{view.get('event_id')}`",
                f"- Source mode: `{view.get('source_mode')}`",
                f"- Images: `{len(view.get('images') or [])}`",
                "",
                fenced(view["prompt"]),
                "",
            ]
        )
        for image_number, image_record in enumerate(view.get("images") or [], 1):
            relative_path = str(image_record.get("asset", {}).get("path") or "").replace("\\", "/")
            if relative_path:
                lines.extend(
                    [
                        f"![P{view['number']:05d} image {image_number}](../{relative_path})",
                        "",
                    ]
                )
    atomic_write(store / "index" / "PROMPTS.md", "\n".join(lines))
    prompt_numbers = {
        str(event.get("event_id") or ""): number
        for number, event in enumerate(events, 1)
    }
    prompt_by_id = {
        str(event.get("event_id") or ""): event
        for event in events
        if event.get("event_id")
    }
    output_numbers = {
        str(event.get("trace_event_id") or event.get("model_output_id") or ""): number
        for number, event in enumerate(model_outputs, 1)
    }
    session_items: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = (
        collections.defaultdict(list)
    )
    subagent_parent: dict[tuple[str, str], tuple[str, str] | None] = {}
    for event in events:
        key = (
            str(event.get("source", {}).get("platform") or "unknown"),
            str(event.get("session", {}).get("id") or "unknown"),
        )
        session_items[key].append(("prompt", event))
        subagent_parent.setdefault(key, None)
    for event in model_outputs:
        platform_name = str(event.get("source", {}).get("platform") or "unknown")
        session_data = event.get("session") if isinstance(event.get("session"), dict) else {}
        key = (platform_name, str(session_data.get("id") or "unknown"))
        session_items[key].append(("trace", event))
        parent_id = str(session_data.get("parent_session_id") or "")
        subagent_parent[key] = (platform_name, parent_id) if parent_id else None

    def session_turn_keys(key: tuple[str, str]) -> set[str]:
        keys: set[str] = set()
        items = session_items.get(key, [])
        for kind, event in items:
            session_data = event.get("session") if isinstance(event.get("session"), dict) else {}
            native_turn_id = str(session_data.get("turn_id") or "")
            if native_turn_id:
                keys.add(f"native:{native_turn_id}")
                continue
            if kind == "prompt":
                event_id = str(event.get("event_id") or "")
                if event_id:
                    keys.add(f"prompt:{event_id}")
                continue
            prompt_event_id = str(event.get("links", {}).get("prompt_event_id") or "")
            if prompt_event_id:
                linked_prompt = prompt_by_id.get(prompt_event_id, {})
                linked_turn_id = str(linked_prompt.get("session", {}).get("turn_id") or "")
                keys.add(
                    f"native:{linked_turn_id}"
                    if linked_turn_id
                    else f"prompt:{prompt_event_id}"
                )
        return keys

    def latest_turn_status(key: tuple[str, str]) -> str:
        items = sorted(session_items.get(key, []), key=trajectory_item_order_key)
        latest_prompt = next(
            (event for kind, event in reversed(items) if kind == "prompt"),
            None,
        )
        if not latest_prompt:
            return "closed" if any(
                kind == "trace"
                and str(event.get("context", {}).get("phase") or "") == "final_answer"
                for kind, event in items
            ) else "open_or_interrupted"
        latest_turn_id = str(latest_prompt.get("session", {}).get("turn_id") or "")
        latest_prompt_id = str(latest_prompt.get("event_id") or "")
        for kind, event in items:
            if kind != "trace" or str(event.get("context", {}).get("phase") or "") != "final_answer":
                continue
            trace_turn_id = str(event.get("session", {}).get("turn_id") or "")
            linked_prompt_id = str(event.get("links", {}).get("prompt_event_id") or "")
            if (latest_turn_id and trace_turn_id == latest_turn_id) or (
                latest_prompt_id and linked_prompt_id == latest_prompt_id
            ):
                return "closed"
        return "open_or_interrupted"

    def session_first_key(key: tuple[str, str]) -> tuple[Any, ...]:
        items = session_items.get(key, [])
        first = min((trajectory_item_order_key(item) for item in items), default=("",))
        return (*first, key[0], key[1])

    children: dict[tuple[str, str], list[tuple[str, str]]] = collections.defaultdict(list)
    top_level: list[tuple[str, str]] = []
    for key in session_items:
        parent = subagent_parent.get(key)
        if parent and parent in session_items and parent != key:
            children[parent].append(key)
        else:
            top_level.append(key)
    top_level.sort(key=session_first_key)
    for values in children.values():
        values.sort(key=session_first_key)

    managed_session_dirs = {
        "prompt": store / "index" / "prompt",
        "modelout": store / "index" / "modelout",
        "trajectory": store / "index" / "trajectory",
    }
    for folder in managed_session_dirs.values():
        folder.mkdir(parents=True, exist_ok=True)
    used_session_filenames: set[str] = set()
    session_filenames: dict[tuple[str, str], str] = {}
    for key in sorted(session_items, key=session_first_key):
        session_filenames[key] = session_markdown_filename(
            key,
            session_items[key],
            used_session_filenames,
        )

    manifest_path = store / "state" / "session-index-manifest.json"
    prior_manifest = read_json_object(manifest_path)
    prior_sessions = prior_manifest.get("sessions") if isinstance(prior_manifest.get("sessions"), dict) else {}
    next_manifest: dict[str, dict[str, str]] = {}
    expected_paths: set[Path] = set()
    for key in sorted(session_items, key=session_first_key):
        platform_name, session_id = key
        items = sorted(session_items[key], key=trajectory_item_order_key)
        prompts_for_session = [event for kind, event in items if kind == "prompt"]
        traces_for_session = [event for kind, event in items if kind == "trace"]
        filename = session_filenames[key]
        session_key_text = f"{platform_name}:{session_id}"
        fingerprint = session_projection_fingerprint(items, prompt_numbers, output_numbers)
        next_manifest[session_key_text] = {"filename": filename, "fingerprint": fingerprint}
        session_paths = {
            name: folder / filename for name, folder in managed_session_dirs.items()
        }
        expected_paths.update(session_paths.values())
        prior = prior_sessions.get(session_key_text) if isinstance(prior_sessions.get(session_key_text), dict) else {}
        unchanged = (
            prior.get("filename") == filename
            and prior.get("fingerprint") == fingerprint
            and all(path.is_file() for path in session_paths.values())
        )
        if not prior_manifest and all(path.is_file() for path in session_paths.values()):
            unchanged = True
        if unchanged:
            continue
        first_time = items[0][1].get("occurred_at") if items else None
        last_time = items[-1][1].get("occurred_at") if items else None
        parent_id = next(
            (
                str(event.get("session", {}).get("parent_session_id") or "")
                for event in traces_for_session
                if event.get("session", {}).get("parent_session_id")
            ),
            "",
        )
        agent_id = next(
            (
                str(event.get("session", {}).get("agent_id") or "")
                for event in traces_for_session
                if event.get("session", {}).get("agent_id")
            ),
            "",
        )
        metadata = [
            f"- Platform: `{platform_name}`",
            f"- Session: `{session_id}`",
            f"- Time: `{first_time}` → `{last_time}`",
            f"- Latest turn: `{latest_turn_status(key)}`",
            f"- Parent session: `{parent_id or 'none'}`",
            f"- Agent: `{agent_id or 'main'}`",
            "",
        ]

        prompt_lines = ["# Session prompts", "", *metadata]
        if not prompts_for_session:
            prompt_lines.extend(["> No human prompt event was recorded for this session.", ""])
        for event in prompts_for_session:
            number = prompt_numbers.get(str(event.get("event_id") or ""), 0)
            prompt_lines.extend(
                render_trajectory_event(
                    f"P{number:05d}" if number else "P-unlinked",
                    "prompt",
                    event,
                    prompt_numbers,
                )
            )
        atomic_write(session_paths["prompt"], "\n".join(prompt_lines))

        modelout_lines = ["# Session model outputs and agent trace", "", *metadata]
        if not traces_for_session:
            modelout_lines.extend(["> No trace event was recorded for this session.", ""])
        for event in traces_for_session:
            trace_id = str(event.get("trace_event_id") or event.get("model_output_id") or "")
            number = output_numbers.get(trace_id, 0)
            modelout_lines.extend(
                render_trajectory_event(
                    f"O{number:05d}" if number else "O-unlinked",
                    "trace",
                    event,
                    prompt_numbers,
                )
            )
        atomic_write(session_paths["modelout"], "\n".join(modelout_lines))

        session_trajectory_lines = [
            "# Session trajectory",
            "",
            *metadata,
            "> Conversation view: every turn starts with its human prompt, followed by all linked model and agent events.",
            "",
        ]
        session_trajectory_lines.extend(
            render_conversation_turns(
                items,
                prompt_by_id,
                prompt_numbers,
                turn_heading_level=2,
                event_heading_level=3,
            )
        )
        atomic_write(
            session_paths["trajectory"],
            "\n".join(session_trajectory_lines),
        )
    for folder in managed_session_dirs.values():
        for stale in folder.glob("*.md"):
            if stale not in expected_paths:
                stale.unlink()
    write_json(
        manifest_path,
        {
            "schema_version": "1.0.0",
            "generated_at": iso_z(),
            "sessions": next_manifest,
        },
    )

    total_turn_count = sum(len(session_turn_keys(key)) for key in session_items)
    sessions_by_platform = collections.Counter(key[0] for key in session_items)
    trajectory_lines = [
        "# Project trajectories",
        "",
        f"- Project: `{events[0].get('project', {}).get('root') if events else model_outputs[0].get('project', {}).get('root') if model_outputs else store.parent}`",
        f"- Total sessions: `{len(session_items)}`",
        f"- Claude sessions: `{sessions_by_platform.get('claude', 0)}`",
        f"- Codex sessions: `{sessions_by_platform.get('codex', 0)}`",
        f"- Total turns: `{total_turn_count}`",
        f"- Total human prompts: `{len(events)}`",
        f"- Trace events: `{len(model_outputs)}`",
        "",
        "> Lightweight project-wide index. Complete content is stored in one full Markdown file per session under `trajectory/`.",
        "",
        "## Session index",
        "",
        "| Session | Platform | Session ID | Latest turn | Turns | Human prompts | Trace events | Per-session trajectory |",
        "|---|---|---|---|---:|---:|---:|---|",
    ]
    ordered_session_keys = sorted(session_items, key=session_first_key)
    for index_number, key in enumerate(ordered_session_keys, 1):
        platform_name, session_id = key
        items = session_items[key]
        filename = session_filenames[key]
        trajectory_lines.append(
            "| "
            f"`S{index_number:05d}` | `{platform_name}` | `{session_id}` | "
            f"`{latest_turn_status(key)}` | "
            f"{len(session_turn_keys(key))} | "
            f"{sum(kind == 'prompt' for kind, _ in items)} | "
            f"{sum(kind == 'trace' for kind, _ in items)} | "
            f"[open](trajectory/{urllib.parse.quote(filename)}) |"
        )
    trajectory_lines.append("")
    modelout_index_lines = [
        "# Model outputs and agent trace",
        "",
        f"- Project: `{events[0].get('project', {}).get('root') if events else model_outputs[0].get('project', {}).get('root') if model_outputs else store.parent}`",
        f"- Total sessions: `{len(session_items)}`",
        f"- Trace events: `{len(model_outputs)}`",
        "",
        "> Lightweight project-wide index. Complete reasoning, tool traffic, injections, subagents, and assistant output remain in `modelout/<session>.md` and canonical `model-events/**/*.jsonl`.",
        "",
        "| Session | Platform | Session ID | Trace events | Full model output |",
        "|---|---|---|---:|---|",
    ]
    for index_number, key in enumerate(ordered_session_keys, 1):
        platform_name, session_id = key
        items = session_items[key]
        filename = session_filenames[key]
        modelout_index_lines.append(
            f"| `S{index_number:05d}` | `{platform_name}` | `{session_id}` | "
            f"{sum(kind == 'trace' for kind, _ in items)} | "
            f"[open](modelout/{urllib.parse.quote(filename)}) |"
        )
    modelout_index_lines.append("")
    modelout_summary_lines = [
        "## Final answers",
        "",
        "> Only complete final assistant answers are included. Open the linked per-session full file for reasoning, tools, injections, subagents, and structured payloads.",
        "",
    ]
    trajectory_summary_lines = [
        "## Compact conversation trajectories",
        "",
        "> Compact conversation view: complete human prompts plus complete final answers. Other trace categories are represented only by counts.",
        "",
    ]
    for index_number, key in enumerate(ordered_session_keys, 1):
        platform_name, session_id = key
        items = sorted(session_items[key], key=trajectory_item_order_key)
        filename = session_filenames[key]
        final_answers = [
            event
            for kind, event in items
            if kind == "trace"
            and str(event.get("event_type") or "") == "assistant_text"
            and str(event.get("context", {}).get("phase") or "") == "final_answer"
        ]
        if final_answers:
            modelout_summary_lines.extend(
                [
                    f"### S{index_number:05d} · {platform_name} · `{session_id}`",
                    "",
                    f"- Full: [open](modelout/{urllib.parse.quote(filename)})",
                    "",
                ]
            )
            for answer_number, event in enumerate(final_answers, 1):
                content = event.get("content") if isinstance(event.get("content"), dict) else event.get("output", {})
                modelout_summary_lines.extend(
                    [
                        f"#### Final answer {answer_number:05d}",
                        "",
                        f"- Time: `{event.get('occurred_at')}`",
                        f"- Turn: `{event.get('session', {}).get('turn_id') or 'none'}`",
                        "",
                        fenced(str(content.get("text") or "")),
                        "",
                    ]
                )

        turns: dict[str, list[tuple[str, dict[str, Any]]]] = collections.defaultdict(list)
        local_prompt_turns = {
            str(event.get("session", {}).get("turn_id") or "")
            for kind, event in items
            if kind == "prompt" and event.get("session", {}).get("turn_id")
        }
        for kind, event in items:
            session_data = event.get("session") if isinstance(event.get("session"), dict) else {}
            native_turn = str(session_data.get("turn_id") or "")
            if kind == "prompt":
                event_id = str(event.get("event_id") or "")
                turn_key = f"native:{native_turn}" if native_turn else f"prompt:{event_id}"
            else:
                prompt_event_id = str(event.get("links", {}).get("prompt_event_id") or "")
                linked_prompt = prompt_by_id.get(prompt_event_id, {})
                linked_turn = str(linked_prompt.get("session", {}).get("turn_id") or "")
                turn_key = (
                    f"native:{native_turn}"
                    if native_turn and native_turn in local_prompt_turns
                    else f"native:{linked_turn}"
                    if linked_turn
                    else f"prompt:{prompt_event_id}"
                    if prompt_event_id
                    else "unlinked"
                )
            turns[turn_key].append((kind, event))
        if turns:
            trajectory_summary_lines.extend(
                [
                    f"### S{index_number:05d} · {platform_name} · `{session_id}`",
                    "",
                    f"- Full: [open](trajectory/{urllib.parse.quote(filename)})",
                    "",
                ]
            )
        ordered_turns = sorted(
            turns.items(),
            key=lambda pair: min(trajectory_item_order_key(item) for item in pair[1]),
        )
        for turn_number, (turn_key, turn_items) in enumerate(ordered_turns, 1):
            prompts = [event for kind, event in turn_items if kind == "prompt"]
            traces = [event for kind, event in turn_items if kind == "trace"]
            final_turn_answers = [
                event
                for event in traces
                if str(event.get("event_type") or "") == "assistant_text"
                and str(event.get("context", {}).get("phase") or "") == "final_answer"
            ]
            counts = collections.Counter(str(event.get("event_type") or "unknown") for event in traces)
            trajectory_summary_lines.extend(
                [
                    f"#### Turn {turn_number:05d}",
                    "",
                    f"- Native turn ID: `{turn_key.removeprefix('native:') if turn_key.startswith('native:') else 'unavailable'}`",
                    f"- Trace summary: `{', '.join(f'{name}={count}' for name, count in sorted(counts.items())) or 'none'}`",
                    "",
                ]
            )
            for prompt_event in prompts:
                trajectory_summary_lines.extend(
                    [
                        "##### HUMAN",
                        "",
                        fenced(str(prompt_event.get("prompt", {}).get("text") or "")),
                        "",
                    ]
                )
            for answer_event in final_turn_answers:
                content = answer_event.get("content") if isinstance(answer_event.get("content"), dict) else answer_event.get("output", {})
                trajectory_summary_lines.extend(
                    [
                        "##### ASSISTANT FINAL",
                        "",
                        fenced(str(content.get("text") or "")),
                        "",
                    ]
                )
    atomic_write(
        store / "index" / "MODELOUT.md",
        "\n".join([*modelout_index_lines, *modelout_summary_lines]),
    )
    atomic_write(
        store / "index" / "TRAJECTORY.md",
        "\n".join([*trajectory_lines, *trajectory_summary_lines]),
    )
    for obsolete in (
        store / "index" / "MODELOUTEASY.md",
        store / "index" / "TRAJECTORYEASY.md",
    ):
        with contextlib.suppress(FileNotFoundError):
            obsolete.unlink()
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
    clear_index_dirty(store)
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
    model_events_root = store / "model-events"
    if model_events_root.exists():
        for path in sorted(model_events_root.rglob("*.jsonl")):
            rendered = []
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
                content_key = "content" if isinstance(event.get("content"), dict) else "output"
                content = event.get(content_key) if isinstance(event.get(content_key), dict) else None
                if content is not None:
                    clean, stats = sanitize_trace_value(content)
                    count = int(stats.get("secret_redactions", 0))
                    omitted = int(stats.get("attachments_omitted", 0))
                    if count or omitted:
                        clean["secret_redactions"] = int(content.get("secret_redactions", 0)) + count
                        clean["attachments_omitted"] = int(content.get("attachments_omitted", 0)) + omitted
                        text = str(clean.get("text") or "")
                        clean["chars"] = len(text)
                        clean["sha256"] = (
                            trace_content_hash(text, clean.get("structured"))
                            if event.get("record_type") == MODEL_OUTPUT_RECORD_TYPE
                            else sha256_text(text)
                        )
                        event[content_key] = clean
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
    active_events = sorted(iter_active_events(store), key=event_order_key)
    prompt_numbers = {
        str(event.get("event_id") or ""): number
        for number, event in enumerate(active_events, 1)
    }
    matches: list[tuple[int, dict[str, Any]]] = []
    for event in active_events:
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
        print(
            json.dumps(
                [
                    {
                        "prompt_number": prompt_numbers.get(str(event.get("event_id") or "")),
                        **event,
                    }
                    for event in selected
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for event in selected:
            number = prompt_numbers.get(str(event.get("event_id") or ""), 0)
            print(
                f"## P{number:05d} · {event.get('occurred_at')} · "
                f"{event.get('source', {}).get('platform')} · {event.get('event_id')}"
            )
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
    supersession_ids: set[str] = set()
    superseded_ids: set[str] = set()
    canonical_ids: set[str] = set()
    for relation in iter_event_supersessions(store):
        relation_id = str(relation.get("supersession_id") or "")
        event_id = str(relation.get("event_id") or "")
        canonical_id = str(relation.get("canonical_event_id") or "")
        if not relation_id:
            errors.append("event supersession is missing supersession_id")
        elif relation_id in supersession_ids:
            errors.append(f"duplicate supersession_id {relation_id}")
        supersession_ids.add(relation_id)
        if event_id not in ids:
            errors.append(f"supersession references missing event_id {event_id}")
        if canonical_id not in ids:
            errors.append(f"supersession references missing canonical_event_id {canonical_id}")
        if event_id == canonical_id:
            errors.append(f"supersession cannot point {event_id} to itself")
        superseded_ids.add(event_id)
        canonical_ids.add(canonical_id)
    for canonical_id in canonical_ids & superseded_ids:
        warnings.append(f"supersession chain includes {canonical_id}; derived views resolve one level only")
    exclusion_ids: set[str] = set()
    excluded_ids: set[str] = set()
    for exclusion in iter_event_exclusions(store):
        exclusion_id = str(exclusion.get("exclusion_id") or "")
        event_id = str(exclusion.get("event_id") or "")
        reason = str(exclusion.get("reason") or "")
        if not exclusion_id:
            errors.append("event exclusion is missing exclusion_id")
        elif exclusion_id in exclusion_ids:
            errors.append(f"duplicate exclusion_id {exclusion_id}")
        exclusion_ids.add(exclusion_id)
        if event_id not in ids:
            errors.append(f"exclusion references missing event_id {event_id}")
        if not reason:
            errors.append(f"exclusion {exclusion_id} is missing reason")
        excluded_ids.add(event_id)
    active_ids = {
        str(event.get("event_id") or "")
        for event in iter_active_events(store)
        if event.get("event_id")
    }
    attachment_ids: set[str] = set()
    image_hashes: dict[Path, str] = {}
    image_count = 0
    active_image_count = 0
    image_root = (store / "assets" / "images").resolve()
    for image in iter_prompt_images(store):
        image_count += 1
        attachment_id = str(image.get("attachment_id") or "")
        if not attachment_id:
            errors.append(f"missing attachment_id in image record #{image_count}")
        elif attachment_id in attachment_ids:
            errors.append(f"duplicate attachment_id {attachment_id}")
        attachment_ids.add(attachment_id)
        event_id = str(image.get("event_id") or "")
        if event_id not in ids:
            errors.append(f"image {attachment_id} references missing event_id {event_id}")
        if event_id in active_ids:
            active_image_count += 1
        asset = image.get("asset") if isinstance(image.get("asset"), dict) else {}
        relative = Path(str(asset.get("path") or ""))
        asset_path = (store / relative).resolve()
        try:
            common = Path(os.path.commonpath((str(image_root), str(asset_path))))
        except ValueError:
            common = Path()
        if common != image_root:
            errors.append(f"image {attachment_id} escapes assets/images")
            continue
        if not asset_path.is_file():
            errors.append(f"image asset is missing for {attachment_id}: {relative}")
            continue
        expected_size = int(asset.get("bytes") or -1)
        if asset_path.stat().st_size != expected_size:
            errors.append(f"image size mismatch for {attachment_id}")
        if asset_path not in image_hashes:
            image_hashes[asset_path] = hashlib.sha256(asset_path.read_bytes()).hexdigest()
        if image_hashes[asset_path] != str(asset.get("sha256") or ""):
            errors.append(f"image hash mismatch for {attachment_id}")
    model_output_ids: set[str] = set()
    model_output_count = 0
    linked_model_output_count = 0
    for output_event in iter_model_outputs(store):
        model_output_count += 1
        output_id = str(
            output_event.get("trace_event_id") or output_event.get("model_output_id") or ""
        )
        required_fields = ["schema_version", "record_type", "captured_at", "occurred_at"]
        if output_event.get("record_type") == MODEL_OUTPUT_RECORD_TYPE:
            required_fields.extend(["trace_event_id", "event_type", "actor", "content"])
        else:
            required_fields.append("model_output_id")
        for field in required_fields:
            if not output_event.get(field):
                errors.append(f"missing {field} in model output #{model_output_count}")
        if output_id in model_output_ids:
            errors.append(f"duplicate model_output_id {output_id}")
        model_output_ids.add(output_id)
        content = output_event.get("content")
        if not isinstance(content, dict):
            content = output_event.get("output", {})
        text = str(content.get("text") or "")
        digest = str(content.get("sha256") or "")
        expected_digest = (
            trace_content_hash(text, content.get("structured"))
            if output_event.get("record_type") == MODEL_OUTPUT_RECORD_TYPE
            else sha256_text(text)
        )
        if digest != expected_digest:
            errors.append(f"model output hash mismatch for {output_id}")
        if trace_value_matches_patterns(content, BASE64_PATTERNS):
            errors.append(f"embedded attachment data remains in {output_id}")
        if trace_value_matches_patterns(content, SECRET_PATTERNS):
            errors.append(f"obvious secret pattern remains in {output_id}")
        prompt_event_id = str(output_event.get("links", {}).get("prompt_event_id") or "")
        if prompt_event_id:
            linked_model_output_count += 1
            if prompt_event_id not in ids:
                errors.append(
                    f"model output {output_id} references missing prompt_event_id {prompt_event_id}"
                )
        if not is_within(output_event.get("project", {}).get("root"), root):
            errors.append(f"project root mismatch in {output_id}")
    if not index_is_dirty(store):
        required_indexes = (
            "PROMPTS.md",
            "MODELOUT.md",
            "TRAJECTORY.md",
        )
        for name in required_indexes:
            if not (store / "index" / name).is_file():
                errors.append(f"derived index is missing: index/{name}")
        session_view_names: dict[str, set[str]] = {}
        for directory in ("prompt", "modelout", "trajectory"):
            folder = store / "index" / directory
            if not folder.is_dir():
                errors.append(f"per-session index directory is missing: index/{directory}")
                session_view_names[directory] = set()
                continue
            session_view_names[directory] = {path.name for path in folder.glob("*.md")}
        if len({frozenset(names) for names in session_view_names.values()}) > 1:
            errors.append("per-session prompt/modelout/trajectory filenames do not match")
    config_path = store / "config.json"
    if not config_path.exists():
        errors.append("config.json is missing")
    if not (store / ".gitignore").exists():
        warnings.append("nested .gitignore is missing")
    else:
        ignored = (store / ".gitignore").read_text(encoding="utf-8", errors="replace")
        if not re.search(r"(?m)^assets/?$", ignored):
            warnings.append("assets/ is not ignored by the nested .gitignore")
    misses = store / "state" / "hook-misses.jsonl"
    if misses.exists():
        miss_count = sum(1 for _ in read_jsonl(misses))
        if miss_count:
            warnings.append(f"{miss_count} hook payloads contained no recoverable user prompt; inspect {misses}")
    image_misses = store / "state" / "image-misses.jsonl"
    if image_misses.exists():
        image_miss_count = sum(1 for _ in read_jsonl(image_misses))
        if image_miss_count:
            warnings.append(f"{image_miss_count} image attachments were omitted; inspect {image_misses}")
    auto_sync_state = read_json_object(auto_sync_state_file(store))
    if auto_sync_state.get("status") == "failed":
        warnings.append(
            "automatic history reconciliation last failed: "
            + str(auto_sync_state.get("last_error") or "unknown error")
        )
    auto_sync_errors = store / "state" / "auto-sync-errors.jsonl"
    if auto_sync_errors.exists():
        error_count = sum(1 for _ in read_jsonl(auto_sync_errors))
        if error_count:
            warnings.append(f"{error_count} automatic reconciliation launches failed; inspect {auto_sync_errors}")
    cursor_state = read_source_cursors(store)
    pending_state = read_json_object(auto_sync_pending_file(store))
    project_bindings = [
        binding
        for binding in active_session_bindings().values()
        if normalize_path(binding.get("project_root")) == normalize_path(root)
    ]
    missing_binding_sources = [
        str(binding.get("source_path"))
        for binding in project_bindings
        if binding.get("source_path") and not Path(str(binding["source_path"])).is_file()
    ]
    if missing_binding_sources:
        warnings.append(
            f"{len(missing_binding_sources)} active session bindings reference missing transcripts"
        )
    return {
        "ok": not errors,
        "event_count": count,
        "active_event_count": len(active_ids),
        "superseded_event_count": len(superseded_ids),
        "excluded_event_count": len(excluded_ids),
        "image_count": image_count,
        "active_image_count": active_image_count,
        "image_file_count": len(image_hashes),
        "model_output_count": model_output_count,
        "linked_model_output_count": linked_model_output_count,
        "active_session_binding_count": len(project_bindings),
        "auto_sync": {
            "status": auto_sync_state.get("status") or "never_run",
            "last_completed_at": auto_sync_state.get("last_completed_at"),
            "last_result": auto_sync_state.get("last_result"),
            "source_cursors_initialized": bool(cursor_state.get("initialized")),
            "source_cursor_count": len(cursor_state.get("sources") or {}),
            "pending_source_count": len(pending_state.get("requests") or []),
            "index_dirty": index_is_dirty(store),
        },
        "errors": errors,
        "warnings": warnings,
    }


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
    if is_unsafe_broad_project_root(root):
        print(json.dumps({"status": "skipped", "reason": "unsafe_broad_project_root", "project": str(root)}))
        return 2
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

    stop_recovery = sub.add_parser(
        "capture-stop-recovery",
        help="recover the latest Codex human prompt from a Stop hook payload",
    )
    stop_recovery.add_argument("--project", type=Path)
    stop_recovery.add_argument(
        "--codex-home",
        type=Path,
        default=native_agent_home("CODEX_HOME", ".codex"),
    )
    stop_recovery.set_defaults(func=capture_stop_recovery)

    fill = sub.add_parser("backfill", help="backfill historical Claude/Codex prompts for one project")
    fill.add_argument("--project", type=Path)
    fill.add_argument("--platform", choices=("all", "claude", "codex"), default="all")
    fill.add_argument(
        "--claude-home",
        type=Path,
        default=native_agent_home("CLAUDE_CONFIG_DIR", ".claude"),
    )
    fill.add_argument(
        "--codex-home",
        type=Path,
        default=native_agent_home("CODEX_HOME", ".codex"),
    )
    fill.add_argument("--rebuild-index", action="store_true")
    fill.set_defaults(func=backfill)

    bind = sub.add_parser(
        "bind-session",
        help="append an explicit session-to-project binding and optionally migrate that session",
    )
    bind.add_argument("--platform", choices=("claude", "codex"), required=True)
    bind.add_argument("--session-id", required=True)
    bind.add_argument("--project", type=Path, required=True)
    bind.add_argument("--source-path", type=Path)
    bind.add_argument("--reason", default="explicit")
    bind.add_argument("--migrate", action="store_true")
    bind.add_argument(
        "--claude-home",
        type=Path,
        default=native_agent_home("CLAUDE_CONFIG_DIR", ".claude"),
    )
    bind.add_argument(
        "--codex-home",
        type=Path,
        default=native_agent_home("CODEX_HOME", ".codex"),
    )
    bind.set_defaults(func=bind_session_command)

    bindings = sub.add_parser("list-bindings", help="list active session-to-project bindings")
    bindings.set_defaults(func=list_bindings)

    automatic = sub.add_parser(
        "auto-sync",
        help="run first-use full discovery or cursor-based incremental reconciliation",
    )
    automatic.add_argument("--project", type=Path)
    automatic.add_argument("--source-platform", choices=("claude", "codex", "unknown"), default="unknown")
    automatic.add_argument("--session-id", default="unknown")
    automatic.add_argument("--trigger", default="manual")
    automatic.add_argument("--source-path", type=Path)
    automatic.add_argument("--force", action="store_true")
    automatic.add_argument(
        "--claude-home",
        type=Path,
        default=native_agent_home("CLAUDE_CONFIG_DIR", ".claude"),
    )
    automatic.add_argument(
        "--codex-home",
        type=Path,
        default=native_agent_home("CODEX_HOME", ".codex"),
    )
    automatic.set_defaults(func=auto_sync_command)

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
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(OSError):
                reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
