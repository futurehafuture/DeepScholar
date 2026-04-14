# 学术 Agent 完整实现方案

> 目标：构建一个真正自主驱动的学术研究 Agent，覆盖从选题调研到论文撰写的完整学术流程。
> 架构哲学：原生 while 循环 + JSONL 消息总线 + MCP 工具链，彻底抛弃图编排框架。

---

## 目录

1. [整体架构概览](#1-整体架构概览)
2. [项目结构](#2-项目结构)
3. [核心模块详解](#3-核心模块详解)
   - 3.1 [主循环引擎](#31-主循环引擎-agent-repl)
   - 3.2 [JSONL 消息总线](#32-jsonl-消息总线)
   - 3.3 [上下文管理器](#33-上下文管理器)
   - 3.4 [阶段状态机](#34-阶段状态机)
   - 3.5 [MCP 工具层](#35-mcp-工具层)
4. [学术流程各阶段设计](#4-学术流程各阶段设计)
   - 4.1 [阶段一：选题与文献调研](#41-阶段一选题与文献调研)
   - 4.2 [阶段二：文献精读与整理](#42-阶段二文献精读与整理)
   - 4.3 [阶段三：核心论点提炼](#43-阶段三核心论点提炼)
   - 4.4 [阶段四：理论创新与实验设计](#44-阶段四理论创新与实验设计)
   - 4.5 [阶段五：实验执行](#45-阶段五实验执行)
   - 4.6 [阶段六：结论分析](#46-阶段六结论分析)
   - 4.7 [阶段七：论文撰写](#47-阶段七论文撰写)
5. [System Prompt 设计](#5-system-prompt-设计)
6. [MCP Server 清单与接入方式](#6-mcp-server-清单与接入方式)
7. [上下文危机处理策略](#7-上下文危机处理策略)
8. [数据持久化与实验记录](#8-数据持久化与实验记录)
9. [启动与恢复流程](#9-启动与恢复流程)
10. [技术选型与依赖](#10-技术选型与依赖)
11. [开发路线图](#11-开发路线图)

---

## 1. 整体架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                     Academic Agent                          │
│                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │  主循环引擎   │───▶│  状态机/阶段  │───▶│  System Prompt│  │
│  │  (REPL Loop) │    │  管理器      │    │  动态替换     │  │
│  └──────┬───────┘    └──────────────┘    └──────────────┘  │
│         │                                                   │
│  ┌──────▼───────┐    ┌──────────────┐    ┌──────────────┐  │
│  │  JSONL 消息  │    │  上下文管理器 │    │  MCP Client  │  │
│  │  总线        │    │  (压缩/截断)  │    │  工具动态加载  │  │
│  └──────────────┘    └──────────────┘    └──────┬───────┘  │
└─────────────────────────────────────────────────┼───────────┘
                                                  │ MCP Protocol
              ┌───────────────────────────────────┤
              │                                   │
    ┌─────────▼──────┐  ┌──────────────┐  ┌──────▼────────┐
    │  Arxiv MCP     │  │  FileSystem  │  │  Python       │
    │  Server        │  │  MCP Server  │  │  Interpreter  │
    │  (搜索/下载论文)│  │  (读写文件)  │  │  MCP Server  │
    └────────────────┘  └──────────────┘  └───────────────┘
```

**核心数据流**：

```
用户输入研究课题
    │
    ▼
Agent 主循环启动
    │
    ▼
LLM 生成思考 + 工具调用
    │
    ├──▶ 执行 MCP 工具（搜论文/跑代码/读文件）
    │         │
    │         ▼
    │    工具结果追加到 JSONL
    │
    ├──▶ 检测阶段切换信号
    │         │
    │         ▼
    │    替换 System Prompt + 工具列表
    │
    └──▶ 上下文超限检测
              │
              ▼
         压缩/折叠历史，继续循环
```

---

## 2. 项目结构

```
academic-agent/
│
├── main.py                    # 入口：解析参数、加载历史、启动主循环
├── agent/
│   ├── __init__.py
│   ├── loop.py                # 核心 REPL 循环
│   ├── state_machine.py       # 阶段状态机 + transition_to_phase
│   ├── context_manager.py     # Token 计数、压缩、滑动窗口
│   └── prompts/
│       ├── base_system.md     # 通用学术 Agent 人设
│       ├── phase_01_survey.md         # 阶段一：选题调研
│       ├── phase_02_literature.md     # 阶段二：文献精读
│       ├── phase_03_arguments.md      # 阶段三：论点提炼
│       ├── phase_04_innovation.md     # 阶段四：理论创新
│       ├── phase_05_experiment.md     # 阶段五：实验执行
│       ├── phase_06_analysis.md       # 阶段六：结论分析
│       └── phase_07_writing.md        # 阶段七：论文撰写
│
├── mcp/
│   ├── client.py              # MCP Client：发现工具、执行工具
│   ├── servers.yaml           # MCP Server 配置清单
│   └── tool_registry.py      # 运行时工具注册表
│
├── bus/
│   ├── jsonl_bus.py           # JSONL 消息总线读写
│   └── compressor.py         # 历史压缩（调用轻量模型）
│
├── workspace/                 # Agent 的工作区（落盘的所有产出）
│   ├── runs/
│   │   └── {run_id}/
│   │       ├── history.jsonl  # 本次 Run 的完整消息历史
│   │       ├── state.json     # 当前阶段、元数据
│   │       └── artifacts/     # 各阶段产出文件
│   │           ├── literature_review.md
│   │           ├── core_arguments.md
│   │           ├── experiment_log.md
│   │           ├── results.md
│   │           └── paper_draft.tex
│   └── papers/               # 下载的 PDF 缓存
│
├── pyproject.toml             # uv 项目配置
└── .env                       # API Key 等环境变量
```

---

## 3. 核心模块详解

### 3.1 主循环引擎（Agent REPL）

主循环是整个 Agent 的心脏。逻辑极其简洁：**调用 LLM → 追加历史 → 执行工具 → 重复**。

```python
# agent/loop.py

import anthropic
from agent.state_machine import StateMachine
from agent.context_manager import ContextManager
from bus.jsonl_bus import JSONLBus
from mcp.client import MCPClient

def run_agent_loop(run_id: str, initial_topic: str = None):
    """
    Agent 主循环。如果 run_id 对应的 history.jsonl 已存在，则恢复继续。
    否则，使用 initial_topic 创建全新的 Run。
    """
    client = anthropic.Anthropic()
    bus = JSONLBus(run_id)
    mcp = MCPClient()
    state_machine = StateMachine(run_id)
    ctx_manager = ContextManager(max_tokens=150_000)

    # 1. 恢复历史 or 初始化
    if bus.exists():
        messages = bus.load_all()
        print(f"[Resume] 从 {len(messages)} 条历史记录恢复运行...")
    else:
        messages = []
        # 将用户的研究课题作为第一条消息
        first_message = {"role": "user", "content": f"开始研究课题：{initial_topic}"}
        messages.append(first_message)
        bus.append(first_message)

    # 2. 主循环
    while True:
        # 获取当前阶段的 System Prompt 和可用工具
        system_prompt = state_machine.get_current_system_prompt()
        available_tools = mcp.get_tools_for_phase(state_machine.current_phase)

        # 上下文管理：若超限则压缩
        messages = ctx_manager.maybe_compress(messages, client)

        # 调用 LLM
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=8096,
            system=system_prompt,
            messages=messages,
            tools=available_tools,
        )

        # 将 LLM 回复追加到总线
        assistant_msg = {"role": "assistant", "content": response.content}
        messages.append(assistant_msg)
        bus.append(assistant_msg)

        # 处理停止原因
        if response.stop_reason == "end_turn":
            # Agent 认为当前阶段任务完成，等待确认
            print("\n[Agent] 已完成当前思考，等待指令...")
            user_input = input(">>> ")
            user_msg = {"role": "user", "content": user_input}
            messages.append(user_msg)
            bus.append(user_msg)

        elif response.stop_reason == "tool_use":
            # 执行所有工具调用
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                tool_name = block.name
                tool_input = block.input

                # 特殊处理：阶段切换工具
                if tool_name == "transition_to_phase":
                    result = state_machine.transition(tool_input["target_phase"])
                    print(f"\n[状态机] 切换至阶段：{tool_input['target_phase']}")
                else:
                    # 调用 MCP 工具
                    result = mcp.call_tool(tool_name, tool_input)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result),
                })

            # 将工具结果作为 user 消息返回给 LLM
            tool_result_msg = {"role": "user", "content": tool_results}
            messages.append(tool_result_msg)
            bus.append(tool_result_msg)

        elif response.stop_reason == "max_tokens":
            # 强制压缩上下文后继续
            print("[警告] 触达 max_tokens，强制压缩上下文...")
            messages = ctx_manager.force_compress(messages, client)
```

---

### 3.2 JSONL 消息总线

JSONL 总线是 Agent 的"大脑记忆"。每条消息独立成行，崩溃后可完整恢复。

```python
# bus/jsonl_bus.py

import json
import os
from pathlib import Path

class JSONLBus:
    def __init__(self, run_id: str):
        self.path = Path(f"workspace/runs/{run_id}/history.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def exists(self) -> bool:
        return self.path.exists() and self.path.stat().st_size > 0

    def append(self, message: dict):
        """追加一条消息到 JSONL 文件（Append-Only）"""
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")

    def load_all(self) -> list[dict]:
        """从文件加载完整历史"""
        messages = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    messages.append(json.loads(line))
        return messages
```

**设计原则**：
- **只追加，永不修改**：历史记录是不可变的事实日志
- **每条消息立即落盘**：LLM 回复后立刻写入，工具结果后立刻写入
- **人类可读**：每行是一个完整的 JSON 对象，可以用 `jq` 直接查看

---

### 3.3 上下文管理器

学术 Agent 的最大挑战：读取数十篇论文后，消息历史会轻易突破 200K tokens。

```python
# agent/context_manager.py

import anthropic
import tiktoken  # 或用 claude 自带的 token 计数

class ContextManager:
    def __init__(self, max_tokens: int = 150_000, compress_threshold: float = 0.7):
        self.max_tokens = max_tokens
        # 达到 70% 时触发压缩
        self.compress_threshold = int(max_tokens * compress_threshold)

    def count_tokens(self, messages: list) -> int:
        """估算消息列表的 token 数量"""
        # 简单实现：用字符数 / 4 估算（生产环境用精确计数）
        total_chars = sum(len(str(m)) for m in messages)
        return total_chars // 4

    def maybe_compress(self, messages: list, client) -> list:
        """如果接近上限，压缩中间历史"""
        token_count = self.count_tokens(messages)
        if token_count > self.compress_threshold:
            print(f"[ContextManager] 当前 ~{token_count} tokens，触发压缩...")
            return self._compress(messages, client)
        return messages

    def force_compress(self, messages: list, client) -> list:
        """强制压缩"""
        return self._compress(messages, client)

    def _compress(self, messages: list, client) -> list:
        """
        压缩策略：
        - 保留最前面 2 条（初始研究课题 + 第一个回复）
        - 保留最后 20 条（近期记忆）
        - 中间部分交给轻量模型压缩为摘要
        """
        if len(messages) <= 22:
            return messages

        head = messages[:2]
        tail = messages[-20:]
        middle = messages[2:-20]

        # 用轻量模型压缩中间历史
        middle_text = "\n".join(str(m) for m in middle)
        summary_response = client.messages.create(
            model="claude-haiku-4-5-20251001",  # 用便宜的模型做压缩
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": (
                    "以下是一个学术研究 Agent 的历史工作记录。"
                    "请将其压缩为一段结构化摘要，保留所有关键发现、已查阅的论文、"
                    "已完成的实验、重要结论和当前进展。格式要精确，信息不能丢失：\n\n"
                    f"{middle_text}"
                )
            }]
        )
        summary = summary_response.content[0].text

        # 用摘要替换中间历史
        summary_msg = {
            "role": "user",
            "content": f"[系统压缩摘要 - 以下是之前工作的压缩记录]\n{summary}"
        }

        compressed = head + [summary_msg] + tail
        print(f"[ContextManager] 压缩完成：{len(messages)} 条 → {len(compressed)} 条")
        return compressed
```

**三层防线**：

| 层次 | 触发条件 | 策略 |
|------|---------|------|
| 工作区挂载 | 始终 | 论文内容写入本地文件，用 `read_file` 按需读取 |
| 软压缩 | 达到 70% 上限 | 轻量模型压缩中间历史 |
| 强制压缩 | 达到 max_tokens | 激进压缩，只保留头尾 |

---

### 3.4 阶段状态机

学术研究的阶段流转不由外部图引擎驱动，而是由 **Agent 自己决定何时切换**。

```python
# agent/state_machine.py

import json
from pathlib import Path
from enum import Enum

class Phase(str, Enum):
    SURVEY       = "survey"           # 选题与文献调研
    LITERATURE   = "literature"       # 文献精读与整理
    ARGUMENTS    = "arguments"        # 核心论点提炼
    INNOVATION   = "innovation"       # 理论创新与实验设计
    EXPERIMENT   = "experiment"       # 实验执行
    ANALYSIS     = "analysis"         # 结论分析
    WRITING      = "writing"          # 论文撰写

# 阶段的线性顺序（默认流转路径）
PHASE_ORDER = [
    Phase.SURVEY, Phase.LITERATURE, Phase.ARGUMENTS,
    Phase.INNOVATION, Phase.EXPERIMENT, Phase.ANALYSIS, Phase.WRITING
]

class StateMachine:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.state_path = Path(f"workspace/runs/{run_id}/state.json")
        self._load_or_init()

    def _load_or_init(self):
        if self.state_path.exists():
            with open(self.state_path) as f:
                state = json.load(f)
            self.current_phase = Phase(state["current_phase"])
            self.metadata = state.get("metadata", {})
        else:
            self.current_phase = Phase.SURVEY
            self.metadata = {}
            self._save()

    def _save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump({
                "current_phase": self.current_phase.value,
                "metadata": self.metadata
            }, f, ensure_ascii=False, indent=2)

    def get_current_system_prompt(self) -> str:
        """读取当前阶段对应的 System Prompt 文件"""
        prompt_path = Path(f"agent/prompts/phase_{self.current_phase.value}.md")
        base_path = Path("agent/prompts/base_system.md")

        base = base_path.read_text(encoding="utf-8")
        phase_prompt = prompt_path.read_text(encoding="utf-8")
        return f"{base}\n\n---\n\n{phase_prompt}"

    def transition(self, target_phase: str) -> str:
        """执行阶段切换（由 Agent 主动调用）"""
        try:
            new_phase = Phase(target_phase)
        except ValueError:
            return f"错误：未知阶段 '{target_phase}'"

        old_phase = self.current_phase
        self.current_phase = new_phase
        self._save()
        return f"已从 [{old_phase.value}] 切换到 [{new_phase.value}]"

    def get_available_phases(self) -> list[str]:
        return [p.value for p in Phase]
```

**`transition_to_phase` 工具的 Tool Schema**（喂给 LLM）：

```json
{
  "name": "transition_to_phase",
  "description": "当你认为当前阶段的工作已经充分完成，主动调用此工具切换到下一个研究阶段。切换后你的角色和可用工具会随之改变。",
  "input_schema": {
    "type": "object",
    "properties": {
      "target_phase": {
        "type": "string",
        "enum": ["survey", "literature", "arguments", "innovation", "experiment", "analysis", "writing"],
        "description": "目标阶段名称"
      },
      "reason": {
        "type": "string",
        "description": "切换原因：当前阶段完成了什么，为什么现在适合进入下一阶段"
      }
    },
    "required": ["target_phase", "reason"]
  }
}
```

---

### 3.5 MCP 工具层

MCP Client 负责：启动时发现所有工具、按阶段过滤、执行工具调用。

```python
# mcp/client.py

import subprocess
import json
from pathlib import Path
import yaml

# 哪些阶段可以用哪些工具
PHASE_TOOL_PERMISSIONS = {
    "survey":     ["arxiv_search", "web_search", "read_file", "write_file", "transition_to_phase"],
    "literature": ["arxiv_search", "download_paper", "read_pdf", "read_file", "write_file", "transition_to_phase"],
    "arguments":  ["read_file", "write_file", "transition_to_phase"],
    "innovation": ["read_file", "write_file", "transition_to_phase"],
    "experiment": ["execute_python", "read_file", "write_file", "list_directory", "transition_to_phase"],
    "analysis":   ["execute_python", "read_file", "write_file", "transition_to_phase"],
    "writing":    ["read_file", "write_file", "read_latex", "compile_latex", "transition_to_phase"],
}

class MCPClient:
    def __init__(self, config_path: str = "mcp/servers.yaml"):
        self.all_tools = {}   # tool_name -> tool_schema
        self.servers = {}     # server_name -> process
        self._load_config(config_path)
        self._discover_tools()

    def _load_config(self, config_path: str):
        with open(config_path) as f:
            self.config = yaml.safe_load(f)

    def _discover_tools(self):
        """向所有 MCP Server 发送 tools/list 请求，动态加载工具"""
        for server_name, server_config in self.config["servers"].items():
            # 启动 MCP Server 进程
            proc = subprocess.Popen(
                server_config["command"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.servers[server_name] = proc

            # 发送 tools/list 请求（MCP 协议）
            request = {
                "jsonrpc": "2.0",
                "method": "tools/list",
                "id": 1,
                "params": {}
            }
            proc.stdin.write(json.dumps(request).encode() + b"\n")
            proc.stdin.flush()

            response = json.loads(proc.stdout.readline())
            for tool in response.get("result", {}).get("tools", []):
                self.all_tools[tool["name"]] = {
                    "schema": tool,
                    "server": server_name
                }

        print(f"[MCP] 已发现 {len(self.all_tools)} 个工具：{list(self.all_tools.keys())}")

    def get_tools_for_phase(self, phase: str) -> list[dict]:
        """获取当前阶段被授权的工具列表（Anthropic tool_use 格式）"""
        allowed = PHASE_TOOL_PERMISSIONS.get(phase, [])
        tools = []

        # 始终加入阶段切换工具
        tools.append(self._transition_tool_schema())

        for tool_name in allowed:
            if tool_name in self.all_tools and tool_name != "transition_to_phase":
                schema = self.all_tools[tool_name]["schema"]
                tools.append({
                    "name": schema["name"],
                    "description": schema["description"],
                    "input_schema": schema["inputSchema"],
                })

        return tools

    def call_tool(self, tool_name: str, tool_input: dict) -> str:
        """通过 MCP 协议调用工具"""
        if tool_name not in self.all_tools:
            return f"错误：工具 '{tool_name}' 不存在"

        server_name = self.all_tools[tool_name]["server"]
        proc = self.servers[server_name]

        request = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 2,
            "params": {
                "name": tool_name,
                "arguments": tool_input
            }
        }
        proc.stdin.write(json.dumps(request).encode() + b"\n")
        proc.stdin.flush()

        response = json.loads(proc.stdout.readline())
        result = response.get("result", {}).get("content", [])

        # 提取文本内容
        texts = [item["text"] for item in result if item.get("type") == "text"]
        return "\n".join(texts)

    def _transition_tool_schema(self) -> dict:
        return {
            "name": "transition_to_phase",
            "description": "当你认为当前阶段工作已充分完成时，主动切换到下一研究阶段。切换后你的角色和可用工具会随之改变。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "target_phase": {
                        "type": "string",
                        "enum": ["survey", "literature", "arguments", "innovation", "experiment", "analysis", "writing"]
                    },
                    "reason": {"type": "string"}
                },
                "required": ["target_phase", "reason"]
            }
        }
```

---

## 4. 学术流程各阶段设计

### 4.1 阶段一：选题与文献调研（Survey）

**Agent 的任务**：
- 理解用户输入的研究方向或关键词
- 在 Arxiv、Semantic Scholar 搜索相关领域的最新论文
- 识别领域的核心问题、当前 SOTA 方法、存在的空白
- 输出：`workspace/runs/{id}/artifacts/survey_overview.md`

**可用工具**：`arxiv_search`、`web_search`、`write_file`

**切换条件**：Agent 认为已了解领域全貌，收集到 20~50 篇相关文献，可主动调用 `transition_to_phase("literature")`

---

### 4.2 阶段二：文献精读与整理（Literature）

**Agent 的任务**：
- 下载并精读阶段一筛选出的核心论文（通常 10~20 篇）
- 对每篇论文提取：研究问题、方法、实验、结论、局限性
- 构建论文知识图谱（写入 Markdown 文件）
- 输出：`artifacts/literature_review.md`（按主题组织的文献综述）

**重要原则**：**不要把整篇 PDF 塞入对话**。Agent 将论文内容写入本地文件，之后通过 `read_file` 工具按需查阅。

**可用工具**：`arxiv_search`、`download_paper`、`read_pdf`、`read_file`、`write_file`

---

### 4.3 阶段三：核心论点提炼（Arguments）

**Agent 的任务**：
- 通读 `literature_review.md`
- 提炼领域内的核心争议点、未解问题、方法论缺陷
- 提出本研究的核心论点（Thesis）和研究问题（Research Questions）
- 输出：`artifacts/core_arguments.md`

**可用工具**：`read_file`、`write_file`

---

### 4.4 阶段四：理论创新与实验设计（Innovation）

**Agent 的任务**：
- 基于核心论点，提出理论创新点（假设）
- 设计验证假设的实验方案（数据集、Baseline、评估指标）
- 输出：`artifacts/experiment_design.md`

**可用工具**：`read_file`、`write_file`

---

### 4.5 阶段五：实验执行（Experiment）

**Agent 的任务**：
- 按照实验设计，编写并执行 Python 代码
- 迭代调试：运行 → 观察结果 → 修改代码 → 再次运行
- 记录每次实验的参数、结果、异常
- 输出：`artifacts/experiment_log.md` + 实验代码文件

**可用工具**：`execute_python`、`read_file`、`write_file`、`list_directory`

**关键能力**：自主调试。当代码报错时，Agent 分析错误信息，修改代码，重新执行，无需人工干预。

---

### 4.6 阶段六：结论分析（Analysis）

**Agent 的任务**：
- 读取实验日志，统计和可视化结果
- 验证最初的假设是否成立
- 分析失败实验的原因
- 输出：`artifacts/results.md`（含图表描述和数值结果）

**可用工具**：`execute_python`（生成图表）、`read_file`、`write_file`

---

### 4.7 阶段七：论文撰写（Writing）

**Agent 的任务**：
- 按照学术论文结构撰写全文：Abstract → Introduction → Related Work → Method → Experiments → Conclusion
- 每个章节从对应的产出文件中提取内容
- 输出：`artifacts/paper_draft.tex`（LaTeX 格式）

**可用工具**：`read_file`、`write_file`、`compile_latex`

---

## 5. System Prompt 设计

### 基础 System Prompt（所有阶段共用）

```markdown
# 你是一个自主学术研究 Agent

## 核心身份
你是一位具有完整科研能力的自主学术研究助手。你的任务是独立完成从选题到论文撰写的完整学术研究流程。

## 工作原则
1. **主动驱动**：不要等待人类指令，在每个阶段主动规划下一步行动并执行
2. **工作区优先**：所有中间产出必须写入工作区文件（workspace/runs/{run_id}/artifacts/），不要只存在于对话中
3. **随时切换**：当你认为一个阶段的任务已充分完成，主动调用 `transition_to_phase` 工具进入下一阶段
4. **失败即调试**：遇到工具报错或实验失败，分析原因，修正策略，继续推进，不要放弃
5. **思维可见**：每次行动前，用 1-2 句话说明你的思考和意图

## 工作区结构
- 文献综述：`artifacts/literature_review.md`
- 核心论点：`artifacts/core_arguments.md`
- 实验设计：`artifacts/experiment_design.md`
- 实验日志：`artifacts/experiment_log.md`
- 结果分析：`artifacts/results.md`
- 论文草稿：`artifacts/paper_draft.tex`
```

### 阶段专属 System Prompt 示例（实验阶段）

```markdown
# 当前阶段：实验执行（Experiment Phase）

## 你现在的角色
你是一名经验丰富的算法工程师和实验科学家。你的目标是通过编写和执行 Python 代码，验证在实验设计阶段提出的假设。

## 工作流程
1. 先用 `read_file` 读取 `artifacts/experiment_design.md`，了解实验方案
2. 编写实验代码，用 `execute_python` 执行
3. 将每次实验的参数、结果、报错信息追加写入 `artifacts/experiment_log.md`
4. 持续迭代，直到获得稳定、可解释的实验结果
5. 全部实验完成后，调用 `transition_to_phase("analysis")`

## 调试原则
- 遇到 ImportError：先检查依赖，用 `!pip install` 安装
- 遇到数据错误：检查数据格式，输出样本查看
- 遇到结果异常：对比实验设计，检查超参数
- 实验失败也是结果，记录下来

## 严格禁止
- 不得跳过实验直接捏造结果
- 每次代码修改必须重新执行验证
```

---

## 6. MCP Server 清单与接入方式

### servers.yaml 配置示例

```yaml
servers:
  # 文献检索
  arxiv:
    command: ["uvx", "arxiv-mcp-server"]
    description: "Arxiv 论文搜索与下载"
    tools: ["arxiv_search", "download_paper", "read_pdf"]

  # 文件系统操作
  filesystem:
    command: ["npx", "@modelcontextprotocol/server-filesystem", "./workspace"]
    description: "本地文件读写"
    tools: ["read_file", "write_file", "list_directory", "create_directory"]

  # Python 代码执行
  python:
    command: ["uvx", "mcp-server-python-repl"]
    description: "执行 Python 代码，支持数据分析和可视化"
    tools: ["execute_python"]

  # 网络搜索（补充 Arxiv 之外的信息）
  brave_search:
    command: ["npx", "@modelcontextprotocol/server-brave-search"]
    description: "网页搜索"
    tools: ["web_search"]
    env:
      BRAVE_API_KEY: "${BRAVE_API_KEY}"
```

### 推荐的社区 MCP Server

| 工具需求 | 推荐 MCP Server | 安装方式 |
|---------|----------------|---------|
| Arxiv 论文搜索 | `arxiv-mcp-server` | `uvx arxiv-mcp-server` |
| 本地文件读写 | `@modelcontextprotocol/server-filesystem` | `npx` |
| Python 执行 | `mcp-server-python-repl` | `uvx` |
| 网络搜索 | `@modelcontextprotocol/server-brave-search` | `npx` |
| LaTeX 编译 | 自定义 MCP Server | 见下文 |

---

## 7. 上下文危机处理策略

### Token 预算分配（以 200K context 为例）

```
┌─────────────────────────────────────────────────┐
│  System Prompt          ~  5,000 tokens  (2.5%) │
│  工具 Schema            ~  3,000 tokens  (1.5%) │
│  核心记忆（头部）         ~  5,000 tokens  (2.5%) │
│  近期对话（尾部20条）     ~ 30,000 tokens   (15%) │
│  压缩摘要               ~ 10,000 tokens    (5%)  │
│  ████████ 安全缓冲区 ████████                    │
│  max_tokens 输出         ~  8,000 tokens    (4%) │
└─────────────────────────────────────────────────┘
     软压缩阈值：140,000 tokens（70%）
```

### 防止上下文爆炸的最佳实践

1. **PDF 解析外置**：用 `PyMuPDF` 将 PDF 提取为 Markdown，写入本地文件，不直接塞入对话
2. **代码执行结果截断**：`execute_python` 的输出超过 2000 字符时自动截断，末尾加 `...（已截断，完整结果见 experiment_log.md）`
3. **渐进式工作区**：Agent 在每个阶段结束时，将本阶段的关键结论写入固定的 Artifact 文件，作为后续阶段的输入

---

## 8. 数据持久化与实验记录

### Run 目录结构（完整版）

```
workspace/runs/{run_id}/
├── history.jsonl          # 完整消息历史（Append-Only）
├── state.json             # 当前阶段 + 元数据
├── artifacts/
│   ├── survey_overview.md         # 领域调研报告
│   ├── literature_review.md       # 文献综述
│   ├── core_arguments.md          # 核心论点
│   ├── experiment_design.md       # 实验设计
│   ├── experiment_log.md          # 实验日志（每次实验追加）
│   ├── results.md                 # 结论分析
│   ├── paper_draft.tex            # 论文草稿
│   └── figures/                   # 实验图表
│       ├── fig1_baseline.png
│       └── fig2_ablation.png
└── code/                          # 实验代码
    ├── experiment_v1.py
    ├── experiment_v2.py
    └── utils.py
```

### history.jsonl 示例（真实格式）

```jsonl
{"role": "user", "content": "开始研究课题：基于图神经网络的药物分子性质预测"}
{"role": "assistant", "content": [{"type": "text", "text": "好的，我将开始对这个课题进行系统调研..."}, {"type": "tool_use", "id": "tu_001", "name": "arxiv_search", "input": {"query": "graph neural network drug property prediction", "max_results": 20}}]}
{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_001", "content": "找到 20 篇相关论文：1. AttentiveFP... 2. MPNN..."}]}
{"role": "assistant", "content": [{"type": "text", "text": "找到了一些核心论文，我将重点阅读 AttentiveFP 和 MPNN..."}, {"type": "tool_use", "id": "tu_002", "name": "write_file", "input": {"path": "artifacts/survey_overview.md", "content": "# 领域调研报告\n..."}}]}
```

---

## 9. 启动与恢复流程

### 启动新 Run

```bash
# 使用 uv 运行
uv run python main.py --topic "基于图神经网络的药物分子性质预测"

# 或者指定 run_id（用于命名）
uv run python main.py --topic "GNN Drug Property Prediction" --run-id "gnn_drug_20240414"
```

### 恢复中断的 Run

```bash
# 直接传入已有的 run_id，Agent 自动从 history.jsonl 恢复
uv run python main.py --resume --run-id "gnn_drug_20240414"
```

### main.py 入口

```python
# main.py

import argparse
import datetime
from agent.loop import run_agent_loop

def main():
    parser = argparse.ArgumentParser(description="Academic Research Agent")
    parser.add_argument("--topic", type=str, help="研究课题（新 Run 必填）")
    parser.add_argument("--run-id", type=str, help="Run ID（恢复时必填）")
    parser.add_argument("--resume", action="store_true", help="恢复已有 Run")
    args = parser.parse_args()

    if args.resume:
        if not args.run_id:
            print("错误：恢复模式必须指定 --run-id")
            return
        run_agent_loop(run_id=args.run_id)
    else:
        if not args.topic:
            print("错误：新 Run 必须指定 --topic")
            return
        # 生成唯一 Run ID
        run_id = args.run_id or f"run_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        run_agent_loop(run_id=run_id, initial_topic=args.topic)

if __name__ == "__main__":
    main()
```

---

## 10. 技术选型与依赖

### pyproject.toml

```toml
[project]
name = "academic-agent"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "anthropic>=0.40.0",     # Claude API SDK
    "pyyaml>=6.0",           # MCP Server 配置解析
    "pymupdf>=1.24.0",       # PDF 解析（fitz）
    "tiktoken>=0.7.0",       # Token 计数
    "python-dotenv>=1.0.0",  # 环境变量管理
    "rich>=13.0.0",          # 终端美化输出
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

### 关键技术选型理由

| 选型 | 选择 | 理由 |
|------|------|------|
| LLM SDK | `anthropic` 官方 SDK | 原生 tool_use 支持，无额外封装 |
| 任务编排 | 纯 Python `while` 循环 | 最大灵活性，无框架约束 |
| 持久化 | JSONL 文件 | 人类可读，Append-Only，崩溃安全 |
| 工具层 | MCP 协议 | 工具与 Agent 解耦，可热插拔 |
| 包管理 | `uv` | 极速依赖解析，环境隔离 |
| 模型策略 | 主力 claude-opus-4-6 + 压缩 claude-haiku-4-5 | 质量与成本平衡 |

---

## 11. 开发路线图

### Phase 0：基础设施（第 1-2 天）
- [ ] `uv init academic-agent`，搭建项目结构
- [ ] 实现 `JSONLBus`（读写/恢复）
- [ ] 实现最小主循环（能调用一个 echo 工具并落盘）
- [ ] **验收**：kill 进程后重启，Agent 从历史恢复并继续

### Phase 1：工具接入（第 3-5 天）
- [ ] 实现 `MCPClient`（发现 + 调用工具）
- [ ] 接入 FileSystem MCP Server（`read_file` / `write_file`）
- [ ] 接入 Python Interpreter MCP Server
- [ ] **验收**：Agent 能写一个文件，执行 Python，结果写回文件

### Phase 2：学术能力（第 6-8 天）
- [ ] 接入 Arxiv MCP Server
- [ ] 实现 `StateMachine`（阶段切换）
- [ ] 编写各阶段 System Prompt
- [ ] **验收**：给一个课题，Agent 能自动完成文献调研并生成 `literature_review.md`

### Phase 3：上下文管理（第 9-10 天）
- [ ] 实现 `ContextManager`（Token 计数 + 软/硬压缩）
- [ ] 实现 PDF 工作区挂载（不塞入对话）
- [ ] **验收**：读取 20 篇论文后，上下文不爆炸

### Phase 4：端到端打通（第 11-14 天）
- [ ] 跑通完整的 7 阶段流程
- [ ] 优化各阶段 System Prompt
- [ ] 加入人机交互点（关键决策时询问用户）
- [ ] **验收**：给一个具体研究课题，产出一篇完整论文草稿

---

*文档版本：v1.0 | 创建日期：2026-04-14*
