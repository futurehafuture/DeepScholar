"""
Phase state machine for DeepScholar.

The model drives phase transitions by calling transition_to_phase().
No external code advances the phase — this is a core design invariant.

State is persisted to workspace/runs/{run_id}/state.json so phase
position survives crashes and process restarts.
"""

import json
from enum import Enum
from pathlib import Path


class Phase(str, Enum):
    SURVEY     = "survey"
    LITERATURE = "literature"
    ARGUMENTS  = "arguments"
    INNOVATION = "innovation"
    EXPERIMENT = "experiment"
    ANALYSIS   = "analysis"
    WRITING    = "writing"


# Tool schema for transition_to_phase — always available in every phase
TRANSITION_TOOL = {
    "type": "function",
    "function": {
        "name": "transition_to_phase",
        "description": (
            "当你认为当前阶段的工作已经充分完成，主动调用此工具切换到下一个研究阶段。"
            "切换后你的角色、工作目标和可用工具会随之改变。"
            "只有在当前阶段的核心产出已写入 Artifact 文件后才应调用此工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_phase": {
                    "type": "string",
                    "enum": [p.value for p in Phase],
                    "description": "目标阶段名称",
                },
                "reason": {
                    "type": "string",
                    "description": "切换原因：当前阶段完成了什么，为什么现在适合进入下一阶段",
                },
            },
            "required": ["target_phase", "reason"],
        },
    },
}


# Tool schema for complete_research — available only in writing phase
COMPLETE_TOOL = {
    "type": "function",
    "function": {
        "name": "complete_research",
        "description": (
            "在 writing 阶段将全部论文内容保存到文件后调用此工具，"
            "标志整个研究任务结束。调用后程序将退出循环并打印最终摘要。"
            "只有在 paper_draft.tex 已成功写入磁盘后才能调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "研究成果摘要：主要发现、产出文件路径列表、核心结论（200字以内）",
                }
            },
            "required": ["summary"],
        },
    },
}


class StateMachine:
    def __init__(self, run_id: str, workspace_root: str = "workspace"):
        self.run_id = run_id
        self.state_path = Path(workspace_root) / "runs" / run_id / "state.json"
        self._load_or_init()

    def _load_or_init(self) -> None:
        if self.state_path.exists():
            with open(self.state_path, encoding="utf-8") as f:
                state = json.load(f)
            self.current_phase = Phase(state["current_phase"])
            self.metadata = state.get("metadata", {})
        else:
            self.current_phase = Phase.SURVEY
            self.metadata = {}
            self._save()

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(
                {"current_phase": self.current_phase.value, "metadata": self.metadata},
                f,
                ensure_ascii=False,
                indent=2,
            )

    def get_current_system_prompt(self) -> str:
        """Compose system prompt: base + phase-specific skill."""
        prompts_dir = Path(__file__).parent / "prompts"
        base = (prompts_dir / "base_system.md").read_text(encoding="utf-8")
        phase_file = prompts_dir / f"phase_{self._phase_index():02d}_{self.current_phase.value}.md"
        phase_prompt = phase_file.read_text(encoding="utf-8")
        return f"{base}\n\n---\n\n{phase_prompt}"

    def transition(self, target_phase: str) -> str:
        """Execute a phase transition. Called when the model invokes transition_to_phase."""
        try:
            new_phase = Phase(target_phase)
        except ValueError:
            return f"错误：未知阶段 '{target_phase}'，有效值为 {[p.value for p in Phase]}"

        old_phase = self.current_phase
        self.current_phase = new_phase
        self._save()
        return f"阶段已从 [{old_phase.value}] 切换到 [{new_phase.value}]"

    def _phase_index(self) -> int:
        order = [p.value for p in Phase]
        return order.index(self.current_phase.value) + 1

    def get_available_phases(self) -> list[str]:
        return [p.value for p in Phase]
