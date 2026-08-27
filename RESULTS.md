# Protocol v8 development diagnostics

The run used Claude Sonnet 5, one repetition, and an 18-tool-call cap for
one case from each accepted corpus. Every case ran against an exclusive
GreptimeDB instance at commit `75cd53243e2c645621ae62202ec9413797bc98aa`.
The adapters did not repair source timestamps, labels, topology, or ground
truth.

These cases are a development set. Protocol changes in versions v6 through v10
used their failures, so the results cannot measure generalization.

## Per-case outcome

| Dataset | Graph coverage | Raw | Table Semantics | Table Semantics + Graph |
| --- | --- | --- | --- | --- |
| RCA100 `t001` | Relational | Component only | Component only | Component only |
| RCAEval `re2ob_checkoutservice_cpu_1` | Entity-only | Joint correct | Joint correct | Joint correct |
| OpenRCA Bank `task_6@2021-03-04T18:00` | None | Incorrect | Incorrect | Incorrect |

Do not aggregate these rows into one accuracy score. RCAEval uses a small
resource-fault taxonomy, RCA100 uses fine-grained mechanisms, and OpenRCA uses
natural-language labels. A sum would assign equal weight to different answer
spaces and difficulty levels.

RCA100 is still a positive usability result for the graph. The Graph agent's
first graph query returned all 13 witnessed `calls` edges. It cited the
checkout-to-payment edge, including 752 errors in 1,541 requests, to separate
the payment failure from its upstream symptoms. Raw and Table Semantics reached
the same component from traces. All three selected the plausible but
non-canonical `codeDefect` label instead of the dataset's exact
`httpError5xx` label, so the official joint score remains false.

RCAEval exposes only seven service identities and no relationships. All three
treatments found the checkout-service CPU fault. The Graph agent used one
unfiltered entity query, but identity did not change the conclusion.

OpenRCA exposes 345 metric tables, one log table, and one trace table, with an
graph identity or relationship semantics. All three agents exhausted their
investigation on transaction and trace latency and missed the direct Redis02
memory signal. The current Table Semantics interface enriches a table only
after its name is known; it does not provide semantic catalog search across a
wide schema. That discovery limitation dominates this case.

## Resource use

| Treatment | Cases | Tools executed / requested | Cap-hit cases | Estimated model cost | Agent time | Queries | Rows | Failed queries |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Raw | 3 | 49 / 53 | 2 / 3 | $1.324 | 254.55 s | 58 | 1,643 | 1 |
| Table Semantics | 3 | 49 / 53 | 2 / 3 | $1.641 | 233.84 s | 75 | 2,292 | 1 |
| Table Semantics + Graph | 3 | 49 / 53 | 2 / 3 | $2.093 | 331.38 s | 63 | 2,267 | 3 |
| Total | 9 | 147 / 159 | 6 / 9 | $5.057 | 819.77 s | 196 | 6,202 | 5 |

Cost uses the report's 2026-08-10 standard API pricing snapshot: $2 per million
input tokens and $10 per million output tokens. Maximum observed database query
concurrency was one in every run. Every case ran in the order Graph, Raw, then
Table Semantics. The elapsed-time values are descriptive; cache and execution
position are fully confounded with treatment.

The call cap was binding on RCA100 and OpenRCA in every treatment. Semantic
metadata and graph calls consumed the same cap as SQL. These runs measure how
far each interface progressed under 18 calls, not whether an unconstrained
agent had enough evidence to stop.

## Decision

Keep these three cases as development and regression cases. Protocol v11 fixes
the confirmed implementation defects and records the design limitations in the
report. Add and freeze representative measurement cases before another paid
batch. Use one model for all treatments, balance execution position, and choose
a cap high enough to separate voluntary completion from forced submission.
