"""
Three-tier context defense for DeepScholar.

Context window and soft-compress threshold are driven by Config, so they
automatically adjust when the user switches to a different model.

Tier 1 — Workspace offloading: PDF content and large outputs go to files, not messages.
Tier 2 — Soft compression (at 70% of context_window): compress middle history via compress_model.
Tier 3 — Hard compression (on finish_reason == "length"): aggressive middle-out.
"""

from zhipuai import ZhipuAI
from bus.compressor import compress_middle
from config import Config

HEAD_KEEP = 2    # initial task + first assistant turn
TAIL_KEEP = 20   # most recent turns


def _estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: total characters / 2 (conservative for mixed CN/EN text)."""
    total_chars = sum(len(str(m)) for m in messages)
    return total_chars // 2


class ContextManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def maybe_compress(self, messages: list[dict], client: ZhipuAI) -> list[dict]:
        """Compress if approaching the token limit. Returns (possibly compressed) messages."""
        estimated = _estimate_tokens(messages)
        if estimated > self.cfg.compress_threshold:
            print(f"[ContextManager] ~{estimated:,} tokens, triggering soft compression ({self.cfg.compress_model})...")
            return self._compress(messages, client)
        return messages

    def force_compress(self, messages: list[dict], client: ZhipuAI) -> list[dict]:
        """Called when finish_reason == 'length' — aggressive compression."""
        print(f"[ContextManager] Hard compression triggered ({self.cfg.compress_model})...")
        return self._compress(messages, client)

    def _compress(self, messages: list[dict], client: ZhipuAI) -> list[dict]:
        if len(messages) <= HEAD_KEEP + TAIL_KEEP + 1:
            return messages

        head = messages[:HEAD_KEEP]
        tail = messages[-TAIL_KEEP:]
        middle = messages[HEAD_KEEP:-TAIL_KEEP]

        summary_text = compress_middle(middle, client, model=self.cfg.compress_model)

        summary_msg = {
            "role": "user",
            "content": (
                "[系统压缩摘要 — 以下是你之前工作的完整记录摘要，信息已压缩但不丢失]\n\n"
                + summary_text
            ),
        }

        compressed = head + [summary_msg] + tail
        print(
            f"[ContextManager] Compressed: {len(messages)} messages → {len(compressed)} messages"
        )
        return compressed
