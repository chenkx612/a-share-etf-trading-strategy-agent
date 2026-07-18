# Loop Harness

## 目标

用确定性 Python Harness 控制 OpenCode 长时间自动研发交易策略：

- 从 0 到 1 开发新策略。
- 从 1 到 N 优化已有策略。
- 连续运行直到达成目标或耗尽预算。
- 每轮使用独立 `opencode run` 会话。
- 实验可评测、可记录、可恢复。

## 理想工作流

```text
人工启动 Harness
→ 从 champion 创建候选
→ opencode run 在开发集内循环研发
→ Harness 运行测试和门禁评测
→ 接受或拒绝候选
→ 保存受控研究记忆
→ 保存状态
→ 自动启动下一轮
→ 达成目标或耗尽预算后停止
→ 独立 OpenCode 会话生成终局复盘报告
```

OpenCode 负责单轮研发。Python Harness 负责外层循环、权限、超时、评测、版本和停止条件。

阶段二不会把门禁区间写入 OpenCode Prompt。阶段三让 candidate 只包含开发期数据，但这属于
研发流程隔离，不是限制工作区外读取的安全边界；强物理隔离留到长时间无人值守运行前实现。

## 所需技术

- 任务配置：目标、数据、修改范围、评测规则、预算和 OpenCode 模型。
- 单轮执行器：以独立 `opencode run --auto --format json` 会话运行研发任务。
- 评测器：独立运行测试、开发集和门禁集回测。
- 实验存储：保存事件日志、代码差异、指标和结论。
- 状态与版本：管理 baseline、champion、候选和失败恢复。
- 循环控制器：管理轮数、总时长、连续失败和人工停止。

## 分阶段目标

### 阶段一：定义契约

状态：MVP 已完成。

定义 `task.toml` 和 `result.json`。

验收：配置和结果可由程序校验。

### 阶段二：打通单轮研发

状态：已迁移为 OpenCode 执行器，自动化测试覆盖命令和事件解析。

实现 `research run-once`：启动 OpenCode、允许其修改文件和执行命令、限制单轮超时、检查修改范围、执行门禁评测并生成结果。

验收：一条命令生成完整实验目录和 `result.json`。

### 阶段三：管理候选版本

状态：已完成。

按 `task.id` 在 `.research/<task-id>/` 隔离研发任务。Harness 用无持久 ref 的临时 Git commit
构造 detached worktree，不修改当前分支或 index；当前项目提供框架基座，`champion.py` 覆盖
任务声明的唯一策略脚本。
评测后，候选必须满足全部硬约束；若 champion 不满足硬约束，首个目标指标有效的合格候选直接
晋升。champion 合格后，候选还必须按配置幅度改善门禁目标，才会由 Harness 原子替换
`champion.py`；失败和拒绝的 worktree 会被删除。
candidate 最初只注入开发数据；OpenCode 退出后，Harness 才在该 worktree 中换入完整数据运行
gate。已有 champion 需要重新评测时使用一次性 evaluator worktree。临时 commit 只服务于
worktree 创建且不作为 Champion 存储；任务
初始化时冻结完整评测数据，并生成截止 `development.end` 的开发数据。这样既避免重复保存大体积
数据，也保证各轮使用同一数据基准。该机制不阻止进程主动读取 candidate 之外的路径，因此不作为
强安全边界。

0→1 任务可配置无 baseline。首个 champion 产生前，候选从同一初始框架快照创建，只按目标指标
有效性和硬约束判定；还可从该快照排除已有策略实现。首个合格候选晋升后，再启用相对 champion
的改善要求。

0→1 任务通过固定 research evaluator 调用候选的标准 `select` 接口，候选不能修改 evaluator、
回测引擎或指标计算。这样策略研发与评测实现解耦，也避免通过修改 CLI 影响 Gate 结果。

```text
.research/<task-id>/
├── champion.json
├── champion.py
├── runs/
│   └── 001/
│       ├── state.json
│       ├── report.md
│       └── rounds/
│           └── 001/
│               ├── result.json
│               ├── decision.json
│               └── candidate.patch
├── .cache/runtime/
│   └── evaluation/
└── .tmp/
    ├── runs/001/events.jsonl
    └── worktrees/001/
        ├── candidates/
        └── evaluators/
```

每个 Round 永久保存 `result.json` 和 `decision.json`；成功产生合法候选修改时额外保存
`candidate.patch`。成功轮次在结构化结果写入后删除重复的 Agent 输出、原始事件流和成功命令
日志；失败轮次保留相关原始事件与失败日志用于诊断。Development 数据是从冻结 Evaluation
数据派生的缓存，Loop 结束后删除并在需要时重建。worktree 只属于临时运行状态，正常结束、
可处理的中断以及下一次初始化都会清理。

最终 Champion 的完整策略源码保存在 `champion.py`，`champion.json` 记录 SHA-256、来源
Run/Round、指标和项目 revision。它不会自动覆盖当前工作分支中的策略脚本。

验收：失败 Round 不会污染下一轮。

### 阶段四：自动多轮循环

状态：已完成。按当前资源约束不做数小时真实长跑验收。

`research loop` 在内部受管理单轮执行器外层自动续轮。`.research/<task-id>/champion.json` 管理
跨 Run 的 champion；每次新 Loop 自动分配下一个三位数字 Run，Run 内的 `state.json` 保存轮数、
累计运行时间、连续失败、当前 Round 和停止原因。每个 Run 的 Round 从 `001` 重新编号。
每轮会直接从已有 Round 记录构建研究历史。下一轮 OpenCode 先读取最近的受控研究历史，
只为上一轮补齐一条简短 `feedback`，更早历史仅用于隐式推理，然后再提出新假设。本轮结束时必须
记录假设、开发集阶段尝试过的方案、开发集效果和最终候选结果，供后续轮次快速理解。研发 Prompt
会明确 objective、相对 champion 的最小改善要求和全部硬约束，但历史不包含精确 gate 指标或 gate
区间；`rejected` 只表示该次具体实现没有胜出，`failed` 不形成策略结论。
硬约束使用显式 `>=`、`<=` 或 `abs<=` 运算符，因此既能表达年化收益率下限，也能表达最大回撤、
换手率等上限。
`rejected` 是正常研究结果，不计为失败；只有 `failed` 增加连续失败计数。中断后已有决策的轮次会
补记结果，没有决策的轮次记为一次中断失败。可选的 `evaluation.target.objective_at_least` 用于在
champion 的 gate 指标达到目标且满足约束时提前停止。

验收：一次启动可持续运行数小时，直到达成目标或耗尽预算。

### 实时可观测性

Harness 在 Run 和 Round 开始、Agent 完成、测试、Development/Gate 回测、决策和停止时向 stdout
输出带 `[run/round]` 前缀的简洁事件，并同步追加到 `.tmp/runs/<run>/events.jsonl`。启动 Loop 的
Codex 可以直接观察控制台或轮询该文件；Run 正常结束后删除临时事件流，技术失败的具体日志则保留
在对应 Round 中。

### 终局复盘

Loop 正常停止后，Harness 启动一次独立、只读的 OpenCode 会话，读取本次 loop 对应的
`result.json`、`decision.json`、最终 champion 指标和 champion 策略源码，生成当前 Run 的
`report.md`。报告逐轮说明
假设、开发集尝试、development/gate 效果、决策和启发，并单独描述最终 champion 的交易逻辑与
确定参数。

报告会话使用任务配置的同一模型和推理强度，最长运行 10 分钟；禁止 Bash、代码编辑、Skill、
Web 和子任务工具。精确 gate 指标只在循环已经结束后用于复盘，不会反馈给后续研发轮次。
Run 的 `state.json` 显式记录本次运行的 Round ID；物理目录边界确保报告不会混入其他 Run。
该状态通过 `report_status`、`report_path` 和 `report_error` 记录报告状态。成功生成
报告后删除原始报告事件；报告失败时保留事件用于诊断。报告失败不会改变已完成的研究决策或
champion，也不会把成功 loop 标记为失败。

可单独补生成或重试报告：

```bash
quant-agent --root <workspace> research report \
  --task <task.toml> \
  --run 1 \
  --research-root .research
```

可清理旧版成功日志、中断 worktree 和派生 Development 缓存：

```bash
quant-agent --root <workspace> research clean \
  --task-id <task-id> \
  --research-root .research
```

### 阶段五：验证两类任务

分别验证 0 → 1 新策略研发和 1 → N 策略优化。

MVP 暂不实现数据库、并行 Worker、自动部署和实盘交易。
