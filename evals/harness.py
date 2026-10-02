"""Run the Loom eval suite:  python evals/harness.py [--tasks DIR] [--model X] [--no-critic] [-v]

Requires an API key in .env (runs the real agent per task). Writes evals/report.md.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from rich.console import Console

from loom.config import Config
from loom.evalrun import render_report, run_suite


def main() -> None:
    here = Path(__file__).parent
    parser = argparse.ArgumentParser(description="Run the Loom eval suite.")
    parser.add_argument("--tasks", default=str(here / "tasks"), help="tasks directory")
    parser.add_argument("--model", help="override LOOM_MODEL")
    parser.add_argument("--no-critic", action="store_true", help="disable the critic")
    parser.add_argument("--report", default=str(here / "report.md"), help="report output path")
    parser.add_argument("-v", "--verbose", action="store_true", help="show agent output per task")
    args = parser.parse_args()

    config = Config()
    if args.model:
        config.model = args.model
    if args.no_critic:
        config.critic_enabled = False

    console = Console()
    console.print(f"[bold]🧵 Loom evals[/] · {config.model} · tasks: {args.tasks}")
    results = run_suite(args.tasks, config, report_path=args.report, verbose=args.verbose)

    passed = sum(r.passed for r in results)
    for r in results:
        mark = "[green]pass[/]" if r.passed else "[red]FAIL[/]"
        console.print(f"  {r.name}: {mark} · {r.steps} steps · ${r.cost:.4f} · {r.seconds:.1f}s")
    console.print(f"\n[bold]{passed}/{len(results)} passed[/] → report: {args.report}")
    print(render_report(results, config.model))


if __name__ == "__main__":
    main()
