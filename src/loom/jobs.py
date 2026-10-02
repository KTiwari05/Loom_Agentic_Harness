"""JobManager — background shell commands (docs/v2/02-module-specs.md §3, schema §11).

A long command can run detached: stdout+stderr stream to ~/.loom/jobs/<session>/<id>.output,
the call returns an opaque 9-char id immediately, and a watcher thread turns the eventual
exit into a `<task-notification>` prompt enqueued on the PromptQueue. That notification
starts a new agent turn — the same pattern a real coding agent uses so the model can kick
off a build/test run and react to the result without blocking the conversation.
"""

from __future__ import annotations

import secrets
import string
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from .queue import PromptQueue

_ALPHABET = string.digits + string.ascii_lowercase  # base36


def new_job_id() -> str:
    """9 chars, base36, first char alphabetic (schema §11)."""
    return secrets.choice(string.ascii_lowercase) + "".join(
        secrets.choice(_ALPHABET) for _ in range(8)
    )


@dataclass
class JobStatus:
    id: str
    state: str                 # "running" | "completed" | "failed"
    description: str
    output_path: str
    exit_code: int | None = None


def notification(status: JobStatus) -> str:
    """The prompt text enqueued when a job ends (schema §11)."""
    verb = "completed" if status.state == "completed" else "failed"
    return (
        "<task-notification>\n"
        f"  <task-id>{status.id}</task-id>\n"
        f"  <status>{status.state}</status>\n"
        f"  <exit-code>{status.exit_code}</exit-code>\n"
        f"  <output-file>{status.output_path}</output-file>\n"
        f'  <summary>Background command "{status.description}" {verb} '
        f"(exit code {status.exit_code})</summary>\n"
        "</task-notification>"
    )


class JobManager:
    def __init__(self, queue: PromptQueue, root: Path, session_id: str):
        self.queue = queue
        self.dir = Path(root) / "jobs" / session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, command: str, description: str, cwd: str, timeout_s: int = 3600) -> JobStatus:
        job_id = new_job_id()
        out_path = self.dir / f"{job_id}.output"
        handle = open(out_path, "w", encoding="utf-8")  # noqa: SIM115 — closed by the watcher
        proc = subprocess.Popen(  # noqa: S602 — shell command is the whole point, gated upstream
            command, shell=True, cwd=cwd,
            stdout=handle, stderr=subprocess.STDOUT, text=True,
        )
        status = JobStatus(job_id, "running", description, str(out_path))
        with self._lock:
            self._jobs[job_id] = {"proc": proc, "status": status, "handle": handle}
        threading.Thread(target=self._watch, args=(job_id, timeout_s),
                         daemon=True, name=f"loom-job-{job_id}").start()
        return status

    def _watch(self, job_id: str, timeout_s: int) -> None:
        entry = self._jobs[job_id]
        proc, status, handle = entry["proc"], entry["status"], entry["handle"]
        try:
            code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait()
            status.state, status.exit_code = "failed", code
            handle.write("\n... (killed: exceeded timeout)\n")
        else:
            status.exit_code = code
            status.state = "completed" if code == 0 else "failed"
        handle.close()
        self.queue.enqueue(notification(status), origin="task-notification")

    def status(self, job_id: str) -> JobStatus | None:
        entry = self._jobs.get(job_id)
        return entry["status"] if entry else None

    def stop(self, job_id: str) -> bool:
        entry = self._jobs.get(job_id)
        if entry is None or entry["status"].state != "running":
            return False
        entry["proc"].kill()
        return True

    def list(self) -> list[JobStatus]:
        with self._lock:
            return [e["status"] for e in self._jobs.values()]
