# Dataset provenance and selection

Semantic RCA Bench downloads upstream telemetry for local execution and does not
redistribute source rows, labels, causal graphs, or archives. Public artifacts
contain sanitized trajectories, derived facts, and source hashes.

## Release cohort

The published measurement contains 18 incidents from two source families:

| Benchmark | Source | Cases | Role |
| --- | --- | ---: | --- |
| Discovery | OpenRCA 1.0 Bank, Market, and Telecom | 6 | Schema and signal discovery |
| Graph retrieval | OpenRCA2 ops-lite | 2 | Service-dependency retrieval |
| End-to-end RCA | OpenRCA2 ops-lite | 10 | Component or edge localization, mechanism diagnosis, and evidence |

The v34 cohort, which has not been run, keeps those eight micro cases and adds
four RCA100 infrastructure-node cases to the end-to-end set, for 22 incidents
from three source families. The ten OpenRCA2 end-to-end cases are the same ones
the published measurement used, carried over rather than reselected.

| Benchmark | Source | Cases | Role |
| --- | --- | ---: | --- |
| End-to-end RCA | RCA100 v1.1 | 4 | Infrastructure-node localization, mechanism diagnosis, and evidence |

The six Discovery cases and two Graph cases form a fixed reference cohort. The
ten end-to-end cases were selected by a frozen source-only ranking before their
formal model trajectories were observed.

The public agent IDs do not expose source case names or injection labels.
Source case names and mechanisms appear only in the report and scorer inputs.

## License boundary

| Dataset | Upstream statement | Benchmark policy |
| --- | --- | --- |
| OpenRCA 1.0 | The paper appendix declares telemetry CC BY-NC 4.0 | Download for local evaluation; do not redistribute telemetry |
| OpenRCA2 ops-lite | The dataset card says Apache-2.0; the paper says CC-BY-SA 4.0 | Publish derived sanitized facts and hashes; do not redistribute telemetry |
| Aegis FSE 2026 reviewer cohort | The dataset record says CC BY 4.0; the reviewer artifact's Apache-2.0 file does not explicitly cover `reproduction/data` | Keep downloader-backed; do not bundle source data |
| RCA100 v1.1 | `RCA100/LICENSE` in the pinned AgenticOpsEval revision declares CC BY-NC-SA 4.0 over the case parquet files, ground truth, summary, and manifest | Keep downloader-backed; do not redistribute telemetry; attribute the dataset paper and license when publishing derived facts |
| RCAEval RE2-OB | The pinned Hugging Face dataset card declares MIT | Local adapter and semantic-coverage validation |
| OpenRCA 1.0 Market and Telecom | Same CC BY-NC 4.0 declaration as OpenRCA Bank | Local evaluation only |

The repository's Apache-2.0 license covers benchmark code, artifact schemas,
and derived reports. It does not relicense upstream data.

## OpenRCA 1.0 micro cohort

- Official repository: <https://github.com/microsoft/OpenRCA>
- Adapter revision: `c1bd4af7f635171a1c31cdd567c07d698dff6abc`
- Public mirror revision: `07714872ea2cec77c13f9dec17a688e9df9621d1`
- Telemetry terms: CC BY-NC 4.0 as declared by the paper appendix

The six Discovery cases cover Bank, Market, and Telecom telemetry. Each fixture
binds one component, signal, table, baseline window, incident window, comparison
field, and minimum effect. The no-model audit requires:

- exact source case and fixture identity;
- successful isolated ingestion;
- a matching frozen evidence predicate;
- the target table in the semantic catalog's top five results;
- an empty exclusive GreptimeDB instance before ingestion.

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
`uv run semantic-rca --help` for their commands.

RCA100 supplies the four infrastructure-node cases of the v34 end-to-end cohort,
alongside the ten OpenRCA2 service and edge cases. It is distributed inside
AgenticOpsEval. The adapter pins dataset revision `v1.1` and source revision
`69cf36430b43024d02530c610b1a4738b5c9a7fb`; the license statement above was read
from `RCA100/LICENSE` at that revision. Attribution requires the dataset paper,
[arXiv:2606.29193](https://arxiv.org/abs/2606.29193).

RCA100 metrics arrive as Prometheus remote write rather than OTLP, so GreptimeDB
records their tables with `metadata_quality: inferred` and no
`greptime.semantic.*` metric options. The four node cases therefore have a
thinner metric semantic surface than the ten OpenRCA2 cases. This predates the
three-arm protocol and is a property of the source, not of the treatment.
