# Changelog

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
