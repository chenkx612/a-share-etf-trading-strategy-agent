from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from quant_core.research.contracts import ResearchTask
from quant_core.research.runner import _run_opencode_read_only
from quant_core.research.workspace import ResearchWorkspace, write_json_atomic


ReportAgentRunner = Callable[[Sequence[str], str, Path, Path, int], int]
_ROUND_ID = re.compile(r"^\d+$")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_loop_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_json_atomic(path, state)


def _loop_round_ids(
    experiments: Path,
    loop_state: Mapping[str, Any],
) -> list[str]:
    configured = loop_state.get("round_ids", loop_state.get("experiment_ids"))
    if isinstance(configured, list):
        return [
            round_id
            for round_id in configured
            if isinstance(round_id, str) and _ROUND_ID.fullmatch(round_id)
        ]

    # Legacy states without round_ids can only be scoped by the completed count.
    try:
        rounds_completed = max(0, int(loop_state.get("rounds_completed", 0)))
    except (TypeError, ValueError):
        rounds_completed = 0
    if not experiments.exists() or rounds_completed == 0:
        return []
    candidates = sorted(
        path.name
        for path in experiments.iterdir()
        if path.is_dir() and _ROUND_ID.fullmatch(path.name)
    )
    return candidates[-rounds_completed:]


def _experiment_records(
    experiments: Path,
    round_ids: Sequence[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for round_id in round_ids:
        experiment = experiments / round_id
        if not experiment.is_dir():
            continue
        result = _read_json(experiment / "result.json") or {}
        decision = _read_json(experiment / "decision.json") or {}
        records.append({
            "experiment_id": experiment.name,
            "status": result.get("status"),
            "decision": decision.get("decision"),
            "decision_reasons": decision.get("reasons", []),
            "decision_objective": decision.get("objective"),
            "decision_constraints": decision.get("constraints"),
            "hypothesis": result.get("hypothesis"),
            "attempts": result.get("attempts"),
            "development_effect": result.get("development_effect"),
            "submitted_candidate": result.get("candidate"),
            "metrics": result.get("metrics"),
            "error": result.get("error"),
        })
    return records


def _strategy_source(
    manager: ResearchWorkspace,
    champion_sha256: object,
) -> str | None:
    if not isinstance(champion_sha256, str):
        return None
    try:
        return manager.champion_path.read_text(encoding="utf-8")
    except OSError:
        return None


def _last_text_event(events_path: Path) -> str | None:
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    text: str | None = None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "text":
            continue
        candidate = event.get("part", {}).get("text")
        if isinstance(candidate, str) and candidate.strip():
            text = candidate.strip()
    return text


def _report_prompt(payload: Mapping[str, Any]) -> str:
    return "\n".join([
        "你是量化研发复盘员。根据下方完整、可信的 Harness 记录，生成一份简洁清晰的中文 Markdown 报告。",
        "只总结给定记录，不搜索外部信息，不运行工具，不修改文件，不虚构指标或因果关系。",
        "开发集和 gate 的结果必须明确区分；失败或中断轮次也必须如实说明。",
        "接受或拒绝原因必须以 decision_objective、decision_constraints 和 decision_reasons "
        "为准；特别要准确说明当时 champion 是否可行、是否要求相对目标改善。",
        "报告固定包含以下结构：",
        "# Research Loop 总结",
        "## 总览",
        "用短段落说明停止原因、轮次数、接受/拒绝/失败数量和总体结论。",
        "## 逐轮复盘",
        "每轮使用三级标题，简述：假设、主要尝试、开发集效果、gate 决策、可复用启发。",
        "每轮控制在 4 个紧凑要点内；没有数据时明确说记录不足。",
        "## 最终 Champion",
        "清楚说明最终交易逻辑、确定参数、相对原始基准的关键变化、开发集与 gate 指标、被接受原因。",
        "如果本轮没有接受新 Champion，明确说明最终 Champion 在本轮未发生变化；"
        "不要擅自称其为原始基准。",
        "## 关键启发",
        "提炼 3 至 6 条跨轮次结论，区分观测事实与推测。",
        "## 遗留风险与下一步",
        "只列最重要的 2 至 4 项。",
        "指标优先展示 objective 和硬约束相关项，避免堆砌所有数字。",
        "最终回复必须只有 Markdown 正文，不要代码围栏、前言、致谢或 JSON。",
        "",
        "Harness record:",
        json.dumps(payload, ensure_ascii=False, indent=2),
    ])


def generate_loop_report(
    task_path: str | Path,
    manager: ResearchWorkspace,
    loop_state: Mapping[str, Any],
    *,
    agent_runner: ReportAgentRunner = _run_opencode_read_only,
) -> Path:
    task = ResearchTask.load(task_path)
    task_state = manager.load_state(task.strategy_path)
    round_ids = _loop_round_ids(manager.rounds, loop_state)
    experiments = _experiment_records(manager.rounds, round_ids)
    champion_round_id = task_state.get("champion_round_id")
    local_champion_round = (
        str(champion_round_id).partition("/")[2]
        if isinstance(champion_round_id, str)
        and str(champion_round_id).startswith(f"{manager.run_id}/")
        else None
    )
    champion_experiment = next(
        (
            record
            for record in experiments
            if record.get("experiment_id") == local_champion_round
        ),
        None,
    )
    payload = {
        "task": {
            "id": task.task_id,
            "goal": task.raw["goal"],
            "strategy": task.raw.get("strategy"),
            "objective": task.raw["evaluation"]["objective"],
            "constraints": task.raw["evaluation"]["constraints"],
            "minimum_improvement": (
                task.raw["evaluation"].get("acceptance", {}).get("minimum_improvement", 0.0)
            ),
            "development_period": task.raw["evaluation"]["fixed"]["development"],
            "gate_period": task.raw["evaluation"]["fixed"]["gate"],
        },
        "loop": {
            key: loop_state.get(key)
            for key in (
                "status", "stop_reason", "rounds_completed", "accepted", "rejected", "failed",
                "elapsed_seconds",
            )
        },
        "experiments": experiments,
        "champion": {
            "experiment_id": champion_round_id,
            "submitted_candidate": (
                champion_experiment.get("submitted_candidate")
                if champion_experiment is not None
                else None
            ),
            "champion_number": task_state.get("champion_number"),
            "sha256": task_state.get("champion_sha256"),
            "source_round": champion_round_id,
            "strategy_path": task_state.get("strategy_path"),
            "metrics": task_state.get("champion_metrics"),
            "strategy_source": _strategy_source(
                manager,
                task_state.get("champion_sha256"),
            ),
        },
    }
    opencode = task.raw["opencode"]
    command = [
        "opencode", "run", "--auto", "--format", "json",
        "--model", str(opencode["model"]), "--dir", str(manager.run_root),
    ]
    if variant := opencode.get("variant"):
        command.extend(["--variant", str(variant)])
    timeout = min(int(opencode["timeout_minutes"]) * 60, 600)
    events_path = manager.run_temp / "report-events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    exit_code = agent_runner(
        command,
        _report_prompt(payload),
        manager.run_root,
        events_path,
        timeout,
    )
    if exit_code != 0:
        failed_events = manager.run_root / "report-events.jsonl"
        if events_path.exists():
            events_path.replace(failed_events)
        reason = "timed out" if exit_code == 124 else f"exited with code {exit_code}"
        raise RuntimeError(f"OpenCode report session {reason}")
    report = _last_text_event(events_path)
    if report is None or not report.lstrip().startswith("# Research Loop 总结"):
        failed_events = manager.run_root / "report-events.jsonl"
        if events_path.exists():
            events_path.replace(failed_events)
        raise RuntimeError("OpenCode report session produced no valid Markdown report")
    report_path = manager.report_path
    temporary = report_path.with_suffix(".md.tmp")
    temporary.write_text(report.rstrip() + "\n", encoding="utf-8")
    temporary.replace(report_path)
    events_path.unlink(missing_ok=True)
    return report_path


def regenerate_loop_report(
    task_path: str | Path,
    *,
    workspace: str | Path = ".",
    research_root: str | Path = ".research",
    run_number: int | None = None,
    agent_runner: ReportAgentRunner = _run_opencode_read_only,
) -> Path:
    task_file = Path(task_path).resolve()
    task = ResearchTask.load(task_file)
    source = Path(workspace).resolve()
    managed_root = Path(research_root)
    if not managed_root.is_absolute():
        managed_root = source / managed_root
    base_manager = ResearchWorkspace(source, managed_root, task.task_id)
    base_manager.load_state(task.strategy_path)
    base_manager.migrate_legacy_loop()
    available = base_manager.run_numbers()
    selected = run_number if run_number is not None else (available[-1] if available else None)
    if selected is None or selected not in available:
        raise FileNotFoundError("Research run does not exist")
    manager = base_manager.for_run(selected)
    loop_state_path = manager.loop_state_path
    loop_state = _read_json(loop_state_path)
    if loop_state is None:
        raise FileNotFoundError(f"Loop state does not exist: {loop_state_path}")
    if loop_state.get("status") != "stopped":
        raise ValueError("Loop report can only be generated after the loop has stopped")
    loop_state["report_status"] = "running"
    loop_state["report_path"] = None
    loop_state["report_error"] = None
    _write_loop_state(loop_state_path, loop_state)
    try:
        report_path = generate_loop_report(
            task_file,
            manager,
            loop_state,
            agent_runner=agent_runner,
        )
    except KeyboardInterrupt:
        loop_state["report_status"] = "interrupted"
        loop_state["report_error"] = "Report generation was interrupted"
        _write_loop_state(loop_state_path, loop_state)
        raise
    except Exception as exc:
        loop_state["report_status"] = "failed"
        loop_state["report_error"] = str(exc)
        _write_loop_state(loop_state_path, loop_state)
        raise
    loop_state["report_status"] = "completed"
    loop_state["report_path"] = report_path.relative_to(manager.run_root).as_posix()
    _write_loop_state(loop_state_path, loop_state)
    return report_path
