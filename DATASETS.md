# Dataset Audit

This audit records why each corpus is included or rejected. The benchmark does
not repair timestamps, labels, topology, or root-cause records. A protocol
adapter may convert declared units and identifiers into a wire representation,
but it must record every lossy or synthetic protocol field.

## Portfolio

| Corpus | System family | Role | Status | Semantic Graph evidence |
| --- | --- | --- | --- | --- |
| RCAEval | Online Boutique | Established control for code-level failures | Included | Case-dependent; treated as graph-negative unless source identity supports edges |
| RCA100 v1.1 | OpenTelemetry Demo Store | Native OpenTelemetry and reference topology | Included | Positive: witnessed service calls are available |
| OpenRCA 1.0 Bank | OpenRCA Bank | Wide enterprise metric schema and legacy multimodal telemetry | Included | Not applicable: no standard entity identity or span roles |
| OpenRCA 1.0 Market | OpenRCA Market | Multi-level node, pod, and service failures over wide legacy telemetry | Included; protocol v17 development case | Not applicable: parent links exist, but client/server span roles and standard identity do not |
| OpenRCA 1.0 Telecom | OpenRCA Telecom | Independent telecom/database system with metrics and traces but no logs | Included; protocol v17 development case | Not applicable: parent links exist, but client/server span roles and standard identity do not |
| Aegis FSE 2026 reviewer cohort | Train Ticket | Public reviewer subset with native span identity and roles | 1.0 ingestion gate; protocol v24 model pilot, now development | Positive: raw Client-to-Server parent-child spans produce independently auditable service-call edges |
| OpenRCA 2.0 ops-lite | Hotel Reservation | Native OTel and verified causal paths | Provisional; internal protocol-formal measurement only | Positive when standard client/server spans witness service calls |
| Amazon PetShop | Amazon PetShop | Component-level causal RCA over service metrics | Rejected | Metric-only; no incident-local mechanism label or continuous baseline |
| AnoMod TrainTicket | TrainTicket | Independent multimodal microservice corpus | Rejected | Rejection is based on incident evidence quality, not graph coverage |
| Eadro / Nezha | Mixed microservice benchmarks | Earlier multimodal RCA corpora | Not audited | No rejection claim until source artifacts are audited |

## Public reference eligibility

Dataset availability is not the same as permission to redistribute the evidence
needed to audit a benchmark result.

| Corpus | Confirmed terms | 1.0 public-reference role |
| --- | --- | --- |
| RCAEval | Dataset repository declares MIT | Eligible for a downloader-backed Table or Graph-negative role; individual cases still require the normal fidelity gates |
| RCA100 v1.1 | No dataset license found; answer key asks users to contact the publisher before redistribution | Internal only until written terms or permission cover the required telemetry and labels |
| OpenRCA 1.0 Bank / Market / Telecom | Paper appendix declares telemetry CC BY-NC 4.0 | Optional noncommercial, downloader-backed evaluation; not the unrestricted reference corpus, and raw telemetry must not be copied into release artifacts |
| Aegis FSE 2026 reviewer cohort | The source dataset record declares CC BY 4.0. The reviewer artifact's root `LICENSE` applies Apache-2.0 to packaging and support code, but does not explicitly apply it to `reproduction/data`. | Use the pinned source download without redistributing telemetry. Obtain clearer permission before bundling any source data in a release. |
| OpenRCA 2.0 ops-lite | Dataset card says Apache-2.0, paper says CC-BY-SA 4.0, and the artifact is not the promised archival release | Internal protocol-formal evidence until provenance and terms are reconciled |

The Aegis reviewer cohort supplies a public, pinned, downloader-backed path for
the Graph-positive transfer case. The benchmark does not redistribute its
telemetry or label files. The release must keep the benchmark code license and
dataset terms separate. Bundling telemetry remains blocked until the publisher
clarifies the reduced subset's license.

## Fresh micro-benchmark measurement audit

Discovery v2 uses the OpenRCA source revision and pinned public mirror recorded
below. The eligible-set counts, SHA-256 digests, prior-trajectory exclusions,
consumed hash ranks, and rejection reasons are stored in
`fixtures/measurement/discovery-selection.json`. Every formal case was ingested
into a fresh neutral database and passed exact protocol row-count validation,
an independent two-window canonical query, and the catalog top-five gate before
its agent cells ran.

| Formal Discovery case | Hidden target | Catalog rank | Baseline count / mean / max | Incident count / mean / max |
| --- | --- | ---: | --- | --- |
| Bank `task_5@2021-03-04T20:00` | `OSLinux_CPU_CPU_CPUUserTime` | 1/41 | 14 / 0.19297 / 0.2206 | 16 / 16.91008 / 88.0287 |
| Bank `task_6@2021-03-25T09:00` | `OSLinux_OSLinux_LOCALDISK_LOCALDISK_sda_DSKRead` | 1/58 | 2 / 0 / 0 | 13 / 2.29744 / 17.6 |
| Market cloudbed-1 `2022-03-20T09:30` | `container_fs_reads_MB__dev_vda` | 4/97 | 14 / 0 / 0 | 6 / 9696.74414 / 16790 |
| Market cloudbed-2 `2022-03-20T14:30` | `container_fs_writes_MB__dev_vda` | 5/64 | 2 / 0 / 0 | 27 / 1543.59910 / 15242.07227 |
| Telecom `2020-05-29T03:30` | `container_cpu_used` | 1/18 | 6 / 9.33333 / 23 | 18 / 42.94444 / 83 |
| Telecom `2020-05-23T04:30` | `container_cpu_used` | 1/18 | 17 / 0.05882 / 1 | 12 / 14.25 / 45 |

The initial Bank CPU case passed its source ratio gate but ranked 14th in
catalog search and was rejected before any model run. The initial cloudbed-2
memory case failed the 2× source ratio; its successor had no unique physical
component binding. A later cloudbed-2 node-write case passed both gates, but its
agent report predates the mixed-case identifier prompt correction and remains
exploratory. The next untouched hash-ranked case supplies the formal replacement.

Graph v3 selection is recorded in
`fixtures/measurement/graph-v3-selection.json`. Six Hotel candidates, all 12
remaining OTel Demo candidates, and the only Train Ticket candidate failed the
pre-model observable-alert or manifest/injection-root contract. The two passing
Hotel cases each expose six direct callees of `frontend`; raw span reconstruction
and Graph rows match exactly. Their unique `search` winners have 504/1,043 and
1,504/2,772 errors/requests respectively. Candidate exhaustion is part of the
result and limits Graph v3 to one system family.

## RCA100 v1.1

- Dataset revision: `v1.1`.
- License: no dataset license was found in the pinned public source. The answer
  key asks users to contact the Alibaba Cloud AIOps Team before redistribution.
  This benchmark downloads it for local evaluation and does not redistribute
  telemetry or answer-key files.
- Adapter source revision: `69cf36430b43024d02530c610b1a4738b5c9a7fb`.
- `t001` contains native metrics, logs, traces, events, alerts, an official
  fault label, and a reference topology used only for validation.
- The source contains 26,397 duplicate metric samples and 13 conflicting
  timestamps. The adapter preserves source order and lets Prometheus primary-key
  semantics determine stored values. It does not add labels to disambiguate
  conflicts.
- The source does not publish an injection timestamp. Onset scoring is disabled
  rather than inferred from snapshots.
- Protocol v14 evaluation case: `t002`, selected by the hash-ranking contract
  and seed recorded in `PLAN.md`; no fault label participated in selection.

## RCAEval RE2-OB

- Dataset artifact revision: `afeacb11bcc94dadfd1c8f483ee4377b2b8b614e`.
- License: MIT, as declared by the pinned Hugging Face dataset card.
- Adapter source revision: `526cdd5818ea9d8c2a34e869ebd637bc6b4fa4b8`.
- Selected case: `re2ob_checkoutservice_cpu_1`.
- The source publishes trace start time in both microseconds and milliseconds.
  The adapter verifies their declared relationship before converting to OTLP;
  it does not infer or shift timestamps.
- No-model gate result: 103,748 metric samples, 171,322 logs, and 391,997 spans
  stored with exact protocol row-count match; 74 semantic tables; seven service
  entities and zero relationships.
- The source lacks span-kind semantics needed to witness client/server call
  edges. The case is therefore entity-only rather than relational.
- Protocol v14 evaluation case: `re2ob_currencyservice_disk_1`. The two
  higher-ranked candidates failed telemetry-label fidelity: the socket case had
  a much stronger competing CPU anomaly, and the loss case had no direct loss
  signal while memory, sockets, and latency all rose. The retained disk case has
  no baseline disk I/O and about 4.5 GB/s after injection. Public case IDs are
  label-bearing, so this is reproducible but not a blinded holdout.
- Its isolated no-model gate stored 105,890 metric samples, 157,693 logs, and
  361,288 spans with exact row-count agreement. It exposes 76 semantic tables,
  seven service entities, and no relationships.

## OpenRCA 1.0 Bank

- Official repository: <https://github.com/microsoft/OpenRCA>, pinned at
  `c1bd4af7f635171a1c31cdd567c07d698dff6abc`.
- License: the code repository is MIT. The OpenRCA paper appendix declares all
  telemetry data CC BY-NC 4.0. The benchmark downloads pinned source files for
  local evaluation and does not redistribute the mirror.
- Public data mirror: `tracer-cloud/opensre`, pinned at
  `07714872ea2cec77c13f9dec17a688e9df9621d1`.
- OpenRCA mirror downloads honor standard proxy environment variables because
  the Market and Telecom artifacts are large and may require configured egress.
  GreptimeDB query and ingestion clients continue to ignore environment proxy
  settings so local benchmark traffic cannot leave the target endpoint.
- Selected case: `task_6@2021-03-04T18:00`; official ground truth is `Redis02`
  with `high memory usage`, occurring at 18:09 UTC+8.
- The official prompt declares metric and log timestamps in seconds, trace
  timestamps in milliseconds, and task times in UTC+8. The adapter performs
  only those declared conversions.
- The source does not declare a trace `duration` unit. The adapter stores the
  raw value as `openrca.duration`, emits a zero-length OTLP span, and does not
  use the value as latency.
- Bank `cmdb_id` identifies a pod-level component, but the source does not
  provide an OTel resource identity convention. The adapter stores it as the
  span attribute `openrca.cmdb_id`; it does not relabel it as `service.name` or
  `k8s.pod.uid`.
- The current `greptime_trace_v1` path rejects a newly created trace table when
  `service.name` is completely absent. The adapter sends an empty
  `service.name` protocol placeholder. It asserts no service identity and
  produces no graph entity.
- No span operation name, kind, or status exists in the source. The adapter
  emits an empty name, `SPAN_KIND_UNSPECIFIED`, and `STATUS_CODE_UNSET`.
- No-model gate result: 28,354 unique metric samples, 34,047 logs, and 324,321
  spans stored; zero rejected spans; exact protocol row-count match; 347 semantic
  tables; zero graph entities and relationships.

The development incident has a direct metric signal without changing the data:
`Redis02` memory utilization rises during the official window. This makes it a
useful Table Semantics case while preserving an explicit negative boundary for
Semantic Graph.

The protocol v14 evaluation case is `task_5@2021-03-09T09:30`, selected from
single-root task windows by the label-independent hash-ranking contract in
`PLAN.md`. Its isolated gate stored 27,390 metric samples, 97,859 logs, and
825,193 spans with exact row-count agreement. It exposes 363 semantic tables
and an empty graph; the official root cause is a Tomcat component with high CPU
usage.

## OpenRCA 1.0 Market

- Authority, source revision, mirror revision, timezone, and CC BY-NC 4.0
  telemetry license are the same as OpenRCA Bank. Market is a distinct online
  market system rather than another Bank case.
- Before downloading Market telemetry, the benchmark ranked 89 distinct
  single-root half-hour windows with seed `semantic-rca-v16-openrca-market`.
  The selected development case is
  `Market/cloudbed-1@2022-03-21T03:30`; official ground truth is node `node-6`
  with `node disk write I/O consumption` at 03:39:14 UTC+8.
- The selected day contains container, mesh, node, runtime, and service metrics;
  proxy and service logs; and traces. Metrics and logs use seconds while traces
  use milliseconds, as declared by the official OpenRCA prompt.
- The source defines `cmdb_id` differently by file: container metrics encode
  `<node>.<pod>`, traces and logs use pod names, node metrics use node names,
  service metrics add protocol suffixes, and mesh metrics encode connection
  strings. These are not interchangeable identities. The adapter preserves
  them and does not join by string stems.
- Traces contain parent IDs, operation names, a source `type`, and a source
  status code, but the source does not map `type` to OTel client/server span
  kind or declare the duration unit. The adapter emits unspecified span kind,
  unset OTel status, zero duration, and retains the raw fields as attributes.
  It does not manufacture Graph calls edges.
- The isolated no-model gate stored 167,100 metric samples, 138,150 logs, and
  94,062 spans with exact protocol row-count agreement. It exposes 587 semantic
  tables and an empty graph.
- `node-6` read throughput is already about 124 MB/s throughout the pre-fault
  baseline, so it is not the injected change. The frozen `03:30–03:38` write-I/O
  window has eight samples, mean 15.9375, and maximum 64.5. The `03:38–03:41`
  window has three samples, mean 224.5, and maximum 502.5. The largest sample is
  timestamped `03:39:00`, 14 seconds before the official injection record; the
  benchmark preserves both timestamps and uses the declared adjacent window.

## OpenRCA 1.0 Telecom

- Authority, source revision, mirror revision, timezone, and CC BY-NC 4.0
  telemetry license are the same as OpenRCA Bank. Telecom is a distinct
  telecom/database system with no log modality.
- Before downloading Telecom telemetry, the benchmark ranked 51 single-root
  half-hour windows with seed `semantic-rca-v16-openrca-telecom`. The selected
  development case is `Telecom@2020-05-27T05:00`; official ground truth is pod
  `docker_001` with `CPU fault` at 05:09:00 UTC+8.
- The selected day contains application, container, middleware, node, and
  service metrics plus traces. All source timestamps are milliseconds.
- Trace `callType`, parent IDs, `cmdb_id`, database name, and service name are
  retained. No source contract maps call types to OTel client/server roles or
  declares `elapsedTime` units, so the adapter does not synthesize those facts.
- The deployment spreadsheet mentioned by the official project FAQ is absent
  from the pinned telemetry mirror. No reference topology is ingested or used
  for validation.
- The isolated no-model gate stored 49,558 unique metric samples and 888,252
  spans with exact protocol row-count agreement. It exposes 130 semantic tables
  and an empty graph. The adapter records 41 duplicate samples, nine conflicting
  timestamps, and the protocol-required deterministic encoding of invalid OTLP
  identifiers; it does not repair any source row.
- In the untouched source, the frozen `docker_001` `container_cpu_used`
  baseline from `05:00–05:09` has nine samples, mean 8.8889, and maximum 64. The
  `05:13–05:20` incident window has seven samples, mean 77.2857, and maximum 83.
  The trace latency change starts near the official record, while the direct CPU
  shift is delayed by about four minutes. The benchmark records this lag and
  does not move either timestamp.

## AnoMod TrainTicket rejection

The audit used the Zenodo artifact associated with DOI
<https://doi.org/10.5281/zenodo.18342898> and source revision
`fdece0e54d9a0a0d286ed16f9fe52b493be88277`.

The corpus is not accepted for scored RCA:

- The normal run and 12 fault runs have nearly identical API and trace error
  distributions, including roughly 1,570 HTTP 403 responses per case and no
  corresponding 5xx signal.
- The target pod's CPU does not increase in the CPU-stress run.
- Code-injection markers are absent from both trace data and application logs.
- Application logs were collected with `kubectl logs --tail` without
  timestamps; many records begin with stack continuations and cannot be placed
  on a trustworthy incident timeline.
- Java log times appear offset from traces, but the source does not declare a
  timezone that would justify shifting them.

Correcting the clock, relabeling faults, or filling missing evidence would turn
the benchmark into an evaluation of an edited derivative. The corpus is
therefore rejected rather than repaired.

## Aegis FSE 2026 reviewer cohort

- The official project page links the reviewer artifact at Zenodo record
  `19522409` and the source dataset at Zenodo record `17105974`. The pinned
  reviewer archive is `FSE_26_RCA_dataset_study_reviewer.tar.gz`, 91,418,171
  bytes, with MD5 `16f0743b6feb20838f856d6e441e0c7b`.
- The reviewer archive contains a publisher-prepared 10-case Train Ticket
  subset. The audit reads the publisher's `index.parquet`,
  `attributes.parquet`, `labels.parquet`, `injection.json`, and source trace
  Parquet files. It does not use or ingest `causal_graph.json`.
- Every trace window has complete, unique `(trace_id, span_id)` identity and
  native service name, span kind, parent ID, duration, and status. Eight cases
  pass the raw relational source gate. Two cases carry a publisher `.invalid`
  marker and are excluded.
- The raw edge builder pairs a `Client` parent with a `Server` child only when
  `trace_id` matches and the child's `parent_span_id` equals the parent's
  `span_id`. `request_count` counts paired server spans. `error_count` counts
  only paired server spans whose source status is `Error`; an HTTP 5xx value
  does not substitute for the source span status.
- Three cases have two service labels, a structured directed injection
  endpoint, and that exact edge in both source windows. The fixed seed
  `semantic-rca-v1-aegis-transfer` ranks response-body replacement first,
  method replacement second, and request delay third.
- The first candidate is rejected because the source traces do not retain a
  response body value, so no deterministic evidence-support predicate can
  verify the declared random replacement. The second candidate,
  `ts0-ts-security-service-request-replace-method-j6gpxx`, is selected before
  any agent trajectory. Its source-declared edge is
  `ts-security-service -> ts-order-other-service`. All 57 normal paired server
  spans use `GET`; all 758 abnormal client spans use `GET`, while their paired
  server spans use the declared replacement method `OPTIONS`.
- The selected case exposes 38 normal and 30 abnormal raw service-call edges,
  with 11,355 and 4,453 Client-to-Server witnesses. The complete edge sets and
  SHA-256 digests come from `semantic-rca aegis-audit`. The frozen selection and
  reduced audit summary are in `fixtures/reference/aegis-selection.json`.
- The selected case now passes its isolated no-model ingestion gate. OTLP
  metrics accepted 330,036 source points and stored 260,740 rows after the
  source's 82,504 duplicates and 39,731 conflicting identities were left to
  protocol primary-key semantics. Loki stored 50,789 logs. OTLP traces stored
  all 105,867 spans with zero rejection or ID remapping.
- One record in `normal_logs.parquet` is timestamped exactly at
  `NORMAL_END == ABNORMAL_START`. The adapter preserves both its publisher file
  assignment and timestamp, records the half-open boundary mismatch, and does
  not move it into the abnormal source file. All trace spans remain strictly
  inside their declared half-open windows.
- The complete stored raw-span and Graph service-call sets contain 41 edges.
  Their normalized SHA-256 is
  `c468db671b63cfa480c4a12b6539e692aee6cdc94f953b62cbf116aa66f6724a` on
  both sides. The audit uses the minimal whole-minute envelope containing both
  publisher windows, verifies that every source span remains inside the
  original half-open envelope, and applies the Graph implementation's
  client-anchored `-5m/+1h` server scan and join bounds.
- Stored telemetry reproduces the frozen mechanism evidence exactly: 57 normal
  paired Server `GET` spans, 758 abnormal Client `GET` spans, and 758 abnormal
  paired Server `OPTIONS` spans on the declared edge. The evaluator-side fixture
  `fixtures/reference/aegis-transfer-scorer.json` freezes this predicate, the
  directed two-service answer, accepted method-replacement labels, and the
  canonical API runner contract. The agent-facing case exposes neither the
  source case name nor the source fault taxonomy. Raw SQL retains the publisher
  half-open window. Agent-facing Semantic Graph queries use the exact audited
  whole-minute envelope required by `observed_at` binning. The protocol v24
  `deepseek-v4-flash` pilot completed all nine cells with no runner error or budget exhaustion,
  but none met the joint diagnosis and evidence predicate. Its trajectories informed the generic
  protocol v25 hypothesis-triage prompt, so this case is development rather than a fresh
  measurement case under v25. DeepSeek context caching is automatic.

## OpenRCA 2.0 ops-lite

- Paper: <https://arxiv.org/abs/2606.27154>.
- Public artifact: <https://huggingface.co/datasets/anon-ops/ops-lite>, pinned at
  `9ac09981c08ab02a0b923eab7830d778934851a8`.
- Provenance and license status: the artifact matches the paper's anonymized
  500-case submission package, but the paper does not link this repository and
  says an archival public release will follow acceptance. The dataset card says
  Apache-2.0 while the paper says CC-BY-SA 4.0 for the dataset. Pinning the
  revision makes bytes reproducible but does not resolve either issue.
- `otel-demo3-shipping-delay-m6fhpx` is retained only as a development audit.
  Its manifest names `shipping` as the root while `injection.json` lists both
  `shipping` and `quote` as ground-truth services. The previous single-component
  scorer therefore had no valid exact-label contract for it.
- Protocol v14 selects `hs1-geo-pod-failure-drdmjj` by the Hotel Reservation
  hash rule in `PLAN.md`. `hs10-geo-memory-exhaustion-pmtmcp` ranked first but
  was rejected because its conclusion contains no observable alert. The accepted
  case passed the manifest/injection root agreement gate and has contiguous
  five-minute normal and abnormal windows.
- The selected case contains metrics, logs, and traces in both normal and
  abnormal windows.
- The trace artifact contains native service names, trace/span IDs, parent IDs,
  span kinds, status codes, HTTP attributes, and nanosecond timestamps and
  durations. The adapter does not ingest the reference causal graph.
- Metrics retain their source Gauge, Sum, and Histogram group. The artifact does
  not publish Sum temporality or monotonicity, Histogram bucket boundaries, or
  whether each `attr.*` column was originally a resource or data-point
  attribute. The adapter records these losses, uses OTLP's unspecified
  temporality and the required `is_monotonic=false` placeholder, represents a
  Histogram with one exhaustive `+Inf` bucket, and keeps `attr.*` as data-point
  attributes. This means Sum subtype metadata is not suitable for scoring.
- The processed metrics omit dimensions needed to distinguish many points.
  There are 98,017 source points, 52,587 unique identities under the published
  columns, and 5,291 identities with conflicting values. The adapter adds no
  synthetic label; GreptimeDB primary-key behavior remains part of the measured
  ingestion result.

The accepted Hotel Reservation case contains 46,958 metric points, 31,684 logs,
and 36,082 spans. Protocol storage produced 39,869 metric rows after primary-key
semantics; 1,947 published metric identities have conflicting values. In a fresh
exclusive instance it exposes 39 semantic tables, nine service entities, and
eight witnessed service-call edges.
The audited OTel Demo development case remains useful evidence that native traces
can produce service-call edges, but it is not a scored measurement case.

## Amazon PetShop rejection

Amazon Science's PetShop dataset is published with the CLeaR 2024 paper at
<https://proceedings.mlr.press/v236/hardt24a.html>. The source was audited at
revision `2e96f937c4c044b8b4aad03217592cd52e66db5d`.

PetShop is authoritative for component-level causal RCA, but it does not fit
this benchmark's incident contract:

- each fault file has only five metric samples at five-minute intervals;
- the normal baseline is stored in a separate run months away from the fault;
- ground truth identifies a root node but leaves the root metric null; and
- a fault mechanism would have to be inferred from reproduction shell commands.

Using it would change the task to component ranking over sparse snapshots and
would require a hand-authored mechanism label. It is rejected rather than
adapted.
