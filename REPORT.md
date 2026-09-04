# Agent RCA Bench — 2026 report

[Interactive report](https://rca-bench.greptime.com) · [中文](REPORT.zh-CN.md)

## Conclusion

Protocol v34 supports one narrow confirmatory result and two broader descriptive findings.

- In the `raw - split_pillars` family, `claude-fable-5-1` used fewer provider-visible input
  tokens under Raw in all 13 eligible cases. The case-median delta was `-446,252.5` tokens;
  the Holm-adjusted p value was `0.001953125`. This is the only primary endpoint that passed
  correction.
- In the `semantic_graph - raw` family, no primary endpoint passed Holm correction. DeepSeek
  reduced rows and complete-run tool calls in most eligible cases; GPT and Fable had mixed or
  adverse medians; GLM had only three eligible cases. The measurement does not support a
  general end-to-end Semantic Graph efficiency claim.
- Semantic Graph consistently compressed focused retrieval. It reduced rows in every eligible
  Discovery case-model result (`23/23`) and every Graph-retrieval result (`8/8`). Graph-retrieval
  tool calls also fell in all eight eligible case-model results.

Diagnosis counts were `60/112` for Split, `80/112` for Raw, and `77/112` for Graph. These are
descriptive totals, not an accuracy hypothesis test. The result changes across datasets: over
the ten OpenRCA2 cases, Split / Raw / Graph produced `48 / 67 / 72` correct diagnoses; over the
four RCA100 infrastructure-node cases, the totals were `12 / 13 / 5`. Semantic Graph helped
service-level localization in part of the cohort but did not transfer to the infrastructure-node
cases.

| Question | Confirmatory result | Supported interpretation |
| --- | --- | --- |
| Raw vs Split interface bundle | One of eight endpoints passed Holm correction | Raw reduced Fable's provider-visible input; no general cross-model claim |
| Semantic Graph vs Raw | No endpoint passed Holm correction | No general end-to-end efficiency claim |
| Focused retrieval | Rows fell in all eligible micro results | Semantic metadata and relationships compressed retrieval in these tasks |
| Diagnosis | Split 60, Raw 80, Graph 77 correct | Descriptive, model- and dataset-dependent |

## Measurement scope

The report contains 464 completed agent cells:

- 128 fixed-cohort micro-benchmark cells: six Discovery cases, two Graph-retrieval cases, four
  models, two treatments, and two repetitions.
- 336 end-to-end cells: 14 cases, four models, three treatments, and two repetitions.

The three end-to-end treatments are:

- `split_pillars`: Prometheus, Loki, and Tempo through their native query APIs.
- `raw`: metric, log, and trace tables in GreptimeDB through read-only SQL and PromQL.
- `semantic_graph`: the Raw surface plus table semantics, entities, relationships, coverage,
  and Semantic Graph query tools.

`raw - split_pillars` compares complete agent-facing interface bundles. It changes the store,
query languages, and tool surface together; it does not isolate storage topology. Its metrics are
correct-completion tool calls and provider-visible input tokens.

`semantic_graph - raw` isolates the added semantic interface over the same GreptimeDB telemetry.
Its metrics are correct-completion tool calls and rows returned. In both families, negative deltas
favor the first treatment named in the comparison.

Both families, their metrics, and the Holm family size were specified and frozen in
`fixtures/reference/transfer-v34-protocol.json` before any run, and every artifact binds that
fixture by hash. "Pre-specified" here means exactly that; there is no public third-party
registration. Everything outside these two families is descriptive.

Headline efficiency requires a correct diagnosis, at least one citation to a successful,
non-truncated query, no runner error, and no budget exhaustion. Repetition deltas are reduced to
one median per model and case before cross-case summaries. Each confirmatory family applies Holm
correction to eight tests independently.

The cohort holds 14 independent cases, and endpoint eligibility leaves 3-13 cases for an
individual test. Exact sign tests on that many cases have low and discrete power, so a
non-significant result indicates insufficient evidence and does not establish equivalence.

## End-to-end results

| Model | Correct Split / Raw / Graph | Eligible Split / Raw / Graph | Capability Split / Raw / Graph |
| --- | ---: | ---: | ---: |
| `gpt-5.6-sol` | 11 / 22 / 20 | 11 / 22 / 20 | 59.24 / 81.93 / 73.11 |
| `deepseek-v4-pro` | 9 / 18 / 18 | 9 / 18 / 18 | 44.54 / 68.07 / 71.85 |
| `claude-fable-5-1` | 25 / 26 / 23 | 25 / 26 / 23 | 91.60 / 93.28 / 86.55 |
| `glm-5.3` | 15 / 14 / 16 | 12 / 11 / 9 | 61.97 / 59.24 / 67.02 |

Correct and eligible counts cover 28 runs per treatment. For GPT, DeepSeek, and Fable, every
correct diagnosis was eligible. GLM lost eligibility when a final answer lacked an
execution-valid citation, even though the provider run itself completed.

### Raw compared with Split

The table reports `Raw - Split`. Negative values favor Raw.

| Model | Eligible cases | Calls delta | Direction | Input delta | Direction | Holm p, calls / input |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 8 | -6 | 7 / 0 / 1 | -373,281.5 | 7 / 0 / 1 | 0.375 / 0.375 |
| `deepseek-v4-pro` | 5 | -3 | 5 / 0 / 0 | +18,043 | 2 / 0 / 3 | 0.375 / 1.0 |
| `claude-fable-5-1` | 13 | -4 | 11 / 0 / 2 | -446,252.5 | 13 / 0 / 0 | 0.1572265625 / 0.001953125 |
| `glm-5.3` | 7 | -11 | 6 / 0 / 1 | -217,676 | 6 / 0 / 1 | 0.375 / 0.375 |

Direction is fewer / tied / more cases for Raw. Raw needed fewer complete-run tool calls for
29 of 33 eligible model-case results, but the call endpoint did not pass Holm correction for
any model. Provider-visible input was not uniformly lower: DeepSeek's median was positive.

The split arm uses native PromQL, LogQL, and TraceQL interfaces and cannot join signals in one
query. Raw can query all three signal families through one SQL surface. The Fable result therefore
supports an interface-bundle claim for that model configuration, not a claim that one storage
engine alone caused the reduction.

### Semantic Graph compared with Raw

The table reports `Graph - Raw`. Negative values favor Graph.

| Model | Eligible cases | Rows delta | Direction | Calls delta | Direction | Holm p, rows / calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 9 | +22.5 | 4 / 0 / 5 | +2.5 | 4 / 0 / 5 | 1.0 / 1.0 |
| `deepseek-v4-pro` | 10 | -258.5 | 7 / 0 / 3 | -3.75 | 8 / 0 / 2 | 1.0 / 0.875 |
| `claude-fable-5-1` | 13 | +16 | 6 / 0 / 7 | +0.5 | 6 / 0 / 7 | 1.0 / 1.0 |
| `glm-5.3` | 3 | -112 | 2 / 0 / 1 | +9 | 0 / 1 / 2 | 1.0 / 1.0 |

DeepSeek shows the strongest directional benefit: fewer calls in 8 of 10 and fewer rows in 7 of
10 eligible cases. The adjusted results remain non-significant. GPT and Fable do not show a
consistent resource reduction, and GLM's three eligible cases are too few for a stable estimate.

Mechanism-level row effects are descriptive because the cohorts are small and uneven. After
aggregating cases within each mechanism, Graph reduced median rows for every estimable model on
workload restart and CPU saturation. It helped two of three estimable models on call-path delay.
It increased rows for both estimable models on memory pressure and for the single estimable model
on each disk-I/O and host-unavailable mechanism.

## Cases and diagnosis pattern

Opaque IDs isolate the agent input. Source mechanisms and targets appear only in the publication
report.

| Case | Dataset | System | Scope and mechanism | Target | Normal / anomalous samples |
| --- | --- | --- | --- | --- | ---: |
| `001` | OpenRCA2 | Hotel Reservation | Component, workload restart | `user` | 13 / 13 |
| `002` | OpenRCA2 | Hotel Reservation | Component, workload restart | `reservation` | 13 / 15 |
| `003` | OpenRCA2 | Hotel Reservation | Component, workload restart | `user` | 13 / 22 |
| `004` | OpenRCA2 | OTel Demo | Component, workload restart | `product-catalog` | 15 / 12 |
| `005` | OpenRCA2 | Hotel Reservation | Dependency edge, call-path delay | `search -> rate` | 1,359 / 93 |
| `006` | OpenRCA2 | Hotel Reservation | Dependency edge, call-path delay | `search -> rate` | 627 / 80 |
| `007` | OpenRCA2 | OTel Demo | Dependency edge, call-path delay | `shipping -> quote` | 15 / 26 |
| `008` | OpenRCA2 | Hotel Reservation | Component, CPU saturation | `search` | 22 / 45 |
| `009` | OpenRCA2 | Hotel Reservation | Component, CPU saturation | `reservation` | 27 / 29 |
| `010` | OpenRCA2 | Hotel Reservation | Component, memory pressure | `geo` | 42 / 43 |
| `011` | RCA100 | OTel Demo Store | Infrastructure node, CPU saturation | `cn-hongkong.10.0.1.49` | 18 / 19 |
| `012` | RCA100 | OTel Demo Store | Infrastructure node, memory pressure | `cn-hongkong.10.0.1.69` | 8 / 9 |
| `013` | RCA100 | OTel Demo Store | Infrastructure node, disk I/O degradation | `cn-hongkong.10.0.1.55` | N/A |
| `014` | RCA100 | OTel Demo Store | Infrastructure node, host unavailable | `cn-hongkong.10.0.1.65` | N/A |

The dataset split matters. Graph produced five more correct diagnoses than Raw on the ten
OpenRCA2 cases (`72` versus `67`) but eight fewer on the four RCA100 cases (`5` versus `13`).
The semantic interface was available and used in every Graph run, so this reversal is not caused
by an unused treatment. The result suggests that the current semantic surface aligns better with
service and dependency identity than with infrastructure-node RCA; four node cases are not enough
for a general claim.

## Focused micro-benchmarks

Discovery asks the model to find the table and signal carrying incident evidence, then return
evidence from the frozen window. Graph retrieval starts from a known anomalous signal and asks for
the correct direct dependency with executable evidence. Metrics stop at the evidence call; they
are not interchangeable with full-run transfer metrics.

| Model | Discovery eligible | Rows delta | Calls delta | Graph eligible | Rows delta | Calls delta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 6 | -294.75 | +0.5 | 2 | -70.5 | -2.25 |
| `deepseek-v4-pro` | 6 | -307 | -0.5 | 2 | -236.5 | -4 |
| `claude-fable-5-1` | 6 | -116.75 | +0.25 | 2 | -67 | -2.25 |
| `glm-5.3` | 5 | -452.5 | -0.5 | 2 | -180 | -7.5 |

Rows fell in every eligible micro case-model result. Discovery calls were mixed because semantic
lookup can add a step before the evidence query. Graph retrieval reduced both rows and calls for
every model in both cases. Fable's Discovery input still increased by a median `7,129` tokens,
showing that row compression does not guarantee token compression.

## Tool-use audit

The trajectories show that all three intended GreptimeDB capabilities were exercised, but not at
the same frequency:

- Every Graph run made at least one successful `query_semantic_graph` call: 112 of 112 runs and
  230 successful calls in total.
- The Raw and Graph arms made 127 successful SQL `JOIN` calls across 64 runs. Most joined spans
  within `traces` or combined metric tables. Joins that actually span two signal kinds were rare:
  two successful calls across two runs, one by GPT and one by GLM.
- Native PromQL `query` or `query_range` calls appeared in 19 of 224 GreptimeDB-arm runs: 12 Raw
  runs and seven Graph runs. Split used successful PromQL queries in 109 of 112 runs because that
  interface exposes metrics through Prometheus rather than SQL.

The prompt states that `execute_sql` can query metric, log, and trace tables and that GreptimeDB
also exposes PromQL. Models still preferred SQL in the GreptimeDB arms. The benchmark therefore
measures how the frozen agents chose the available interfaces; it does not measure the maximum
performance of hand-designed cross-signal SQL or PromQL plans.

## Capability score and evidence audit

The post-measurement capability index assigns 40 points to location, 40 to root cause, and 5 to an
executed citation. Scores are normalized to 100 over all 84 transfer runs per model; failed or
incorrect runs remain in the denominator. The index is descriptive and is not used for hypothesis
testing.

| Rank | Model | Overall | Split | Raw | Graph |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | `claude-fable-5-1` | 90.48 | 91.60 | 93.28 | 86.55 |
| 2 | `gpt-5.6-sol` | 71.43 | 59.24 | 81.93 | 73.11 |
| 3 | `glm-5.3` | 62.75 | 61.97 | 59.24 | 67.02 |
| 4 | `deepseek-v4-pro` | 61.48 | 44.54 | 68.07 | 71.85 |

Deterministic evidence sufficiency remains a separate audit. The SQL verifier proved required
evidence in 43 Raw and 37 Graph runs. It cannot evaluate native Prometheus, Loki, or Tempo evidence,
so Split is `not estimable`, never a failed proof, and deterministic proof is excluded from the
capability score. Split still has the same headline requirement for at least one successful,
non-truncated cited query.

## Reliability, tokens, and cost

The final 464-cell report contains no runner errors and no budget exhaustion. Four transient
Anthropic connection failures from the first transfer attempt were rerun as the same frozen cells;
only the successful replacements enter the final artifact. Database-query failures remain in the
trajectories and report: GPT 57, DeepSeek 125, Fable 49, and GLM 258.

| Model | Provider-visible input | Uncached | Cache read | Cache write | Output | Reasoning output |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 73,817,172 | 14,685 | 60,936,673 | 12,865,814 | 491,441 | 146,497 |
| `deepseek-v4-pro` | 109,026,459 | 7,793,435 | 101,233,024 | 0 | 1,801,489 | Not separately reported |
| `claude-fable-5-1` | 69,755,036 | 741,472 | 61,126,348 | 7,887,216 | 974,831 | 289,721 |
| `glm-5.3` | 103,210,965 | 5,888,213 | 97,322,752 | 0 | 1,185,652 | 637,041 |

Reasoning is a subset of output and is not added twice. DeepSeek does not report a separate
reasoning breakdown; zero in that field does not mean that the model performed no reasoning.

End-to-end estimated cost covers all 28 executed runs in each treatment, not only eligible pairs.
Every figure is an estimate from the frozen provider rates, not an invoice:

| Model | Split | Raw | Graph | Raw - Split | Graph - Raw |
| --- | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | USD 54.6501 | USD 18.2465 | USD 24.4954 | USD -36.4036 | USD +6.2489 |
| `deepseek-v4-pro` | USD 8.6517 | USD 6.8193 | USD 5.6526 | USD -1.8324 | USD -1.1667 |
| `claude-fable-5-1` | USD 85.6178 | USD 36.8636 | USD 38.2825 | USD -48.7542 | USD +1.4189 |
| `glm-5.3` | CNY 102.1473 | CNY 83.2793 | CNY 86.9650 | CNY -18.8680 | CNY +3.6857 |

Across transfer and micro runs, estimates by billing currency:

- `gpt-5.6-sol`: `USD 98.5912992`
- `deepseek-v4-pro`: `USD 21.875483696`
- `claude-fable-5-1`: `USD 170.028057`
- `glm-5.3`: `CNY 274.949464`

GLM-5.3 is billed in CNY at `8.0 / 2.0 / 28.0` per million input, cache-hit and output tokens,
verified on 2026-09-04 at <https://bigmodel.cn/pricing>. That rate was in force during the run but
was missed when the pricing snapshot was first assembled; filling it in changes no token count and
no model behaviour. Cache storage is billed per million tokens per hour and was a limited-time
free promotion, so no cache-write rate is frozen and a run reporting cache-creation tokens stays
unpriced.

Spend is recorded in the currency it was billed in. The cross-currency total converts at `6.7179`
CNY per USD, verified on 2026-09-03 at <https://tradingeconomics.com/china/currency>, giving
`USD 331.422729` across all four models. An exchange rate is a market quote, not a measurement.

## Dataset attribution and license boundary

- **OpenRCA 1.0.** The six Discovery cases use selected Bank, Market, and
  Telecom source files. The paper credits Junjielong Xu, Qinan Zhang, Zhiqing
  Zhong, Shilin He, Chaoyun Zhang, Qingwei Lin, Dan Pei, Pinjia He, Dongmei
  Zhang, and Qi Zhang. See the [OpenRCA repository](https://github.com/microsoft/OpenRCA),
  [paper](https://openreview.net/forum?id=M4qNIzQYpd), and
  [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/).
- **OpenRCA2 ops-lite.** The two Graph-retrieval and ten end-to-end cases come
  from the [ops-lite dataset](https://huggingface.co/datasets/anon-ops/ops-lite).
  Its dataset card declares Apache-2.0, while its paper declares CC-BY-SA 4.0.
  This project does not resolve that conflict.
- **RCA-100 v1.1.** The source-only selection records 15 aggregate node-fault
  candidate profiles and selects four cases. The dataset citation credits Xidao
  Wen, Haibin Liu, Guiyang Liu, Cheng Zhang, Fang Situ, and Qi Zhou. See the
  [dataset paper](https://arxiv.org/abs/2606.29193), the pinned
  [dataset license](https://www.aiops.cn/gitlab/aiops-live-benchmark/agenticopseval/-/raw/69cf36430b43024d02530c610b1a4738b5c9a7fb/RCA100/LICENSE),
  and [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/).

Agent RCA Bench selects cases, restricts evaluation to frozen case windows, and
maps source formats into its ingestion protocols. Published artifacts contain
sanitized identifiers, derived facts, aggregate measurements, and source
hashes. They contain no source telemetry rows, source archives, topology,
causal graphs, or ground-truth files.

Apache-2.0 applies only to the benchmark's original code, artifact schemas,
report text, and independently derived aggregates. It does not relicense
upstream data or upstream dataset documentation. [DATASETS.md](DATASETS.md)
records the source-level inventory and transformations.

## Artifacts

- [Combined JSON](artifacts/measurement/agent-rca-v34.json)
- [Self-contained HTML](artifacts/measurement/agent-rca-v34.html)
- [Micro artifact](artifacts/measurement/agent-rca-v34-micro.json)
- [Transfer artifact](artifacts/measurement/agent-rca-v34-transfer.json)

The provider trajectories were executed before the public release tag. The tagged tree validates
the sanitized artifacts and deterministically regenerates scoring, aggregates, JSON, and HTML.
Calling a provider again is a replication, not part of report reproduction.
