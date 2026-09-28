"""对话工作台的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


CHAT_ROLES = frozenset({"user", "assistant"})

TRANSCRIPT_EVENT_TYPES = frozenset(
    {
        "session_created",
        "session_model_selected",
        "session_reasoning_selected",
        "book_bound",
        "story_timeline_rewritten",
        "message_added",
        "action_started",
        "action_completed",
        "action_failed",
        "tool_called",
        "tool_result",
        "summary_updated",
        "memory_extracted",
        "chapter_plan_prepared",
        "chapter_plan_revised",
        "chapter_plan_approved",
        "chapter_plan_confirmed",
        "chapter_plan_rejected",
        "chapter_plan_cancelled",
        "chapter_plan_expired",
        "action_proposal_pending",
        "action_proposal_confirmed",
        "action_proposal_cancelled",
    }
)


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是非空字符串")


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """一次持久化的用户或助手消息。"""

    message_id: str
    role: str
    content: str
    created_at: str
    action: str = "chat"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    sequence: int = 0

    def __post_init__(self) -> None:
        _require_text(self.message_id, "message_id")
        _require_text(self.content, "content")
        _require_text(self.created_at, "created_at")
        _require_text(self.action, "action")
        if self.role not in CHAT_ROLES:
            raise ValueError(f"不支持的消息角色：{self.role}")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata 必须是 Mapping")
        if not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("sequence 必须是非负整数")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    """会话 JSONL 中的一条不可变事件。"""

    schema_version: int
    event_id: str
    session_id: str
    sequence: int
    event_type: str
    created_at: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValueError("仅支持 Transcript schema_version=2")
        for field_name in ("event_id", "session_id", "created_at"):
            _require_text(getattr(self, field_name), field_name)
        if self.sequence < 1:
            raise ValueError("Transcript sequence 必须大于 0")
        if self.event_type not in TRANSCRIPT_EVENT_TYPES:
            raise ValueError(f"不支持的 Transcript 事件：{self.event_type}")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload 必须是 Mapping")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class TimelinePage:
    """前端按倒序游标读取的一页会话事件。"""

    events: tuple[TranscriptEvent, ...]
    has_more: bool
    next_before_sequence: int | None


@dataclass(frozen=True, slots=True)
class ChatSession:
    """一段可恢复并可绑定小说项目的会话。"""

    session_id: str
    title: str
    created_at: str
    updated_at: str
    book_id: str | None = None
    messages: tuple[ChatMessage, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("session_id", "title", "created_at", "updated_at"):
            _require_text(getattr(self, field_name), field_name)
        if self.book_id is not None:
            _require_text(self.book_id, "book_id")
        if not isinstance(self.messages, tuple):
            raise TypeError("messages 必须是 tuple")
        if any(not isinstance(message, ChatMessage) for message in self.messages):
            raise TypeError("messages 只能包含 ChatMessage")


@dataclass(frozen=True, slots=True)
class ChatSessionSummary:
    """左侧历史列表使用的轻量会话摘要。"""

    session_id: str
    title: str
    updated_at: str
    book_id: str | None
    message_count: int
