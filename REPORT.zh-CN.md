# Agent RCA Bench — 2026 年报告

[六模型交互报告](artifacts/measurement/agent-rca-v34-six-model.html) ·
[English](REPORT.md)

## 结论

Raw 减少了 Fable、Gemini 和 Qwen 的输入量，但结果不支持 Semantic Graph 普遍提升端到端效率。

- Raw 与 Split：12 项预先指定端点中，3 项通过各自冻结的 Holm 检验，均为输入量指标。
  没有工具调用指标通过校正。
- Graph 与 Raw：没有端点通过校正。不显著不代表两种接口等价。
- 聚焦检索：Graph 在 Discovery 的 35/35 个合格结果中减少 rows，在 Graph retrieval 的
  11/12 个结果中同时减少 rows 和 calls；Qwen 有一个反例。

正确诊断数为 Split 105/168、Raw 130/168、Graph 124/168，仅作描述。Graph 在服务与依赖故障
（OpenRCA2）上优于 Raw，在节点故障（RCA100）上则相反。这是测量后的分组分析，数据集与故障层级混杂，
不能据此解释差异。

## 数据修正

<!-- split-rerun-correction:start -->
数据修正：重新执行并替换 168 个 Split 单元，保留 336 个 Raw/Graph 单元及全部 micro 结果。重跑修正了 Tempo 保留时间和查询结果中重复的标签名称。Split max_items 沿用原 SQL max_rows 的参数建议，返回单位改为 items；工具主描述未追加聚合建议。Raw/Graph 的 query_metrics 参数描述保持原样。每次替换调查前后均通过 trace 可见性和样本完整性检查，逐单元检查记录和被取代结果的哈希保存在 JSON 中。Raw/Graph 保留原始 PromQL 编码，运行时间也不同；本次比较没有单独检验 provider 随时间变化或各项修正的影响。后续用户授权允许对临时连接或 provider 错误每个单元最多重跑 3 次（总尝试最多 4 次），并保留每次失败记录；错误诊断和预算耗尽不重跑。3 个单元共发生 6 次失败，已按授权重跑，原因是 SDK 连接错误，额外观测费用为 USD 28.9673；最终替换单元的正常费用已计入下方调查成本。未返回用量的请求可能另有未观测到账单费用。JSON 保留授权、执行修订、脱敏失败尝试及来源哈希。输出受限的失败原样保留，未重跑（gemini-3.8-flash / semantic-rca-transfer-014 / rep0）；按冻结的运行器契约计为失败，并保留 runner error 标记。固定查询和轨迹，以 o200k_base 分词并按模型系数校准，重复标签名称的输入开销估算为保留 Raw 输入的 0.71%、Graph 输入的 0.21%。这些是离线估算，不是 provider 新用量测量，也不表示对诊断准确率的影响。保留该冗余使 GreptimeDB 显得更费 token，对 GreptimeDB 不利；扣除后，Split 与 Raw 的总输入差距扩大约 0.8%。Gemini 请求（含 SDK 重试）共用每分钟 1,800,000 输入 token 的本地额度，发送前估算、响应后按实际用量校正；总耗时包含等待额度的时间。
<!-- split-rerun-correction:end -->

## 测量范围

报告覆盖 6 个模型配置、696 个 agent cell：

- 192 个固定 cohort 的 micro-benchmark cell：6 个 Discovery case、2 个 Graph-retrieval
  case、6 个模型、2 个 treatment、2 次重复。
- 504 个端到端 cell：14 个 case、6 个模型、3 个 treatment、2 次重复。

每个模型条目代表完整的模型服务配置。不同服务的推理设置不代表相同的计算预算；模型排名仅作描述。

保留的 Qwen 端到端结果中，5 次运行将单个 provider 的并发上限从冻结值 2 调整为 4，
工件保留了逐次执行记录。本次测量未单独检验该偏离的影响。

本次 Split 重跑的并发上限为全局 8、每 cohort 4、每 provider 2、每个 case 环境 1。
这些上限适用于替换运行；保留的 Raw/Graph 运行沿用原有执行记录。

三个端到端实验组分别是：

- `split_pillars`：通过原生查询 API 使用 Prometheus、Loki 和 Tempo。
- `raw`：通过只读 SQL 和 PromQL 查询 GreptimeDB 中的 metric、log 和 trace 表。
- `semantic_graph`：在 Raw 接口上增加表语义、实体、关系、覆盖范围和 Semantic Graph
  查询工具。

`raw - split_pillars` 比较 Agent 面对的完整接口组合，同时改变存储、查询语言和工具接口，
不能单独识别存储拓扑的影响。指标是正确完成调查的工具调用次数和模型输入 token。

`semantic_graph - raw` 比较相同 GreptimeDB 遥测数据上增加语义接口的效果。指标是正确完成
调查的工具调用次数和返回行数。两个检验族的负差值都表示比较名称左侧的实验组用量更少。

两个检验族、指标和校正范围在运行前冻结，每份工件都按哈希绑定协议。
「预先指定」指 Git 中有哈希绑定的协议文件，不代表公开的第三方注册。其他结果均为描述性结果。

主要效率样本要求诊断正确、至少一条引用对应成功且未截断的查询、无运行器错误，且未耗尽预算。同一模型和 case 的重复差值先归并为一个中位数，再做跨 case 汇总。两个
确认性检验族分别校正。GPT、DeepSeek、Fable、GLM 的冻结校正范围为 8，Gemini、Qwen
为 4；交互报告的端点表以 `m` 列标明。

样本包含 14 个独立 case，资格筛选后每项检验有 2 到 14 个 case。该样本量下，精确符号检验
效能有限，p 值取值离散；不显著表示证据不足，不能据此认定两组等价。

## 端到端结果

| 模型 | 正确 Split / Raw / Graph | 合格 Split / Raw / Graph | 能力分 Split / Raw / Graph |
| --- | --- | --- | --- |
| `gpt-5.6-sol` | 13 / 22 / 20 | 13 / 22 / 20 | 55.04 / 81.93 / 73.11 |
| `deepseek-v4-pro` | 13 / 18 / 18 | 13 / 18 / 18 | 57.98 / 68.07 / 71.85 |
| `claude-fable-5-1` | 27 / 26 / 23 | 27 / 26 / 23 | 97.06 / 93.28 / 86.55 |
| `glm-5.3` | 10 / 14 / 16 | 6 / 11 / 9 | 50.84 / 59.24 / 67.02 |
| `gemini-3.8-flash` | 22 / 25 / 24 | 22 / 25 / 24 | 81.72 / 94.12 / 90.76 |
| `qwen3.8-max-0902` | 20 / 25 / 23 | 20 / 25 / 23 | 75.63 / 92.02 / 85.71 |

每个实验组的正确和合格计数都覆盖 28 次运行。除 GLM 外，各模型的每次正确诊断
都满足效率样本条件。GLM 的部分最终答案缺少执行有效的引用，因此即使运行已
完成，仍不能进入效率样本。

### Raw 与 Split

下表计算 `Raw - Split`，负数表示 Raw 更省。

| 模型 | 合格 case | Calls 差值 | 方向 | Input 差值 | 方向 | 精确 p | Holm p |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `gpt-5.6-sol` | 8 | -7.25 | 7 / 0 / 1 | -308,273.5 | 8 / 0 / 0 | 0.0703125 / 0.0078125 | 0.421875 / 0.0546875 |
| `deepseek-v4-pro` | 6 | -4 | 4 / 2 / 0 | -188,749.5 | 3 / 0 / 3 | 0.125 / 1.0 | 0.625 / 1.0 |
| `claude-fable-5-1` | 13 | -1.5 | 7 / 1 / 5 | -276,203.5 | 13 / 0 / 0 | 0.7744140625 / 0.000244140625 | 1.0 / 0.001953125 |
| `glm-5.3` | 2 | -10 | 2 / 0 / 0 | -3,594,276.5 | 2 / 0 / 0 | 0.5 / 0.5 | 1.0 / 1.0 |
| `gemini-3.8-flash` | 11 | +1 | 2 / 2 / 7 | -2,492,151 | 10 / 0 / 1 | 0.1796875 / 0.01171875 | 0.1796875 / 0.03515625 |
| `qwen3.8-max-0902` | 14 | -6 | 11 / 0 / 3 | -523,121 | 13 / 0 / 1 | 0.057373046875 / 0.0018310546875 | 0.11474609375 / 0.00732421875 |

方向列依次表示 Raw 更少、相同、更多的 case 数。54 个合格的 model-case 结果中，Raw 有 33 个
减少了完整运行的工具调用，但没有模型的 calls 指标通过 Holm 校正。六个模型的 input case 中位差均为负，但单个 case 的方向仍有差异。

Split 使用原生 PromQL、LogQL 和 TraceQL 接口，不能在一次查询中 join 多种信号。Raw 可以
通过一个 SQL 接口查询三类信号。因此，Fable、Gemini 和 Qwen 的显著结果支持各自配置下的接口组合结论，
不能解释为单独由某个存储引擎造成。

六个模型的 Raw 总输入量均低于 Split。下表覆盖每臂全部 28 次运行，包括错误诊断；
这是描述性总量，不是经过合格性筛选、按 case 取中位数的主要指标。

| 模型 | Split 输入 token | Raw 输入 token | Raw 降幅 |
| --- | --- | --- | --- |
| `gpt-5.6-sol` | 29,038,323 | 13,648,607 | 53.00% |
| `deepseek-v4-pro` | 48,752,606 | 38,039,654 | 21.97% |
| `claude-fable-5-1` | 30,904,794 | 15,827,965 | 48.78% |
| `glm-5.3` | 43,892,554 | 32,011,651 | 27.07% |
| `gemini-3.8-flash` | 187,148,396 | 68,462,777 | 63.42% |
| `qwen3.8-max-0902` | 42,106,989 | 31,832,932 | 24.40% |

Qwen 的 Raw 端到端估算成本比 Split 低 27.98%。Gemini 返回的 usage 未完整区分缓存与
未缓存输入，因此无法按冻结价格精确估算成本；输入量下降不等于成本同比例下降。

### Semantic Graph 与 Raw

下表计算 `Graph - Raw`，负数表示 Graph 更省。

| 模型 | 合格 case | Rows 差值 | 方向 | Calls 差值 | 方向 | 精确 p | Holm p |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `gpt-5.6-sol` | 9 | +22.5 | 4 / 0 / 5 | +2.5 | 4 / 0 / 5 | 1.0 / 1.0 | 1.0 / 1.0 |
| `deepseek-v4-pro` | 10 | -258.5 | 7 / 0 / 3 | -3.75 | 8 / 0 / 2 | 0.34375 / 0.109375 | 1.0 / 0.875 |
| `claude-fable-5-1` | 13 | +16 | 6 / 0 / 7 | +0.5 | 6 / 0 / 7 | 1.0 / 1.0 | 1.0 / 1.0 |
| `glm-5.3` | 3 | -112 | 2 / 0 / 1 | +9 | 0 / 1 / 2 | 1.0 / 0.5 | 1.0 / 1.0 |
| `gemini-3.8-flash` | 12 | -138.75 | 8 / 0 / 4 | -1 | 8 / 0 / 4 | 0.3876953125 / 0.3876953125 | 1.0 / 1.0 |
| `qwen3.8-max-0902` | 12 | -381.75 | 9 / 0 / 3 | -1.5 | 8 / 0 / 4 | 0.14599609375 / 0.3876953125 | 0.583984375 / 1.0 |

DeepSeek 在 10 个合格 case 中有 8 个减少 calls，7 个减少 rows。Gemini 和 Qwen 的两项
中位差也均为负，但均未通过校正。GPT 和 Fable 没有一致的资源下降；GLM 只有 3 个合格
case，不足以形成稳定估计。

## 故障场景与诊断差异

Agent 输入使用不透明的 case ID；参考答案中的机制与目标只在报告中展示。

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

## 专项检索测试

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

轨迹记录了 Semantic Graph、SQL JOIN 和 PromQL 的实际使用：

- 168 个 Graph run 全部至少执行了一次成功的 `query_semantic_graph`，合计 369 次成功调用。
- Raw 和 Graph 共在 92 个 run 中执行了 192 次成功的 SQL `JOIN`。大部分是 `traces` 内部 span
  join 或多张 metric 表 join。真正跨越两种信号的 join 很少：3 个 run 共 3 次成功调用。
- 336 个 GreptimeDB treatment run 中，只有 22 个使用了成功的原生 PromQL `query` 或
  `query_range`：Raw 15 个，Graph 7 个。Split 有 160/168 个 run 使用成功的 PromQL，因为该
  treatment 只能通过 Prometheus 查询 metrics。

Prompt 已明确说明 `execute_sql` 可以查询 metric、log 和 trace 表，GreptimeDB 也提供 PromQL。
模型在 GreptimeDB treatment 中仍主要选择 SQL。因此，本报告测量的是冻结 agent 对可用接口
的实际选择，不代表人工设计的跨信号 SQL 或 PromQL 查询所能达到的最佳效果。

## 能力评分与证据审计

测量后定义的能力指数给定位 40 分、根因 40 分、已执行 citation 5 分。每个模型的 84 个
transfer run 都留在分母中，分数再归一到 100。该指数只作描述，不参与假设检验。

| 排名 | 模型 | 综合 | Split | Raw | Graph |
| --- | --- | --- | --- | --- | --- |
| 1 | `claude-fable-5-1` | 92.30 | 97.06 | 93.28 | 86.55 |
| 2 | `gemini-3.8-flash` | 88.87 | 81.72 | 94.12 | 90.76 |
| 3 | `qwen3.8-max-0902` | 84.45 | 75.63 | 92.02 | 85.71 |
| 4 | `gpt-5.6-sol` | 70.03 | 55.04 | 81.93 | 73.11 |
| 5 | `deepseek-v4-pro` | 65.97 | 57.98 | 68.07 | 71.85 |
| 6 | `glm-5.3` | 59.03 | 50.84 | 59.24 | 67.02 |

确定性 evidence sufficiency 单独审计。SQL verifier 在 71 个 Raw run 和 58 个 Graph run 中证明了
所需证据。它不能评估原生 Prometheus、Loki 或 Tempo evidence，因此 Split 记为
`not estimable`，而不是证明失败；deterministic proof 也不计入能力分。Split 仍需满足相同的主要
效率样本条件：至少有一条成功、未截断且被引用的查询。

## 可靠性、token 与成本

纳入报告的 696 个 cell 中有 1 个 runner error：Gemini 达到单次回复的输出上限，
保留计为失败，未重跑。没有 cell 耗尽工具预算。另有 6 次已归档的 SDK 连接失败，
按「数据修正」中的授权和上限重跑。端到端数据库查询失败仍保留在轨迹和报告中：
GPT 64、DeepSeek 107、Fable 38、GLM 220、Gemini 117、Qwen 140。
下表合并每个模型的 32 次 micro 和 84 次端到端运行。

| 模型 | Provider 可见输入 | 非缓存 | 缓存读取 | 缓存写入 | 输出 | 推理输出 |
| --- | --- | --- | --- | --- | --- | --- |
| `gpt-5.6-sol` | 63,937,235 | 14,727 | 53,056,153 | 10,866,355 | 495,559 | 149,632 |
| `deepseek-v4-pro` | 118,029,923 | 8,507,107 | 109,522,816 | 0 | 1,789,201 | 未单独报告 |
| `claude-fable-5-1` | 63,120,294 | 741,396 | 55,383,433 | 6,995,465 | 961,633 | 277,310 |
| `glm-5.3` | 109,751,221 | 6,089,525 | 103,661,696 | 0 | 1,287,048 | 733,828 |
| `gemini-3.8-flash` | 332,964,907 | N/A | N/A | N/A | 1,852,742 | 1,452,993 |
| `qwen3.8-max-0902` | 109,241,549 | N/A | N/A | N/A | 2,491,291 | 1,703,008 |

Reasoning 是 output 的子集，不能重复相加。DeepSeek 没有单列 reasoning breakdown；该字段为 0
不表示模型没有推理。
Gemini 缺失的 thinking 明细由 provider 报告的总 token 减去输入和可见输出重建。
缺失的缓存字段保留 N/A，不记为零。Qwen 的端到端缓存明细完整，但 micro 缺少缓存创建
计数，因此合并后的缓存明细与总成本不可估算。Gemini 的缓存明细不完整，也无法完整计价。
报告不提供六模型 micro 与端到端合并后的成本合计。

端到端估算成本包含每个 treatment 的全部 28 个 run，不只统计合格 pair。所有数字都是按冻结价格
估算的，不是账单：

| 模型 | Split | Raw | Graph | Raw - Split | Graph - Raw |
| --- | --- | --- | --- | --- | --- |
| `gpt-5.6-sol` | USD 41.5831 | USD 18.2465 | USD 24.4954 | USD -23.3366 | USD +6.2489 |
| `deepseek-v4-pro` | USD 9.9098 | USD 6.8193 | USD 5.6526 | USD -3.0905 | USD -1.1667 |
| `claude-fable-5-1` | USD 72.3745 | USD 36.8636 | USD 38.2825 | USD -35.5110 | USD +1.4189 |
| `glm-5.3` | CNY 119.2748 | CNY 83.2793 | CNY 86.9650 | CNY -35.9955 | CNY +3.6857 |
| `gemini-3.8-flash`* | USD 23.0362–26.4730 | USD 10.1512–12.2814 | USD 11.1215–13.2096 | USD -16.3217–-10.7548 | USD -1.1599–+3.0584 |
| `qwen3.8-max-0902` | CNY 130.7835 | CNY 94.1838 | CNY 97.5194 | CNY -36.5998 | CNY +3.3356 |

*Gemini 区间保留已报告的缓存折扣，仅将缺失缓存明细的输入分别按缓存价和普通输入价计算；
输出包含 reasoning，不重复计费。区间不是置信区间或账单金额。Raw 的上界低于 Split 的下界，
但 Graph 与 Raw 的区间重叠，不能确定二者成本方向。报告不提供六模型精确成本合计。
其余五模型均按已报告的缓存用量计价。

以下端到端成本合计包含全部六个模型，Gemini 按费用区间计入。每种接口覆盖 168 次运行，
包括纳入报告的失败运行；归档重试的额外 USD 28.9673 不计入。按冻结费率估算，
不代表账单金额或置信区间。

| 接口 | 估算总成本（USD） |
| --- | ---: |
| Split | 183.95–187.39 |
| Raw | 98.37–100.50 |
| Graph | 106.88–108.97 |

Qwen 冻结价格为每百万普通输入、缓存创建、缓存读取、输出 token 分别 CNY
`12 / 15 / 1 / 36`，于 2026-09-06 核实
（[百炼定价](https://help.aliyun.com/zh/model-studio/qwen3-8-max)）。Gemini 的普通输入、缓存
输入、含 thinking 的输出分别为每百万 token USD `0.75 / 0.075 / 3.75`，同日核实
（[Gemini API 定价](https://ai.google.dev/gemini-api/docs/pricing)）。价格已知不能补全缺失的用量字段。

可完整计价模型的端到端与专项测试成本，按计费币种列示：

- `gpt-5.6-sol`：`USD 85.5243242`
- `deepseek-v4-pro`：`USD 23.133621104`
- `claude-fable-5-1`：`USD 156.78478075`
- `glm-5.3`：`CNY 292.076936`

GLM-5.3 按 CNY 计费，输入、缓存命中、输出分别为每百万 token `8.0 / 2.0 / 28.0`，于 2026-09-04
在 <https://bigmodel.cn/pricing> 核实。该价格在运行期间已经生效，只是最初组装定价快照时遗漏，
补入不改变任何 token 计数和模型行为。缓存存储按每百万 token 每小时计费，当时为限时免费，因此
没有冻结缓存写入费率，返回 cache-creation token 的 run 保持不计价。

成本按计费币种记录。六个模型中，四个可完整计价 micro 与端到端测量，因此不提供全模型合计。
美元合计和图表采用冻结汇率：GLM 为 `6.7179` CNY/USD（2026-09-03，
[Trading Economics](https://tradingeconomics.com/china/currency)）；Qwen 为 `6.7787` CNY/USD
（2026-09-04，[SAFE](https://www.safe.gov.cn/AppStructured/hlw/RMBQuery.do)）。分模型表保留原币金额。

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
- **RCA-100 v1.1。**仅依据源数据的选择过程记录了 15 个节点故障候选的聚合
  概况，并选出 4 个 case。数据集署名作者为 Xidao Wen、Haibin Liu、
  Guiyang Liu、Cheng Zhang、Fang Situ 和 Qi Zhou。参见
  [数据集论文](https://arxiv.org/abs/2606.29193)、固定版本的
  [数据集许可](https://www.aiops.cn/gitlab/aiops-live-benchmark/agenticopseval/-/raw/69cf36430b43024d02530c610b1a4738b5c9a7fb/RCA100/LICENSE)和
  [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)。

Agent RCA Bench 选择 case，将评测限制在冻结的 case 时间窗内，并把源格式映射到评测
使用的写入协议。发布工件只包含脱敏标识符、派生事实、聚合测量和源文件哈希，
不包含原始遥测行、源数据归档、拓扑、因果图或参考答案文件。

Apache-2.0 只适用于本项目原创的代码、工件格式、报告文本和独立派生的聚合结果，
不重新许可上游数据或上游数据集文档。[DATASETS.md](DATASETS.md)记录源文件级别的清单
和转换过程。

## 报告工件

[JSON](artifacts/measurement/agent-rca-v34-six-model.json) 按哈希绑定数据和协议，
[HTML](artifacts/measurement/agent-rca-v34-six-model.html) 展示六模型结果。
复现步骤见 [README.md](README.md#reproduce-the-published-report)。

模型调查在公开发布标签之前执行。标签对应的代码可验证脱敏工件，并确定性
重建评分、聚合、JSON 和 HTML。重新调用模型服务属于测量复跑，不属于报告复现。

测量更新时间：`2026-09-08T03:50:20Z`（UTC）。

报告生成时间：`2026-09-08T04:43:03Z`（UTC）。
