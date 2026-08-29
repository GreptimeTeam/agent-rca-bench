# Semantic RCA Bench

This project measures whether GreptimeDB Semantic Graph changes the efficiency
of an LLM agent performing root-cause analysis.

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

OpenAI models use `OPENAI_API_KEY` or the `semantic-rca-bench-openai` Keychain
service. The runner uses the Responses API with `store=false`, replays every
response output item between tool turns, and requests encrypted reasoning items
for stateless continuity. GPT-5.6 uses implicit 30-minute prefix caching, and
reports account for uncached input, cache writes, cache reads, and output
separately:

```bash
read -s "BENCH_KEY?OpenAI API key: "; echo
security add-generic-password -U -a "$USER" \
  -s semantic-rca-bench-openai -w "$BENCH_KEY"
unset BENCH_KEY
uv run semantic-rca run \
  --report .reports/smoke-<run-id>.json \
  --model gpt-5.6-sol \
  --api-transport openai-responses \
  --reasoning-effort medium \
  --max-output-tokens 16384 \
  --repetitions 2
```

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
and adds the OpenAI Responses API runner. The
agent establishes the failing operation, uses discriminating queries instead of unconditional
resource sweeps, and compares the same operation across the change point. The
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
taxonomy. The evaluator scores the mechanism by normalized equality. The diagnosis reports
the directly affected workload or infrastructure component separately from an
optional causal dependency. Component scoring is unavailable when a dataset
publishes conflicting structured component labels. A predicted causal
dependency remains diagnostic output; no dependency accuracy field exists
until a source publishes a canonical dependency label. Confidence remains a
diagnostic calibration signal and does not contribute to correctness.

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

Protocol v24 and v25 model runs are complete historical development runs. V25 executed all 27
three-model cells without runner errors or budget exhaustion, and one later Opus Graph diagnostic
also completed. Their scorer used server duration for a fault injected between client and server
start, required a hidden exact fault label, and constrained evidence to the canonical aggregate.
The resulting zero eligible pairs are not a model-quality or semantic-layer result. Historical
reports and the sanitized v25 artifact remain immutable records. The current runtime only accepts
v28 for new agent execution and does not rescore, resume, or export earlier development report,
scorer, or protocol schemas. A non-v28 execution request fails before contacting a provider.

Protocol v28 reuses the trajectory-blind v27 selection of a fresh, source-observable JVM exception
case for measurement. It compares Raw with the complete GreptimeDB Semantic Graph surface and adds
OpenAI Responses API support. The agent sees only `aegis-transfer-003`, an empty fault taxonomy,
and the incident windows. Source labels, the source case name, and `causal_graph.json` remain
outside the agent input and ingestion path.

Run the complete provider-free gate sequence before requesting paid execution:

```bash
uv run semantic-rca aegis-transfer-audit \
  --cases-dir .data/aegis/rcabench-platform-v2/data/rcabench \
  --meta-dir .data/aegis/rcabench-platform-v2/meta/rcabench \
  --archive .data/aegis/FSE_26_RCA_dataset_study_reviewer.tar.gz \
  --selection fixtures/reference/aegis-transfer-v27-selection.json \
  --run-dir .instances/aegis-transfer-v28-source-01 \
  --database case_03 \
  --output .reports/aegis-transfer-v28-source.json

uv run semantic-rca aegis-transfer-scorer-audit \
  --transfer-audit .reports/aegis-transfer-v28-source.json \
  --scorer fixtures/reference/aegis-transfer-v28-scorer.json \
  --output .reports/aegis-transfer-v28-scorer.json

uv run semantic-rca aegis-transfer-protocol-audit \
  --source-audit .reports/aegis-transfer-v28-source.json \
  --scorer-audit .reports/aegis-transfer-v28-scorer.json \
  --scorer fixtures/reference/aegis-transfer-v28-scorer.json \
  --protocol fixtures/reference/aegis-transfer-v28-four-model-protocol.json \
  --output .reports/aegis-transfer-v28-protocol.json

uv run semantic-rca aegis-transfer-formal-preflight \
  --source-audit .reports/aegis-transfer-v28-source.json \
  --scorer-audit .reports/aegis-transfer-v28-scorer.json \
  --protocol-audit .reports/aegis-transfer-v28-protocol.json \
  --scorer fixtures/reference/aegis-transfer-v28-scorer.json \
  --protocol fixtures/reference/aegis-transfer-v28-four-model-protocol.json \
  --output .reports/aegis-transfer-v28-formal.json
```

The source oracle requires zero `retrieveByName` Error spans and zero exception logs during the
normal window, followed by repeated observations of both signals during the abnormal window. The
stored aggregate is 0/0 to 1,981/1,981. The case has one source-labeled component and no declared
dependency edge; the loader and scorer do not invent one.

The Graph equality audit derives its comparison strategy from source boundary counts. A boundary
that can be represented by `observed_at` minute bins must pass normal and abnormal raw/Graph exact
equality separately and also pass the combined-window comparison. If both periods contain clients
in the same minute, separate Graph periods are not representable; the audit records that fact and
requires exact equality over the contiguous union. The selected source follows the latter path.
Its normalized union contains 40 edges and has the same hash on both sides. Two independent
ingestions produce the same source-semantic hash.

The scorer accepts a combined aggregate, separate trace and log aggregates, or complete raw rows.
It requires the source service, operation, Error status, exception log predicate, and complete
half-open windows. It rejects identity cherry-picking, truncated results, `LIMIT`, hard-coded
aggregate aliases, predicates neutralized by `OR` or `NOT`, and required predicates placed only in
an unrelated nested query. Aggregate period labels must be derived from the source timestamp; time
literals in projections do not substitute for exact positive window filters. Ingestion preserves
the exact source values. The no-model audit also proves that ASCII case normalization is collision
free for service identity and the stored OTel role/status domains, so the scorer accepts equivalent
`LOWER` or `UPPER` predicates without accepting a broader identity or enum set.

Protocol v28 reports `diagnosis_correct`, `required_evidence_covered`, `citation_integrity`,
`execution_reliability`, `auditable_completion`, and `efficiency_eligible` separately. The primary
efficiency comparison requires correct structured diagnosis, required claim coverage, and reliable
execution. An unrelated invalid extra citation blocks auditable completion without changing
diagnosis correctness or erasing valid required evidence. Every citation used to cover a required
claim must still be execution-valid. Free-text `fault_type` is explanatory; the scored mechanism is
the global, case-independent `mechanism_code`.

The formal fixture freezes `deepseek-v4-pro`, `claude-sonnet-5`, `claude-opus-4-8`, and
`gpt-5.6-sol`. Each model runs two position-balanced repetitions over Raw and GreptimeDB Semantic
Graph, for 16 cells. DeepSeek uses provider-managed prefix caching. Claude uses ephemeral request
cache control. OpenAI uses implicit 30-minute prefix caching through the Responses API, freezes
reasoning effort at `medium`, and uses a 16,384-token combined reasoning-and-visible-output budget.
Preflight records zero completed cells and does not read credentials, start GreptimeDB, or call a
provider.

**Warning:** The next command calls a paid API. The manifest schedules OpenAI first, and
`--max-new-runs 1` limits this invocation to one cell so output-budget and transport behavior can
be inspected before separately approving the remaining cells.

```bash
uv run semantic-rca aegis-transfer-formal-run \
  --cases-dir .data/aegis/rcabench-platform-v2/data/rcabench \
  --meta-dir .data/aegis/rcabench-platform-v2/meta/rcabench \
  --archive .data/aegis/FSE_26_RCA_dataset_study_reviewer.tar.gz \
  --selection fixtures/reference/aegis-transfer-v27-selection.json \
  --run-dir .instances/aegis-transfer-v28-paid-01 \
  --database case_03 \
  --report .reports/aegis-transfer-v28-formal.json \
  --source-audit-output .reports/aegis-transfer-v28-paid-01-source.json \
  --scorer-audit-output .reports/aegis-transfer-v28-paid-01-scorer.json \
  --protocol-audit-output .reports/aegis-transfer-v28-paid-01-protocol.json \
  --scorer fixtures/reference/aegis-transfer-v28-scorer.json \
  --protocol fixtures/reference/aegis-transfer-v28-four-model-protocol.json \
  --max-new-runs 1 \
  --confirm-paid-api
```

The runner atomically persists every completed cell and resumes only an exact schedule prefix.
An intentionally bounded invocation returns success when its requested cells complete without a
runner error or budget exhaustion, even though the full schedule remains incomplete. Resume the
same report only after separate approval, omit `--max-new-runs`, and use a new empty run directory
and new audit-output paths.
Runner errors and budget exhaustion remain scored cell outcomes and do not stop the rest of the
batch. A roster, scheduled-model, or tool-budget contract violation is a harness failure and stops
the invocation after recording the cell. Resume with the same report, a new empty run directory,
new audit output paths, and a new explicit approval. The preflight binds the pricing snapshot by
content so a later pricing-table update cannot invalidate an in-progress report or alter its
exported cost estimate.

After all 16 cells complete, export the sanitized measurement artifact without a provider or
database connection:

```bash
uv run semantic-rca aegis-transfer-measurement-export \
  --run-report .reports/aegis-transfer-v28-formal.json \
  --source-audit .reports/aegis-transfer-v28-paid-01-source.json \
  --scorer-audit .reports/aegis-transfer-v28-paid-01-scorer.json \
  --protocol-audit .reports/aegis-transfer-v28-paid-01-protocol.json \
  --scorer fixtures/reference/aegis-transfer-v28-scorer.json \
  --protocol fixtures/reference/aegis-transfer-v28-four-model-protocol.json \
  --output artifacts/measurement/aegis-transfer-v28-four-model.json
```

The exporter deterministically rescores every cell. It excludes provider responses, free-form
explanations, evidence claim text, source rows, query IDs, timings, local paths, and process data.
For mechanism queries, it publishes only SQL, columns, row counts, truncation state, and a result
hash. It reports paired efficiency deltas within each model and never pools correctness across
models.

Since protocol v24, end-to-end RCA counts a citation as execution-valid only when
it uniquely identifies a successful, non-truncated SQL or Graph query result,
the result carries the same query ID, and the evidence claim is non-empty.
Catalog and schema discovery are not incident evidence. This check prevents
failed or fabricated references from unlocking efficiency metrics, but it does
not prove that the cited rows support the diagnosis. The Aegis transfer scorer
adds a source-specific deterministic evidence-support predicate. Protocol v28
also requires each citation to declare the structured claim types it supports,
then evaluates causal-scope coverage, mechanism coverage, referential integrity,
and execution reliability separately. The generic RCA scorer still does not
infer evidence entailment.

Token fields are runner-specific. The API runner sums provider usage over all
responses; `run.usage.input_tokens` excludes cache creation and cache reads.
Raw response usage keeps those fields so reports reconstruct total model-visible
input and billing. Anthropic API requests enable automatic
5-minute prompt caching. DeepSeek context caching is enabled by the provider and
requires no request flag; its Anthropic-compatible endpoint ignores
`cache_control`. OpenAI Responses requests use implicit 30-minute caching and
persist `cached_tokens` and `cache_write_tokens` in raw usage. OpenAI reasoning tokens are recorded
as a subset of provider-reported output tokens; cost and total-token calculations do not add them
again. Codex reads exactly one cumulative
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
