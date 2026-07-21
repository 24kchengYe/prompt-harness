#!/usr/bin/env python3
"""Best-effort entry point for the Prompt Harness plugin hook.

The Codex hook command locates the newest installed copy of this launcher at
run time. That indirection keeps long-lived tasks working after a plugin
upgrade removes the versioned cache directory they originally loaded.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def harness_home() -> Path:
    configured = os.environ.get("PROMPT_HARNESS_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".prompt-harness"


def record_launcher_error(error: BaseException | str, *, script: Path) -> None:
    """Record diagnostics without persisting stdin or prompt text."""

    if isinstance(error, BaseException):
        error_type = type(error).__name__
        message = str(error)
    else:
        error_type = "HookExit"
        message = error
    entry: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "component": "plugin_hook_launcher",
        "error_type": error_type,
        "message": message[:1000],
        "script": str(script),
        "plugin_root": os.environ.get("PLUGIN_ROOT"),
    }
    with contextlib.suppress(Exception):
        path = harness_home() / "state" / "plugin-hook-errors.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")


def main() -> int:
    script = Path(__file__).resolve().parents[1] / "scripts" / "prompt_harness.py"
    if not script.is_file():
        record_launcher_error("prompt_harness.py was not found", script=script)
        return 0

    prior_argv = sys.argv
    sys.argv = [str(script), "capture-hook", "--platform", "codex"]
    try:
        # Prompt Harness writes operator-facing business JSON, while Codex
        # validates hook stdout against its own hook-response schema.
        with contextlib.redirect_stdout(io.StringIO()):
            runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        if code:
            record_launcher_error(f"capture-hook exited with code {code}", script=script)
    except Exception as exc:  # A logging hook must not block or distract the active task.
        record_launcher_error(exc, script=script)
    finally:
        sys.argv = prior_argv
    sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
