from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectPaths:
    root: Path = Path(".")

    @property
    def data_raw(self) -> Path:
        return self.root / "data" / "raw"

    @property
    def data_processed(self) -> Path:
        return self.root / "data" / "processed"

    @property
    def data_universe(self) -> Path:
        return self.root / "data" / "universe"

    @property
    def outputs(self) -> Path:
        return self.root / "outputs"

    def ensure(self) -> None:
        for path in [
            self.data_raw,
            self.data_processed,
            self.data_universe,
            self.outputs / "factors",
            self.outputs / "backtests",
            self.outputs / "recommendations",
            self.outputs / "reports",
        ]:
            path.mkdir(parents=True, exist_ok=True)
