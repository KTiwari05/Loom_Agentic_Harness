"""Eval-harness tests — score a task suite offline with a scripted completer (no API key)."""

from __future__ import annotations

import json
from pathlib import Path

from loom.config import Config
from loom.evalrun import discover_tasks, run_suite
from loom.models import ModelResponse, ToolCall
from loom.replay import SequenceCompleter


def _write_call(path: str, content: str) -> ModelResponse:
    args = json.dumps({"path": path, "content": content})
    return ModelResponse(
        text="",
        tool_calls=[ToolCall(id="c1", name="write_file", arguments=args)],
        assistant_message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": args}}
            ],
        },
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        cost=0.0001,
        model="fake",
    )


def _final(text: str) -> ModelResponse:
    return ModelResponse(
        text=text,
        tool_calls=[],
        assistant_message={"role": "assistant", "content": text},
        usage={"prompt_tokens": 12, "completion_tokens": 4},
        cost=0.0001,
        model="fake",
    )


def _make_task(root: Path, name: str) -> Path:
    task = root / name
    (task / "repo").mkdir(parents=True)
    (task / "task.json").write_text(
        json.dumps({"goal": "create hello.txt containing exactly: hi", "check": "python check.py"}),
        encoding="utf-8",
    )
    (task / "check.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        'sys.exit(0 if Path("hello.txt").read_text(encoding="utf-8") == "hi" else 1)\n',
        encoding="utf-8",
    )
    return task


def test_suite_scores_pass_and_fail_and_writes_report(tmp_path):
    tasks = tmp_path / "tasks"
    _make_task(tasks, "001-pass")
    _make_task(tasks, "002-fail")
    assert [t.name for t in discover_tasks(tasks)] == ["001-pass", "002-fail"]

    completer = SequenceCompleter(
        [
            _write_call("hello.txt", "hi"), _final("Done."),        # task 1: correct
            _write_call("hello.txt", "WRONG"), _final("Done."),     # task 2: wrong content
        ]
    )
    config = Config(critic_enabled=False, auto_approve=True)
    report = tmp_path / "report.md"
    results = run_suite(tasks, config, completer=completer, report_path=report)

    assert [r.passed for r in results] == [True, False]
    assert results[0].steps == 2 and results[0].cost > 0

    text = report.read_text(encoding="utf-8")
    assert "1/2" in text and "001-pass" in text and "002-fail" in text


def test_shipped_task_fixtures_are_wellformed():
    tasks_dir = Path(__file__).parent.parent / "evals" / "tasks"
    tasks = discover_tasks(tasks_dir)
    assert len(tasks) >= 3
    for task in tasks:
        spec = json.loads((task / "task.json").read_text(encoding="utf-8"))
        assert spec["goal"].strip() and spec["check"].strip()
        assert (task / "repo").is_dir()
