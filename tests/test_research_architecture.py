from __future__ import annotations

import ast
from pathlib import Path


RESEARCH_ROOT = Path("src/quant_core/research")


def _imports(path: Path) -> list[ast.ImportFrom]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]


def test_loop_and_report_use_only_public_runner_entrypoints() -> None:
    for name in ("loop.py", "report.py"):
        private = [
            alias.name
            for node in _imports(RESEARCH_ROOT / name)
            if node.module == "quant_core.research.runner"
            for alias in node.names
            if alias.name.startswith("_")
        ]
        assert private == []


def test_contract_and_state_modules_do_not_depend_on_orchestration() -> None:
    forbidden = {
        "quant_core.research.loop",
        "quant_core.research.runner",
        "quant_core.research.workspace",
    }
    for name in (
        "decision.py",
        "loop_state.py",
        "result_validation.py",
        "task_validation.py",
    ):
        dependencies = {node.module for node in _imports(RESEARCH_ROOT / name)}
        assert dependencies.isdisjoint(forbidden)
