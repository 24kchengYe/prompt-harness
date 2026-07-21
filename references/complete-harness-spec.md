# Complete Harness specification

This file is the completion contract for the full Prompt Harness badcase and
adaptive-regression layer. A feature is not complete because a command or a
Markdown field exists. Each requirement needs canonical state, a rebuildable
view, validation, and an end-to-end test that proves the stated behavior.

The design absorbs the strongest Context Guard ideas while keeping Prompt
Harness's richer append-only prompt and agent-trace ledger as the evidence
source. It does not duplicate `.codex/context/` or silently turn every user
correction into a permanent test.

## Invariants

1. Prompt, image, and agent-trace facts remain immutable and authoritative.
2. Automatic detection can create review candidates only. It cannot confirm a
   failure, approve a test, run a model, or inject compensation.
3. Durable tests, replay adapters, judges, and compensations require explicit
   approval events. Approval cannot be inferred from an agent-written file.
4. An approved guard must be red-capable: the old symptom must make it fail,
   and a known-good fixture must make it pass.
5. Commands are stored as argument arrays and run without a shell. Environment
   variables are allowlisted. Raw secrets are never copied into the harness.
6. Successful runs keep compact canonical facts and clean disposable
   artifacts. Failed or blocked runs preserve bounded diagnostic evidence.
7. Feature chains are the default durable regression unit. One workflow may
   cover several cases at named checkpoints; one script per case is a fallback.
8. Issue state, test state, compensation state, and Harness lifecycle are
   independent. A resolved issue can still have an active regression guard.
9. Probation runs the guard without compensation. Retirement never deletes
   the case, decision, test, run, or evidence history.
10. Exact project-root isolation, remote/local boundaries, Windows-safe paths,
    UTF-8 output, locking, secret sanitation, and append-only recovery apply to
    every new ledger.

## Requirement matrix

### CG-01 — Complete badcase contract

Confirmed cases support status, scope, tags, frequency, trigger/reproduction,
root cause, fix, guard type, verification, run policy, artifact policy, blocker
handling, Red, Green, expected-failure reason, reusable guard, feature/task-case
coverage, test-chain issue, recurrence analysis, route-change note, and stable
prompt/trace evidence.

Evidence required: schema validation, append-only update tests, compact case
view, and doctor rejection of an invalid resolved/recurred case.

### CG-02 — Human-owned proposal and approval

Guards, feature chains, task cases, model adapters, judges, and compensations
have separate `proposed`, `approved`, `disabled`, and `retired` transitions.
Only dedicated approval commands may append approval events. Direct JSON edits
or creation commands cannot bypass preflight.

Evidence required: bypass tests, idempotent approval tests, and audit history.

### CG-03 — Feature-chain coverage

The harness can propose a chain, attach cases to named checkpoints, mark a
checkpoint required or optional with a reason, audit overlap, summarize
coverage density, and identify unassigned case groups without mutating state.

Evidence required: one chain covering at least three cases and duplicate-chain
suppression in a fresh project.

### CG-04 — Red/Green dry-run gate

Approval executes a safe preflight. Feature-chain commands emit
`PH_CHECKPOINT:<name>:PASS` or `PH_CHECKPOINT:<name>:FAIL:<reason>`. Unknown,
failed, or missing required checkpoints fail approval. Optional checkpoints may
be absent only when the optional policy is explicit.

Evidence required: failing Red fixture, passing Green fixture, missing/unknown
marker tests, timeout/blocker tests, and unchanged proposal state after failure.

### CG-05 — Test Hub completion gate

One Test Hub runs all approved `every-dev-completion` tests, plus explicitly
selected relevant/manual tests. It reports every test independently, supports a
bounded worker count, preserves failure evidence, writes `last-run.json`, and
generates a read-only HTML status page. One failure cannot hide another pass.

Evidence required: two independent approved chains, mixed pass/fail run,
preserved evidence, fixed rerun, all-pass cleanup, and deterministic exit codes.

### CG-06 — Task-case workflows

Complex queues, retries, workers, recovery, browser flows, and cleanup use a
task case with ordered phases, checkpoints, exclusions, stop condition,
cleanup, blocker policy, and linked cases. Task cases stay proposed until an
approval dry-run succeeds.

Evidence required: multi-phase failure localization and approved Test Hub run.

### CG-07 — Safe project snapshots

Each replay pins project root, VCS HEAD, dirty-diff hash, relevant file manifest,
prompt/trace IDs, platform, model, tools, skills, configuration, and budgets.
Snapshot creation does not copy ignored/private files by default. Materialized
workspaces require an explicit approved policy and remain outside the source
project.

Evidence required: stable manifest hashing, dirty-state distinction, path
escape rejection, and no source mutation.

### CG-08 — Cross-model replay protocol

Approved model adapters receive a sanitized run-input manifest and write a
structured result. A replay matrix can compare model and Harness variants while
pinning the same case/snapshot/oracle. Historical answers, root causes, fixes,
and hidden judge fields are excluded from model input.

Evidence required: two deterministic fake adapters, matrix comparison, timeout,
non-zero exit, malformed result, and leakage tests.

### CG-09 — Outcome judge and attribution

Prefer deterministic assertions. A narrow approved judge adapter is allowed
only when assertions cannot decide the outcome. Run facts distinguish task
failure from environment, tool/runtime, judge, changed intent, or policy
blockers. Manual attribution is an append-only override, never a rewrite.

Evidence required: deterministic and judge paths plus each blocker class.

### CG-10 — Adaptive compensation lifecycle

Failed confirmed runs may generate proposal-only compensation candidates of
type instruction, skill, tool guard, workflow checkpoint, retry policy, or
human approval boundary. Approval, activation, supersession, probation, and
retirement are explicit events. The smallest approved compensation is rendered
only into the selected replay/runtime context.

Evidence required: baseline fail, compensated pass, uncompensated probation
passes, retirement recommendation, recurrence reactivation, and no automatic
project/source modification.

### CG-11 — Policy, cost, and retirement gates

Per-test and per-case policies define timeout, attempts, parallelism, token/cost
budgets, required consecutive passes, distinct model minimum, probation window,
and recurrence behavior. Lifecycle evaluation is read-only; applying a
transition requires an explicit command and reason.

Evidence required: budget stop, policy recommendation, approved transition,
and history preservation.

### CG-12 — Context, task, and subagent continuity

The derived context view shows the active task, parked/resume candidates,
durable user constraints, route checkpoints, open/recurred cases, approved test
status, and next step without duplicating the transcript. Explicit subagent
bindings map an agent/session to its real local project; completion evidence is
idempotent and cannot leak into a parent or remote workspace.

Evidence required: task switch/resume, child-root binding, repeated completion,
remote-path rejection, and concise regenerated context.

### CG-13 — Privacy and portability

Record language is project-scoped. Exact errors, commands, identifiers, and
paths keep their original language. Public views contain redacted pointers
only; Prompt Harness never persists raw durable credentials. All files and CLI
output work on macOS, Linux, and Windows path/encoding rules.

Evidence required: Chinese/English records, secret fixtures, GBK console test,
drive/UNC normalization tests, and nested `.gitignore` migration.

### CG-14 — Automatic but non-blocking operation

Turn hooks keep prompt capture bounded. Trace reconciliation, candidate
detection, approved completion tests, and view rebuilds run in ordered detached
workers with project/global locks and coalesced requests. A failed test never
breaks prompt capture; its status appears on the next readable view.

Evidence required: overlapping hook requests, crash recovery, stale lock,
long-lived plugin upgrade, and multiple-project load tests.

### CG-15 — Validation and human views

`doctor` validates every canonical ID, reference, state transition, policy,
snapshot, run, evidence path, approval, checkpoint, lifecycle, secret, and
derived view. Markdown remains the agent-readable source; HTML is read-only and
never becomes canonical.

Evidence required: corruption fixtures for every ledger and byte-for-byte
rebuild tests for Markdown/HTML projections.

### PH-01 — Evidence-native advantages

Every case, chain, run, judge, and compensation links to immutable Prompt
Harness prompt/trace IDs, session, turn, platform, and model. Compact views may
copy complete relevant prompts/final answers, while full reasoning, tools,
injections, and subagent bodies remain in the private trajectory ledger.

Evidence required: end-to-end navigation from a run to case, checkpoint,
prompt, final answer, and full trajectory.

## Canonical ledgers

All ledgers are append-only JSONL under `.prompt-harness/badcases/`:

```text
candidates.jsonl
decisions.jsonl
case-events.jsonl
guard-events.jsonl
feature-chain-events.jsonl
task-case-events.jsonl
adapter-events.jsonl
judge-events.jsonl
snapshot-events.jsonl
run-events.jsonl
attribution-events.jsonl
compensation-events.jsonl
policy-events.jsonl
subagent-events.jsonl
```

Bounded run artifacts live under `badcases/runs/<run-id>/`. Rebuildable views
live under `index/badcase/`, `index/test-hub/`, and `visualizations/`.

## Complete-version audit rule

Before declaring the full version complete, map every requirement above to:

- implementation entry point;
- canonical schema;
- doctor validation;
- unit/regression test;
- at least one fresh-project end-to-end artifact;
- documentation and skill routing instruction.

Missing or indirect evidence means the requirement is still incomplete.

The implementation-to-test evidence map is maintained in
[completion-audit.md](completion-audit.md).
