# Semantic RCA Bench v31 测量分析

本轮 release candidate 完成了 216 个预定 cell，覆盖 6 个模型配置、9 个 case、2 个
treatment 和 2 次重复。执行期间没有 runner error，也没有 budget exhaustion。可复算结果见
[综合 JSON 报告](semantic-rca-v31-report.json)、[自包含 HTML 报告](semantic-rca-v31-report.html)
和两份脱敏源 artifact。

## 结论

数据支持一个范围较窄的结论：当任务是检索已有 source 证据支持的服务关系时，Semantic
Graph 能明显减少调查工作量；本轮数据不支持「Semantic Graph 普遍提升端到端 RCA 效率」的
结论。

在 2 个关系检索 micro-benchmark case 上，6 个模型中有 5 个产生了 correctness-preserving
配对结果。这 5 个模型使用 Semantic Graph 时，返回行数和工具调用次数全部下降。各模型的
Graph-minus-Raw 中位数变化为 `-1.5` 至 `-4.75` 次调用、`-64` 至 `-183` 行；reported token
也全部下降。Claude Opus 5 在 4 个 Graph run 中都找到了正确 edge，但最终引用的是
`execute_sql` 结果，而不是 Graph 查询结果。这 4 个 run 没有通过 treatment-compatible
evidence gate，因此不能进入效率比较。

6 个 discovery case 的结果是混合的。语义元数据经常减少返回行数，但 catalog 探索也会让
部分模型增加调用或 token。对于 table localization，这一能力不是稳定的效率增益。

Fresh Aegis 端到端 case 给出了负面的 transfer 结果。24 个 run 全部找到了
`ts-auth-service` 和 `workload_restart` 机制，12 个 Graph run 也全部调用了
`query_semantic_graph`。但是只有 `gpt-5.6-sol` 的 1 个 Graph run 同时覆盖了 causal locus
和 normal-to-abnormal restart transition。没有任何一次重复的 Raw 与 Graph 两侧同时满足
evidence eligibility，因此不能把两侧的 rows、calls 或 tokens 解释为有效的端到端效率差异。

这个结果说明了产品能力的边界：Graph 导航能够帮助模型找到正确组件和关系，但不能替代指标级
机制证据。模型仍需查询并引用两个窗口中的 restart counter。

## 模型表现

表中计数顺序为 `Raw / Semantic Graph`。Discovery 每个 treatment 有 12 个 run，关系检索有
4 个，transfer 有 2 个。各模型单独报告，不合并成一个总排名。

| 模型 | Micro 有效性 | Discovery 有效性 | Discovery calls / rows / tokens 中位数差 | 关系检索有效性 | 关系检索 calls / rows / tokens 中位数差 | Transfer 诊断 | Transfer 证据 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `gpt-5.6-sol` | 16 / 16 | 12 / 12 | 0 / -197.25 / -377.5 | 4 / 4 | -1.5 / -64 / -2,159.25 | 2 / 2 | 0 / 1 |
| `deepseek-v4-pro` | 16 / 15 | 12 / 11 | +0.5 / -180 / +774 | 4 / 4 | -4.75 / -98 / -15,141.25 | 2 / 2 | 0 / 0 |
| `claude-opus-5` | 14 / 10 | 10 / 10 | 0 / -228 / +2,662.5 | 4 / 0 | 不可估计 | 2 / 2 | 0 / 0 |
| `claude-fable-5` | 16 / 16 | 12 / 12 | 0 / +12 / +12,363 | 4 / 4 | -2.75 / -67 / -8,610.75 | 2 / 2 | 0 / 0 |
| `glm-5.3` | 12 / 15 | 8 / 11 | -1.5 / -461.75 / -4,096.25 | 4 / 4 | -4.75 / -183 / -11,052 | 2 / 2 | 0 / 0 |
| `qwen3.8-2.4t-a95b` | 15 / 13 | 11 / 9 | 0 / -288.5 / +4,134 | 4 / 4 | -2.25 / -152.5 / -7,211.75 | 2 / 2 | 0 / 0 |

`gpt-5.6-sol` 和 `claude-fable-5` 完成了全部 32 个 micro task；
`deepseek-v4-pro` 完成 31 个，`qwen3.8-2.4t-a95b` 完成 28 个，`glm-5.3` 完成 27 个，
`claude-opus-5` 完成 24 个。这些计数混合了两类 micro task，不能当作 RCA 总分。所有模型的
端到端 diagnosis count 相同，只有一个 run 在 evidence coverage 上拉开差异。

## 可靠性与成本

- 正式报告包含 216 个已完成 cell，runner error 和 budget exhaustion 均为 0。
- Agent 共执行 2,038 次数据库查询，其中 45 次失败，失败率为 `2.21%`。Raw 为 24/957，Graph
  为 21/1,081。失败 SQL 会返回给 agent 修正，不能成为有效证据。
- 各模型的综合 cache hit rate 为 `59.3%` 至 `91.1%`。报告没有估算「完全不使用 cache」的
  反事实成本。
- 两个 Claude 模型都开启了 prompt cache，但 provider 在 micro-benchmark response 中报告的
  cache creation 和 cache read 都是 0；更长的 transfer response 正常报告了两者。成本估算
  严格使用 provider 返回的 usage，不推定未报告的 cache hit。
- 已知估算费用为 `USD 29.223846104` 加 `CNY 12.0569415`。冻结协议时，BigModel 价格页尚未
  列出 GLM 5.3 的价格，因此 GLM 成本保持为空。报告不换算或合并不同币种。
- 执行期间发生过两次可恢复事件。DashScope endpoint 缺失导致 4 个 pre-provider failure，
  这些失败没有 token 或查询消耗；Graph source hash 在首个 Graph model cell 前暴露了浮点诊断
  字段不稳定。两次恢复都保留 exact-prefix 语义，没有重跑已经完成的付费 cell。

## 审计边界

两份公开 artifact 不包含 provider response、reasoning payload、credential、本机路径或 source
telemetry row。Micro artifact 只保留被引用的规范化聚合证据，可以确定性复算 192 个 cell。
Transfer artifact 保留结构化诊断、citation grounding certificate、执行计数、usage 和确定性
evaluation；公开 validator 会复算分层评分和模型汇总。

本轮绑定了 GreptimeDB commit `e67af3fad8f698d964398843d78df782f20847bb`、release build
profile、task fixture、selection manifest、scorer、protocol 和 source checksum，但没有把
benchmark 工作区绑定到 Git commit。根据仓库的发布规则，这是一轮 release-candidate
measurement，不是首次公开的 1.0 正式运行。正式 tag 需要从干净且 commit-bound 的代码重新执行
纳入报告的 cell。

## 结论边界

- 6 个 discovery 和 2 个关系检索 case 是固定 reference cohort，早期 run 参与过 task interface
  的研发，不是 fresh population holdout。
- 端到端结果只有 1 个独立 measurement case。两次重复只能描述模型随机性，不能增加样本量。
- 关系检索 micro-benchmark 来自同一个 system family。
- Provider reasoning 配置是冻结的模型配置，不是统一的计算预算。
- 本实验测量完整的 agent-facing Semantic Graph interface，包括语义元数据、使用指导、恢复指导和
  coverage 信息，不是单纯的数据表示 ablation。
