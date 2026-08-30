"""面向 Web Chat 的 Session Memory 与上下文组装。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..llm import LlmMessage, LlmMessageRole
from ..memory.long_term import LongTermMemoryType
from ..memory.services import (
    LongTermMemoryRetriever,
    _json_value,
    _response_text,
    format_recent_dialogue,
)
from ..application.models import ChatMessage, ChatSession, TranscriptEvent
from ..application.ports import ChatSessionRepository
from .policy import ChatContextPolicy
from .selection import ContextCandidate, ContextTrace, SelectedSource


@dataclass(frozen=True, slots=True)
class ChatContextPackage:
    """最终交给 Chat Agent 的消息和轻量 Trace。"""

    messages: tuple[LlmMessage, ...]
    items: tuple[ContextCandidate, ...]
    trace: ContextTrace


_SUMMARY_FIELDS = (
    "current_goal",
    "confirmed_decisions",
    "user_constraints",
    "completed_work",
    "pending_work",
    "important_references",
)


class SessionContextManager:
    """保留原始 Transcript，仅对模型可见上下文做摘要和裁剪。"""

    def __init__(
        self,
        *,
        sessions: ChatSessionRepository,
        generate_text: Callable[[str], Awaitable[str]],
        memory_retriever: LongTermMemoryRetriever,
        policy: ChatContextPolicy | None = None,
    ) -> None:
        self.sessions = sessions
        self.generate_text = generate_text
        self.memory_retriever = memory_retriever
        self.policy = policy or ChatContextPolicy()

    async def build(
        self,
        *,
        session: ChatSession,
        system_prompt: str,
        book_context: str | None = None,
    ) -> ChatContextPackage:
        binding_sequence = self.sessions.current_binding_sequence(session.session_id)
        chat_messages = tuple(
            item
            for item in session.messages
            if item.action == "chat" and item.sequence > binding_sequence
        )
        current_index = next(
            (
                index
                for index in range(len(chat_messages) - 1, -1, -1)
                if chat_messages[index].role == "user"
            ),
            None,
        )
        current_query = (
            chat_messages[current_index].content if current_index is not None else ""
        )
        recent_context = format_recent_dialogue(
            (item.role, item.content)
            for item in (chat_messages[:current_index] if current_index is not None else ())
        )
        memories = await self.memory_retriever.retrieve(
            query=current_query,
            book_id=session.book_id,
            recent_context=recent_context,
        )
        summary = await self._ensure_summary(
            session=session,
            system_prompt=system_prompt,
            book_context=book_context,
            memories_text="\n".join(item.content for item in memories),
            binding_sequence=binding_sequence,
        )
        covered = max(
            binding_sequence,
            int(summary.payload.get("covered_through_sequence", 0)) if summary else 0,
        )
        recent = [item for item in chat_messages if item.sequence > covered]
        recent = recent[-self.policy.recent_message_limit :]

        items: list[ContextCandidate] = []
        selected_messages: list[LlmMessage] = []
        excluded: list[str] = []
        compressed: list[str] = []
        notes: list[str] = [
            f"当前作品绑定分段起点 sequence={binding_sequence}"
        ]

        self._append_item(
            items,
            selected_messages,
            source_id="system:chat",
            source_type="system_prompt",
            content=system_prompt,
            role=LlmMessageRole.SYSTEM,
            protected=True,
            priority=100,
            reason="聊天 Agent 的行为边界",
        )
        if book_context:
            self._append_item(
                items,
                selected_messages,
                source_id=f"book:{session.book_id}",
                source_type="novel_state",
                content=book_context,
                role=LlmMessageRole.SYSTEM,
                protected=True,
                priority=100,
                reason="当前关联作品的权威摘要",
            )
        for memory in memories:
            protected = memory.memory_type != LongTermMemoryType.REFERENCE
            self._append_item(
                items,
                selected_messages,
                source_id=f"memory:{memory.memory_id}",
                source_type=memory.memory_type.value,
                content=f"会话记忆：{memory.description}\n{memory.content}",
                role=LlmMessageRole.SYSTEM,
                protected=protected,
                priority=95 if protected else 65,
                reason="与当前请求相关的跨会话记忆",
            )
        if summary:
            constraints = self._summary_values(summary.payload, "user_constraints")
            if constraints:
                self._append_item(
                    items,
                    selected_messages,
                    source_id=f"summary-constraints:{summary.sequence}",
                    source_type="session_constraints",
                    content="历史会话中确认的用户约束：\n- " + "\n- ".join(constraints),
                    role=LlmMessageRole.SYSTEM,
                    protected=True,
                    priority=98,
                    reason="滚动摘要中必须跨轮次保留的用户约束",
                )
            summary_payload = dict(summary.payload)
            summary_payload["user_constraints"] = []
            summary_content = self._render_summary(summary_payload)
            self._append_item(
                items,
                selected_messages,
                source_id=f"summary:{summary.sequence}",
                source_type="session_summary",
                content=summary_content,
                role=LlmMessageRole.SYSTEM,
                protected=False,
                priority=80,
                reason="较早会话内容的滚动摘要",
            )
            compressed.append(
                f"transcript:{binding_sequence + 1}-{covered}"
            )

        activities = self._recent_activity(session.session_id, covered)
        for event in activities:
            content, was_compressed = self._activity_content(event)
            if was_compressed:
                compressed.append(f"event:{event.sequence}")
            unfinished = event.event_type == "action_started"
            self._append_item(
                items,
                selected_messages,
                source_id=f"event:{event.sequence}",
                source_type=event.event_type,
                content=content,
                role=LlmMessageRole.SYSTEM,
                protected=unfinished,
                priority=98 if unfinished else 55,
                reason=(
                    "尚未完成、必须继续跟踪的动作"
                    if unfinished
                    else "近期已经完成的动作或工具结果"
                ),
            )

        for message in recent:
            self._append_item(
                items,
                selected_messages,
                source_id=f"message:{message.message_id}",
                source_type="chat_message",
                content=message.content,
                role=(LlmMessageRole.USER if message.role == "user" else LlmMessageRole.ASSISTANT),
                protected=message is recent[-1] and message.role == "user",
                priority=90 if message.role == "user" else 75,
                reason="最近未压缩的原始聊天",
            )

        protected_tokens = sum(item.estimated_tokens for item in items if item.protected)
        if protected_tokens > self.policy.token_budget:
            raise ValueError(
                "当前用户指令、系统规则和作品约束已超过 Chat Context 预算，"
                "请缩短本轮输入或提高 STORYWEAVER_CONTEXT_TOKEN_BUDGET"
            )

        # 从最低优先级且非受保护来源开始裁剪，并同步删除对应模型消息。
        pairs = list(zip(items, selected_messages, strict=True))
        while sum(item.estimated_tokens for item, _ in pairs) > self.policy.token_budget:
            removable = [pair for pair in pairs if not pair[0].protected]
            if not removable:
                break
            target = min(removable, key=lambda pair: (pair[0].priority, pair[0].source_id))
            pairs.remove(target)
            excluded.append(target[0].source_id)
        final_items = tuple(pair[0] for pair in pairs)
        final_messages = tuple(pair[1] for pair in pairs)
        used = sum(item.estimated_tokens for item in final_items)
        notes.append(f"选择 {len(final_items)} 个来源，估算 {used}/{self.policy.token_budget} Token")
        trace = ContextTrace(
            agent_role="chat",
            policy_version="chat-context-v1",
            book_version=None,
            renderer_version="chat-messages-v1",
            budget=self.policy.token_budget,
            estimated_tokens=used,
            selected_sources=tuple(
                SelectedSource(
                    source_id=item.source_id,
                    source_type=item.source_type,
                    estimated_tokens=item.estimated_tokens,
                    protected=item.protected,
                )
                for item in final_items
            ),
            excluded_source_ids=tuple(excluded),
            compressed_source_ids=tuple(compressed),
            selected_memory_ids=tuple(item.memory_id for item in memories),
            summary_sequence=summary.sequence if summary else None,
            notes=tuple(notes),
            source_reasons=tuple(
                (item.source_id, item.reason) for item in final_items
            )
            + tuple((source_id, "超过预算或优先级较低") for source_id in excluded)
            + tuple((source_id, "原始内容已压缩为摘要或引用") for source_id in compressed),
        )
        return ChatContextPackage(final_messages, final_items, trace)

    async def refresh_summary(
        self,
        *,
        session: ChatSession,
        system_prompt: str,
        book_context: str | None = None,
    ) -> TranscriptEvent | None:
        """在主回复完成后按阈值更新摘要。

        这个方法属于后处理服务入口；Context Builder 绝不能调用它，避免
        一次意图判断隐含额外模型调用和持久化副作用。
        """

        return await self._ensure_summary(
            session=session,
            system_prompt=system_prompt,
            book_context=book_context,
            memories_text="",
            binding_sequence=self.sessions.current_binding_sequence(session.session_id),
        )

    async def _ensure_summary(
        self,
        *,
        session: ChatSession,
        system_prompt: str,
        book_context: str | None,
        memories_text: str,
        binding_sequence: int,
    ) -> TranscriptEvent | None:
        latest = self._latest_summary_for_binding(
            session,
            binding_sequence=binding_sequence,
        )
        covered = max(
            binding_sequence,
            int(latest.payload.get("covered_through_sequence", 0)) if latest else 0,
        )
        chat_messages = [
            item
            for item in session.messages
            if item.action == "chat" and item.sequence > covered
        ]
        estimated = self._estimate_tokens(
            "\n".join(
                (
                    system_prompt,
                    book_context or "",
                    memories_text,
                    *(item.content for item in chat_messages),
                )
            )
        )
        if estimated <= self.policy.token_budget:
            return latest
        to_summarize = chat_messages[: -self.policy.recent_message_limit]
        if not to_summarize:
            return latest
        previous = self._render_summary(latest.payload) if latest else "（暂无）"
        dialogue = "\n".join(f"{item.role}: {item.content}" for item in to_summarize)
        prompt = (
            "把以下较早会话压缩为结构化 JSON。必须保留用户约束、已确认决定、"
            "当前目标、已完成和未完成工作、重要文件或章节引用。"
            "字段必须是 current_goal, confirmed_decisions, user_constraints, "
            "completed_work, pending_work, important_references；每个字段使用字符串数组。"
            f"目标控制在约 {self.policy.summary_target_tokens} Token。\n\n"
            f"已有摘要：\n{previous}\n\n新增对话：\n{dialogue}"
        )
        try:
            raw = _json_value(await self.generate_text(prompt), dict)
            summary_data = self._normalize_summary(raw)
            strategy = "llm"
        except Exception:
            summary_data = self._fallback_summary(latest, to_summarize)
            strategy = "deterministic_fallback"
        payload = {
            "covered_through_sequence": to_summarize[-1].sequence,
            "book_id": session.book_id,
            "binding_sequence": binding_sequence,
            "strategy": strategy,
            **summary_data,
        }
        return self.sessions.append_event(
            session.session_id,
            event_type="summary_updated",
            payload=payload,
        )

    def _latest_summary_for_binding(
        self,
        session: ChatSession,
        *,
        binding_sequence: int,
    ) -> TranscriptEvent | None:
        """只复用当前作品绑定分段内生成的滚动摘要。"""

        summaries = tuple(
            event
            for event in self.sessions.list_events(session.session_id)
            if event.event_type == "summary_updated"
            and event.sequence > binding_sequence
        )
        for event in reversed(summaries):
            recorded_boundary = event.payload.get("binding_sequence")
            if recorded_boundary is None:
                # 从未换绑的旧会话摘要可安全兼容。
                if binding_sequence == 1:
                    return event
                continue
            if recorded_boundary != binding_sequence:
                continue
            if event.payload.get("book_id") != session.book_id:
                continue
            return event
        return None

    def _recent_activity(
        self,
        session_id: str,
        covered: int,
    ) -> tuple[TranscriptEvent, ...]:
        events = self.sessions.list_events(session_id)
        terminal_run_ids = {
            str(event.payload.get("run_id"))
            for event in events
            if event.event_type in {"action_completed", "action_failed"}
            and event.payload.get("run_id")
        }
        selected = []
        for event in events:
            if event.sequence <= covered:
                continue
            if event.event_type in {"action_completed", "action_failed", "tool_result"}:
                selected.append(event)
            elif (
                event.event_type == "action_started"
                and str(event.payload.get("run_id")) not in terminal_run_ids
            ):
                selected.append(event)
        return tuple(selected[-8:])

    def _activity_content(self, event: TranscriptEvent) -> tuple[str, bool]:
        payload = dict(event.payload)
        raw = json.dumps(payload, ensure_ascii=False, default=str)
        token_count = self._estimate_tokens(raw)
        if token_count <= self.policy.tool_inline_token_limit:
            return raw, False
        summary = str(payload.get("summary") or payload.get("status") or "工具结果已持久化")
        reference = str(payload.get("result_ref") or f"transcript:event:{event.sequence}")
        return f"{summary}\n完整结果：{reference}", True

    def _append_item(
        self,
        items: list[ContextCandidate],
        messages: list[LlmMessage],
        *,
        source_id: str,
        source_type: str,
        content: str,
        role: LlmMessageRole,
        protected: bool,
        priority: int,
        reason: str,
    ) -> None:
        item = ContextCandidate(
            source_id=source_id,
            source_type=source_type,
            content=content,
            protected=protected,
            priority=priority,
            reason=reason,
        )
        items.append(item)
        messages.append(LlmMessage(role, content))

    @staticmethod
    def _estimate_tokens(content: str) -> int:
        return max(1, (len(content) + 1) // 2)

    @staticmethod
    def _normalize_summary(data: dict[str, Any]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for field in _SUMMARY_FIELDS:
            value = data.get(field, [])
            if isinstance(value, str):
                result[field] = [value.strip()] if value.strip() else []
            elif isinstance(value, list):
                result[field] = [str(item).strip() for item in value if str(item).strip()]
            else:
                result[field] = []
        return result

    def _fallback_summary(
        self,
        previous: TranscriptEvent | None,
        messages: list[ChatMessage],
    ) -> dict[str, list[str]]:
        result = self._normalize_summary(dict(previous.payload) if previous else {})
        for message in messages:
            text = " ".join(message.content.split())
            if len(text) > 240:
                text = text[:239] + "…"
            if message.role == "user" and any(
                marker in text
                for marker in ("必须", "不要", "不能", "禁止", "偏好", "要求", "始终", "请用")
            ):
                target = "user_constraints"
            else:
                target = "confirmed_decisions" if message.role == "user" else "completed_work"
            result[target].append(text)
        for field in result:
            result[field] = result[field][-12:]
        return result

    @staticmethod
    def _render_summary(payload: Any) -> str:
        if not isinstance(payload, Mapping):
            return ""
        labels = {
            "current_goal": "当前目标",
            "confirmed_decisions": "已确认决定",
            "user_constraints": "用户约束",
            "completed_work": "已完成",
            "pending_work": "待处理",
            "important_references": "重要引用",
        }
        sections = ["会话历史摘要："]
        for field, label in labels.items():
            values = payload.get(field, [])
            if isinstance(values, str):
                values = [values]
            if isinstance(values, list) and values:
                sections.append(f"{label}：" + "；".join(str(item) for item in values))
        return "\n".join(sections)

    @staticmethod
    def _summary_values(payload: Any, field: str) -> list[str]:
        if not isinstance(payload, Mapping):
            return []
        value = payload.get(field, [])
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        return []
