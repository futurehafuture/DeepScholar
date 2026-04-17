# DeepScholar 构建笔记

> 记录此次从零实现 DeepScholar 的思路、关键决策和代码结构。

---

## 实现思路

设计文档（`academic_agent_design.md`）已经把架构想清楚了，实现的核心工作是**将 Anthropic SDK 的思路完整替换为智谱 API**，两者在接口格式上有几处关键差异。

---

## 核心差异：Anthropic → 智谱（GLM-4）

| 概念 | Anthropic | 智谱（OpenAI 兼容） |
|------|-----------|-----------------|
| 系统提示 | `client.messages.create(system=...)` | `{"role": "system", "content": "..."}` 作为第一条消息 |
| 工具结果 | `{"role": "user", "content": [{"type": "tool_result", ...}]}` | `{"role": "tool", "content": "...", "tool_call_id": "..."}` |
| 工具参数 | `block.input`（已是 dict） | `tc.function.arguments`（**JSON 字符串**，需 `json.loads()`）|
| 停止原因 | `"tool_use"` / `"end_turn"` / `"max_tokens"` | `"tool_calls"` / `"stop"` / `"length"` |
| 工具 schema | `{"name": ..., "input_schema": ...}` | `{"type": "function", "function": {"name": ..., "parameters": ...}}` |
| 上下文窗口 | 200K tokens | 128K tokens（软压缩阈值调整为 90K） |

---

## 代码的数据流

```
用户输入 research topic
        ↓
main.py 解析参数，启动 MCPClient，调用 run_agent_loop()
        ↓
agent/loop.py — while True 主循环:
  1. state_machine.get_current_system_prompt()  ← base_system.md + phase_0X.md
  2. mcp.get_tools_for_phase(current_phase)      ← 按阶段过滤工具
  3. ctx_manager.maybe_compress(messages)        ← 超 90K tokens 时压缩
  4. client.chat.completions.create(...)         ← 调用 GLM-4-Plus
  5. bus.append(assistant_msg)                   ← 立刻落盘 history.jsonl
  6. 分发工具调用:
     - transition_to_phase → state_machine.transition()  ← 内部工具
     - 其他工具 → mcp.call_tool()                        ← 转发给 MCP server
  7. bus.append(tool_result)                     ← 结果也立刻落盘
  8. 回到步骤 1
```

---

## 三层能力架构（最容易混淆的部分）

```
Skills 层（agent/prompts/phase_*.md）
  ↓ 定义"这个阶段能用哪些工具、扮演什么角色"
Tools 层（mcp_servers/client.py PHASE_TOOL_PERMISSIONS）
  ↓ 暴露给 LLM 的函数接口，有些来自 MCP server，有些是内部实现
MCP 层（mcp_servers/client.py MCPClient）
  ↓ 管理外部进程（arxiv server、filesystem server 等）的 stdio 连接
```

**关键设计**：`transition_to_phase` 是唯一的内部工具，不经过 MCP，直接调用 `state_machine.transition()`。其余工具全部通过 MCP 协议转发。

---

## 崩溃恢复的实现

`bus/jsonl_bus.py` 只有一个核心约束：**只追加，永不修改**。

```
进程在实验阶段崩溃
        ↓
history.jsonl 保留了所有消息（每条消息在产生时立即 flush）
state.json 保留了当前阶段（每次 transition 后立即写入）
        ↓
uv run python main.py --resume --run-id xxx
        ↓
bus.load_all() 读回完整历史
state_machine._load_or_init() 读回当前阶段
        ↓
Agent 从中断点继续，无任何信息丢失
```

---

## 上下文管理的三道防线

```
Tier 1 — 工作区卸载（始终有效）
  → 论文 PDF 内容写文件，不放进 messages
  → execute_python 输出超 3000 字符自动截断，末尾附 read_file 提示

Tier 2 — 软压缩（超过 90K tokens 触发）
  → 保留头 2 条（初始任务）+ 尾 20 条（近期记忆）
  → 中间部分用 glm-4-flash 压缩成结构化摘要（节省成本）

Tier 3 — 强制压缩（finish_reason == "length" 触发）
  → 同上策略，但在 max_tokens 被打满时强制触发
```

---

## 文件结构速查

```
DeepScholar/
├── main.py                    # 入口：CLI 解析 + 异步启动
├── agent/
│   ├── loop.py                # 主循环（核心逻辑全在这里）
│   ├── state_machine.py       # 7 阶段状态机 + transition 工具 schema 定义
│   ├── context_manager.py     # Token 估算 + 三层压缩调度
│   └── prompts/
│       ├── base_system.md     # 通用 Agent 人设（所有阶段共用）
│       ├── phase_01_survey.md
│       ├── phase_02_literature.md
│       ├── phase_03_arguments.md
│       ├── phase_04_innovation.md
│       ├── phase_05_experiment.md
│       ├── phase_06_analysis.md
│       └── phase_07_writing.md
├── bus/
│   ├── jsonl_bus.py           # Append-only 持久化（只追加，永不修改）
│   └── compressor.py          # 历史压缩（调用 glm-4-flash）
├── mcp_servers/
│   ├── client.py              # MCP 连接管理 + 工具发现 + 阶段权限过滤
│   └── servers.yaml           # MCP server 配置（增删 server 只改这里）
└── pyproject.toml             # 依赖：zhipuai, mcp, pymupdf, rich, ...
```

---

## 几个实现细节的决策

**为什么用 `mcp` 库而不是自己写 JSON-RPC？**
设计文档里的 `readline()` 做法在真实 MCP server 上不工作——MCP 协议用的是 Content-Length 帧头（类似 LSP），不是换行分隔。`mcp` 库封装了正确的 stdio transport。

**为什么 Token 计数用字符估算而不是 tiktoken？**
`tiktoken` 是 OpenAI 的分词器，对 GLM-4 不准确。中英混合文本用 `chars // 2` 做保守估算足够触发压缩，精确性不是关键。

**为什么 `PHASE_TOOL_PERMISSIONS` 是运行时过滤而不是静态配置？**
MCP server 在启动时动态暴露它们的工具列表。权限过滤在工具发现之后进行，允许添加新 server 而不需要改权限表——新工具默认对所有阶段不可见，直到显式加入白名单。

---

## 启动步骤

```bash
cp .env.example .env          # 填入 ZHIPUAI_API_KEY
uv sync                        # 安装依赖
# 确保 Node.js 已安装（filesystem MCP server 需要 npx）
uv run python main.py --topic "你的研究方向"
```
