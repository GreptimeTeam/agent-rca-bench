# Semantic RCA Benchmark Plan

This document is the canonical project plan. Local implementation details and
individual pilot findings must not replace its objective or experiment design.

## Objective

Measure whether GreptimeDB's semantic surfaces improve the investigation
efficiency of an LLM agent performing root-cause analysis over real, labeled
failure telemetry without reducing diagnosis validity.

For every case, compare two treatments over identical telemetry:

1. `raw`: telemetry tables and ordinary schema metadata.
2. `semantic_graph`: the complete GreptimeDB Semantic Graph surface, including
   table semantics, computed entities and relationships, and diagnostic fields.

The benchmark measures `semantic_graph - raw`. It does not assume the delta is positive.
Table semantics is an internal Graph capability, not a main treatment. A separate
ablation protocol may isolate internal contributions without changing the main comparison.
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
  metadata in isolation. `benchmark_protocol()` records this estimand and the
  components visible in each treatment so reports cannot describe the result as
  a data-representation-only effect.
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
  every execution position. With two treatments, one repetition is not
  position-balanced.
- Report correctness per dataset when answer taxonomies differ. Do not sum
  heterogeneous labels into one accuracy number.
- Freeze the dataset set, case-selection rules, and protocol before paid runs.

The pre-registered primary efficiency metrics are GreptimeDB rows returned and
tool calls through a correct diagnosis whose required causal claims are grounded
by execution-valid evidence. Claim annotations record agent intent but do not determine support;
the scorer derives scope from SQL, facts from returned rows, and the claim verdict from the frozen
rubric. Apply the same eligibility guardrail to both metrics. For each case,
take the median eligible run-pair delta; use the case medians for the exact
two-sided sign test and adjust the two primary tests with Holm's method. Run-pair
directions are descriptive only. A direction-consistent result that does not pass Holm is reported
with the directional case count, eligible case count, case median, unadjusted p-value, and
Holm-adjusted p-value; it is not called statistically significant. Discovery ordering and other trajectory metrics
are exploratory and cannot support headline claims.

Protocol v26 introduced, and protocol v30 retains, a transfer guardrail that does not collapse
the result into one success bit. `diagnosis_correct` covers the causal locus, causal operation,
category, and mechanism code.
`required_evidence_covered`
requires claim-typed evidence for the causal locus and mechanism. `claim_grounding` records the
required status and supporting citations for each claim.
Entity existence and an ordinary witnessed calls edge are navigation evidence, not causal-locus
evidence. Required locus grounding must be incident-local and tied to the declared operation or
mechanism under both treatments; a mechanism-bound result may ground both required claims.
`citation_integrity` and `execution_reliability` are reported separately.
Transfer efficiency is eligible only when the diagnosis is correct, the
required claims are supported, and execution is reliable. An unrelated invalid
extra citation prevents `auditable_completion` but does not retroactively make
the diagnosis incorrect or erase an otherwise eligible efficiency trajectory.

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

Protocol labels such as v24 through v31 identify internal development cycles. They record changes
to the harness and make development experiments interpretable; they are not release versions. The
current tree does not preserve runtime compatibility with earlier development protocols.

The first public report is produced only after one commit freezes the complete cohort, model roster,
GreptimeDB revision, prompt, tools, scorer, statistics, and artifact contract. Every published cell
is run from that frozen commit. The report records the exact commit and is released with a Git tag;
reproduction checks out that tag rather than asking current `main` to read an older report schema.
The tagged tree must deterministically rescore the published sanitized trajectories and regenerate
the report. Reinvoking a provider is a replication run and is not expected to reproduce model text
byte for byte.

The completed Discovery v2 and Graph v3 results are mechanism evidence, not a
powered estimate of a general semantic-layer effect and not yet a public 1.0
cohort. Benchmark product readiness and confirmatory research readiness are
separate milestones.

Benchmark 1.0 requires a frozen specification and canonical API runner, a small
legally reproducible cohort covering Semantic Graph positive and negative roles,
one fresh end-to-end transfer demonstration, and public
artifacts from which a third party can reproduce scoring and primary metrics.
The first report includes six model configurations, but does not pool them into
one score or use model count as statistical power. It does not require many
system families or power for a population claim. Results must be labeled as
evidence over the fixed cohort rather than a general effect.

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
measurement demonstration. The next candidate from the original frozen unconsumed list was frozen
separately as `aegis-transfer-002`. Its isolated production-protocol replay and exact 41-edge
raw/Graph equality pass.

Protocol v25 executed 27 cells over `deepseek-v4-flash`, `deepseek-v4-pro`, and
`claude-sonnet-5`, plus one post-measurement Opus Graph diagnostic. Those runs are retained as
development evidence, not as a valid transfer measurement: the v25 scorer used server span
duration instead of the publisher-declared client-to-server start gap, required free-text fault
labels to equal a hidden token, and constrained mechanism evidence to the canonical aggregate
shape. The 27 cells completed without runner errors or budget exhaustion, but their zero eligible
pairs do not measure model ability or semantic-layer efficiency under a valid contract.

Protocol v26 corrected the delay observable to `server.timestamp - client.timestamp`, introduced
a case-independent structured mechanism ontology, typed evidence by claim, and accepted equivalent
threshold proofs while retaining AST-verified edge, operation, role, parent, identity, and
half-open-window constraints. The consumed v25 case is bound to v26 only as a development
calibration fixture. Its nine `deepseek-v4-flash` cells completed with no runner error or budget
exhaustion. One Semantic Graph run produced the correct structured diagnosis, but no run executed a
complete Client/Server start-gap transition proof; the calibration therefore did not establish an
eligible efficiency pair. The observed failure is model investigation behavior rather than a
scorer false negative. The v27 cycle starts before any fresh-case model call. It fixes fail-closed
audit metadata and makes evidence validation depend on SQL semantics rather than one surface
spelling: the source audit proves case normalization cannot merge identities or OTel enum values,
while the scorer requires every role, identity, parent, operation, and window predicate to remain
an effective filter in one telemetry scope. Earlier fixture files are immutable records only. The
current runtime does not rescore, resume, or export their report, scorer, or protocol schemas.

The fresh v27 measurement selection preserves the trajectory-blind v26 selection bytes and was
computed before agent trajectory from all source-observable, graph-eligible Aegis cases supported
end to end by the transfer loader, oracle, and scorer after excluding the exact v24 and v25 parent
manifests. The supported source
predicates are the Client-to-Server start gap and JVM exception predicates; source-observable
restart and memory-pressure predicates are not silently admitted without a matching transfer
pipeline. The frozen selection selects the single-component JVM exception case as opaque ID
`aegis-transfer-003`. The source and stored oracles require a clean normal window and repeated
`retrieveByName` Error spans plus exception logs after onset. The scorer uses `component` scope,
requires `causal_component=ts-train-service` with no edge endpoints, and scores the global
`application_error` mechanism. It accepts
equivalent complete-window evidence without prescribing one SQL statement.

Before any `aegis-transfer-003` model cell, the agent-visible Semantic Graph relationship
projection was frozen against the current GreptimeDB contract with `unmatched_count` and
`duration_max` in addition to the existing RED fields. The benchmark has one current Semantic Graph query surface;
it does not retain a runtime branch for the earlier experimental GreptimeDB shape.

The v27 Graph equality gate selects its comparison strategy from the source boundary evidence. A
minute-representable boundary requires independent normal and abnormal raw/Graph equality plus
combined-window equality. When both source periods contain clients in the same `observed_at` minute,
the audit proves that separate Graph periods are not representable and requires exact equality over
the contiguous union instead. The selected v27 case follows the latter path. Two independent
ingestion runs produce the same source-semantic hash. No v27 model cell ran.

Protocol v28 removes `table_semantics` as a standalone treatment. The main experiment compares
Raw with the complete GreptimeDB Semantic Graph surface; internal table semantics remain available
inside the Graph treatment. It adds `gpt-5.6-sol` through the OpenAI Responses API alongside
`deepseek-v4-pro`, `claude-sonnet-5`, and `claude-opus-4-8`. Each model runs two position-balanced
repetitions over the two treatments, for 16 cells. OpenAI runs use stateless output-item replay and
implicit 30-minute prompt caching, explicit `medium` reasoning effort, and a 16,384-token combined
reasoning-and-visible-output budget. The first scheduled OpenAI cell is a separately authorized,
bounded transport and truncation probe before the remaining paid cells. The first v28 OpenAI
Raw/Graph pair ran as a development probe. Its Graph trajectory found both the caller-to-callee
error-propagation edge and the downstream service's local exceptions, but the output contract
encouraged the model to encode the caller as the affected component and the callee as a
dependency-edge failure.
This selection exhausts the pinned reviewer cohort under the supported-mechanism rule: after
the v24 method-replacement and v25 delay cases are consumed, the v27 JVM-exception case is the only
remaining eligible candidate. A later fresh transfer case therefore requires a new pinned cohort
or a separately frozen expansion of end-to-end mechanism support; it cannot reuse a consumed case.
Raw `.reports/` trajectories are not the release contract because they contain
provider payloads and local metadata.

Protocol v29 makes the causal answer isomorphic to the source truth. A component-scoped answer
contains one `causal_component`; a dependency-edge answer contains one directed `edge_source` and
`edge_destination`. `impacted_component` is descriptive and does not affect root-cause
correctness. The SQL tool returns at most 200 rows by default and accepts an explicit per-query
limit up to 1,000. A truncated result cannot be cited; both API transports return an invalid final
submission to the agent so it can issue an aggregate, narrow the query, or request a larger bounded
result. Because the v28 case-003 trajectory caused these changes, v29 binds case 003 only as a
development calibration case. A fresh transfer measurement requires a new source case or cohort.

The two v29 OpenAI development cells exposed a scorer false negative rather than a runner or model
failure. The Raw run cited a complete operation-level Error transition and a complete grouped log
result containing 1,981 `NullPointerException` records, but the scorer required the hidden
intervention boundary and an `exception` predicate in SQL. The Graph run correctly identified the
component and operation but did not inspect exception logs. Protocol v30 replaces query-recipe
matching with the claim-grounding contract in `SCORING.md`. The scorer accepts a telemetry-derived
observed onset and an exception signature present in a complete grouped result, while still
requiring source OTel Error status, source identity, operation lineage, complete baseline coverage,
and exception evidence. Shadow scoring therefore grounds the v29 Raw trajectory and leaves the
Graph mechanism ungrounded. The v29 results remain immutable development records.

A two-cell v30 `gpt-5.6-sol` Raw/Graph calibration then produced correct, fully cited diagnoses on
both treatments. The first v30 scorer revision rejected both because it recognized neither
status-grouped and conditional trace aggregates nor complete grouped log results with an omitted
zero-period row. Revision `aegis-transfer-jvm-exception-v5` defines these as equivalent result
shapes, provided source identity, operation and OTel status lineage, complete windows, and
non-truncation remain provable. Deterministic rescoring accepts both cells. This pair is development
calibration because it changed the scorer; it is not evidence for the final semantic-layer effect.

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

### Current v32 release candidate

The current formal suite compares `raw` with the complete `semantic_graph` product surface. It
binds one release GreptimeDB revision, one six-model API roster, one shared 16,384-token output
ceiling, provider-specific reasoning settings, and two position-balanced repetitions. The suite
contains 192 fixed-cohort Discovery/Graph cells and 240 fresh end-to-end OpenRCA2 cells, for 432
cells total. Case is the independent unit; repetitions describe variability only.

The fresh transfer selection contains ten trajectory-blind cases: four workload restarts, three
call-path delays, two CPU-saturation cases, and one memory-pressure case across Hotel Reservation
and OpenTelemetry Demo. It uses opaque agent IDs and never ingests the publisher causal graph. Two
development cases form a 24-cell pilot over three models. Measurement expansion requires at least
6 of 12 paired repetitions to be jointly efficiency-eligible, at least three eligible pairs per
pilot case. This 50% floor is a post-hoc development calibration set after observing the first
pilot case's v5 shadow score of four jointly eligible pairs out of six. It tests whether the primary
paired metrics remain estimable rather than requiring near-perfect model evidence behavior; it is
not a pre-registered effect threshold. Treatment-asymmetric eligibility and its per-claim rejection reasons are mandatory
diagnostics rather than a hard gate: unequal completion may be a real treatment outcome.

All ten measurement cases have completed two independent provider-free preflights against the
bound release binary. Both passes accepted the same source and protocol row counts, rejected no
trace or span IDs, produced exact complete raw-span/Graph edge-set equality, matched the frozen
stored mechanism predicate, and produced identical source-semantic hashes. No measurement provider
cell has run. Twelve development cells completed for the first pilot case. Their trajectories
exposed result-side identity false negatives. After closing UNION source, namespace identity, and
`HAVING` completeness fail-open paths, deterministic v6 shadow scoring still yields ten eligible
runs and four jointly eligible pairs out of six. The second
pilot case has not run. The current verifier and bound fixtures require provider-free validation and
code review before a newly authorized paid continuation.

The 240-cell transfer schedule must not start until the 24-cell development pilot passes. The
public artifact exporter must deterministically recompute diagnosis, claim grounding, citation
integrity, execution reliability, primary efficiency fields, case medians, the fixed 12-hypothesis
Holm family, and the artifact hash from sanitized tool inputs and result projections. The combined
JSON and HTML report must be generated only from the validated micro and transfer artifacts.

### Prior internal evidence

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

The release-focused Aegis path has a fresh measurement case. Protocol v31 freezes
`aegis-transfer-004`, a component-scoped `PodFailure` whose source restart counter changes from 24
zero samples to 24 one-valued samples. The loader preserves the stale publisher pod label as an
audited mismatch and binds the mechanism to the exact source container identity. Production OTLP
metrics and traces plus Loki logs pass row-count, protocol-rejection, and ID-remapping gates. The
complete raw and Graph union contains 43 equal distinct edges with the same normalized hash. Source,
scorer, protocol, and preflight audits pass. The execution roster contains `gpt-5.6-sol`,
`deepseek-v4-pro`,
`claude-opus-5`, `claude-fable-5`, `glm-5.3`, and the open-weight
`qwen3.8-2.4t-a95b`. Two Raw/Graph repetitions produce 24 cells. GLM and Qwen use explicit
provider-bound China transports rather than model-name routing: BigModel Chat Completions for GLM
and a caller-supplied Beijing workspace Responses endpoint for Qwen. Tenant endpoint identifiers
must not enter repository fixtures or public artifacts. Qwen's Session cache header and disabled
parallel tool calls are explicit runner parameters rather than provider defaults. All models use a
16,384-token combined reasoning-and-visible-output limit. Provider-specific reasoning levels are
set explicitly to their documented defaults and are not treated as a common cross-provider compute
scale. The v31 provider run completed all 24 transfer cells without runner errors or budget
exhaustion. Every run produced the correct structured diagnosis. One `gpt-5.6-sol` Graph run
covered both required evidence claims; no Raw/Graph repetition pair was jointly eligible for an
end-to-end efficiency comparison.
The provider interface probes are complete. DeepSeek accepted the frozen Anthropic-compatible
`output_config.effort` request; BigModel and DashScope returned the usage-detail objects used for
reasoning and cache breakdown. These were synthetic interface probes, not formal cells.

The formal suite manifest binds the six Discovery cases, two Graph cases, Aegis case 004, the
GreptimeDB revision, the `release` build profile, and the six-model roster. All 192 micro cells and
24 Aegis cells completed, for 216 total. The eight independent micro preflights and the Aegis
source, scorer, protocol, exact-edge, and mechanism gates passed against the bound release binary.
The sanitized micro artifact deterministically rescores all 192 cells from canonical cited
aggregates. The transfer artifact deterministically validates the public diagnosis, citation
grounding, reliability, primary-metric eligibility, and model summaries. The combined report
generator reads only those public artifacts and produces machine-readable JSON and self-contained
HTML.

This is a release-candidate measurement, not the first public 1.0 run. The model execution started
from a dirty benchmark worktree, so the private reports do not bind the benchmark source to a Git
commit. The 1.0 run must start from the tagged clean commit required by the release contract.
Catalog broad-recall and powered cross-system enrollment remain deferred.

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

The current Discovery v3 and Graph v5 protocols compare `raw` with
`semantic_graph`. They do not preserve `table_semantics` as a standalone
treatment. Earlier Table-versus-Raw and Graph-versus-Table results remain
development ablations and are not mixed with the current paired comparison.

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
- A cross-model pooled score or a powered cross-system effect claim.
- Publishing raw provider trajectories as benchmark evidence.
