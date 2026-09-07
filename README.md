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

Read the six-model [English report](REPORT.md) or [Chinese report](REPORT.zh-CN.md).
The [interactive report](artifacts/measurement/agent-rca-v34-six-model.html) compares all six models.

## Results

Protocol v34 supports two model-specific input reductions. With `claude-fable-5-1`, the
GreptimeDB all-in-one interface used fewer provider-visible input tokens than
the Prometheus, Loki, and Tempo interface bundle in all 13 eligible cases. The
case-median difference was `-446,252.5` tokens (exact p `0.000244140625`, Holm p `0.001953125`).
Gemini 3.8 Flash reduced input in 10 of 11 eligible cases, with a median difference of
`-1,575,917` tokens (exact p `0.01171875`, Holm p `0.046875`). These were the only passing
endpoints: two of twelve passed their frozen Holm tests.

No endpoint in the `semantic_graph - raw` family passed Holm correction. The
Semantic Graph reduced rows in every eligible Discovery result (`35/35`), and both rows
and calls in 11/12 Graph-retrieval results. One Qwen case increased both. End-to-end
case medians point in both directions.

Across 168 runs per treatment, Split, Raw, and Graph produced 97, 130, and 124
correct diagnoses. These totals are descriptive. The direction also changes
between the OpenRCA2 service cases and the RCA-100 infrastructure-node cases; this
post-measurement split confounds dataset with fault level.
Across all 28 runs per arm, Raw reduced Gemini input by 54.35% and Qwen input by 22.06%.
Qwen's estimated Raw cost was 25.52% lower than Split. Gemini's displayed end-to-end
cost uses ordinary input rates without cache discounts because its cache breakdown is incomplete.
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

The measurement combines 192 fixed-cohort micro-benchmark cells and 504
end-to-end cells over ten OpenRCA2 and four RCA-100 incidents. It covers six
model configurations and two repetitions per model, case, and treatment. The
artifacts bind the model configurations and protocols by hash.

Each paired comparison has its own two efficiency metrics, specified and frozen
in the protocol before any run:

| Comparison | Metrics |
| --- | --- |
| `semantic_graph - raw` | rows returned, complete-run tool calls |
| `raw - split_pillars` | provider-visible input tokens, complete-run tool calls |

Rows returned does not apply across the split stack and reports N/A there. Cache
use, output, reasoning, and cost are reported separately and are descriptive.

## Reproduce the published report

For agent-assisted setup, reproduction, and model execution, use the repository
skill [run-rca-bench](.agents/skills/run-rca-bench/SKILL.md).

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
  --output-json "$output_dir/agent-rca-v34.json" \
  --output-html "$output_dir/agent-rca-v34.html"

cmp artifacts/measurement/agent-rca-v34.json \
  "$output_dir/agent-rca-v34.json"
cmp artifacts/measurement/agent-rca-v34.html \
  "$output_dir/agent-rca-v34.html"
```

Both `cmp` commands exit with status 0 when the original four-model report reproduces
byte for byte. The Gemini and Qwen extension is reproduced separately:

```bash
uv run agent-rca formal-suite-report \
  --micro-artifact artifacts/measurement/agent-rca-v34-two-model-extension-micro.json \
  --transfer-artifact artifacts/measurement/agent-rca-v34-two-model-extension-transfer.json \
  --suite-protocol fixtures/reference/agent-rca-v34-two-model-extension-suite.json \
  --transfer-protocol fixtures/reference/transfer-v34-two-model-extension-protocol.json \
  --publication-metadata artifacts/measurement/agent-rca-v34-two-model-extension-publication.json \
  --output-json "$output_dir/agent-rca-v34-two-model-extension.json" \
  --output-html "$output_dir/agent-rca-v34-two-model-extension.html"

cmp artifacts/measurement/agent-rca-v34-two-model-extension.json \
  "$output_dir/agent-rca-v34-two-model-extension.json"
cmp artifacts/measurement/agent-rca-v34-two-model-extension.html \
  "$output_dir/agent-rca-v34-two-model-extension.html"
```

The publication metadata fixes both UTC timestamps for deterministic reproduction.
The original cohort retains eight tests per confirmatory family; the extension has
four per family. The six-model page combines descriptive results and reports
the two cohorts' corrected tests separately.

Generate the unified page from the two reproduced reports and their bound transfer artifacts:

```bash
uv run agent-rca formal-report-merge \
  --reports "$output_dir/agent-rca-v34.json" \
    "$output_dir/agent-rca-v34-two-model-extension.json" \
  --transfer-artifacts artifacts/measurement/agent-rca-v34-transfer.json \
    artifacts/measurement/agent-rca-v34-two-model-extension-transfer.json \
  --publication-metadata artifacts/measurement/agent-rca-v34-six-model-publication.json \
  --output-json "$output_dir/agent-rca-v34-six-model.json" \
  --output-html "$output_dir/agent-rca-v34-six-model.html"

cmp artifacts/measurement/agent-rca-v34-six-model.json \
  "$output_dir/agent-rca-v34-six-model.json"
cmp artifacts/measurement/agent-rca-v34-six-model.html \
  "$output_dir/agent-rca-v34-six-model.html"
```

Run the complete provider-free validation before publishing a change:

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv lock --check
uv build
shasum -a 256 -c artifacts/measurement/agent-rca-v34-SHA256SUMS
shasum -a 256 -c artifacts/measurement/agent-rca-v34-two-model-extension-SHA256SUMS
shasum -a 256 -c artifacts/measurement/agent-rca-v34-six-model-SHA256SUMS
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
