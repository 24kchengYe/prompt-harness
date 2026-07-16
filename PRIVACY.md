# Privacy model

Prompt histories can contain unpublished work, personal context, file paths, and credentials. Prompt Harness therefore treats every project ledger as private local data.

## Defaults

- Prompt bodies and complete structured agent traces live under `<project>/.prompt-harness/`.
- A nested `.gitignore` excludes canonical events, generated indexes, session summaries, state, and badcase data.
- The global project registry stores locations and timestamps only, never prompt bodies.
- The append-only global session binding ledger stores platform, native session ID, project path, optional local transcript path, and binding timestamps only. It never copies prompt bodies or image bytes.
- Runtime capture redacts common API-key, access-token, password, and bearer-token patterns.
- User-sent raster images are copied into project-local, content-addressed `assets/images/` files and linked by `assets/manifest.jsonl`.
- Ordinary file bodies and non-image attachment payloads are never copied. A parseable local attachment path is retained as prompt text.
- Assistant text, reasoning/thinking, tool calls/results, subagent traffic, and machine-injected instructions are stored as typed `agent_trace` records under `model-events/`, rendered individually to `index/MODELOUT.md`, and grouped by session in `index/TRAJECTORY.md`.
- Structured values are recursively sanitized for obvious secrets and embedded binary payloads, but an event is never dropped merely because it is reasoning, tool traffic, injected context, or subagent content.
- Automatic reconciliation scans only local Claude/Codex transcript files associated with the resolved project. It performs one first-use discovery, then reads only changed source tails from local cursors. It does not upload prompts or call a model.

Redaction is a safety net, not a proof that arbitrary secrets are impossible. Run `doctor`, inspect any export, and use a secret scanner before publishing data. The source code may be public; prompt ledgers should remain private unless each record has been reviewed and deliberately released.

## File references

If a human writes a path in a prompt, that path stays in the prompt text. For an ordinary native file attachment, Prompt Harness keeps a parseable path but does not open or copy the file body. User-sent raster images are the explicit exception: PNG, JPEG, GIF, WebP, and BMP bytes are copied locally, bounded to 20 images and 50 MB total per event. Remote image URLs are not downloaded, SVG is rejected, and image assets are ignored by Git with the rest of the private ledger.

## Configuration safety

The standalone installer creates a timestamped backup before changing Claude Code or Codex hook configuration and preserves unrelated hook entries. It does not inspect or upload authentication values.

Automatic reconciliation is enabled by default and runs in a detached local process. Configure `auto_sync.enabled`, `auto_sync.platform`, or `auto_sync.background` in the project `config.json` to disable or limit it. Source cursors, pending requests, and diagnostic state remain inside the ignored `.prompt-harness/state/` directory. Home directories and drive roots are rejected as project roots to prevent accidental cross-project collection.

Set `privacy.store_agent_trace` to `false` in project `config.json` to stop adding new trace events. The legacy `privacy.store_model_outputs` setting remains a compatibility fallback. Existing private trace facts are not deleted automatically.

Explicit session migration is local and append-only. It may copy already archived user images between two private project stores, then append an exclusion relation to the old store. It does not delete source event rows or upload any content.
