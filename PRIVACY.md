# Privacy model

Prompt histories can contain unpublished work, personal context, file paths, and credentials. Prompt Harness therefore treats every project ledger as private local data.

## Defaults

- Prompt bodies live under `<project>/.prompt-harness/`.
- A nested `.gitignore` excludes canonical events, generated indexes, session summaries, state, and badcase data.
- The global project registry stores locations and timestamps only, never prompt bodies.
- Runtime capture redacts common API-key, access-token, password, and bearer-token patterns.
- User-sent raster images are copied into project-local, content-addressed `assets/images/` files and linked by `assets/manifest.jsonl`.
- Ordinary file bodies and non-image attachment payloads are never copied. A parseable local attachment path is retained as prompt text.
- Assistant output, tools, subagents, and machine-injected instructions are excluded.
- Automatic reconciliation scans only local Claude/Codex transcript files associated with the resolved project. It does not upload prompts or call a model.

Redaction is a safety net, not a proof that arbitrary secrets are impossible. Run `doctor`, inspect any export, and use a secret scanner before publishing data. The source code may be public; prompt ledgers should remain private unless each record has been reviewed and deliberately released.

## File references

If a human writes a path in a prompt, that path stays in the prompt text. For an ordinary native file attachment, Prompt Harness keeps a parseable path but does not open or copy the file body. User-sent raster images are the explicit exception: PNG, JPEG, GIF, WebP, and BMP bytes are copied locally, bounded to 20 images and 50 MB total per event. Remote image URLs are not downloaded, SVG is rejected, and image assets are ignored by Git with the rest of the private ledger.

## Configuration safety

The standalone installer creates a timestamped backup before changing Claude Code or Codex hook configuration and preserves unrelated hook entries. It does not inspect or upload authentication values.

Automatic reconciliation is enabled by default and runs in a detached local process. Configure `auto_sync.enabled`, `auto_sync.min_interval_seconds`, or `auto_sync.background` in the project `config.json` to disable or tune it. Diagnostic state remains inside the ignored `.prompt-harness/state/` directory.
