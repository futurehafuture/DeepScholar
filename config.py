"""
DeepScholar runtime configuration.

Priority (highest → lowest):
  1. CLI arguments (--model, --compress-model, --max-tokens)
  2. Environment variables (DEEPSCHOLAR_MODEL, DEEPSCHOLAR_COMPRESS_MODEL, etc.)
  3. Built-in defaults

Example .env overrides:
  DEEPSCHOLAR_MODEL=glm-4-plus
  DEEPSCHOLAR_COMPRESS_MODEL=glm-4-flash
  DEEPSCHOLAR_MAX_TOKENS=8096
  DEEPSCHOLAR_CONTEXT_WINDOW=128000
"""

from dataclasses import dataclass
import os


@dataclass
class Config:
    # Model used for the main research loop
    model: str = "glm-4-plus"
    # Lightweight model used for context compression (should be cheap/fast)
    compress_model: str = "glm-4-flash"
    # Max tokens per LLM response
    max_tokens: int = 8096
    # Total context window of the main model (used to set soft-compress threshold)
    context_window: int = 128_000

    @property
    def compress_threshold(self) -> int:
        """Soft-compress when messages exceed 70% of the context window."""
        return int(self.context_window * 0.70)

    @classmethod
    def from_env(cls) -> "Config":
        """Load config from environment variables, falling back to defaults."""
        return cls(
            model=os.getenv("DEEPSCHOLAR_MODEL", cls.model),
            compress_model=os.getenv("DEEPSCHOLAR_COMPRESS_MODEL", cls.compress_model),
            max_tokens=int(os.getenv("DEEPSCHOLAR_MAX_TOKENS", cls.max_tokens)),
            context_window=int(os.getenv("DEEPSCHOLAR_CONTEXT_WINDOW", cls.context_window)),
        )

    def with_overrides(self, **kwargs) -> "Config":
        """Return a new Config with specific fields replaced (for CLI overrides)."""
        import dataclasses
        return dataclasses.replace(self, **{k: v for k, v in kwargs.items() if v is not None})

    def describe(self) -> str:
        return (
            f"model={self.model}  compress_model={self.compress_model}  "
            f"max_tokens={self.max_tokens}  context_window={self.context_window:,}"
        )
