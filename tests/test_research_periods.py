from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from quant_core.research.contracts import ResearchTask
from quant_core.research.periods import resolve_relative_periods
from quant_core.research.workspace import (
    ResearchWorkspace,
    build_evaluation_view,
    validate_evaluation_view,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def relative_task(tmp_path: Path) -> ResearchTask:
    payload = copy.deepcopy(
        ResearchTask.load(REPOSITORY_ROOT / "tasks/active_etf_sharpe.toml").raw
    )
    payload["data"]["universe"] = "universe.csv"
    pd.DataFrame({"symbol": ["A", "B"], "name": ["A", "B"]}).to_csv(
        tmp_path / "universe.csv", index=False
    )
    return ResearchTask.from_mapping(payload)


def test_relative_contract_requires_resolution_before_absolute_access(
    tmp_path: Path,
) -> None:
    task = relative_task(tmp_path)

    with pytest.raises(RuntimeError, match="must be resolved"):
        _ = task.evaluation_periods


def test_relative_contract_rejects_non_positive_months(tmp_path: Path) -> None:
    task = relative_task(tmp_path)
    payload = copy.deepcopy(task.raw)
    payload["evaluation"]["walk_forward"]["relative"]["gate_months"] = 0

    with pytest.raises(ValueError, match="gate_months.*positive integer"):
        ResearchTask.from_mapping(payload)


def write_daily(
    tmp_path: Path,
    start: str = "2021-01-01",
    end: str = "2026-07-30",
) -> None:
    dates = pd.bdate_range(start, end)
    frame = pd.DataFrame(
        [
            {"date": day, "symbol": symbol, "close": 1.0}
            for day in dates
            for symbol in ("A", "B")
        ]
    )
    (tmp_path / "data").mkdir(exist_ok=True)
    frame.to_parquet(tmp_path / "data/etf_daily.parquet", index=False)


def test_resolves_contiguous_relative_periods_and_freezes_anchor(
    tmp_path: Path,
) -> None:
    task = relative_task(tmp_path)
    write_daily(tmp_path)

    resolved = resolve_relative_periods(
        task, source=tmp_path, runtime=tmp_path
    )

    assert resolved.period_resolution is not None
    assert resolved.period_resolution["anchor"] == "2026-07-30"
    assert resolved.guard_period == {"start": "2026-02-02", "end": "2026-07-30"}
    assert pd.Timestamp(resolved.development_period["end"]) < pd.Timestamp(
        resolved.gate_period["start"]
    )
    assert pd.Timestamp(resolved.gate_period["end"]) < pd.Timestamp(
        resolved.guard_period["start"]
    )


def test_rejects_incomplete_latest_universe_date(tmp_path: Path) -> None:
    task = relative_task(tmp_path)
    write_daily(tmp_path)
    path = tmp_path / "data/etf_daily.parquet"
    frame = pd.read_parquet(path)
    frame = frame[
        ~((frame["date"] == pd.Timestamp("2026-07-30")) & (frame["symbol"] == "B"))
    ]
    frame.to_parquet(path, index=False)

    with pytest.raises(ValueError, match="latest.*incomplete.*B"):
        resolve_relative_periods(task, source=tmp_path, runtime=tmp_path)


def test_rejects_insufficient_training_history(tmp_path: Path) -> None:
    task = relative_task(tmp_path)
    write_daily(tmp_path, start="2023-07-31")

    with pytest.raises(ValueError, match="insufficient market-data history"):
        resolve_relative_periods(task, source=tmp_path, runtime=tmp_path)


def test_evaluation_views_are_content_addressed_and_run_frozen(
    tmp_path: Path,
) -> None:
    write_daily(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    manager = ResearchWorkspace(
        tmp_path, tmp_path / ".research", "relative-task", run_number=1
    )
    first, first_manifest = build_evaluation_view(
        tmp_path, manager.evaluation_views
    )
    same, same_manifest = build_evaluation_view(
        tmp_path, manager.evaluation_views
    )
    assert same == first
    assert same_manifest == first_manifest
    periods = {
        "schema_version": 1,
        "periods": {
            "development": {"start": "2022-01-01", "end": "2024-06-30"},
            "gate": {"start": "2024-07-01", "end": "2025-06-30"},
            "guard": {"start": "2025-07-01", "end": "2025-12-31"},
        },
        "resolution": {"anchor": "2025-12-31"},
    }
    manager.freeze_run_evaluation_inputs(first_manifest, periods)
    validate_evaluation_view(manager.evaluation_runtime, first_manifest)

    path = tmp_path / "data/etf_daily.parquet"
    frame = pd.read_parquet(path)
    frame.loc[0, "close"] = 2.0
    frame.to_parquet(path, index=False)
    second, second_manifest = build_evaluation_view(
        tmp_path, manager.evaluation_views
    )
    assert second != first
    assert second_manifest["evaluation_inputs_sha256"] != first_manifest[
        "evaluation_inputs_sha256"
    ]
    assert manager.evaluation_runtime.name == first.name
