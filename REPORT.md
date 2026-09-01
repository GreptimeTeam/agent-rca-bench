# Semantic RCA Bench — 2026 report

[Interactive report](https://semantic-rca.greptime.com) · [中文](REPORT.zh-CN.md)

## Conclusion

The measurement shows strong retrieval compression in most focused tasks, especially dependency navigation. It does not show a general end-to-end RCA efficiency or cost improvement.

- In the Discovery micro-benchmark, four of five models reduced rows in every eligible case. Their median case deltas ranged from `-188.5` to `-249.5` rows. `claude-fable-5` regressed in all six cases.
- In the Graph micro-benchmark, every eligible case returned fewer rows. DeepSeek, Fable, and GLM also used fewer tool calls. Opus increased input, output, and cost.
- In the ten fresh end-to-end cases, the effect depended on the model. DeepSeek had a `-1033.5` median row delta and improved 8 of 9 eligible cases. GPT had a `+419` median and regressed in 7 of 10 cases.
- No end-to-end primary metric passed the Holm correction. Only DeepSeek reduced the complete, directly comparable end-to-end spend (`USD 3.5932` Raw versus `USD 3.0628` Graph). The results do not support a general efficiency or cost claim.
- Semantic Graph does not replace model reasoning. Raw produced 88 correct diagnoses and Graph produced 91 across 200 end-to-end runs. This `+3` is descriptive, not an inferred accuracy uplift.

The supported product claim is narrower: Semantic Graph provides retrieval compression and dependency-navigation value, most clearly for call-path delay investigations. Whether that capability becomes a cheaper end-to-end RCA depends on the model's investigation strategy. It can reduce rows while increasing calls, input, or spend.

| Evaluation layer | Observed result | Supported interpretation |
| --- | --- | --- |
| Discovery | Four of five models reduced rows in every eligible case | Semantic metadata usually reduced schema-search output |
| Graph retrieval | Every eligible case returned fewer rows | Graph relationships compressed dependency retrieval |
| End-to-end RCA | No primary metric passed Holm correction | No general efficiency claim over this cohort |
| Diagnosis | Raw 88 correct; Graph 91 correct | Descriptive difference, not inferred accuracy uplift |

## Measurement scope

The report contains 360 completed agent cells:

- 160 fixed-cohort micro-benchmark cells: six Discovery cases, two Graph cases, five models, two treatments, and two repetitions.
- 200 fresh OpenRCA2 end-to-end cells: ten cases, five models, two treatments, and two repetitions.

End-to-end headline efficiency requires a correct diagnosis, at least one citation that resolves to a successful non-truncated query, and reliable execution. The deterministic evidence-sufficiency verifier is a separate audit and does not select the headline sample.

Rows and complete-run tool calls are the two primary end-to-end metrics. Input, output, reasoning, and cost are exploratory; reasoning is a subset of output and is never added twice. Every delta is Semantic Graph minus Raw within the same model and case. Negative values favor Semantic Graph.

## End-to-end RCA

| Model | Correct Raw / Graph | Eligible cases | Rows delta | Calls delta | Input delta | Output delta | Cost delta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 20 / 18 | 10 | +419 | +0.5 | +47,356.75 | -626.25 | USD +0.0951 |
| `deepseek-v4-pro` | 17 / 18 | 9 | -1,033.5 | +0.5 | -120,109 | +412 | USD -0.0122 |
| `claude-opus-5` | 18 / 20 | 10 | -40.25 | -0.75 | +24,552.25 | -560.25 | USD +0.0055 |
| `claude-fable-5` | 19 / 19 | 10 | -83.5 | -1.5 | +85,706 | +29.5 | USD +0.1424 |
| `glm-5.3` | 14 / 16 | 7 | -624 | +2.5 | -180,977 | -352 | Not estimable |

DeepSeek had the strongest row direction. Its unadjusted sign-test result was `p=0.0390625`, but the fixed ten-test Holm adjustment raised it to `0.390625`. Every other rows or calls comparison had a Holm-adjusted value of `1.0`.

Calls and rows did not move together. Input and output also moved in different directions. GPT and Opus used more input but less output; DeepSeek used less input but slightly more output. Applying the frozen provider prices changes the interpretation again: DeepSeek is the only model with a lower median end-to-end cost. GPT, Opus, and Fable cost more under Graph despite lower output for GPT and Opus. GLM has no frozen model-specific price, so its cost effect is not estimable.

## Cases and fault mechanisms

Opaque IDs isolate the agent input. The publication report identifies each source mechanism and target so readers can interpret and reproduce the results.

| Case | System | Mechanism | Fault target | Frozen oracle | Normal / anomalous samples |
| --- | --- | --- | --- | --- | ---: |
| `001` | Hotel Reservation | Workload restart | `user` | restart counter ≥ 1 | 13 / 13 |
| `002` | Hotel Reservation | Workload restart | `reservation` | restart counter ≥ 1 | 13 / 15 |
| `003` | Hotel Reservation | Workload restart | `user` | restart counter ≥ 1 | 13 / 22 |
| `004` | OTel Demo | Workload restart | `product-catalog` | restart counter ≥ 1 | 15 / 12 |
| `005` | Hotel Reservation | Call-path delay | `search → rate` | server start − client start ≥ 500 ms | 1,359 / 93 |
| `006` | Hotel Reservation | Call-path delay | `search → rate` | server start − client start ≥ 500 ms | 627 / 80 |
| `007` | OTel Demo | Call-path delay | `shipping → quote` | server start − client start ≥ 500 ms | 15 / 26 |
| `008` | Hotel Reservation | CPU saturation | `search` | CPU usage ≥ 0.5 | 22 / 45 |
| `009` | Hotel Reservation | CPU saturation | `reservation` | CPU usage ≥ 0.5 | 27 / 29 |
| `010` | Hotel Reservation | Memory pressure | `geo` | working set ≥ 512 MiB | 42 / 43 |

Mechanism is the strongest structural signal in the end-to-end data:

- Call-path delay reduced median rows for all five models, the only mechanism with a consistent retrieval benefit. Cost improved for GPT and Fable, increased for DeepSeek and Opus, and is unavailable for GLM; fewer rows therefore did not imply lower spend.
- Workload restart produced 40 / 40 correct Raw diagnoses and 40 / 40 correct Graph diagnoses, but efficiency direction varied by model.
- CPU saturation increased rows for GPT, Fable, and GLM and reduced them for DeepSeek and Opus.
- The single memory case increased rows for all three estimable models; DeepSeek and GLM had no eligible pair. One case does not support generalization.

| Model | Mechanism | Cases | Rows | Calls | Input | Output | Cost |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| GPT | Restart | 4 | +235 | -2.75 | +47,356.75 | -357 | USD +0.0435 |
| GPT | Delay | 3 | -64 | +1.5 | -54,534 | -1,230 | USD -0.0433 |
| GPT | CPU | 2 | +715 | +0.5 | +398,814.25 | -205 | USD +0.2815 |
| GPT | Memory | 1 | +4,456 | +7 | +404,360 | +1,509 | USD +0.3887 |
| DeepSeek | Restart | 4 | -479.5 | +0.5 | -138,692.25 | -2,196.25 | USD -0.0169 |
| DeepSeek | Delay | 3 | -3,342 | +4 | +94,734 | +412 | USD +0.0106 |
| DeepSeek | CPU | 2 | -897.25 | +4.25 | -149,945.25 | +1,564.75 | USD -0.0403 |
| DeepSeek | Memory | 0 | — | — | — | — | — |
| Opus | Restart | 4 | -0.25 | +0.25 | +24,552.25 | -111.75 | USD +0.0673 |
| Opus | Delay | 3 | -126.5 | +1.5 | +64,352.5 | -101.5 | USD +0.0065 |
| Opus | CPU | 2 | -70.5 | -4.5 | -75,977.25 | -2,502.25 | USD -0.1060 |
| Opus | Memory | 1 | +8 | -3 | +58,730 | -732 | USD +0.0045 |
| Fable | Restart | 4 | -94.75 | +2.25 | +98,354.5 | +326.5 | USD +0.1538 |
| Fable | Delay | 3 | -204 | -1.5 | +10,997 | -1,035.5 | USD -0.0589 |
| Fable | CPU | 2 | +67.75 | -5.75 | +37,812.75 | -4,609.5 | USD -0.1836 |
| Fable | Memory | 1 | +338 | -1.5 | +100,408.5 | -96.5 | USD +0.1987 |
| GLM | Restart | 4 | -807.5 | +0.75 | -176,223.25 | +2,954 | — |
| GLM | Delay | 2 | -7,520.5 | +5 | -294,134.5 | -6,543 | — |
| GLM | CPU | 1 | +2,192 | +4 | +325,102 | +36,585 | — |
| GLM | Memory | 0 | — | — | — | — | — |

## Micro-benchmarks

The three benchmark layers measure different work:

- **Discovery micro-benchmark:** from an unfamiliar schema, find the telemetry table that carries the target component and anomalous signal, then return evidence from the frozen window. It isolates schema discovery and evidence retrieval.
- **Graph micro-benchmark:** start from a known anomalous signal, find the correct service dependency, and confirm it with executable evidence. It isolates topology navigation and relationship retrieval.
- **End-to-end transfer:** localize the component, diagnose the mechanism, and cite evidence from the incident window. It measures the complete RCA path rather than one retrieval operation.

Micro metrics stop at the cited evidence call. End-to-end calls count the complete successful run. These values are not interchangeable.

Discovery row effects were more stable than end-to-end effects. Input, output, and cost show why a single total-token column is insufficient:

| Model | Eligible cases | Rows delta | Calls delta | Input delta | Output delta | Cost delta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 6 | -203 | 0 | -5,332 | -35.25 | USD -0.0210 |
| `deepseek-v4-pro` | 6 | -249.5 | +0.5 | +1,323.25 | +388.5 | USD -0.0004 |
| `claude-opus-5` | 5 | -188.5 | +1 | -8,675.5 | +68 | USD -0.0471 |
| `claude-fable-5` | 6 | +45.75 | -0.25 | +14,707 | -78 | USD +0.1424 |
| `glm-5.3` | 5 | -195 | -0.5 | +1,331 | -621 | Not estimable |

The Graph micro-benchmark has only two cases and supports descriptive claims only. Rows decreased in every eligible case. DeepSeek had median deltas of `-275.75` rows, `-6.25` calls, and `USD -0.0140`; Fable had `-67`, `-3`, and `USD -0.1240`; GPT's single eligible case had `-64`, `-2`, and `USD -0.0217`. Opus increased both input and output, with `USD +0.0772`. GLM cost is not estimable.

## Model results

Structured diagnosis correctness over 40 end-to-end runs was:

1. `gpt-5.6-sol`, `claude-opus-5`, and `claude-fable-5`: 38 / 40.
2. `deepseek-v4-pro`: 35 / 40.
3. `glm-5.3`: 30 / 40.

This is not a configuration-independent model ranking. Each model used a frozen provider transport and reasoning configuration. GPT, DeepSeek, and Fable had no runner errors. Opus had one. GLM had five, mainly from provider output truncation, and completed 26 of 40 runs under the headline validity contract.

Semantic Graph affected diagnosis differently by model. Opus moved from 18 / 20 correct in Raw to 20 / 20 in Graph. DeepSeek moved from 17 / 20 to 18 / 20. GLM moved from 14 / 20 to 16 / 20. Fable was unchanged. GPT moved from 20 / 20 to 18 / 20. Semantic Graph is not a one-way accuracy enhancer.

### Descriptive capability score

The report adds a post-measurement descriptive score. Each run earns points independently:

| Dimension | Item | Points | Rule |
| --- | --- | ---: | --- |
| Location | Causal scope | 10 | Identifies component or dependency-edge scope |
| Location | Causal locus | 30 | Matches the declared component or directed edge |
| Root cause | Fault category | 10 | Matches the fault class |
| Root cause | Mechanism code | 30 | Matches the causal mechanism |
| Evidence | Executed citation | 5 | At least one citation resolves to a successful query |
| Evidence | Deterministic proof | 15 | Cited results pass the frozen evidence verifier |

Runner reliability and efficiency are excluded from the capability score. Raw and Graph each contain 20 runs per model. `Overall = sum(points across all 40 runs) / 40`, which is equivalent to `(Raw score + Graph score) / 2`. Failed runs remain in the denominator; the ranking does not discard difficult or unsuccessful runs.

The structure follows [RCAgentBench's](https://github.com/CSTCloudOps/RCAgentBench/blob/main/eval.py) Location / Type / Explainability weighting and [RCAEval's](https://github.com/phamquiluan/RCAEval/blob/main/RCAEval/benchmark/evaluation.py) separation of service localization from fine-grained root cause. The weights were not tuned to these results. This score is not a pre-registered endpoint and is not used for significance testing.

| Rank | Model | Overall | Raw | Graph | Graph − Raw |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | `gpt-5.6-sol` | 91.88 | 94.00 | 89.75 | -4.25 |
| 2 | `claude-opus-5` | 88.75 | 84.25 | 93.25 | +9.00 |
| 3 | `deepseek-v4-pro` | 87.12 | 84.75 | 89.50 | +4.75 |
| 4 | `claude-fable-5` | 86.88 | 88.25 | 85.50 | -2.75 |
| 5 | `glm-5.3` | 71.88 | 65.75 | 78.00 | +12.25 |

Graph raised the descriptive score for Opus, DeepSeek, and GLM and lowered it for GPT and Fable. GLM had the largest increase but the lowest absolute score and five runner errors, so the increase cannot be interpreted independently of execution reliability.

## Token accounting

Input is provider-visible input. Reasoning is a subset of output and must not be added again. DeepSeek did not report a separate reasoning breakdown; zero in that field does not mean no reasoning occurred.

| Model | Input | Uncached input | Cache read | Cache write | Output | Reasoning output |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| GPT | 15,662,733 | 13,326 | 13,421,728 | 2,227,679 | 244,139 | 65,671 |
| DeepSeek | 30,740,982 | 2,200,182 | 28,540,800 | 0 | 793,541 | Not separately reported |
| Opus | 12,620,958 | — | — | — | 359,718 | 110,588 |
| Fable | 9,618,824 | 570,472 | 8,092,251 | 956,101 | 367,126 | 129,671 |
| GLM | 31,600,908 | 2,219,788 | 29,381,120 | 0 | 1,285,702 | 959,074 |

## Evidence verifier audit

The end-to-end runs contain 179 correct diagnoses. Of 200 runs, 175 satisfy headline efficiency eligibility and 94 pass the stricter deterministic evidence-sufficiency audit.

The gap shows that deterministic verification of arbitrary SQL causal proofs remains incomplete. The artifact retains each citation's scope, lineage, period facts, and rejection reasons, but verifier coverage does not control the primary efficiency sample. No LLM judge changes the headline result.

## Cost and reliability

The 360 cells recorded six runner errors and no budget exhaustion.

| Model | Runs | Runner errors | Failed transfer SQL | Correct Raw / Graph | Eligible Raw / Graph | Graph-only eligible pairs |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| GPT | 72 | 0 | 25 | 20 / 18 | 20 / 18 | 0 |
| DeepSeek | 72 | 0 | 70 | 17 / 18 | 17 / 18 | 2 |
| Opus | 72 | 1 | 17 | 18 / 20 | 18 / 20 | 2 |
| Fable | 72 | 0 | 18 | 19 / 19 | 19 / 19 | 0 |
| GLM | 72 | 5 | 102 | 14 / 16 | 10 / 16 | 8 |

GLM had the most runner errors, failed SQL queries, and Graph-only eligible pairs. These observations are associated, but the experiment does not isolate provider reliability as their cause.

Frozen rates make token composition economically material:

| Model | Uncached input / 1M | Cache read / 1M | Cache write / 1M | Output / 1M |
| --- | ---: | ---: | ---: | ---: |
| GPT | USD 4.00 | USD 0.40 | USD 5.00 | USD 20.00 |
| DeepSeek | USD 1.32 | USD 0.044 | USD 1.32 | USD 3.96 |
| Opus | USD 5.00 | USD 0.50 | USD 6.25 | USD 25.00 |
| Fable | USD 10.00 | USD 1.00 | USD 12.50 | USD 50.00 |
| GLM | Not frozen | Not frozen | Not frozen | Not frozen |

The frozen sources are the [OpenAI model page](https://developers.openai.com/api/docs/models/gpt-5.6-sol), [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing), and [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/).

Output costs five times uncached input for GPT, Opus, and Fable, and three times for DeepSeek. Cache-read input is much cheaper than both. Cost deltas therefore use the provider's per-run priced amount rather than multiplying total tokens by one rate.

Actual spend by treatment includes every executed run, including unsuccessful runs. End-to-end cost is shown separately from the 360-cell total:

| Model | End-to-end Raw | End-to-end Graph | Graph − Raw | All Raw | All Graph |
| --- | ---: | ---: | ---: | ---: | ---: |
| GPT | USD 9.2835 | USD 11.0214 | USD +1.7379 | USD 9.9993 | USD 11.4439 |
| DeepSeek | USD 3.5932 | USD 3.0628 | USD -0.5304 | USD 3.9390 | USD 3.3634 |
| Opus | Not estimable | USD 10.6400 | Not estimable | Not estimable | USD 13.9690 |
| Fable | USD 18.3408 | USD 18.4312 | USD +0.0904 | USD 21.3562 | USD 22.7483 |
| GLM | Not estimable | Not estimable | Not estimable | Not estimable | Not estimable |

Each cell below is the actual `Raw / Graph` spend across both repetitions for that end-to-end case. Values are USD; `n/a` means at least one run or the model price was not fully priceable.

| Case | GPT | DeepSeek | Opus | Fable | GLM |
| --- | ---: | ---: | ---: | ---: | ---: |
| `001` | 0.6681 / 0.6100 | 0.2888 / 0.1997 | 1.1200 / 1.0862 | 1.7317 / 2.2154 | n/a |
| `002` | 0.6948 / 0.9612 | 0.3440 / 0.3196 | 0.8387 / 1.0244 | 1.3570 / 1.5989 | n/a |
| `003` | 0.7555 / 0.6350 | 0.2953 / 0.2521 | 1.1466 / 1.2658 | 1.4722 / 1.8457 | n/a |
| `004` | 0.8636 / 1.0956 | 0.3438 / 0.3910 | 1.0797 / 1.2300 | 1.7544 / 1.9610 | n/a |
| `005` | 1.0791 / 0.9302 | 0.6335 / 0.3256 | 1.0424 / 1.2216 | 1.3688 / 1.2510 | n/a |
| `006` | 0.8401 / 0.9883 | 0.2721 / 0.2934 | 1.5355 / 1.1758 | 1.7266 / 2.0546 | n/a |
| `007` | 1.0580 / 0.9714 | 0.2381 / 0.2981 | 1.0192 / 1.0322 | 2.1005 / 1.2607 | n/a |
| `008` | 0.8585 / 1.1846 | 0.5221 / 0.3737 | n/a | 3.8587 / 2.5219 | n/a |
| `009` | 1.5760 / 2.1462 | 0.2915 / 0.2786 | 0.9368 / 0.7839 | 1.5235 / 1.8771 | n/a |
| `010` | 0.8898 / 1.4988 | 0.3640 / 0.3311 | 1.4311 / 0.8795 | 1.4474 / 1.8449 | n/a |

Reproducible complete cost estimates total `USD 72.8501615`:

- `gpt-5.6-sol`: `USD 21.4431702`
- `deepseek-v4-pro`: `USD 7.3024578`
- `claude-fable-5`: `USD 44.1045335`

The Opus report has one transfer run without a complete cache breakdown, so the frozen pricing contract rejects a partial estimate. GLM has no frozen official model-specific CNY price. Currencies are not converted and unavailable models are excluded; `USD 72.8501615` is therefore not the total experiment cost.

## Artifacts

- [Combined JSON](artifacts/measurement/semantic-rca-v32.json)
- [Self-contained HTML](artifacts/measurement/semantic-rca-v32.html)
- [Micro artifact](artifacts/measurement/semantic-rca-v32-micro.json)
- [Transfer artifact](artifacts/measurement/semantic-rca-v32-transfer.json)

The provider trajectories were executed before the public release tag. The release records that provenance explicitly. The tagged code deterministically validates the sanitized trajectories and reproduces the scoring, aggregates, JSON, and HTML; invoking the providers again is a replication rather than a byte-identical report reproduction.
