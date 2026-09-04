# Agent RCA Bench

[![CI](https://github.com/GreptimeTeam/agent-rca-bench/actions/workflows/ci.yml/badge.svg)](https://github.com/GreptimeTeam/agent-rca-bench/actions/workflows/ci.yml)
[![Report](https://img.shields.io/badge/report-2026-0c7259)](https://rca-bench.greptime.com)
[![Python](https://img.shields.io/badge/python-3.11-3776ab)](https://www.python.org/downloads/release/python-3110/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Agent RCA Bench measures how the observability interface behind a large
language model agent changes root cause analysis (RCA). It compares the native
Prometheus, Loki, and Tempo interface bundle with GreptimeDB's all-in-one query
surface, then compares raw GreptimeDB telemetry with the complete GreptimeDB
Semantic Graph interface. Each paired comparison holds the model, incident,
prompt, runner, telemetry, and resource budget constant.

Read the [interactive report](https://rca-bench.greptime.com), the
[English report](REPORT.md), or the [Chinese report](REPORT.zh-CN.md).

## Results

Protocol v34 produced one confirmatory result. With `claude-fable-5-1`, the
GreptimeDB all-in-one interface used fewer provider-visible input tokens than
the Prometheus, Loki, and Tempo interface bundle in all 13 eligible cases. The
case-median difference was `-446,252.5` tokens (Holm-adjusted p `0.001953125`).

No endpoint in the `semantic_graph - raw` family passed Holm correction. The
Semantic Graph reduced rows in every eligible Discovery result (`23/23`) and
Graph-retrieval result (`8/8`), but the end-to-end case medians point in both
directions.

Across 112 runs per treatment, Split, Raw, and Graph produced 60, 80, and 77
correct diagnoses. These totals are descriptive. The direction also changes
between the OpenRCA2 service cases and the RCA-100 infrastructure-node cases.
[REPORT.md](REPORT.md) has the complete results and power limits.

## What the benchmark measures

The suite has three layers:

| Layer | Task | Purpose |
| --- | --- | --- |
| Discovery | Find the table and signal that carry incident evidence | Isolate schema and signal discovery |
| Graph retrieval | Find and verify the relevant service dependency | Isolate topology navigation |
| End-to-end RCA | Locate the fault, diagnose the mechanism, and cite evidence | Measure the complete investigation |

| Treatment | Agent interface |
| --- | --- |
| `split_pillars` | Native Prometheus, Loki, and Tempo query APIs |
| `raw` | Read-only SQL and PromQL over GreptimeDB telemetry tables |
| `semantic_graph` | The Raw interface plus table semantics, entities, relationships, coverage, and Semantic Graph query tools |

The shared system prompt does not contain case-specific query recipes or hidden
answers. Ground truth, injection metadata, publisher causal graphs, and
label-bearing case names are excluded from the agent surface.

The measurement contains 128 fixed-cohort micro-benchmark cells and 336
end-to-end cells over ten OpenRCA2 and four RCA-100 incidents. It covers four
model configurations and two repetitions per model, case, and treatment.

Each paired comparison has its own two efficiency metrics, specified and frozen
in the protocol before any run:

| Comparison | Metrics |
| --- | --- |
| `semantic_graph - raw` | rows returned, complete-run tool calls |
| `raw - split_pillars` | provider-visible input tokens, complete-run tool calls |

Rows returned does not apply across the split stack and reports N/A there. Cache
use, output, reasoning, and cost are reported separately and are descriptive.

## Reproduce the published report

Report reproduction uses Python 3.11 and [`uv`](https://docs.astral.sh/uv/). It
does not call a model provider, download source telemetry, or require Docker or
a GreptimeDB build.

```bash
git clone https://github.com/GreptimeTeam/agent-rca-bench.git
cd agent-rca-bench
uv sync --extra dev --frozen
```

From the v34 release tree, validate the sanitized artifacts, recompute the
combined report, and render the self-contained HTML report:

```bash
output_dir=$(mktemp -d)

uv run agent-rca formal-suite-report \
  --micro-artifact artifacts/measurement/agent-rca-v34-micro.json \
  --transfer-artifact artifacts/measurement/agent-rca-v34-transfer.json \
  --suite-protocol fixtures/reference/agent-rca-v34-four-model-suite.json \
  --transfer-protocol fixtures/reference/transfer-v34-protocol.json \
  --output-json "$output_dir/agent-rca.json" \
  --output-html "$output_dir/agent-rca.html"

cmp artifacts/measurement/agent-rca-v34.json \
  "$output_dir/agent-rca.json"
cmp artifacts/measurement/agent-rca-v34.html \
  "$output_dir/agent-rca.html"
```

Both `cmp` commands exit with status 0 when the release reproduces byte for
byte. Run the complete provider-free validation before publishing a change:

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv lock --check
uv build
shasum -a 256 -c artifacts/measurement/agent-rca-v34-SHA256SUMS
```

Source replay, Semantic Graph preflight, credential setup, paid model execution,
and the public artifact contract are documented in [RUNNING.md](RUNNING.md).

## Documentation

| Document | Scope |
| --- | --- |
| [REPORT.md](REPORT.md) | Canonical measurement report |
| [REPORT.zh-CN.md](REPORT.zh-CN.md) | Maintained Chinese report |
| [RUNNING.md](RUNNING.md) | Source audits and model execution |
| [DATASETS.md](DATASETS.md) | Dataset provenance, selection, attribution, and license statements |
| [SCORING.md](SCORING.md) | Diagnosis, evidence, and eligibility rules |
| [DISCOVERY.md](DISCOVERY.md) | Schema-discovery micro-benchmark |
| [GRAPH.md](GRAPH.md) | Dependency-retrieval micro-benchmark |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development and review workflow |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [CITATION.cff](CITATION.cff) | Citation metadata for the benchmark and source datasets |

Raw telemetry, source archives, provider responses, reasoning payloads,
credentials, and private trajectories are not part of the repository.

## License and data terms

The benchmark's original code, artifact schemas, report text, and independently
derived aggregates are licensed under [Apache-2.0](LICENSE). This license does
not cover or relicense upstream data or upstream dataset documentation.

The repository publishes sanitized identifiers, derived facts, aggregate
measurements, and source hashes. It does not publish source telemetry rows,
archives, topology, causal graphs, or ground-truth files. OpenRCA 1.0 and RCA100
retain their CC BY-NC 4.0 and CC BY-NC-SA 4.0 terms, respectively. OpenRCA2's
dataset card declares Apache-2.0, while its paper declares CC-BY-SA-4.0 and the
downloaded artifact is not the archival release described by the paper.
[DATASETS.md](DATASETS.md) records the source scope, transformations,
attribution, provenance, and license statements.

Greptime sponsors and maintains this benchmark. The report discloses that
relationship.
