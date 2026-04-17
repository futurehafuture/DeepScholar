"""
DeepScholar entry point.

Usage:
  uv run python main.py --topic "Graph Neural Networks for Drug Property Prediction"
  uv run python main.py --resume --run-id run_20240414_143022
  uv run python main.py --list-runs

  # Use a different model:
  uv run python main.py --topic "..." --model glm-4-plus --compress-model glm-4-flash

  # Disable the web monitor:
  uv run python main.py --topic "..." --no-web

  # Or set via .env:
  DEEPSCHOLAR_MODEL=glm-4-plus
  DEEPSCHOLAR_COMPRESS_MODEL=glm-4-flash
"""

import argparse
import asyncio
import datetime
import os
import threading
import webbrowser
from threading import Event
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from agent.loop import run_agent_loop
from config import Config
from mcp_servers.client import MCPClient
from web import create_app, find_free_port

load_dotenv()
console = Console()

WEB_PORT = int(os.getenv("DEEPSCHOLAR_WEB_PORT", "8000"))


def start_web_server(run_id: str, port: int, msg_queue: asyncio.Queue, stop_event: Event) -> None:
    """Start the web monitor in a background daemon thread."""
    app = create_app(run_id, msg_queue, stop_event)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


def list_runs(workspace_root: str = "workspace") -> None:
    runs_dir = Path(workspace_root) / "runs"
    if not runs_dir.exists():
        console.print("[yellow]No runs found.[/yellow]")
        return

    table = Table(title="DeepScholar Runs")
    table.add_column("Run ID", style="cyan")
    table.add_column("Phase", style="green")
    table.add_column("Messages")
    table.add_column("Last Modified")

    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        state_file = run_dir / "state.json"
        history_file = run_dir / "history.jsonl"

        phase = "unknown"
        if state_file.exists():
            import json
            state = json.loads(state_file.read_text())
            phase = state.get("current_phase", "unknown")

        msg_count = "0"
        mtime = "—"
        if history_file.exists():
            lines = [l for l in history_file.read_text().splitlines() if l.strip()]
            msg_count = str(len(lines))
            mtime = datetime.datetime.fromtimestamp(
                history_file.stat().st_mtime
            ).strftime("%Y-%m-%d %H:%M")

        table.add_row(run_dir.name, phase, msg_count, mtime)

    console.print(table)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="DeepScholar — Autonomous Academic Research Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Model configuration (CLI > .env > defaults):\n"
            "  --model             Main research model         [DEEPSCHOLAR_MODEL]\n"
            "  --compress-model    Context compression model   [DEEPSCHOLAR_COMPRESS_MODEL]\n"
            "  --max-tokens        Max tokens per response     [DEEPSCHOLAR_MAX_TOKENS]\n"
            "  --context-window    Model context window size   [DEEPSCHOLAR_CONTEXT_WINDOW]\n"
        ),
    )
    parser.add_argument("--topic", type=str, help="Research topic (required for new runs)")
    parser.add_argument("--run-id", type=str, help="Run ID (required for --resume)")
    parser.add_argument("--resume", action="store_true", help="Resume an existing run")
    parser.add_argument("--list-runs", action="store_true", help="List all runs")
    parser.add_argument("--workspace", type=str, default="workspace", help="Workspace root directory")
    parser.add_argument("--no-web", action="store_true", help="Disable the web monitor")
    parser.add_argument("--web-port", type=int, default=WEB_PORT, help="Web monitor port (default 8000)")

    # Model configuration
    parser.add_argument("--model", type=str, default=None, help="Main model (e.g. glm-4-plus, glm-4-0520)")
    parser.add_argument("--compress-model", type=str, default=None, dest="compress_model", help="Compression model (e.g. glm-4-flash)")
    parser.add_argument("--max-tokens", type=int, default=None, dest="max_tokens", help="Max tokens per response")
    parser.add_argument("--context-window", type=int, default=None, dest="context_window", help="Model context window size in tokens")

    args = parser.parse_args()

    if args.list_runs:
        list_runs(args.workspace)
        return

    if not os.getenv("ZHIPUAI_API_KEY"):
        console.print("[bold red]Error: ZHIPUAI_API_KEY is not set. Add it to .env[/bold red]")
        return

    # Build config: env vars as base, CLI args as override
    cfg = Config.from_env().with_overrides(
        model=args.model,
        compress_model=args.compress_model,
        max_tokens=args.max_tokens,
        context_window=args.context_window,
    )

    if args.resume:
        if not args.run_id:
            console.print("[bold red]Error: --resume requires --run-id[/bold red]")
            return
        run_id = args.run_id
        initial_topic = None
    else:
        if not args.topic:
            console.print("[bold red]Error: --topic is required for a new run[/bold red]")
            return
        run_id = args.run_id or f"run_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        initial_topic = args.topic

    # Ensure workspace root exists before MCP filesystem server starts
    Path(args.workspace).mkdir(parents=True, exist_ok=True)

    msg_queue: asyncio.Queue = asyncio.Queue()
    stop_event: Event = Event()

    # Start web monitor in background (unless disabled)
    if not args.no_web:
        port = find_free_port(args.web_port)
        t = threading.Thread(
            target=start_web_server,
            args=(run_id, port, msg_queue, stop_event),
            daemon=True,
        )
        t.start()
        url = f"http://127.0.0.1:{port}"
        console.print(f"[dim]Monitor: [link={url}]{url}[/link][/dim]")
        await asyncio.sleep(0.8)
        webbrowser.open(url)

    async with MCPClient() as mcp:
        await run_agent_loop(
            run_id=run_id,
            mcp=mcp,
            cfg=cfg,
            initial_topic=initial_topic,
            workspace_root=args.workspace,
            msg_queue=msg_queue,
            web_enabled=not args.no_web,
            stop_event=stop_event,
        )

    # Keep web server alive after agent loop ends so the user can still browse history
    if not args.no_web:
        console.print(f"[bold green]✓ Session ended. Web monitor still live — press Ctrl+C to exit.[/bold green]")
        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass


if __name__ == "__main__":
    asyncio.run(main())
