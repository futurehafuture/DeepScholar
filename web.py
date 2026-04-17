"""
DeepScholar Web Monitor

Runs alongside the agent to show real-time progress in a browser.

Usage:
  uv run python web.py --run-id run_20260416_225809
  uv run python web.py --run-id run_20260416_225809 --port 8080

Then open http://localhost:8000
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route
from starlette.middleware.cors import CORSMiddleware

load_dotenv()

WORKSPACE = "workspace"
PHASE_COLORS = {
    "survey":     "#6366f1",
    "literature": "#8b5cf6",
    "arguments":  "#ec4899",
    "innovation": "#f59e0b",
    "experiment": "#10b981",
    "analysis":   "#3b82f6",
    "writing":    "#14b8a6",
}
PHASES = ["survey", "literature", "arguments", "innovation", "experiment", "analysis", "writing"]


def get_run_id(request: Request) -> str:
    return request.app.state.run_id


def state_path(run_id: str) -> Path:
    return Path(WORKSPACE) / "runs" / run_id / "state.json"


def history_path(run_id: str) -> Path:
    return Path(WORKSPACE) / "runs" / run_id / "history.jsonl"


def read_state(run_id: str) -> dict:
    p = state_path(run_id)
    if p.exists():
        return json.loads(p.read_text())
    return {"current_phase": "survey", "metadata": {}}


def read_history(run_id: str) -> list[dict]:
    p = history_path(run_id)
    if not p.exists():
        return []
    lines = [l.strip() for l in p.read_text().splitlines() if l.strip()]
    result = []
    for line in lines:
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return result


async def homepage(request: Request) -> HTMLResponse:
    run_id = get_run_id(request)
    return HTMLResponse(HTML_TEMPLATE.replace("__RUN_ID__", run_id))


async def api_state(request: Request) -> JSONResponse:
    run_id = get_run_id(request)
    state = read_state(run_id)
    history = read_history(run_id)
    return JSONResponse({
        "run_id": run_id,
        "current_phase": state.get("current_phase", "survey"),
        "message_count": len(history),
        "phases": PHASES,
    })


async def api_history(request: Request) -> JSONResponse:
    run_id = get_run_id(request)
    history = read_history(run_id)
    return JSONResponse({"messages": history})


async def api_stream(request: Request):
    """SSE stream: watches history.jsonl for new lines and pushes them to the browser."""
    run_id = get_run_id(request)
    p = history_path(run_id)

    async def generator():
        # Send current state on connect
        state = read_state(run_id)
        history = read_history(run_id)
        yield {"event": "init", "data": json.dumps({
            "current_phase": state.get("current_phase", "survey"),
            "message_count": len(history),
            "messages": history[-50:],  # last 50 on connect
        })}

        last_size = p.stat().st_size if p.exists() else 0
        last_mtime = p.stat().st_mtime if p.exists() else 0

        while True:
            await asyncio.sleep(1)

            if await request.is_disconnected():
                break

            # Check for new state
            state = read_state(run_id)

            # Check for new lines in history
            if p.exists():
                current_mtime = p.stat().st_mtime
                if current_mtime != last_mtime:
                    current_size = p.stat().st_size
                    if current_size > last_size:
                        # Read only new lines
                        with open(p, "r") as f:
                            f.seek(last_size)
                            new_content = f.read()
                        new_messages = []
                        for line in new_content.splitlines():
                            line = line.strip()
                            if line:
                                try:
                                    new_messages.append(json.loads(line))
                                except json.JSONDecodeError:
                                    pass
                        if new_messages:
                            yield {"event": "messages", "data": json.dumps({
                                "current_phase": state.get("current_phase", "survey"),
                                "new_messages": new_messages,
                            })}
                        last_size = current_size
                    last_mtime = current_mtime

    return EventSourceResponse(generator())


async def api_system_prompt(request: Request) -> JSONResponse:
    """Return system prompts for all phases (plus current phase indicator)."""
    run_id = get_run_id(request)
    state = read_state(run_id)
    current_phase = state.get("current_phase", "survey")

    prompts_dir = Path(__file__).parent / "agent" / "prompts"
    phase_map = {
        "survey": "01_survey", "literature": "02_literature",
        "arguments": "03_arguments", "innovation": "04_innovation",
        "experiment": "05_experiment", "analysis": "06_analysis",
        "writing": "07_writing",
    }
    try:
        base = (prompts_dir / "base_system.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        base = "(base_system.md not found)"

    phases = {}
    for phase, fname in phase_map.items():
        try:
            phase_content = (prompts_dir / f"phase_{fname}.md").read_text(encoding="utf-8")
            phases[phase] = f"{base}\n\n---\n\n{phase_content}"
        except FileNotFoundError:
            phases[phase] = f"{base}\n\n---\n\n(phase file not found: phase_{fname}.md)"

    return JSONResponse({"current_phase": current_phase, "phases": phases})


async def api_message(request: Request) -> JSONResponse:
    """Receive a message from the web UI and forward it to the agent loop."""
    q: asyncio.Queue | None = request.app.state.msg_queue
    if q is None:
        return JSONResponse({"error": "no agent running"}, status_code=503)
    body = await request.json()
    content = body.get("content", "").strip()
    if not content:
        return JSONResponse({"error": "empty message"}, status_code=400)
    await q.put(content)
    return JSONResponse({"ok": True})


async def api_stop(request: Request) -> JSONResponse:
    """Signal the agent loop to stop."""
    import threading
    ev = request.app.state.stop_event
    if ev is None:
        return JSONResponse({"error": "no stop_event configured"}, status_code=503)
    ev.set()
    return JSONResponse({"ok": True})


def create_app(
    run_id: str,
    msg_queue: asyncio.Queue | None = None,
    stop_event=None,  # threading.Event | None
) -> Starlette:
    app = Starlette(routes=[
        Route("/", homepage),
        Route("/api/state", api_state),
        Route("/api/history", api_history),
        Route("/api/stream", api_stream),
        Route("/api/message", api_message, methods=["POST"]),
        Route("/api/stop", api_stop, methods=["POST"]),
        Route("/api/system-prompt", api_system_prompt),
    ])
    app.state.run_id = run_id
    app.state.msg_queue = msg_queue
    app.state.stop_event = stop_event
    return app


def find_free_port(start: int = 8000) -> int:
    import socket
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start




HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DeepScholar</title>
<style>
:root {
  --paper:    #F5F0E8;
  --paper-2:  #EDE7D9;
  --paper-3:  #E4DBCC;
  --ink:      #1C1611;
  --ink-2:    #3D3328;
  --ink-3:    #6B5E4E;
  --ink-4:    #9C8E80;
  --gold:     #B8860B;
  --gold-l:   #D4A017;
  --gold-bg:  #FBF3DC;
  --sage:     #5A7A5A;
  --sage-bg:  #EBF2EB;
  --rose:     #9B4D52;
  --rose-bg:  #F9ECED;
  --blue:     #2C5F8A;
  --blue-bg:  #EAF1F8;
  --shadow-sm: 0 1px 3px rgba(28,22,17,.08), 0 1px 2px rgba(28,22,17,.06);
  --shadow:    0 4px 12px rgba(28,22,17,.10), 0 2px 4px rgba(28,22,17,.06);
  --radius:    10px;
}

:root {
  --font-sans:  -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  --font-serif: Georgia, "Palatino Linotype", Palatino, serif;
  --font-mono:  Menlo, Consolas, "Courier New", monospace;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: var(--font-serif);
  background: var(--paper);
  color: var(--ink);
  height: 100vh;
  display: flex;
  flex-direction: column;
  font-size: 15px;
  line-height: 1.65;
}

/* ── HEADER ─────────────────────────────────────── */
.header {
  padding: 0 28px;
  height: 52px;
  background: var(--ink);
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
  position: relative;
  overflow: hidden;
}
.header::after {
  content: '';
  position: absolute;
  inset: 0;
  background: repeating-linear-gradient(
    90deg,
    transparent, transparent 60px,
    rgba(184,134,11,.06) 60px, rgba(184,134,11,.06) 61px
  );
  pointer-events: none;
}
.logo {
  font-family: var(--font-serif);
  font-size: 20px;
  font-weight: 700;
  color: var(--paper);
  letter-spacing: -0.3px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.logo-accent { color: var(--gold-l); }
.logo-divider { width: 1px; height: 18px; background: rgba(245,240,232,.2); }
.logo-sub { font-family: var(--font-mono); font-size: 10px; color: rgba(245,240,232,.45); font-weight: 400; letter-spacing: 1px; text-transform: uppercase; }

.header-right { display: flex; align-items: center; gap: 16px; }
.run-badge {
  font-family: var(--font-mono);
  font-size: 10px;
  color: rgba(245,240,232,.5);
  letter-spacing: 0.5px;
  border: 1px solid rgba(245,240,232,.15);
  padding: 3px 8px;
  border-radius: 4px;
}
.live-dot {
  display: flex; align-items: center; gap: 6px;
  font-size: 11px; color: var(--gold-l);
  font-family: var(--font-mono); letter-spacing: 0.5px;
}
.live-dot::before {
  content: '';
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--gold-l);
  box-shadow: 0 0 8px var(--gold);
  animation: pulse 2.2s ease-in-out infinite;
}
@keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.5;transform:scale(.85)} }
.stop-btn {
  height: 28px; padding: 0 12px;
  background: transparent;
  border: 1px solid rgba(155,77,82,.5);
  border-radius: 5px;
  color: #c97a7e;
  font-family: var(--font-mono);
  font-size: 10px; letter-spacing: 0.8px; text-transform: uppercase;
  cursor: pointer; transition: all .2s;
}
.stop-btn:hover { background: rgba(155,77,82,.15); border-color: #c97a7e; color: #e8a0a4; }
.stop-btn.stopped { background: rgba(155,77,82,.2); border-color: #9b4d52; color: #e8a0a4; cursor: default; }

/* ── PHASE TIMELINE ─────────────────────────────── */
.timeline-wrap {
  background: var(--paper-2);
  border-bottom: 1px solid var(--paper-3);
  padding: 0 28px;
  flex-shrink: 0;
  overflow-x: auto;
}
.timeline {
  display: flex;
  align-items: stretch;
  height: 44px;
  gap: 0;
  min-width: max-content;
}
.phase-step {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 16px;
  font-size: 12px;
  font-family: var(--font-mono);
  color: var(--ink-4);
  letter-spacing: 0.3px;
  position: relative;
  border-right: 1px solid var(--paper-3);
  transition: all .3s ease;
  cursor: pointer;
  user-select: none;
}
.phase-step:hover { background: rgba(184,134,11,.06); }
.phase-step:last-child { border-right: none; }
.phase-num {
  width: 18px; height: 18px;
  border-radius: 50%;
  border: 1.5px solid var(--ink-4);
  display: flex; align-items: center; justify-content: center;
  font-size: 9px; font-weight: 500;
  transition: all .3s;
  flex-shrink: 0;
}
.phase-step.done { color: var(--sage); }
.phase-step.done .phase-num { background: var(--sage); border-color: var(--sage); color: white; font-size: 10px; }
.phase-step.done .phase-num::after { content: '✓'; }
.phase-step.done .phase-num span { display: none; }
.phase-step.active {
  color: var(--gold);
  background: linear-gradient(to bottom, var(--gold-bg), transparent);
}
.phase-step.active .phase-num {
  background: var(--gold);
  border-color: var(--gold);
  color: white;
  box-shadow: 0 0 10px rgba(184,134,11,.3);
}
.phase-label { font-size: 11px; }

/* ── MAIN LAYOUT ─────────────────────────────────── */
.layout { display: flex; flex: 1; overflow: hidden; }

/* ── FEED ─────────────────────────────────────────── */
.feed-wrap { flex: 1; overflow-y: auto; padding: 24px 28px 16px; display: flex; flex-direction: column; gap: 12px; }
.feed-wrap::-webkit-scrollbar { width: 5px; }
.feed-wrap::-webkit-scrollbar-track { background: var(--paper-2); }
.feed-wrap::-webkit-scrollbar-thumb { background: var(--paper-3); border-radius: 3px; }

/* ── CARDS ─────────────────────────────────────────── */
.card {
  border-radius: var(--radius);
  padding: 14px 18px;
  box-shadow: var(--shadow-sm);
  border: 1px solid transparent;
  position: relative;
}
.card-label {
  font-family: var(--font-mono);
  font-size: 9px;
  font-weight: 500;
  letter-spacing: 1.2px;
  text-transform: uppercase;
  margin-bottom: 7px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.card-text { color: var(--ink-2); font-size: 14.5px; white-space: pre-wrap; word-break: break-word; }
.card-text--assistant { max-height: 600px; overflow-y: auto; }

/* User */
.card-user { background: white; border-color: var(--paper-3); }
.card-user .card-label { color: var(--ink-4); }

/* Assistant */
.card-assistant { background: white; border-color: var(--paper-3); border-left: 3px solid var(--gold-l); }
.card-assistant .card-label { color: var(--gold); }

/* Tool call */
.card-tool { background: var(--ink); border-color: rgba(255,255,255,.06); }
.card-tool .card-label { color: rgba(245,240,232,.45); }
.card-tool .tool-name { font-family: var(--font-mono); font-size: 13px; font-weight: 500; color: var(--gold-l); }
.card-tool .tool-args {
  font-family: var(--font-mono); font-size: 11px; color: rgba(245,240,232,.55);
  background: rgba(0,0,0,.2); border-radius: 6px; padding: 8px 10px; margin-top: 8px;
  white-space: pre-wrap; word-break: break-all; max-height: 110px; overflow-y: auto;
}

/* Tool result */
.card-result { background: var(--paper-2); border-color: var(--paper-3); }
.card-result .card-label { color: var(--ink-4); }
.card-result .card-text {
  font-family: var(--font-mono); font-size: 11px;
  color: var(--ink-3); max-height: 300px; overflow-y: auto;
}

/* Phase transition */
.card-transition {
  background: var(--sage-bg);
  border: 1px dashed var(--sage);
  text-align: center;
  padding: 10px 18px;
}
.card-transition .card-text {
  font-family: var(--font-mono);
  font-size: 12px; color: var(--sage);
  letter-spacing: 0.3px;
}

/* System prompt */
.card-sys {
  background: var(--blue-bg);
  border-color: rgba(44,95,138,.2);
  border-left: 3px solid var(--blue);
}
.card-sys .card-label { color: var(--blue); }
.sp-tabs {
  display: flex; flex-wrap: wrap; gap: 4px; margin-top: 8px;
}
.sp-tab {
  font-family: var(--font-mono); font-size: 9px; letter-spacing: 0.5px;
  text-transform: uppercase; padding: 3px 8px; border-radius: 4px;
  border: 1px solid rgba(44,95,138,.25); color: var(--ink-4);
  cursor: pointer; background: transparent; transition: all .15s;
  user-select: none;
}
.sp-tab:hover { border-color: var(--blue); color: var(--blue); background: rgba(44,95,138,.06); }
.sp-tab.active { background: var(--blue); border-color: var(--blue); color: white; }
.sp-tab.current-phase { border-color: var(--gold); color: var(--gold); }
.sp-tab.current-phase.active { background: var(--gold); border-color: var(--gold); color: white; }
.sp-body {
  font-family: var(--font-mono); font-size: 11px; color: var(--ink-3);
  white-space: pre-wrap; word-break: break-word; line-height: 1.7;
  max-height: 420px; overflow-y: auto; display: none; margin-top: 10px;
  border-top: 1px solid rgba(44,95,138,.12); padding-top: 10px;
}
.sp-body.open { display: block; }
.card-sys-toggle {
  font-family: var(--font-mono); font-size: 10px;
  color: var(--blue); cursor: pointer; border: none; background: none;
  padding: 0; letter-spacing: 0.3px;
}
.card-sys-toggle:hover { text-decoration: underline; }

/* LLM Call record */
.card-llm {
  background: var(--ink);
  border-color: rgba(255,255,255,.06);
  border-left: 3px solid var(--blue);
}
.card-llm .card-label { color: rgba(245,240,232,.4); justify-content: space-between; }
.card-llm .llm-meta { font-family: var(--font-mono); font-size: 11px; color: var(--gold-l); }
.llm-sections { margin-top: 8px; display: flex; flex-direction: column; gap: 6px; }
.llm-section-header {
  font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.5px;
  color: rgba(245,240,232,.4); cursor: pointer; display: flex; align-items: center; gap: 6px;
  user-select: none; padding: 2px 0;
}
.llm-section-header:hover { color: rgba(245,240,232,.7); }
.llm-section-body {
  font-family: var(--font-mono); font-size: 11px;
  color: rgba(245,240,232,.65); white-space: pre-wrap; word-break: break-word;
  line-height: 1.6; background: rgba(0,0,0,.25); border-radius: 5px;
  padding: 10px 12px; max-height: 360px; overflow-y: auto; display: none;
}
.llm-section-body.open { display: block; }

/* Compression */
.card-compress { background: var(--rose-bg); border-color: rgba(155,77,82,.2); }
.card-compress .card-label { color: var(--rose); }
.card-compress .card-text { font-size: 13px; color: var(--ink-3); }

/* Empty */
.empty-state {
  flex: 1; display: flex; align-items: center; justify-content: center;
  flex-direction: column; gap: 16px; color: var(--ink-4); padding: 60px 0;
}
.empty-icon { font-size: 40px; opacity: .5; }
.empty-text { font-family: var(--font-serif); font-size: 18px; color: var(--ink-3); }
.empty-sub { font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.5px; }

/* ── INPUT BAR ─────────────────────────────────────── */
.input-area {
  background: white;
  border-top: 1px solid var(--paper-3);
  padding: 12px 28px;
  display: flex;
  gap: 10px;
  align-items: flex-end;
  flex-shrink: 0;
  box-shadow: 0 -2px 8px rgba(28,22,17,.05);
}
.input-area textarea {
  flex: 1;
  border: 1px solid var(--paper-3);
  border-radius: 8px;
  background: var(--paper);
  color: var(--ink);
  font-family: var(--font-serif);
  font-size: 14.5px;
  padding: 8px 14px;
  resize: none;
  height: 38px;
  outline: none;
  transition: border-color .2s;
}
.input-area textarea:focus { border-color: var(--gold-l); }
.input-area textarea::placeholder { color: var(--ink-4); }
.send-btn {
  height: 38px;
  padding: 0 20px;
  background: var(--ink);
  color: var(--paper);
  border: none;
  border-radius: 8px;
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 1px;
  text-transform: uppercase;
  cursor: pointer;
  transition: background .2s, transform .1s;
  flex-shrink: 0;
}
.send-btn:hover { background: var(--ink-2); }
.send-btn:active { transform: scale(.97); }
.input-hint {
  text-align: center;
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--ink-4);
  padding: 4px 28px 8px;
  background: white;
  letter-spacing: 0.3px;
  flex-shrink: 0;
}

#bottom { height: 1px; }
</style>
</head>
<body>

<header class="header">
  <div class="logo">
    <span>Deep<span class="logo-accent">Scholar</span></span>
    <div class="logo-divider"></div>
    <span class="logo-sub">Research Monitor</span>
  </div>
  <div class="header-right">
    <span class="run-badge" id="run-label">__RUN_ID__</span>
    <button class="stop-btn" id="stop-btn" onclick="stopAgent()">■ Stop</button>
    <span class="live-dot" id="live-dot">LIVE</span>
  </div>
</header>

<div class="timeline-wrap">
  <div class="timeline" id="timeline"></div>
</div>

<div class="layout">
  <div class="feed-wrap" id="feed">
    <div id="sys-prompt-container"></div>
    <div class="empty-state" id="empty-state">
      <div class="empty-icon">◎</div>
      <div class="empty-text">Waiting for agent…</div>
      <div class="empty-sub">RUN · __RUN_ID__</div>
    </div>
    <div id="bottom"></div>
  </div>
</div>

<div class="input-area">
  <textarea id="msg-input" placeholder="Send a message to the agent (will be delivered on next pause)…"></textarea>
  <button class="send-btn" id="msg-send">Send</button>
</div>
<div class="input-hint">Agent runs autonomously · messages delivered on next stop</div>

<script>
const PHASES = ["survey","literature","arguments","innovation","experiment","analysis","writing"];
const PHASE_ZH = {survey:"调研",literature:"文献",arguments:"论点",innovation:"创新",experiment:"实验",analysis:"分析",writing:"撰写"};

let currentPhase = "survey";
let totalMessages = 0;
let autoScroll = true;
let loadedPhase = "";

// Build timeline
const tl = document.getElementById("timeline");
PHASES.forEach((p, i) => {
  const el = document.createElement("div");
  el.className = "phase-step";
  el.id = "ps-" + p;
  el.title = `跳转到 ${PHASE_ZH[p]} 阶段`;
  el.innerHTML = `<div class="phase-num"><span>${i+1}</span></div><span class="phase-label">${PHASE_ZH[p]}</span>`;
  el.addEventListener("click", () => scrollToPhase(p));
  tl.appendChild(el);
});

function updateTimeline(phase) {
  if (phase === currentPhase && loadedPhase) return;
  currentPhase = phase;
  const idx = PHASES.indexOf(phase);
  PHASES.forEach((p, i) => {
    const el = document.getElementById("ps-" + p);
    el.className = "phase-step" + (i < idx ? " done" : i === idx ? " active" : "");
  });
}

function esc(s) {
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
function trunc(s, n) { s=String(s); return s.length>n ? s.slice(0,n)+"…" : s; }

function scrollToBottom() {
  if (autoScroll) document.getElementById("bottom").scrollIntoView({behavior:"smooth"});
}

// Store large LLM call content in JS (NOT in DOM) to avoid memory bloat
const llmStore = new Map(); // id → {system_prompt, tools_json}
let llmCallCount = 0;

function renderMsg(msg) {
  const role = msg.role;
  const content = msg.content;

  if (role === "__llm_call__") {
    llmCallCount++;
    const id = "llmc" + llmCallCount;
    // Store content in JS Map — NOT in DOM
    llmStore.set(id, {
      system_prompt: msg.system_prompt || "",
      tools_json: JSON.stringify(msg.tools || [], null, 2),
    });
    const d = document.createElement("div");
    d.className = "card card-llm";
    d.setAttribute("data-phase", msg.phase || currentPhase);
    d.innerHTML = `
      <div class="card-label" style="cursor:pointer" onclick="toggleLlmCard('${id}')">
        <span>LLM Call #${llmCallCount}</span>
        <span style="display:flex;align-items:center;gap:10px">
          <span class="llm-meta">${esc(msg.phase||"")} · ${esc(msg.model||"")} · ${msg.message_count||0} msgs</span>
          <span id="${id}-arrow" style="font-size:10px;color:rgba(245,240,232,.35)">▶ expand</span>
        </span>
      </div>
      <div class="llm-sections" id="${id}-sections" style="display:none">
        <div>
          <div class="llm-section-header" onclick="event.stopPropagation();toggleLlmSection('${id}','sp',this)">
            <span>▶</span> System Prompt
          </div>
          <div class="llm-section-body" id="${id}-sp"></div>
        </div>
        <div>
          <div class="llm-section-header" onclick="event.stopPropagation();toggleLlmSection('${id}','tools',this)">
            <span>▶</span> Tools Schema (${(msg.tools||[]).length} tools)
          </div>
          <div class="llm-section-body" id="${id}-tools"></div>
        </div>
      </div>`;
    return d;
  }

  if (role === "tool") {
    const d = document.createElement("div");
    d.className = "card card-result";
    d.setAttribute("data-phase", currentPhase);
    d.innerHTML = `<div class="card-label">Tool Result</div><div class="card-text">${esc(content||"")}</div>`;
    return d;
  }

  if (role === "user") {
    if (typeof content === "string" && content.startsWith("[系统压缩摘要")) {
      const d = document.createElement("div");
      d.className = "card card-compress";
      d.setAttribute("data-phase", currentPhase);
      d.innerHTML = `<div class="card-label">Compression Summary</div><div class="card-text">${esc(content)}</div>`;
      return d;
    }
    const d = document.createElement("div");
    d.className = "card card-user";
    d.setAttribute("data-phase", currentPhase);
    const text = typeof content==="string" ? content : JSON.stringify(content);
    d.innerHTML = `<div class="card-label">User</div><div class="card-text">${esc(text)}</div>`;
    return d;
  }

  if (role === "assistant") {
    const frag = document.createDocumentFragment();
    if (content) {
      const d = document.createElement("div");
      d.className = "card card-assistant";
      d.setAttribute("data-phase", currentPhase);
      d.innerHTML = `<div class="card-label">Assistant · ${esc(currentPhase)}</div><div class="card-text card-text--assistant">${esc(content)}</div>`;
      frag.appendChild(d);
    }
    (msg.tool_calls||[]).forEach(tc => {
      const name = tc.function?.name || "";
      let args = {};
      try { args = JSON.parse(tc.function?.arguments||"{}"); } catch(e){}
      if (name === "transition_to_phase") {
        const d = document.createElement("div");
        d.className = "card card-transition";
        d.setAttribute("data-phase", args.target_phase || currentPhase);
        d.innerHTML = `<div class="card-text">Phase transition → <strong>${esc(args.target_phase||"?")}</strong> &nbsp;·&nbsp; ${esc(args.reason||"")}</div>`;
        frag.appendChild(d);
        return;
      }
      if (name === "complete_research") {
        const d = document.createElement("div");
        d.className = "card card-transition";
        d.setAttribute("data-phase", currentPhase);
        d.style.borderColor = "var(--sage)";
        d.innerHTML = `<div class="card-text">✓ Research complete &nbsp;·&nbsp; ${esc(args.summary||"")}</div>`;
        frag.appendChild(d);
        return;
      }
      const d = document.createElement("div");
      d.className = "card card-tool";
      d.setAttribute("data-phase", currentPhase);
      d.innerHTML = `<div class="card-label">Tool Call</div><div class="tool-name">${esc(name)}</div><div class="tool-args">${esc(JSON.stringify(args,null,2))}</div>`;
      frag.appendChild(d);
    });
    return frag;
  }
  return null;
}

function appendMessages(msgs, phase) {
  const empty = document.getElementById("empty-state");
  if (empty) empty.remove();
  const bottom = document.getElementById("bottom");
  const feed = document.getElementById("feed");
  // Temporarily set currentPhase for tagging if provided
  const prev = currentPhase;
  if (phase) currentPhase = phase;
  msgs.forEach(m => {
    // Update phase mid-batch if a transition_to_phase is encountered
    if (m.role === "assistant" && m.tool_calls) {
      const tr = m.tool_calls.find(tc => tc.function?.name === "transition_to_phase");
      if (tr) {
        try { const a = JSON.parse(tr.function.arguments||"{}"); if (a.target_phase) currentPhase = a.target_phase; } catch(e){}
      }
    }
    const el = renderMsg(m);
    if (el) feed.insertBefore(el, bottom);
  });
  if (!phase) currentPhase = prev;
  totalMessages += msgs.length;
  scrollToBottom();
}

function scrollToPhase(phase) {
  const el = document.querySelector(`[data-phase="${phase}"]`);
  if (el) el.scrollIntoView({behavior: "smooth", block: "start"});
}

// System prompt card — loaded once, tabs for all phases
let spData = null;
let spActiveTab = "";
let spOpen = false;

async function loadSystemPrompt(phase) {
  // Always update the active phase indicator
  if (spData) {
    updateSpActivePhase(phase);
    return;
  }
  // First load: fetch all phases
  try {
    const res = await fetch("/api/system-prompt");
    spData = await res.json();
    renderSpCard(spData.current_phase);
  } catch(e) {}
}

function renderSpCard(activePhase) {
  const c = document.getElementById("sys-prompt-container");
  const phaseZh = {survey:"调研",literature:"文献",arguments:"论点",innovation:"创新",experiment:"实验",analysis:"分析",writing:"撰写"};
  const tabs = PHASES.map(p =>
    `<button class="sp-tab${p===activePhase?' current-phase':''}${p===spActiveTab?' active':''}" id="spt-${p}" onclick="switchSpTab('${p}')">${phaseZh[p]||p}</button>`
  ).join("");
  c.innerHTML = `<div class="card card-sys">
    <div class="card-label" style="justify-content:space-between">
      <span>System Prompt</span>
      <button class="card-sys-toggle" id="sp-toggle" onclick="toggleSP()">${spOpen?'▴ collapse':'▾ expand'}</button>
    </div>
    <div class="sp-tabs">${tabs}</div>
    <div class="sp-body${spOpen?' open':''}" id="sp-body"></div>
  </div>`;
  // Default to showing active phase tab
  if (!spActiveTab) spActiveTab = activePhase;
  switchSpTab(spActiveTab, false);
}

function switchSpTab(phase, scroll) {
  if (!spData) return;
  spActiveTab = phase;
  document.querySelectorAll(".sp-tab").forEach(t => {
    t.classList.toggle("active", t.id === "spt-" + phase);
  });
  const body = document.getElementById("sp-body");
  if (body) {
    body.textContent = spData.phases[phase] || "(not available)";
    if (scroll !== false && spOpen) body.scrollTop = 0;
  }
}

function updateSpActivePhase(phase) {
  document.querySelectorAll(".sp-tab").forEach(t => {
    const p = t.id.replace("spt-","");
    t.classList.toggle("current-phase", p === phase);
  });
}

function toggleLlmCard(id) {
  const sections = document.getElementById(id + "-sections");
  const arrow = document.getElementById(id + "-arrow");
  if (!sections) return;
  const open = sections.style.display === "none";
  sections.style.display = open ? "block" : "none";
  if (arrow) arrow.textContent = open ? "▼ collapse" : "▶ expand";
}

function toggleLlmSection(storeId, key, header) {
  const bodyId = storeId + "-" + key;
  const body = document.getElementById(bodyId);
  if (!body) return;
  const open = body.classList.toggle("open");
  const arrow = header.querySelector("span");
  if (arrow) arrow.textContent = open ? "▼" : "▶";
  if (open) {
    // Lazy-load from JS store (not from DOM)
    if (!body.textContent) {
      const data = llmStore.get(storeId);
      if (data) body.textContent = key === "sp" ? data.system_prompt : data.tools_json;
    }
    body.scrollTop = 0;
  } else {
    // Clear DOM content when collapsed to free memory
    body.textContent = "";
  }
}

function toggleSP() {
  spOpen = !spOpen;
  const body = document.getElementById("sp-body");
  const btn = document.getElementById("sp-toggle");
  if (body) body.classList.toggle("open", spOpen);
  if (btn) btn.textContent = spOpen ? "▴ collapse" : "▾ expand";
  if (spOpen && body) body.scrollTop = 0;
}

// Stop agent
async function stopAgent() {
  const btn = document.getElementById("stop-btn");
  if (btn.classList.contains("stopped")) return;
  if (!confirm("确认终止 Agent 任务？")) return;
  btn.classList.add("stopped");
  btn.textContent = "■ Stopping…";
  try {
    await fetch("/api/stop", {method: "POST"});
    btn.textContent = "■ Stopped";
    document.getElementById("live-dot").style.opacity = "0.3";
  } catch(e) {
    btn.classList.remove("stopped");
    btn.textContent = "■ Stop";
  }
}

// SSE — track how many messages have been appended to avoid duplicates on reconnect
let appendedCount = 0;
let activeEs = null;

function connect() {
  // Always close previous connection before opening a new one
  if (activeEs) { activeEs.close(); activeEs = null; }

  const es = new EventSource("/api/stream");
  activeEs = es;

  es.addEventListener("init", e => {
    const data = JSON.parse(e.data);
    updateTimeline(data.current_phase);
    loadSystemPrompt(data.current_phase);
    const msgs = data.messages || [];
    // Only append messages we haven't seen yet (prevents duplicates on reconnect)
    const unseen = msgs.slice(appendedCount);
    if (unseen.length > 0) appendMessages(unseen, data.current_phase);
    appendedCount = Math.max(appendedCount, msgs.length);
    document.getElementById("live-dot").style.opacity = "1";
  });

  es.addEventListener("messages", e => {
    const data = JSON.parse(e.data);
    updateTimeline(data.current_phase);
    loadSystemPrompt(data.current_phase);
    const newMsgs = data.new_messages || [];
    appendMessages(newMsgs, data.current_phase);
    appendedCount += newMsgs.length;
  });

  es.onerror = () => {
    document.getElementById("live-dot").style.opacity = "0.3";
    if (activeEs === es) setTimeout(connect, 3000); // only reconnect if still active
  };
}

document.querySelector(".feed-wrap").addEventListener("scroll", e => {
  const el = e.target;
  autoScroll = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
});

// Send message
async function sendMessage() {
  const input = document.getElementById("msg-input");
  const content = input.value.trim();
  if (!content) return;
  input.value = "";
  input.disabled = true;
  try {
    await fetch("/api/message", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({content}),
    });
  } finally {
    input.disabled = false;
    input.focus();
  }
}

document.getElementById("msg-input").addEventListener("keydown", e => {
  if (e.key==="Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
document.getElementById("msg-send").addEventListener("click", sendMessage);

// Load system prompt immediately on page load
fetch("/api/system-prompt").then(r=>r.json()).then(d=>{ spData=d; renderSpCard(d.current_phase); }).catch(()=>{});

connect();
</script>
</body>
</html>"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DeepScholar Web Monitor")
    parser.add_argument("--run-id", type=str, required=True, help="Run ID to monitor")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind to")
    args = parser.parse_args()

    app = create_app(args.run_id)
    print(f"\n  DeepScholar Monitor")
    print(f"  Run: {args.run_id}")
    print(f"  Open: http://{args.host}:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
