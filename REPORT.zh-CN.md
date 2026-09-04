# Semantic RCA Bench — 2026 年报告

[交互式报告](https://semantic-rca.greptime.com) · [English](REPORT.md)

## 结论

v34 协议得到一项范围较窄的验证性结果，以及两项范围更广的描述性结果。

- 在 `raw - split_pillars` 检验族中，`claude-fable-5-1` 的 Raw 在全部 13 个合格 case
  上都减少了 provider-visible input，case-median 差值为 `-446,252.5` tokens，Holm 校正
  后的 p 值为 `0.001953125`。这是唯一通过多重比较校正的主要指标。
- 在 `semantic_graph - raw` 检验族中，没有主要指标通过 Holm 校正。DeepSeek 在多数合格
  case 上减少了 rows 和完整运行的工具调用；GPT 和 Fable 的方向混合或中位数变差；GLM
  只有 3 个合格 case。结果不支持 Semantic Graph 普遍提升端到端效率的结论。
- Semantic Graph 在聚焦检索任务中稳定压缩了返回数据：Discovery 的全部合格
  case-model 结果（`23/23`）和 Graph retrieval 的全部结果（`8/8`）都减少了 rows；后者
  的 8 个结果也全部减少了工具调用。

三个 treatment 的正确诊断分别为 Split `60/112`、Raw `80/112`、Graph `77/112`。这些是
描述性总数，不是准确率假设检验。不同数据集的方向不同：10 个 OpenRCA2 case 上，Split /
Raw / Graph 分别正确 `48 / 67 / 72` 次；4 个 RCA100 基础设施节点 case 上，分别为
`12 / 13 / 5`。Semantic Graph 在部分服务级定位中有帮助，但没有迁移到基础设施节点 case。

| 问题 | 验证性结果 | 可以支持的解释 |
| --- | --- | --- |
| Raw 与 Split 接口组合 | 8 项主要指标中 1 项通过 Holm 校正 | Raw 减少了 Fable 的 provider-visible input；不能外推到所有模型 |
| Semantic Graph 与 Raw | 没有主要指标通过 Holm 校正 | 不能声称普遍提升端到端效率 |
| 聚焦检索 | 所有合格 micro 结果都减少 rows | 本轮任务中，语义元数据和关系压缩了检索数据 |
| 诊断 | Split 60、Raw 80、Graph 77 次正确 | 仅作描述，结果依赖模型和数据集 |

## 测量范围

报告包含 464 个完成的 agent cell：

- 128 个固定 cohort 的 micro-benchmark cell：6 个 Discovery case、2 个 Graph-retrieval
  case、4 个模型、2 个 treatment、2 次重复。
- 336 个端到端 cell：14 个 case、4 个模型、3 个 treatment、2 次重复。

三个端到端 treatment 分别是：

- `split_pillars`：通过原生查询 API 使用 Prometheus、Loki 和 Tempo。
- `raw`：通过只读 SQL 和 PromQL 查询 GreptimeDB 中的 metric、log 和 trace 表。
- `semantic_graph`：在 Raw 接口上增加表语义、实体、关系、覆盖范围和 Semantic Graph
  查询工具。

`raw - split_pillars` 比较完整的 agent-facing interface bundle。两臂同时改变存储、查询语言
和工具接口，不能解释为只隔离了存储拓扑。指标是正确完成所需的工具调用和
provider-visible input。

`semantic_graph - raw` 隔离相同 GreptimeDB 遥测数据上的语义接口增量。指标是正确完成
所需的工具调用和返回行数。两个检验族的负差值都表示比较名称左侧的 treatment 更省资源。

两个检验族、各自的指标和 Holm 族大小，在任何 run 开始前就已在
`fixtures/reference/transfer-v34-protocol.json` 中指定并冻结，每份 artifact 都按哈希绑定
该 fixture。本文中的「预先指定」仅指这一点，没有公开的第三方登记。这两个族之外的全部内容
都是描述性的。

主要效率样本要求诊断正确、至少一条 citation 对应成功且未截断的查询、无 runner error、无
budget exhaustion。同一模型和 case 的重复差值先归并为一个中位数，再做跨 case 汇总。两个
验证性检验族各自独立对 8 项检验做 Holm 校正。

队列包含 14 个独立 case，端点合格性筛选后单项检验只剩 3 到 13 个 case。这个规模下的 exact
sign test 检验效能低且取值离散，因此不显著只表示证据不足，不能据此认定两臂等价。

## 端到端结果

| 模型 | 正确 Split / Raw / Graph | 合格 Split / Raw / Graph | 能力分 Split / Raw / Graph |
| --- | ---: | ---: | ---: |
| `gpt-5.6-sol` | 11 / 22 / 20 | 11 / 22 / 20 | 59.24 / 81.93 / 73.11 |
| `deepseek-v4-pro` | 9 / 18 / 18 | 9 / 18 / 18 | 44.54 / 68.07 / 71.85 |
| `claude-fable-5-1` | 25 / 26 / 23 | 25 / 26 / 23 | 91.60 / 93.28 / 86.55 |
| `glm-5.3` | 15 / 14 / 16 | 12 / 11 / 9 | 61.97 / 59.24 / 67.02 |

每个 treatment 的正确和合格计数都覆盖 28 个 run。GPT、DeepSeek 和 Fable 的每次正确诊断
都满足效率样本条件。GLM 的部分最终答案没有执行有效的 citation，因此虽然 provider run 已
完成，仍不能进入效率样本。

### Raw 与 Split

下表计算 `Raw - Split`，负数表示 Raw 更省。

| 模型 | 合格 case | Calls 差 | 方向 | Input 差 | 方向 | Holm p，calls / input |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 8 | -6 | 7 / 0 / 1 | -373,281.5 | 7 / 0 / 1 | 0.375 / 0.375 |
| `deepseek-v4-pro` | 5 | -3 | 5 / 0 / 0 | +18,043 | 2 / 0 / 3 | 0.375 / 1.0 |
| `claude-fable-5-1` | 13 | -4 | 11 / 0 / 2 | -446,252.5 | 13 / 0 / 0 | 0.1572265625 / 0.001953125 |
| `glm-5.3` | 7 | -11 | 6 / 0 / 1 | -217,676 | 6 / 0 / 1 | 0.375 / 0.375 |

方向列依次表示 Raw 更少、相同、更多的 case 数。33 个合格的 model-case 结果中，Raw 有 29 个
减少了完整运行的工具调用，但没有模型的 calls 指标通过 Holm 校正。Input 也不是一致下降：
DeepSeek 的中位差为正。

Split 使用原生 PromQL、LogQL 和 TraceQL 接口，不能在一次查询中 join 多种信号。Raw 可以
通过一个 SQL 接口查询三类信号。因此，Fable 的显著结果支持该模型配置下的接口组合结论，
不能解释为单独由某个存储引擎造成。

### Semantic Graph 与 Raw

下表计算 `Graph - Raw`，负数表示 Graph 更省。

| 模型 | 合格 case | Rows 差 | 方向 | Calls 差 | 方向 | Holm p，rows / calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 9 | +22.5 | 4 / 0 / 5 | +2.5 | 4 / 0 / 5 | 1.0 / 1.0 |
| `deepseek-v4-pro` | 10 | -258.5 | 7 / 0 / 3 | -3.75 | 8 / 0 / 2 | 1.0 / 0.875 |
| `claude-fable-5-1` | 13 | +16 | 6 / 0 / 7 | +0.5 | 6 / 0 / 7 | 1.0 / 1.0 |
| `glm-5.3` | 3 | -112 | 2 / 0 / 1 | +9 | 0 / 1 / 2 | 1.0 / 1.0 |

DeepSeek 的方向最强：10 个合格 case 中 8 个减少 calls，7 个减少 rows，但校正后的结果仍不
显著。GPT 和 Fable 没有一致的资源下降；GLM 只有 3 个合格 case，不足以形成稳定估计。

机制级 rows 结果只作描述，因为 cohort 小且不均衡。按机制内 case 聚合后，Graph 在 workload
restart 和 CPU saturation 上减少了每个可估算模型的 rows；call-path delay 有 2/3 个模型改善；
memory pressure 的 2 个可估算模型都变差；disk I/O 和 host unavailable 各只有 1 个可估算模型，
也都变差。

## Case 与诊断差异

Opaque ID 用于隔离 agent 输入。Source mechanism 和 target 只出现在发布报告中。

| Case | 数据集 | 系统 | Scope 与机制 | Target | Normal / abnormal samples |
| --- | --- | --- | --- | --- | ---: |
| `001` | OpenRCA2 | Hotel Reservation | Component，workload restart | `user` | 13 / 13 |
| `002` | OpenRCA2 | Hotel Reservation | Component，workload restart | `reservation` | 13 / 15 |
| `003` | OpenRCA2 | Hotel Reservation | Component，workload restart | `user` | 13 / 22 |
| `004` | OpenRCA2 | OTel Demo | Component，workload restart | `product-catalog` | 15 / 12 |
| `005` | OpenRCA2 | Hotel Reservation | Dependency edge，call-path delay | `search -> rate` | 1,359 / 93 |
| `006` | OpenRCA2 | Hotel Reservation | Dependency edge，call-path delay | `search -> rate` | 627 / 80 |
| `007` | OpenRCA2 | OTel Demo | Dependency edge，call-path delay | `shipping -> quote` | 15 / 26 |
| `008` | OpenRCA2 | Hotel Reservation | Component，CPU saturation | `search` | 22 / 45 |
| `009` | OpenRCA2 | Hotel Reservation | Component，CPU saturation | `reservation` | 27 / 29 |
| `010` | OpenRCA2 | Hotel Reservation | Component，memory pressure | `geo` | 42 / 43 |
| `011` | RCA100 | OTel Demo Store | Infrastructure node，CPU saturation | `cn-hongkong.10.0.1.49` | 18 / 19 |
| `012` | RCA100 | OTel Demo Store | Infrastructure node，memory pressure | `cn-hongkong.10.0.1.69` | 8 / 9 |
| `013` | RCA100 | OTel Demo Store | Infrastructure node，disk I/O degradation | `cn-hongkong.10.0.1.55` | N/A |
| `014` | RCA100 | OTel Demo Store | Infrastructure node，host unavailable | `cn-hongkong.10.0.1.65` | N/A |

数据集差异很明显。10 个 OpenRCA2 case 上，Graph 比 Raw 多 5 次正确诊断（`72` 对 `67`）；
4 个 RCA100 case 上，Graph 则少 8 次（`5` 对 `13`）。每个 Graph run 都实际使用了语义接口，
因此这个反转不是 treatment 未使用造成的。结果提示当前语义接口更适合服务和依赖身份，而不
是基础设施节点 RCA；4 个节点 case 不足以支持普遍结论。

## 聚焦 micro-benchmark

Discovery 要求模型从未知 schema 中找到承载事故证据的表和信号，再返回冻结窗口内的证据。
Graph retrieval 从已知异常信号出发，查找正确的直接依赖关系并提供可执行证据。Micro 指标只
统计到 evidence call，不能与完整 transfer run 的指标混用。

| 模型 | Discovery 合格 | Rows 差 | Calls 差 | Graph 合格 | Rows 差 | Calls 差 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 6 | -294.75 | +0.5 | 2 | -70.5 | -2.25 |
| `deepseek-v4-pro` | 6 | -307 | -0.5 | 2 | -236.5 | -4 |
| `claude-fable-5-1` | 6 | -116.75 | +0.25 | 2 | -67 | -2.25 |
| `glm-5.3` | 5 | -452.5 | -0.5 | 2 | -180 | -7.5 |

所有合格 micro case-model 结果都减少了 rows。Discovery 的 calls 方向混合，因为语义查找可能
在 evidence query 前增加一步。Graph retrieval 中，4 个模型在 2 个 case 上都同时减少 rows
和 calls。Fable 的 Discovery input 中位数仍增加 `7,129` tokens，说明 rows 减少不保证 token
减少。

## 工具使用审计

三类预期的 GreptimeDB 能力都被使用，但频率相差很大：

- 112 个 Graph run 全部至少执行了一次成功的 `query_semantic_graph`，合计 230 次成功调用。
- Raw 和 Graph 共在 64 个 run 中执行了 127 次成功的 SQL `JOIN`。大部分是 `traces` 内部 span
  join 或多张 metric 表 join。真正跨越两种信号的 join 很少：2 个 run 共 2 次成功调用，GPT 和
  GLM 各一次。
- 224 个 GreptimeDB treatment run 中，只有 19 个使用了成功的原生 PromQL `query` 或
  `query_range`：Raw 12 个，Graph 7 个。Split 有 109/112 个 run 使用成功的 PromQL，因为该
  treatment 只能通过 Prometheus 查询 metrics。

Prompt 已明确说明 `execute_sql` 可以查询 metric、log 和 trace 表，GreptimeDB 也提供 PromQL。
模型在 GreptimeDB treatment 中仍主要选择 SQL。因此，本报告测量的是冻结 agent 对可用接口
的实际选择，不代表人工设计的跨信号 SQL 或 PromQL 查询所能达到的最佳效果。

## 能力评分与证据审计

测量后定义的能力指数给定位 40 分、根因 40 分、已执行 citation 5 分。每个模型的 84 个
transfer run 都留在分母中，分数再归一到 100。该指数只作描述，不参与假设检验。

| 排名 | 模型 | Overall | Split | Raw | Graph |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | `claude-fable-5-1` | 90.48 | 91.60 | 93.28 | 86.55 |
| 2 | `gpt-5.6-sol` | 71.43 | 59.24 | 81.93 | 73.11 |
| 3 | `glm-5.3` | 62.75 | 61.97 | 59.24 | 67.02 |
| 4 | `deepseek-v4-pro` | 61.48 | 44.54 | 68.07 | 71.85 |

确定性 evidence sufficiency 单独审计。SQL verifier 在 43 个 Raw run 和 37 个 Graph run 中证明了
所需证据。它不能评估原生 Prometheus、Loki 或 Tempo evidence，因此 Split 记为
`not estimable`，而不是证明失败；deterministic proof 也不计入能力分。Split 仍需满足相同的主要
效率样本条件：至少有一条成功、未截断且被引用的查询。

## 可靠性、token 与成本

最终 464-cell 报告有 0 个 runner error、0 个 budget exhaustion。Transfer 第一轮出现的 4 个
Anthropic 临时连接错误已按相同冻结 cell 重跑，最终 artifact 只包含成功结果。数据库查询失败
仍保留在轨迹和报告中：GPT 57、DeepSeek 125、Fable 49、GLM 258。

| 模型 | Provider-visible input | Uncached | Cache read | Cache write | Output | Reasoning output |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 73,817,172 | 14,685 | 60,936,673 | 12,865,814 | 491,441 | 146,497 |
| `deepseek-v4-pro` | 109,026,459 | 7,793,435 | 101,233,024 | 0 | 1,801,489 | 未单列 |
| `claude-fable-5-1` | 69,755,036 | 741,472 | 61,126,348 | 7,887,216 | 974,831 | 289,721 |
| `glm-5.3` | 103,210,965 | 5,888,213 | 97,322,752 | 0 | 1,185,652 | 637,041 |

Reasoning 是 output 的子集，不能重复相加。DeepSeek 没有单列 reasoning breakdown；该字段为 0
不表示模型没有推理。

端到端估算成本包含每个 treatment 的全部 28 个 run，不只统计合格 pair。所有数字都是按冻结价格
估算的，不是账单：

| 模型 | Split | Raw | Graph | Raw - Split | Graph - Raw |
| --- | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | USD 54.6501 | USD 18.2465 | USD 24.4954 | USD -36.4036 | USD +6.2489 |
| `deepseek-v4-pro` | USD 8.6517 | USD 6.8193 | USD 5.6526 | USD -1.8324 | USD -1.1667 |
| `claude-fable-5-1` | USD 85.6178 | USD 36.8636 | USD 38.2825 | USD -48.7542 | USD +1.4189 |
| `glm-5.3` | N/A | N/A | N/A | N/A | N/A |

Transfer 和 micro 的完整成本估算合计为 `USD 290.494839896`：

- `gpt-5.6-sol`：`USD 98.5912992`
- `deepseek-v4-pro`：`USD 21.875483696`
- `claude-fable-5-1`：`USD 170.028057`

`paid_execution.pricing_snapshot_required_at_execution` 要求定价快照与运行绑定。2026-08-30 冻结
的快照没有 GLM-5.3 的价格，因为当时 BigModel 定价页尚未列出该模型，因此 GLM-5.3 的支出不属于
测量记录，不计入上述合计，也不出现在任何按接口的成本数字中。

该页面此后公布了 GLM-5.3 的价格：输入、缓存命中、输出分别为每百万 token `8.0 / 2.0 / 28.0`
CNY。把这一价格应用到 run 已记录的 token 计数，估算为 `CNY 274.949464`，combined report 以
`costs.post_hoc_estimates` 单独记录，页面标注为运行之后定价。它不并入测量合计：协议规定执行
时的快照才是记录，事后公布的价格不能追溯成为它的一部分。冻结时缓存存储为限时免费，因此没有
冻结缓存写入费率，返回 cache-creation token 的 run 保持不计价。

成本按计费币种记录。跨币种合计按 `6.7179` CNY 兑 1 USD 换算，汇率于 2026-09-03 从
<https://tradingeconomics.com/china/currency> 冻结，且只在全部模型都可计价时给出，因此这里没有
该合计。汇率是市场报价，不是测量值。

## Artifacts

- [Combined JSON](artifacts/measurement/semantic-rca-v34.json)
- [中英文自包含 HTML](artifacts/measurement/semantic-rca-v34.html)
- [Micro artifact](artifacts/measurement/semantic-rca-v34-micro.json)
- [Transfer artifact](artifacts/measurement/semantic-rca-v34-transfer.json)

Provider trajectory 在公开 release tag 之前执行。对应 tag 的代码可以验证脱敏 artifact，并确定性
重建评分、聚合、JSON 和 HTML。重新调用 provider 属于 replication，不是报告复现的一部分。
