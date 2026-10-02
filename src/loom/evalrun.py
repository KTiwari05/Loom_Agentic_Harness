"""The eval harness — run Loom against a task suite and produce a scored report.

A task is a directory:
    tasks/001-add-slugify/
      task.json     {"goal": "...", "check": "python check.py"}
      repo/         the starting workspace (copied fresh per run)
      check.py      exit 0 = the agent succeeded (referenced by task.json's check command)

Metrics per task: pass/fail, steps, cost, wall-clock. The report is what turns "it worked
when I tried it" into a number — see docs/06-evals-and-observability.md.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from .config import Config
from .orchestrator import Orchestrator

_CHECK_TIMEOUT = 120


@dataclass
class TaskResult:
    name: str
    passed: bool
    steps: int
    cost: float
    baseline_cost: float
    seconds: float
    error: str = ""


def discover_tasks(tasks_dir: str | Path) -> list[Path]:
    root = Path(tasks_dir)
    return sorted(p.parent for p in root.glob("*/task.json"))


def run_task(task_dir: Path, config_template: Config, completer=None, verbose: bool = False) -> TaskResult:
    spec = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    goal, check = spec["goal"], spec["check"]

    workspace = Path(tempfile.mkdtemp(prefix=f"loom-eval-{task_dir.name}-"))
    repo = task_dir / "repo"
    if repo.is_dir():
        shutil.copytree(repo, workspace, dirs_exist_ok=True)
    for extra in task_dir.iterdir():  # check scripts etc. live next to task.json
        if extra.name not in ("task.json", "repo") and extra.is_file():
            shutil.copy2(extra, workspace / extra.name)

    config = Config(
        model=config_template.model,
        model_strong=config_template.model_strong,
        model_critic=config_template.model_critic,
        temperature=config_template.temperature,
        max_steps=config_template.max_steps,
        max_cost_usd=config_template.max_cost_usd,
        critic_enabled=config_template.critic_enabled,
        auto_approve=True,
        workdir=str(workspace),
    )
    console = Console() if verbose else Console(file=io.StringIO())
    agent = Orchestrator(config, console, completer=completer)

    prev_cwd = os.getcwd()
    started = time.monotonic()
    error = ""
    try:
        os.chdir(workspace)  # tools resolve paths against cwd
        agent.run(goal)
        passed = _run_check(check, workspace)
    except Exception as exc:  # noqa: BLE001
        passed, error = False, str(exc)
    finally:
        os.chdir(prev_cwd)

    return TaskResult(
        name=task_dir.name,
        passed=passed,
        steps=agent.last_turn_steps,
        cost=agent.total_cost,
        baseline_cost=agent.baseline_cost,
        seconds=time.monotonic() - started,
        error=error,
    )


def _run_check(check: str, workspace: Path) -> bool:
    # Cross-platform: route a leading `python` to the interpreter running the harness.
    if check.split(maxsplit=1)[0] == "python":
        check = f'"{sys.executable}" ' + check.split(maxsplit=1)[1]
    proc = subprocess.run(
        check, shell=True, cwd=workspace, capture_output=True, text=True, timeout=_CHECK_TIMEOUT
    )
    return proc.returncode == 0


def run_suite(
    tasks_dir: str | Path,
    config: Config,
    completer=None,
    report_path: str | Path | None = None,
    verbose: bool = False,
) -> list[TaskResult]:
    results = [run_task(t, config, completer=completer, verbose=verbose) for t in discover_tasks(tasks_dir)]
    if report_path is not None:
        Path(report_path).write_text(render_report(results, config.model), encoding="utf-8")
    return results


def render_report(results: list[TaskResult], model: str) -> str:
    n = len(results)
    passed = sum(r.passed for r in results)
    total_cost = sum(r.cost for r in results)
    total_baseline = sum(r.baseline_cost for r in results)
    saved_pct = (1 - total_cost / total_baseline) * 100 if total_baseline > 0 else 0.0

    lines = [
        "# Loom eval report",
        "",
        f"- date: {time.strftime('%Y-%m-%d %H:%M')}",
        f"- model: `{model}`",
        f"- **success rate: {passed}/{n}" + (f" ({passed / n * 100:.0f}%)**" if n else "**"),
        f"- total cost: ${total_cost:.4f} · frontier baseline: ${total_baseline:.4f} "
        f"· saved: {saved_pct:.0f}%",
        "",
        "| task | result | steps | cost | time |",
        "|---|---|---:|---:|---:|",
    ]
    for r in results:
        status = "✅ pass" if r.passed else ("💥 error" if r.error else "❌ fail")
        lines.append(f"| {r.name} | {status} | {r.steps} | ${r.cost:.4f} | {r.seconds:.1f}s |")
    if any(r.error for r in results):
        lines += ["", "## Errors", ""]
        lines += [f"- **{r.name}**: {r.error}" for r in results if r.error]
    return "\n".join(lines) + "\n"
