# Badcase harness roadmap

Prompt Harness keeps badcase intake separate from source prompt and trace facts. Candidate detection, human decisions, and case lifecycle updates are append-only; Markdown is always rebuildable.

## Implemented lifecycle

1. Automatic reconciliation examines canonical human prompts after trace ingestion.
2. The deterministic `explicit-user-correction-v1` detector creates a review-only candidate when a prompt contains a high-confidence correction signal.
3. The candidate references the current correction prompt, the preceding prompt, and the intervening/linked trace IDs. It does not copy trace bodies and explicitly records `asserts_failure: false`.
4. A reviewer confirms, dismisses, or merges the candidate. Decisions are immutable events.
5. Confirmation creates a case only when Red, Green, and expected-failure conditions are supplied.
6. Case changes append lifecycle events. Issue status is separate from Harness lifecycle:
   - issue: `open`, `resolved`, `recurred`, `deferred`, `superseded-by-route-change`
   - Harness: `active`, `stable`, `probation`, `retired`
7. `index/BADCASES.md` and `index/badcase/<case-id>.md` are derived from canonical events. Compact evidence contains complete referenced prompts and final answers; full reasoning/tool/injection/subagent traffic remains in the linked session trajectory.
8. Feature Chains and ordered Task Cases remain proposals until Red fails for the expected reason and Green passes required checkpoints.
9. Test Hub runs approved completion checks; safe snapshots pin replay input; approved adapters and judges compare models without leaking historical solutions.
10. Judged failures may propose minimal compensation. Baseline-fail/compensated-pass approval, activation, probation, recurrence, and retirement are explicit append-only transitions.

## Canonical contract

```text
badcases/
├── candidates.jsonl   # badcase_candidate
├── decisions.jsonl    # badcase_decision: confirmed/dismissed/merged
├── case-events.jsonl
├── feature-chain-events.jsonl
├── task-case-events.jsonl
├── snapshot-events.jsonl
├── adapter-events.jsonl
├── judge-events.jsonl
├── run-events.jsonl
├── attribution-events.jsonl
├── compensation-events.jsonl
├── policy-events.jsonl
├── subagent-events.jsonl
└── runs/              # bounded failed/blocked evidence only
```

Stable IDs:

- `bcc_...`: candidate identity, deterministic from detector version plus source prompt event ID.
- `bcd_...`: review decision.
- `BC-YYYYMMDD-XXXXXXXX`: confirmed case.
- `bce_...`: append-only case lifecycle event.

## Safety boundaries

- Detection never calls a model and never claims that a candidate is a real failure.
- Automatic sync never changes the user's prompt, system instructions, skills, tests, or project files because of a candidate.
- A confirmed case requires a red-capable contract: the Red condition must describe recurrence, the Green condition must describe absence, and expected-failure must distinguish the old symptom from a broken test/environment.
- Candidate evidence uses stable prompt/trace IDs. Source ledgers remain authoritative and are never rewritten.
- Similar candidates should merge into one case rather than create one permanent rule per user correction.
- Harness compensation is separately proposed and approved, and is removed from model context during `probation` before retirement.

## Complete-version contract

The implemented completion contract is governed by
[complete-harness-spec.md](complete-harness-spec.md). That specification turns
Context Guard's strongest lifecycle, feature-chain, Test Hub, approval, task
case, subagent, and completion-gate ideas into evidence-native Prompt Harness
requirements, then adds snapshot-pinned cross-model replay, judges, attribution,
adaptive compensation, budgets, probation, and retirement.

## Replay and adaptive compensation

1. Pin a safe project snapshot, agent configuration, tools, skills, model, and budgets.
2. Convert the confirmed failure contract into deterministic checks where possible and a narrow judge rubric where not.
3. Run the original task without leaking the historical answer, root cause, or fix to the tested model.
4. Attribute failure among model behavior, Prompt/Skill, tool/runtime, environment, judge, and changed user intent.
5. Propose the smallest compensation: instruction, skill, tool guard, workflow checkpoint, retry policy, or human approval boundary.
6. Store run and judge facts under `badcases/runs/` and compare model/Harness versions.
7. Move stable cases to `probation` by removing compensation while retaining the test. Retire only after an explicit policy is satisfied; recurrence restores `active`.

The executable protocol, approval boundaries, adapter result schema, privacy exclusions, policy limits, and retirement rules are documented in [adaptive-harness.md](adaptive-harness.md).
