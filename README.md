# Semantic RCA Bench

This project measures whether GreptimeDB Semantic Graph changes the efficiency
of an LLM agent performing root-cause analysis.

[`SCORING.md`](SCORING.md) defines diagnosis correctness, causal-claim grounding, citation
integrity, execution reliability, and efficiency eligibility for end-to-end RCA tasks.

The benchmark uses two visibility modes over the same ingested data:

- `raw`: telemetry tables and ordinary schema metadata only.
- `semantic_graph`: the complete GreptimeDB Semantic Graph surface, including table semantics,
  computed entities and relationships, and a graph coverage summary.

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
security add-generic-password -U -a "$USER" \
  -s semantic-rca-bench-anthropic -w
uv run semantic-rca run \
  --report .reports/smoke-<run-id>.json \
  --repetitions 3
```

DeepSeek models use `DEEPSEEK_API_KEY` or the
`semantic-rca-bench-deepseek` Keychain service. The runner uses DeepSeek's
official Anthropic-compatible endpoint, so tool trajectories and usage records
keep the same report format:

```bash
security add-generic-password -U -a "$USER" \
  -s semantic-rca-bench-deepseek -w
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

OpenAI models use `OPENAI_API_KEY` or the `semantic-rca-bench-openai` Keychain
service. The runner uses the Responses API with `store=false`, replays every
response output item between tool turns, and requests encrypted reasoning items
for stateless continuity. GPT-5.6 uses implicit 30-minute prefix caching, and
reports account for uncached input, cache writes, cache reads, and output
separately:

```bash
security add-generic-password -U -a "$USER" \
  -s semantic-rca-bench-openai -w
uv run semantic-rca run \
  --report .reports/smoke-<run-id>.json \
  --model gpt-5.6-sol \
  --api-transport openai-responses \
  --reasoning-effort medium \
  --max-output-tokens 16384 \
  --repetitions 2
```

GLM-5.3 uses `BIGMODEL_API_KEY` or the `semantic-rca-bench-bigmodel` Keychain service and
BigModel's China Chat Completions endpoint. Its contract fixes `thinking.type=enabled`, `max`
reasoning, and a 16,384-token output budget. BigModel automatically caches repeated context:

```bash
security add-generic-password -U -a "$USER" \
  -s semantic-rca-bench-bigmodel -w
uv run semantic-rca run \
  --report .reports/smoke-<run-id>.json \
  --model glm-5.3 \
  --api-transport bigmodel-chat-completions \
  --reasoning-effort max \
  --max-output-tokens 16384 \
  --repetitions 2
```

The Qwen arm uses `qwen3.8-max` through Alibaba Cloud Model Studio. It reads
`DASHSCOPE_API_KEY` or the `semantic-rca-bench-dashscope` Keychain service. The
`DASHSCOPE_BASE_URL` environment variable or `semantic-rca-bench-dashscope-base-url` Keychain
service must name the caller's China (Beijing) workspace Responses endpoint; the tenant-specific
hostname is runtime configuration and is not written to reports. The contract fixes `xhigh`
reasoning and enables the provider's Session cache header:

```bash
security add-generic-password -U -a "$USER" \
  -s semantic-rca-bench-dashscope -w
security add-generic-password -U -a "$USER" \
  -s semantic-rca-bench-dashscope-base-url -w
uv run semantic-rca run \
  --report .reports/smoke-<run-id>.json \
  --model qwen3.8-max \
  --api-transport dashscope-cn-beijing-responses \
  --reasoning-effort xhigh \
  --max-output-tokens 16384 \
  --repetitions 2
```

Both China providers bill in CNY, which reports preserve without inventing an exchange rate.
Alibaba Cloud publishes the selected Beijing model's uncached, automatic-cache-hit, and output
rates. BigModel's public price page did not list GLM-5.3 when this protocol was prepared, so GLM
token usage remains auditable but its cost estimate fails closed until an official rate is frozen.

The runner uses a seeded treatment order and rotates it once per repetition.
Two repetitions place both treatments in both execution positions once. Reports mark
cross-treatment latency as confounded when the
schedule is not position-balanced. The runner applies the same base prompt,
model, tool-call cap, and database to every level, and withholds ground truth
until deterministic scoring. Each completed
`repetition × visibility` pair is persisted, so rerunning the same output file
resumes unfinished work.

Protocol v21 established the same system contract for API, Codex subscription,
and Claude subscription runners. Protocol v24 added provider-specific API prompt
caching and native cache-usage accounting. Protocol v25 added provisional causal-hypothesis
triage. Protocol v26 makes that method case-invariant and symmetric across mechanism classes,
adds structured causal scope and mechanism fields, and routes cited results to typed claims. The
Semantic Graph tool also exposes `unmatched_count` and `duration_max` from the current
GreptimeDB relationship contract. It preserves their source populations: `unmatched_count` is not
generally additive to `request_count`, and `duration_max` uses the same population as the duration
sum and count. Table profiles distinguish the identity qualifier from separately reported scope
columns. A nine-cell v26 calibration used only the consumed delay case. Protocol v27 retains the
same agent-visible surface and hardens its no-model audit contract before any fresh-case run. The
v28 cycle reduces the main comparison to Raw versus the complete GreptimeDB Semantic Graph surface
and adds the OpenAI Responses API runner. Protocol v29 replaces the ambiguous affected-component
and dependency pair with a causal locus that is either one component or one complete directed
edge. It keeps the default SQL result cap at 200 rows, lets the agent request up to 1,000 rows per
query, and returns truncated final citations for repair. Protocol v30 scores source-derived causal
claims rather than canonical query recipes and reports per-claim grounding. Protocol v31 adds the
case-independent `workload_restart` mechanism and binds the first fresh Aegis measurement case. The
agent establishes the failing operation, uses discriminating queries instead of unconditional
resource sweeps, and compares the same operation across the change point. The
v32 transfer scorer separates query scope, returned values, and the logical claim. A complete,
value-independent baseline proves absence; enough in-window anomalous observations prove existence.
Claim support is inferred from cited results rather than citation labels, evaluator-specific
aliases, or one identity-filter spelling. A complete result may establish the target population
through direct identity columns, and source-proven pod identities are accepted as equivalent to the
canonical container identity. The
scorer also accepts a full-window workload restart-counter transition with an anomalous first
crossing, and an exact Client/Server parent-child aggregate whose Client duration changes while
Server execution remains fast. Runs that pass diagnosis, citation, provenance, and execution hard
gates but remain unresolved by deterministic grounding enter a two-model semantic-adjudication
queue; the report retains deterministic-only and adjudicated results separately. The
API runner sets its turn limit above the visible tool-call cap and records turn
exhaustion as a failed run instead of aborting the batch. The supported
subscription CLIs do not expose a turn-limit option; their broker enforces the
same database-tool cap, and the process timeout bounds the session. A taxonomy
violation is a scored incorrect answer in every runner. Protocol v23 retains
returned rows and calls through a correct diagnosis with at least one
execution-valid evidence citation as the RCA efficiency metrics. Both metrics
use the same eligibility guardrail. Each case contributes the median eligible
run-pair delta to inference; repetitions remain descriptive, and Holm adjustment
covers the two primary tests. The protocol gives the agent the selected
dataset's fault taxonomy and requires one canonical fault mechanism from that
taxonomy. The evaluator scores the mechanism by normalized equality. A component-scoped diagnosis
names one `causal_component`. An edge-scoped diagnosis names one directed `edge_source` and
`edge_destination`. `impacted_component` records propagated impact but does not define or score the
root-cause locus. Component scoring is unavailable when a dataset publishes conflicting structured
component labels. Confidence remains a diagnostic calibration signal and does not contribute to
correctness.

These protocol numbers identify internal development cycles, not public
releases. Results produced before the release freeze remain development
experiments even when a case has a trajectory-blind measurement role. A public
report is produced from one frozen commit and binds itself to the corresponding
Git release tag. Reproduction checks out that tag; current code does not retain
runtime compatibility with earlier development protocols.

The Graph treatment has a dedicated query tool that applies the half-open
incident window and groups each relationship by window and edge identity before
aggregating Rate, Error, and Duration (RED) fields. It also distinguishes
external alert identifiers from canonical graph entity IDs and tells the agent
to discover graph endpoints before filtering by ID.

The Semantic Graph treatment also exposes semantic catalog search. It
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
ablation. The machine-readable protocol records this estimand and the exact
components assigned to Raw and Semantic Graph. Semantic discovery and graph calls consume the same fixed cap as SQL
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
independent public reproduction. The current v32 workflow exports sanitized
micro and transfer artifacts and deterministically produces combined JSON and
HTML. Development pilot cells have run, but no measurement cell has run. The first public report must execute the
complete suite from one clean tagged benchmark commit.

Benchmark 1.0 targets one canonical API runner and a small fixed public cohort
covering discovery and Graph retrieval plus a fresh end-to-end RCA transfer
demonstration. The first report uses the same five-model roster throughout;
model report cards remain separate by benchmark and do not create a pooled
cross-task score. Subscription runners, catalog broad-recall, and a powered
cross-system effect estimate remain post-1.0 work.

The current end-to-end transfer cohort uses ten fresh OpenRCA2 cases selected before any of
their agent trajectories were observed. The frozen strata contain four workload-restart cases,
three call-path-delay cases, two CPU-saturation cases, and one memory-pressure case across Hotel
Reservation and OpenTelemetry Demo. Agent input uses only opaque IDs
`semantic-rca-transfer-001` through `semantic-rca-transfer-010`; source case names, injection
labels, conclusions, and reference causal graphs remain outside the prompt and ingestion path.

The provider-free workflow is:

```bash
uv run semantic-rca transfer-selection-audit \
  --output .reports/openrca2-transfer-v32-selection-audit.json

uv run semantic-rca transfer-preflight \
  --phase pilot \
  --greptimedb-repo /path/to/greptimedb \
  --run-root .instances/openrca2-transfer-v32-pilot-preflight \
  --output .reports/openrca2-transfer-v32-pilot.json

uv run semantic-rca transfer-preflight \
  --phase measurement \
  --greptimedb-repo /path/to/greptimedb \
  --run-root .instances/openrca2-transfer-v32-measurement-preflight \
  --output .reports/openrca2-transfer-v32-measurement.json
```

Each preflight starts the bound release GreptimeDB binary in one exclusive loopback-only instance
per case, uses an independent data directory and database, and stops only the process it started.
It rejects source-file or selection drift, protocol rejection, trace/span ID remapping, source
identity changes, stored row-count changes, reference causal-graph ingestion, and a non-empty
Semantic Graph instance. It independently reconstructs every service `calls` edge from stored
Client-to-Server parent-child spans and requires complete normalized edge-set equality with
`greptime_private.semantic_relationships`, including request and OTel server-error counts. The
window audit records source half-open windows, the minimal whole-minute Graph envelope, widened
server scan, boundary-bin strategy, normalized rows, and both edge-set hashes.

The mechanism gate replays one source-faithful stored-telemetry predicate per selected mechanism.
It requires a non-empty baseline with no observations above the frozen threshold and at least two
anomalous observations above it. Delay evidence uses the directed source edge, native Client and
Server roles, exact trace/parent identity, source operation, and server-start minus client-start
nanoseconds. Metric mechanisms retain the source table, container identity, value, timestamp, and
threshold. The scorer accepts complete raw rows, expression-equivalent aggregates, split windows,
`BETWEEN`, aliases, transparent CTEs, unambiguous grouped partitions, and a complete baseline paired
with partial anomalous existence evidence. A frozen exact namespace predicate is accepted when the
source audit proves it preserves the container population. Value filtering cannot prove baseline
absence but may prove anomalous existence. A SQL limit is accepted only when the returned row count
is strictly below it; truncated results always fail. Wrong identity, roles, parent relations,
lineage, ambiguous periods, and hard-coded result constants fail closed.

The formal transfer fixture freezes five models, Raw and Semantic Graph, two position-balanced
repetitions, and 48 executed tool calls per cell. All configurations have a 16,384-token shared
reasoning-and-visible-output ceiling. Reasoning settings are provider-specific frozen
configurations, not a common compute scale: OpenAI `medium`, DeepSeek and Claude `high`, and GLM
`max`. Prefix caching is enabled through each provider's supported interface. DeepSeek and BigModel
connect directly and ignore process-level proxy environment variables. OpenAI and Anthropic retain
the operator's environment routing. The development pilot also freezes Qwen `xhigh` through the
direct DashScope Beijing transport, but Qwen is not part of the formal roster because its eight
pilot cells took about 1.35 hours, with 8.8-minute median and 15.4-minute maximum cell latency.

Before the 200-cell measurement schedule, the frozen 24-cell development pilot runs two untouched
development cases with `gpt-5.6-sol`, `deepseek-v4-pro`, and
`qwen3.8-max`. The pilot diagnostic compares its result with thresholds of 6 of 12 Raw/Graph pairs
overall and three eligible pairs per case. A failed threshold remains a reported negative result;
it does not block measurement execution. Treatment-asymmetric eligibility is
reported with per-claim rejection reasons; it is not a hard gate because unequal completion is part
of the treatment outcome. The measurement report stores all 12 pair decisions and recomputes this
gate on every resume.

The final scorer-only replay of the retained pilot has 6 of 12 eligible pairs: 5 of 6 for the
restart case and 1 of 6 for the delay case. The per-case diagnostic threshold is false. Two GPT Raw
runs that summarized Client and Server durations are not accepted as start-gap evidence because
those durations cannot establish `server_start - client_start`. Two diagnosis-correct Qwen Graph
runs enter semantic adjudication. Adjudicating either Qwen Graph run cannot make its pair eligible
because the corresponding Raw run has an incorrect diagnosis.

**The following commands call paid APIs. Run them only after reviewing the current code, fixtures,
and no-model artifacts and granting a new approval for that invocation.** A run executes pending
cells only for the next scheduled case. Every resume uses a new empty run directory; the private
report is updated atomically and must remain an exact schedule prefix.

```bash
uv run semantic-rca transfer-run \
  --report .reports/openrca2-transfer-v32-pilot.json \
  --run-dir .instances/openrca2-transfer-v32-pilot-case-01 \
  --confirm-paid-api

# Repeat with a new run directory for pilot case 02. After binding the completed pilot diagnostic:
uv run semantic-rca transfer-run \
  --report .reports/openrca2-transfer-v32-measurement.json \
  --pilot-report .reports/openrca2-transfer-v32-pilot.json \
  --run-dir .instances/openrca2-transfer-v32-measurement-case-01 \
  --confirm-paid-api
```

Use `--max-new-runs 1` for an explicitly approved transport probe. Runner failures are persisted
as scoreable failed cells instead of aborting the batch. Provider confirmation is never stored and
subscription fallback is forbidden.

After a pilot or measurement report completes, generate the exhaustive private adjudication queue:

```bash
uv run semantic-rca transfer-adjudication-queue \
  --run-report .reports/openrca2-transfer-v32-pilot.json \
  --output .reports/openrca2-transfer-v32-adjudication-queue.json
```

The queue is a private artifact because it contains the cited raw telemetry rows. Keep it under an
ignored report directory and do not publish it.

The judge input omits model identity, the explicit treatment label, run order, and aggregate
outcomes. Treatment remains inferable from tool names and query surfaces, which stay visible because
they define evidence provenance; the review is not treatment-blind. The non-roster models
`claude-sonnet-5` and `deepseek-v4-flash` review every candidate independently; both must accept it
for automatic passage, and disagreement requires a human decision under the same rubric.
Deterministic hard-gate failures are not eligible for adjudication. Deterministic case effects and
primary statistics remain the headline result; adjudicated results are published separately as a
sensitivity analysis.

The fixed Discovery and Graph reference cohort contributes 160 cells: eight cases, five models, two
treatments, and two repetitions. It must be rerun from the same v32 suite fixture because the agent
surface and model roster are part of the protocol:

```bash
uv run semantic-rca formal-suite-micro-preflight \
  --greptimedb-repo /path/to/greptimedb \
  --run-root .instances/formal-suite-v32-preflight \
  --source-audits-dir .reports/formal-suite-v32-source-audits \
  --output .reports/formal-suite-v32-micro.json

# Paid API boundary:
uv run semantic-rca formal-suite-micro-run \
  --greptimedb-repo /path/to/greptimedb \
  --run-root .instances/formal-suite-v32-paid \
  --live-audits-dir .reports/formal-suite-v32-live-audits \
  --report .reports/formal-suite-v32-micro.json \
  --confirm-paid-api
```

The complete report contains 360 cells: 160 fixed-cohort micro cells and 200 fresh end-to-end
transfer cells. Repetitions describe model variability; the ten transfer cases are the independent
units for the transfer estimate. Primary transfer metrics are Graph-minus-Raw rows returned and
correct-completion tool calls, reduced to a median within each case before cross-case summaries.
The fixed Holm family contains two metrics for each of five models. Null means not estimable, a zero
call delta means no observed step reduction, and non-significance is not equivalence. A
direction-consistent result that does not pass Holm is reported descriptively with its directional
case count, eligible case count, case median, unadjusted p-value, and Holm-adjusted p-value.

After both schedules complete, export sanitized artifacts and generate the combined report:

```bash
uv run semantic-rca formal-suite-micro-export \
  --run-report .reports/formal-suite-v32-micro.json \
  --output artifacts/measurement/semantic-rca-v32-micro.json

uv run semantic-rca transfer-export \
  --run-report .reports/openrca2-transfer-v32-measurement.json \
  --adjudication .reports/openrca2-transfer-v32-adjudication-resolution.json \
  --output artifacts/measurement/openrca2-transfer-v32-five-model.json

uv run semantic-rca formal-suite-report \
  --micro-artifact artifacts/measurement/semantic-rca-v32-micro.json \
  --transfer-artifact artifacts/measurement/openrca2-transfer-v32-five-model.json \
  --output-json artifacts/measurement/semantic-rca-v32-report.json \
  --output-html artifacts/measurement/semantic-rca-v32-report.html
```

The transfer artifact retains every tool input, result row count, truncation flag, result hash,
per-call database load, citation-to-call resolution, deterministic scope/facts/claim verdicts,
per-claim rejection codes, locus projection, treatment-specific eligibility, and case-level effect.
It excludes provider responses, reasoning text, provider and query IDs, free-form
claims and explanations, adjudicator rationales, raw telemetry rows, local paths, endpoints, and
provider error text. The
tagged source deterministically revalidates every public citation, hard-gate result, primary metric
calculation, aggregate, adjudication binding, and artifact hash without rerunning an investigating
model. Published structured judge decisions are replayed rather than regenerated.

Aegis v24-v31 and the earlier one-case six-model run are historical development experiments. Their
JSON records may remain in the repository, but current code intentionally provides no loader,
scorer, resume, export, or renderer compatibility for those internal protocols.

Since protocol v24, end-to-end RCA counts a citation as execution-valid only when
it uniquely identifies a successful, non-truncated SQL or Graph query result,
the result carries the same query ID, and the evidence claim is non-empty.
Catalog and schema discovery are not incident evidence. This check prevents
failed or fabricated references from unlocking efficiency metrics, but it does
not prove that the cited rows support the diagnosis. The current transfer scorer
adds deterministic source-derived evidence-support predicates. Each citation
also declares structured claim types as an auditable annotation; the scorer infers actual support
from the executed query scope and returned facts. It evaluates causal-locus coverage, mechanism coverage, referential integrity,
and execution reliability separately. The generic RCA scorer still does not
infer evidence entailment.

Token fields are runner-specific. The API runner sums provider usage over all
responses; `run.usage.input_tokens` is total provider-visible input. Anthropic
totals are reconstructed from input, cache-creation, and cache-read fields. Raw
response usage retains the cache breakdown for billing. Anthropic API requests
enable automatic 5-minute prompt caching. DeepSeek context caching is enabled by
the provider and requires no request flag; its Anthropic-compatible endpoint
ignores `cache_control`. OpenAI Responses requests use implicit 30-minute caching
and persist `cached_tokens` and `cache_write_tokens` in raw usage. The runner
normalizes `reasoning_tokens` and `thinking_tokens` as a subset of
provider-reported output tokens when a provider returns that breakdown; cost and
total-token calculations do not add them again. Providers without a separate
breakdown still include reasoning in output tokens. Codex reads exactly one cumulative
`turn.completed` event and persists input including cache without a cache
breakdown. Claude adds input, cache creation, and cache reads into
`input_tokens`. Provider context includes system prompts, tool schemas, prior
tool or MCP results, and structured output, but no runner exposes a comparable
component-level split. Compare token deltas only within the same model, runner,
protocol, and case. API cost rendering uses separate cache-read pricing when raw
usage provides that split.

The implemented `discovery-audit` command checks the frozen evidence query and
catalog top-five gate without calling a model. `discovery-run` is a separate,
model-invoking command for the paired Raw/Semantic Graph task. See
`DISCOVERY.md` for the exact fixtures, scoring contract, and commands.

The separate [`GRAPH.md`](GRAPH.md) protocol compares Raw with GreptimeDB Semantic Graph on
witnessed service-call retrieval. Its
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
