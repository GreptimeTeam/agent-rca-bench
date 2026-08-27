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

Telemetry is replayed through production ingestion protocols. The RCAEval,
RCA100, and OpenRCA adapters send metrics through Prometheus remote write v1,
traces through OpenTelemetry Protocol (OTLP) HTTP using `greptime_trace_v1`,
and logs through Loki push. RCA100 also sends Kubernetes events and alerts
through separate Loki tables. The adapters do not create telemetry tables,
attach semantic options, or ingest a dataset's reference topology.

## Development

```bash
uv sync --extra dev
uv run semantic-rca doctor \
  --greptimedb-repo /Users/dennis/programming/rust/greptimedb
```

Dataset downloads, prepared data, database files, trajectories, and reports are
kept under ignored local directories.

API credentials are read from the process environment or macOS Keychain. Never
put them in a tracked configuration file or command-line argument.

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

Protocol v11 gives the agent the selected dataset's fault taxonomy and
requires one canonical fault mechanism from that taxonomy. The evaluator scores
the mechanism and component with deterministic normalized equality. It records
the broader fault category separately. Confidence remains a diagnostic
calibration signal and does not contribute to the correctness score. The Graph
treatment has a dedicated query tool that applies the incident window and
deduplicates relationship observation windows before aggregating RED fields. It
also distinguishes external alert identifiers from canonical graph entity IDs
and tells the agent to discover graph endpoints before filtering by ID.

Table Semantics and Graph treatments also expose semantic catalog search. It
searches table names, semantic options, and entity declarations across the
incident database, then ranks candidates by matched concepts. This lets the
agent find relevant tables in a wide schema before calling `describe_table`.
When Graph coverage is `empty`, protocol v11 suppresses the graph query tool and
explicitly tells the agent not to infer topology.

The semantic treatment includes the metadata, tool schemas, basic usage guides,
and coverage snapshot that an MCP server would provide. The benchmark therefore
measures the complete agent-facing semantic interface, not a data-only
ablation. Semantic discovery and graph calls consume the same fixed cap as SQL
calls. Each run records requested calls and whether the cap rejected any call.

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

Import and validate RCA100 `t001` without calling a model:

```bash
uv run semantic-rca smoke-rca100 \
  --cache-dir .data/rca100 \
  --endpoint http://127.0.0.1:4000 \
  --database semantic_graph_rca100_t001 \
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
  --database semantic_openrca_bank_task6 \
  --case task_6@2021-03-04T18:00
```

The case selector combines OpenRCA's repeated task index with the official
UTC+8 window start. Its report records all protocol representation decisions,
including unknown trace duration units and the absence of graph-capable span
semantics. Source timestamps, labels, topology, and ground truth are never
repaired.

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

The current three-corpus pilot result and its limits are recorded in
[`RESULTS.md`](RESULTS.md).
