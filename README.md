# Semantic RCA Bench

This project measures the marginal effect of GreptimeDB's table semantic layer
and semantic graph on an LLM agent performing root-cause analysis.

The benchmark uses three nested visibility modes over the same ingested data:

- `raw`: telemetry tables and ordinary schema metadata only.
- `table_semantics`: adds a table profile over `information_schema.table_semantics`.
- `semantic_graph`: includes Table Semantics, then adds the computed entity and relationship
  tables plus a graph coverage summary. Reports display this cumulative treatment as
  `Table Semantics + Semantic Graph`.

The semantic tools simulate the context assembly planned for the GreptimeDB MCP
server. The benchmark does not treat the raw system tables as a complete agent
interface. Reports identify this simulated MCP surface in the protocol metadata.

Telemetry is replayed through production ingestion protocols. RCAEval, RCA100,
and OpenRCA 1.0 send metrics through Prometheus remote write v1. OpenRCA 2.0
preserves its source Gauge, Sum, and Histogram groups through OTLP metrics. All
trace-bearing adapters use OTLP HTTP with `greptime_trace_v1`; logs use Loki
push. RCA100 also sends Kubernetes events and alerts through separate Loki
tables. The adapters do not create telemetry tables, attach semantic options,
or ingest a dataset's reference topology.

## Development

```bash
uv sync --extra dev
uv run semantic-rca doctor \
  --greptimedb-repo /Users/dennis/programming/rust/greptimedb
```

Dataset downloads and prepared data under `.cache/` and `.data/`, database files
under `.instances/`, run state under `.runs/`, and trajectories and reports under
`.reports/` are ignored local artifacts.

API credentials are read from the process environment or macOS Keychain. Never
put them in a tracked configuration file or command-line argument.

The benchmark can also use the interactive coding-agent subscriptions instead
of usage-billed API credentials. Subscription runners execute the same benchmark
tools through an isolated local MCP broker; the broker retains treatment
visibility, read-only SQL enforcement, tool budgets, trajectories, and database
load measurement. Provider API credentials and endpoint overrides are removed
from the child process, so a subscription run cannot silently fall back to API
billing.

For Codex, authenticate with ChatGPT and select the subscription runner:

```bash
codex login
codex login status
uv run semantic-rca run \
  --report .reports/smoke-<run-id>.json \
  --runner codex-subscription \
  --model gpt-5.6-luna \
  --repetitions 1 \
  --output .reports/codex-subscription.json
```

For Claude Code, authenticate with a Claude Pro or Max account. Do not use
`claude auth login --console`, which selects Anthropic Console API billing.
The runner preserves the process proxy variables, reads OAuth from the normal
Claude Code login, disables plugin-provided MCP servers, and loads only its
run-local MCP server:

```bash
claude auth login
claude auth status
uv run semantic-rca run \
  --report .reports/smoke-<run-id>.json \
  --runner claude-subscription \
  --model sonnet \
  --repetitions 1 \
  --output .reports/claude-subscription.json
```

Subscription plans have shared usage limits and are not unlimited. Reports mark
the runner explicitly and omit API dollar estimates; compare treatments only
within the same runner and model. The API runner remains the default for
reproducible external measurements.

Claude models use `ANTHROPIC_API_KEY` or the
`semantic-rca-bench-anthropic` Keychain service. To avoid putting the key in
shell history:

```bash
read -s "BENCH_KEY?Anthropic API key: "; echo
security add-generic-password -U -a "$USER" \
  -s semantic-rca-bench-anthropic -w "$BENCH_KEY"
unset BENCH_KEY
uv run semantic-rca run \
  --report .reports/smoke-<run-id>.json \
  --repetitions 3
```

DeepSeek models use `DEEPSEEK_API_KEY` or the
`semantic-rca-bench-deepseek` Keychain service. The runner uses DeepSeek's
official Anthropic-compatible endpoint, so tool trajectories and usage records
keep the same report format:

```bash
read -s "BENCH_KEY?DeepSeek API key: "; echo
security add-generic-password -U -a "$USER" \
  -s semantic-rca-bench-deepseek -w "$BENCH_KEY"
unset BENCH_KEY
uv run semantic-rca run \
  --report .reports/smoke-<run-id>.json \
  --model deepseek-v4-flash \
  --repetitions 1
```

Use `deepseek-v4-flash` for low-cost interface gates and
`deepseek-v4-pro` when a stronger DeepSeek control is justified. Their pricing
snapshot is recorded in generated reports. A scored comparison must use one
model for every visibility treatment; results from different models measure a
model change, not a semantic-layer effect.

The runner uses a seeded treatment order and rotates it once per repetition.
Three repetitions place each of the three treatments in every execution
position once. Reports mark cross-treatment latency as confounded when the
schedule is not position-balanced. The runner applies the same base prompt,
model, tool-call cap, and database to every level, and withholds ground truth
until deterministic scoring. Each completed
`repetition × visibility` pair is persisted, so rerunning the same output file
resumes unfinished work.

Protocol v21 established the same system contract for API, Codex subscription,
and Claude subscription runners. Protocol v24 added provider-specific API prompt
caching and native cache-usage accounting. Protocol v25 adds provisional causal-hypothesis
triage: the agent establishes the failing operation, uses discriminating queries instead of
unconditional resource sweeps, and compares the same traced operation across the change point.
The
API runner sets its turn limit above the visible tool-call cap and records turn
exhaustion as a failed run instead of aborting the batch. The supported
subscription CLIs do not expose a turn-limit option; their broker enforces the
same database-tool cap, and the process timeout bounds the session. A taxonomy
violation is a scored incorrect answer in every runner. Protocol v23 retains
returned rows and calls through a correct diagnosis with at least one
execution-valid evidence citation as the RCA efficiency metrics. Both metrics
use the same eligibility guardrail. Each case contributes the median eligible
run-pair delta to inference; repetitions remain descriptive, and Holm adjustment
covers the four primary tests. The protocol gives the agent the selected
dataset's fault taxonomy and requires one canonical fault mechanism from that
taxonomy. The evaluator scores the mechanism by normalized equality. The diagnosis reports
the directly affected workload or infrastructure component separately from an
optional causal dependency. Component scoring is unavailable when a dataset
publishes conflicting structured component labels. A predicted causal
dependency remains diagnostic output; no dependency accuracy field exists
until a source publishes a canonical dependency label. Confidence remains a
diagnostic calibration signal and does not contribute to correctness.

The Graph treatment has a dedicated query tool that applies the half-open
incident window and groups each relationship by window and edge identity before
aggregating Rate, Error, and Duration (RED) fields. It also distinguishes
external alert identifiers from canonical graph entity IDs and tells the agent
to discover graph endpoints before filtering by ID.

Table Semantics and Graph treatments also expose semantic catalog search. It
searches table names, semantic options, and entity declarations across the
incident database, then ranks candidates by matched concepts. The tokenizer
normalizes slash abbreviations such as `I/O` to `io`, removes one-letter terms
and stop words, and matches short terms only at token boundaries. This lets the
agent find relevant tables in a wide schema before calling `describe_table`.
When Graph coverage is `empty`, the current protocol suppresses the graph query
tool and explicitly tells the agent not to infer topology.

The semantic treatment includes the metadata, tool schemas, basic usage guides,
and coverage snapshot that an MCP server would provide. The benchmark therefore
measures the complete agent-facing semantic interface, not a data-only
ablation. Semantic discovery and graph calls consume the same fixed cap as SQL
calls. Each run records requested calls and whether the cap rejected any call.

The 48-call default is a safety cap, not the measured budget. The initial prompt
and every tool-result turn tell the agent how many calls remain. Completion
efficiency is reported only when the cap is non-binding and the diagnosis is
jointly correct with at least one execution-valid evidence citation. Reports
also show discovery calls, failed and exact repeated calls, and calls to a
correct diagnosis.

Table profiles return the complete schema and semantic metadata. Sample rows are
opt-in because wide OTLP tables can otherwise dominate model input without
helping schema discovery. Each agent run records the SQL query count, failed
query count, returned row count, cumulative query time, and maximum query
concurrency at the GreptimeDB client boundary.

Use `--case-role development` while changing adapters, tools, prompts, or the
protocol. Use `--case-role measurement` only for cases frozen before their
telemetry and model behavior were inspected. Combined reports reject mixed
roles. Reports show the telemetry window, history before the alert, and whether
the ground truth provides a known pre-fault boundary.

Formal runs also reject database names containing the ground-truth component or
fault type. The database name is visible in SQL discovery instructions, so use
neutral identifiers such as `case_01` rather than case or incident labels.

The lower-cost discovery and Graph micro-benchmarks specified in
[`DISCOVERY.md`](DISCOVERY.md) and [`GRAPH.md`](GRAPH.md) are implemented and
their fresh measurement cells are complete. [`RESULTS.md`](RESULTS.md) records
the formal cohorts, exclusions, paired statistics, and limitations. A standalone
Chinese report is available in [`RESULTS.zh-CN.md`](RESULTS.zh-CN.md). The result
supports a constrained investigation-efficiency mechanism. Correct task output
is the guardrail against trading validity for fewer rows or tokens; higher
accuracy is not the primary claim. Do not start another paid full RCA batch from
this result alone.

`fixtures/measurement/results-summary.json` records exact hashes for the ignored
formal report artifacts, run-pair descriptions, and case-level inference. Model
tokens are post-hoc and exploratory; rows and tool calls are the pre-registered
primary efficiency metrics. Regenerate the tracked summary from retained local
reports with:

```bash
uv run python -m semantic_rca_bench.measurement_summary
```

The hashes make a retained local cohort auditable but are not sufficient for
independent public reproduction. The current formal reports are raw working
trajectories and are not the 1.0 release contract. Before 1.0, freeze a legally
usable reference cohort and a public artifact from which a third party can
reproduce scoring and primary metrics without exposing provider payloads,
credentials, or local machine data.

Benchmark 1.0 targets one canonical API runner and a small fixed public cohort
covering Table-positive, Graph-positive, and Graph-negative roles, plus a fresh
end-to-end RCA transfer demonstration. Subscription runners, multiple-model
report cards, catalog broad-recall, and a powered cross-system effect estimate
remain post-1.0 work.

The Graph-positive transfer source audit does not call a model or require a
GreptimeDB server. Download, verify, extract, and audit the pinned artifact with:

```bash
uv run semantic-rca aegis-fetch \
  --cache-dir .data/aegis \
  --output .reports/aegis-source-audit.json
```

The command requires the exact archive size and MD5 from the Zenodo record. It
extracts only `reproduction/data/rcabench-platform-v2`, rejects unsafe archive
members, and reconstructs service-call edges only from native Client-to-Server
parent-child spans. The audit applies the frozen selection gate in
`fixtures/reference/aegis-selection.json`. It does not ingest
`causal_graph.json` or redistribute source telemetry. `DATASETS.md` records the
license boundary, exclusions, and selected case. Use `aegis-audit` instead when
you already have a verified, extracted artifact.

After the source audit passes, run the selected-case production-protocol and
Graph equality gate in a fresh managed instance:

```bash
uv run semantic-rca aegis-transfer-audit \
  --cases-dir .data/aegis/rcabench-platform-v2/data/rcabench \
  --meta-dir .data/aegis/rcabench-platform-v2/meta/rcabench \
  --archive .data/aegis/FSE_26_RCA_dataset_study_reviewer.tar.gz \
  --run-dir .instances/aegis-transfer-001 \
  --database case_01 \
  --output .reports/aegis-transfer-audit.json
```

The command starts and stops only its own loopback GreptimeDB process. The run
directory must not already exist. It replays metrics and traces through OTLP
and logs through Loki, rejects protocol loss or identifier remapping, and writes
the complete normalized raw and Graph edge sets plus their hashes. Because the
publisher windows start at second 38, equality uses one minimal minute-aligned
envelope around the complete normal-plus-abnormal source window. The source
audit proves that the envelope adds no telemetry rows. Frozen normal and
abnormal method evidence continues to use the original publisher half-open
windows. This is a no-model gate; it does not run or authorize the transfer
experiment.

After the transfer audit passes, validate the frozen transfer scorer without
calling a model:

```bash
uv run semantic-rca aegis-transfer-scorer-audit \
  --transfer-audit .reports/aegis-transfer-audit.json \
  --output .reports/aegis-transfer-scorer-audit.json
```

The command binds the source audit to
`fixtures/reference/aegis-transfer-scorer.json`. The fixture freezes the
directed two-service answer, accepted HTTP method-replacement labels, complete
`GET`/`OPTIONS` evidence predicate, and canonical API runner contract. The
scorer requires a cited, successful SQL result from a Client-to-Server
parent-child join. One citation can prove the complete predicate, or multiple
unique citations can prove non-overlapping parts whose normalized union exactly
matches the predicate. A single-service answer, reversed edge, invalid citation,
wrong parent relation, duplicate evidence cell, or missing evidence row fails
the audit. The agent input contains the opaque case ID and no fault taxonomy.

`aegis-transfer-run` is the only model-invoking Aegis command. Its frozen model
is `deepseek-v4-flash`. It executes nine paid API trajectories: three
position-balanced repetitions across Raw, Table Semantics, and Semantic Graph.
Do not run it without explicit cost approval. The protocol v24 pilot completed all nine
trajectories but produced no jointly correct diagnosis. Because those trajectories informed the
protocol v25 prompt, the fixture now marks this case as development; another run cannot be reported
as a fresh measurement cell.
After approval, start the canonical run with:

```bash
uv run semantic-rca aegis-transfer-run \
  --cases-dir .data/aegis/rcabench-platform-v2/data/rcabench \
  --meta-dir .data/aegis/rcabench-platform-v2/meta/rcabench \
  --archive .data/aegis/FSE_26_RCA_dataset_study_reviewer.tar.gz \
  --run-dir .instances/aegis-transfer-paid-001 \
  --database case_01 \
  --source-audit-output .reports/aegis-transfer-paid-source-audit.json \
  --scorer-audit-output .reports/aegis-transfer-paid-scorer-audit.json \
  --output .reports/aegis-transfer-paid-run.json \
  --confirm-paid-api
```

The run directory and both output paths must not exist. The command starts one
exclusive loopback GreptimeDB process, repeats ingestion and every no-model
gate, and calls the API only after the source and scorer gates pass. Raw SQL and
the agent prompt retain the publisher half-open window. The Semantic Graph tool
uses the audited minute envelope because `observed_at` is minute-binned. The
local run report contains provider responses and is not a release artifact.

Since protocol v24, end-to-end RCA counts a citation as execution-valid only when
it uniquely identifies a successful, non-truncated SQL or Graph query result,
the result carries the same query ID, and the evidence claim is non-empty.
Catalog and schema discovery are not incident evidence. This check prevents
failed or fabricated references from unlocking efficiency metrics, but it does
not prove that the cited rows support the diagnosis. The Aegis transfer scorer
adds its source-specific deterministic evidence-support predicate; the generic
RCA scorer does not infer evidence entailment.

Token fields are runner-specific. The API runner sums provider usage over all
responses; `run.usage.input_tokens` excludes cache creation and cache reads.
Raw response usage keeps those fields so reports reconstruct total model-visible
input and billing. Anthropic API requests enable automatic
5-minute prompt caching. DeepSeek context caching is enabled by the provider and
requires no request flag; its Anthropic-compatible endpoint ignores
`cache_control`. Codex reads exactly one cumulative
`turn.completed` event and persists input including cache without a cache
breakdown. Claude adds input, cache creation, and cache reads into
`input_tokens`. Provider context includes system prompts, tool schemas, prior
tool or MCP results, and structured output, but no runner exposes a comparable
component-level split. Compare token deltas only within the same model, runner,
protocol, and case. API cost rendering uses separate cache-read pricing when raw
usage provides that split.

The implemented `discovery-audit` command checks the frozen evidence query and
catalog top-five gate without calling a model. `discovery-run` is a separate,
model-invoking command for the paired Raw/Table Semantics pilot. See
`DISCOVERY.md` for the exact fixtures, scoring contract, and commands.

The separate [`GRAPH.md`](GRAPH.md) protocol compares Table Semantics with
Table Semantics plus Semantic Graph on witnessed service-call retrieval. Its
`graph-audit` command independently reconstructs calls from raw spans and
requires exact equality with the Graph edge set before any model run.

The benchmark code and harness are licensed under Apache-2.0. Dataset licenses
remain independent and are recorded in [`DATASETS.md`](DATASETS.md); external
telemetry is not relicensed by this repository.

Import and validate RCA100 `t001` without calling a model:

```bash
uv run semantic-rca smoke-rca100 \
  --cache-dir .data/rca100 \
  --endpoint http://127.0.0.1:4000 \
  --database case_01 \
  --task t001
```

The smoke report distinguishes source metric rows, valid submitted samples,
unique Prometheus samples, duplicate samples, and conflicting timestamps. It
also checks database row counts and compares derived service call pairs with the
reference topology without ingesting that topology.

Import and validate OpenRCA Bank without calling a model:

```bash
uv run semantic-rca smoke-openrca \
  --cache-dir .data/openrca \
  --endpoint http://127.0.0.1:4000 \
  --database case_02 \
  --case task_6@2021-03-04T18:00
```

The case selector combines OpenRCA's repeated task index with the official
UTC+8 window start. Its report records all protocol representation decisions,
including unknown trace duration units and the absence of graph-capable span
semantics. Source timestamps, labels, topology, and ground truth are never
repaired.

OpenRCA Market and Telecom use source-window selectors that were frozen before
their selected telemetry was downloaded:

```bash
uv run semantic-rca smoke-openrca \
  --cache-dir .data/openrca \
  --endpoint http://127.0.0.1:4000 \
  --database case_market_01 \
  --case Market/cloudbed-1@2022-03-21T03:30

uv run semantic-rca smoke-openrca \
  --cache-dir .data/openrca \
  --endpoint http://127.0.0.1:4000 \
  --database case_telecom_01 \
  --case Telecom@2020-05-27T05:00
```

Their legacy traces preserve parent IDs and source operation fields but do not
declare OTel client/server span roles or standard entity identities. The adapter
does not infer those missing semantics from names or topology documents.

Import and validate the pre-registered OpenRCA 2.0 measurement case without calling a
model:

```bash
uv run semantic-rca smoke-openrca2 \
  --cache-dir .data/openrca2 \
  --endpoint http://127.0.0.1:4000 \
  --database case_03
```

The smoke gate records source metric collisions and unavailable aggregation
metadata, checks stored row counts after database primary-key semantics, and
verifies that native spans derive service-call relationships. The reference
causal graph is validation-only and is never ingested. The gate rejects a case
when manifest and injection root-service ground truth disagree.

Run independent case instances concurrently when throughput matters:

```bash
uv run semantic-rca batch \
  --report .reports/smoke-case-a.json .reports/smoke-case-b.json \
  --repetitions 3 \
  --jobs 2
```

Every source report passed to `batch` must point to a different, fresh
GreptimeDB endpoint. Separate databases on one server are invalid for Graph
evaluation because computed graph tables currently enumerate all user schemas.
`--jobs` does not parallelize treatments within a case. Reports record runner
concurrency; use `--jobs 1` for canonical latency measurements.

Render one case or combine multiple cases that use the same model, protocol,
tool budget, and repetition count:

```bash
uv run semantic-rca render \
  --report .reports/eval-case-a.json .reports/eval-case-b.json \
  --output .reports/pilot.html
```

The current benchmark result and its limits are recorded in
[`RESULTS.md`](RESULTS.md).
