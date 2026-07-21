---
name: prompt-harness
description: Automatically initialize, reconcile, capture, backfill, search, export, or validate human prompts, user-sent images, and complete structured agent traces from Claude Code and Codex in a private per-project ledger.
---

# Prompt Harness

Use the bundled `scripts/prompt_harness.py` as the only writer for prompt ledgers. A project ledger lives at `<project>/.prompt-harness/` and is private by default.

## Automatic behavior

After installation and trust, the user should not need to initialize each project manually. A hook invocation is valid for automatic routing only when the native session launch `cwd` exactly equals the resolved project root, or when the native session has an active explicit binding to that project. Every valid hook invocation:

1. resolves the project and creates `.prompt-harness` if absent;
2. synchronously stores the current prompt and user images;
3. launches a detached `auto-sync` process that performs first-use discovery or reads only appended transcript tails;
4. returns without waiting for the historical scan.

On first use, the hook materializes the three lightweight Markdown indexes from the current captured facts before detached historical reconciliation finishes. This ensures a newly created ledger is immediately visible even when older project sessions are large.

The first valid interaction performs one full Claude/Codex discovery for exact-root or explicitly bound sessions in that project. Every later interaction reconciles immediately from `state/source-cursors.json`: unchanged files are not parsed and changed JSONL files are read from their saved byte offsets. Codex incremental reconciliation also enumerates rollout filenames and opens only previously unknown files, so sibling tasks created later with the same exact launch `cwd` are discovered automatically. There is no time throttle. A project lock coalesces overlapping requests in `state/auto-sync-pending.json`, and one global lock serializes disk-heavy work across projects. Inspect `state/auto-sync.json` for `mode`, `sources_changed`, `bytes_read`, completion, and failures.

The plugin uses both `UserPromptSubmit` and `Stop`. Submission captures the human prompt immediately. Stop tails the same native transcript after the agent turn ends so reasoning, tool calls/results, assistant messages, `modelout/<session>.md`, and `trajectory/<session>.md` are current without waiting for another prompt. Treat Stop as turn closure only; sessions remain resumable. Derived indexes report the latest turn as `closed` or `open_or_interrupted`, never as permanently completed.

Project-wide `MODELOUT.md` contains the session index and complete final assistant answers only. Project-wide `TRAJECTORY.md` contains the session index plus complete human prompts and complete final answers in turn order, with reasoning/tool/injection/subagent bodies replaced by type counts. Do not truncate prompt or final-answer text. Do not generate separate Easy-named files or a project-wide full-process duplicate. Complete trace content remains partitioned by session. Rebuilds fingerprint per-session projections and rewrite only sessions whose facts or derived numbering changed.

After trace reconciliation, automatic sync may create high-confidence badcase candidates from explicit user corrections. Treat every candidate as review-only evidence, never as proof that a model failed. Do not change prompts, skills, tests, or project files because of a candidate. Confirm a case only with explicit Red, Green, and expected-failure conditions; dismiss clarification-only signals and merge repeated evidence into an existing case.

Approved `every-dev-completion` feature chains and task cases run only from the detached Stop/Goal reconciliation worker or an explicit `dev-complete` command. Proposal creation never executes a command. Approval requires a real Red reproduction and Green pass, uses JSON argv arrays with `shell=False`, and preserves failed evidence while cleaning disposable successful artifacts. A failed completion check must never fail prompt capture.

## Core workflow

1. Identify the candidate project root. Prefer an explicit path from the user, then an active native-session binding; otherwise use the nearest existing `.prompt-harness`, Git root, `AGENTS.md`, or `CLAUDE.md`. Resolving a parent candidate is not sufficient for automatic capture: require the session launch `cwd` to equal that root exactly. Only an active `bind-session` record may intentionally override this exact-root gate.
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
- Treat `model-events/**/*.jsonl` as the append-only source of truth for typed agent trace events, including assistant text, reasoning/thinking, tool calls/results, injected instructions, system events, and subagent traffic.
- Apply `state/event-supersessions.jsonl` when selecting active events. It compensates for migrated duplicates without deleting raw event lines.
- Apply `state/event-exclusions.jsonl` when selecting active events. It compensates for automatic context that older versions incorrectly captured.
- Treat exact project-root identity as the automatic ownership boundary. Do not include an unbound Claude/Codex session whose launch `cwd` is a child of, parent of, or merely contained by the project root.
- Legacy events captured from descendant sessions remain immutable; reconciliation appends a project-scoped exclusion. An active binding to that project dynamically re-enables the event.
- Treat `assets/manifest.jsonl` as the append-only image-to-event relation ledger and `assets/images/` as content-addressed user-image facts.
- Treat `index/catalog.json`, `index/PROMPTS.md`, `index/MODELOUT.md`, `index/TRAJECTORY.md`, `index/sessions.json`, `sessions/**/*.json`, `reports/SESSION_SUMMARIES.md`, and `visualizations/timeline.html` as rebuildable views.
- Treat `P00001` labels as derived chronological positions. Backfilling an earlier prompt must renumber later P labels; use the immutable `event_id` for durable links and badcase references.
- Put project-specific narrative summaries and curated Markdown exports under `reports/`, not in the project root.
- Keep `index/PROMPTS.md` factual: one minimal title, per-event metadata, and exact sanitized human prompt text. Never put project interpretation or extraction methodology in it.
- Keep `index/MODELOUT.md` factual: chronological `O` labels, event type, actor, provenance, model/phase metadata, subagent linkage, linked prompt event, readable text, and sanitized structured payload.
- Keep `index/TRAJECTORY.md` project-wide but session-safe: include every session, partition by `(platform, session_id)`, then group by native `turn_id`. Put every human message from that turn first, in source order, and only then show linked injections, reasoning, tools, subagents, and answers in native trace order. Put unlinked facts after prompt-backed turns.
- Rebuild matching per-session files under `index/prompt/`, `index/modelout/`, and `index/trajectory/`. Use the same sanitized `time-platform-model-session topic.md` filename in all three directories; create an explicit no-human-prompt file for subagent sessions that have trace facts but no human prompt fact.
- Keep changing conclusions in separate report files. `SESSION_SUMMARIES.md` is prompt-derived and mutable; a curated `PROJECT_SUMMARY.md` may add analysis when the user requests it.
- Display platform on every record. Display model when captured or reliably derived from the transcript, and label transcript-derived model metadata instead of presenting it as hook-captured.
- Store user-authored prompt text, user-sent raster images, and complete local agent trace facts with the provenance needed to reconstruct execution order and relationships.
- Preserve paths written by the user and parseable ordinary attachment paths, but never read those paths merely to copy ordinary file bodies into the ledger.
- Copy only validated PNG, JPEG, GIF, WebP, or BMP image bytes to `assets/images/`. Never download remote image URLs and never store SVG.
- Omit non-image attachment payloads and redact obvious secrets.
- Include reasoning/thinking, tool calls/results, subagent traffic, injected project/system/developer instructions, and runtime system events in the trace ledger. Preserve structured payloads and mark their type; recursively redact obvious secrets and embedded binary data instead of dropping the event category. Continue excluding Claude-to-Codex mirror rows to avoid duplicate provenance.
- Preserve legitimate repeated prompts as separate events. Prefer native IDs and exact source path/line identities. Use `(turn_id, prompt hash)`, never `turn_id` alone, to match a source row to a live hook event; distinct source lines still remain distinct even when turn and text are identical. Use occurrence matching only during a complete historical scan.
- Allow a user's home directory as a project root only under the same exact launch-`cwd` ownership rule. Descendant sessions must not enter the home ledger unless explicitly bound. Reject filesystem and drive roots.
- Never commit `.prompt-harness/events`, `assets`, `sessions`, `index`, `reports`, or badcase run data unless the user explicitly changes the privacy policy.
- Keep badcase candidate, decision, and case lifecycle JSONL append-only. Use `badcase-confirm`, `badcase-decide`, and `badcase-update`; do not hand-edit canonical records.
- Separate issue status (`open/resolved/recurred/deferred`) from Harness lifecycle (`active/stable/probation/retired`). Probation means the compensation is removed while evidence/testing remains; retirement never deletes history.
- Keep guard, feature-chain, task-case, snapshot, adapter, judge, attribution, compensation, policy, and subagent ledgers append-only. Propose first; use dedicated approval commands after executable preflight. Never infer approval from a file an agent wrote.
- Prefer deterministic assertions. Use a separately approved judge only for an outcome that deterministic checks cannot decide. Model replay input must omit correction prompts, historical answers, root causes, fixes, expected failure reasons, and judge-only oracle fields.
- Apply only the smallest explicitly approved compensation to the selected replay/runtime context. Never modify project source or a global prompt automatically. Probation removes compensation while retaining tests; retirement is an explicit event after uncompensated cross-model passes.
- Treat `index/CONTEXT.md`, `index/TEST_HUB.md`, and `index/test-hub/index.html` as rebuildable navigation, never canonical state.

Read [event-schema.md](../../references/event-schema.md) when changing the event envelope. Read [architecture.md](../../references/architecture.md) when changing ingestion or deduplication. Read [badcase-roadmap.md](../../references/badcase-roadmap.md) before changing candidate detection or case contracts. Read [adaptive-harness.md](../../references/adaptive-harness.md) before changing Test Hub, replay, judge, policy, or compensation behavior.

## Hook behavior

The plugin's `UserPromptSubmit` hook performs a bounded local append/copy, queues detached reconciliation, and returns success so source checking does not block the model. Its command resolves an available Prompt Harness launcher at invocation time instead of permanently depending on the versioned plugin root loaded when a task started. Tasks created with version `0.7.0+` therefore survive later plugin cache replacement; if the runtime is entirely absent, the hook exits successfully and records no prompt. Capture exceptions produce prompt-free diagnostics under `~/.prompt-harness/state/`. Full discovery occurs only when source cursors are absent or an operator explicitly forces it; normal turns read changed tails. A standalone installer is available for Claude Code and global Codex hooks. Do not enable both the Codex plugin hook and the global Codex hook:

Codex launchers that inject another model provider, including `aiden x codex`, still share `CODEX_HOME` and may use the enabled Prompt Harness plugin alongside their own inline hooks. Keep Prompt Harness enabled as a plugin instead of adding a second global copy. Hook launchers must suppress Prompt Harness business JSON and emit only a schema-valid neutral Codex hook response. After changing plugin or hook configuration, start a new CLI task because a running process may retain its original hook registry.

```powershell
python "$env:PLUGIN_ROOT\scripts\install_hooks.py" --platform claude
```

Codex tasks created before the plugin hook was installed may retain their original plugin-hook set. First verify that a newly created task captures normally. If only old tasks miss prompts, add the optional `Stop` recovery hook instead of adding a second global `UserPromptSubmit` hook:

```powershell
python "$env:PLUGIN_ROOT\scripts\install_hooks.py" --platform codex --codex-hook stop-recovery
```

The recovery command initializes the ledger if needed, reads only the latest human row and its image blocks from the stopped task's own rollout, records `source.mode=stop_recovery`, and queues the same reconciliation path. It reconciles by turn ID plus prompt hash so multiple messages in one turn remain distinct. It is post-turn recovery rather than submission-time capture: the current prompt normally appears after the assistant finishes and the Stop hook runs, so a same-turn read can be early. Interrupted or aborted turns retry through automatic reconciliation on the next interaction.

Stop recovery reverse-reads the rollout tail instead of parsing the complete transcript in the foreground. Full trace tailing and Markdown projection work are scheduled in the detached reconciliation process.

Automatic discovery and Stop recovery honor `CODEX_HOME`; Claude discovery honors `CLAUDE_CONFIG_DIR`. Keep these variables available to hook subprocesses when native agent data is stored outside the default user-home directories, especially on Windows.

When Codex Desktop's local thread index is available, discovery uses it only as a read-only exact-`cwd` prefilter and still verifies native rollout metadata. If the index is absent or unreadable, transcript discovery remains the fallback.

When diagnosing an old Codex task, do not run manual backfill if the user asked to test automation. Send one test prompt, let the assistant finish, then inspect the project ledger on the following turn. Report `event_id`, `source.mode`, `platform`, `model`, and image count/path. On Windows, inspect `~/.codex/hooks/codex_turn_end.log` if recovery is missing; Stop payload forwarding must use UTF-8 (`ensure_ascii=True`, explicit subprocess `encoding="utf-8"`, and `PYTHONIOENCODING=utf-8`) rather than the console GBK code page.

The installer must back up existing configuration before editing it and preserve unrelated hooks. After installation, verify with a real one-turn prompt in a disposable project and run `doctor` there.

## Failure handling

- If a hook record is missing, inspect the hook payload, transcript path, project-root resolution, and hook trust state before changing the ledger.
- If Codex reports `UserPromptSubmit hook (failed)` after a plugin upgrade, compare the task-start Prompt Harness version with the current cache. A pre-`0.7.0` task may retain a deleted `$PLUGIN_ROOT`; restart that task or use Stop recovery. For `0.7.0+`, inspect `~/.prompt-harness/state/plugin-hook-errors.jsonl` and `hook-errors.jsonl` before changing data.
- If image capture is missing, inspect `state/image-misses.jsonl`, the transcript image block, file existence, raster signature, and per-event limits. Do not fetch remote URLs as a fallback.
- If automatic history is missing, inspect `state/auto-sync.json`, `state/source-cursors.json`, `state/auto-sync-pending.json`, `state/auto-sync-errors.jsonl`, project resolution, and `auto_sync` config before running a manual backfill.
- If a session was started below the project root, treat its absence from the parent ledger as expected exact-root isolation. Start the task at the intended root or explicitly use `bind-session --migrate`; do not weaken the boundary to parent-directory containment.
- If a task has multiple roots or its `cwd` points to the wrong project, verify the native session ID and transcript `session_meta`, inspect `list-bindings`, then use `bind-session --migrate` rather than copying or deleting ledger files by hand.
- If historical counts look inflated, check native event IDs, Claude branch copies, subagent folders, injected context, and imported Codex mirrors.
- If a secret or attachment body survives sanitation, stop publishing or exporting, improve the sanitizer, backfill into a fresh retained test project, and rerun `doctor`.
- Do not delete or rewrite canonical JSONL to repair an error without explicit user approval. Prefer a compensating event or a documented migration.

## Reporting

Report the project root, raw/active/superseded/excluded event counts, image relation/file counts, session count, automatic reconciliation mode and changed-source/byte diagnostics, privacy status, doctor result, and locations of the canonical ledger, image assets/manifest, fact Markdown, mutable summary, and local HTML timeline. Distinguish source prompts from imported mirrors and explain missing or transcript-derived model metadata. When citing a prompt, include both its current P number and stable `event_id`; note that P numbers can change after earlier history is backfilled.
