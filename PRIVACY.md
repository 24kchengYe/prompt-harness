# Privacy model

Prompt histories can contain unpublished work, personal context, file paths, and credentials. Prompt Harness therefore treats every project ledger as private local data.

## Defaults

- Prompt bodies live under `<project>/.prompt-harness/`.
- A nested `.gitignore` excludes canonical events, generated indexes, session summaries, state, and badcase data.
- The global project registry stores locations and timestamps only, never prompt bodies.
- Runtime capture redacts common API-key, access-token, password, and bearer-token patterns.
- Embedded image/document/base64 payloads are replaced with an omission marker.
- Assistant output, tools, subagents, and machine-injected instructions are excluded.

Redaction is a safety net, not a proof that arbitrary secrets are impossible. Run `doctor`, inspect any export, and use a secret scanner before publishing data. The source code may be public; prompt ledgers should remain private unless each record has been reviewed and deliberately released.

## File references

If a human writes a path in a prompt, that path stays in the prompt text. The capture process does not open the file or store its body. Native hook attachment blocks are represented by a short omission marker and an optional source path.

## Configuration safety

The standalone installer creates a timestamped backup before changing Claude Code or Codex hook configuration and preserves unrelated hook entries. It does not inspect or upload authentication values.
