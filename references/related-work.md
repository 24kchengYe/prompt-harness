# Related session-history tools

Prompt Harness deliberately focuses on a narrower layer than a general chat-history viewer: project-local, human-prompt-only facts that can later become stable badcase inputs.

## Useful precedents

- [Claude Code History Viewer](https://github.com/jhlee0409/claude-code-history-viewer) is a local-first multi-provider viewer for Claude Code, Codex, Gemini CLI, and other agents. Its project/session browser, provider filters, search, session board, and activity timeline validate the usefulness of a provider-aware session hierarchy.
- [claude-code-viewer](https://github.com/d-kimuson/claude-code-viewer) demonstrates a lightweight local viewer over Claude Code history.
- Skills discovery surfaced conversation summarization, chat-history, handoff-prompt, and graph skills, but none matched the exact combination of per-project hooks, Claude/Codex deduplication, prompt-only privacy boundaries, and a future badcase harness.

## Design choice here

The generated view therefore uses the smallest useful hierarchy:

```text
project → session → timestamped human-prompt nodes
```

Provider, model provenance, time, session, and event identity remain visible. Assistant messages and tools stay outside the view. The timeline is a standalone HTML artifact so every project can retain an inspectable snapshot without running a dashboard or exposing prompts to a web service.
