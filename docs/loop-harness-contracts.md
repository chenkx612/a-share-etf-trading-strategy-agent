# Loop Harness Contracts

## Task

```toml
id = "strategy-research"
goal = "Develop a strategy from a testable hypothesis"

[budget]
max_rounds = 10
max_hours = 4
max_consecutive_failures = 3

[opencode]
model = "xai/grok-4.5"
variant = "high"
timeout_minutes = 60

[strategy]
name = "example-strategy"
module = "quant_core.strategy.example_strategy"

[baseline]
mode = "workspace"
# For strict from-zero strategy research:
# mode = "none"
# exclude = ["src/quant_core/strategy/"]

[data]
universe = "path/to/universe.csv"

[scope]
editable = ["src/quant_core/strategy/", "tests/"]
forbidden = ["src/quant_core/backtest/", "data/"]

[commands]
test = ["{python}", "-m", "pytest", "-q"]
backtest = [
  "{python}", "-m", "quant_core.cli", "backtest", "run",
  "--universe", "{universe}", "--start", "{start}", "--end", "{end}",
  "--run-id", "{run_id}", "--strategy", "{strategy_name}"
]
metrics_path = "outputs/backtests/{run_id}/metrics.json"

[evaluation]
mode = "fixed"
objective = "sortino"

[evaluation.constraints]
annual_return = { operator = ">=", threshold = 0.10 }
max_drawdown = { operator = "abs<=", threshold = 0.20 }

[evaluation.acceptance]
minimum_improvement = 0.01

[evaluation.target]
objective_at_least = 1.50

[evaluation.fixed.development]
start = "2018-01-01"
end = "2021-12-31"

[evaluation.fixed.gate]
start = "2022-01-01"
end = "2024-12-31"

[evaluation.test]
start = "2025-01-01"
end = "2025-12-31"
```

`strategy.name` 和 `strategy.module` 显式标识被评测的策略。配置了 `strategy` 时，回测命令必须
引用 `{strategy_name}` 或 `{strategy_module}`，避免策略元数据与实际评测对象脱节。旧任务可以
省略该表以保持兼容。

`{python}` 由 Harness 替换为自身解释器。`baseline.mode` 默认为 `workspace`，即把任务首次初始化
时的工作区快照作为初始 champion。0→1 研发可设置 `mode = "none"`：首个目标指标有效且满足全部
硬约束的候选成为初始 champion，之后才应用 `minimum_improvement`。可通过 `exclude` 从初始
候选中移除已有策略实现，同时保留数据、回测和 CLI 等基础设施。

当前仅支持固定 development/gate 区间；walk-forward 在后续阶段实现。
`evaluation.test` 为可选保留区间，研发循环不会读取或评测该区间。

硬约束必须使用显式的 `operator`/`threshold` 表达式，支持 `>=`、`<=` 和 `abs<=`，不接受省略
运算方向的纯数字配置。

`model` 使用 OpenCode 的 `provider/model` 格式。支持推理强度的模型通过 `variant` 选择档位；
Harness 将其传给 `opencode run --variant`。Harness 以 `opencode run --auto --format json` 启动
无人值守任务，并通过 `OPENCODE_PERMISSION` 禁止访问工作区外目录和运行中提问。
OpenCode 的权限规则不是操作系统级沙箱；高风险任务应在容器或隔离的 worktree 中运行。

可用的模型配置示例：

| 模型 | `model` 配置 | 最大推理强度 | Provider |
| --- | --- | --- | --- |
| Grok 4.5 | `xai/grok-4.5` | `variant = "high"` | xAI |
| DeepSeek V4 Pro | `deepseek/deepseek-v4-pro` | `variant = "max"` | DeepSeek |
| HY3 Free | `opencode/hy3-free` | 不支持配置，省略 `variant` | OpenCode Zen |

任务配置默认选用模型支持的最大推理强度：Grok 4.5 使用 `high`，DeepSeek V4 Pro 使用 `max`。
切换模型时必须同时填写表中对应的 `variant`。HY3 Free 当前没有可配置的推理强度，因此应
省略 `variant`，Harness 也不会传递 `--variant`。

同一模型通过不同 Provider 调用时，其认证方式、价格和限额可能不同。使用前先执行
`opencode auth list` 检查对应 Provider 的认证，再通过 `opencode models <provider>` 确认当前
OpenCode 版本识别的模型 ID；模型目录可能随 Provider 更新。

运行前必须确保：

- Python 依赖已经安装。
- 数据和回测输出位于工作区内。
- 测试与回测命令不依赖系统级修改。

## Result

Harness 写入 `result.json`：

```json
{
  "experiment_id": "experiment-001",
  "status": "completed",
  "feedback": "Filled by the next round after this round's gate decision is available",
  "hypothesis": "A testable hypothesis",
  "attempts": "Tested the base signal and one volatility-filtered variant",
  "development_effect": "The filtered variant improved development stability",
  "candidate": "Retained the volatility-filtered candidate implementation",
  "changes": {
    "files": ["src/quant_core/strategy/candidate.py"]
  },
  "metrics": {
    "development": {"sortino": 1.4},
    "gate": {"sortino": 1.1}
  }
}
```

OpenCode 的结构化输出、完整 JSONL 事件、测试日志和回测日志保存在实验目录中。
一轮结束时还没有自己的 gate 结论，因此该轮的 `feedback` 先留空，由下一轮 AI 在开头根据已产生的
受控决策补齐。每轮只强制补上一轮的简短反馈，更早记录仅作为隐式推理上下文。`attempts` 记录本轮在
开发集上试过的方案和变体，不表示 gate 拒绝或未晋升 champion；`development_effect` 记录开发集效果，
`candidate` 记录实际提交给 Harness 做 gate 评测的最终候选方案。Harness 不会向研发 AI 注入精确
gate 指标或 gate 区间。

## Run once

```bash
quant-agent --root <workspace> research run-once \
  --task <task.toml> \
  --experiment-id experiment-001 \
  --output <experiment-dir>
```

## Managed candidate

阶段三使用受管理入口：

```bash
quant-agent --root <workspace> research run-managed \
  --task <task.toml> \
  --experiment-id experiment-001 \
  --research-root .research
```

研发状态保存到 `.research/<task-id>/`。Harness 用临时 Git commit 捕获任务启动时的工作区状态，
不修改当前分支或 index；每轮 candidate 是从当前 champion commit 创建的 detached worktree。
候选满足门禁约束且目标指标至少改善 `minimum_improvement` 时提交并推进任务专属 champion ref，
否则删除 worktree。行情和因子输入在任务初始化时冻结；candidate 只获得截止 `development.end` 的数据，
gate 和 champion 使用带完整数据的一次性 evaluator 副本。这里的数据隔离用于避免正常研发流程
接触门禁数据，不构成限制工作区外读取的安全边界。

当 `baseline.mode = "none"` 时，首个 champion 产生前，每轮 candidate 都从同一 seed commit
创建，不执行基线回测；`baseline.exclude` 指定的现有实现不会进入该快照。不满足硬约束的候选会
被拒绝且不会改变该快照。

严格的 0→1 任务应使用 Harness 提供的固定 evaluator，而不是候选可修改的 CLI。候选实现
`quant_core.strategy.research_candidate.select(daily, universe, start, end)`，返回包含 `date`、
`symbol`、`target_weight` 的 DataFrame；evaluator 负责校验标的、日期和权重，并用不可编辑的
回测引擎生成指标。

## Automated loop

```bash
quant-agent --root <workspace> research loop \
  --task <task.toml> \
  --research-root .research
```

循环状态保存在 `.research/<task-id>/loop-state.json`。循环在达到 `max_rounds`、`max_hours` 或
`max_consecutive_failures` 后停止；配置 `evaluation.target.objective_at_least` 时，champion 的 gate
目标指标达到阈值且满足约束也会停止。总时长用于判断是否启动下一轮，不会缩短已经开始的单轮
超时。`rejected` 不计入连续失败；中断的未完成轮次在恢复时计为 `failed`。后续轮次直接从各实验的
`result.json`、`decision.json` 和 `agent-output.json` 动态构建受控研究历史，不维护重复的记忆文件。
