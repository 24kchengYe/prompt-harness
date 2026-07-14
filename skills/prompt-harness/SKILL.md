---
name: prompt-harness
description: Capture, backfill, search, export, or validate human prompts from Claude Code and Codex in a private per-project ledger. Use whenever a user asks to archive project prompts, extract user messages from AI sessions, set up a UserPromptSubmit hook, inspect prompt history, prepare prompts for badcase analysis, or diagnose missing/duplicated prompt records.
---

# Prompt Harness

Use the bundled `scripts/prompt_harness.py` as the only writer for prompt ledgers. A project ledger lives at `<project>/.prompt-harness/` and is private by default.

## Core workflow

1. Identify the project root. Prefer an explicit path from the user; otherwise use the nearest existing `.prompt-harness`, Git root, `AGENTS.md`, or `CLAUDE.md`.
2. Initialize the ledger:

   ```powershell
   python "$env:PLUGIN_ROOT\scripts\prompt_harness.py" init --project "<project-root>"
   ```

3. For historical recovery, backfill both platforms and rebuild the readable index:

   ```powershell
   python "$env:PLUGIN_ROOT\scripts\prompt_harness.py" backfill --project "<project-root>" --platform all --rebuild-index
   ```

4. Validate before reporting success:

   ```powershell
   python "$env:PLUGIN_ROOT\scripts\prompt_harness.py" doctor --project "<project-root>"
   ```

5. Search when the user asks what they previously told the model:

   ```powershell
   python "$env:PLUGIN_ROOT\scripts\prompt_harness.py" search "<query>" --project "<project-root>" --limit 20
   ```

On non-Windows systems, use `python3` and `$PLUGIN_ROOT/scripts/prompt_harness.py`.

## Data contract

- Treat `events/**/*.jsonl` as the append-only source of truth.
- Treat `index/catalog.json`, `index/PROMPTS.md`, and `sessions/**/*.json` as rebuildable views.
- Store only user-authored prompt text and minimal provenance.
- Preserve prompt paths written by the user, but never read those paths merely to copy file bodies into the ledger.
- Omit attachment payloads and redact obvious secrets.
- Exclude assistant output, tool results, subagent traffic, injected project/system instructions, local command wrappers, and Claude-to-Codex mirror rows.
- Preserve legitimate repeated prompts as separate events. Merge only historical branch copies that share a native event identifier.
- Never commit `.prompt-harness/events`, `sessions`, `index`, or future badcase run data unless the user explicitly changes the privacy policy.

Read [event-schema.md](../../references/event-schema.md) when changing the event envelope. Read [architecture.md](../../references/architecture.md) when changing ingestion or deduplication. Read [badcase-roadmap.md](../../references/badcase-roadmap.md) before implementing phase 2.

## Hook behavior

The plugin's `UserPromptSubmit` hook performs a fast append and returns success so prompt capture does not block the model. A standalone installer is available for Claude Code and global Codex hooks. Do not enable both the Codex plugin hook and the global Codex hook:

```powershell
python "$env:PLUGIN_ROOT\scripts\install_hooks.py" --platform claude
```

The installer must back up existing configuration before editing it and preserve unrelated hooks. After installation, verify with a real one-turn prompt in a disposable project and run `doctor` there.

## Failure handling

- If a hook record is missing, inspect the hook payload, transcript path, project-root resolution, and hook trust state before changing the ledger.
- If historical counts look inflated, check native event IDs, Claude branch copies, subagent folders, injected context, and imported Codex mirrors.
- If a secret or attachment body survives sanitation, stop publishing or exporting, improve the sanitizer, backfill into a fresh retained test project, and rerun `doctor`.
- Do not delete or rewrite canonical JSONL to repair an error without explicit user approval. Prefer a compensating event or a documented migration.

## Reporting

Report the project root, event count by platform, session count, privacy status, doctor result, and locations of the canonical ledger and readable Markdown index. Distinguish source prompts from imported mirrors and explain any exclusions.
