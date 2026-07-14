---
name: prompt-harness
description: Automatically initialize, reconcile, capture, backfill, search, export, or validate human prompts and user-sent images from Claude Code and Codex in a private per-project ledger. Use whenever a user starts or resumes project conversations and wants all sessions synchronized, asks to archive project prompts or images, extract user messages, set up or diagnose UserPromptSubmit/legacy Stop hooks, inspect prompt history, prepare badcases, or fix missing/duplicated records.
---

# Prompt Harness

Use the bundled `scripts/prompt_harness.py` as the only writer for prompt ledgers. A project ledger lives at `<project>/.prompt-harness/` and is private by default.

## Automatic behavior

After installation and trust, the user should not need to initialize each project manually. Every valid hook invocation:

1. resolves the project and creates `.prompt-harness` if absent;
2. synchronously stores the current prompt and user images;
3. launches a detached `auto-sync` process that performs first-use discovery or reads only appended transcript tails;
4. returns without waiting for the historical scan.

The first valid interaction performs one full Claude/Codex discovery for that project. Every later interaction reconciles immediately from `state/source-cursors.json`: unchanged files are not parsed and changed JSONL files are read from their saved byte offsets. There is no time throttle. A project lock coalesces overlapping requests in `state/auto-sync-pending.json`, and one global lock serializes disk-heavy work across projects. Inspect `state/auto-sync.json` for `mode`, `sources_changed`, `bytes_read`, completion, and failures.

## Core workflow

1. Identify the project root. Prefer an explicit path from the user, then an active native-session binding; otherwise use the nearest existing `.prompt-harness`, Git root, `AGENTS.md`, or `CLAUDE.md`.
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

6. When one conversation has multiple workspace roots, stale `cwd`, or prompts in the wrong project, bind and migrate the native session explicitly:

   ```powershell
   python "$env:PLUGIN_ROOT\scripts\prompt_harness.py" bind-session --platform codex --session-id "<session-id>" --project "<project-root>" --migrate
   ```

   Supply `--source-path` if automatic transcript lookup cannot find the exact native JSONL. Treat the binding ledger as append-only: a later project switch appends a replacement binding. Migration may append exclusions in the prior store but must not delete canonical event rows.

On non-Windows systems, use `python3` and `$PLUGIN_ROOT/scripts/prompt_harness.py`.

## Data contract

- Treat `events/**/*.jsonl` as the append-only source of truth.
- Apply `state/event-supersessions.jsonl` when selecting active events. It compensates for migrated duplicates without deleting raw event lines.
- Apply `state/event-exclusions.jsonl` when selecting active events. It compensates for automatic context that older versions incorrectly captured.
- Treat `assets/manifest.jsonl` as the append-only image-to-event relation ledger and `assets/images/` as content-addressed user-image facts.
- Treat `index/catalog.json`, `index/PROMPTS.md`, `index/sessions.json`, `sessions/**/*.json`, `reports/SESSION_SUMMARIES.md`, and `visualizations/timeline.html` as rebuildable views.
- Treat `P00001` labels as derived chronological positions. Backfilling an earlier prompt must renumber later P labels; use the immutable `event_id` for durable links and badcase references.
- Put project-specific narrative summaries and curated Markdown exports under `reports/`, not in the project root.
- Keep `index/PROMPTS.md` factual: one minimal title, per-event metadata, and exact sanitized human prompt text. Never put project interpretation or extraction methodology in it.
- Keep changing conclusions in separate report files. `SESSION_SUMMARIES.md` is prompt-derived and mutable; a curated `PROJECT_SUMMARY.md` may add analysis when the user requests it.
- Display platform on every record. Display model when captured or reliably derived from the transcript, and label transcript-derived model metadata instead of presenting it as hook-captured.
- Store only user-authored prompt text, user-sent raster images, and minimal provenance.
- Preserve paths written by the user and parseable ordinary attachment paths, but never read those paths merely to copy ordinary file bodies into the ledger.
- Copy only validated PNG, JPEG, GIF, WebP, or BMP image bytes to `assets/images/`. Never download remote image URLs and never store SVG.
- Omit non-image attachment payloads and redact obvious secrets.
- Exclude assistant output, tool results, subagent traffic, injected project/system instructions, local command wrappers, and Claude-to-Codex mirror rows.
- Preserve legitimate repeated prompts as separate events. Prefer native IDs and exact source path/line identities. Use `(turn_id, prompt hash)`, never `turn_id` alone, to match a source row to a live hook event; distinct source lines still remain distinct even when turn and text are identical. Use occurrence matching only during a complete historical scan.
- Reject a user's home directory or drive root as a project root. These catch-all roots can absorb unrelated projects.
- Never commit `.prompt-harness/events`, `assets`, `sessions`, `index`, `reports`, or future badcase run data unless the user explicitly changes the privacy policy.

Read [event-schema.md](../../references/event-schema.md) when changing the event envelope. Read [architecture.md](../../references/architecture.md) when changing ingestion or deduplication. Read [badcase-roadmap.md](../../references/badcase-roadmap.md) before implementing phase 2.

## Hook behavior

The plugin's `UserPromptSubmit` hook performs a bounded local append/copy, queues detached reconciliation, and returns success so source checking does not block the model. Its command resolves an available Prompt Harness launcher at invocation time instead of permanently depending on the versioned plugin root loaded when a task started. Tasks created with version `0.7.0+` therefore survive later plugin cache replacement; if the runtime is entirely absent, the hook exits successfully and records no prompt. Capture exceptions produce prompt-free diagnostics under `~/.prompt-harness/state/`. Full discovery occurs only when source cursors are absent or an operator explicitly forces it; normal turns read changed tails. A standalone installer is available for Claude Code and global Codex hooks. Do not enable both the Codex plugin hook and the global Codex hook:

```powershell
python "$env:PLUGIN_ROOT\scripts\install_hooks.py" --platform claude
```

Codex tasks created before the plugin hook was installed may retain their original plugin-hook set. First verify that a newly created task captures normally. If only old tasks miss prompts, add the optional `Stop` recovery hook instead of adding a second global `UserPromptSubmit` hook:

```powershell
python "$env:PLUGIN_ROOT\scripts\install_hooks.py" --platform codex --codex-hook stop-recovery
```

The recovery command initializes the ledger if needed, reads only the latest human row and its image blocks from the stopped task's own rollout, records `source.mode=stop_recovery`, and queues the same reconciliation path. It reconciles by turn ID plus prompt hash so multiple messages in one turn remain distinct. It is post-turn recovery rather than submission-time capture: the current prompt normally appears after the assistant finishes and the Stop hook runs, so a same-turn read can be early. Interrupted or aborted turns retry through automatic reconciliation on the next interaction.

When diagnosing an old Codex task, do not run manual backfill if the user asked to test automation. Send one test prompt, let the assistant finish, then inspect the project ledger on the following turn. Report `event_id`, `source.mode`, `platform`, `model`, and image count/path. On Windows, inspect `~/.codex/hooks/codex_turn_end.log` if recovery is missing; Stop payload forwarding must use UTF-8 (`ensure_ascii=True`, explicit subprocess `encoding="utf-8"`, and `PYTHONIOENCODING=utf-8`) rather than the console GBK code page.

The installer must back up existing configuration before editing it and preserve unrelated hooks. After installation, verify with a real one-turn prompt in a disposable project and run `doctor` there.

## Failure handling

- If a hook record is missing, inspect the hook payload, transcript path, project-root resolution, and hook trust state before changing the ledger.
- If Codex reports `UserPromptSubmit hook (failed)` after a plugin upgrade, compare the task-start Prompt Harness version with the current cache. A pre-`0.7.0` task may retain a deleted `$PLUGIN_ROOT`; restart that task or use Stop recovery. For `0.7.0+`, inspect `~/.prompt-harness/state/plugin-hook-errors.jsonl` and `hook-errors.jsonl` before changing data.
- If image capture is missing, inspect `state/image-misses.jsonl`, the transcript image block, file existence, raster signature, and per-event limits. Do not fetch remote URLs as a fallback.
- If automatic history is missing, inspect `state/auto-sync.json`, `state/source-cursors.json`, `state/auto-sync-pending.json`, `state/auto-sync-errors.jsonl`, project resolution, and `auto_sync` config before running a manual backfill.
- If a task has multiple roots or its `cwd` points to the wrong project, verify the native session ID and transcript `session_meta`, inspect `list-bindings`, then use `bind-session --migrate` rather than copying or deleting ledger files by hand.
- If historical counts look inflated, check native event IDs, Claude branch copies, subagent folders, injected context, and imported Codex mirrors.
- If a secret or attachment body survives sanitation, stop publishing or exporting, improve the sanitizer, backfill into a fresh retained test project, and rerun `doctor`.
- Do not delete or rewrite canonical JSONL to repair an error without explicit user approval. Prefer a compensating event or a documented migration.

## Reporting

Report the project root, raw/active/superseded/excluded event counts, image relation/file counts, session count, automatic reconciliation mode and changed-source/byte diagnostics, privacy status, doctor result, and locations of the canonical ledger, image assets/manifest, fact Markdown, mutable summary, and local HTML timeline. Distinguish source prompts from imported mirrors and explain missing or transcript-derived model metadata. When citing a prompt, include both its current P number and stable `event_id`; note that P numbers can change after earlier history is backfilled.
