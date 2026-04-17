"""
History compressor using a configurable lightweight model (default: glm-4-flash).

Called by ContextManager when the token budget is approaching its limit.
Compresses the middle portion of conversation history into a structured summary,
preserving the head (initial task) and tail (recent turns) verbatim.
"""

from zhipuai import ZhipuAI


COMPRESSION_PROMPT = (
    "以下是一个自主学术研究 Agent 的历史工作记录片段。"
    "请将其压缩为一段结构化摘要，必须保留：\n"
    "- 已检索/阅读的论文（标题、作者、核心结论）\n"
    "- 已完成的实验及其结果（参数、指标、结论）\n"
    "- 重要决策点和当前研究进展\n"
    "- 已生成的 Artifact 文件名及其内容摘要\n\n"
    "格式要精确，信息不能丢失。直接输出摘要，不要加前缀说明。\n\n"
    "历史记录如下：\n\n"
)


def compress_middle(middle_messages: list[dict], client: ZhipuAI, model: str = "glm-4-flash") -> str:
    """Compress a list of messages into a text summary using the given model."""
    middle_text = "\n".join(
        _message_to_text(m) for m in middle_messages
    )

    response = client.chat.completions.create(
        model=model,
        messages=[{
            "role": "user",
            "content": COMPRESSION_PROMPT + middle_text,
        }],
        max_tokens=2000,
    )
    return response.choices[0].message.content


def _message_to_text(message: dict) -> str:
    role = message.get("role", "unknown")
    content = message.get("content", "")

    if isinstance(content, str):
        return f"[{role}]: {content}"
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    parts.append(f"<tool_call name={block.get('name')}> {block.get('input', {})}")
                elif block.get("type") == "tool_result":
                    parts.append(f"<tool_result> {block.get('content', '')[:500]}")
            elif isinstance(block, str):
                parts.append(block)
        return f"[{role}]: " + " | ".join(parts)
    return f"[{role}]: {str(content)[:500]}"
