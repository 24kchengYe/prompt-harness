# Complete Harness implementation audit

This matrix maps the completion contract to executable implementation and
regression evidence. `doctor_store` validates every canonical ledger and clean
derived-view presence; `test_doctor_rejects_corruption_in_every_adaptive_ledger`
injects corruption into each new ledger independently.

| Requirement | Implementation entry points | Canonical schema / view | Regression evidence |
|---|---|---|---|
| CG-01 complete case | `confirm_badcase_candidate`, `update_badcase_case` | `badcase-case*.schema.json`, `BADCASES.md`, per-case view | `test_badcase_confirmation_merge_dismiss_and_lifecycle_are_append_only` validates append-only state and rejects an invalid recurrence |
| CG-02 human approval | `approve_feature_chain`, `approve_task_case`, `approve_model_adapter`, `approve_judge_adapter`, `approve_compensation`, transition commands | feature/task/adapter/judge/compensation event schemas | Red/Green, adapter-failure, adaptive-lifecycle tests prove failed preflight remains proposed and repeated approval is idempotent |
| CG-03 feature coverage | proposal, attach, checkpoint policy, coverage/overlap/candidate/plan reports, duplicate approval suppression | `feature-chain-event.schema.json`, `TEST_HUB.md` | planning test covers three cases in one chain, unassigned grouping, read-only planning, overlap, and duplicate suppression |
| CG-04 Red/Green gate | `evaluate_feature_chain_output`, `run_feature_chain_command` | run and feature-chain schemas | feature-chain test covers Red, Green, optional, missing, unknown, duplicate policy, timeout, unchanged proposal, and preserved evidence |
| CG-05 Test Hub | `test_hub_dev_complete`, Stop/Goal invocation, HTML renderer | `harness-run-event.schema.json`, `TEST_HUB.md`, `test-hub/index.html`, `last-run.json` | two-chain mixed pass/fail and repaired rerun; Stop-triggered detached completion test |
| CG-06 task cases | proposal, Red/Green approval, phase runner, Test Hub integration | `task-case-event.schema.json`, Test Hub task table | adaptive lifecycle test localizes two phases, cleanup, exclusion, blockers, approval, and completion run |
| CG-07 snapshots | manifest creation, materialization approval, external materialization | `harness-snapshot.schema.json`, snapshot policy event | snapshot test proves stable hash, dirty distinction, secret exclusion, escape rejection, external copy, rollback boundary, and no source mutation |
| CG-08 replay | adapter proposal/approval, protocol runner, replay input and matrix | `model-adapter-event.schema.json`, run schema | two-adapter fixed-snapshot matrix plus timeout, nonzero, malformed, blocked, task-failure, and hidden-field tests |
| CG-09 judge/attribution | deterministic evaluator, judge proposal/approval, manual override | `judge-event.schema.json`, `attribution-event.schema.json` | adaptive test exercises deterministic and approved-judge paths; adapter failure test covers environment/runtime/protocol/task/policy classes; changed intent is append-only |
| CG-10 compensation | proposal, baseline/compensated approval, transition, recommendation | `compensation-event.schema.json`, Test Hub summary | adaptive test proves baseline fail, compensated pass, activation, compensation-free probation, cross-model retirement recommendation, recurrence, and reactivation |
| CG-11 policies | policy validation/set/effective merge, timeout and replay-budget enforcement | `harness-policy-event.schema.json` | adaptive test stops at attempt budget and restores by a later append-only policy; feature/task/replay runners apply bounded timeout |
| CG-12 continuity | `render_context_view`, subagent bind/completion | `subagent-event.schema.json`, `CONTEXT.md` | task switch/resume test plus exact-root binding, remote rejection, and idempotent child completion |
| CG-13 privacy/portability | sanitation, exact-root routing, Windows path normalizer, UTF-8 hook/CLI, nested ignore migration | all schemas and private nested `.gitignore` | secret/image/GBK/Windows detached/drive/UNC/exact-root/home-root tests |
| CG-14 non-blocking automatic | bounded hook append, detached coalesced worker, candidate then approved completion then one rebuild | `auto-sync.json`, `last-run.json` | full/incremental, coalescing, Windows detached, long-lived launcher, new sibling, and Stop completion tests |
| CG-15 validation/views | `doctor_store`, all renderers | every schema, Markdown and HTML views | per-ledger corruption test and byte-for-byte adaptive view rebuild test |
| PH-01 evidence-native | case evidence map, snapshot prompt/trace partition, run context, trajectory links | prompt/trace ledgers, per-case and per-session trajectory views | badcase and replay tests navigate immutable prompt IDs, trace IDs, case, snapshot, adapter, run, and complete trajectory |

## Fresh-project proof

All adaptive tests use new retained projects under `tests/_artifacts/`. They
initialize a real `.prompt-harness`, append source facts, execute safe fake
commands as subprocesses, rebuild private Markdown/HTML, and finish with
`doctor_store`. No test relies on the developer's own project ledger.
