from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from quant_core.research.workspace import (
    DevelopmentInputsError,
    build_development_view,
    validate_development_view,
)
from quant_core.research.evaluator import evaluate_walk_forward


def _runtime(root: Path) -> Path:
    runtime = root / "evaluation"
    (runtime / "data").mkdir(parents=True)
    (runtime / "outputs/factors").mkdir(parents=True)
    return runtime


def test_development_view_truncates_csv_and_parquet_but_keeps_prior_history(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    frame = pd.DataFrame({
        "date": ["2018-01-01", "2021-12-31", "2022-01-01"],
        "value": [1, 2, 3],
    })
    frame.to_csv(runtime / "data/prices.csv", index=False)
    frame.to_parquet(runtime / "outputs/factors/signal.parquet", index=False)

    view, manifest = build_development_view(
        runtime,
        tmp_path / "development-views",
        date(2021, 12, 31),
    )

    assert pd.read_csv(view / "data/prices.csv")["date"].tolist() == [
        "2018-01-01",
        "2021-12-31",
    ]
    assert pd.read_parquet(view / "outputs/factors/signal.parquet")[
        "date"
    ].tolist() == ["2018-01-01", "2021-12-31"]
    assert manifest["files"][0]["date_min"] == "2018-01-01"
    assert manifest["files"][0]["date_max"] == "2021-12-31"
    validate_development_view(view, manifest)


def test_development_view_records_empty_tables_and_reuses_same_content(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    pd.DataFrame({"date": pd.Series(dtype="object"), "value": []}).to_csv(
        runtime / "data/empty.csv",
        index=False,
    )
    views = tmp_path / "development-views"

    first, first_manifest = build_development_view(
        runtime, views, date(2021, 12, 31)
    )
    second, second_manifest = build_development_view(
        runtime, views, date(2021, 12, 31)
    )

    assert first == second
    assert first_manifest == second_manifest
    assert first_manifest["files"][0]["rows"] == 0
    assert first_manifest["files"][0]["date_min"] is None
    assert first_manifest["files"][0]["date_max"] is None


def test_future_only_csv_uses_the_schema_read_from_the_persisted_empty_view(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    pd.DataFrame({
        "date": ["2022-01-01"],
        "value": [1],
    }).to_csv(runtime / "data/future.csv", index=False)

    view, manifest = build_development_view(
        runtime,
        tmp_path / "development-views",
        date(2021, 12, 31),
    )

    assert pd.read_csv(view / "data/future.csv").empty
    assert manifest["files"][0]["rows"] == 0
    assert manifest["files"][0]["schema"] == [
        {"name": "date", "dtype": "object"},
        {"name": "value", "dtype": "object"},
    ]
    validate_development_view(view, manifest)


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("unknown.json", "{}", "Unsupported"),
        ("missing.csv", "value\n1\n", "missing date"),
        ("invalid.csv", "date,value\nnot-a-date,1\n", "invalid dates"),
        ("broken.parquet", "not parquet", "could not be read"),
    ],
)
def test_development_view_rejects_uncontracted_or_invalid_files(
    tmp_path: Path,
    name: str,
    content: str,
    message: str,
) -> None:
    runtime = _runtime(tmp_path)
    (runtime / "data" / name).write_text(content, encoding="utf-8")

    with pytest.raises(DevelopmentInputsError, match=message):
        build_development_view(runtime, tmp_path / "views", date(2021, 12, 31))

    assert not list((tmp_path / "views").glob("[0-9a-f]" * 64))


def test_development_view_rejects_file_and_directory_symlinks(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    outside = tmp_path / "outside.csv"
    outside.write_text("date,value\n2021-01-01,1\n", encoding="utf-8")
    (runtime / "data/link.csv").symlink_to(outside)

    with pytest.raises(DevelopmentInputsError, match="symbolic links"):
        build_development_view(runtime, tmp_path / "views", date(2021, 12, 31))

    (runtime / "data/link.csv").unlink()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (runtime / "data/link").symlink_to(outside_dir, target_is_directory=True)
    with pytest.raises(DevelopmentInputsError, match="symbolic links"):
        build_development_view(runtime, tmp_path / "views", date(2021, 12, 31))


def test_tampered_content_addressed_view_is_atomically_rebuilt(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    source = runtime / "data/prices.csv"
    source.write_text("date,value\n2021-01-01,1\n", encoding="utf-8")
    views = tmp_path / "views"
    view, manifest = build_development_view(runtime, views, date(2021, 12, 31))
    (view / "data/prices.csv").write_text(
        "date,value\n2021-01-01,999\n",
        encoding="utf-8",
    )

    rebuilt, rebuilt_manifest = build_development_view(
        runtime, views, date(2021, 12, 31)
    )

    assert rebuilt == view
    assert rebuilt_manifest == manifest
    assert pd.read_csv(rebuilt / "data/prices.csv")["value"].tolist() == [1]
    assert not list(views.glob(".building-*"))
    assert not list(views.glob(".corrupt-*"))


def test_manifest_tampering_is_detected(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    (runtime / "data/prices.csv").write_text(
        "date,value\n2021-01-01,1\n",
        encoding="utf-8",
    )
    view, manifest = build_development_view(
        runtime, tmp_path / "views", date(2021, 12, 31)
    )
    changed = dict(manifest)
    changed["development_end"] = "2020-01-01"
    (view / "manifest.json").write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(DevelopmentInputsError, match="manifest"):
        validate_development_view(view, manifest)


def test_development_view_is_metric_equivalent_to_host_period_filter(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    dates = pd.date_range("2024-01-01", "2024-06-30", freq="D")
    daily = pd.DataFrame([
        {
            "date": day,
            "symbol": symbol,
            "open": 10.0 + index + (0.0 if symbol == "A" else 1.0),
        }
        for index, day in enumerate(dates)
        for symbol in ("A", "B")
    ])
    daily.to_parquet(runtime / "data/prices.parquet", index=False)
    end = date(2024, 5, 31)
    view, _ = build_development_view(runtime, tmp_path / "views", end)
    universe = pd.DataFrame({"symbol": ["A", "B"]})
    walk_forward = {
        "train_months": 3,
        "max_parameter_sets": 2,
        "schedule": {
            "period": "calendar_month",
            "interval": 1,
            "trigger": "start",
        },
    }
    constraints = {
        "max_drawdown": {"operator": "abs<=", "threshold": 1.0}
    }

    def selector(daily, universe, start, end, params):
        return pd.DataFrame({
            "date": pd.date_range(start, end, freq="D"),
            "symbol": params["symbol"],
            "target_weight": 1.0,
        })

    arguments = (
        universe,
        {"start": "2024-04-01", "end": end.isoformat()},
        walk_forward,
        constraints,
        "sortino",
        [{"symbol": "A"}, {"symbol": "B"}],
        selector,
    )
    host = evaluate_walk_forward(daily, *arguments)
    frozen = evaluate_walk_forward(
        pd.read_parquet(view / "data/prices.parquet"),
        *arguments,
    )

    pd.testing.assert_frame_equal(host[0], frozen[0])
    assert host[1].metrics == frozen[1].metrics
    assert host[2] == frozen[2]
