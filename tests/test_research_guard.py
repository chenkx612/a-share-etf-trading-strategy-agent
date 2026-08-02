from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Sequence

import pandas as pd
import pytest

from quant_core.research.guard import universe_equal_weight_annual_return
from quant_core.research.contracts import ResearchTask
from quant_core.research.runner import GuardEvaluationError, _evaluate_guard


def _write_inputs(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    pd.DataFrame({"symbol": ["A", "B"], "name": ["A", "B"]}).to_csv(
        tmp_path / "universe.csv", index=False
    )
    (tmp_path / "data").mkdir()
    pd.DataFrame(rows).to_csv(tmp_path / "data/etf_daily.csv", index=False)
    return tmp_path / "universe.csv"


def test_equal_weight_uses_real_adjacent_opens_and_terminal_zero_return(
    tmp_path: Path,
) -> None:
    universe = _write_inputs(tmp_path, [
        {"date": "2025-01-01", "symbol": "A", "open": 10.0},
        {"date": "2025-01-02", "symbol": "A", "open": 11.0},
        {"date": "2025-01-03", "symbol": "A", "open": 12.0},
        {"date": "2025-01-02", "symbol": "B", "open": 20.0},
        {"date": "2025-01-03", "symbol": "B", "open": 18.0},
    ])

    annual = universe_equal_weight_annual_return(
        tmp_path, universe, {"start": "2025-01-01", "end": "2025-01-03"}
    )

    # Jan 1 has only A (+10%); Jan 2 has A (+9.09%) and B (-10%).
    expected = ((1.10 * (1.0 + ((12 / 11 - 1) + (18 / 20 - 1)) / 2)) ** (252 / 3)) - 1
    assert annual == pytest.approx(expected)


def test_equal_weight_rejects_an_empty_valid_cross_section(tmp_path: Path) -> None:
    universe = _write_inputs(tmp_path, [
        {"date": "2025-01-01", "symbol": "A", "open": 10.0},
        {"date": "2025-01-02", "symbol": "B", "open": 20.0},
    ])

    with pytest.raises(ValueError, match="empty valid cross-section"):
        universe_equal_weight_annual_return(
            tmp_path, universe, {"start": "2025-01-01", "end": "2025-01-02"}
        )


def test_guard_rejects_mutation_of_its_fresh_evaluator_inputs(tmp_path: Path) -> None:
    universe = _write_inputs(tmp_path, [
        {"date": "2025-01-01", "symbol": "A", "open": 10.0},
        {"date": "2025-01-02", "symbol": "A", "open": 11.0},
        {"date": "2025-01-03", "symbol": "A", "open": 12.0},
        {"date": "2025-01-04", "symbol": "A", "open": 13.0},
    ])
    (tmp_path / "strategy.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    task = ResearchTask.from_mapping({
        "id": "guard-mutation",
        "goal": "Verify Guard isolation",
        "budget": {
            "max_rounds": 1,
            "max_hours": 1,
            "max_consecutive_failures": 1,
            "round_minutes": 1,
        },
        "opencode": {"model": "test/model"},
        "execution": {"command_timeout_minutes": 1},
        "data": {"universe": universe.name},
        "scope": {"editable": ["strategy.py"]},
        "commands": {
            "tests": ["tests/test_strategy.py"],
            "backtest": [
                "backtest", "--start", "{start}", "--end", "{end}",
                "--run-id", "{run_id}",
            ],
        },
        "evaluation": {
            "mode": "fixed",
            "objective": "sortino",
            "constraints": {
                "max_drawdown": {"operator": "abs<=", "threshold": 0.2},
            },
            "fixed": {
                "development": {"start": "2024-01-01", "end": "2024-06-30"},
                "gate": {"start": "2025-01-01", "end": "2025-01-02"},
                "guard": {"start": "2025-01-03", "end": "2025-01-04"},
            },
            "guard": {
                "benchmark": "universe_equal_weight",
                "max_excess_annual_return_degradation": 0.1,
            },
        },
    })
    experiment = tmp_path / "evidence"
    experiment.mkdir()

    def mutating_runner(
        command: Sequence[str], cwd: Path, log_path: Path, timeout: int
    ) -> int:
        (cwd / "universe.csv").write_text("symbol,name\nA,changed\n", encoding="utf-8")
        run_id = command[command.index("--run-id") + 1]
        metrics = cwd / "outputs/backtests" / run_id / "metrics.json"
        metrics.parent.mkdir(parents=True)
        metrics.write_text('{"annual_return": 0.2}', encoding="utf-8")
        return 0

    with pytest.raises(GuardEvaluationError, match="modified immutable"):
        _evaluate_guard(
            task,
            tmp_path / "task.toml",
            tmp_path,
            tmp_path,
            experiment,
            "001/001",
            {"annual_return": 0.2},
            "a" * 64,
            mutating_runner,
            None,
            "001",
        )
