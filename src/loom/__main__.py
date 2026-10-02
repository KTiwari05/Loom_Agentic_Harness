"""`loom` CLI entry point.

    loom "add a --version flag to the CLI"       # run one goal in the current dir
    loom --dir ../other-project                   # chat with another project
    loom                                          # interactive chat (history persists)
    loom -y "..."                                 # auto-approve tools (trusted dirs only)
    loom ui                                       # the desktop app (opens in the browser)
    loom desktop                                  # same app in a native OS window

Interactive slash commands:
    /cost     spend + frontier-baseline savings so far
    /rewind   undo the file changes of the last turn
    /model X  switch the workhorse model mid-session
    /clear    start a fresh conversation (files stay as they are)
    /help     list commands
"""

from __future__ import annotations

import argparse
import os
import sys

from rich.console import Console

from . import __version__, skills
from .config import Config
from .orchestrator import Orchestrator


def _force_utf8() -> None:
    """Avoid UnicodeEncodeError on Windows when output is piped (legacy cp1252)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


def _slash(agent: Orchestrator, console: Console, line: str) -> bool:
    """Handle a slash command. Returns True if the line was a command."""
    if not line.startswith("/"):
        return False
    cmd, _, arg = line.partition(" ")
    cmd = cmd.lower()

    if cmd == "/cost":
        console.print(agent.cost_summary())
    elif cmd == "/rewind":
        restored = agent.rewind()
        if restored:
            console.print("[green]rewound:[/] " + ", ".join(restored))
        else:
            console.print("[yellow]nothing to rewind.[/]")
    elif cmd == "/model":
        if arg.strip():
            agent.config.model = arg.strip()
            console.print(f"model → [bold]{agent.config.model}[/]")
        else:
            console.print(f"model: [bold]{agent.config.model}[/] (strong: {agent.config.model_strong})")
    elif cmd == "/clear":
        agent.clear()
        console.print("[dim]conversation cleared.[/]")
    elif cmd == "/skills":
        for s in skills.discover(agent.config.workdir):
            console.print(f"[bold]/{s.name}[/] [dim]({s.source})[/] — {s.description}")
    elif cmd == "/help":
        console.print("/cost · /rewind · /model [name] · /skills · /clear · /help · exit")
        console.print("[dim]plus any skill as /<name> — see /skills[/]")
    else:
        skill = skills.find(agent.config.workdir, cmd[1:])
        if skill is not None:
            agent.run(skills.render(skill, arg))
        else:
            console.print(f"[yellow]unknown command {cmd}[/] — try /help")
    return True


def _serve_ui(port: int, open_browser: bool) -> None:
    """Start the desktop app: FastAPI + the static SPA, then open the browser."""
    import threading
    import webbrowser

    import uvicorn

    from .server.app import create_app

    os.environ.setdefault("LOOM_AI_TITLES", "1")  # session auto-titles in the app

    url = f"http://127.0.0.1:{port}"
    console = Console()
    console.print(f"[bold]🧵 Loom[/] desktop · [link={url}]{url}[/] [dim](Ctrl+C to stop)[/]")
    if open_browser:
        def open_browser_later():
            import time
            time.sleep(1)
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=open_browser_later, daemon=True).start()
    try:
        uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Error starting server:[/] {exc}")
        raise


def _serve_desktop(port: int) -> None:
    """The app in a native OS window (pywebview). Falls back to the browser if
    pywebview isn't installed or can't create a window."""
    import uvicorn

    from .server.app import create_app

    os.environ.setdefault("LOOM_AI_TITLES", "1")  # session auto-titles in the app
    try:
        import webview  # type: ignore[import-not-found]
    except ImportError:
        Console().print(
            "[yellow]pywebview is not installed[/] — falling back to the browser UI.\n"
            'Install the native window with: [bold]pip install -e ".[desktop]"[/]'
        )
        _serve_ui(port=port, open_browser=True)
        return

    import threading
    import webbrowser

    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    try:
        # pywebview must run on the main thread; blocks until the window closes.
        webview.create_window("Loom", url, width=1280, height=840, min_size=(900, 600))
        webview.start()
    except Exception as exc:  # noqa: BLE001  (no WebView2 runtime / no display)
        Console().print(f"[yellow]No native window ({exc})[/] — serving at [link={url}]{url}[/] instead.")
        webbrowser.open(url)
        thread.join()  # keep serving until killed
        return
    server.should_exit = True
    thread.join(timeout=5)


def main() -> None:
    _force_utf8()
    parser = argparse.ArgumentParser(prog="loom", description="An open coding agent on cheap open models.")
    parser.add_argument("goal", nargs="*", help="the task to perform; omit for an interactive chat")
    parser.add_argument("--dir", dest="directory", help="project directory to work in (default: cwd)")
    parser.add_argument("--model", help="override LOOM_MODEL for this run")
    parser.add_argument("--no-critic", action="store_true", help="skip the critic review step")
    parser.add_argument("--replay", metavar="TRACE", help="replay a recorded run (.loom/traces/*.jsonl)")
    parser.add_argument(
        "--fork-at",
        type=int,
        metavar="N",
        help="with --replay: replay the first N model calls, then continue with the live model",
    )
    parser.add_argument("-y", "--yes", action="store_true", help="auto-approve all tools (use with care)")
    parser.add_argument("--port", type=int, default=8763, help="port for `loom ui` (default 8763)")
    parser.add_argument("--no-open", action="store_true", help="with `loom ui`: don't open the browser")
    parser.add_argument("--version", action="version", version=f"loom {__version__}")
    args = parser.parse_args()

    if args.goal and args.goal[0].lower() == "ui":
        _serve_ui(port=args.port, open_browser=not args.no_open)
        return
    if args.goal and args.goal[0].lower() == "desktop":
        _serve_desktop(port=args.port)
        return

    console = Console()
    if args.directory:
        try:
            os.chdir(args.directory)
        except OSError as exc:
            console.print(f"[bold red]Cannot open project dir:[/] {exc}")
            raise SystemExit(1) from exc

    config = Config()
    config.workdir = os.getcwd()
    if args.model:
        config.model = args.model
    if args.yes:
        config.auto_approve = True
    if args.no_critic:
        config.critic_enabled = False

    if args.replay:
        from .replay import SequenceCompleter, load_trace

        try:
            goal, responses = load_trace(args.replay)
        except (OSError, ValueError) as exc:
            console.print(f"[bold red]Cannot replay:[/] {exc}")
            raise SystemExit(1) from exc

        cut = args.fork_at if args.fork_at is not None else len(responses)
        completer = SequenceCompleter(responses[:cut], live_after=args.fork_at is not None)
        agent = Orchestrator(config, console, completer=completer)
        mode = f"fork after call {cut}" if args.fork_at is not None else "full replay"
        console.print(
            f"[bold]🧵 Loom replay[/] [dim]· {args.replay} · {len(responses)} recorded calls "
            f"· {mode} · {config.workdir}[/]"
        )
        console.print(f"[dim]goal:[/] {goal}")
        agent.run(goal)
        return

    agent = Orchestrator(config, console)
    console.print(f"[bold]🧵 Loom[/] [dim]· {config.model} · {config.workdir}[/]")

    if args.goal:
        agent.run(" ".join(args.goal))
        return

    console.print("[dim]Chat with your project. /help for commands, 'exit' to quit.[/]")
    while True:
        try:
            line = console.input("\n[bold cyan]loom›[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if line.lower() in {"exit", "quit", "q"}:
            break
        if not line:
            continue
        if _slash(agent, console, line):
            continue
        agent.run(line)


if __name__ == "__main__":
    main()
