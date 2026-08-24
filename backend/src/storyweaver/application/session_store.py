"""基于 JSON 文件的对话会话持久化。"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .models import (
    ChatMessage,
    ChatSession,
    ChatSessionSummary,
    TimelinePage,
    TranscriptEvent,
)


_SESSION_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class ChatSessionStore:
    """以追加式 JSONL 保存会话，并兼容旧版 JSON 快照。"""

    def __init__(self, sessions_directory: str | Path) -> None:
        self.sessions_directory = Path(sessions_directory)
        self._lock = threading.RLock()

    def create_session(
        self,
        *,
        title: str = "新对话",
        book_id: str | None = None,
    ) -> ChatSession:
        now = self._timestamp()
        session = ChatSession(
            session_id=str(uuid4()),
            title=title.strip() or "新对话",
            book_id=book_id.strip() if book_id and book_id.strip() else None,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._append_event(
                session.session_id,
                event_type="session_created",
                created_at=now,
                payload={
                    "title": session.title,
                    "book_id": session.book_id,
                    "created_at": session.created_at,
                    "updated_at": session.updated_at,
                },
                allow_missing=True,
            )
        return session

    def load_session(self, session_id: str) -> ChatSession:
        with self._lock:
            events = self._read_events(session_id)
            if events:
                return self._project_session(events)
            legacy_path = self._legacy_session_path(session_id)
            try:
                data = json.loads(legacy_path.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise KeyError(f"会话不存在：{session_id}") from exc
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"无法读取会话 {session_id}：{exc}") from exc
            return self._decode_session(data)

    def list_sessions(self) -> tuple[ChatSessionSummary, ...]:
        if not self.sessions_directory.exists():
            return ()
        with self._lock:
            try:
                stems = {
                    path.stem
                    for pattern in ("*.jsonl", "*.json")
                    for path in self.sessions_directory.glob(pattern)
                }
            except OSError as exc:
                raise ValueError(f"无法列出会话：{exc}") from exc
        summaries = tuple(
            ChatSessionSummary(
                session_id=session.session_id,
                title=session.title,
                updated_at=session.updated_at,
                book_id=session.book_id,
                message_count=len(session.messages),
            )
            for session in (self.load_session(stem) for stem in stems)
        )
        return tuple(sorted(summaries, key=lambda item: item.updated_at, reverse=True))

    def append_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        action: str = "chat",
        metadata: Mapping[str, Any] | None = None,
    ) -> ChatSession:
        with self._lock:
            self._ensure_jsonl(session_id)
            session = self.load_session(session_id)
            now = self._timestamp()
            message = ChatMessage(
                message_id=str(uuid4()),
                role=role,
                content=content,
                created_at=now,
                action=action,
                metadata=metadata or {},
            )
            self._append_event(
                session_id,
                event_type="message_added",
                created_at=now,
                payload={
                    "message_id": message.message_id,
                    "role": message.role,
                    "content": message.content,
                    "action": message.action,
                    "metadata": dict(message.metadata),
                },
            )
            return self.load_session(session_id)

    def bind_book(
        self,
        session_id: str,
        book_id: str | None,
        *,
        allow_nonempty: bool = False,
    ) -> ChatSession:
        with self._lock:
            self._ensure_jsonl(session_id)
            session = self.load_session(session_id)
            normalized = book_id.strip() if book_id and book_id.strip() else None
            if session.book_id == normalized:
                return session
            if not allow_nonempty and self._has_conversation_activity(session_id):
                raise ValueError(
                    "当前会话已有消息或创作动作，不能原地切换作品；"
                    "请为目标作品创建新会话"
                )
            self._append_event(
                session_id,
                event_type="book_bound",
                payload={"book_id": normalized},
            )
            return self.load_session(session_id)

    def current_binding_sequence(self, session_id: str) -> int:
        """返回当前作品绑定或正史时间线分段的起始 sequence。"""

        events = self.list_events(session_id)
        boundary = next(
            (
                event.sequence
                for event in reversed(events)
                if event.event_type in {
                    "session_created",
                    "book_bound",
                    "story_timeline_rewritten",
                }
            ),
            None,
        )
        if boundary is None:  # pragma: no cover - Transcript 不变式
            raise ValueError("会话缺少创建或绑定事件")
        return boundary

    def _has_conversation_activity(self, session_id: str) -> bool:
        activity_types = {
            "message_added",
            "action_started",
            "action_completed",
            "action_failed",
            "tool_called",
            "tool_result",
            "chapter_plan_prepared",
            "chapter_plan_revised",
            "chapter_plan_confirmed",
            "chapter_plan_rejected",
            "chapter_plan_cancelled",
            "chapter_plan_expired",
            "story_timeline_rewritten",
        }
        return any(
            event.event_type in activity_types
            for event in self._read_events(session_id)
        )

    def append_event(
        self,
        session_id: str,
        *,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> TranscriptEvent:
        """追加动作、工具、摘要或记忆诊断事件。"""

        with self._lock:
            self._ensure_jsonl(session_id)
            return self._append_event(
                session_id,
                event_type=event_type,
                payload=payload or {},
            )

    def list_events(self, session_id: str) -> tuple[TranscriptEvent, ...]:
        """返回完整事件流；仅供上下文构建和诊断使用。"""

        with self._lock:
            self._ensure_jsonl(session_id)
            return self._read_events(session_id)

    def load_timeline(
        self,
        session_id: str,
        *,
        before_sequence: int | None = None,
        limit: int = 50,
    ) -> TimelinePage:
        """按 sequence 游标返回适合 UI 展示的一页事件。"""

        if limit < 1 or limit > 200:
            raise ValueError("limit 必须在 1 到 200 之间")
        if before_sequence is not None and before_sequence < 1:
            raise ValueError("before_sequence 必须大于 0")
        visible_types = {
            "message_added",
            "book_bound",
            "story_timeline_rewritten",
            "action_started",
            "action_completed",
            "action_failed",
            "tool_called",
            "tool_result",
            "chapter_plan_prepared",
            "chapter_plan_revised",
            "chapter_plan_confirmed",
            "chapter_plan_rejected",
            "chapter_plan_cancelled",
            "chapter_plan_expired",
        }
        events = tuple(
            event
            for event in self.list_events(session_id)
            if event.event_type in visible_types
            and (before_sequence is None or event.sequence < before_sequence)
        )
        page_events = events[-limit:]
        has_more = len(events) > len(page_events)
        next_before = page_events[0].sequence if has_more and page_events else None
        return TimelinePage(page_events, has_more, next_before)

    def latest_summary(self, session_id: str) -> TranscriptEvent | None:
        summaries = tuple(
            event
            for event in self.list_events(session_id)
            if event.event_type == "summary_updated"
        )
        return summaries[-1] if summaries else None

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            paths = (
                self._transcript_path(session_id),
                self._legacy_session_path(session_id),
            )
            found = False
            for path in paths:
                if path.exists():
                    path.unlink()
                    found = True
            if not found:
                raise KeyError(f"会话不存在：{session_id}")

    def _append_event(
        self,
        session_id: str,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        created_at: str | None = None,
        allow_missing: bool = False,
    ) -> TranscriptEvent:
        path = self._transcript_path(session_id)
        if not allow_missing and not path.is_file():
            raise KeyError(f"会话不存在：{session_id}")
        events = self._read_events(session_id)
        if path.is_file():
            self._repair_corrupt_tail(path, events)
        event = TranscriptEvent(
            schema_version=2,
            event_id=str(uuid4()),
            session_id=session_id,
            sequence=events[-1].sequence + 1 if events else 1,
            event_type=event_type,
            created_at=created_at or self._timestamp(),
            payload=payload,
        )
        self.sessions_directory.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(self._encode_event(event), ensure_ascii=False))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise ValueError(f"无法写入会话 {session_id}：{exc}") from exc
        return event

    def _repair_corrupt_tail(
        self,
        path: Path,
        events: tuple[TranscriptEvent, ...],
    ) -> None:
        """追加前移除已被读取器忽略的损坏尾行。"""

        try:
            nonempty_count = sum(
                1
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        except OSError as exc:
            raise ValueError(f"无法检查 Transcript 尾行 {path}：{exc}") from exc
        if nonempty_count == len(events):
            return
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.repair.tmp")
        try:
            temporary.write_text(
                "".join(
                    json.dumps(self._encode_event(event), ensure_ascii=False) + "\n"
                    for event in events
                ),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ValueError(f"无法修复 Transcript 尾行 {path}：{exc}") from exc

    def _read_events(self, session_id: str) -> tuple[TranscriptEvent, ...]:
        path = self._transcript_path(session_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise ValueError(f"无法读取会话 {session_id}：{exc}") from exc
        nonempty_lines = [line for line in raw.splitlines() if line.strip()]
        events: list[TranscriptEvent] = []
        for index, line in enumerate(nonempty_lines):
            try:
                event = self._decode_event(json.loads(line))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                if index == len(nonempty_lines) - 1:
                    break
                raise ValueError(
                    f"会话 {session_id} 第 {index + 1} 条事件损坏：{exc}"
                ) from exc
            if event.session_id != session_id:
                raise ValueError("Transcript 中的 session_id 与文件名不一致")
            expected = len(events) + 1
            if event.sequence != expected:
                raise ValueError(
                    f"Transcript sequence 不连续：期望 {expected}，实际 {event.sequence}"
                )
            events.append(event)
        return tuple(events)

    def _ensure_jsonl(self, session_id: str) -> None:
        """首次继续旧会话时生成 V2 日志，旧文件保持不变。"""

        transcript_path = self._transcript_path(session_id)
        if transcript_path.is_file():
            return
        legacy_path = self._legacy_session_path(session_id)
        try:
            legacy = self._decode_session(
                json.loads(legacy_path.read_text(encoding="utf-8"))
            )
        except FileNotFoundError as exc:
            raise KeyError(f"会话不存在：{session_id}") from exc
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法迁移会话 {session_id}：{exc}") from exc

        event_data: list[dict[str, Any]] = []
        sequence = 1
        event_data.append(
            self._encode_event(
                TranscriptEvent(
                    2,
                    str(uuid4()),
                    session_id,
                    sequence,
                    "session_created",
                    legacy.created_at,
                    {
                        "title": legacy.title,
                        "book_id": legacy.book_id,
                        "created_at": legacy.created_at,
                        "updated_at": legacy.updated_at,
                    },
                )
            )
        )
        for message in legacy.messages:
            sequence += 1
            event_data.append(
                self._encode_event(
                    TranscriptEvent(
                        2,
                        str(uuid4()),
                        session_id,
                        sequence,
                        "message_added",
                        message.created_at,
                        {
                            "message_id": message.message_id,
                            "role": message.role,
                            "content": message.content,
                            "action": message.action,
                            "metadata": dict(message.metadata),
                        },
                    )
                )
            )

        self.sessions_directory.mkdir(parents=True, exist_ok=True)
        temporary = transcript_path.with_name(
            f".{transcript_path.name}.{uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                "".join(
                    json.dumps(item, ensure_ascii=False) + "\n"
                    for item in event_data
                ),
                encoding="utf-8",
            )
            os.replace(temporary, transcript_path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ValueError(f"无法迁移会话 {session_id}：{exc}") from exc

    def _project_session(
        self,
        events: tuple[TranscriptEvent, ...],
    ) -> ChatSession:
        created = events[0]
        if created.event_type != "session_created":
            raise ValueError("Transcript 第一条事件必须是 session_created")
        session_id = created.session_id
        title = str(created.payload.get("title") or "新对话")
        book_id_value = created.payload.get("book_id")
        book_id = str(book_id_value) if book_id_value else None
        created_at = str(created.payload.get("created_at") or created.created_at)
        updated_at = str(created.payload.get("updated_at") or created.created_at)
        messages: list[ChatMessage] = []
        for event in events[1:]:
            updated_at = event.created_at
            if event.event_type == "book_bound":
                value = event.payload.get("book_id")
                book_id = str(value) if value else None
                continue
            if event.event_type != "message_added":
                continue
            message = ChatMessage(
                message_id=str(event.payload.get("message_id") or event.event_id),
                role=str(event.payload.get("role") or ""),
                content=str(event.payload.get("content") or ""),
                created_at=event.created_at,
                action=str(event.payload.get("action") or "chat"),
                metadata=(
                    event.payload.get("metadata")
                    if isinstance(event.payload.get("metadata"), Mapping)
                    else {}
                ),
                sequence=event.sequence,
            )
            messages.append(message)
            if title == "新对话" and message.role == "user":
                title = self._derive_title(message.content)
        return ChatSession(
            session_id=session_id,
            title=title,
            book_id=book_id,
            created_at=created_at,
            updated_at=updated_at,
            messages=tuple(messages),
        )

    def _validate_session_id(self, session_id: str) -> None:
        if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.fullmatch(
            session_id
        ):
            raise ValueError("session_id 格式不合法")

    def _transcript_path(self, session_id: str) -> Path:
        self._validate_session_id(session_id)
        return self.sessions_directory / f"{session_id}.jsonl"

    def _legacy_session_path(self, session_id: str) -> Path:
        self._validate_session_id(session_id)
        return self.sessions_directory / f"{session_id}.json"

    @staticmethod
    def _encode_event(event: TranscriptEvent) -> dict[str, Any]:
        return {
            "schema_version": event.schema_version,
            "event_id": event.event_id,
            "session_id": event.session_id,
            "sequence": event.sequence,
            "event_type": event.event_type,
            "created_at": event.created_at,
            "payload": dict(event.payload),
        }

    @staticmethod
    def _decode_event(data: object) -> TranscriptEvent:
        if not isinstance(data, dict):
            raise ValueError("Transcript 事件必须是 JSON 对象")
        expected = {
            "schema_version",
            "event_id",
            "session_id",
            "sequence",
            "event_type",
            "created_at",
            "payload",
        }
        if set(data) != expected:
            raise ValueError("Transcript 事件字段不完整或包含未知字段")
        return TranscriptEvent(**data)

    @staticmethod
    def _derive_title(content: str) -> str:
        normalized = " ".join(content.split())
        return normalized[:28] + ("…" if len(normalized) > 28 else "")

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _encode_session(session: ChatSession) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "title": session.title,
            "book_id": session.book_id,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "messages": [
                {
                    "message_id": message.message_id,
                    "role": message.role,
                    "content": message.content,
                    "created_at": message.created_at,
                    "action": message.action,
                    "metadata": dict(message.metadata),
                    **({"sequence": message.sequence} if message.sequence else {}),
                }
                for message in session.messages
            ],
        }

    @staticmethod
    def _decode_session(data: object) -> ChatSession:
        if not isinstance(data, dict):
            raise ValueError("会话文件必须是 JSON 对象")
        required = {"session_id", "title", "book_id", "created_at", "updated_at", "messages"}
        if set(data) != required:
            raise ValueError("会话文件字段不完整或包含未知字段")
        raw_messages = data["messages"]
        if not isinstance(raw_messages, list):
            raise ValueError("messages 必须是数组")
        messages = []
        for item in raw_messages:
            if not isinstance(item, dict):
                raise ValueError("message 必须是对象")
            messages.append(ChatMessage(**item))
        return ChatSession(
            session_id=data["session_id"],
            title=data["title"],
            book_id=data["book_id"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            messages=tuple(messages),
        )
