# Loop Harness

## 目标

用确定性 Python Harness 控制 Codex 长时间自动研发交易策略：

- 从 0 到 1 开发新策略。
- 从 1 到 N 优化已有策略。
- 连续运行直到达成目标或耗尽预算。
- 每轮使用独立 `codex exec` 会话。
- 实验可评测、可记录、可恢复。

## 理想工作流

```text
人工启动 Harness
→ 从 champion 创建候选
→ codex exec 在开发集内循环研发
→ Harness 运行测试和门禁评测
→ 接受或拒绝候选
→ 保存状态
→ 自动启动下一轮
→ 达成目标或耗尽预算后停止
```

Codex 负责单轮研发。Python Harness 负责外层循环、权限、超时、评测、版本和停止条件。

阶段二不会把门禁区间写入 Codex Prompt。阶段三让 candidate 只包含开发期数据，但这属于
研发流程隔离，不是限制工作区外读取的安全边界；强物理隔离留到长时间无人值守运行前实现。

## 所需技术

- 任务配置：目标、数据、修改范围、评测规则、预算和 Codex 权限。
- 单轮执行器：以独立 `workspace-write` 的 `codex exec` 会话运行研发任务。
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

状态：已完成并通过真实 `codex exec` 演练。

实现 `research run-once`：启动 Codex、允许其修改文件和执行命令、限制单轮超时、检查修改范围、执行门禁评测并生成结果。

验收：一条命令生成完整实验目录和 `result.json`。

### 阶段三：管理候选版本

状态：已完成。

按 `task.id` 在 `.research/<task-id>/` 隔离研发任务。baseline、champion 和 candidate
使用文件快照管理，不创建 Git commit。受管理单轮从 champion 创建 candidate，评测后仅将
满足约束且门禁目标优于 champion 的候选晋升；失败和拒绝的候选会被清理。
gate 和 champion 回测在一次性 evaluator 副本中执行，评测产物不会写回 candidate 或 champion。
版本快照只保存代码；任务初始化时冻结完整评测数据，并生成截止 `development.end` 的开发数据。
candidate 只注入开发数据，Codex 结束后 evaluator 才换入完整数据。这样既避免每代 champion
重复保存大体积数据，也保证各轮使用同一数据基准。该机制不阻止进程主动读取 candidate 之外
的路径，因此不作为强安全边界。

```text
.research/<task-id>/
├── state.json
├── runtime/
│   ├── development/
│   └── evaluation/
├── versions/
├── candidates/
└── experiments/<experiment-id>/
```

每个实验永久保存 `result.json`、`decision.json` 和已有的执行日志；成功产生合法候选修改时额外
保存 `candidate.patch`。最终 champion 仍是研发中间结果，只有人工明确采用后才进入真实工作区
和 Git。

验收：失败实验不会污染下一轮。

### 阶段四：自动多轮循环

状态：已完成。按当前资源约束不做数小时真实长跑验收。

`research loop` 在 `run-managed` 外层自动续轮。`.research/<task-id>/state.json` 继续管理
champion，新增 `loop-state.json` 保存当前循环的轮数、累计运行时间、连续失败、当前实验和停止原因。
`rejected` 是正常研究结果，不计为失败；只有 `failed` 增加连续失败计数。中断后已有决策的轮次会
补记结果，没有决策的轮次记为一次中断失败。可选的 `evaluation.target.objective_at_least` 用于在
champion 的 gate 指标达到目标且满足约束时提前停止。

验收：一次启动可持续运行数小时，直到达成目标或耗尽预算。

### 阶段五：验证两类任务

分别验证 0 → 1 新策略研发和 1 → N 策略优化。

MVP 暂不实现数据库、并行 Worker、自动部署和实盘交易。
