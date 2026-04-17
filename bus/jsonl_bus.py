"""
Append-only JSONL message bus.

Every LLM response, tool call, and tool result is immediately flushed to disk.
This makes the agent crash-safe: kill the process at any point and it resumes
from the exact same position.

Design invariant: append() only — never rewrite or delete history.jsonl.
"""

import json
from pathlib import Path


class JSONLBus:
    def __init__(self, run_id: str, workspace_root: str = "workspace"):
        self.path = Path(workspace_root) / "runs" / run_id / "history.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def exists(self) -> bool:
        return self.path.exists() and self.path.stat().st_size > 0

    def append(self, message: dict) -> None:
        """Append one message to the JSONL file (append-only, never modifies existing lines)."""
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")

    def load_all(self) -> list[dict]:
        """Load the complete message history from disk.

        Records with role="__llm_call__" are metadata (for the web monitor)
        and are excluded from the list returned to the agent loop.
        """
        messages = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    record = json.loads(line)
                    if record.get("role") != "__llm_call__":
                        messages.append(record)
        return messages
