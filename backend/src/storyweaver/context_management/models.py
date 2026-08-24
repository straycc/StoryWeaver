"""上下文管理公共模型。"""

from __future__ import annotations

from dataclasses import dataclass

from ..llm import LlmMessage


@dataclass(frozen=True, slots=True)
class ContextItem:
    source_id: str
    source_type: str
    content: str
    protected: bool
    priority: int
    estimated_tokens: int
    reason: str


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    token_budget: int = 6000
    recent_message_limit: int = 12
    summary_target_tokens: int = 1000
    tool_inline_token_limit: int = 800
    memory_limit: int = 5

    def __post_init__(self) -> None:
        if self.token_budget < 1:
            raise ValueError("token_budget 必须大于 0")
        if self.recent_message_limit < 2:
            raise ValueError("recent_message_limit 不能小于 2")
        if self.summary_target_tokens < 1:
            raise ValueError("summary_target_tokens 必须大于 0")
        if self.tool_inline_token_limit < 1:
            raise ValueError("tool_inline_token_limit 必须大于 0")
        if self.memory_limit < 0:
            raise ValueError("memory_limit 不能小于 0")


@dataclass(frozen=True, slots=True)
class ContextAssemblyTrace:
    budget: int
    estimated_tokens: int
    selected_source_ids: tuple[str, ...]
    excluded_source_ids: tuple[str, ...]
    protected_source_ids: tuple[str, ...]
    compressed_source_ids: tuple[str, ...]
    selected_memory_ids: tuple[str, ...]
    summary_sequence: int | None = None
    notes: tuple[str, ...] = ()
    source_reasons: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ContextPackage:
    messages: tuple[LlmMessage, ...]
    items: tuple[ContextItem, ...]
    trace: ContextAssemblyTrace
