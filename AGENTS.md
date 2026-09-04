# Agent RCA Bench contributor guide

This guide applies to the entire repository. Read any more specific `AGENTS.md`
on the path you edit. If `.local/AGENTS.md` exists, read it as well. Files under
`.local/` are machine-local and must not be committed.

## Purpose

Agent RCA Bench measures how the observability stack behind an LLM changes
the accuracy, investigation work, and cost of root cause analysis (RCA). One
measurement answers three questions:

1. **Storage shape.** How the GreptimeDB all-in-one agent-facing interface
   compares with a Prometheus, Loki and Tempo native interface bundle. Paired
   comparison `raw - split_pillars`; secondary confirmatory.
2. **Semantic layer.** Whether the complete GreptimeDB Semantic Graph interface
   changes RCA. Paired comparison `semantic_graph - raw`; primary confirmatory.
3. **Model ranking.** How models differ at RCA. Descriptive; no hypothesis test.

Every comparison holds the model, incident, telemetry, prompt, runner contract,
and resource budget fixed. The prompt's investigation method and diagnosis
contract are byte-identical across all three arms; only the notes describing how
to drive a particular store differ, and those state interface facts rather than
investigation strategy. The two GreptimeDB arms therefore receive one identical
prompt and differ only in the tools they are given.

Question 1 is not a single-factor comparison of storage topology. The split arm
changes the store, the query languages, and the tool surface together, which is
what `treatment_estimand: complete-agent-facing-interface` in `protocol.py`
already declares. Describe it as an interface-bundle comparison, never as
isolating storage shape.

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

- Compare only the same model and frozen configuration across `split_pillars`,
  `raw` and `semantic_graph`.
- Treat cases as the independent units. Repetitions describe variability and do
  not increase the inferential sample size.
- Reduce eligible repetition-pair deltas to one median per model and case before
  cross-case summaries.
- The two confirmatory families are corrected separately, each with `m` equal to
  the number of models times the number of endpoints in that family. Pooling
  them would make one question's significance depend on how many tests the other
  question ran.
- The two families, their metrics, and the Holm family size are frozen in the
  protocol fixture before any run, and every artifact binds that fixture by
  hash. Machine fields keep the names `confirmatory_families` and
  `primary_metrics`. Published prose calls them **pre-specified**, never
  "registered" or "pre-registered": those words imply a public third-party
  registry entry with a timestamp, and this benchmark has a hashed fixture in a
  Git repository instead. State that difference where the page first uses the
  term.
- The end-to-end efficiency metrics frozen in the protocol are
  `evaluation.correct_completion_tool_calls` for both families,
  `database_load.rows_returned` for `semantic_graph - raw`, and
  `usage.provider_visible_input_tokens` for `raw - split_pillars`.
- `database_load.rows_returned` is not applicable to `split_pillars`: a
  Prometheus sample, a Loki entry and a Tempo trace are not the unit a
  GreptimeDB row is. It serializes as null there and must render as N/A, never
  as zero.
- Metric queries answered by PromQL count their returned rows into
  `rows_returned` exactly as SQL does. Leaving them uncounted would make the
  same investigation look free on a registered endpoint.
- End-to-end efficiency eligibility requires a correct diagnosis, at least one
  citation to a successful non-truncated query, no runner error, and no budget
  exhaustion.
- Deterministic evidence sufficiency is a separate audit. It does not select the
  headline efficiency sample and an LLM judge must not rewrite the primary
  endpoints.
- The evidence verifier reads SQL. A run whose cited evidence is entirely in
  another query language is `not estimable`, carrying
  `grounding_not_estimable_reason`, and never `False`. Recording a failure there
  would report a missing tool as a model defect and would penalise whichever arm
  the verifier does not cover.
- Discovery and Graph micro-benchmarks use
  `rows_returned_through_evidence` and `tool_calls_through_evidence`. Do not mix
  these fields with end-to-end metrics.
- Report input, cache read, cache write, output, and reasoning tokens according
  to the frozen provider contract. Reasoning that is included in output must
  not be counted twice.
- Preserve provider currencies. Spend is recorded in the currency it was billed
  in, and that subtotal is never overwritten. A cross-currency total is derived
  from those subtotals at the rates frozen in `EXCHANGE_RATES_TO_USD`, published
  with their date and source beside every figure they produce, and only when
  every model in the cohort could be priced. A currency with no frozen rate
  raises rather than converting at a guess.
- The pricing snapshot records the rates in force for the run, and
  `paid_execution.pricing_snapshot_required_at_execution` requires it to exist
  before the run starts. Correcting a rate the snapshot missed is a fix and
  keeps the note saying what was corrected and when it was verified. Applying a
  rate that only took effect after the run is not: that would price the run at
  something it was never billed. Either way the token counts are untouched, and
  the correction is stated wherever the cost appears.
- Provider reasoning settings define complete model configurations, not a
  common cross-provider compute scale.

`SCORING.md` defines diagnosis, citation, claim grounding, reliability, and
eligibility. `src/agent_rca_bench/protocol.py` and the active reference
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
- Use one exclusive Prometheus, Loki and Tempo container per formal case, on
  loopback ports, from digest-pinned images. A split stack that will not start
  or will not ingest is an environment failure that stops the case before any
  provider is called; it is not a scoreable model failure.
- Do not modify, reuse, or stop an unrelated service on `localhost:4000`.
- Commands that call model providers require explicit user approval for that
  invocation.

## Split-stack ingestion

Traces reach Tempo as the same protocol bytes GreptimeDB receives. Metrics and
logs cannot, so each carries a declared protocol mapping and is audited on
stored content instead of payload bytes.

- Metrics: GreptimeDB keeps OTLP; Prometheus receives remote write. Prometheus
  rejects `AGGREGATION_TEMPORALITY_UNSPECIFIED` sums and histograms, and the
  OpenRCA2 archives declare no temporality. Writing one would stamp
  `metric.temporality` into GreptimeDB's semantic options, so the replay keeps
  the source's silence and changes transport instead.
- Logs: Loki accepts only `[a-zA-Z_][a-zA-Z0-9_]*` label names, does not read
  `x-greptime-log-table-name`, indexes every distinct label set as a stream,
  treats an empty label value as absent, and collapses entries identical in
  labels, timestamp and line. The fanout folds label names, carries the table as
  a `log_table` label, and sends `trace_id` and `span_id` as structured
  metadata.
- Loki's `discover_service_name` and `discover_log_levels` are off. A store that
  invents `service_name: unknown_service` would supply an identity the source
  does not have, in a benchmark about establishing identity.
- Loki answers a push `204` even when it drops entries. Ingestion is proved by
  reading the case back, never by the response code.
- Build every split-stack expectation from the source archive. Deriving one
  store's input from the other store's contents would let the audit clear
  itself.

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

The HTML report renders in the browser from two inlined payloads: the combined
report JSON and the view model `formal_report_view.py` derives from it. Which
side a string belongs to is fixed:

- A sentence whose wording depends on a measured value is built in Python and
  enters the view model in both languages. Moving one into the renderer would
  put a claim about the measurement outside the reach of the test suite, which
  is what keeps published copy from drifting away from the numbers.
- Static interface copy that no measurement can change - section titles, table
  headers, legends, the glossary - lives in `assets/report/i18n.json`. Both
  language maps carry the same keys.
- A cross-reference the renderer follows - a currency into the rate table, a
treatment into a value map, a benchmark into a label map - is closed by the
view model, not checked in the browser. A missing key throws and blanks every
section, and the Python suite stays green while it happens, so the closure is
asserted in  instead.

The renderer lays out and draws. It must not word a claim about significance,
  direction, eligibility, or which arm a comparison favours: those sentences
  arrive from the view model already written. Mapping a value the view model
  already signed to a colour or a bar length is layout, not judgement.

No grading tier may sit beside the pre-specified test. An endpoint either
survives Holm correction or it does not, and the page says so with the frozen
numbers. A softer tier invented after the run - "directional", "near
significant" - reads as a weaker result while being a threshold chosen once its
effect on the conclusion is already visible, which is the reading the frozen
protocol exists to prevent. Report the case medians and their signs as
description instead; they are the same evidence without the borrowed authority.

The reverse move is also barred: do not restate the two frozen families as
exploratory, drop the binary outcome, or remove the endpoint tally once the
results are known. The tally is what shows how many comparisons were run, so
without it a reader cannot tell whether the one surviving endpoint was selected
after the fact. Relabelling a comparison that was frozen before the run is the
same post-hoc adjustment as adding a softer tier, in the other direction.

Lead each endpoint sentence with the effect: how many eligible cases moved which
way, and the case median. The exact sign p and then the Holm-adjusted p follow
it. The test constrains what may be concluded; it is not the subject of the
sentence. State the power limit alongside, because eligibility leaves far fewer
cases than the cohort holds and a non-significant result on that sample means
insufficient evidence, never equivalence.

Post-hoc breakdowns - by fault level, dataset, or mechanism - are published as
descriptions and labelled as made after the measurement. Where two splits are
collinear, as fault level and source dataset are in this cohort, say so in the
same sentence that reports the split; neither may be credited with the effect.

Rendering client-side means the page needs a no-JavaScript path. Python emits a
static summary of the verdicts and headline numbers from the same view model, so
the fallback cannot state a different result from the page.

## Repository map

- `README.md`: project entry point and reproduction workflow.
- `REPORT.md`, `REPORT.zh-CN.md`: English and Chinese measurement reports.
- `SCORING.md`: end-to-end scoring and evidence-verifier specification.
- `DATASETS.md`: dataset provenance, selection, fidelity, and license audit.
- `DISCOVERY.md`: schema-discovery micro-benchmark.
- `GRAPH.md`: dependency-retrieval micro-benchmark and exact-edge audit.
- `fixtures/reference/`: active source-selection and formal protocol fixtures.
- `fixtures/measurement/`: frozen micro-benchmark case fixtures and selection
  manifests.
- `src/agent_rca_bench/agent.py`: provider runners and tool loop.
- `src/agent_rca_bench/formal_suite*.py`: micro schedule, execution, export,
  and protocol binding.
- `src/agent_rca_bench/transfer_*.py`: end-to-end schedule, scorer, export,
  and report aggregation.
- `src/agent_rca_bench/datasets/`: source adapters and provider-free audits.
- `src/agent_rca_bench/greptimedb/`: GreptimeDB process and query boundary.
- `src/agent_rca_bench/formal_report.py`: deterministic combined report.
- `src/agent_rca_bench/formal_report_view.py`: view model and HTML renderer.
- `src/agent_rca_bench/assets/report/`: page skeleton, stylesheet, renderer,
  and static interface strings, inlined into one self-contained HTML file.
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
