# Changelog

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
