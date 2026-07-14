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
    "mode": "hook | backfill",
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
- A future badcase record references `event_id`; it does not copy or mutate the source prompt.

## Identity

Native event or turn identifiers are preferred for event identity. Historical branch recovery uses exact timestamp plus prompt hash to recognize copied history while retaining all native source IDs. A live hook lacking a native turn identifier receives a fresh nonce so repeated identical human prompts are preserved rather than collapsed.
