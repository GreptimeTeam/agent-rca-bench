# Run source audits and model measurements

This guide covers source replay, Semantic Graph preflight, credentials, paid
model execution, and the public artifact contract. To reproduce the published
report without source telemetry or model calls, use the procedure in
[README.md](README.md#reproduce-the-published-report).

## Prepare the measurement environment

The source and model workflows require:

- Python 3.11 and [`uv`](https://docs.astral.sh/uv/);
- a GreptimeDB checkout at the protocol revision, built in release mode;
- Docker for the digest-pinned Prometheus, Loki, and Tempo images used by
  `split_pillars`;
- access to the source datasets under their upstream terms;
- provider credentials for model measurements.

Install the project:

```bash
git clone https://github.com/GreptimeTeam/agent-rca-bench.git
cd agent-rca-bench
uv sync --extra dev --frozen
```

Check the GreptimeDB and container environment:

```bash
uv run agent-rca doctor --greptimedb-repo /path/to/greptimedb
```

Each formal case starts an exclusive GreptimeDB process and an exclusive split
stack on loopback ports. The runner removes only the processes and containers it
started. Review [DATASETS.md](DATASETS.md) before downloading source files.

## Run source and Semantic Graph audits

The source audit reads pinned source files and replays cases in batches of up to
four. At most two environments are prepared concurrently. Every environment in
a batch must pass its provider-free gates before the batch is released.

Run the frozen selection audit:

```bash
uv run agent-rca transfer-selection-audit \
  --output .reports/openrca2-transfer-selection-audit.json
```

Run the source and storage preflight:

```bash
uv run agent-rca transfer-preflight \
  --greptimedb-repo /path/to/greptimedb \
  --run-root .instances/openrca2-transfer-preflight \
  --output .reports/openrca2-transfer-measurement.json
```

The preflight fails on source or fixture drift, protocol rejection, identity or
span-ID remapping, stored row-count drift, reference causal-graph ingestion, a
non-empty Semantic Graph instance, or any raw-span/Graph edge mismatch. It also
recomputes each frozen mechanism predicate from stored telemetry. Run
directories remain available for inspection.

## Configure provider credentials

The runner reads credentials from environment variables or macOS Keychain:

| Provider | Environment variable | Keychain service |
| --- | --- | --- |
| OpenAI | `OPENAI_API_KEY` | `agent-rca-bench-openai` |
| Anthropic | `ANTHROPIC_API_KEY` | `agent-rca-bench-anthropic` |
| DeepSeek | `DEEPSEEK_API_KEY` | `agent-rca-bench-deepseek` |
| BigModel | `BIGMODEL_API_KEY` | `agent-rca-bench-bigmodel` |
| DashScope | `DASHSCOPE_API_KEY` | `agent-rca-bench-dashscope` |
| Gemini | `GEMINI_API_KEY` | `agent-rca-bench-gemini` |

DashScope also requires a caller-owned Beijing workspace Responses endpoint in
`DASHSCOPE_BASE_URL` or the `agent-rca-bench-dashscope-base-url` Keychain
service. Tenant hostnames remain local runtime configuration.

On macOS, add a key without placing it in shell history:

```bash
security add-generic-password -U -a "$USER" \
  -s agent-rca-bench-openai -w
```

Replace the Keychain service with the service for the provider being configured.

## Run model measurements

**Warning:** The `*-run` commands call paid model APIs. Review the active
fixtures, provider-free audits, expected schedule, and frozen pricing before
passing `--confirm-paid-api`.

Run pending end-to-end cells with the concurrency limits frozen in the transfer
protocol:

```bash
uv run agent-rca transfer-run \
  --greptimedb-repo /path/to/greptimedb \
  --report .reports/openrca2-transfer-measurement.json \
  --run-root .instances/openrca2-transfer-measurement \
  --confirm-paid-api
```

Cases run in batches of up to four, with at most two environments prepared
concurrently. Every environment in a batch is ready before any model call, so
ingestion does not overlap measured queries. By default, the runner limits each
provider to two active cells, journals a cell before calling its provider, and records the
result before merging it into the report.

`--provider-concurrency-limit N` sets the provider scheduling limit for pending
cells. The value must be between one and the frozen per-provider maximum. The runner
records the setting in private execution records. Cells within a case remain
serial; model configurations and per-cell budgets are unchanged.

The exporter retains recorded concurrency overrides, including historical
deviations above the frozen limit. Such reports can be audited and exported but
cannot be resumed as protocol-conforming runs. Increasing the frozen maximum
requires a new protocol.

A run-root lock rejects a second runner invocation. If an invocation stops with
an active cell and no recorded result, the next invocation refuses to retry that
cell automatically. A runner failure persists as a failed cell; it does not
fall back to another provider or abort the complete schedule.

Run `uv run agent-rca --help` for the micro-benchmark, export, and render
commands.

## Public artifact contract

The public micro and transfer artifacts retain the information required to
recompute scoring and aggregate results:

- sanitized tool inputs and result projections;
- execution status, row counts, truncation, and result hashes;
- citation resolution and deterministic claim verdicts;
- protocol, selection, source, and GreptimeDB bindings;
- model usage and frozen pricing fields;
- case-level paired effects and integrity hashes.

They exclude provider payloads, reasoning text, free-form diagnosis
explanations, raw telemetry rows, credentials, endpoints, query IDs, and local
paths. Re-running a provider is a replication. Deterministic rescoring from the
published artifacts is the report-reproduction contract.
