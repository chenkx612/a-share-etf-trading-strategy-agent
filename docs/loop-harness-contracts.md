# Loop Harness Contracts

## Task

```toml
id = "strategy-research"
goal = "Develop a strategy from a testable hypothesis"

[budget]
max_rounds = 10
max_hours = 4
max_consecutive_failures = 3

[codex]
sandbox = "workspace-write"
approval_policy = "never"
timeout_minutes = 60

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
  "--run-id", "{run_id}"
]
metrics_path = "outputs/backtests/{run_id}/metrics.json"

[evaluation]
mode = "fixed"
objective = "sortino"

[evaluation.constraints]
max_drawdown = 0.20

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

`baseline` 可选。`{python}` 由 Harness 替换为自身解释器。

当前仅支持固定 development/gate 区间；walk-forward 在后续阶段实现。

`workspace-write` 允许 Codex 修改策略、创建测试并执行命令，但不能修改工作区外的文件。`approval_policy = "never"` 防止无人值守任务等待审批。

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
  "hypothesis": "A testable hypothesis",
  "changes": {
    "summary": "Implemented the candidate strategy",
    "files": ["src/quant_core/strategy/candidate.py"]
  },
  "metrics": {
    "development": {"sortino": 1.4},
    "gate": {"sortino": 1.1}
  }
}
```

Codex 的结构化输出、完整 JSONL 事件、测试日志和回测日志保存在实验目录中。

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

研发状态保存到 `.research/<task-id>/`。每轮 candidate 从该任务的当前 champion 文件快照创建；
候选满足门禁约束且目标指标至少改善 `minimum_improvement` 时晋升，否则清理。Harness 不会创建
Git commit。行情和因子输入在任务初始化时冻结；candidate 只获得截止 `development.end` 的数据，
gate 和 champion 使用带完整数据的一次性 evaluator 副本。这里的数据隔离用于避免正常研发流程
接触门禁数据，不构成限制工作区外读取的安全边界。

## Automated loop

```bash
quant-agent --root <workspace> research loop \
  --task <task.toml> \
  --research-root .research
```

循环状态保存在 `.research/<task-id>/loop-state.json`。循环在达到 `max_rounds`、`max_hours` 或
`max_consecutive_failures` 后停止；配置 `evaluation.target.objective_at_least` 时，champion 的 gate
目标指标达到阈值且满足约束也会停止。总时长用于判断是否启动下一轮，不会缩短已经开始的单轮
超时。`rejected` 不计入连续失败；中断的未完成轮次在恢复时计为 `failed`。
