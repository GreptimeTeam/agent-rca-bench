# Dataset Audit

This audit records why each corpus is included or rejected. The benchmark does
not repair timestamps, labels, topology, or root-cause records. A protocol
adapter may convert declared units and identifiers into a wire representation,
but it must record every lossy or synthetic protocol field.

## Portfolio

| Corpus | Role | Status | Semantic Graph evidence |
| --- | --- | --- | --- |
| RCAEval | Established control for code-level failures | Included | Case-dependent; treated as graph-negative unless source identity supports edges |
| RCA100 v1.1 | Native OpenTelemetry and reference topology | Included | Positive: witnessed service calls are available |
| OpenRCA 1.0 Bank | Wide enterprise metric schema and legacy multimodal telemetry | Included | Not applicable: no standard entity identity or span roles |
| OpenRCA 2.0 ops-lite | Native OTel and verified causal paths | Included as a frozen measurement case | Positive: standard client/server spans witness service calls |
| Amazon PetShop | Component-level causal RCA over service metrics | Rejected | Metric-only; no incident-local mechanism label or continuous baseline |
| AnoMod TrainTicket | Independent multimodal microservice corpus | Rejected | Rejection is based on incident evidence quality, not graph coverage |
| Eadro / Nezha | Earlier multimodal RCA corpora | Rejected | Modalities do not provide a reliable shared incident timeline |

## RCA100 v1.1

- Dataset revision: `v1.1`.
- Adapter source revision: `69cf36430b43024d02530c610b1a4738b5c9a7fb`.
- `t001` contains native metrics, logs, traces, events, alerts, an official
  fault label, and a reference topology used only for validation.
- The source contains 26,397 duplicate metric samples and 13 conflicting
  timestamps. The adapter preserves source order and lets Prometheus primary-key
  semantics determine stored values. It does not add labels to disambiguate
  conflicts.
- The source does not publish an injection timestamp. Onset scoring is disabled
  rather than inferred from snapshots.

## RCAEval RE2-OB

- Dataset artifact revision: `afeacb11bcc94dadfd1c8f483ee4377b2b8b614e`.
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

## OpenRCA 1.0 Bank

- Official repository: <https://github.com/microsoft/OpenRCA>, pinned at
  `c1bd4af7f635171a1c31cdd567c07d698dff6abc`.
- Public data mirror: `tracer-cloud/opensre`, pinned at
  `07714872ea2cec77c13f9dec17a688e9df9621d1`.
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

The selected incident has a direct metric signal without changing the data:
`Redis02` memory utilization rises during the official window. This makes it a
useful Table Semantics case while preserving an explicit negative boundary for
Semantic Graph.

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

## OpenRCA 2.0 ops-lite

- Paper: <https://arxiv.org/abs/2606.27154>.
- Public artifact: <https://huggingface.co/datasets/anon-ops/ops-lite>, pinned at
  `9ac09981c08ab02a0b923eab7830d778934851a8`.
- The case-selection rule was frozen from the manifest before telemetry
  inspection: choose a non-hybrid OpenTelemetry Demo case, exclude frontend and
  load-generator roots, maximize propagation depth, and prefer a fault family
  not already represented. This selects
  `otel-demo3-shipping-delay-m6fhpx`.
- Ground truth is `shipping` with `NetworkDelay`; the injection contract targets
  `quote`. The case has a contiguous five-minute normal window and five-minute
  abnormal window, 4,484 standard OTel spans, and a validation-only 15-node,
  18-edge causal graph.
- The selected case has no log records. It is a metrics-and-traces case, not a
  claimed tri-modal case.
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

This case is retained because the selected network fault is supported by native
trace structure and a directly testable `shipping -> quote` call edge. Its
metric collision counts are a hard quality caveat, not hidden preprocessing.

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
