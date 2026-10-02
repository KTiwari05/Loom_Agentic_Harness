"""Event sinks — how the agent reports what it is doing.

The Orchestrator emits structured events; a Sink renders them. This is what lets one
agent core drive many frontends:

- ConsoleSink  → the `loom` terminal experience (rich)
- QueueSink    → the desktop app: every event is forwarded as JSON over a WebSocket
                 (see server/bridge.py)

A sink also answers permission questions (`ask_permission`), because *where* the
allow/deny prompt appears is a frontend concern; *that* it must be asked is enforced
by the harness (permissions.py), never the model.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.markdown import Markdown


class Sink:
    """No-op base sink. Subclasses render events somewhere."""

    def emit(self, type: str, **data: Any) -> None:  # noqa: A002 - matches event JSON key
        pass

    def ask_permission(self, tool_name: str, preview: str) -> str:
        """Return 'y' (once), 'a' (always this session) or 'n' (deny)."""
        return "n"


class ConsoleSink(Sink):
    """Renders agent events in the terminal — the classic `loom` CLI look."""

    def __init__(self, console: Console | None = None):
        self.console = console or Console()

    def emit(self, type: str, **d: Any) -> None:  # noqa: A002
        c = self.console
        if type == "step":
            tag = " · escalated" if d["escalated"] else ""
            c.print(
                f"[dim]step {d['step']} · {d['model'].split('/')[-1]}{tag} · "
                f"{d['tokens']:,} tok · ${d['cost']:.4f} · {d['tool_calls']} tool call(s)[/]"
            )
        elif type == "assistant_text":
            c.print(Markdown(d["text"]))
        elif type == "tool":
            c.print(f"[dim]→ {d['name']}: {d['outcome']}[/]")
        elif type == "critic_start":
            c.print(f"[dim]critic · {d['model'].split('/')[-1]} · reviewing the diff…[/]")
        elif type == "critic_verdict":
            if d["approved"]:
                c.print("[green]critic · approved ✓[/]")
            else:
                c.print(f"[yellow]critic · revise:[/] {d['feedback']}")
        elif type == "critic_error":
            c.print(f"[yellow]critic unavailable ({d['error']}); accepting without review.[/]")
        elif type == "budget_stop":
            c.print(
                f"[bold red]Budget reached[/] (${d['total_cost']:.4f} > "
                f"${d['max_cost']:.2f}). Stopping."
            )
        elif type == "max_steps":
            c.print(f"[bold red]Hit max steps ({d['max_steps']}).[/] Stopping.")
        elif type == "done":
            c.print(f"\n[bold green]✓ done[/] · turn ${d['turn_cost']:.4f} · {d['summary']}")
        elif type == "model_error":
            c.print(f"\n[bold red]Model call failed:[/] {d['error']}")
            if d.get("key_hint"):
                c.print(
                    "[yellow]Looks like an API-key problem.[/] Copy [bold].env.example[/] to "
                    "[bold].env[/] and set a key (OpenRouter is easiest), matching LOOM_MODEL's prefix."
                )
        elif type == "subagent_start":
            c.print(f"[dim]⤷ subagent ({d['agent_type']}): {d['description'][:80]}[/]")
        elif type == "subagent_done":
            c.print(f"[dim]⤶ subagent done · {d['steps']} step(s) · ${d['cost']:.4f}[/]")
        # unknown event types are ignored — sinks only render what they understand

    def ask_permission(self, tool_name: str, preview: str) -> str:
        self.console.print()
        self.console.print(f"[bold yellow]⚠ {tool_name}[/] wants to run:")
        self.console.print(preview)
        try:
            answer = self.console.input(
                "[bold]Allow?[/] [green]y[/]=once  [cyan]a[/]=always  [red]n[/]=deny › "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "n"
        if answer in {"a", "always"}:
            return "a"
        return "y" if answer in {"y", "yes", ""} else "n"


class QueueSink(Sink):
    """Forwards every event as a dict onto a queue — the desktop app drains it to a WebSocket.

    Permission questions are delegated to an `asker` callable that blocks the agent
    thread until the UI answers (see server/bridge.py PermissionBridge).
    """

    def __init__(self, queue, asker=None):
        self.queue = queue
        self.asker = asker

    def emit(self, type: str, **data: Any) -> None:  # noqa: A002
        self.queue.put({"type": type, **data})

    def ask_permission(self, tool_name: str, preview: str) -> str:
        if self.asker is None:
            return "n"
        return self.asker(tool_name, preview)
