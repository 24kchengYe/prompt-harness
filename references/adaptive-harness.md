# Adaptive Harness protocol

Prompt Harness uses the private prompt/trace ledger as evidence and keeps every
test, replay, decision, and compensation in append-only side ledgers. Automatic
detection stops at a review candidate. Executable behavior begins only after a
dedicated approval command completes its preflight.

## State and approval boundaries

Feature chains, task cases, model adapters, judges, and compensations are
created as `proposed`. An agent-written JSON file is never approval. Dedicated
approval commands append an `approved` event only after:

- feature/task guard: old fixture fails for the expected reason and known-good
  fixture passes every required checkpoint;
- model adapter: protocol self-test writes one valid structured result;
- judge: protocol self-test writes a boolean `metrics.passed` decision;
- compensation: the pinned uncompensated replay fails and the same adapter,
  case, snapshot, and oracle pass with the proposed compensation.

Disable, activate, supersede, probation, reactivate, and retire transitions are
explicit events with reasons. They never delete proposals, approvals, runs, or
failure evidence.

## Feature chains and task cases

Feature chains cover several badcases at named checkpoints. Task cases add
ordered phases, exclusions, stop condition, cleanup, and blocker policy for
queues, retries, browser flows, workers, and recovery.

Commands are JSON argv arrays or objects and run with `shell=False`. Their
working directory must remain in the project root, timeouts are bounded, and
only named environment variables pass through. A command reports:

```text
PH_CHECKPOINT:<exact checkpoint name>:PASS
PH_CHECKPOINT:<exact checkpoint name>:FAIL:<reason>
```

Missing required, duplicate, unknown, or failed markers fail the run. Optional
markers may be absent only after an audited optional-policy event.

`dev-complete` runs every approved `every-dev-completion` feature/task guard.
Workers are bounded to eight. Every result is reported independently. Passing
runs keep compact facts and delete disposable run directories; failed or
blocked runs retain sanitized output and bounded artifacts.

## Safe snapshots

`snapshot-create` writes a deterministic manifest, not a source copy. It pins:

- project ID/root, Git HEAD, dirty status and staged/worktree/status hashes;
- tracked and nonignored file path, size, mode, and content hash;
- task/correction prompt IDs and trace IDs;
- platform, Python, selected tools, skills, configuration, and budgets.

The manifest excludes `.prompt-harness`, `.git`, dependency caches, `.env`
variants, credential files, and private-key formats. It rejects path escapes,
limits the file count, and omits content hashes for files above 50 MiB. A
materialized workspace requires a separate explicit policy that pins an
already-existing destination parent outside the source project. Materializing
verifies current source hashes, refuses overwrite, and rolls back partial
copies; source projects are never mutated by snapshotting.

## Model adapter protocol

An approved adapter receives these environment variables:

```text
PROMPT_HARNESS_RUN_ID
PROMPT_HARNESS_RUN_DIR
PROMPT_HARNESS_INPUT_PATH
PROMPT_HARNESS_RESULT_PATH
```

It reads UTF-8 JSON input and writes UTF-8 JSON:

```json
{
  "schema_version": "1.0.0",
  "status": "completed",
  "answer": "model result",
  "reason": "optional",
  "metrics": {
    "passed": true,
    "tokens": 123,
    "cost_usd": 0.01
  }
}
```

`status` is `completed`, `failed`, or `blocked`; `answer` is required and
bounded. Replay input includes only task prompts, sanitized snapshot facts,
target model identity, and the selected approved compensation. It excludes the
correction prompt, historical output, root cause, fix, expected-failure reason,
and judge oracle. A replay matrix pins one case and snapshot across every
adapter/Harness variant.

Timeout, nonzero exit, malformed result, adapter-reported failure, and blocked
policy are distinct facts. Failed/blocked artifacts remain under
`badcases/runs/<run-id>/`.

Adapter and judge processes do not use the source project as their working
directory. They run in the private run directory by default, or in the
explicitly approved external materialized snapshot when one exists. Commands
are still trusted executable adapters rather than an OS security sandbox, so
approval must review their argv and implementation.

## Outcome judge and attribution

Use `metrics.passed` when a deterministic assertion can decide the result. If
it is absent, `judge-evaluate` may pass only the candidate answer/metrics and
the case oracle to a separately approved narrow judge. The model adapter never
sees judge-only fields.

Initial attribution is one of `task`, `environment`, `tool-runtime`,
`adapter-protocol`, `policy-blocker`, or `judge`. A human may append a
`changed-intent` or other attribution override with a reason. The original run
fact remains unchanged.

## Compensation, policy, and retirement

Only a judged failed replay can seed a compensation proposal. Types are:

- instruction;
- skill;
- tool guard;
- workflow checkpoint;
- retry policy;
- human approval boundary.

Replay renders only the selected approved compensation into the adapter input.
Prompt Harness never edits project source, global prompts, skills, or tool
configuration automatically.

Policies bound timeout, attempts, parallelism, tokens, cost, required
consecutive passes, distinct model minimum, probation window, and recurrence
behavior. `compensation-recommend` is read-only. Entering probation removes the
compensation from normal replay while retaining its guard. Retirement requires
an explicit transition; a post-probation recurrence can recommend reactivation.

## Automatic completion and views

Prompt submission remains a bounded append. Trace reconciliation, candidate
detection, approved Stop/Goal completion tests, and view rebuilding use the
existing detached coalesced worker under project/global locks. Candidate
detection and completion tests run only when `badcases.automation_enabled` is
explicitly set to `true`; the default is `false`, and manual commands remain
available. Test failure does not change hook exit status.

Canonical JSONL lives under `badcases/`. `index/BADCASES.md`,
`index/TEST_HUB.md`, `index/CONTEXT.md`, `index/test-hub/index.html`, and per-case
views are disposable projections. `doctor` validates IDs, transitions,
references, approvals, commands, snapshots, evidence paths, secrets, and clean
view presence.
