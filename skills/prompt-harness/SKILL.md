---
name: prompt-harness
description: Capture, backfill, search, export, or validate human prompts and user-sent images from Claude Code and Codex in a private per-project ledger. Use whenever a user asks to archive project prompts or images, extract user messages from AI sessions, set up or diagnose a UserPromptSubmit or legacy Stop recovery hook, inspect prompt history, prepare prompts for badcase analysis, or diagnose missing/duplicated prompt records.
---

# Prompt Harness

Use the bundled `scripts/prompt_harness.py` as the only writer for prompt ledgers. A project ledger lives at `<project>/.prompt-harness/` and is private by default.

## Core workflow

1. Identify the project root. Prefer an explicit path from the user; otherwise use the nearest existing `.prompt-harness`, Git root, `AGENTS.md`, or `CLAUDE.md`.
2. Initialize the ledger:

   ```powershell
   python "$env:PLUGIN_ROOT\scripts\prompt_harness.py" init --project "<project-root>"
   ```

3. For historical recovery, backfill both platforms and rebuild the readable Markdown and HTML views:

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
- Treat `assets/manifest.jsonl` as the append-only image-to-event relation ledger and `assets/images/` as content-addressed user-image facts.
- Treat `index/catalog.json`, `index/PROMPTS.md`, `index/sessions.json`, `sessions/**/*.json`, `reports/SESSION_SUMMARIES.md`, and `visualizations/timeline.html` as rebuildable views.
- Put project-specific narrative summaries and curated Markdown exports under `reports/`, not in the project root.
- Keep `index/PROMPTS.md` factual: one minimal title, per-event metadata, and exact sanitized human prompt text. Never put project interpretation or extraction methodology in it.
- Keep changing conclusions in separate report files. `SESSION_SUMMARIES.md` is prompt-derived and mutable; a curated `PROJECT_SUMMARY.md` may add analysis when the user requests it.
- Display platform on every record. Display model when captured or reliably derived from the transcript, and label transcript-derived model metadata instead of presenting it as hook-captured.
- Store only user-authored prompt text, user-sent raster images, and minimal provenance.
- Preserve paths written by the user and parseable ordinary attachment paths, but never read those paths merely to copy ordinary file bodies into the ledger.
- Copy only validated PNG, JPEG, GIF, WebP, or BMP image bytes to `assets/images/`. Never download remote image URLs and never store SVG.
- Omit non-image attachment payloads and redact obvious secrets.
- Exclude assistant output, tool results, subagent traffic, injected project/system instructions, local command wrappers, and Claude-to-Codex mirror rows.
- Preserve legitimate repeated prompts as separate events. Merge only historical branch copies that share a native event identifier.
- Never commit `.prompt-harness/events`, `assets`, `sessions`, `index`, `reports`, or future badcase run data unless the user explicitly changes the privacy policy.

Read [event-schema.md](../../references/event-schema.md) when changing the event envelope. Read [architecture.md](../../references/architecture.md) when changing ingestion or deduplication. Read [badcase-roadmap.md](../../references/badcase-roadmap.md) before implementing phase 2.

## Hook behavior

The plugin's `UserPromptSubmit` hook performs a bounded local append/copy and returns success so prompt capture does not block the model. A standalone installer is available for Claude Code and global Codex hooks. Do not enable both the Codex plugin hook and the global Codex hook:

```powershell
python "$env:PLUGIN_ROOT\scripts\install_hooks.py" --platform claude
```

Codex tasks created before the plugin hook was installed may retain their original plugin-hook set. First verify that a newly created task captures normally. If only old tasks miss prompts, add the optional `Stop` recovery hook instead of adding a second global `UserPromptSubmit` hook:

```powershell
python "$env:PLUGIN_ROOT\scripts\install_hooks.py" --platform codex --codex-hook stop-recovery
```

The recovery command reads only the latest human row and its image blocks from the stopped task's own rollout, records `source.mode=stop_recovery`, and reconciles by turn ID so it can coexist with the plugin's immediate hook. It is post-turn recovery rather than submission-time capture: the current prompt normally appears after the assistant finishes and the Stop hook runs, so a same-turn read can be early. Interrupted or aborted turns may still require historical backfill.

When diagnosing an old Codex task, do not run manual backfill if the user asked to test automation. Send one test prompt, let the assistant finish, then inspect the project ledger on the following turn. Report `event_id`, `source.mode`, `platform`, `model`, and image count/path. On Windows, inspect `~/.codex/hooks/codex_turn_end.log` if recovery is missing; Stop payload forwarding must use UTF-8 (`ensure_ascii=True`, explicit subprocess `encoding="utf-8"`, and `PYTHONIOENCODING=utf-8`) rather than the console GBK code page.

The installer must back up existing configuration before editing it and preserve unrelated hooks. After installation, verify with a real one-turn prompt in a disposable project and run `doctor` there.

## Failure handling

- If a hook record is missing, inspect the hook payload, transcript path, project-root resolution, and hook trust state before changing the ledger.
- If image capture is missing, inspect `state/image-misses.jsonl`, the transcript image block, file existence, raster signature, and per-event limits. Do not fetch remote URLs as a fallback.
- If historical counts look inflated, check native event IDs, Claude branch copies, subagent folders, injected context, and imported Codex mirrors.
- If a secret or attachment body survives sanitation, stop publishing or exporting, improve the sanitizer, backfill into a fresh retained test project, and rerun `doctor`.
- Do not delete or rewrite canonical JSONL to repair an error without explicit user approval. Prefer a compensating event or a documented migration.

## Reporting

Report the project root, event count by platform, image count, session count, privacy status, doctor result, and locations of the canonical ledger, image assets/manifest, fact Markdown, mutable summary, and local HTML timeline. Distinguish source prompts from imported mirrors and explain missing or transcript-derived model metadata.
