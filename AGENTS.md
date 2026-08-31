# Semantic RCA Bench agent guide

本文件适用于整个仓库。开始工作前先读取本文件，以及目标路径下可能存在的更具体 `AGENTS.md`。

## 项目定位

本项目要建设一个可开源、可公开复现、允许证伪的 Agent RCA benchmark。核心问题是：GreptimeDB 的 semantic layer 能否在真实故障 telemetry 上，为 LLM agent 的 RCA 调查带来效率提升。

项目不以证明 semantic layer 必然有效为目标。正结果、负结果和适用边界都是有效产出。不得为了得到正结果而修补数据、放宽 scorer、选择已知有利 case 或补造语义关系。

项目北极星是：

> GreptimeDB 不替 LLM 做 RCA。数据库提供准确、结构化、可追溯、便于查询的事实、实体和关系，让强 LLM 在固定诊断有效性要求下，用更少的调查资源完成 RCA。

数据库负责 telemetry 存储与执行、schema 和 signal semantics、stable entity identity、source-proven relationships、provenance，以及 token-efficient query surfaces。调查策略、证据权衡和因果推理由 LLM 完成。不要在数据库或 benchmark 中实现面向具体故障的 RCA 专家规则。

## 两条评估主线

### Semantic layer causal measurement

这是项目的主要研究问题。对同一个模型、case 和 telemetry，比较两个 treatment：

- `raw`
- `semantic_graph`

估计 `semantic_graph - raw`。`semantic_graph` 表示完整的 GreptimeDB Semantic Graph
产品面，包括 table semantics、entity、relationship 和诊断字段。Table semantics 不是独立
treatment；需要归因内部能力时，应使用单独的 ablation protocol，不能混入主 benchmark。

同一比较中的模型、prompt、case window、runner contract、工具预算、turn/timeout policy 和数据库内容必须一致。不同模型的能力差异不能混入 semantic-layer effect。

主要目标是 correctness-preserving investigation efficiency：

- GreptimeDB rows returned to valid evidence
- tool calls to valid evidence or diagnosis
- model input/output tokens
- elapsed time
- cost
- valid-completion rate under a fixed resource budget

正确 diagnosis 和有效 evidence citation 是 efficiency comparison 的 validity guardrail。不得把「少查数据但答错」计为效率提升。准确率变化可以作为次要观察，但不是 semantic layer 必须产生的效果。

### Model RCA report cards

这是 benchmark 的附带产物。模型只能在相同 case、protocol、treatment 和 runner contract 下比较。分别报告：

- RCA validity：causal locus（component 或 directed edge）、fault mechanism、evidence citation，以及有 canonical truth 时的 onset 和 propagated impact
- Investigation efficiency：rows、calls、tokens、time 和可比较时的 cost
- Agent reliability：runner failure、invalid tool call、budget exhaustion、repeated call 和 structured-output failure
- Semantic utilization：catalog、table profile 和 Graph 的使用方式，以及每个模型的 Graph uplift

不要把异构 taxonomy、不同 treatment 或不同 runner 的结果压成一个不透明总分。优先发布 valid-completion rate、分层结果和 correctness-efficiency Pareto frontier。

## 实验与统计契约

- Case 是跨 incident 推断的主要独立统计单位。
- Repetitions 用于描述同一 case 上的模型随机性，不能冒充独立 cases。
- 每个 case 先取 jointly successful run-pair deltas 的 median，再跨 case 做 effect summary 和预注册检验。
- Run-pair statistics 可以保留，但必须明确标为 descriptive。
- 端到端 RCA 的预注册主要效率字段是
  `database_load.rows_returned`（combined report 中为 `rows_returned`）和
  `evaluation.correct_completion_tool_calls`。两者使用冻结 protocol 定义的同一个
  eligibility guardrail。通用 RCA v23 guardrail 要求正确 diagnosis、至少一条 citation、
  全部 citation 有效、无 runner error、无 budget hit。当前 OpenRCA2 transfer 使用分层契约：
  diagnosis 正确、全部 required evidence claim 由 execution-valid citation 覆盖、且无
  runner error/budget hit 时可比较效率；无关的额外无效 citation 只使
  `auditable_completion=false`，不能抹掉已经成立的 required evidence 或效率轨迹。
  Graph entity 存在和普通 calls edge 只能用于导航或传播分析，不能单独证明 required
  `causal_locus`；locus 必须由 incident-local、绑定所声明 operation 或 mechanism 的证据支持。
- Metric evidence may query a population wider than the causal container when the complete result
  directly projects a source identity and the scorer selects the target rows. Pod identity is
  equivalent to container identity only when that equivalence is frozen by the provider-free
  source audit; SQL spelling is not the identity contract.
- 端到端 execution-valid citation 必须唯一对应一次成功、非截断的
  `execute_sql` 或 `query_semantic_graph` `QueryResult`，且 output query ID 与 citation
  一致。Schema/catalog discovery 和空 claim 不构成 evidence。这个检查只证明引用了
  真实执行结果，不证明结果支持 diagnosis；正式 transfer case 还必须在模型运行前
  冻结 deterministic evidence-support predicate。
- Discovery 与 Graph micro-benchmark 的预注册主要效率字段是
  `rows_returned_through_evidence` 和 `tool_calls_through_evidence`。它们只统计到
  cited canonical evidence，不得与端到端 RCA 字段混用。
- Model token 结果在 runner accounting contract 完成审计和预注册前属于 exploratory metric。API `run.usage.input_tokens` 保存 provider-visible total input；cache breakdown 留在 raw response usage。必须说明 cached input、system prompt、tool schema、tool results、structured output 和 reasoning output 的计量范围；reasoning 是 output 的子集时不得重复计数。
- 跨 provider 的 reasoning effort 名称不是共同算力标尺。模型 report card 使用相同外部资源上限，并显式冻结各 provider 对所选模型公布的默认 reasoning 档位；报告比较的是完整冻结配置，不能把结果表述成脱离配置的模型能力排名。
- Latency 只有在 treatment execution position 平衡、服务器负载可比时才能跨 treatment 解释。
- Dataset taxonomy 不同的 correctness 结果分 corpus 报告，除非存在经过论证的共同 scoring contract。
- Execution、provenance、identity、诊断结构和明确可判定的 claim 由 deterministic scorer
  负责。只有 diagnosis 正确、citation 全部 execution-valid、执行可靠，但 deterministic
  verifier 无法完整判定 evidence sufficiency 的 run 才进入冻结的语义裁决流程。所有符合
  trigger 的 run 必须统一送审，不能按 treatment 或结果挑选；两个不同模型家族独立判断，
  分歧按同一 rubric 人工裁决。Deterministic 结果是主要口径；adjudicated 结果只作为并列
  sensitivity analysis。两套口径必须分别发布 case effect 和推断统计。
- Scorer 判断 causal claim 是否由 cited result 支持，不要求 agent 复刻 canonical SQL、隐藏
  injection timestamp 或精确 source count。查询必须保留 claim 所依赖的 source identity、
  operation、role、status、parent、time 和 result lineage；硬编码、谓词中和、row multiplication、
  truncated result，以及不能证明完整区间的 absence claim 必须 fail closed。规范见
  `SCORING.md`。Transfer scorer 按 `scope × values × claim` 判定：SQL 确定 population 和
  lineage，rows 确定观测事实，claim 的逻辑形式决定完整性要求。Citation 的 `claim_types`
  只记录 agent 意图，不能直接让证据通过。
- Protocol、prompt、scorer、selection、runner representation 或主要指标发生实质变化时，升级 protocol，并禁止与旧 protocol 混合统计。
- `benchmark_protocol()` 必须以机器可读字段声明 treatment estimand 和每个 treatment 的
  agent-facing components。主实验测量完整 Semantic Graph interface，包括 metadata、Graph、
  usage guidance、runtime recovery 和 coverage snapshot，不得表述成纯数据表示效果。

## Dataset 与 source fidelity

- 使用 authoritative、public failure datasets；记录 revision、license、provenance 和下载文件 hash。
- Adapter 必须保留源 schema、timestamp、identity、metric type、span role 和 source defect。不得静默修复或合成缺失事实。
- 不根据名字相似度补造 topology、entity identity、client/server role、relationship 或 causal dependency。
- Graph treatment 只适用于源数据或正式声明能够证明 entity 和 relationship 的 case。数据不支持时明确记录 graph-negative/empty，而不是制造覆盖。
- Reference topology、causal graph 和 ground truth 只用于 selection gate、validation 或 scoring，除非 protocol 明确规定，否则不得暴露给 agent 或作为 telemetry ingest。
- `development` case 可以用于修复工具、prompt 和 scorer；受其行为影响的 case 不得再标成 fresh measurement holdout。
- `measurement` case 必须在查看 agent trajectory 前冻结。记录 eligible set、exclusions、deterministic ranking、replacement 和所有 no-model rejection。
- Public benchmark 的 harness 可以开源；许可证不允许再分发的数据只能通过 pinned downloader 获取，不能提交到仓库。

## Runner 与隔离要求

- API、Codex subscription 和 Claude subscription 必须表达同一 system contract，并记录 runner capability 差异。API transport、provider endpoint、credential source、reasoning effort 和 reasoning/visible output 共享的预算必须由执行协议显式绑定，不能根据模型名或 provider 默认值推断。不同币种的 provider 成本必须保留原币种，未冻结汇率时不得聚合。SQL 默认返回 200 行，agent 可逐次显式提高到 1000 行；截断结果不能成为最终 citation，runner 必须给 agent 一次可修复的错误反馈。
- Tenant-specific provider endpoints stay in runtime configuration. The protocol must validate the provider, region, and API surface without publishing workspace identifiers.
- DeepSeek, BigModel, and DashScope API clients must bypass process-level proxy environment variables. OpenAI and Anthropic retain the operator's environment routing.
- Subscription runner 不能静默回退到 API billing。Provider credentials 和 endpoint overrides 不得传入 subscription child process。
- Tool-call cap 必须对 agent 可见；未知工具和 cap rejection 分开记录。Turn exhaustion 或 runner failure 应持久化为可评分失败，不能中断整个 batch。
- 一个正式 case 使用独占 GreptimeDB instance。Semantic Graph 会枚举实例中的 user schemas，单纯使用不同 database 不能保证隔离。
- 启动临时实例前确认 endpoint、进程和数据目录。只停止本任务启动的精确进程，不得修改或停止用户已有的 `localhost:4000` 或其他实例。
- 未经用户明确授权，不运行付费全量 RCA、批量模型实验或会消耗大量 subscription quota 的任务。先执行 no-model gate 和最小验证。

## Benchmark 1.0 与研究结论边界

`v24` 至 `v32` 等 protocol 编号是内部研发周期标识，用于区分工具、prompt、
scorer 和实验装置的迭代，不是公开发布版本。冻结前的运行均为研发实验；当前代码不为
旧研发周期保留 loader、scorer、resume、renderer 或其他兼容层。

正式发布以 Git release tag 为复现边界。第一次完整正式运行必须从同一个冻结 commit
执行全部纳入报告的 case 和模型；公开 artifact 记录 tag、commit、GreptimeDB revision、
数据 checksum、protocol、selection 和 scorer hash。对应 tag 必须自带复算 scorer、主要
指标和生成报告所需的完整代码。后续改变正式实验表面时发布新 tag，不要求 `main` 读取
旧 tag 的报告。模型重跑用于 replication；公开的 sanitized trajectories 和 tool results
用于确定性复算已经发布的评分与统计。

第一个公开版本是可执行、可审计的 benchmark 产品，不以证明跨系统普遍效果为
完成条件。1.0 至少需要交付：

1. 冻结且版本化的 benchmark specification、paired treatments、canonical API runner contract、deterministic hard gates、语义裁决契约和 case-level 统计方法。
2. 第三方可执行的数据获取、ingestion、no-model audit、agent run、public audit artifact export 和 report generation workflow。
3. 一个小型、固定且可合法公开复现的 reference cohort，覆盖 Semantic Graph positive 和 negative applicability roles；不要求用 case 数量证明总体效果。
4. 从 retrieval micro-benchmark 到完整 RCA 的 correctness-preserving transfer demonstration，并公开每个 case 的 effect size、负结果和 applicability boundary。
5. Machine-readable public summary、sanitized audit-artifact hashes、reproduction commands、英文与中文报告，以及已知限制。

首个公开报告包含五个冻结模型配置，但模型之间不做 pooled score，模型数量也不构成
统计样本量。跨多个独立 system families 的 powered effect estimate、
correctness-efficiency Pareto frontier 和 catalog broad-recall study 是后续研究交付。
要发布宽泛的 semantic-layer effect claim，必须先冻结 practical
effect threshold、power analysis、multiplicity policy 和独立 case enrollment；
repetitions 不能计入样本量。不要把一个可运行的 1.0 benchmark 表述成已经完成的
confirmatory study。

## Source of truth 与仓库地图

- `PLAN.md`：canonical objective、experiment design、milestones 和当前阶段。
- `SCORING.md`：端到端 RCA correctness、claim grounding、citation、reliability 和 efficiency eligibility 的规范。
- `src/semantic_rca_bench/protocol.py`：当前机器可读 protocol identifiers。
- `DISCOVERY.md`：Table Semantics discovery micro-benchmark contract。
- `GRAPH.md`：Semantic Graph micro-benchmark contract。
- `DATASETS.md`：dataset provenance、fidelity、license、selection 和 rejection audit。
- `RESULTS.md`、`RESULTS.zh-CN.md`：英文历史结果和中文结论报告。
- `fixtures/measurement/`：冻结的 measurement fixtures、selection manifests 和 tracked aggregate。
- `src/semantic_rca_bench/cli.py`：smoke、audit、run、batch 和 render 命令入口，以及 report resume contract。
- `src/semantic_rca_bench/discovery.py`：Discovery task、fixture、runner 和 deterministic scorer。
- `src/semantic_rca_bench/graph_benchmark.py`：Graph task、raw-edge audit、runner 和 deterministic scorer。
- `src/semantic_rca_bench/evaluation.py`：端到端 RCA correctness、evidence validity 和 trajectory metrics。
- `src/semantic_rca_bench/evidence.py`：runner 和 scorer 共用的 execution-valid citation 契约。
- `src/semantic_rca_bench/report.py`、`assets/report.html`：combined report、case-level inference、token/cost accounting 和 report card UI。
- `src/semantic_rca_bench/measurement_summary.py`：从 ignored formal reports 生成 case-level aggregate 和 report hashes。
- `fixtures/reference/semantic-rca-v32-five-model-suite.json`：当前 360-cell suite 的 case、模型、GreptimeDB revision、release build profile、协议和文件哈希绑定。
- `src/semantic_rca_bench/formal_suite_protocol.py`、`formal_suite.py`：160 个 Discovery/Graph cells 的 schedule、独占实例 no-model preflight、exact-prefix resume 和确定性重评分。
- `src/semantic_rca_bench/formal_suite_release.py`：micro measurement artifact 的脱敏、公开重评分和按模型/benchmark 的 case-level aggregate；不得在这里创建跨任务或跨模型总分。
- `src/semantic_rca_bench/datasets/openrca2_transfer.py`、`edge_audit.py`：十个 fresh transfer cases 的冻结 loader、source-faithful replay、独立 raw-span edge reconstruction 和完整 Graph equality gate。
- `src/semantic_rca_bench/transfer_scorer.py`、`transfer_protocol.py`、`transfer_formal.py`、`transfer_release.py`：transfer claim grounding、24-cell development pilot、200-cell measurement schedule、resume、脱敏和公开确定性重评分。
- `src/semantic_rca_bench/transfer_adjudication.py`：对 deterministic grounding 未决、但硬门均通过的 run 生成去模型/去 treatment 标签的语义裁决输入，并验证双模型裁决与人工分歧处理。
- `src/semantic_rca_bench/formal_report.py`、`assets/formal-measurement-report.html`：从两份脱敏 measurement artifact 生成综合 JSON 和自包含 HTML；只能做分 benchmark、分模型汇总，不得创建 pooled score。
- `src/semantic_rca_bench/selection.py`：离线构造和审计 selection manifest 的 deterministic ranking primitive；运行时 CLI 不重新选 case。
- `src/semantic_rca_bench/datasets/`：dataset adapters 和 source audits。
- `src/semantic_rca_bench/agent.py`、`subscription.py`：API 与 subscription runner contracts。
- `src/semantic_rca_bench/greptimedb/`：query visibility、semantic profile 和 GreptimeDB client boundary。
- `tests/`：scorer、runner、adapter、protocol 和 regression tests。

实现与文档不一致时，不要凭文档猜测。读取代码、正式 report JSON、selection manifest 和测试，确定事实后修复 source of truth 的漂移。

## 工作流程

1. 编辑前运行 `git status --short`，保留用户和其他进程的无关修改。
2. 复杂实验先明确 main use case、estimand、统计单位、selection、no-model gates、验收条件和不在范围内的工作。
3. 先完成 source/data audit 和 deterministic gates，再调用模型。
4. 使用 development cases 修复实现；冻结协议后再选择未受 trajectory 影响的 measurement cases。
5. 每个结论引用实际 report、query result、source artifact 或测试。不能确认时明确标记未知，不要猜测。
6. 结果不支持原假设时直接报告，不调 scorer、删 case 或扩大解释范围。
7. Commit、push、PR 和外部发布只在用户明确要求时执行。Commit 使用 conventional title 和 `git commit -s`，不添加 AI 署名。

## 常用命令

项目使用 Python 3.11 和 `uv`。

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv lock --check
```

查看 benchmark 主流程入口：

```bash
uv run semantic-rca smoke --help
uv run semantic-rca smoke-rca100 --help
uv run semantic-rca smoke-openrca --help
uv run semantic-rca smoke-openrca2 --help
uv run semantic-rca discovery-audit --help
uv run semantic-rca discovery-run --help
uv run semantic-rca graph-audit --help
uv run semantic-rca graph-run --help
uv run semantic-rca formal-suite-micro-preflight --help
uv run semantic-rca formal-suite-micro-run --help
uv run semantic-rca formal-suite-micro-export --help
uv run semantic-rca formal-suite-report --help
uv run semantic-rca transfer-selection-audit --help
uv run semantic-rca transfer-preflight --help
uv run semantic-rca transfer-run --help
uv run semantic-rca transfer-export --help
uv run semantic-rca run --help
uv run semantic-rca batch --help
uv run semantic-rca render --help
```

参数、隔离要求和完整 workflow 以 `README.md`、`DISCOVERY.md` 和 `GRAPH.md`
为准。`*-run`、`run` 和 `batch` 会调用模型；未通过 no-model gate 或未经授权时
不得执行。

从保留在 `.reports/` 的正式 micro-benchmark reports 重新生成 tracked summary：

```bash
uv run python -m semantic_rca_bench.measurement_summary
```

生成后比较 `fixtures/measurement/results-summary.json`，确认 report hashes、selection manifest 和统计结果一致。`.cache/`、`.data/`、`.instances/`、`.runs/` 和 `.reports/` 是本地 ignored artifacts，不得提交 telemetry、credentials 或未审计的模型 trajectories。

Ignored raw reports 只有 hash 时不能满足公开复现要求，也不得直接作为发布合同。
1.0 headline 所依赖的 run 必须有可合法发布、可独立复算 scorer 和主要指标的审计
artifact，并删除 provider raw responses、credentials 和本机信息。Artifact contract
只在 public cohort 和 transfer scorer 冻结后定义，不能从当前内部 report 格式反推。
当前缺口和验收条件以 `PLAN.md` 为准。

验证从覆盖改动的最窄测试开始，再根据影响范围扩展到完整测试。只报告实际执行过的命令和真实结果。
