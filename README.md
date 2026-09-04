# Semantic RCA Bench

[![CI](https://github.com/GreptimeTeam/semantic-rca-bench/actions/workflows/ci.yml/badge.svg)](https://github.com/GreptimeTeam/semantic-rca-bench/actions/workflows/ci.yml)
[![Report](https://img.shields.io/badge/report-2026-0c7259)](https://semantic-rca.greptime.com)
[![Python](https://img.shields.io/badge/python-3.11-3776ab)](https://www.python.org/downloads/release/python-3110/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Semantic RCA Bench measures how the observability interface behind a large
language model agent changes root cause analysis (RCA). It compares the native
Prometheus, Loki, and Tempo interface bundle with GreptimeDB's all-in-one query
surface, then compares raw GreptimeDB telemetry with the complete GreptimeDB
Semantic Graph interface. Each paired comparison holds the model, incident,
prompt, runner, telemetry, and resource budget constant.

Read the [interactive report](https://semantic-rca.greptime.com), the
[English report](REPORT.md), or the [Chinese report](REPORT.zh-CN.md).
[CHANGELOG.md](CHANGELOG.md) records what each release measured, and
[CONTRIBUTING.md](CONTRIBUTING.md) covers how to work on it.

## Results in one paragraph

The interface bundle mattered more than the semantic layer. Over 112 runs per
treatment the three-backend bundle produced 60 correct diagnoses against
GreptimeDB's 80, cost 2.21x as much, and made the models read 1.54x the tokens;
one endpoint in that family survived Holm correction, `claude-fable-5-1` reading
fewer provider-visible input tokens under GreptimeDB in all 13 eligible cases
(case median -446,252.5, Holm p 0.00195). The Semantic Graph compressed focused
retrieval on every eligible micro case but moved no end-to-end endpoint: 0 of 8
passed correction and the case medians point both ways, which is insufficient
evidence rather than evidence of no effect. Graph's diagnosis totals also
reverse between the OpenRCA2 service cases and the RCA100 infrastructure-node
cases. See [REPORT.md](REPORT.md) for the complete results, the power limit, and
what the cohort cannot support.

## What the benchmark measures

The suite has three layers:

| Layer | Task | Purpose |
| --- | --- | --- |
| Discovery | Find the table and signal that carry incident evidence | Isolate schema and signal discovery |
| Graph retrieval | Find and verify the relevant service dependency | Isolate topology navigation |
| End-to-end RCA | Locate the fault, diagnose the mechanism, and cite evidence | Measure the complete investigation |

The micro-benchmarks compare two treatments:

- `raw`: production SQL over the ingested metric, log, and trace tables.
- `semantic_graph`: the same SQL surface plus table semantics, entities,
  relationships, coverage, and Semantic Graph query tools.

The end-to-end benchmark adds a third treatment:

- `split_pillars`: Prometheus, Loki, and Tempo through their native query APIs.

The shared system prompt does not contain case-specific query recipes or hidden
answers. Ground truth, injection metadata, publisher causal graphs, and
label-bearing case names are excluded from the agent surface.

The measurement contains 464 completed agent cells:

- 128 fixed-cohort micro-benchmark cells;
- 336 end-to-end cells over ten OpenRCA2 and four RCA100 incidents;
- four model configurations;
- two repetitions per model, case, and treatment.

Each paired comparison has its own two efficiency metrics, specified and frozen
in the protocol before any run:

| Comparison | Metrics |
| --- | --- |
| `semantic_graph - raw` | rows returned, complete-run tool calls |
| `raw - split_pillars` | provider-visible input tokens, complete-run tool calls |

Rows returned does not apply across the split stack and reports N/A there. Cache
use, output, reasoning, and cost are reported separately and are descriptive.

### Published protocol

The published report uses benchmark protocol v34. Its third end-to-end
treatment, `split_pillars`, replays the same case into Prometheus, Loki, and
Tempo and gives the agent those stores' native query APIs. The schedule is 14
cases × 4 models × 3 treatments × 2 repetitions = 336 end-to-end cells.

v34 answers two paired questions instead of one, each corrected on its own:
`semantic_graph - raw` for the semantic layer, and `raw - split_pillars` for the
all-in-one interface against a native interface bundle. Model ranking is
reported descriptively. `rows_returned` does not apply to `split_pillars` and is
reported as N/A there; the cross-stack endpoints are provider-visible input
tokens and complete-run tool calls.

See [SCORING.md](SCORING.md) for the scoring contract,
[DATASETS.md](DATASETS.md) for provenance and selection, [DISCOVERY.md](DISCOVERY.md)
for schema discovery, and [GRAPH.md](GRAPH.md) for Graph retrieval and exact-edge
validation.

## Repository layout

```text
artifacts/measurement/   Sanitized public measurement artifacts
fixtures/measurement/    Fixed micro-benchmark case fixtures and selection manifests
fixtures/reference/      Active source-selection and formal protocol fixtures
src/semantic_rca_bench/  Adapters, runners, scorers, exporters, and report code
tests/                   Protocol and regression tests
REPORT.md                Canonical English report
REPORT.zh-CN.md          Maintained Chinese translation
```

Raw telemetry, source archives, provider responses, reasoning payloads,
credentials, and private trajectories are not part of the repository.

## Install

Use Python 3.11 and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/GreptimeTeam/semantic-rca-bench.git
cd semantic-rca-bench
uv sync --extra dev --frozen
uv run semantic-rca doctor
```

Reproducing the published report needs neither Docker nor a GreptimeDB build.
Running the provider measurement needs both: a GreptimeDB checkout at the
protocol revision built in release mode, and Docker for the digest-pinned
Prometheus, Loki, and Tempo images that back the `split_pillars` treatment. Each
case starts its own four stores on loopback ports and removes them afterwards.

Build the wheel and source distribution with:

```bash
uv build
```

## Reproduce the published report

Report reproduction does not call a model provider or require source telemetry.
It validates both sanitized source artifacts, recomputes the combined report,
and renders a self-contained HTML file.

Run the command from the v34 release tree. The command rejects artifacts whose
protocol or source bindings differ from the checked-out fixtures.

```bash
output_dir=$(mktemp -d)

uv run semantic-rca formal-suite-report \
  --micro-artifact artifacts/measurement/semantic-rca-v34-micro.json \
  --transfer-artifact artifacts/measurement/semantic-rca-v34-transfer.json \
  --suite-protocol fixtures/reference/semantic-rca-v34-four-model-suite.json \
  --transfer-protocol fixtures/reference/transfer-v34-protocol.json \
  --output-json "$output_dir/semantic-rca.json" \
  --output-html "$output_dir/semantic-rca.html"

cmp artifacts/measurement/semantic-rca-v34.json \
  "$output_dir/semantic-rca.json"
cmp artifacts/measurement/semantic-rca-v34.html \
  "$output_dir/semantic-rca.html"
```

Run the complete provider-free repository validation with:

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv lock --check
uv build
shasum -a 256 -c artifacts/measurement/semantic-rca-v34-SHA256SUMS
```

## Reproduce source and Semantic Graph audits

The end-to-end source audit reads the pinned source artifacts and replays cases
in batches of up to four. Each case has an exclusive release-mode GreptimeDB
process and split stack. At most two environments are prepared concurrently;
all environments in a batch must pass their provider-free gates before that
batch is released. The command stops only the processes and containers it
started and retains the run directories for inspection.

```bash
uv run semantic-rca transfer-selection-audit \
  --output .reports/openrca2-transfer-selection-audit.json

uv run semantic-rca transfer-preflight \
  --greptimedb-repo /path/to/greptimedb \
  --run-root .instances/openrca2-transfer-preflight \
  --output .reports/openrca2-transfer-preflight.json
```

The preflight fails on source or fixture drift, protocol rejection, identity or
span-ID remapping, stored row-count drift, reference causal-graph ingestion, a
non-empty Semantic Graph instance, or any raw-span/Graph edge mismatch. It also
recomputes each frozen mechanism predicate from stored telemetry.

The audit does not redistribute upstream telemetry. Review
[DATASETS.md](DATASETS.md) before downloading a dataset.

## Run model measurements

The `*-run` commands call paid APIs. Review the active fixtures, provider-free
audits, expected schedule, and pricing before passing `--confirm-paid-api`.

The runner reads credentials from environment variables or macOS Keychain:

| Provider | Environment variable | Keychain service |
| --- | --- | --- |
| OpenAI | `OPENAI_API_KEY` | `semantic-rca-bench-openai` |
| Anthropic | `ANTHROPIC_API_KEY` | `semantic-rca-bench-anthropic` |
| DeepSeek | `DEEPSEEK_API_KEY` | `semantic-rca-bench-deepseek` |
| BigModel | `BIGMODEL_API_KEY` | `semantic-rca-bench-bigmodel` |
| DashScope | `DASHSCOPE_API_KEY` | `semantic-rca-bench-dashscope` |

DashScope also requires a caller-owned Beijing workspace Responses endpoint in
`DASHSCOPE_BASE_URL` or the `semantic-rca-bench-dashscope-base-url` Keychain
service. Tenant hostnames remain local runtime configuration.

Add a key without placing it in shell history:

```bash
security add-generic-password -U -a "$USER" \
  -s semantic-rca-bench-openai -w
```

Run pending end-to-end cells with the concurrency limits frozen in the transfer
protocol:

```bash
uv run semantic-rca transfer-run \
  --report .reports/openrca2-transfer-measurement.json \
  --run-root .instances/openrca2-transfer-measurement \
  --confirm-paid-api
```

Each active case receives an exclusive GreptimeDB process, split stack, port
set, and data directory under `--run-root`. Cases run in batches of up to four,
with at most two environments prepared concurrently. Every environment in a
batch is ready before any model call, so ingestion does not overlap measured
queries. The runner limits each provider to two active cells, journals a cell
before calling its provider, and records the result before merging it into the
report. A run-root lock rejects a second runner invocation. If an invocation
stops with an active cell and no recorded result, the next invocation refuses
to retry that cell automatically.

Run `uv run semantic-rca --help` for the micro-benchmark, export, and
render commands. A runner failure is persisted as a failed cell. It does not
silently fall back to another provider or abort the complete schedule.

## Artifact contract

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
paths. Re-running a provider is a replication; deterministic rescoring from the
published artifacts is the report-reproduction contract.

## License and data terms

The benchmark code, report schema, and derived report are licensed under
[Apache-2.0](LICENSE). Upstream datasets keep their own terms and are not
relicensed by this repository.

OpenRCA2's dataset card declares Apache-2.0, while its paper declares
CC-BY-SA-4.0 and the downloaded artifact is not the archival release described
by the paper. The repository therefore publishes derived, sanitized facts and
hashes but not source telemetry. [DATASETS.md](DATASETS.md) records the complete
provenance and license audit.

## Project governance

Greptime sponsors and maintains this benchmark. The report states that interest
directly. The protocol is designed to report negative results and applicability
limits, not to guarantee a Semantic Graph improvement.

Changes to the prompt, treatment surface, scorer, selection, runner, or primary
metrics require a new protocol identifier. Historical internal protocols are
available from Git history rather than compatibility code in `main`.
