# Semantic RCA Bench contributor guide

This guide applies to the entire repository. Read any more specific `AGENTS.md`
on the path you edit. If `.local/AGENTS.md` exists, read it as well. Files under
`.local/` are machine-local and must not be committed.

## Purpose

Semantic RCA Bench measures whether the complete GreptimeDB Semantic Graph
interface changes the accuracy, investigation work, and cost of LLM-based root
cause analysis (RCA). The primary paired comparison is `semantic_graph - raw`
for the same model, incident, telemetry, prompt, runner contract, and resource
budget.

The benchmark is designed to falsify as well as support the hypothesis. Do not
alter source data, select cases after observing trajectories, relax a scorer to
improve a result, or synthesize topology and identity that the source does not
provide.

GreptimeDB supplies telemetry storage, schema and signal semantics, stable
entity identity, source-proven relationships, provenance, and efficient query
surfaces. The model remains responsible for investigation strategy, evidence
weighing, and causal reasoning. Do not add incident-specific RCA expert rules to
the database or benchmark.

## Measurement contract

- Compare only the same model and frozen configuration across `raw` and
  `semantic_graph`.
- Treat cases as the independent units. Repetitions describe variability and do
  not increase the inferential sample size.
- Reduce eligible repetition-pair deltas to one median per model and case before
  cross-case summaries.
- The registered end-to-end efficiency metrics are
  `database_load.rows_returned` and
  `evaluation.correct_completion_tool_calls`.
- End-to-end efficiency eligibility requires a correct diagnosis, at least one
  citation to a successful non-truncated query, no runner error, and no budget
  exhaustion.
- Deterministic evidence sufficiency is a separate audit. It does not select the
  headline efficiency sample and an LLM judge must not rewrite the primary
  endpoints.
- Discovery and Graph micro-benchmarks use
  `rows_returned_through_evidence` and `tool_calls_through_evidence`. Do not mix
  these fields with end-to-end metrics.
- Report input, cache read, cache write, output, and reasoning tokens according
  to the frozen provider contract. Reasoning that is included in output must
  not be counted twice.
- Preserve provider currencies. Do not aggregate currencies without a frozen
  exchange-rate policy.
- Provider reasoning settings define complete model configurations, not a
  common cross-provider compute scale.

`SCORING.md` defines diagnosis, citation, claim grounding, reliability, and
eligibility. `src/semantic_rca_bench/protocol.py` and the active reference
fixtures are the machine-readable protocol sources.

## Source fidelity

- Record each dataset revision, checksum, provenance, and license statement.
- Preserve source timestamps, identifiers, metric types, span roles, status,
  and source defects.
- Do not infer topology, entity identity, client/server roles, parent relations,
  or causal dependencies from names.
- Reference labels and causal graphs may be used for selection and scoring, but
  must not be ingested or exposed to the agent unless a protocol says so.
- Freeze measurement cases before inspecting their model trajectories. Record
  the eligible population, exclusions, deterministic ranking, and no-model
  rejections.
- Keep upstream telemetry downloader-backed. Never commit source archives,
  telemetry, label files, credentials, provider payloads, or local paths.

## Runner and database isolation

- Bind the API transport, endpoint class, credential source, reasoning setting,
  output ceiling, tool budget, turn policy, and timeout policy in the protocol.
- Tenant-specific endpoints stay in runtime configuration and must not enter
  fixtures or public artifacts.
- Subscription runners must not fall back to billable APIs.
- Persist runner failures and budget exhaustion as scoreable failed cells. Do
  not abort a complete schedule because a model cell fails.
- Use one exclusive GreptimeDB process, port, data directory, and database for
  each formal case. Stop only the process started by the benchmark.
- Do not modify, reuse, or stop an unrelated service on `localhost:4000`.
- Commands that call model providers require explicit user approval for that
  invocation.

## Public artifacts

English is the primary project language. `REPORT.md` is the canonical narrative
report, and `REPORT.zh-CN.md` is its maintained Chinese translation. Runtime
strings required by the bilingual HTML report may contain both languages.

Public measurement artifacts contain sanitized trajectories, derived facts,
protocol bindings, and hashes. They exclude raw telemetry rows, provider
responses, reasoning text, credentials, endpoints, labels, source archives, and
machine-local paths. Historical development reports, fixtures, and notes belong
under `.local/`, not in the release tree.

The public release tag is the code and report-reproduction boundary. The tagged
tree must deterministically rescore the published sanitized artifacts and
regenerate the combined JSON and HTML. Reinvoking a provider is a replication,
not a byte-for-byte reproduction of model output.

## Repository map

- `README.md`: project entry point and reproduction workflow.
- `REPORT.md`, `REPORT.zh-CN.md`: English and Chinese measurement reports.
- `SCORING.md`: end-to-end scoring and evidence-verifier specification.
- `DATASETS.md`: dataset provenance, selection, fidelity, and license audit.
- `DISCOVERY.md`: schema-discovery micro-benchmark.
- `GRAPH.md`: dependency-retrieval micro-benchmark and exact-edge audit.
- `fixtures/reference/`: active source-selection and formal protocol fixtures.
- `fixtures/measurement/`: frozen micro-benchmark fixtures and summaries.
- `src/semantic_rca_bench/agent.py`: provider runners and tool loop.
- `src/semantic_rca_bench/formal_suite*.py`: micro schedule, execution, export,
  and protocol binding.
- `src/semantic_rca_bench/transfer_*.py`: end-to-end schedule, scorer, export,
  and report aggregation.
- `src/semantic_rca_bench/datasets/`: source adapters and provider-free audits.
- `src/semantic_rca_bench/greptimedb/`: GreptimeDB process and query boundary.
- `src/semantic_rca_bench/formal_report.py`: deterministic combined report.
- `src/semantic_rca_bench/assets/formal-measurement-report.html`: bilingual
  self-contained HTML template.
- `tests/`: protocol, scorer, runner, adapter, and regression tests.

## Development workflow

Use Python 3.11 and `uv`.

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv lock --check
uv build
```

Run the narrowest relevant tests first, then expand based on the change's blast
radius. Verify every command, path, fixture, and result quoted in documentation.

Before editing, run `git status --short` and preserve unrelated work. Use small,
reviewable diffs. Do not add compatibility layers for internal protocol cycles;
inspect an older experiment at its historical Git revision instead.

Commits and external publication require explicit user approval. Use signed-off
Conventional Commit messages without generated-tool attribution.
