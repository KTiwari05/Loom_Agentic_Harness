"""demo_v2.py — every v2 subsystem in one offline run, derived from one JSONL.

Builds a session log in a temp dir demonstrating: prompt queue, background job
notification, task ledger, sidechain subagent, compaction boundary — then prints
the activity stats DERIVED from the log (no second source of truth, no model calls,
no API key). This is the master plan §6 definition-of-done demo.

Run:  .venv\\Scripts\\python.exe scripts\\demo_v2.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from loom.compaction import Compactor  # noqa: E402
from loom.context import ContextAssembler  # noqa: E402
from loom.eventlog import EventLog  # noqa: E402
from loom.jobs import JobManager  # noqa: E402
from loom.ledger import TaskLedger  # noqa: E402
from loom.models import ModelResponse  # noqa: E402
from loom.queue import PromptQueue  # noqa: E402
from loom.server.eventstore import activity_from_records  # noqa: E402


def fake_summarizer(model, messages, tools, temperature):
    """Stands in for the compaction model call — the demo stays offline."""
    return ModelResponse(
        text="1. Primary Request and Intent\nDemonstrate every Loom v2 subsystem offline.",
        tool_calls=[],
        assistant_message={"role": "assistant", "content": "summary"},
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        cost=0.0, model="demo/fake",
    )


def _assistant(text, *tool_uses):
    return {"role": "assistant", "model": "demo/fake",
            "content": [{"type": "text", "text": text}, *tool_uses],
            "usage": {"input_tokens": 20, "output_tokens": 8}}


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="loom-demo-"))
    workspace = root / "workspace"
    workspace.mkdir()
    log = EventLog(root, "demo-project")
    print(f"Loom v2 demo — session log: {log.path}\n")

    # 1) Prompt queue: messages typed while the agent is busy wait their turn.
    queue = PromptQueue(log)
    queue.enqueue("write the report")
    queue.enqueue("also add tests")
    first = queue.pop()
    print(f"[queue]      2 prompts enqueued while busy; popped {first.content!r} "
          f"({len(queue)} still waiting, every op is a queue-operation record)")

    # 2) One agent turn: user + assistant + tool results (feeds the activity stats).
    log.user(first.content)
    a = log.assistant(_assistant(
        "Reading the code, then writing the report.",
        {"type": "tool_use", "id": "tc_demo0000000000000000001", "name": "read_file",
         "input": {"path": "main.py"}},
        {"type": "tool_use", "id": "tc_demo0000000000000000002", "name": "write_file",
         "input": {"path": "report.md"}},
    ))
    log.tool_result("tc_demo0000000000000000001", "def main(): ...",
                    {"tool": "read_file", "ok": True, "target": "main.py",
                     "category": "file_read", "detail": {"lines": 12}}, a)
    log.tool_result("tc_demo0000000000000000002", "Created 'report.md'",
                    {"tool": "write_file", "ok": True, "target": "report.md",
                     "category": "file_write", "detail": {}}, a)

    # 3) Background job: detached process → .output file → notification on the queue.
    queue.pop()  # drain the second human prompt first — the queue is strict FIFO
    jobs = JobManager(queue, root, log.session_id)
    status = jobs.start(f'"{sys.executable}" -c "print(42)"', "compute the answer",
                        str(workspace))
    deadline = time.time() + 10
    while not len(queue) and time.time() < deadline:
        time.sleep(0.05)
    note = queue.pop()
    summary_line = next((ln.strip() for ln in note.content.splitlines()
                         if "<summary>" in ln), "")
    print(f"[jobs]       job {status.id} → {summary_line or 'notification enqueued'} "
          f"(origin={note.origin})")

    # 4) Task ledger: numbered JSON files, mutated only by explicit calls.
    ledger = TaskLedger(root, log.session_id)
    t1 = ledger.create("Ship the v2 demo", "Prove all subsystems in one log",
                       "Shipping the v2 demo")
    ledger.update(t1, status="completed")
    print(f"[ledger]     task #{t1} '{ledger.get(t1)['subject']}' → "
          f"{ledger.get(t1)['status']}  ({ledger.dir})")

    # 5) Sidechain: a subagent turn recorded in the SAME file, flagged.
    with log.sidechain("sc_demo0001"):
        log.user("explore: how big is main.py?", origin="human",
                 sidechainAgentType="explore")
        log.assistant(_assistant("It is 12 lines."))
    print("[sidechain]  subagent turn recorded in the same JSONL (isSidechain=true)")

    # 6) Compaction: boundary record, summary, preserved tail — raw history stays on disk.
    assembler = ContextAssembler(log, str(workspace))
    Compactor(log, assembler, fake_summarizer, "demo/fake").compact("manual")
    boundary = next(r for r in log.read_all()
                    if r.get("type") == "system" and r.get("subtype") == "compact_boundary")
    meta = boundary["compactMetadata"]
    print(f"[compaction] boundary written: ~{meta['preTokens']} → ~{meta['postTokens']} "
          f"est. tokens, {len(meta['preservedUuids'])} tail records preserved verbatim")

    # 7) Everything above, derived back OUT of the log — the whole point of v2.
    records = log.read_all()
    print("\nDerived from the log (no second source of truth):")
    for turn in activity_from_records([r for r in records if not r.get("isSidechain")]):
        print(f"  activity · {turn['summary'] or '(no tool activity)'}")
    census = Counter(r.get("type", "?") for r in records)
    print(f"  census   · {dict(sorted(census.items()))}")
    report = log.verify()
    print(f"  verify   · {'OK — tree intact, tool linkage complete' if report.ok else report}")


if __name__ == "__main__":
    main()
