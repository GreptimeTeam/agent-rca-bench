# Semantic RCA Bench：Discovery v2 与 Graph v3 测量报告

## 结论

Semantic layer 在满足数据契约的场景中可以提高 RCA 调查效率。现有数据支持的效果是减少检索结果、工具调用和模型上下文消耗，不是普遍提高诊断准确率。

正确输出在本实验中是效率比较的有效性门槛：只有两个 treatment 都给出有效证据的配对，才进入完成效率统计。这样可以避免把「少查数据但答错」计为效率提升。准确率变化可以作为次要观察，但不是本轮实验要求出现的效果。

本轮结果分为两部分：

- Table Semantics 在 5 个可比较的 Discovery case 中全部减少了截至引用证据查询的累计返回行数，median case delta 为 `-273`，精确双侧 sign test 为 `p=0.0625`。工具调用和模型 token 没有稳定下降。
- Semantic Graph 在 2 个 Graph case 中都减少了返回行数、工具调用和报告中的模型 token，median case delta 分别为 `-61.25`、`-2.5` 和 `-40,831`，三项均为 `p=0.5`。方向一致，但不能外推为广泛效果。

这些结果为 semantic layer 的调查效率机制提供了直接证据，但还没有形成跨 case 的统计结论，也没有证明端到端 RCA 效率。下一阶段需要增加独立、source-faithful 的 relational cases，再冻结端到端 RCA transfer test。当前结果不支持继续运行付费全量 RCA。

## 实验口径

正式测量使用 Codex subscription runner 和 `gpt-5.6-luna`，没有运行付费全量 RCA。

| Micro-benchmark | Treatments | Cases | Repetitions | Cells | Paired observations |
| --- | --- | ---: | ---: | ---: | ---: |
| Discovery v2 | Raw / Table Semantics | 6 | 每个 case 2 次、顺序平衡 | 24 | 12 |
| Graph v3 | Table Semantics / Semantic Graph | 2 | 每个 case 2 次、顺序平衡 | 8 | 4 |

正式 case 在模型运行前完成冻结和 no-model gate。selection manifest 记录 eligible set、hash ranking、既有 agent 轨迹排除项、候选消耗顺序和拒绝原因。正式 32 个 cells 均无 runner error、tool budget hit 或 rejected tool call。

效率统计采用 treatment 减 baseline 的 paired delta。负数表示 semantic treatment 使用的资源更少。跨 incident 推断以 case 为主要独立单位；同一 case 的重复运行只用于描述模型随机性。每个 case 先取共同成功 run-pair delta 的 median，再跨 case 计算方向和 sign test。模型 token 指 Codex 唯一一条累计 `turn.completed` 中的 `input_tokens + output_tokens`：input 包含 cached input，但 CLI 没有保留 cache breakdown；output 包含 reasoning，但没有独立 reasoning 计数。Codex runner context、MCP schema/result 和 output-schema handling 都在 provider context 内，无法拆分各自贡献。该字段只能在同一 Codex CLI、模型、protocol 和 case 内比较，不等同于 API 账单金额，也不能与 API 或 Claude subscription 字段横向比较；它不是本轮预注册的主要指标。

## Discovery v2：Table Semantics

### 正式 case

| Case 与隐藏信号 | Catalog rank | Raw success | Table Semantics success | 共同成功配对的 row delta | Call delta |
| --- | ---: | ---: | ---: | ---: | ---: |
| Market cloudbed-1，email container read throughput | 4/97 | 2/2 | 2/2 | -191，-733 | +4，-3 |
| Market cloudbed-2，email container write throughput | 5/64 | 2/2 | 2/2 | -520，-502 | 0，+2 |
| Bank，Redis02 CPU user utilization | 1/41 | 0/2 | 0/2 | 不适用 | 不适用 |
| Bank，Tomcat01 local-disk read I/O | 1/58 | 1/2 | 2/2 | -273 | -2 |
| Telecom，docker_001 CPU | 1/18 | 2/2 | 2/2 | -117，-117 | 0，-1 |
| Telecom，docker_006 CPU | 1/18 | 2/2 | 2/2 | -116，-116 | -1，-1 |

Raw 成功 `9/12`，Table Semantics 成功 `10/12`。配对结果为 1 次改善、0 次退化、11 次持平，`p=1.0`。唯一改善出现在 Bank disk-read case：一次 Raw run 选择了 `DSKRTps`，两次 Table Semantics run 都选择了冻结目标 `DSKRead`。这个 case 说明 semantic metadata 有时可以修正定位，但样本不足以支持一般性的准确率结论。

两个 treatment 在 Redis CPU case 的四次运行中全部失败。Catalog 已将冻结目标 `OSLinux_CPU_CPU_CPUUserTime` 排在第一位，但模型仍选择 Redis 的累计 `used_cpu_user` counter。错误信号的 mean 只从约 `15838.26` 变为 `15840.30`；独立 canonical query 的 OS CPU utilization mean 从 `0.19297` 变为 `16.91008`。Catalog recall 正确并不代表 agent 能区分语义相近但统计性质不同的信号。

### 完成效率

以下 run-pair 统计只包含 9 个两个 treatment 都成功的配对，用于描述冻结 case 集合内的模型随机性：

| Metric，Table Semantics - Raw | Better / worse / ties | Median delta | 精确双侧 sign test |
| --- | ---: | ---: | ---: |
| 截至引用证据查询的累计返回行数 | 9 / 0 / 0 | -191 | `p=0.00390625` |
| 截至引用证据查询的工具调用 | 5 / 2 / 2 | -1 | `p=0.453125` |
| 截至引用证据查询的 discovery calls | 5 / 2 / 2 | -1 | `p=0.453125` |
| 完成运行所报告的模型 token | 5 / 4 / 0 | -506 | `p=1.0` |

跨 case 推断先取每个 case 的 median delta：

| Metric，Table Semantics - Raw | Cases better / worse / ties | Median case delta | 精确双侧 sign test |
| --- | ---: | ---: | ---: |
| 截至引用证据查询的累计返回行数 | 5 / 0 / 0 | -273 | `p=0.0625` |
| 截至引用证据查询的工具调用 | 3 / 2 / 0 | -0.5 | `p=1.0` |
| 截至引用证据查询的 discovery calls | 3 / 2 / 0 | -0.5 | `p=1.0` |
| 完成运行所报告的模型 token | 3 / 2 / 0 | -2,725 | `p=1.0` |

Table Semantics 在全部 5 个可比较 case 中都缩小了查询结果集，覆盖 Bank、Market、Telecom 三个系统和两个 Market cloudbed。方向一致，但 case-level `p=0.0625`，不能表述为统计显著。更少的返回行数没有稳定转化为更少的调用或 token；semantic catalog search 和 table profile 本身也会占用上下文。

所有正式 fixture 都通过 catalog top-five gate，catalog mean reciprocal rank 为 `0.7417`。这个 gate 是重要限制：实验测量的是目标已进入前五名时的 agent 行为，不是整个 eligible incident 集合上的 catalog recall。一个 Bank CPU 候选在模型运行前因 rank 14 被拒绝。

Market cloudbed-2 的早期 node-write report 有 4 个有效 exploratory cells，但运行时间早于 mixed-case identifier prompt 修复，未并入正式结果。正式 cohort 使用下一个未运行 agent 的 hash-ranked case，保证 24 个 cells 使用同一 prompt contract。

## Graph v3：Semantic Graph

### 数据契约与 case attrition

Graph benchmark 只接受源数据能够证明的关系：同一 `trace_id` 下，`SPAN_KIND_CLIENT` span 与以其 `span_id` 为 `parent_span_id` 的 `SPAN_KIND_SERVER` child 构成 `service -> service` 的 `calls` edge。Benchmark 不根据名称相似度补造 entity identity、span role 或 relationship。

冻结选择最终只有两个 Hotel Reservation case 通过全部 gate：

- `hs4-geo-pod-failure-pdt289`
- `hs1-rate-pod-failure-vmvtxr`

6 个较早的 Hotel 候选、全部 12 个 OTel Demo 候选和唯一的 Train Ticket 候选因 observable-alert 缺失或 manifest/injection root 冲突被拒绝。没有使用已知 case 补足配额。

两个正式 case 的独立 raw client/server span self-join 与 deduplicated Graph aggregation 均精确返回 6 条 edge，endpoint、`request_count` 和 `error_count` 逐项一致：

| Case | Caller | Unique greatest-error callee | Errors / requests |
| --- | --- | --- | ---: |
| `hs4-geo-pod-failure-pdt289` | `frontend` | `search` | 504 / 1,043 |
| `hs1-rate-pod-failure-vmvtxr` | `frontend` | `search` | 1,504 / 2,772 |

### Agent 结果与完成效率

Semantic Graph 成功 `4/4`，Table Semantics 成功 `3/4`。唯一的 Table Semantics 失败已经找到正确的 `search` destination 和 RED counts，但提交了 `dst_type=search`，而独立 edge set 证明 canonical type 是 `service`。这是结构化输出错误，不是未找到异常 callee。

以下 run-pair 统计只包含 3 个共同成功配对：

| Metric，Semantic Graph - Table Semantics | Better / worse / ties | Median delta | 精确双侧 sign test |
| --- | ---: | ---: | ---: |
| 截至引用证据查询的累计返回行数 | 3 / 0 / 0 | -72 | `p=0.25` |
| 截至引用证据查询的工具调用 | 3 / 0 / 0 | -2 | `p=0.25` |
| 完成运行所报告的模型 token | 3 / 0 / 0 | -40,705 | `p=0.25` |

按 case 聚合后的结果为：

| Metric，Semantic Graph - Table Semantics | Cases better / worse / ties | Median case delta | 精确双侧 sign test |
| --- | ---: | ---: | ---: |
| 截至引用证据查询的累计返回行数 | 2 / 0 / 0 | -61.25 | `p=0.5` |
| 截至引用证据查询的工具调用 | 2 / 0 / 0 | -2.5 | `p=0.5` |
| 完成运行所报告的模型 token | 2 / 0 / 0 | -40,831 | `p=0.5` |

三个有效 run pair 的总 token delta 分别为 `-40,705`、`-679` 和 `-81,235`。直接查询 Graph edge 可以替代 schema inspection 和 raw span self-join，因此行数、调用和 token 呈现一致方向。Token 分析是 post-hoc exploratory result，不是预注册的主要指标。

这个结果依赖高质量 entity 与 relationship 定义。OpenRCA Bank、Market 和 Telecom 缺少标准 span role 或一致 entity identity，因此 Graph coverage 为空，不属于 Graph treatment 的适用场景。Graph v3 又只有两个同属 Hotel Reservation 的 case，所以只能说明机制成立，不能估计更广泛系统上的 effect size。

OpenRCA 2.0 artifact 的 paper 与 dataset card 对许可证声明不一致，artifact 也不是 paper 承诺的 archival release。Graph v3 因此是内部 protocol-formal measurement，不应在解决 provenance 和 licensing 之前包装成外部研究结论。

## 本轮修复的测量缺陷

本轮在测量前修复并测试了以下问题：

- API runner 的 turn limit 调整为 `max_tool_calls + 10`，turn exhaustion 记录为可评分失败，不再中断整个 batch。
- Codex subscription runner 接收与 API、Claude 相同的 system contract。
- Taxonomy 外答案在所有 runner 中统一记为错误答案。
- 未知工具调用统一记录为 `rejected_tool_calls`，但不触发 `tool_budget_exhausted`。
- `rows_returned` 和完成调用数采用同一 eligibility guardrail：runner 无错误、未触发 budget、joint diagnosis 正确，并且至少有一条 citation 且全部 citation 有效。
- 单 case 的 repetition 只输出 descriptive run-pair summary，不再附 sign-test p 值；端到端 RCA 推断按 case median 进行，并对四个主检验做 Holm 校正。
- 删除跨 runner 含义不一致的 `first_component_mention_turn`；Codex 工具结果不再被误记为模型首次想到组件。
- Graph observation window 在 isolation、coverage 和 query path 中统一为半开区间 `[start, end)`。
- `information_schema` scope 改为检查解析后的 `table_schema = current_database` conjunct，不再使用字符串包含判断。
- Semantic catalog 将 `I/O` 规范化为 `io`，过滤单字符和停用词，并识别 `io_w`、`io_r` 的方向语义。
- Discovery canonical SQL 保留 identifier 大小写并使用双引号；scorer 支持合法的 current-database qualification。
- Zero-row log 或 trace modality 不再触发无意义的 validation query。
- `GroundTruth` 不再保留没有 producer 的 `causal_dependency`；`Diagnosis.causal_dependency` 继续作为诊断输出，但没有 canonical truth 时不评分。

这些修复保证 runner、treatment 和 scorer 的效率统计口径一致。它们不构成 semantic layer 的效果数据。

## 数据完整性与验证

- 6 份 Discovery 与 2 份 Graph 正式 agent reports 和当前 protocol、selection manifest 完全一致。
- 正式 32 个 cells 中，runner error、tool budget hit 和 rejected tool call 均为 0。
- 两个 Graph no-model audits 的 raw span edge set 与 Graph edge set 完全一致。
- 17 个带上游 SHA-256 的 OpenRCA source artifacts 共 19,713,341,344 bytes，校验结果为 0 missing、0 mismatch；下载目录没有 `.part` 文件。
- 测试结果为 `182 passed`。
- Ruff lint、Ruff format check、`git diff --check`、Python compileall 和 `uv lock --check` 均通过。
- 临时 GreptimeDB `127.0.0.1:4100` 已停止；既有 `127.0.0.1:4000` 实例未被本轮实验修改。

## 后续实验建议

下一阶段仍然围绕效率，不要求 semantic layer 必须提高准确率：

1. 增加不同系统的 source-faithful relational cases。Entity identity、span role 和 relationship 必须来自源数据或正式声明，不能为了覆盖 Graph 而补造。
2. 冻结端到端 RCA transfer test。主要效率指标至少包括截至正确诊断的累计返回行数、工具调用、`input_tokens + output_tokens` 和 elapsed time。
3. 将正确 affected component、fault mechanism 和有效 evidence citation 作为完成效率 eligibility guardrail。存在 canonical causal dependency 时再加入 dependency validity；没有真值时不评分。
4. 分别报告 Table Semantics 与 Raw、Semantic Graph 与 Table Semantics 的 paired deltas，不混合不同 protocol、runner、模型或 corpus taxonomy。
5. 在独立 case 数量足够、token accounting 口径冻结前，不启动新的付费全量 RCA batch。

## 审计入口

- 英文结果与历史记录：[`RESULTS.md`](RESULTS.md)
- Canonical plan：[`PLAN.md`](PLAN.md)
- Discovery protocol：[`DISCOVERY.md`](DISCOVERY.md)
- Graph protocol：[`GRAPH.md`](GRAPH.md)
- Dataset audit：[`DATASETS.md`](DATASETS.md)
- Discovery selection：[`fixtures/measurement/discovery-selection.json`](fixtures/measurement/discovery-selection.json)
- Graph selection：[`fixtures/measurement/graph-v3-selection.json`](fixtures/measurement/graph-v3-selection.json)
- 可复现统计与正式 report 哈希：[`fixtures/measurement/results-summary.json`](fixtures/measurement/results-summary.json)

从保留在本地的正式 reports 重新生成摘要：

```bash
uv run python -m semantic_rca_bench.measurement_summary
```
