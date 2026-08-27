# Semantic RCA Benchmark Plan

This document is the canonical project plan. Local implementation details and
individual pilot findings must not replace its objective or experiment design.

## Objective

Measure whether GreptimeDB's semantic surfaces improve an LLM agent's root-cause
analysis over real, labeled failure telemetry.

For every case, compare three nested treatments over identical telemetry:

1. `raw`: telemetry tables and ordinary schema metadata.
2. `table_semantics`: raw access plus Table Semantics.
3. `semantic_graph`: Table Semantics plus computed entities and relationships.

The benchmark measures `table_semantics - raw` and
`semantic_graph - table_semantics`. It does not assume either delta is positive.

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

Select additional datasets by authority, root-cause quality, telemetry fidelity,
system and failure diversity, and non-overlap with this portfolio. Freeze the
measurement cases before inspecting their telemetry or agent results. Record
candidate decisions in `DATASETS.md`.

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
- Semantic metadata calls consume the same tool-call cap as SQL calls. A binding
  cap measures fixed-budget investigation efficiency, not unconstrained RCA
  capability. Record requested calls and cap exhaustion for every run.
- Ground truth is unavailable to the agent and only enters deterministic scoring.
- Treat graph evidence coverage as an observed property of a case, not as a
  benchmark fixture.
- Record the GreptimeDB revision, dataset revision, adapter version, model,
  prompt protocol, telemetry counts, query counts, returned rows, concurrency,
  elapsed time, tokens, and cost.
- Do not mix results from different protocol versions.
- Label cases as `development` or `measurement`. Never use a case for formal
  measurement after its failures influenced the protocol or prompt.
- Compare treatment latency only when every treatment appears equally often in
  every execution position. With three treatments, one repetition is not
  position-balanced.
- Report correctness per dataset when answer taxonomies differ. Do not sum
  heterogeneous labels into one accuracy number.
- Freeze the dataset set, case-selection rules, and protocol before paid runs.

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

## Current status and next step

RCAEval, RCA100, and OpenRCA adapters pass their no-model ingestion and semantic
surface gates. The selected cases cover entity-only, relational, and empty graph
surfaces. These cases are development cases because protocol versions v6 through
v10 changed in response to their failures.

The protocol v8 runs are diagnostics, not an effect estimate. Six of the nine
runs requested more calls than the 18-call cap. One repetition also put Graph
first, Raw second, and Table Semantics third for every case. The fixed cap and
execution order confound capacity and latency comparisons. The three datasets
also use different fault taxonomies, so their correctness counts must remain
per-dataset. `RESULTS.md` records the valid observations and limitations.

Protocol v11 fixes the confirmed implementation defects and makes the
limitations auditable. It gates the Graph tool on the produced `empty` status,
scopes RCAEval taxonomies by dataset, filters discovery rows before truncation,
aligns catalog recall and ranking fields, records requested and rejected tool
calls, records execution position and case role, and reports baseline
availability.

Next, evaluate authoritative dataset candidates and add only complementary
corpora or cases that pass no-model fidelity gates. Freeze the measurement set
and case-selection rules before inspecting agent behavior. Do not run paid RCA
while adding datasets. After the set is frozen, run one controlled batch and
use its failures for the next implementation review.

## Out of scope for the current stage

- Demo-generated telemetry as a substitute for an open failure dataset.
- Multi-agent RCA, remediation, or an LLM judge.
- Artificial topology or manually declared relationships.
- A generic dataset plugin framework before two concrete adapters prove the
  shared boundary.
- Paid RCA runs before the measurement dataset and protocol are frozen.
