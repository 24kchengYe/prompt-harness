#!/usr/bin/env python3
"""Install or remove Prompt Harness UserPromptSubmit hooks safely."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


MARKER = "prompt_harness.py"


def timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            Path(temp_name).unlink()
        except FileNotFoundError:
            pass


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    destination = path.with_name(f"{path.name}.prompt-harness-{timestamp()}.bak")
    shutil.copy2(path, destination)
    return destination


def is_ours(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    for hook in entry.get("hooks", []):
        if isinstance(hook, dict) and MARKER in str(hook.get("command", "")):
            return True
    return False


def command_for(script: Path, platform: str) -> str:
    return f'"{sys.executable}" "{script}" capture-hook --platform {platform}'


def update_file(path: Path, script: Path, platform: str, remove: bool, dry_run: bool) -> dict[str, Any]:
    data = load_json(path)
    hooks = data.setdefault("hooks", {})
    entries = hooks.setdefault("UserPromptSubmit", [])
    if not isinstance(entries, list):
        raise ValueError(f"hooks.UserPromptSubmit must be an array in {path}")
    kept = [entry for entry in entries if not is_ours(entry)]
    if not remove:
        kept.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": command_for(script, platform),
                        "timeout": 5,
                        "statusMessage": "Archiving project prompt",
                    }
                ]
            }
        )
    changed = kept != entries
    hooks["UserPromptSubmit"] = kept
    if not changed:
        return {"path": str(path), "changed": False, "backup": None}
    if dry_run:
        return {"path": str(path), "changed": True, "backup": None, "dry_run": True}
    backup_path = backup(path)
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return {
        "path": str(path),
        "changed": True,
        "backup": str(backup_path) if backup_path else None,
        "action": "removed" if remove else "installed",
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Install Prompt Harness UserPromptSubmit hooks")
    result.add_argument("--platform", choices=("all", "codex", "claude"), default="all")
    result.add_argument("--remove", action="store_true", help="remove only Prompt Harness hook entries")
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--codex-hooks", type=Path, default=Path.home() / ".codex" / "hooks.json")
    result.add_argument("--claude-settings", type=Path, default=Path.home() / ".claude" / "settings.json")
    result.add_argument("--script", type=Path, default=Path(__file__).with_name("prompt_harness.py"))
    return result


def main() -> int:
    args = parser().parse_args()
    script = args.script.expanduser().resolve()
    if not script.exists():
        raise FileNotFoundError(script)
    results = []
    if args.platform in {"all", "codex"}:
        results.append(update_file(args.codex_hooks.expanduser(), script, "codex", args.remove, args.dry_run))
    if args.platform in {"all", "claude"}:
        results.append(update_file(args.claude_settings.expanduser(), script, "claude", args.remove, args.dry_run))
    print(json.dumps({"ok": True, "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
