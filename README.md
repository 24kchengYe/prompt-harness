# Prompt Harness

Prompt Harness turns human instructions from Claude Code and Codex into a private, structured, project-local event ledger. It combines a `UserPromptSubmit` hook for new prompts with historical backfill for existing sessions, giving future evaluation and badcase tooling a stable source of truth.

> 当前阶段只做好“提示词事实层”。badcase 分析、可复现测试和模型回归是 phase 2；它们将通过不可变的 `event_id` 引用提示词，不需要破坏现有格式。

## Why

AI coding work is scattered across sessions, branches, clients, and imported archives. Raw transcripts are too noisy for systematic review: they mix human requests with injected instructions, attachments, tools, assistant output, subagents, and duplicate mirrors. Prompt Harness keeps only the human-authored instruction stream and retains enough provenance to trace each record back to its source.

## Per-project layout

```text
<project>/.prompt-harness/
├── config.json                       # project identity and privacy policy
├── events/YYYY/MM/prompts-*.jsonl    # append-only canonical events
├── sessions/{claude,codex}/*.json    # rebuildable session summaries
├── index/catalog.json                # counts and coverage
├── index/PROMPTS.md                  # readable generated export
├── reports/*.md                      # project-specific analyses/curated reports
├── state/                            # locks and ingestion state
└── badcases/                         # reserved phase-2 namespace
```

The nested `.gitignore` excludes prompt bodies, indexes, reports, state, and badcase data by default. The global registry at `~/.prompt-harness/projects.json` stores only project locations and contains no prompt text.

## Install in Codex

The repository is a Codex plugin with a bundled `UserPromptSubmit` hook and skill. Clone it into a Codex marketplace's `plugins/prompt-harness` directory, add that marketplace if it is not the built-in personal marketplace, and then install the plugin from the marketplace name:

```powershell
codex plugin marketplace add "C:\path\to\marketplace-root"
codex plugin add prompt-harness@<marketplace-name>
```

For local development through the automatically discovered personal marketplace at `~/.agents/plugins/marketplace.json`, install the entry as `prompt-harness@personal`; no `marketplace add` command is needed for that default location.

Codex project hooks require trust. Inspect them with `/hooks`; for unattended test runs only, Codex also supports its explicit hook-trust bypass flag.

## Install standalone hooks

The standalone installer preserves unrelated settings and creates a timestamped backup before every changed config file. Use it for Claude Code. For Codex, choose either the plugin hook or the standalone global hook, not both.

```powershell
python scripts/install_hooks.py --platform claude
```

It targets `~/.codex/hooks.json` and `~/.claude/settings.json`. To preview without writing:

```powershell
python scripts/install_hooks.py --platform claude --dry-run
```

## Use

```powershell
# Initialize a project
python scripts/prompt_harness.py init --project "G:\path\to\project"

# Recover historical Claude Code + Codex prompts and generate Markdown
python scripts/prompt_harness.py backfill --project "G:\path\to\project" --platform all --rebuild-index

# Search human prompts
python scripts/prompt_harness.py search "major revision" --project "G:\path\to\project"

# Validate IDs, hashes, privacy guards, and project identity
python scripts/prompt_harness.py doctor --project "G:\path\to\project"
```

On macOS/Linux, replace `python` with `python3` where needed.

## What is and is not stored

Stored:

- human-authored prompt text;
- platform/session/turn identifiers when available;
- timestamps, source references, project root, model and permission metadata;
- hashes, sanitation counts, and future badcase links.

Excluded:

- assistant messages and tool results;
- subagent traffic and sidechains;
- injected `AGENTS.md`, environment, permission, and continuation wrappers;
- raw image/document/base64 bodies;
- obvious credentials and tokens;
- Claude rows merely mirrored into an imported Codex archive.

Paths explicitly typed by a human remain text. Prompt Harness never opens the referenced file just to copy its contents into the ledger.

## Design documents

- [Event schema](references/event-schema.md)
- [Architecture and deduplication](references/architecture.md)
- [Badcase harness roadmap](references/badcase-roadmap.md)
- [Privacy model](PRIVACY.md)

## Development

```powershell
python -m unittest discover -s tests -v
python scripts/prompt_harness.py doctor --project <test-project>
```

Prompt Harness is standard-library-only at runtime. It supports Windows, macOS, and Linux; the initial integration is tested most heavily on Windows.

## Status

Version `0.1.0` implements phase 1: capture, backfill, deduplicate, index, search, and validate. The badcase directory and event links are deliberately reserved for a later harness without prematurely fixing the failure taxonomy.

## License

MIT
