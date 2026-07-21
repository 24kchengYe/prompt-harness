# Prompt event schema v1

Each line in `events/YYYY/MM/prompts-YYYY-MM-DD.jsonl` is one self-contained JSON object. JSONL is the canonical format because it supports append-only hooks, streaming recovery, diffable migrations, and references from later evaluation records.

```json
{
  "schema_version": "1.0.0",
  "record_type": "user_prompt",
  "event_id": "phe_<stable hash>",
  "captured_at": "2026-07-14T04:00:00.000Z",
  "occurred_at": "2026-07-14T03:59:59.000Z",
  "source": {
    "mode": "hook | stop_recovery | backfill",
    "platform": "claude | codex",
    "path": null,
    "line": null,
    "native_event_id": null,
    "refs": []
  },
  "project": {
    "id": "prj_<root hash>",
    "name": "project-name",
    "root": "G:\\path\\to\\project"
  },
  "session": {
    "id": "native session id",
    "alias_ids": [],
    "turn_id": null,
    "transcript_path": null
  },
  "prompt": {
    "text": "human-authored request",
    "sha256": "...",
    "chars": 22,
    "secret_redactions": 0,
    "attachments_omitted": 0
  },
  "context": {
    "cwd": "G:\\path\\to\\project",
    "model": null,
    "permission_mode": null
  },
  "links": {
    "response_event_id": null,
    "badcase_ids": []
  }
}
```

## Stability rules

- `schema_version`, `record_type`, `event_id`, `occurred_at`, project identity, prompt text, and prompt hash are immutable facts.
- Later schemas may add fields. A breaking semantic change requires a new major version and a migration that retains the original line or provenance.
- Generated indexes are never canonical and may be overwritten.
- A badcase candidate/case references `event_id`; it does not copy or mutate the source prompt.

## Agent trace event

Complete local execution facts are stored separately under
`model-events/YYYY/MM/model-outputs-YYYY-MM-DD.jsonl`. Each row uses the
`agent_trace` envelope and has a stable `trace_event_id` (`ate_...`). The
compatibility alias `model_output_id` contains the same value.

```json
{
  "schema_version": "1.0.0",
  "record_type": "agent_trace",
  "trace_event_id": "ate_<stable hash>",
  "event_type": "assistant_text | reasoning | tool_call | tool_result | system_instruction | developer_instruction | system_event | agent_event",
  "source": {
    "platform": "claude | codex",
    "path": "...",
    "line": 42,
    "block_index": 0,
    "raw_type": "assistant.thinking"
  },
  "session": {
    "id": "native session id",
    "turn_id": null,
    "parent_session_id": null,
    "agent_id": null,
    "is_subagent": false
  },
  "actor": {
    "role": "assistant | tool | system | developer",
    "name": null
  },
  "content": {
    "text": "readable projection, possibly empty",
    "structured": {"type": "native payload"},
    "sha256": "...",
    "chars": 20,
    "secret_redactions": 0,
    "attachments_omitted": 0
  },
  "links": {
    "prompt_event_id": "phe_... or null",
    "tool_call_id": "native call id or null",
    "parent_trace_event_id": null
  }
}
```

One native content block becomes one trace event. Structured tool arguments,
results, reasoning payloads, injected instructions, runtime context, and
subagent content are retained after recursive secret/binary sanitation.
`index/MODELOUT.md` is a readable projection with chronological `O00001`
labels. `index/TRAJECTORY.md` is the project-wide interaction projection:
all sessions are present and isolated by `(platform, session_id)`. Inside each
session, events are grouped by native `turn_id`; every Turn renders all human
messages first in source order, then its linked trace events in
timestamp/path/line/block order. Subagent sessions remain separately
identified, nest under a known parent, and use the linked parent prompt as
their conversational anchor.

For Codex, `session.turn_id` is copied from the native rollout turn metadata.
Claude does not expose the same field: Prompt Harness follows `parentUuid`
ancestry to the nearest human user row and stores that row's `promptId`, or its
`uuid` as fallback, in the normalized `session.turn_id` slot.
Unlinked facts are shown after prompt-backed turns. Derived labels may change
after older history is recovered, while `trace_event_id` remains stable.

## Derived prompt numbers

`P00001`, `P00002`, and similar labels exist only in generated views. They are the one-based chronological rank of active events, ordered first by `occurred_at` and then by deterministic transcript provenance for exact timestamp ties. Backfilling an earlier event intentionally renumbers later P labels. The stable cross-rebuild identity is always `event_id`.

## Append-only supersession

When a schema upgrade has already produced two rows for one native message, Prompt Harness does not delete either canonical JSONL line. It appends a compensating relation to `state/event-supersessions.jsonl`:

```json
{
  "schema_version": "1.0.0",
  "record_type": "event_supersession",
  "supersession_id": "phs_<stable hash>",
  "event_id": "phe_<legacy event>",
  "canonical_event_id": "phe_<clean event>",
  "reason": "legacy_image_omission_migrated_to_image_manifest",
  "recorded_at": "2026-07-14T04:00:02.000Z"
}
```

Raw audit tools may read every event line. Prompt indexes, search, session summaries, and badcase candidate selection use active events after applying supersession relations.

## Append-only exclusion

When an older collector stored machine-injected project/environment context as if it were human input, Prompt Harness keeps the raw event and appends an exclusion to `state/event-exclusions.jsonl`:

```json
{
  "schema_version": "1.0.0",
  "record_type": "event_exclusion",
  "exclusion_id": "phx_<stable hash>",
  "event_id": "phe_<incorrect automatic event>",
  "reason": "automatic_context_not_human_input",
  "recorded_at": "2026-07-14T04:00:03.000Z"
}
```

Active events are raw prompt events minus both superseded and excluded IDs. `catalog.json` reports raw, active, superseded, excluded, and total inactive counts separately.

## Image attachment sidecar

User-sent raster images do not change the prompt event envelope. Bytes are stored at `assets/images/<sha256>.<ext>` and each event relation is appended to `assets/manifest.jsonl`:

```json
{
  "schema_version": "1.0.0",
  "record_type": "prompt_image",
  "attachment_id": "phi_<stable event-and-content hash>",
  "event_id": "phe_<prompt event hash>",
  "captured_at": "2026-07-14T04:00:01.000Z",
  "asset": {
    "path": "assets/images/<sha256>.png",
    "sha256": "<sha256>",
    "bytes": 12345,
    "media_type": "image/png"
  },
  "source": {
    "kind": "local_path | data_url | base64",
    "original_name": "diagram.png",
    "transcript_path": null,
    "line": null
  }
}
```

The attachment ID is deterministic for one `event_id` plus image hash, so Stop recovery and backfill can add missing image relations without duplicating them or rewriting prompt events. Image-only user turns use an empty prompt string and remain valid because the manifest supplies the attached fact.

## Identity

Native event identifiers are preferred for event identity. Historical rows without a native ID use exact source path/line identity before a turn identifier, so even identical text repeated inside one Codex turn remains distinct. A live turn identifier is always combined with the prompt hash, and reconciliation can use that pair to match the later source row to the live event. Historical branch recovery uses exact timestamp plus prompt hash to recognize copied history while retaining all native source IDs. A live hook lacking both source and native turn identity receives a fresh nonce so repeated identical human prompts are preserved rather than collapsed.

## Session-to-project binding side ledger

Project routing is not part of the prompt event envelope. Explicit routing decisions are appended to the private user-level `~/.prompt-harness/session-bindings.jsonl` ledger:

```json
{
  "schema_version": "1.0.0",
  "record_type": "session_project_binding",
  "binding_id": "phb_<stable hash>",
  "platform": "codex",
  "session_id": "native session id",
  "project_id": "prj_<root hash>",
  "project_root": "D:\\path\\to\\project",
  "source_path": "C:\\Users\\me\\.codex\\sessions\\...jsonl",
  "reason": "explicit",
  "replaces_binding_id": null,
  "recorded_at": "2026-07-15T04:00:00.000Z"
}
```

The latest valid record for `(platform, session_id)` is active. Rebinding appends; it does not rewrite prior decisions. This ledger contains routing metadata only, never prompt bodies or image bytes.

## Badcase side ledgers

Badcase intake and adaptive regression do not change prompt or trace envelopes. They use project-local append-only side ledgers:

- `badcases/candidates.jsonl`: deterministic `badcase_candidate` records. Candidates reference prompt and trace IDs and must set `detector.asserts_failure` to `false`.
- `badcases/decisions.jsonl`: `badcase_decision` records with action `confirmed`, `dismissed`, or `merged`.
- `badcases/case-events.jsonl`: `badcase_case_event` records that create or update one `BC-YYYYMMDD-XXXXXXXX` case.
- `badcases/feature-chain-events.jsonl`: checkpoint-based workflow guard proposals, approval, policy, and state transitions.
- `badcases/task-case-events.jsonl`: ordered multi-phase workflow proposals, approval commands, and state transitions.
- `badcases/snapshot-events.jsonl`: sanitized deterministic project manifests (`snp_...`).
- `badcases/adapter-events.jsonl`: proposed/approved model protocol adapters (`MA-...`).
- `badcases/judge-events.jsonl`: judge registry events plus immutable replay outcome decisions.
- `badcases/run-events.jsonl`: started/completed Red, Green, Test Hub, replay, and judge run facts (`hrn_...`).
- `badcases/attribution-events.jsonl`: manual attribution overrides that never rewrite the original run.
- `badcases/compensation-events.jsonl`: minimal compensation proposal, approval, activation, probation, recurrence, supersession, and retirement (`CP-...`).
- `badcases/policy-events.jsonl`: bounded execution and snapshot-materialization policies.
- `badcases/subagent-events.jsonl`: exact-root child bindings and idempotent completion evidence.

A confirmed case carries separate issue, guard, compensation, and Harness lifecycle states. Its full v2 contract includes scope, tags, frequency, reproduction, verification, run/artifact/blocker policies, Red, Green, expected failure, reusable guard, feature/task coverage, recurrence, and route-change notes. Schemas for every canonical ledger live in `schemas/`; executable protocol details live in [adaptive-harness.md](adaptive-harness.md).
