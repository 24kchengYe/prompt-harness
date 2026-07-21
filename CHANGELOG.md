# Changelog

## 0.12.1 - 2026-07-21

- Emit schema-valid neutral JSON from Codex Prompt Harness hook launchers while keeping capture diagnostics out of hook stdout.
- Install standalone Codex hooks through the protocol-safe launchers instead of invoking business CLI commands directly.
- Prune missing or archived selected rollout cursors during incremental reconciliation so one stale path cannot stop project updates.
- Add regression coverage for strict Hook output parsing and stale source cursor recovery.

## 0.12.0 - 2026-07-18

- Add review-gated feature chains, ordered task cases, Red/Green approval preflights, checkpoint markers, bounded parallel Test Hub runs, compact Markdown, and read-only HTML status views.
- Add sanitized manifest-only project snapshots and approved model-adapter replay matrices that pin one case/snapshot while excluding correction prompts, historical answers, root causes, fixes, and judge-only fields from model input.
- Add deterministic outcome assertions, separately approved narrow judge adapters, explicit run attribution, and evidence preservation for task, environment, runtime, policy, judge, and protocol failures.
- Add proposal-only adaptive compensations with baseline-fail/compensated-pass approval, activation, supersession, probation, recurrence reactivation, and retirement recommendations.
- Add per-project/case/test execution policies, token/cost/attempt budgets, exact-root subagent bindings, idempotent completion evidence, and a concise regenerated `index/CONTEXT.md`.
- Run approved completion checks from detached Stop/Goal reconciliation without blocking prompt capture; failures remain visible and cannot suppress independent passes.
- Expand `doctor`, schemas, Test Hub views, CLI routing, and cross-platform regression coverage for every new append-only ledger.

## 0.11.0 - 2026-07-18

- Add a deterministic, no-model `explicit-user-correction-v1` detector that runs once after automatic reconciliation and creates review-only badcase candidates without asserting failure.
- Link candidates to the correction prompt, preceding prompt, intervening/linked trace IDs, session, model, and detector signals while keeping prompt/trace ledgers unchanged.
- Add append-only candidate decisions (`confirmed`, `dismissed`, `merged`) and append-only confirmed-case lifecycle events.
- Require Red, Green, and expected-failure conditions when confirming a case; keep issue status separate from Harness lifecycle (`active`, `stable`, `probation`, `retired`).
- Add `badcase-detect`, `badcase-list`, `badcase-confirm`, `badcase-decide`, and `badcase-update` commands.
- Generate project-wide `index/BADCASES.md` and one compact evidence file per confirmed case, preserving complete prompts/final answers while linking full reasoning/tool/injection/subagent traffic through the session trajectory.
- Extend `doctor`, catalog metadata, privacy defaults, schemas, documentation, and regression tests for candidate/case integrity.
- Coalesce automatic ingestion, candidate detection, and dirty projection into at most one index rebuild per sync worker.

## 0.10.0 - 2026-07-16

- Archive Claude Code and Codex execution facts as append-only typed `agent_trace` events: assistant text, reasoning/thinking, tool calls/results, system/developer injection, runtime system events, and subagent traffic.
- Generate `index/MODELOUT.md` with chronological `O` labels, stable `trace_event_id` values, actor/type/model/phase/subagent provenance, structured JSON payloads, and links to prompt `event_id` values.
- Generate project-wide `index/TRAJECTORY.md`, isolating all conversations by platform/session and nesting separately identified subagent trajectories under their parent conversation.
- Generate matching per-session Markdown files under `index/prompt/`, `index/modelout/`, and `index/trajectory/`, named from UTC time, platform, model, and the first human prompt-derived session topic.
- Render trajectory files as native-turn conversational groups: all human messages in one `turn_id` appear before linked injections, reasoning, tool calls/results, subagent facts, and assistant answers, regardless of raw transcript write order.
- Reconcile output-only transcript tails so a completed assistant response is added even when no new human prompt appears in that incremental chunk.
- Add trace privacy configuration, recursive secret/binary redaction, schema validation, `doctor` coverage, and Claude/Codex/subagent regression tests.
- Fix `doctor` falsely treating the `[REDACTED_SECRET]` placeholder as a remaining secret.
- Add project trajectory totals for sessions, platforms, turns, prompts, trace events, and latest-turn closure.
- Add a plugin `Stop` hook so model output and trajectory projections close each turn automatically.
- Fix incremental Codex discovery so new sibling tasks with the same exact launch directory are added after first initialization.
- Replace per-event model-output rescanning with locked bulk deduplication and append for large historical backfills.
- Keep project-wide `MODELOUT.md` as final-answer aggregation and `TRAJECTORY.md` as prompt/final-answer conversation aggregation while retaining complete per-session projections.
- Replace intermediate event bodies in project-wide trajectories with compact type counts, without truncating prompts or final answers.
- Fingerprint per-session derived projections so routine hook rebuilds do not rewrite unchanged large Markdown files.
- Read the latest Codex prompt from the end of the rollout for Stop handling instead of parsing the complete growing transcript in the foreground.
- Honor `CODEX_HOME` and `CLAUDE_CONFIG_DIR` in automatic discovery, Stop recovery, backfill, and session migration so non-default Windows and cross-platform data directories remain discoverable.
- Use Codex Desktop's read-only thread index when available to select exact-project rollout IDs before opening transcripts, avoiding a first-use metadata scan across very large unrelated global session trees.
- Materialize the three lightweight indexes immediately on the first live capture, so a new project shows prompt/session files before detached historical trace reconciliation finishes.

## 0.8.1 - 2026-07-15

- Allow a user's home directory to host its own Prompt Harness ledger now that automatic ownership requires exact launch-directory equality.
- Keep descendant sessions isolated from the home ledger; they may create their own ledger or use an explicit binding.
- Continue rejecting filesystem and drive roots, and add regression coverage for exact-home capture.

## 0.8.0 - 2026-07-15

- Require an exact normalized match between a Claude/Codex session launch directory and the project root for automatic live capture, Stop recovery, full discovery, and incremental reconciliation.
- Keep append-only session bindings authoritative so explicitly routed cross-root sessions remain supported while parent projects no longer absorb unbound descendant sessions.
- Append dynamic exclusions for legacy descendant events without deleting canonical JSONL; binding the session back to the project re-enables those events.
- Replace full-scan cursor sets authoritatively and prune stale out-of-scope transcript cursors from incremental checks.
- Add regression coverage for Claude and Codex backfill, live hooks, Stop recovery, legacy repair, and cursor pruning across root/child boundaries.

## 0.7.0 - 2026-07-15

- Add a version-resilient plugin hook launcher that resolves the newest available Prompt Harness cache at invocation time, so long-lived Codex CLI and Desktop tasks survive plugin upgrades that remove their originally loaded version directory.
- Make a missing plugin runtime a successful no-op instead of a visible `UserPromptSubmit hook (failed)` error.
- Suppress capture-runtime exceptions at the hook boundary and record prompt-free diagnostics under `~/.prompt-harness/state/`.
- Add regression coverage for deleted old caches, absent runtimes, and privacy-safe launcher failures.

## 0.6.0 - 2026-07-15

- Add an append-only user-level session binding ledger so a Claude or Codex conversation can be explicitly assigned to one project even when a task exposes multiple workspace roots or stale `cwd` metadata.
- Make live capture, Codex Stop recovery, full discovery, and incremental reconciliation honor the latest explicit session binding.
- Prefer an explicitly supplied Codex transcript's native `session_meta` over stale Stop payload identity and working-directory fields.
- Add `bind-session --migrate` to backfill one exact native session into its destination project, copy retained user images, and append exclusions in any previously assigned project store without deleting canonical event rows.
- Keep rebinding auditable: every switch appends a new `session_project_binding` record and the latest record wins.
- Force CLI stdout/stderr to UTF-8 so prompt search and migration JSON remain printable on Windows systems whose inherited console encoding is GBK.

## 0.5.0 - 2026-07-14

- Replace five-minute throttled full scans with one first-use discovery followed by cursor-based JSONL tail reconciliation after every prompt.
- Serialize disk-heavy discovery across all projects with a global lock and coalesce overlapping per-project requests in a durable pending queue.
- Preserve multiple distinct user messages that share one Codex turn by reconciling `turn_id` together with the prompt hash.
- Reject home-directory and drive-root catch-all projects so unrelated transcript histories cannot be absorbed into one ledger.
- Append exclusions for previously captured automatic AGENTS/environment envelopes instead of deleting canonical event rows.
- Track source cursors, changed-source counts, and bytes read for performance diagnosis while rebuilding prompt order after every reconciliation.
- Cache transcript-derived model metadata and skip redundant derived-view rebuilds when neither events nor image relations changed.

## 0.4.1 - 2026-07-14

- Define `P00001` labels as rebuildable chronological positions that intentionally renumber when earlier history is recovered.
- Keep immutable `event_id` values visible in Markdown, search output, and timeline details for durable references.
- Use transcript path, source line, native message identity, and event ID as deterministic tie-breakers when occurrence timestamps are equal.

## 0.4.0 - 2026-07-14

- Automatically create a project ledger on the first captured message and launch a detached full-project Claude/Codex reconciliation.
- Reconcile on every newly observed session and after a five-minute interval for resumed sessions, with a project-level lock to prevent overlapping scans.
- Let legacy Codex Stop recovery bootstrap and reconcile projects even when the immediate plugin hook was unavailable.
- Persist automatic reconciliation status, trigger, session history, timing, and result under `state/auto-sync.json` for diagnosis.
- Match historical rows by native message ID, turn ID, or source path/line before prompt hashes, preventing attachment-format upgrades from duplicating prompts.
- Add append-only supersession relations for legacy image-omission duplicates so derived views remain clean without deleting canonical history.

## 0.3.0 - 2026-07-14

- Archive user-sent PNG, JPEG, GIF, WebP, and BMP images under project-local, content-addressed `assets/images/` paths.
- Link image assets to immutable prompt events through an append-only attachment manifest and embed them in `index/PROMPTS.md`.
- Recover images from Claude base64 blocks, Codex data URLs, and Codex local-image paths during live capture, Stop recovery, and historical backfill.
- Preserve parseable ordinary file-attachment paths in prompt text without copying file bodies.
- Validate image paths, hashes, sizes, event links, and privacy ignores in `doctor`.
- Increase hook timeouts to accommodate bounded local image copies without network access.

## 0.2.3 - 2026-07-14

- Read hook payloads from UTF-8 bytes instead of the Windows console code page.
- Document UTF-8-safe forwarding for existing Codex Stop adapters.

## 0.1.1 - 2026-07-14

- Add a private `reports/` namespace for project-specific analyses and curated prompt exports.
- Keep project roots free of duplicate prompt reports.

## 0.1.0 - 2026-07-14

- Add project-local append-only prompt event ledger.
- Add Codex and Claude Code `UserPromptSubmit` capture.
- Add Claude Code and Codex historical backfill with mirror and branch-copy filtering.
- Add Markdown/catalog generation, search, project registry, and doctor checks.
- Reserve stable event links and directories for a future badcase regression harness.
