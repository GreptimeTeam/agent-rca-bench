# Agent RCA Bench — 2026 年报告

[六模型交互报告](artifacts/measurement/agent-rca-v34-six-model.html) ·
[English](REPORT.md)

## 结论

Raw 减少了 Fable 和 Gemini 的输入量，但结果不支持 Semantic Graph 普遍提升端到端效率。

- Raw 与 Split：12 项预先指定端点中，2 项通过各自冻结的 Holm 检验，均为输入量指标。
  没有工具调用指标通过校正。
- Graph 与 Raw：没有端点通过校正。不显著不代表两种接口等价。
- 聚焦检索：Graph 在 Discovery 的 35/35 个合格结果中减少 rows，在 Graph retrieval 的
  11/12 个结果中同时减少 rows 和 calls；Qwen 有一个反例。

正确诊断数为 Split 97/168、Raw 130/168、Graph 124/168，仅作描述。Graph 在服务与依赖故障
（OpenRCA2）上优于 Raw，在节点故障（RCA100）上则相反。这是测量后的分组分析，数据集与故障层级混杂，
不能据此解释差异。

## 测量范围

报告覆盖 6 个模型配置、696 个 agent cell：

- 192 个固定 cohort 的 micro-benchmark cell：6 个 Discovery case、2 个 Graph-retrieval
  case、6 个模型、2 个 treatment、2 次重复。
- 504 个端到端 cell：14 个 case、6 个模型、3 个 treatment、2 次重复。

每个模型条目代表完整的 provider 配置。不同 provider 的 reasoning 设置不是统一的计算量
刻度，模型排名仅作描述。

Qwen 的 8 次端到端运行将单个 provider 的并发上限从冻结值 2 调整为 4，
工件保留了逐次执行记录。本次测量未单独检验该偏离的影响。

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

两个检验族、指标和校正范围在运行前冻结，每份 artifact 都按哈希绑定协议。
「预先指定」指 Git 中的哈希 fixture，不是公开的第三方登记。其他结果均为描述性结果。

主要效率样本要求诊断正确、至少一条 citation 对应成功且未截断的查询、无 runner error、无
budget exhaustion。同一模型和 case 的重复差值先归并为一个中位数，再做跨 case 汇总。两个
确认性检验族分别校正。GPT、DeepSeek、Fable、GLM 的冻结校正范围为 8，Gemini、Qwen
为 4；交互报告的端点表以 `m` 列标明。

队列包含 14 个独立 case，端点合格性筛选后单项检验只剩 3 到 13 个 case。这个规模下的 exact
sign test 检验效能低且取值离散，因此不显著只表示证据不足，不能据此认定两臂等价。

## 端到端结果

| 模型 | 正确 Split / Raw / Graph | 合格 Split / Raw / Graph | 能力分 Split / Raw / Graph |
| --- | ---: | ---: | ---: |
| `gpt-5.6-sol` | 11 / 22 / 20 | 11 / 22 / 20 | 59.24 / 81.93 / 73.11 |
| `deepseek-v4-pro` | 9 / 18 / 18 | 9 / 18 / 18 | 44.54 / 68.07 / 71.85 |
| `claude-fable-5-1` | 25 / 26 / 23 | 25 / 26 / 23 | 91.60 / 93.28 / 86.55 |
| `glm-5.3` | 15 / 14 / 16 | 12 / 11 / 9 | 61.97 / 59.24 / 67.02 |
| `gemini-3.8-flash` | 21 / 25 / 24 | 21 / 25 / 24 | 78.15 / 94.12 / 90.76 |
| `qwen3.8-max-0902` | 16 / 25 / 23 | 16 / 25 / 23 | 69.33 / 92.02 / 85.71 |

每个 treatment 的正确和合格计数都覆盖 28 个 run。除 GLM 外，各模型的每次正确诊断
都满足效率样本条件。GLM 的部分最终答案没有执行有效的 citation，因此虽然 provider run 已
完成，仍不能进入效率样本。

### Raw 与 Split

下表计算 `Raw - Split`，负数表示 Raw 更省。

| 模型 | 合格 case | Calls 差 | 方向 | Input 差 | 方向 | Exact p，calls / input | Holm p，calls / input |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 8 | -6 | 7 / 0 / 1 | -373,281.5 | 7 / 0 / 1 | 0.0703125 / 0.0703125 | 0.375 / 0.375 |
| `deepseek-v4-pro` | 5 | -3 | 5 / 0 / 0 | +18,043 | 2 / 0 / 3 | 0.0625 / 1.0 | 0.375 / 1.0 |
| `claude-fable-5-1` | 13 | -4 | 11 / 0 / 2 | -446,252.5 | 13 / 0 / 0 | 0.0224609375 / 0.000244140625 | 0.1572265625 / 0.001953125 |
| `glm-5.3` | 7 | -11 | 6 / 0 / 1 | -217,676 | 6 / 0 / 1 | 0.125 / 0.125 | 0.375 / 0.375 |
| `gemini-3.8-flash` | 11 | 0 | 5 / 1 / 5 | -1,575,917 | 10 / 0 / 1 | 1.0 / 0.01171875 | 1.0 / 0.046875 |
| `qwen3.8-max-0902` | 9 | -8.5 | 7 / 0 / 2 | -370,741 | 5 / 0 / 4 | 0.1796875 / 1.0 | 0.5390625 / 1.0 |

方向列依次表示 Raw 更少、相同、更多的 case 数。53 个合格的 model-case 结果中，Raw 有 41 个
减少了完整运行的工具调用，但没有模型的 calls 指标通过 Holm 校正。Input 也不是一致下降：
DeepSeek 的中位差为正。

Split 使用原生 PromQL、LogQL 和 TraceQL 接口，不能在一次查询中 join 多种信号。Raw 可以
通过一个 SQL 接口查询三类信号。因此，Fable 和 Gemini 的显著结果支持各自配置下的接口组合结论，
不能解释为单独由某个存储引擎造成。

六个模型的 Raw 总输入量均低于 Split。下表覆盖每臂全部 28 次运行，包括错误诊断；
这是描述性总量，不是经过合格性筛选、按 case 取中位数的主要指标。

| 模型 | Split input tokens | Raw input tokens | Raw 降幅 |
| --- | ---: | ---: | ---: |
| `gpt-5.6-sol` | 38,918,260 | 13,648,607 | 64.93% |
| `deepseek-v4-pro` | 39,749,142 | 38,039,654 | 4.30% |
| `claude-fable-5-1` | 37,539,536 | 15,827,965 | 57.84% |
| `glm-5.3` | 37,352,298 | 32,011,651 | 14.30% |
| `gemini-3.8-flash` | 149,985,955 | 68,462,777 | 54.35% |
| `qwen3.8-max-0902` | 40,844,825 | 31,832,932 | 22.06% |

Qwen 的 Raw 端到端估算成本比 Split 低 25.52%。Gemini 返回的 usage 未完整区分缓存与
未缓存输入，因此无法按冻结价格精确估算成本；输入量下降不等于成本同比例下降。

### Semantic Graph 与 Raw

下表计算 `Graph - Raw`，负数表示 Graph 更省。

| 模型 | 合格 case | Rows 差 | 方向 | Calls 差 | 方向 | Exact p，rows / calls | Holm p，rows / calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 9 | +22.5 | 4 / 0 / 5 | +2.5 | 4 / 0 / 5 | 1.0 / 1.0 | 1.0 / 1.0 |
| `deepseek-v4-pro` | 10 | -258.5 | 7 / 0 / 3 | -3.75 | 8 / 0 / 2 | 0.34375 / 0.109375 | 1.0 / 0.875 |
| `claude-fable-5-1` | 13 | +16 | 6 / 0 / 7 | +0.5 | 6 / 0 / 7 | 1.0 / 1.0 | 1.0 / 1.0 |
| `glm-5.3` | 3 | -112 | 2 / 0 / 1 | +9 | 0 / 1 / 2 | 1.0 / 0.5 | 1.0 / 1.0 |
| `gemini-3.8-flash` | 12 | -138.75 | 8 / 0 / 4 | -1 | 8 / 0 / 4 | 0.3876953125 / 0.3876953125 | 1.0 / 1.0 |
| `qwen3.8-max-0902` | 12 | -381.75 | 9 / 0 / 3 | -1.5 | 8 / 0 / 4 | 0.14599609375 / 0.3876953125 | 0.583984375 / 1.0 |

DeepSeek 在 10 个合格 case 中有 8 个减少 calls，7 个减少 rows。Gemini 和 Qwen 的两项
中位差也均为负，但均未通过校正。GPT 和 Fable 没有一致的资源下降；GLM 只有 3 个合格
case，不足以形成稳定估计。

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

在这项测量后的分组分析中，10 个 OpenRCA2 case 上 Graph 比 Raw 多 6 次正确诊断
（`112` 对 `106`）；4 个 RCA100 case 上则少 12 次（`12` 对 `24`）。所有基础设施节点
case 均来自 RCA100，数据来源与故障层级完全混杂，不能把反转归因于其中一个因素。
每个 Graph run 都使用了语义接口，但 4 个节点 case 不足以支持对差异的普遍解释。

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
| `gemini-3.8-flash` | 6 | -301 | -1.5 | 2 | -92.75 | -7.25 |
| `qwen3.8-max-0902` | 6 | -130.75 | -0.25 | 2 | -207 | -0.5 |

Discovery 的 35 个合格 case-model 结果全部减少 rows。Graph retrieval 的 12 个结果中，
11 个同时减少 rows 和 calls，Qwen 的一个 case 两项均增加。Discovery 的 calls 方向混合，
因为语义查找可能在 evidence query 前增加一步。Fable 的 Discovery input 中位差为
`+7,129` tokens；Qwen 的总 token 中位差在 Discovery 和 Graph retrieval 中分别为
`+3,443.75`、`+8,031.25`。Rows 减少不保证 token 减少。

## 工具使用审计

三类预期的 GreptimeDB 能力都被使用，但频率相差很大：

- 168 个 Graph run 全部至少执行了一次成功的 `query_semantic_graph`，合计 369 次成功调用。
- Raw 和 Graph 共在 92 个 run 中执行了 192 次成功的 SQL `JOIN`。大部分是 `traces` 内部 span
  join 或多张 metric 表 join。真正跨越两种信号的 join 很少：3 个 run 共 3 次成功调用。
- 336 个 GreptimeDB treatment run 中，只有 22 个使用了成功的原生 PromQL `query` 或
  `query_range`：Raw 15 个，Graph 7 个。Split 有 165/168 个 run 使用成功的 PromQL，因为该
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
| 2 | `gemini-3.8-flash` | 87.68 | 78.15 | 94.12 | 90.76 |
| 3 | `qwen3.8-max-0902` | 82.35 | 69.33 | 92.02 | 85.71 |
| 4 | `gpt-5.6-sol` | 71.43 | 59.24 | 81.93 | 73.11 |
| 5 | `glm-5.3` | 62.75 | 61.97 | 59.24 | 67.02 |
| 6 | `deepseek-v4-pro` | 61.48 | 44.54 | 68.07 | 71.85 |

确定性 evidence sufficiency 单独审计。SQL verifier 在 71 个 Raw run 和 58 个 Graph run 中证明了
所需证据。它不能评估原生 Prometheus、Loki 或 Tempo evidence，因此 Split 记为
`not estimable`，而不是证明失败；deterministic proof 也不计入能力分。Split 仍需满足相同的主要
效率样本条件：至少有一条成功、未截断且被引用的查询。

## 可靠性、token 与成本

696 个 cell 有 0 个 runner error、0 个 budget exhaustion。端到端数据库查询失败仍保留在
轨迹和报告中：GPT 57、DeepSeek 125、Fable 49、GLM 258、Gemini 114、Qwen 138。
下表合并每个模型的 32 次 micro 和 84 次端到端运行。

| 模型 | Provider-visible input | Uncached | Cache read | Cache write | Output | Reasoning output |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 73,817,172 | 14,685 | 60,936,673 | 12,865,814 | 491,441 | 146,497 |
| `deepseek-v4-pro` | 109,026,459 | 7,793,435 | 101,233,024 | 0 | 1,801,489 | 未单列 |
| `claude-fable-5-1` | 69,755,036 | 741,472 | 61,126,348 | 7,887,216 | 974,831 | 289,721 |
| `glm-5.3` | 103,210,965 | 5,888,213 | 97,322,752 | 0 | 1,185,652 | 637,041 |
| `gemini-3.8-flash` | 295,802,466 | N/A | N/A | N/A | 1,836,996 | 1,429,124 |
| `qwen3.8-max-0902` | 107,979,385 | N/A | N/A | N/A | 2,404,662 | 1,642,750 |

Reasoning 是 output 的子集，不能重复相加。DeepSeek 没有单列 reasoning breakdown；该字段为 0
不表示模型没有推理。
Gemini 缺失的 thinking 明细由 provider 报告的总 token 减去输入和可见输出重建。
缺失的缓存字段保留 N/A，不记为零。Qwen 的端到端缓存明细完整，但 micro 缺少缓存创建
计数，因此合并后的缓存明细与总成本不可估算。Gemini 的缓存明细不完整，也无法完整计价。
报告不提供六模型 micro 与端到端合并后的成本合计。

端到端估算成本包含每个 treatment 的全部 28 个 run，不只统计合格 pair。所有数字都是按冻结价格
估算的，不是账单：

| 模型 | Split | Raw | Graph | Raw - Split | Graph - Raw |
| --- | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | USD 54.6501 | USD 18.2465 | USD 24.4954 | USD -36.4036 | USD +6.2489 |
| `deepseek-v4-pro` | USD 8.6517 | USD 6.8193 | USD 5.6526 | USD -1.8324 | USD -1.1667 |
| `claude-fable-5-1` | USD 85.6178 | USD 36.8636 | USD 38.2825 | USD -48.7542 | USD +1.4189 |
| `glm-5.3` | CNY 102.1473 | CNY 83.2793 | CNY 86.9650 | CNY -18.8680 | CNY +3.6857 |
| `gemini-3.8-flash`* | USD 114.7369 | USD 53.3436 | USD 58.6155 | USD -61.3932 | USD +5.2719 |
| `qwen3.8-max-0902` | USD 18.6539 | USD 13.8941 | USD 14.3862 | USD -4.7598 | USD +0.4921 |

*Gemini 为保守估算：所有输入按冻结的普通输入单价计算，不计缓存折扣；输出包含 reasoning。
这些数字及差值不是账单金额。其他模型仍按已报告的缓存用量计价。统一页面的端到端合计
包含这项 Gemini 估算，并标明计价依据。

Qwen 的美元数字按冻结汇率 `6.7787` CNY 兑 1 USD 换算，日期为 2026-09-04
（[SAFE](https://www.safe.gov.cn/AppStructured/hlw/RMBQuery.do)）。原币 Split / Raw / Graph
小计仍保留为 `CNY 126.449341 / 94.183764 / 97.519404`。换算不改变计费币种。

Qwen 冻结价格为每百万普通输入、缓存创建、缓存读取、输出 token 分别 CNY
`12 / 15 / 1 / 36`，于 2026-09-06 核实
（[百炼定价](https://help.aliyun.com/zh/model-studio/qwen3-8-max)）。Gemini 的普通输入、缓存
输入、含 thinking 的输出分别为每百万 token USD `0.75 / 0.075 / 3.75`，同日核实
（[Gemini API 定价](https://ai.google.dev/gemini-api/docs/pricing)）。价格已知不能补全缺失的用量字段。

可完整计价模型的 transfer 与 micro 成本，按计费币种：

- `gpt-5.6-sol`：`USD 98.5912992`
- `deepseek-v4-pro`：`USD 21.875483696`
- `claude-fable-5-1`：`USD 170.028057`
- `glm-5.3`：`CNY 274.949464`

GLM-5.3 按 CNY 计费，输入、缓存命中、输出分别为每百万 token `8.0 / 2.0 / 28.0`，于 2026-09-04
在 <https://bigmodel.cn/pricing> 核实。该价格在运行期间已经生效，只是最初组装定价快照时遗漏，
补入不改变任何 token 计数和模型行为。缓存存储按每百万 token 每小时计费，当时为限时免费，因此
没有冻结缓存写入费率，返回 cache-creation token 的 run 保持不计价。

成本按计费币种记录。六个模型中，四个可完整计价 micro 与端到端测量，因此不提供全模型合计。
GLM 的美元估算按 `6.7179` CNY 兑 1 USD 换算，汇率于 2026-09-03 在
<https://tradingeconomics.com/china/currency> 核实。

## 数据集署名与许可边界

- **OpenRCA 1.0。**6 个 Discovery case 使用 Bank、Market 和 Telecom 的选定源文件。
  论文作者为 Junjielong Xu、Qinan Zhang、Zhiqing Zhong、Shilin He、Chaoyun Zhang、
  Qingwei Lin、Dan Pei、Pinjia He、Dongmei Zhang 和 Qi Zhang。参见
  [OpenRCA 仓库](https://github.com/microsoft/OpenRCA)、
  [论文](https://openreview.net/forum?id=M4qNIzQYpd)和
  [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)。
- **OpenRCA2 ops-lite。**2 个 Graph-retrieval case 和 10 个端到端 case 来自
  [ops-lite 数据集](https://huggingface.co/datasets/anon-ops/ops-lite)。Dataset card
  声明 Apache-2.0，论文声明 CC-BY-SA 4.0。本项目不解决这项冲突。
- **RCA-100 v1.1。**Source-only selection 记录 15 个节点故障 candidate 的聚合
  profile，并选出 4 个 case。数据集 citation 列出的作者为 Xidao Wen、Haibin Liu、
  Guiyang Liu、Cheng Zhang、Fang Situ 和 Qi Zhou。参见
  [数据集论文](https://arxiv.org/abs/2606.29193)、固定版本的
  [数据集许可](https://www.aiops.cn/gitlab/aiops-live-benchmark/agenticopseval/-/raw/69cf36430b43024d02530c610b1a4738b5c9a7fb/RCA100/LICENSE)和
  [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)。

Agent RCA Bench 选择 case，将评测限制在冻结的 case 时间窗内，并把源格式映射到评测
使用的 ingestion protocol。发布物只包含经过 sanitization 的标识符、派生事实、聚合测量
和源文件哈希，不包含源 telemetry row、源 archive、topology、causal graph 或 ground-truth
文件。

Apache-2.0 只适用于本项目原创的代码、artifact schema、报告文本和独立派生的聚合结果，
不重新许可上游数据或上游数据集文档。[DATASETS.md](DATASETS.md)记录源文件级别的清单
和转换过程。

## Artifacts

[JSON](artifacts/measurement/agent-rca-v34-six-model.json) 按哈希绑定数据和协议，
[HTML](artifacts/measurement/agent-rca-v34-six-model.html) 展示六模型结果。
复现步骤见 [README.md](README.md#reproduce-the-published-report)。

Provider trajectory 在公开 release tag 之前执行。对应 tag 的代码可以验证脱敏 artifact，并确定性
重建评分、聚合、JSON 和 HTML。重新调用 provider 属于 replication，不是报告复现的一部分。

测量更新时间：`2026-09-07T01:35:19Z`（UTC）。

报告生成时间：`2026-09-07T02:01:15Z`（UTC）。
