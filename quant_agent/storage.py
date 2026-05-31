from __future__ import annotations

from pathlib import Path

import pandas as pd


def parquet_available() -> bool:
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        try:
            import fastparquet  # noqa: F401
        except ImportError:
            return False
    return True


def table_path(base_path: Path) -> Path:
    if base_path.suffix:
        return base_path
    if parquet_available():
        return base_path.with_suffix(".parquet")
    return base_path.with_suffix(".csv")


def write_table(df: pd.DataFrame, base_path: Path) -> Path:
    path = table_path(base_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".parquet":
        df.to_parquet(path, index=False)
    elif path.suffix == ".csv":
        df.to_csv(path, index=False)
    else:
        raise ValueError(f"Unsupported table format: {path}")
    return path


def read_table(base_path: Path, parse_dates: list[str] | None = None) -> pd.DataFrame:
    candidates = [base_path] if base_path.suffix else [
        base_path.with_suffix(".parquet"),
        base_path.with_suffix(".csv"),
    ]
    for path in candidates:
        if path.exists():
            if path.suffix == ".parquet":
                return pd.read_parquet(path)
            if path.suffix == ".csv":
                return pd.read_csv(path, parse_dates=parse_dates)
    raise FileNotFoundError(f"No table found for {base_path}")
