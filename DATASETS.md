# Dataset provenance and selection

Agent RCA Bench downloads source files only for selected cases and source-only
selection audits. It does not redistribute source rows, labels, causal graphs,
ground-truth files, or archives. Public artifacts contain sanitized trajectories,
derived facts, aggregate measurements, and source hashes.

## Release cohort

The published v34 measurement contains 22 incidents from three source families:

| Benchmark | Source | Cases | Role |
| --- | --- | ---: | --- |
| Discovery | OpenRCA 1.0 Bank, Market, and Telecom | 6 | Schema and signal discovery |
| Graph retrieval | OpenRCA2 ops-lite | 2 | Service-dependency retrieval |
| End-to-end RCA | OpenRCA2 ops-lite | 10 | Component or edge localization, mechanism diagnosis, and evidence |
| End-to-end RCA | RCA100 v1.1 | 4 | Infrastructure-node localization, mechanism diagnosis, and evidence |

The six Discovery cases and two Graph cases form a fixed reference cohort. The
ten OpenRCA2 end-to-end cases were selected by a frozen source-only ranking
before their formal model trajectories were observed and carried over from the
previous measurement. The RCA100 source-only selection ranked 15 node-fault
candidates and selected four cases before their formal model trajectories were
observed.

The public agent IDs do not expose source case names or injection labels.
Source case names and mechanisms appear only in the report and scorer inputs.

## License boundary

| Dataset | Upstream statement | Benchmark policy |
| --- | --- | --- |
| OpenRCA 1.0 | The paper appendix declares telemetry CC BY-NC 4.0 | Download files for six selected Discovery cases; publish no source telemetry or ground-truth files |
| OpenRCA2 ops-lite | The dataset card says Apache-2.0; the paper says CC-BY-SA 4.0 | Publish derived sanitized facts and hashes; do not redistribute telemetry |
| Aegis FSE 2026 reviewer cohort | The dataset record says CC BY 4.0; the reviewer artifact's Apache-2.0 file does not explicitly cover `reproduction/data` | Keep downloader-backed; do not bundle source data |
| RCA100 v1.1 | `RCA100/LICENSE` in the pinned AgenticOpsEval revision declares CC BY-NC-SA 4.0 over the case Parquet files, ground truth, summary, manifest, README, and disclaimer | Rank 15 node-fault candidate profiles, download four selected case packages, and publish no source telemetry, topology, or ground-truth files |
| RCAEval RE2-OB | The pinned Hugging Face dataset card declares MIT | Local adapter and semantic-coverage validation |
| OpenRCA 1.0 Market and Telecom | Same CC BY-NC 4.0 declaration as OpenRCA Bank | Same six-case policy as the OpenRCA row |

The repository's Apache-2.0 license covers the benchmark's original code,
artifact schemas, report text, and independently derived aggregates. It does
not relicense upstream data or upstream dataset documentation.

## OpenRCA 1.0 micro cohort

- Official repository: <https://github.com/microsoft/OpenRCA>
- Adapter revision: `c1bd4af7f635171a1c31cdd567c07d698dff6abc`
- Public mirror revision: `07714872ea2cec77c13f9dec17a688e9df9621d1`
- Telemetry terms: CC BY-NC 4.0 as declared by the paper appendix
- License: [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)
- Paper: [OpenRCA: Can Large Language Models Locate the Root Cause of Software
  Failures?](https://openreview.net/forum?id=M4qNIzQYpd)

The paper credits Junjielong Xu, Qinan Zhang, Zhiqing Zhong, Shilin He, Chaoyun
Zhang, Qingwei Lin, Dan Pei, Pinjia He, Dongmei Zhang, and Qi Zhang.

The six Discovery cases cover Bank, Market, and Telecom telemetry. Each fixture
binds one component, signal, table, baseline window, incident window, comparison
field, and minimum effect. The no-model audit requires:

- exact source case and fixture identity;
- successful isolated ingestion;
- a matching frozen evidence predicate;
- the target table in the semantic catalog's top five results;
- an empty exclusive GreptimeDB instance before ingestion.

The selected source cases are:

- `Bank/task_5@2021-03-04T20:00`
- `Bank/task_6@2021-03-25T09:00`
- `Market/cloudbed-1@2022-03-20T09:30`
- `Market/cloudbed-2@2022-03-20T14:30`
- `Telecom@2020-05-29T03:30`
- `Telecom@2020-05-23T04:30`

The adapter downloads each source system's `query.csv` and `record.csv` and the
required telemetry files for the selected dates. It evaluates the frozen case
windows and maps source formats into the benchmark ingestion protocols. Public
fixtures record case identifiers, components, signals, source table names,
windows, comparison fields, thresholds, and aggregate ingestion and query
audits. They contain no source telemetry rows or ground-truth records.

The source formats do not always carry OpenTelemetry identity, span-kind,
operation, status, or duration semantics. Adapters preserve those absences. They
do not synthesize Graph entities or relationships from component names.

OpenRCA metric duplicates and conflicting timestamp identities are preserved at
the protocol boundary. The public artifact records source, accepted, stored,
duplicate, conflict, and identifier-remapping counts for each case.

## OpenRCA2 source

- Repository: <https://huggingface.co/datasets/anon-ops/ops-lite>
- Pinned ops-lite revision: `9ac09981c08ab02a0b923eab7830d778934851a8`
- Dataset-card license: Apache-2.0
- Paper license: CC-BY-SA 4.0

The license statements conflict, and the downloaded artifact is not the
archival release described by the paper. Pinning the revision makes the bytes
reproducible but does not resolve that conflict. The benchmark therefore keeps
the source downloader-backed and publishes no telemetry rows.

OpenRCA2 supplies normal and anomalous Parquet telemetry, an environment file,
publisher attributes and labels, injection metadata, and a reference causal
graph. The adapter uses source telemetry and injection metadata for validation
and scoring. It never ingests `causal_graph.json`.

### End-to-end selection

The active manifest is
`fixtures/reference/openrca2-transfer-v32-selection.json`. It freezes:

- the pinned source revision;
- the eligible source population;
- trajectory exclusions from development pilots;
- mechanism and system strata;
- deterministic ranking and selected source cases;
- opaque IDs `semantic-rca-transfer-001` through
  `semantic-rca-transfer-010`;
- source-preserving predicates that the scorer may accept.

Every exclusion must name an existing source case. The selection audit rejects
unknown exclusions, a selected development trajectory, hash drift, ordering
drift, and a mismatch between source labels, injection metadata, and the frozen
mechanism contract.

The ten cases are:

| Opaque ID | System | Mechanism | Causal target |
| --- | --- | --- | --- |
| `001` | Hotel Reservation | Workload restart | `user` |
| `002` | Hotel Reservation | Workload restart | `reservation` |
| `003` | Hotel Reservation | Workload restart | `user` |
| `004` | OpenTelemetry Demo | Workload restart | `product-catalog` |
| `005` | Hotel Reservation | Call-path delay | `search -> rate` |
| `006` | Hotel Reservation | Call-path delay | `search -> rate` |
| `007` | OpenTelemetry Demo | Call-path delay | `shipping -> quote` |
| `008` | Hotel Reservation | CPU saturation | `search` |
| `009` | Hotel Reservation | CPU saturation | `reservation` |
| `010` | Hotel Reservation | Memory pressure | `geo` |

The formal agent sees the opaque ID, incident window, schema, telemetry, and
case-independent mechanism ontology. It does not see the source case name,
selected mechanism, injection label, or reference causal graph.

### Source-faithful replay

Each formal case uses a new release-mode GreptimeDB process, loopback port, data
directory, and database. The adapter replays:

- metrics through OpenTelemetry Protocol (OTLP);
- logs through Loki;
- traces through OTLP.

Trace replay preserves trace ID, span ID, parent span ID, service identity,
Client/Server role, source status, timestamp, duration, HTTP attributes, and
source operation. It does not infer topology, repair identity, synthesize span
roles, or convert HTTP 5xx into OpenTelemetry `STATUS_CODE_ERROR`.

The audit records source, protocol, and stored row counts; rejected rows;
duplicate and conflicting metric identities; protocol representation losses;
and identifier remapping. Unexpected service, trace, or span identity changes
fail the audit.

### Raw-span and Graph equality

The provider-free preflight independently reconstructs every `calls` edge from
stored trace rows:

- parent kind is `SPAN_KIND_CLIENT`;
- child kind is `SPAN_KIND_SERVER`;
- trace IDs match;
- `child.parent_span_id = parent.span_id`;
- service identity comes from stored source fields;
- request count counts paired Server spans;
- error count counts Server spans with `STATUS_CODE_ERROR`.

It compares the complete normalized edge set with
`greptime_private.semantic_relationships`, including source and destination
types and IDs, relationship type, provenance, request count, and error count.
Hash equality is recorded but never substitutes for full row equality.

OpenRCA2 windows are not necessarily minute-aligned. The audit records the
source half-open windows, minimal whole-minute Graph envelope, widened
server-side scan, boundary-bin strategy, source boundary observations, complete
normalized rows, and both hashes. All ten published cases passed this gate in
two independent preflights with identical source-semantic hashes.

### Frozen mechanism evidence

The source and stored-telemetry audits bind one measurable transition per case:

- workload restart: restart counter is below `1` in the complete baseline and
  at least two anomalous observations are at or above `1`;
- call-path delay: paired Server start minus Client start is below `500 ms` in
  the complete baseline and at least two anomalous pairs cross the threshold;
- CPU saturation: container CPU is below `0.5` in the complete baseline and at
  least two anomalous observations cross it;
- memory pressure: working set is below `512 MiB` in the complete baseline and
  at least two anomalous observations cross it.

The canonical query is an audit oracle, not an agent query recipe. The scorer
accepts semantically equivalent evidence when its SQL establishes the required
source, identity, time scope, lineage, and result completeness. See
[SCORING.md](SCORING.md).

## Other adapters

RCAEval RE2-OB, Aegis, OpenRCA Market, and OpenRCA Telecom adapters remain
available for smoke tests and source-fidelity audits. They are not part of the
2026 report unless listed in the release cohort. Run
`uv run agent-rca --help` for their commands.

RCA100 supplies the four infrastructure-node cases of the v34 end-to-end cohort,
alongside the ten OpenRCA2 service and edge cases. It is distributed inside
AgenticOpsEval. The adapter pins dataset revision `v1.1` and source revision
`69cf36430b43024d02530c610b1a4738b5c9a7fb`. The pinned
[RCA100 license](https://www.aiops.cn/gitlab/aiops-live-benchmark/agenticopseval/-/raw/69cf36430b43024d02530c610b1a4738b5c9a7fb/RCA100/LICENSE)
declares [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)
and requires the citation in the dataset README. That citation credits Xidao
Wen, Haibin Liu, Guiyang Liu, Cheng Zhang, Fang Situ, and Qi Zhou. The associated
AgenticOpsEval paper is [arXiv:2606.29193](https://arxiv.org/abs/2606.29193).

The frozen selection fixture records aggregate profiles for 15 node-fault
candidates and selects `t019`, `t003`, `t022`, and `t073`. Candidate profiles
contain a case identifier, node name, source fault type, selection status, and,
when available, normal, anomalous, and peer maxima. Selected-case records add
the frozen windows, benchmark fault category, evidence threshold and sample
counts when available, source table name, and hashes of the source files.

The adapter downloads `task.json`, telemetry Parquet files, events, alerts, and
topology for each selected case, plus its answer key and the shared taxonomy.
It maps the source telemetry into the benchmark ingestion protocols and exposes
neither topology nor answer keys to the agent. Public artifacts contain no
Parquet rows, topology content, causal graphs, or ground-truth files.

RCA100 metrics arrive as Prometheus remote write rather than OTLP, so GreptimeDB
records their tables with `metadata_quality: inferred` and no
`greptime.semantic.*` metric options. The four node cases therefore have a
thinner metric semantic surface than the ten OpenRCA2 cases. This predates the
three-arm protocol and is a property of the source, not of the treatment.
