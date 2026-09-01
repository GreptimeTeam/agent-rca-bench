# Semantic RCA Bench — 2026 年报告

[交互式报告](https://semantic-rca.greptime.com) · [English](REPORT.md)

## 结论

本轮测量显示，Semantic Graph 在多数聚焦任务中能明显压缩检索数据，依赖导航场景最稳定；但没有证明它能普遍提升端到端 RCA 效率或降低成本。

- Discovery micro-benchmark 中，5 个模型有 4 个在所有合格 case 上减少了返回行数，case-median 降幅为 `188.5–249.5` 行；`claude-fable-5` 在 6 个 case 上全部增加。
- Graph micro-benchmark 中，所有合格 case 都减少了返回行数；DeepSeek、Fable 和 GLM 同时减少了工具调用，Opus 的 input、output 和成本则全部增加。
- 10 个 fresh 端到端 case 中，效果依赖模型和故障机制。DeepSeek 的行数中位差为 `-1033.5`，9 个合格 case 中 8 个改善；GPT 为 `+419`，10 个 case 中 7 个退化。
- 端到端主要指标均未通过 Holm 校正。只有 DeepSeek 的完整、可直接比较端到端实际成本下降：Raw `USD 3.5932`，Graph `USD 3.0628`。不能声称 Semantic Graph 普遍提升效率或降低成本。
- Raw 在 200 次端到端运行中得到 88 次正确诊断，Graph 得到 91 次。`+3` 只能作描述，不能解释为已证明的正确率提升。

可以支持的产品结论更窄：Semantic Graph 提供检索压缩和依赖导航价值，其中调用路径延迟最明确。能否转化为更便宜的端到端 RCA，取决于模型的调查策略；Graph 可以减少 rows，同时增加 calls、input 或实际成本。

| 评测层 | 观测结果 | 可以支持的解释 |
| --- | --- | --- |
| Discovery | 五个模型中四个在所有合格 case 上减少 rows | 语义元数据通常能减少 schema 搜索输出 |
| Graph retrieval | 所有合格 case 都减少 rows | Graph 关系压缩了依赖检索 |
| 端到端 RCA | 没有主要指标通过 Holm 校正 | 不能在该 cohort 上声称普遍效率提升 |
| 诊断 | Raw 正确 88 次，Graph 正确 91 次 | 描述性差异，不构成准确率提升推断 |

## 测量范围

报告包含 360 个完成的 agent cells：

- 160 个固定 cohort 的 micro-benchmark cells：6 个 Discovery case、2 个 Graph case、5 个模型、2 个 treatment、2 次重复。
- 200 个 fresh OpenRCA2 端到端 cells：10 个 case、5 个模型、2 个 treatment、2 次重复。

端到端主要效率样本要求诊断正确、至少一条 citation 对应成功且未截断的查询，并且运行可靠。确定性 evidence-sufficiency verifier 是独立审计，不决定主要效率样本。

主要端到端指标是返回行数和完整运行的工具调用数。Input、output、reasoning 和成本只作探索性分析；reasoning 是 output 的子集，不能重复相加。所有差值均为同一模型、同一 case 下的 `Semantic Graph − Raw`，负数表示 Graph 更省。

## 端到端 RCA

| 模型 | 正确诊断 Raw / Graph | 合格 case | Rows 差 | Calls 差 | Input 差 | Output 差 | 成本差 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 20 / 18 | 10 | +419 | +0.5 | +47,356.75 | -626.25 | USD +0.0951 |
| `deepseek-v4-pro` | 17 / 18 | 9 | -1,033.5 | +0.5 | -120,109 | +412 | USD -0.0122 |
| `claude-opus-5` | 18 / 20 | 10 | -40.25 | -0.75 | +24,552.25 | -560.25 | USD +0.0055 |
| `claude-fable-5` | 19 / 19 | 10 | -83.5 | -1.5 | +85,706 | +29.5 | USD +0.1424 |
| `glm-5.3` | 14 / 16 | 7 | -624 | +2.5 | -180,977 | -352 | 不可估算 |

DeepSeek 的返回行数方向最强。它的未校正 sign-test 为 `p=0.0390625`，固定 10 项检验的 Holm 校正后为 `0.390625`。其他 rows 或 calls 比较的 Holm 校正值均为 `1.0`。

Rows 和 calls 没有同步变化，input 和 output 也可能反向。GPT、Opus 使用了更多 input、但生成了更少 output；DeepSeek 使用更少 input、但 output 略增。按冻结 provider 价格加权后，只有 DeepSeek 的端到端成本中位差为负。GPT、Opus 和 Fable 的 Graph 成本更高，其中 GPT 和 Opus 即使 output 减少，增加的 input 与缓存组成仍使成本上升。GLM 没有冻结的模型级价格，无法估算成本差。

## Case 与故障机制

Opaque ID 只用于隔离 agent 输入。发布报告列出 source case、机制和目标，便于理解与复现。

| Case | 系统 | 机制 | 故障目标 | 冻结 oracle | Normal / Abnormal samples |
| --- | --- | --- | --- | --- | ---: |
| `001` | Hotel Reservation | workload restart | `user` | restart counter ≥ 1 | 13 / 13 |
| `002` | Hotel Reservation | workload restart | `reservation` | restart counter ≥ 1 | 13 / 15 |
| `003` | Hotel Reservation | workload restart | `user` | restart counter ≥ 1 | 13 / 22 |
| `004` | OTel Demo | workload restart | `product-catalog` | restart counter ≥ 1 | 15 / 12 |
| `005` | Hotel Reservation | call-path delay | `search → rate` | server start − client start ≥ 500 ms | 1,359 / 93 |
| `006` | Hotel Reservation | call-path delay | `search → rate` | server start − client start ≥ 500 ms | 627 / 80 |
| `007` | OTel Demo | call-path delay | `shipping → quote` | server start − client start ≥ 500 ms | 15 / 26 |
| `008` | Hotel Reservation | CPU saturation | `search` | CPU usage ≥ 0.5 | 22 / 45 |
| `009` | Hotel Reservation | CPU saturation | `reservation` | CPU usage ≥ 0.5 | 27 / 29 |
| `010` | Hotel Reservation | memory pressure | `geo` | working set ≥ 512 MiB | 42 / 43 |

机制差异是本轮最强的结构信号：

- `call_path_delay`：5 个模型的 rows 中位差全部为负，是唯一呈现一致检索收益的机制。GPT、Fable 的成本下降，DeepSeek、Opus 的成本上升，GLM 不可估算；少读 rows 不等于少花钱。
- `workload_restart`：40 个 Raw 和 40 个 Graph 运行全部诊断正确，但效率方向随模型变化。Graph 没有稳定减少步骤。
- `cpu_saturation`：GPT、Fable、GLM 的 rows 为正；DeepSeek、Opus 为负。Graph 提供定位信息，但指标证据仍要回到原始时序数据。
- `memory_pressure`：只有 1 个 case。3 个可估计模型的 rows 均增加；DeepSeek 和 GLM 没有合格配对。不能从单 case 外推。

按模型和机制汇总的 rows / calls / input / output / cost 中位差如下：

| 模型 | 机制 | Case | Rows | Calls | Input | Output | 成本 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| GPT | restart | 4 | +235 | -2.75 | +47,356.75 | -357 | USD +0.0435 |
| GPT | delay | 3 | -64 | +1.5 | -54,534 | -1,230 | USD -0.0433 |
| GPT | CPU | 2 | +715 | +0.5 | +398,814.25 | -205 | USD +0.2815 |
| GPT | memory | 1 | +4,456 | +7 | +404,360 | +1,509 | USD +0.3887 |
| DeepSeek | restart | 4 | -479.5 | +0.5 | -138,692.25 | -2,196.25 | USD -0.0169 |
| DeepSeek | delay | 3 | -3,342 | +4 | +94,734 | +412 | USD +0.0106 |
| DeepSeek | CPU | 2 | -897.25 | +4.25 | -149,945.25 | +1,564.75 | USD -0.0403 |
| DeepSeek | memory | 0 | — | — | — | — | — |
| Opus | restart | 4 | -0.25 | +0.25 | +24,552.25 | -111.75 | USD +0.0673 |
| Opus | delay | 3 | -126.5 | +1.5 | +64,352.5 | -101.5 | USD +0.0065 |
| Opus | CPU | 2 | -70.5 | -4.5 | -75,977.25 | -2,502.25 | USD -0.1060 |
| Opus | memory | 1 | +8 | -3 | +58,730 | -732 | USD +0.0045 |
| Fable | restart | 4 | -94.75 | +2.25 | +98,354.5 | +326.5 | USD +0.1538 |
| Fable | delay | 3 | -204 | -1.5 | +10,997 | -1,035.5 | USD -0.0589 |
| Fable | CPU | 2 | +67.75 | -5.75 | +37,812.75 | -4,609.5 | USD -0.1836 |
| Fable | memory | 1 | +338 | -1.5 | +100,408.5 | -96.5 | USD +0.1987 |
| GLM | restart | 4 | -807.5 | +0.75 | -176,223.25 | +2,954 | — |
| GLM | delay | 2 | -7,520.5 | +5 | -294,134.5 | -6,543 | — |
| GLM | CPU | 1 | +2,192 | +4 | +325,102 | +36,585 | — |
| GLM | memory | 0 | — | — | — | — | — |

## Micro-benchmark

三个 benchmark 层次测量的任务不同：

- **Discovery micro-benchmark：**面对未知 schema，找到承载目标组件和异常信号的正确遥测表，并返回冻结窗口内的证据。它隔离测量 schema discovery 和证据检索成本。
- **Graph micro-benchmark：**从已知异常信号出发，找到正确的服务依赖边，并用可执行证据确认。它隔离测量拓扑导航和关系检索成本。
- **End-to-end transfer：**从事故窗口和工具开始，完成组件定位、机制诊断和证据引用。它测量完整 RCA，而不是单次检索操作。

Micro 指标统计到命中证据的调用为止；端到端 calls 统计完整的成功运行。两者不能混用。

Discovery 的行数效果比端到端稳定。Input、output 和成本拆分也说明，单一 total-token 列不足以表达经济效果：

| 模型 | 合格 case | Rows 差 | Calls 差 | Input 差 | Output 差 | 成本差 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 6 | -203 | 0 | -5,332 | -35.25 | USD -0.0210 |
| `deepseek-v4-pro` | 6 | -249.5 | +0.5 | +1,323.25 | +388.5 | USD -0.0004 |
| `claude-opus-5` | 5 | -188.5 | +1 | -8,675.5 | +68 | USD -0.0471 |
| `claude-fable-5` | 6 | +45.75 | -0.25 | +14,707 | -78 | USD +0.1424 |
| `glm-5.3` | 5 | -195 | -0.5 | +1,331 | -621 | 不可估算 |

Graph micro-benchmark 只有 2 个 case，只支持描述性结论。所有合格 case 的 rows 都下降。DeepSeek 的中位差为 `-275.75` rows、`-6.25` calls、`USD -0.0140`；Fable 为 `-67`、`-3`、`USD -0.1240`；GPT 唯一合格 case 为 `-64`、`-2`、`USD -0.0217`。Opus 的 input 和 output 都增加，成本差为 `USD +0.0772`。GLM 成本不可估算。

## 模型能力评分

为便于比较模型能力，报告提供一个测量后定义的描述性评分。每个 run 独立计分：

| 维度 | 评分项 | 分值 | 判定 |
| --- | --- | ---: | --- |
| 定位 | Causal scope | 10 | 识别 component 或 dependency-edge scope |
| 定位 | Causal locus | 30 | 命中正式声明的组件或有向依赖边 |
| 根因 | Fault category | 10 | 命中故障大类 |
| 根因 | Mechanism code | 30 | 命中具体因果机制 |
| 证据 | Executed citation | 5 | 至少一条 citation 对应成功执行的查询 |
| 证据 | Deterministic proof | 15 | 引用结果通过冻结 evidence verifier |

Runner 可靠性和效率不计入能力分。每个模型的 Raw 和 Graph 各有 20 个 runs。`Overall = 40 个 runs 的总得分 / 40`，数学上等于 `(Raw 分 + Graph 分) / 2`。失败 run 仍留在分母中，不会因为运行失败或答案错误而从排名样本中删除。

这个权重没有按本轮结果调参。它对应 [RCAgentBench](https://github.com/CSTCloudOps/RCAgentBench/blob/main/eval.py) 的 Location 40% / Type 40% / Explainability 20% 结构，也保持了 [RCAEval](https://github.com/phamquiluan/RCAEval/blob/main/RCAEval/benchmark/evaluation.py) 将服务定位与细粒度根因分开评估的原则。该分数不是预注册终点，不参与显著性检验。

| 排名 | 模型 | 总分 | Raw | Graph | Graph − Raw |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | `gpt-5.6-sol` | 91.88 | 94.00 | 89.75 | -4.25 |
| 2 | `claude-opus-5` | 88.75 | 84.25 | 93.25 | +9.00 |
| 3 | `deepseek-v4-pro` | 87.12 | 84.75 | 89.50 | +4.75 |
| 4 | `claude-fable-5` | 86.88 | 88.25 | 85.50 | -2.75 |
| 5 | `glm-5.3` | 71.88 | 65.75 | 78.00 | +12.25 |

Graph 对 Opus、DeepSeek 和 GLM 的描述性能力分有帮助，对 GPT 和 Fable 没有。GLM 的增幅最大，但绝对分仍最低，而且存在 5 次 runner error，不能把增幅单独解释为模型能力。

## Token 使用

`input` 是 provider-visible input；`reasoning` 是 `output` 的子集，不能重复相加。DeepSeek 没有独立 reasoning breakdown，不表示它没有推理。

| 模型 | Input | Uncached input | Cache read | Cache write | Output | Reasoning output |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| GPT | 15,662,733 | 13,326 | 13,421,728 | 2,227,679 | 244,139 | 65,671 |
| DeepSeek | 30,740,982 | 2,200,182 | 28,540,800 | 0 | 793,541 | 未单列 |
| Opus | 12,620,958 | — | — | — | 359,718 | 110,588 |
| Fable | 9,618,824 | 570,472 | 8,092,251 | 956,101 | 367,126 | 129,671 |
| GLM | 31,600,908 | 2,219,788 | 29,381,120 | 0 | 1,285,702 | 959,074 |

缓存命中显著降低了 GPT、DeepSeek、Fable 和 GLM 的未缓存输入。Opus 有一个 transfer run 缺少完整 cache breakdown，因此不能按冻结计价契约给出部分成本估算。

## 执行可靠性与成本

| 模型 | Runs | Runner errors | Transfer SQL 失败 | 正确诊断 Raw / Graph | 合格 Raw / Graph | 仅 Graph 合格 pair |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| GPT | 72 | 0 | 25 | 20 / 18 | 20 / 18 | 0 |
| DeepSeek | 72 | 0 | 70 | 17 / 18 | 17 / 18 | 2 |
| Opus | 72 | 1 | 17 | 18 / 20 | 18 / 20 | 2 |
| Fable | 72 | 0 | 18 | 19 / 19 | 19 / 19 | 0 |
| GLM | 72 | 5 | 102 | 14 / 16 | 10 / 16 | 8 |

360 个 cells 共记录 6 次 runner error、0 次 budget exhaustion。GLM 同时具有最多的 runner error、SQL 失败和 Graph-only eligible pairs；这些指标相关，但本实验不能把差异因果归于 provider 可靠性。

冻结价格说明 token 组成会实质影响成本：

| 模型 | Uncached input / 1M | Cache read / 1M | Cache write / 1M | Output / 1M |
| --- | ---: | ---: | ---: | ---: |
| GPT | USD 4.00 | USD 0.40 | USD 5.00 | USD 20.00 |
| DeepSeek | USD 1.32 | USD 0.044 | USD 1.32 | USD 3.96 |
| Opus | USD 5.00 | USD 0.50 | USD 6.25 | USD 25.00 |
| Fable | USD 10.00 | USD 1.00 | USD 12.50 | USD 50.00 |
| GLM | 未冻结 | 未冻结 | 未冻结 | 未冻结 |

冻结价格来源为 [OpenAI 模型页](https://developers.openai.com/api/docs/models/gpt-5.6-sol)、[Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing) 和 [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/)。

GPT、Opus、Fable 的 output 单价是 uncached input 的 5 倍，DeepSeek 是 3 倍；cache-read input 又远低于两者。因此成本差直接使用 provider 按 run 计算的金额，而不是把 total tokens 乘一个统一单价。

实际成本按 treatment 汇总所有已执行 run，包括失败运行。端到端成本与 360-cell 全部成本分开列出：

| 模型 | 端到端 Raw | 端到端 Graph | Graph − Raw | 全部 Raw | 全部 Graph |
| --- | ---: | ---: | ---: | ---: | ---: |
| GPT | USD 9.2835 | USD 11.0214 | USD +1.7379 | USD 9.9993 | USD 11.4439 |
| DeepSeek | USD 3.5932 | USD 3.0628 | USD -0.5304 | USD 3.9390 | USD 3.3634 |
| Opus | 不可估算 | USD 10.6400 | 不可估算 | 不可估算 | USD 13.9690 |
| Fable | USD 18.3408 | USD 18.4312 | USD +0.0904 | USD 21.3562 | USD 22.7483 |
| GLM | 不可估算 | 不可估算 | 不可估算 | 不可估算 | 不可估算 |

下表每格是该端到端 case 两次重复的实际 `Raw / Graph` 成本，单位 USD。`n/a` 表示至少一个 run 或模型价格无法完整计价。

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

可复算的完整成本估算为：

- `gpt-5.6-sol`: `USD 21.4431702`
- `deepseek-v4-pro`: `USD 7.3024578`
- `claude-fable-5`: `USD 44.1045335`
- 同币种完整估算小计：`USD 72.8501615`

`claude-opus-5` 因一个 run 缺少完整 cache breakdown 而不可估算；`glm-5.3` 因未冻结官方、模型级 CNY 单价而不可估算。币种不转换，不把不可估算模型计入小计，因此 `USD 72.8501615` 不是实验总成本。

## Evidence verifier 审计

200 次端到端运行产生 179 次正确诊断，其中 175 次满足主要效率样本资格，94 次通过更严格的确定性 evidence-sufficiency audit。

差距说明：确定性验证任意 SQL 是否构成完整因果证明，覆盖能力仍然有限。Artifact 保留 citation 的 scope、lineage、period facts 和拒绝原因，但 verifier 不控制主要效率样本，也没有 LLM judge 改写主要结果。

## Artifacts

- [Combined JSON](artifacts/measurement/semantic-rca-v32.json)
- [中英文自包含 HTML](artifacts/measurement/semantic-rca-v32.html)
- [Micro artifact](artifacts/measurement/semantic-rca-v32-micro.json)
- [Transfer artifact](artifacts/measurement/semantic-rca-v32-transfer.json)

Provider trajectories 在公开 release tag 之前执行，发布物会明确记录这一来源。Tag 对应的代码能够确定性验证脱敏轨迹，并复现评分、聚合、JSON 和 HTML；重新调用 provider 属于 replication，不是逐字节复现报告的必要条件。
