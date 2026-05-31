from __future__ import annotations

from pathlib import Path

import pandas as pd


def build_markdown_report(
    run_id: str,
    metrics: dict[str, float],
    recommendation: pd.DataFrame,
    output_path: Path,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Backtest Report: {run_id}",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for key, value in metrics.items():
        lines.append(f"| {key} | {value:.6f} |")
    lines.extend(["", "## Latest Recommendation", "", "| Symbol | Name | Score | Target Weight |", "| --- | --- | ---: | ---: |"])
    if not recommendation.empty:
        for row in recommendation.itertuples(index=False):
            lines.append(
                f"| {row.symbol} | {row.name} | {float(row.score):.6f} | {float(row.target_weight):.4f} |"
            )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path
