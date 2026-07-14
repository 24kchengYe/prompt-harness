# Badcase harness roadmap

Phase 1 deliberately captures only durable human-input facts. Phase 2 can be built without changing those facts.

## Proposed lifecycle

1. Select one or more prompt `event_id` values whose downstream task remained unresolved.
2. Attach a failure analysis: observable symptom, root-cause hypothesis, failure taxonomy, environment, required fixtures, and risk.
3. Convert the desired outcome into executable acceptance checks where possible and a narrow rubric where not.
4. Replay against a pinned model, agent configuration, tools, skills, and project snapshot.
5. Store every run, judge result, latency/cost metadata, and trace reference.
6. Improve the harness, skill, context routing, or tool—not the historical prompt—and rerun until the acceptance gate passes.
7. Keep the case as a regression test across future model and harness versions.

## Reserved case contract

```text
badcases/cases/<case-id>/
├── case.json             # IDs, source event links, state, taxonomy
├── analysis.md           # human-readable evidence and hypotheses
├── fixtures/paths.json   # references to retained fixtures, not copied secrets
├── acceptance.json       # deterministic checks and/or judge rubric
└── runs/<model>/<run-id>.jsonl
```

Important open decisions for phase 2 include project snapshotting, safe tool replay, outcome judges, model-specific versus model-agnostic acceptance gates, token/cost budgets, and how a human confirms that an apparently solved case is genuinely complete.
