# Loop Harness Contracts

## Task

`task.toml` describes one research campaign. `baseline` is optional. `evaluation.mode` is `fixed` or `walk_forward`.
Harness must withhold the `evaluation.test` section from Codex until the loop ends.

```toml
id = "strategy-research"
goal = "Develop a strategy from a testable hypothesis"
max_iterations = 3

[data]
universe = "path/to/universe.csv"

[scope]
editable = ["src/quant_core/strategy/", "tests/"]
forbidden = ["src/quant_core/backtest/", "data/"]

[commands]
test = ["pytest"]
backtest = ["python3", "-m", "quant_core.cli", "backtest", "run"]

[evaluation]
mode = "fixed"
objective = "sortino"

[evaluation.constraints]
max_drawdown = 0.20

[evaluation.fixed.train]
start = "2018-01-01"
end = "2021-12-31"

[evaluation.fixed.validation]
start = "2022-01-01"
end = "2024-12-31"

[evaluation.test]
start = "2025-01-01"
end = "2025-12-31"
```

For walk-forward evaluation, replace `evaluation.fixed` with:

```toml
[evaluation]
mode = "walk_forward"
objective = "sortino"

[evaluation.constraints]
max_drawdown = 0.20

[evaluation.walk_forward]
start = "2018-01-01"
end = "2024-12-31"
train_months = 36
validation_months = 12
step_months = 12
```

## Result

Harness writes `result.json` from Codex output and command results. It records facts only; Harness decides acceptance. Test metrics are never included during the loop.

```json
{
  "experiment_id": "experiment-001",
  "status": "completed",
  "hypothesis": "A testable strategy hypothesis",
  "changes": {
    "summary": "Implement the first strategy prototype",
    "files": ["src/quant_core/strategy/candidate.py"]
  },
  "verification": {
    "tests_passed": true,
    "backtest_completed": true
  },
  "metrics": {
    "train": {
      "sortino": 1.4,
      "max_drawdown": -0.12
    },
    "validation": {
      "sortino": 1.1,
      "max_drawdown": -0.16
    }
  }
}
```
