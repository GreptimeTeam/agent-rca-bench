# Semantic RCA Benchmark Plan

This document is the canonical project plan. Local implementation details and
individual pilot findings must not replace its objective or experiment design.

## Objective

Measure whether GreptimeDB's semantic surfaces improve the investigation
efficiency of an LLM agent performing root-cause analysis over real, labeled
failure telemetry without reducing diagnosis validity.

For every case, compare three nested treatments over identical telemetry:

1. `raw`: telemetry tables and ordinary schema metadata.
2. `table_semantics`: raw access plus Table Semantics.
3. `semantic_graph`: Table Semantics plus computed entities and relationships.

The benchmark measures `table_semantics - raw` and
`semantic_graph - table_semantics`. It does not assume either delta is positive.
Returned rows, tool calls, model tokens, and elapsed time are efficiency
outcomes. Correct task output is an eligibility guardrail for comparing
completion efficiency, not the primary treatment effect. Accuracy changes may
be reported as secondary observations but are not required for an efficiency
benefit.

## Dataset strategy

- Use authoritative, public failure datasets with telemetry before and after a
  fault and explicit root-cause labels.
- Prefer metrics, logs, and traces, but do not require every dataset to exercise
  Semantic Graph. A graph-negative corpus is useful for measuring Table
  Semantics and defining Graph's applicability boundary.
- Select datasets for complementary evidence, not by accumulating similar cases
  from one system.
- Audit the real schema, timestamps, identifiers, labels, and license before
  implementing an adapter.
- Never synthesize missing span kinds, topology, identities, relationships, or
  other telemetry facts to make Semantic Graph appear useful.
- Do not repair source timestamps, labels, topology, or ground truth. Record
  defects and reject a case when its original telemetry cannot support one
  evidence window. Protocol-native behavior, such as Prometheus sample
  deduplication, remains part of the experiment and must be measured.

Use this development corpus portfolio:

1. RCAEval as the established graph-negative control.
2. RCA100 v1.1 as the competition-validated, graph-positive OpenTelemetry corpus.
3. OpenRCA 1.0 Bank as an established enterprise-telemetry corpus for measuring
   Table Semantics over a wide legacy metric schema. Its traces do not contain
   enough standard identity or span-role information to evaluate relationships.
4. OpenRCA 2.0 ops-lite as a provisional relational corpus. It has paired
   normal/abnormal windows and native relational trace evidence, but its
   anonymized submission artifact is not yet an archival release and its paper
   and dataset card declare different licenses. Results from it remain
   exploratory until provenance and licensing are resolved.
5. OpenRCA 1.0 Market as a wide legacy, multi-level node and container corpus.
   It tests temporal discovery across 587 semantic tables and preserves the
   source's incompatible node, pod, service, log, and trace identifiers.
6. OpenRCA 1.0 Telecom as an independent metrics-and-traces corpus without logs.
   It tests delayed resource evidence across 130 semantic tables.

Select additional datasets by authority, root-cause quality, telemetry fidelity,
system and failure diversity, and non-overlap with this portfolio. Freeze the
measurement cases before inspecting their telemetry or agent results. Record
candidate decisions in `DATASETS.md`.

The development cases were selected by a label-independent
hash ranking. The selection is reproducible but not blinded: public case names can
encode components or mechanisms, and the evaluator necessarily loads ground truth.
No blinded-holdout claim is made.

For each corpus, sort eligible case IDs by
`sha256((seed + "\\0" + case_id).encode()).digest()` and choose the first case
that passes its predeclared no-model gate. The implementation is
`selection.deterministic_rank`.

1. RCAEval seed `semantic-rca-v12-rcaeval-measurement` initially ranked
   `re2ob_emailservice_socket_3`, but its CPU signal dominated the labeled socket
   mechanism. The next loss case had no direct loss signal and several competing
   resource anomalies. Both failed the telemetry-label fidelity gate. The next
   candidate, `re2ob_currencyservice_disk_1`, has disk I/O absent in baseline and
   about 4.5 GB/s after injection and is retained.
2. RCA100 seed `semantic-rca-v12-rca100-measurement` selects `t002` from
   `t002` through `t100`.
3. OpenRCA Bank seed `semantic-rca-v12-openrca-bank-measurement` selects
   `task_5@2021-03-09T09:30` from task windows containing exactly one official
   root-cause record after excluding the development case.
4. OpenRCA 2.0 seed `semantic-rca-v12-openrca2-hotel-measurement` ranks new-source,
   non-hybrid Hotel Reservation cases with one manifest root, propagation depth
   at least three, and a non-network fault. The first candidate had no observable
   alert and was rejected by the predeclared gate; the next candidate,
   `hs1-geo-pod-failure-drdmjj`, passed. The gate also requires the manifest root
   to equal the injection ground-truth service set.
5. OpenRCA Market seed `semantic-rca-v16-openrca-market` ranks distinct half-hour
   windows from both cloudbeds that contain exactly one official root-cause record.
   Before any Market telemetry was downloaded, the rule selected
   `Market/cloudbed-1@2022-03-21T03:30` from 89 eligible windows.
6. OpenRCA Telecom seed `semantic-rca-v16-openrca-telecom` ranks distinct half-hour
   windows that contain exactly one official root-cause record. Before any Telecom
   telemetry was downloaded, the rule selected `Telecom@2020-05-27T05:00` from 51
   eligible windows.

These rules used only case indexes, task metadata, and ground-truth records. Do
not replace a case after telemetry ingestion or agent execution unless its
predeclared no-model fidelity gate fails; record any rejection before applying
the same deterministic rule to a replacement.

## Experiment invariants

- Ingest through production protocols when the source data can be represented
  faithfully: Prometheus remote write, Loki, and OTLP.
- All treatments for one case query the same imported rows in one exclusive
  GreptimeDB instance. Never load another case or schema into that instance:
  computed Semantic Graph tables currently enumerate every user schema in the
  catalog, so database-level isolation is insufficient.
- The base prompt, model, tool-call cap, case window, and execution limits are
  identical across treatments. Semantic treatments expose their documented
  metadata, tool schemas, usage guides, and coverage snapshot. The measured
  treatment is the complete agent-facing semantic interface, not stored
  metadata in isolation.
- Semantic metadata calls consume the same tool-call safety cap as SQL calls.
  Formal efficiency runs use a cap high enough that exhaustion is exceptional;
  any treatment with cap-hit runs is excluded from completion-efficiency claims.
  Record requested calls and cap exhaustion for every run.
- The initial prompt states the shared cap and every tool-result turn states the
  remainder. The runner does not let an uninformed agent discover the budget only
  after exhausting it.
- Ground truth is unavailable to the agent and only enters deterministic scoring.
- Database and report identifiers visible to the agent must not contain the
  ground-truth component or fault type. Formal runs reject such identifiers.
- Treat graph evidence coverage as an observed property of a case, not as a
  benchmark fixture.
- Record the GreptimeDB revision, dataset revision, adapter version, model,
  prompt protocol, telemetry counts, query counts, returned rows, concurrency,
  elapsed time, tokens, and cost.
- Record calls to a correct diagnosis, discovery calls before evidence queries,
  failed calls, and exact repeated calls. Cost-to-correct is defined only for
  jointly correct runs with at least one valid evidence citation.
- Do not mix results from different protocol versions.
- Label cases as `development` or `measurement`. Never use a case for formal
  measurement after its failures influenced the protocol or prompt.
- Compare treatment latency only when every treatment appears equally often in
  every execution position. With three treatments, one repetition is not
  position-balanced.
- Report correctness per dataset when answer taxonomies differ. Do not sum
  heterogeneous labels into one accuracy number.
- Freeze the dataset set, case-selection rules, and protocol before paid runs.

The pre-registered primary efficiency metrics are GreptimeDB rows returned and
tool calls through a jointly correct diagnosis with at least one valid evidence
citation. Apply the same eligibility guardrail to both metrics. For each case,
take the median eligible run-pair delta; use the case medians for the exact
two-sided sign test and adjust the four primary tests with Holm's method. Run-pair
directions are descriptive only. Discovery ordering and other trajectory metrics
are exploratory and cannot support headline claims.

The two benchmark lines use different report fields:

- End-to-end RCA uses `database_load.rows_returned`, exposed by the combined
  report as `rows_returned`, and
  `evaluation.correct_completion_tool_calls`.
- Discovery and Graph micro-benchmarks use
  `rows_returned_through_evidence` and `tool_calls_through_evidence` from their
  task-specific evaluations. Discovery calls through evidence are secondary.

These names are not interchangeable. The RCA fields cover the complete run to a
jointly valid diagnosis; the micro-benchmark fields stop at the cited canonical
evidence result.

Protocol v22 defines an execution-valid RCA citation as one non-empty claim that
uniquely references a successful, non-truncated `execute_sql` or
`query_semantic_graph` `QueryResult` with the same output query ID. Catalog and
schema discovery do not count as incident evidence. This referential check does
not establish that the result supports the diagnosis. Before the end-to-end
transfer cohort is frozen, each case must therefore add a deterministic,
source-faithful evidence-support predicate. Do not substitute an LLM judge.

Reported model tokens remain exploratory until a runner-specific accounting
contract is frozen. API usage sums provider response usage and stores uncached
input in `run.usage`; cache creation and cache reads remain in the raw response
events. Codex uses one cumulative `turn.completed` usage event and does not
persist a cache breakdown. Claude aggregates input, cache creation, and cache
reads into `input_tokens`. System prompts, tool schemas, prior tool or MCP
results, and structured output are present in the provider context, but the
runners do not expose a comparable component-level breakdown. Token deltas may
therefore be compared only within the same model, runner, protocol, and case.

## Benchmark 1.0 release gate and research-claim gate

The current Discovery v2 and Graph v3 results are mechanism evidence, not a
powered estimate of a general semantic-layer effect and not yet a public 1.0
cohort. Benchmark product readiness and confirmatory research readiness are
separate milestones.

Benchmark 1.0 requires a frozen specification and canonical API runner, a small
legally reproducible cohort covering Table-positive, Graph-positive, and
Graph-negative roles, one fresh end-to-end transfer demonstration, and public
artifacts from which a third party can reproduce scoring and primary metrics.
It does not require multiple models, many system families, or statistical power
for a population claim. Results must be labeled as evidence over the fixed
cohort rather than a general effect.

The pinned Aegis FSE 2026 reviewer cohort supplies the Graph-positive ingestion source for
the downloader-backed 1.0 path. Its source dataset record declares CC BY 4.0,
but the reviewer artifact's Apache-2.0 grant does not explicitly cover the
reduced telemetry subset. Do not redistribute the telemetry. Source-faithful
ingestion and the raw-span versus stored Graph exact edge-set gate now pass for
the selected case. Its evaluator-side transfer fixture freezes the directed
two-service answer, deterministic method-replacement evidence predicate, and
canonical API runner contract. Its real no-model scorer audit passes. The
dedicated runner now binds the same source and scorer gates, preserves the
publisher window for Raw SQL, and applies the audited minute envelope only to
Semantic Graph queries. The protocol v24 `deepseek-v4-flash` pilot completed all nine cells
without runner or budget failure, but none passed the joint diagnosis and mechanism-evidence
predicate. The model usually stopped at the downstream NPE and did not compare the normal and
abnormal traced operation. Those trajectories informed protocol v25's generic
provisional-hypothesis triage and discriminating-query strategy. Under the experiment invariant
above, that case is therefore a development case for v25 and cannot supply the fresh 1.0
measurement demonstration. The next candidate from the original frozen unconsumed list is now
frozen separately as `aegis-transfer-002`. Its source-declared request-delay predicate, isolated
production-protocol replay, exact 41-edge raw/Graph equality, deterministic scorer, and no-model
protocol audit all pass. No trajectory from this case influenced the prompt or selection.

The optional three-model formal protocol freezes `deepseek-v4-flash`, `deepseek-v4-pro`, and
`claude-sonnet-5`, three repetitions per treatment and model, provider-specific prompt caching,
and within-model semantic-layer comparisons. Correctness is not pooled across models. This exceeds
the minimum one-model 1.0 gate but does not turn one case into a population estimate. The remaining
transfer gate is the explicitly approved 27-run execution and a sanitized public measurement
artifact.
Raw `.reports/` trajectories are not the release contract because they contain
provider payloads and local metadata.

A later confirmatory study must pre-register practical effect thresholds,
multiplicity, exclusion assumptions, power, and independent-case enrollment.
Repetitions never count toward sample size.

## Delivery stages

### M0: Environment

Build the Python project, validate the GreptimeDB revision, and isolate benchmark
databases and local artifacts.

### M1: Dataset adapters and ingestion

Download and audit a real case, replay it through production protocols, and
verify row counts, time ranges, identifiers, Table Semantics, and graph evidence.

### M2: Treatment isolation

Enforce raw, Table Semantics, and Table Semantics plus Semantic Graph visibility
without changing ordinary telemetry queries.

### M3: Agent and evaluation

Run a single agent with structured output, deterministic scoring, complete
session trajectories, and database-load and model-cost accounting.

### M4: Pilot

After the protocol and dataset gates pass, run a small balanced pilot across
complementary corpora. Repeat treatments only enough to characterize model
variance. Expand case count only after the pilot exposes no correctness issue.

The protocol v17 batch used three repetitions, so every treatment occupied every
execution position once. The same six development cases ran with Codex
`gpt-5.6-terra` and Claude Code `sonnet` through their interactive subscriptions.
Every treatment used the same 48-call safety cap. Correctness remains separate
by corpus because the taxonomies are heterogeneous.

## Current status and next step

The fresh micro-benchmark stage is complete. Discovery v2 has six formal
trajectory-blind OpenRCA cases, 24 agent cells, and 12 paired observations.
Table Semantics improved task success in one pair, regressed in none, and tied
in 11. Repeated run pairs describe model variation; cross-case inference uses
the median jointly successful delta per case. Returned rows improved in all five
eligible cases, with median case delta `-273` and exact two-sided sign-test
`p=0.0625`. Calls and reported model tokens were each better / worse / tied in
`3 / 2 / 0`, with `p=1.0`. Discovery therefore shows a consistent row-retrieval
direction, not a statistically conclusive or general token effect.

Graph v3 exhausted the frozen Hotel, OTel Demo, and Train Ticket candidate
strata and found only two eligible fresh Hotel cases. Their raw span self-joins
and Graph edge sets match exactly. Across four agent pairs Graph improved task
success once, regressed zero times, and returned fewer rows and used fewer calls
in all three jointly successful pairs. The sample is too small and
system-specific for a broad Graph effect claim. At case level, rows, calls, and
reported model tokens improved in both cases, with median deltas `-61.25`,
`-2.5`, and `-40,831`; each sign test has `p=0.5`.

`RESULTS.md` records the case-level evidence, paired statistics, exclusions, and
limitations. `fixtures/measurement/results-summary.json` records the exact raw
formal-report hashes, run-pair descriptions, case-level inference, and metric
registration status. No paid full RCA batch was run. Do not promote these
retrieval results into an RCA claim.

The next stage is release-focused, not a broad case-expansion program. The
pinned Aegis downloader, source-only selection audit, production-protocol
adapter, exact raw-span versus Graph edge-set audit, and both stored mechanism gates are complete.
The protocol v24 paid pilot is retained as a negative development result; do not rerun it as if it
were fresh measurement. The sequentially frozen `aegis-transfer-002` delay case is the fresh v25
measurement case. Its selection, scorer, raw/Graph equality, stored threshold evidence, and
three-model execution protocol pass their no-model audits. The sanitized v24 development artifact is tracked at
`artifacts/development/aegis-transfer-v24-deepseek.json`; it is a release-format regression, not the
fresh 1.0 measurement result. Private no-model reports remain local audit inputs. The next action
is an explicitly approved 27-run API execution followed by sanitized measurement export and final
release reporting. Catalog broad-recall, powered cross-system enrollment, and additional report UI
remain deferred.

## Historical development status

All six protocol v17 cases pass isolated no-model ingestion and semantic-surface
gates. They cover entity-only, relational, and empty graph surfaces. They are a
development evaluation, not a holdout. Both subscription models completed all
54 runs under one frozen protocol, for 108 runs in total.

The protocol v8 runs are diagnostics, not an effect estimate. Six of the nine
runs requested more calls than the 18-call cap. One repetition also put Graph
first, Raw second, and Table Semantics third for every case. The fixed cap and
execution order confound capacity and latency comparisons. The three datasets
also use different fault taxonomies, so their correctness counts must remain
per-dataset. `RESULTS.md` records the valid observations and limitations.

Protocol v11 fixed the confirmed implementation defects and made the
limitations auditable. It gates the Graph tool on the produced `empty` status,
scopes RCAEval taxonomies by dataset, filters discovery rows before truncation,
aligns catalog recall and ranking fields, records requested and rejected tool
calls, records execution position and case role, and reports baseline
availability.

Protocol v12 added completion-efficiency metrics and executable hash selection.
Calibration showed that a nominally larger cap still bound because the agent did
not know its remainder and eventually hit the separate turn limit. Protocol v13
makes the shared budget visible at the start and after each tool-result turn. Run
all four selected cases through no-model gates. Protocol v14 also accepts only
explicit service-role suffixes when normalizing component answers, so a response
such as `geo service pod (...)` matches `geo` without accepting dependency paths.
Protocol v17 adds controlled Codex and Claude subscription runners, pre-registers
paired returned rows and calls to a correct diagnosis, and fixes the trajectory
metrics that previously favored Graph by construction. The v17 results do not
replicate the v14 Graph row reduction and do not establish an RCA benefit for
either semantic layer. Table Semantics has a promising but non-significant
Telecom result, while Market exposes poor catalog precision for `I/O` searches.

A post-v17 audit found a runner-level protocol divergence: Codex subscription
did not receive the system prompt used by Claude subscription and API runs. The
API runner also capped interactions at 30 turns while advertising a 48-call
tool budget. Protocol v19 supplies the same contract to every runner, sets the
API turn limit to `max_tool_calls + 10`, persists runner failures, and treats
taxonomy violations as incorrect answers rather than invalid runs. The
supported subscription CLIs do not expose a turn-limit option; their broker
enforces the shared database-tool cap, and a process timeout bounds the session.
Rejected calls to unavailable tools remain audit events but do not consume the
investigation budget.

The RCA100 `t002` answer file conflicts across structured component fields and
does not publish a canonical Redis/Valkey dependency entity. Protocol v19
disables component and joint scoring for that case, retains mechanism scoring,
and records causal-dependency predictions without adding unreachable truth or
evaluation fields. Add dependency scoring only when a source provides a frozen
canonical label.

Protocol v20 corrects the benchmark Graph window to the half-open
`[start, end)` contract implemented by GreptimeDB's computed tables. The earlier
`<= end` wrapper, coverage queries, and isolation gate could include a bucket
whose start equaled the exclusive upper bound. No v20 RCA batch has been run.

Protocol v21 applies one completion-efficiency guardrail to returned rows and
tool calls: joint diagnosis correctness, at least one citation, all citation IDs
valid, no runner error, and no budget hit. It makes case medians the inferential
unit, limits primary comparisons to Table-minus-Raw and Graph-minus-Table, and
uses Holm adjustment across the four primary tests. It removes the
runner-dependent component-mention trajectory field, records the actual
runner-specific token-accounting contract, and isolates subscription child
processes from provider-prefixed environment configuration. No v21 RCA batch has
been run.

Protocol v22 closes the remaining citation-validity gap. Protocol v21 counted
any issued query ID as valid evidence, including failed, truncated,
metadata-only, duplicated, or output-mismatched calls. V22 requires a unique,
successful, non-truncated SQL or Graph result, a matching output query ID, and a
non-empty evidence claim. `correct_completion_tool_calls` now applies the same
diagnosis, evidence, runner-error, and budget guard as returned rows. This is an
execution-validity contract, not evidence entailment; the transfer cohort still
needs case-specific deterministic evidence-support predicates. No v22 RCA batch
has been run.

Protocol v23 persists the valid-completion decision once and makes both primary
metrics and the report consume that decision. It separates final-output-
superseded tool requests from failed calls, uses typed cache-inclusion metadata,
requires an explicit provider cache breakdown before estimating cache-aware API
cost, and preserves parsed Codex responses when cumulative usage validation
fails. No v23 RCA batch has been run.

The discovery v1 development protocol is now implemented separately from the
RCA protocol. It compares only `raw` and `table_semantics`, supplies the target
component, signal concept, and incident boundary, and scores whether the agent
finds the hidden table and executes a valid baseline/incident evidence query.
The catalog tokenizer now preserves `I/O`, removes low-information terms, and
maps adjacent `io_w`/`io_r` identifier tokens to their direction concepts.
Market and Telecom remain regression fixtures, not measurement cases.

Both live fixture databases pass `discovery-audit`: the canonical telemetry
predicates hold, Market ranks the target table second, and Telecom ranks it
first. The paired discovery development pilot is complete: all eight cells
succeeded after scorer v2 accepted current-database qualification and typed
timestamp literals. Table Semantics did not change success, but reduced rows
returned through evidence in all four pairs. Market used catalog search instead
of Raw's schema enumeration, then made one additional describe call; Telecom
used the same number of calls.

The separate Graph v1 development protocol is implemented for witnessed
service-call retrieval. It compares `table_semantics` with `semantic_graph` and
does not reuse RCA100's disputed component or dependency labels. Its no-model
gate executes both a raw client/server span self-join and the Graph aggregation
over minute-aligned half-open windows. RCA100 `t002` and OpenRCA 2.0 Hotel both
pass exact edge-set equality, unique-winner, and frozen-winner checks. They
remain development fixtures because their telemetry and earlier trajectories
influenced the task.

The position-balanced Graph development pilot is complete. All eight cells
succeeded. Graph reduced calls through evidence from three to one and reduced
rows returned in all four pairs; the combined median deltas were `-2` calls and
`-157.5` rows, with exact two-sided sign-test `p=0.125` for each metric. A
scorer-only correction accepted destination type proven by the exact canonical
result instead of requiring a redundant query argument; saved trajectories were
rescored without rerunning a model. These results validate the retrieval
mechanism but are not an RCA or measurement effect.

The fresh trajectory-blind Discovery and Graph selection described here was the
next stage at that point and is now complete. Its results and current limitations
are recorded above and in `RESULTS.md`.
Do not tune and rerun the six v17 RCA development cases as if they were a
holdout. No full RCA batch is authorized at this stage.

## Out of scope for the current stage

- Demo-generated telemetry as a substitute for an open failure dataset.
- Multi-agent RCA, remediation, or an LLM judge.
- Artificial topology or manually declared relationships.
- A generic dataset plugin framework before two concrete adapters prove the
  shared boundary.
- Paid RCA runs before the measurement dataset and protocol are frozen.
- Catalog broad-recall measurement and further micro-benchmark expansion.
- Multi-model report cards or a powered cross-system effect claim.
- Publishing raw provider trajectories as benchmark evidence.
