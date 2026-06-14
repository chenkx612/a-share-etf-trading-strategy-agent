from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectPaths:
    root: Path = Path(".")

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def data_daily(self) -> Path:
        return self.data / "etf_daily"

    @property
    def outputs(self) -> Path:
        return self.root / "outputs"

    def ensure_data(self) -> None:
        self.data.mkdir(parents=True, exist_ok=True)

    def ensure(self) -> None:
        self.ensure_data()
        for path in [
            self.outputs / "factors",
            self.outputs / "backtests",
            self.outputs / "recommendations",
            self.outputs / "reports",
        ]:
            path.mkdir(parents=True, exist_ok=True)
