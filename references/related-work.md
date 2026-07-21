# Related session-history tools

Prompt Harness deliberately focuses on a narrower layer than a general chat-history viewer: project-local prompt/trace facts that can become stable, review-gated badcase evidence.

## Useful precedents

- [Claude Code History Viewer](https://github.com/jhlee0409/claude-code-history-viewer) is a local-first multi-provider viewer for Claude Code, Codex, Gemini CLI, and other agents. Its project/session browser, provider filters, search, session board, and activity timeline validate the usefulness of a provider-aware session hierarchy.
- [claude-code-viewer](https://github.com/d-kimuson/claude-code-viewer) demonstrates a lightweight local viewer over Claude Code history.
- Skills discovery surfaced conversation summarization, chat-history, handoff-prompt, and graph skills, but none matched the exact combination of per-project hooks, Claude/Codex deduplication, private full-trace evidence, and review-gated badcase intake.
- [Context Guard Skill](https://github.com/Michel-Johnson/Context-Guard-Skill) established strong patterns for human-owned badcase design, red-capable guards, workflow Feature Chains, ordered Task Cases, Test Hub completion gates, checkpoint evidence, subagent root binding, and retirement-aware context management. Prompt Harness adopts those lifecycle and governance patterns while keeping its own prompt/trace ledger, immutable evidence IDs, safe snapshots, cross-model replay, judges, attribution, and compensation policies as the canonical evaluation layer.

## Design choice here

The generated view therefore uses the smallest useful hierarchy:

```text
project → session → native turn → prompt and typed agent-trace nodes
```

Provider, model provenance, time, session, turn, prompt identity, reasoning, tool traffic, system injection, subagent facts, and final answers remain visible in private per-session projections. Project-wide views stay compact; complete intermediate bodies remain partitioned by session. The timeline and Test Hub are standalone read-only HTML artifacts so every project can remain inspectable without exposing prompts to a web service.
