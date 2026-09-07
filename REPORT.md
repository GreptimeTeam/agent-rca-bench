# Agent RCA Bench — 2026 report

[Interactive six-model report](artifacts/measurement/agent-rca-v34-six-model.html) ·
[中文](REPORT.zh-CN.md)

## Conclusion

Raw reduced input for Fable and Gemini, but the results do not support a general end-to-end
efficiency gain from Semantic Graph.

- Raw vs Split: two of twelve pre-specified endpoints passed their frozen Holm tests.
  Both measured input reduction; no tool-call endpoint passed.
- Graph vs Raw: no endpoint passed. Non-significance does not establish equivalence.
- Focused retrieval: Graph reduced rows in 35/35 eligible Discovery results and both rows
  and calls in 11/12 Graph-retrieval results. Qwen had one counterexample.

Correct diagnoses were Split 97/168, Raw 130/168, and Graph 124/168. These are descriptive totals.
Graph did better than Raw on service and dependency faults (OpenRCA2), but worse on node faults
(RCA100). This post-measurement breakdown
confounds dataset with node-level faults, so it does not explain the difference.

## Measurement scope

The report covers six model configurations and 696 agent cells:

- 192 fixed-cohort micro-benchmark cells: six Discovery cases, two Graph-retrieval cases, six
  models, two treatments, and two repetitions.
- 504 end-to-end cells: 14 cases, six models, three treatments, and two repetitions.

Each model entry is a complete provider configuration. Reasoning settings are not a shared
cross-provider compute scale; model ranking is descriptive.

Eight Qwen end-to-end runs used a per-provider concurrency limit of 4 instead of
the frozen limit of 2. Their execution metadata is retained in the artifacts.
The measurement does not isolate the effect of this deviation.

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

Both families, their metrics, and their correction scopes were frozen before execution.
Every artifact binds its protocol by hash. "Pre-specified" means a hashed Git fixture, not a
public third-party registration. Results outside these families are descriptive.

Headline efficiency requires a correct diagnosis, at least one citation to a successful,
non-truncated query, no runner error, and no budget exhaustion. Repetition deltas are reduced to
one median per model and case before cross-case summaries. Each confirmatory family applies Holm
correction independently. The frozen family size is 8 for GPT, DeepSeek, Fable, and GLM, and 4
for Gemini and Qwen. The interactive endpoint tables report it as `m`.

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
| `gemini-3.8-flash` | 21 / 25 / 24 | 21 / 25 / 24 | 78.15 / 94.12 / 90.76 |
| `qwen3.8-max-0902` | 16 / 25 / 23 | 16 / 25 / 23 | 69.33 / 92.02 / 85.71 |

Correct and eligible counts cover 28 runs per treatment. For every model except GLM,
all correct diagnoses were eligible. GLM lost eligibility when a final answer lacked an
execution-valid citation, even though the provider run itself completed.

### Raw compared with Split

The table reports `Raw - Split`. Negative values favor Raw.

| Model | Eligible cases | Calls delta | Direction | Input delta | Direction | Exact p, calls / input | Holm p, calls / input |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 8 | -6 | 7 / 0 / 1 | -373,281.5 | 7 / 0 / 1 | 0.0703125 / 0.0703125 | 0.375 / 0.375 |
| `deepseek-v4-pro` | 5 | -3 | 5 / 0 / 0 | +18,043 | 2 / 0 / 3 | 0.0625 / 1.0 | 0.375 / 1.0 |
| `claude-fable-5-1` | 13 | -4 | 11 / 0 / 2 | -446,252.5 | 13 / 0 / 0 | 0.0224609375 / 0.000244140625 | 0.1572265625 / 0.001953125 |
| `glm-5.3` | 7 | -11 | 6 / 0 / 1 | -217,676 | 6 / 0 / 1 | 0.125 / 0.125 | 0.375 / 0.375 |
| `gemini-3.8-flash` | 11 | 0 | 5 / 1 / 5 | -1,575,917 | 10 / 0 / 1 | 1.0 / 0.01171875 | 1.0 / 0.046875 |
| `qwen3.8-max-0902` | 9 | -8.5 | 7 / 0 / 2 | -370,741 | 5 / 0 / 4 | 0.1796875 / 1.0 | 0.5390625 / 1.0 |

Direction is fewer / tied / more cases for Raw. Raw needed fewer complete-run tool calls for
41 of 53 eligible model-case results, but the call endpoint did not pass Holm correction for
any model. Provider-visible input was not uniformly lower: DeepSeek's median was positive.

The split arm uses native PromQL, LogQL, and TraceQL interfaces and cannot join signals in one
query. Raw can query all three signal families through one SQL surface. The Fable and Gemini results
support an interface-bundle claim for those configurations, not a claim that one storage
engine alone caused the reduction.

The full input workload was lower under Raw for all six models. These totals cover all 28 runs
per arm, including incorrect diagnoses; they are descriptive and differ from the eligibility-filtered,
case-median endpoint above.

| Model | Split input tokens | Raw input tokens | Raw reduction |
| --- | ---: | ---: | ---: |
| `gpt-5.6-sol` | 38,918,260 | 13,648,607 | 64.93% |
| `deepseek-v4-pro` | 39,749,142 | 38,039,654 | 4.30% |
| `claude-fable-5-1` | 37,539,536 | 15,827,965 | 57.84% |
| `glm-5.3` | 37,352,298 | 32,011,651 | 14.30% |
| `gemini-3.8-flash` | 149,985,955 | 68,462,777 | 54.35% |
| `qwen3.8-max-0902` | 40,844,825 | 31,832,932 | 22.06% |

Qwen's estimated end-to-end cost was 25.52% lower under Raw than Split. Gemini's exact
frozen-rate cost is not estimable because its returned usage does not consistently separate
cached input from uncached input. Lower input volume alone does not establish the same cost reduction.

### Semantic Graph compared with Raw

The table reports `Graph - Raw`. Negative values favor Graph.

| Model | Eligible cases | Rows delta | Direction | Calls delta | Direction | Exact p, rows / calls | Holm p, rows / calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 9 | +22.5 | 4 / 0 / 5 | +2.5 | 4 / 0 / 5 | 1.0 / 1.0 | 1.0 / 1.0 |
| `deepseek-v4-pro` | 10 | -258.5 | 7 / 0 / 3 | -3.75 | 8 / 0 / 2 | 0.34375 / 0.109375 | 1.0 / 0.875 |
| `claude-fable-5-1` | 13 | +16 | 6 / 0 / 7 | +0.5 | 6 / 0 / 7 | 1.0 / 1.0 | 1.0 / 1.0 |
| `glm-5.3` | 3 | -112 | 2 / 0 / 1 | +9 | 0 / 1 / 2 | 1.0 / 0.5 | 1.0 / 1.0 |
| `gemini-3.8-flash` | 12 | -138.75 | 8 / 0 / 4 | -1 | 8 / 0 / 4 | 0.3876953125 / 0.3876953125 | 1.0 / 1.0 |
| `qwen3.8-max-0902` | 12 | -381.75 | 9 / 0 / 3 | -1.5 | 8 / 0 / 4 | 0.14599609375 / 0.3876953125 | 0.583984375 / 1.0 |

DeepSeek used fewer calls in 8 of 10 and fewer rows in 7 of 10 eligible cases. Gemini and Qwen
also had negative case medians for both metrics, but none passed correction. GPT and Fable do not show a
consistent resource reduction, and GLM's three eligible cases are too few for a stable estimate.

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

In this post-measurement breakdown, Graph produced six more correct diagnoses than Raw on the ten
OpenRCA2 cases (`112` versus `106`) but twelve fewer on the four RCA100 cases (`12` versus `24`).
All infrastructure-node cases come from RCA100, so source and fault level are fully confounded;
neither can be credited with the reversal. Every Graph run used the semantic interface, but four
node cases are insufficient for a general explanation of the gap.

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
| `gemini-3.8-flash` | 6 | -301 | -1.5 | 2 | -92.75 | -7.25 |
| `qwen3.8-max-0902` | 6 | -130.75 | -0.25 | 2 | -207 | -0.5 |

Discovery rows fell in all 35 eligible case-model results. Graph retrieval reduced both rows and
calls in 11 of 12 results; one Qwen case increased both. Discovery calls were mixed because semantic
lookup can add a step before the evidence query. Fable's Discovery input increased by a median
`7,129` tokens. Qwen's median total-token deltas were `+3,443.75` in Discovery and `+8,031.25` in
Graph retrieval. Row compression does not guarantee token compression.

## Tool-use audit

The trajectories show that all three intended GreptimeDB capabilities were exercised, but not at
the same frequency:

- Every Graph run made at least one successful `query_semantic_graph` call: 168 of 168 runs and
  369 successful calls in total.
- The Raw and Graph arms made 192 successful SQL `JOIN` calls across 92 runs. Most joined spans
  within `traces` or combined metric tables. Joins that actually span two signal kinds were rare:
  three successful calls across three runs.
- Native PromQL `query` or `query_range` calls appeared in 22 of 336 GreptimeDB-arm runs: 15 Raw
  runs and seven Graph runs. Split used successful PromQL queries in 165 of 168 runs because that
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
| 2 | `gemini-3.8-flash` | 87.68 | 78.15 | 94.12 | 90.76 |
| 3 | `qwen3.8-max-0902` | 82.35 | 69.33 | 92.02 | 85.71 |
| 4 | `gpt-5.6-sol` | 71.43 | 59.24 | 81.93 | 73.11 |
| 5 | `glm-5.3` | 62.75 | 61.97 | 59.24 | 67.02 |
| 6 | `deepseek-v4-pro` | 61.48 | 44.54 | 68.07 | 71.85 |

Deterministic evidence sufficiency remains a separate audit. The SQL verifier proved required
evidence in 71 Raw and 58 Graph runs. It cannot evaluate native Prometheus, Loki, or Tempo evidence,
so Split is `not estimable`, never a failed proof, and deterministic proof is excluded from the
capability score. Split still has the same headline requirement for at least one successful,
non-truncated cited query.

## Reliability, tokens, and cost

The 696-cell measurement contains no runner errors and no budget exhaustion. End-to-end
database-query failures remain in the trajectories and report: GPT 57, DeepSeek 125, Fable 49,
GLM 258, Gemini 114, and Qwen 138. The usage table combines 32 micro and 84 end-to-end runs per model.

| Model | Provider-visible input | Uncached | Cache read | Cache write | Output | Reasoning output |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 73,817,172 | 14,685 | 60,936,673 | 12,865,814 | 491,441 | 146,497 |
| `deepseek-v4-pro` | 109,026,459 | 7,793,435 | 101,233,024 | 0 | 1,801,489 | Not separately reported |
| `claude-fable-5-1` | 69,755,036 | 741,472 | 61,126,348 | 7,887,216 | 974,831 | 289,721 |
| `glm-5.3` | 103,210,965 | 5,888,213 | 97,322,752 | 0 | 1,185,652 | 637,041 |
| `gemini-3.8-flash` | 295,802,466 | N/A | N/A | N/A | 1,836,996 | 1,429,124 |
| `qwen3.8-max-0902` | 107,979,385 | N/A | N/A | N/A | 2,404,662 | 1,642,750 |

Reasoning is a subset of output and is not added twice. DeepSeek does not report a separate
reasoning breakdown; zero in that field does not mean that the model performed no reasoning.
Gemini's missing thinking breakdown is reconstructed from provider-reported total minus input
and visible completion tokens. Missing cache fields remain N/A, not zero. Qwen's end-to-end
cache breakdown is complete, but its micro runs omit cache-creation counts, so the combined
breakdown and combined cost are not estimable. Gemini's incomplete cache breakdown also prevents
a complete cache-aware cost estimate. No combined micro-plus-end-to-end six-model cost is reported.

End-to-end estimated cost covers all 28 executed runs in each treatment, not only eligible pairs.
Every figure is an estimate from the frozen provider rates, not an invoice:

| Model | Split | Raw | Graph | Raw - Split | Graph - Raw |
| --- | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | USD 54.6501 | USD 18.2465 | USD 24.4954 | USD -36.4036 | USD +6.2489 |
| `deepseek-v4-pro` | USD 8.6517 | USD 6.8193 | USD 5.6526 | USD -1.8324 | USD -1.1667 |
| `claude-fable-5-1` | USD 85.6178 | USD 36.8636 | USD 38.2825 | USD -48.7542 | USD +1.4189 |
| `glm-5.3` | CNY 102.1473 | CNY 83.2793 | CNY 86.9650 | CNY -18.8680 | CNY +3.6857 |
| `gemini-3.8-flash`* | USD 114.7369 | USD 53.3436 | USD 58.6155 | USD -61.3932 | USD +5.2719 |
| `qwen3.8-max-0902` | USD 18.6539 | USD 13.8941 | USD 14.3862 | USD -4.7598 | USD +0.4921 |

*Gemini is a conservative estimate: all input is charged at the frozen ordinary input
rate, without cache discounts; output includes reasoning. These figures and their differences
are not billed spend. Other models retain their reported cache usage in the cost calculation.
The unified page's end-to-end total includes this Gemini estimate and labels that basis.

Qwen's USD figures use the frozen rate of `6.7787` CNY per USD, dated 2026-09-04
([SAFE](https://www.safe.gov.cn/AppStructured/hlw/RMBQuery.do)). Native Split / Raw / Graph
subtotals remain `CNY 126.449341 / 94.183764 / 97.519404` in the artifacts. This is an exchange-rate
conversion, not a change to the billed currency.

Qwen's frozen rates are CNY `12 / 15 / 1 / 36` per million ordinary input, cache creation,
cache read, and output tokens, verified on 2026-09-06
([Model Studio](https://help.aliyun.com/zh/model-studio/qwen3-8-max)). Gemini's rates are USD
`0.75 / 0.075 / 3.75` per million ordinary input, cached input, and output including thinking,
verified on 2026-09-06 ([Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)).
The rates alone cannot fill missing usage fields.

Complete micro-plus-end-to-end estimates, by billing currency:

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

Spend is recorded in the currency it was billed in. Complete micro-plus-end-to-end
estimates are available for four of the six models; no all-model total is reported.
GLM's USD estimates use `6.7179` CNY per USD, verified on 2026-09-03 at
<https://tradingeconomics.com/china/currency>.

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

The [JSON](artifacts/measurement/agent-rca-v34-six-model.json) binds the source artifacts and
protocols by hash. The [HTML](artifacts/measurement/agent-rca-v34-six-model.html) renders the
six-model results. [README.md](README.md#reproduce-the-published-report) documents reproduction.

The provider trajectories were executed before the public release tag. The tagged tree validates
the sanitized artifacts and deterministically regenerates scoring, aggregates, JSON, and HTML.
Calling a provider again is a replication, not part of report reproduction.

Measurement updated at: `2026-09-07T01:35:19Z` (UTC).

Report generated at: `2026-09-07T02:01:15Z` (UTC).
