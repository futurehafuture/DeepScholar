"""
DeepScholar main REPL loop.

This is the entire orchestration engine. The loop:
  1. Builds the current system prompt + tool list from state machine + MCP client
  2. Compresses context if needed
  3. Calls the configured main model (tool_choice="required" — model must always call a tool)
  4. Appends the response to the JSONL bus
  5. Dispatches tool calls:
       - transition_to_phase → handled internally by StateMachine
       - complete_research   → prints summary and breaks the loop (research done)
       - everything else     → dispatched to MCP servers
  6. Repeats until complete_research is called

On stop (finish_reason == "stop"): fallback — injects a corrective message to force tool use.
On tool_calls (finish_reason == "tool_calls"): executes all tool calls and continues.
On length (finish_reason == "length"): force-compresses context and continues.
"""

import asyncio
import json
import threading
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from zhipuai import ZhipuAI

from agent.context_manager import ContextManager
from agent.state_machine import StateMachine
from bus.jsonl_bus import JSONLBus
from config import Config
from mcp_servers.client import MCPClient

console = Console()


async def run_agent_loop(
    run_id: str,
    mcp: MCPClient,
    cfg: Config,
    initial_topic: str | None = None,
    workspace_root: str = "workspace",
    msg_queue: asyncio.Queue | None = None,
    web_enabled: bool = False,
    stop_event: threading.Event | None = None,
) -> None:
    """
    Run (or resume) a DeepScholar research session.

    If history.jsonl already exists for run_id, the full message history is
    loaded and the agent continues from where it left off.
    """
    client = ZhipuAI()  # reads ZHIPUAI_API_KEY from environment
    bus = JSONLBus(run_id, workspace_root)
    state_machine = StateMachine(run_id, workspace_root)
    ctx_manager = ContextManager(cfg)

    # ── Bootstrap ────────────────────────────────────────────────────────────
    if bus.exists():
        messages = bus.load_all()
        console.print(
            f"[bold green][Resume][/bold green] Loaded {len(messages)} messages "
            f"from history — current phase: [bold]{state_machine.current_phase.value}[/bold]"
        )
    else:
        if not initial_topic:
            raise ValueError("initial_topic is required for a new run")
        first_msg = {"role": "user", "content": f"开始研究课题：{initial_topic}"}
        messages = [first_msg]
        bus.append(first_msg)
        console.print(
            Panel(
                f"[bold]New run:[/bold] {run_id}\n"
                f"[bold]Topic:[/bold] {initial_topic}\n"
                f"[dim]{cfg.describe()}[/dim]",
                title="DeepScholar",
            )
        )

    # Workspace path the model must use for all file writes
    artifact_root = f"{workspace_root}/runs/{run_id}"

    # Pre-create the directory structure so MCP write_file never hits "parent does not exist"
    import os
    for d in ["artifacts", "artifacts/figures", "code"]:
        os.makedirs(f"{artifact_root}/{d}", exist_ok=True)

    # Stuck-detection: track consecutive identical tool failures
    _last_fail_key: str = ""
    _fail_count: int = 0
    MAX_CONSECUTIVE_FAILURES = 3
    # Stuck-detection: track consecutive stop turns (model ignoring tool_choice="required")
    _stop_count: int = 0
    MAX_STOP_REPEATS = 2

    # Interactive mode: entered after complete_research, waits for user messages
    interactive_mode = False

    # ── Main loop ─────────────────────────────────────────────────────────────
    while True:
        # Check for stop signal from web UI
        if stop_event is not None and stop_event.is_set():
            console.print("[bold red][Stopped by user via web UI][/bold red]")
            break

        # Interactive mode: block until user sends a message
        if interactive_mode:
            if msg_queue is None:
                break  # no queue, nothing to wait for
            try:
                user_content = await asyncio.wait_for(msg_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue  # loop back to check stop_event
            user_msg = {"role": "user", "content": user_content}
            messages.append(user_msg)
            bus.append(user_msg)
            console.print(f"[bold yellow][User][/bold yellow] {_truncate(user_content, 120)}")

        system_prompt = state_machine.get_current_system_prompt()
        # Inject the actual run path so the model writes to the right place
        system_prompt = system_prompt.replace(
            "workspace/runs/{run_id}/", f"{artifact_root}/"
        ).replace("{run_id}", run_id)
        tools = mcp.get_tools_for_phase(state_machine.current_phase.value)
        messages = ctx_manager.maybe_compress(messages, client)

        console.print(
            f"\n[dim]Phase: {state_machine.current_phase.value} | "
            f"Model: {cfg.model} | Messages: {len(messages)} | Tools: {len(tools)}[/dim]"
        )

        # Log the full LLM input to the bus for the web monitor (not added to messages)
        api_messages = _build_api_messages(system_prompt, messages)
        bus.append({
            "role": "__llm_call__",
            "phase": state_machine.current_phase.value,
            "model": cfg.model,
            "message_count": len(api_messages),
            "system_prompt": system_prompt,
            "tools": tools,
        })

        response = client.chat.completions.create(
            model=cfg.model,
            messages=api_messages,
            tools=tools,
            # interactive mode: model can respond freely; research mode: must call a tool
            tool_choice="auto" if interactive_mode else "required",
            max_tokens=cfg.max_tokens,
        )

        choice = response.choices[0]
        finish_reason = choice.finish_reason
        msg = choice.message

        # ── Append assistant turn to bus ──────────────────────────────────────
        assistant_record = _assistant_record(msg)
        messages.append(assistant_record)
        bus.append(assistant_record)

        # ── Handle finish_reason ──────────────────────────────────────────────
        if finish_reason == "stop":
            if msg.content:
                if web_enabled or interactive_mode:
                    console.print(f"[dim]↓ stop [{state_machine.current_phase.value}] {_truncate(msg.content, 120)}[/dim]")
                else:
                    console.print(Panel(Text(msg.content), title=f"[bold cyan]Agent [{state_machine.current_phase.value}][/bold cyan]"))

            if interactive_mode:
                # In interactive mode, stop is expected — wait for next user message (handled at loop top)
                pass
            else:
                # Research mode: model should not be stopping without a tool call
                _stop_count += 1
                user_content = None
                if msg_queue is not None:
                    try:
                        user_content = msg_queue.get_nowait()
                        console.print(f"[bold yellow][Web message][/bold yellow] {user_content}")
                        _stop_count = 0
                    except asyncio.QueueEmpty:
                        pass

                if user_content is None:
                    if _stop_count >= MAX_STOP_REPEATS:
                        user_content = (
                            f"[系统强制指令] 你已连续 {_stop_count} 次只输出文字而未调用任何工具，这不被允许。"
                            f"你必须立即调用以下工具之一：\n"
                            f"- write_file：将当前分析内容写入对应 artifact 文件\n"
                            f"- read_file：读取已有的 artifact 文件\n"
                            f"- transition_to_phase：如果本阶段无法推进，切换到下一阶段\n"
                            f"禁止再输出纯文字！必须调用工具！"
                        )
                        _stop_count = 0
                        console.print(f"[bold red][Stop-loop detected] Injecting forced tool instruction[/bold red]")
                    else:
                        user_content = (
                            "请立即调用工具完成当前阶段的任务。"
                            "所有内容必须通过 write_file 保存到文件，不要只输出文字。"
                            "如果当前阶段任务已完成，请调用 transition_to_phase 进入下一阶段。"
                        )

                nudge = {"role": "user", "content": user_content}
                messages.append(nudge)
                bus.append(nudge)

        elif finish_reason == "tool_calls":
            _stop_count = 0  # model is doing work, reset stop counter
            if msg.content:
                console.print(f"[dim cyan]{_truncate(msg.content, 200)}[/dim cyan]")

            tool_calls = msg.tool_calls or []
            tool_result_messages: list[dict] = []
            research_complete = False

            for tc in tool_calls:
                tool_name = tc.function.name
                try:
                    tool_input = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    tool_input = {}

                console.print(f"[bold magenta]→ Tool:[/bold magenta] {tool_name}({_truncate(str(tool_input), 120)})")

                if tool_name == "transition_to_phase":
                    result = state_machine.transition(tool_input.get("target_phase", ""))
                    console.print(f"[bold green]{result}[/bold green]")
                    _last_fail_key = ""
                    _fail_count = 0

                elif tool_name == "complete_research":
                    summary = tool_input.get("summary", "(no summary)")
                    console.print(Panel(
                        Text(f"{summary}\n\n[dim]Web monitor still live. Send messages from browser to continue.[/dim]"),
                        title="[bold green]Research Complete — Interactive Mode[/bold green]",
                    ))
                    result = f"研究任务已完成。{summary}\n\n现在进入交互模式，你可以回答用户的后续问题，也可以根据用户要求修改论文。"
                    research_complete = True

                else:
                    result = await mcp.call_tool(tool_name, tool_input)
                    if len(result) > 3000:
                        result = result[:3000] + "\n...（输出已截断，完整结果请通过 read_file 查看对应 artifact 文件）"

                    # Stuck detection: same tool+args failing repeatedly → force skip
                    fail_key = f"{tool_name}:{tool_input}"
                    is_error = "error" in result.lower() or "denied" in result.lower() or "not found" in result.lower()
                    if is_error and fail_key == _last_fail_key:
                        _fail_count += 1
                    elif is_error:
                        _last_fail_key = fail_key
                        _fail_count = 1
                    else:
                        _last_fail_key = ""
                        _fail_count = 0

                    if _fail_count >= MAX_CONSECUTIVE_FAILURES:
                        result = (
                            f"{result}\n\n"
                            f"[系统提示] 此工具调用已连续失败 {_fail_count} 次，请放弃此操作，"
                            f"跳过当前论文/文件，继续处理下一个任务。不要再重试相同的调用。"
                        )
                        _last_fail_key = ""
                        _fail_count = 0
                        console.print(f"[bold red][Stuck detected] Injecting skip instruction[/bold red]")

                console.print(f"[dim]  ↳ {_truncate(result, 200)}[/dim]")

                tool_result_messages.append({
                    "role": "tool",
                    "content": result,
                    "tool_call_id": tc.id,
                })

                if research_complete:
                    break  # no need to process further tool calls

            for tr in tool_result_messages:
                messages.append(tr)
                bus.append(tr)

            if research_complete:
                interactive_mode = True  # switch to interactive instead of exiting

        elif finish_reason == "length":
            console.print("[bold red][Warning] Context length limit hit — forcing compression...[/bold red]")
            messages = ctx_manager.force_compress(messages, client)

        else:
            console.print(f"[yellow]Unexpected finish_reason: {finish_reason}[/yellow]")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_api_messages(system_prompt: str, messages: list[dict]) -> list[dict]:
    """Prepend system message. GLM-4 uses OpenAI message format."""
    return [{"role": "system", "content": system_prompt}] + messages


def _assistant_record(msg: Any) -> dict:
    """Serialize a GLM-4 assistant message for the JSONL bus."""
    record: dict = {"role": "assistant", "content": msg.content or None}
    if msg.tool_calls:
        record["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in msg.tool_calls
        ]
    return record


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[:max_len] + "..."
