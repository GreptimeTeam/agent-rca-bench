# Changelog

This file records releases of the benchmark code and the measurements it
publishes. It follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

A benchmark protocol version is separate from the package version. Changing the
prompt, the treatment surface, the scorer, case selection, the runner contract,
or a frozen metric produces a new protocol identifier rather than mutating a
published one, and a published measurement is never recomputed under a later
protocol.

## [Unreleased]

- Extend the report to six models with Gemini and Qwen: 192 micro cells and
  504 end-to-end cells, 696 in total.
- Correct Tempo retention and repeated label names in Split query results.
  Replace all 168 Split cells; retain 336 Raw/Graph cells and all micro results.
  Publish the replacement records, trace checks, and retry audit.
- Report correct diagnoses as Split 105/168, Raw 130/168, and Graph 124/168.
  Three Raw-vs-Split input endpoints pass the separately frozen cohort Holm
  corrections (m = 8 and m = 4); no Graph-vs-Raw endpoint passes.
  Updated case medians and p values are in the
  [Raw-vs-Split endpoint table](REPORT.md#raw-compared-with-split).
- Add Gemini cost intervals that preserve observed cache discounts, and update
  the bilingual reports and charts.
- Use transfer artifact schema 3 and report schema 8. Reproduction and CI compose
  the three measurement sources and compare eight derived artifacts byte for byte.

## [0.1.0] - 2026-09-04

Published as `agent-rca-bench`. The project was developed under the name
`semantic-rca-bench`, which named one of the three interfaces it compares
rather than the comparison itself. Identifiers frozen inside the v34
measurement keep the earlier prefix: case ids, artifact types, and the suite
protocol revision belong to that measurement's identity and are not rewritten
to match a later name.

First public release, carrying benchmark protocol v34.

### Measurement

- 464 completed agent cells: 128 micro-benchmark cells over 8 fixed cases, and
  336 end-to-end cells over 14 incidents, across 4 models and 2 repetitions.
- Three end-to-end interfaces compared on identical incidents, prompts, and
  budgets: Prometheus with Loki and Tempo through their native query APIs,
  GreptimeDB over read-only SQL and PromQL, and that same GreptimeDB with its
  Semantic Graph.
- Two paired comparison families, each specified and frozen in the protocol
  before any run and Holm-corrected on its own: `semantic_graph - raw` on rows
  returned and complete-run tool calls, `raw - split_pillars` on
  provider-visible input tokens and complete-run tool calls.
- One endpoint survived correction: `claude-fable-5-1` read fewer
  provider-visible input tokens through GreptimeDB in all 13 eligible cases,
  case median -446,252.5, Holm-adjusted p 0.00195.
- No `semantic_graph - raw` endpoint passed correction and the case medians
  disagree in direction. The cohort leaves 3 to 13 cases per test, which is
  insufficient evidence rather than evidence of no effect.
- Descriptive totals over 112 runs per interface: 60 correct diagnoses through
  the three-backend bundle, 80 through GreptimeDB, 77 with the Semantic Graph.
  The bundle cost 2.21x, converted at the frozen `6.7179` CNY per USD rate
  checked 2026-09-03, and read 1.54x the tokens.
- Sources: OpenRCA 1.0, OpenRCA2 ops-lite, and RCA100, each under its own terms.
  No source telemetry is redistributed.

### Reproduction

- The tagged tree rescores the published artifacts and regenerates the combined
  JSON and HTML byte for byte, without calling a model provider, building
  GreptimeDB, or running Docker.
- Published artifacts carry protocol, selection, source, and GreptimeDB
  bindings, with hashes over both narrative reports.
- Report schema version 7.

### Known limits

- The eligible sample per endpoint is small, so a non-significant result means
  insufficient evidence and never equivalence.
- Fault level and source dataset are fully confounded in this cohort: every
  infrastructure-node case comes from RCA100. Breakdowns by either are
  descriptive and neither can be credited with the reversal.
- The deterministic evidence verifier reads SQL. A run whose cited evidence is
  entirely in another query language is reported as not estimable rather than
  as a failure.
- Costs are estimates from frozen provider rates, not invoices, and are kept in
  the currency each provider billed.

[Unreleased]: https://github.com/GreptimeTeam/agent-rca-bench/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/GreptimeTeam/agent-rca-bench/releases/tag/v0.1.0
