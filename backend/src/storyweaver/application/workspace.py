"""对话、预设动作与小说应用服务之间的编排。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
from collections.abc import Awaitable, Callable
from typing import Any, Mapping
from uuid import uuid4

from agents import ModelSettings
from ..llm import LlmMessage, LlmMessageRole
from ..context_management import ContextPolicy, SessionContextManager
from ..llm import OpenAICompatibleProviderSettings, WorkerSettings, run_text_worker
from ..memory import (
    JsonLongTermMemoryStore,
    LongTermMemoryConsolidator,
    LongTermMemoryExtractor,
    LongTermMemoryRetriever,
    MemoryScopeType,
    LongTermMemoryStatus,
)
from ..novel_creation.application import (
    PROJECT_ROOT,
    NovelApplicationSettings,
    NovelService,
    build_novel_service,
)
from ..novel_creation.exceptions import ProjectAlreadyExistsError, ProjectPersistenceError
from ..novel_creation.models import (
    BatchPlanningContext,
    ChapterPlanProposal,
    CreateNovelRequest,
)
from ..novel_creation.observability import NovelRunObserver
from ..novel_creation.pipeline import CreateNovelPipeline
from ..observability import logging_context
from ..skills import applied_skill_markers
from .models import ChatMessage, ChatSession
from .run_progress import RunProgressStore
from .session_store import ChatSessionStore


DEFAULT_SESSIONS_DIRECTORY = PROJECT_ROOT / "data" / "chat_sessions"

ACTION_LABELS = {
    "chat": "自由对话",
    "create_novel": "创建小说",
    "create_example": "创建示例小说",
    "write_next": "写下一章",
    "start_chapter_batch": "连续创作",
    "rewrite_chapter": "重写章节",
    "revise_chapter_plan": "调整章节计划",
    "confirm_chapter_plan": "按计划生成章节",
    "cancel_chapter_plan": "取消章节计划",
    "project_status": "查看作品状态",
    "latest_chapter": "查看最新章节",
    "list_projects": "列出我的作品",
}

_WRITE_NEXT_COMMAND = re.compile(
    r"^(?:请(?:帮我)?|帮我)?\s*"
    r"(?:开始|继续)?\s*"
    r"(?:写|生成|创作)(?:一下)?\s*下一章"
    r"(?:\s*[：:,，]\s*(?P<instruction>.+?))?"
    r"\s*[。！!]?\s*$"
)

_DIRECT_CHAT_COMMANDS = {
    "作品状态": "project_status",
    "查看作品状态": "project_status",
    "最新正文": "latest_chapter",
    "查看最新正文": "latest_chapter",
    "我的作品": "list_projects",
    "列出我的作品": "list_projects",
}

CHAT_SYSTEM_PROMPT = """你是 StoryWeaver 小说创作工作台中的编辑助手。
你可以与用户讨论创意、人物、情节、写作方法和当前作品，但不能声称已经修改项目文件。
真正的建书、写章和状态读取只能由界面的预设动作执行。
回答使用清晰自然的中文；需要用户决定时，给出少量明确选项。
"""


@dataclass(frozen=True, slots=True)
class ChatActionResult:
    session: ChatSession
    assistant_message: ChatMessage


class ChatWorkspaceApplication:
    """UI 唯一调用的应用服务，不让浏览器直接操作 Store。"""

    def __init__(
        self,
        *,
        sessions: ChatSessionStore,
        novels: NovelService,
        generate_chat_text: Callable[[str], Awaitable[str]],
        model_name: str,
        context_manager: SessionContextManager | None = None,
        memory_store: JsonLongTermMemoryStore | None = None,
        memory_extractor: LongTermMemoryExtractor | None = None,
        memory_consolidator: LongTermMemoryConsolidator | None = None,
        run_progress: RunProgressStore | None = None,
    ) -> None:
        self.sessions = sessions
        self.novels = novels
        self.generate_chat_text = generate_chat_text
        self.model_name = model_name
        self.context_manager = context_manager
        self.memory_store = memory_store
        self.memory_extractor = memory_extractor
        self.memory_consolidator = memory_consolidator
        self.run_progress = run_progress or RunProgressStore()

    def bootstrap(self) -> dict[str, Any]:
        return {
            "model": self.model_name,
            "sessions": [self._summary_data(item) for item in self.sessions.list_sessions()],
            "projects": [self._metadata_data(item) for item in self.novels.list_projects()],
            "actions": ACTION_LABELS,
        }

    def create_session(self, *, book_id: str | None = None) -> ChatSession:
        if book_id is not None:
            self.novels.store.load_metadata(book_id)
        return self.sessions.create_session(book_id=book_id)

    def bind_book(self, session_id: str, book_id: str | None) -> ChatSession:
        if book_id is not None:
            self.novels.store.load_metadata(book_id)
        return self.sessions.bind_book(session_id, book_id)

    async def send_message(
        self,
        session_id: str,
        *,
        content: str,
        action: str = "chat",
        book_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        run_id: str | None = None,
    ) -> ChatActionResult:
        if action not in ACTION_LABELS:
            raise ValueError(f"不支持的预设动作：{action}")
        action = self.resolve_action(action=action, content=content)
        normalized_content = content.strip() or ACTION_LABELS[action]
        return await self._execute_request(
            session_id,
            content=normalized_content,
            action=action,
            book_id=book_id,
            payload=payload or {},
            append_user_message=True,
            requested_run_id=run_id,
        )

    async def retry_action(
        self,
        session_id: str,
        run_id: str,
        *,
        attempt_run_id: str | None = None,
    ) -> ChatActionResult:
        """重试一个已失败的动作，但不伪造新的用户消息。"""

        action, content, root_run_id, payload = self._resolve_retry_request(
            session_id,
            run_id,
        )
        return await self._execute_request(
            session_id,
            content=content,
            action=action,
            book_id=None,
            payload=payload,
            append_user_message=False,
            root_run_id=root_run_id,
            retry_of_run_id=run_id,
            requested_run_id=attempt_run_id,
        )

    async def _execute_request(
        self,
        session_id: str,
        *,
        content: str,
        action: str,
        book_id: str | None,
        payload: Mapping[str, Any],
        append_user_message: bool,
        root_run_id: str | None = None,
        retry_of_run_id: str | None = None,
        requested_run_id: str | None = None,
    ) -> ChatActionResult:
        """执行首次请求或同一逻辑动作的新尝试。"""

        if book_id is not None:
            self.bind_book(session_id, book_id)
        may_bind_created_book = (
            action in {"create_novel", "create_example"}
            and not self._has_conversation_activity(session_id)
        )
        if action in {"create_novel", "create_example"} and not may_bind_created_book:
            raise ValueError(
                "当前会话已有内容；创建新小说前请先新建空会话"
            )
        if append_user_message:
            session = self.sessions.append_message(
                session_id,
                role="user",
                content=content,
                action=action,
            )
            user_message = session.messages[-1]
        else:
            session = self.sessions.load_session(session_id)
            user_message = None
        action_run_id = (
            requested_run_id or str(uuid4())
            if action != "chat"
            else None
        )
        action_root_run_id = root_run_id or action_run_id
        progress_started = False
        if action != "chat":
            action_payload = {
                "run_id": action_run_id,
                "root_run_id": action_root_run_id,
                "retry_of_run_id": retry_of_run_id,
                "action": action,
                "label": ACTION_LABELS[action],
                "book_id": session.book_id,
            }
            for field_name in ("proposal_id", "chapter_number"):
                if payload.get(field_name):
                    action_payload[field_name] = payload[field_name]
            self.sessions.append_event(
                session_id,
                event_type="action_started",
                payload=action_payload,
            )
            self.run_progress.start_run(
                action_run_id,
                session_id=session_id,
                action=action,
                label=ACTION_LABELS[action],
            )
            progress_started = True

        try:
            with logging_context(
                session_id=session_id,
                book_id=session.book_id,
                run_id=action_run_id,
                action=action,
            ):
                reply, metadata, resulting_book_id = await self._dispatch(
                    session,
                    content=content,
                    action=action,
                    payload=payload,
                )
            plan_event_type = metadata.pop("_plan_event_type", None)
            timeline_rewrite = metadata.pop("_timeline_rewrite", None)
            additional_events = metadata.pop("_additional_events", ())
            if resulting_book_id is not None and resulting_book_id != session.book_id:
                if not may_bind_created_book:
                    raise ValueError("当前会话不允许绑定新建作品")
                session = self.sessions.bind_book(
                    session_id,
                    resulting_book_id,
                    allow_nonempty=True,
                )
            if action != "chat":
                self.sessions.append_event(
                    session_id,
                    event_type="action_completed",
                    payload={
                        "action": action,
                        "run_id": action_run_id,
                        "root_run_id": action_root_run_id,
                        "retry_of_run_id": retry_of_run_id,
                        "label": ACTION_LABELS[action],
                        "book_id": session.book_id,
                        "summary": self._compact_action_summary(reply),
                        **metadata,
                    },
                )
            if timeline_rewrite is not None:
                if not isinstance(timeline_rewrite, Mapping):
                    raise RuntimeError("时间线重写事件缺少结构化数据")
                self.sessions.append_event(
                    session_id,
                    event_type="story_timeline_rewritten",
                    payload=dict(timeline_rewrite),
                )
            if not isinstance(additional_events, tuple):
                raise RuntimeError("附加会话事件必须是 tuple")
            for extra_event in additional_events:
                if not isinstance(extra_event, Mapping):
                    raise RuntimeError("附加会话事件缺少结构化数据")
                event_type = extra_event.get("event_type")
                event_payload = extra_event.get("payload")
                if not isinstance(event_type, str) or not isinstance(event_payload, Mapping):
                    raise RuntimeError("附加会话事件格式无效")
                self.sessions.append_event(
                    session_id,
                    event_type=event_type,
                    payload=dict(event_payload),
                )
            if plan_event_type is not None:
                chapter_plan = metadata.get("chapter_plan")
                if not isinstance(chapter_plan, Mapping):
                    raise RuntimeError("章节计划事件缺少结构化计划")
                self.sessions.append_event(
                    session_id,
                    event_type=str(plan_event_type),
                    payload=dict(chapter_plan),
                )
            session = self.sessions.append_message(
                session_id,
                role="assistant",
                content=reply,
                action=action,
                metadata=metadata,
            )
            if action == "chat":
                if user_message is None:  # pragma: no cover - 内部调用不变式
                    raise RuntimeError("自由对话缺少用户消息")
                await self._extract_long_term_memory(
                    session=session,
                    user_message=user_message,
                    assistant_message=session.messages[-1],
                )
                session = self.sessions.load_session(session_id)
            elif progress_started:
                self.run_progress.complete_run(
                    action_run_id,
                    summary=self._compact_action_summary(reply),
                )
            return ChatActionResult(session=session, assistant_message=session.messages[-1])
        except Exception as exc:
            error_message = f"操作失败：{type(exc).__name__}: {exc}"
            if action != "chat":
                self.sessions.append_event(
                    session_id,
                    event_type="action_failed",
                    payload={
                        "action": action,
                        "run_id": action_run_id,
                        "root_run_id": action_root_run_id,
                        "retry_of_run_id": retry_of_run_id,
                        "label": ACTION_LABELS[action],
                        "book_id": session.book_id,
                        "error": error_message,
                    },
                )
            else:
                # 自由对话没有独立动作卡片，仍保留可见的错误消息。
                self.sessions.append_message(
                    session_id,
                    role="assistant",
                    content=error_message,
                    action=action,
                    metadata={"error": True},
                )
            if progress_started:
                self.run_progress.fail_run(
                    action_run_id,
                    error=error_message,
                )
            raise

    def _resolve_retry_request(
        self,
        session_id: str,
        run_id: str,
    ) -> tuple[str, str, str, dict[str, Any]]:
        """校验重试目标，并恢复首次动作的用户指令。"""

        normalized_run_id = run_id.strip()
        if not normalized_run_id:
            raise ValueError("run_id 不能为空")
        events = self.sessions.list_events(session_id)
        failed = next(
            (
                event
                for event in reversed(events)
                if event.event_type == "action_failed"
                and event.payload.get("run_id") == normalized_run_id
            ),
            None,
        )
        if failed is None:
            raise ValueError("只能重试已失败的动作")

        action = str(failed.payload.get("action") or "")
        if action not in {"write_next", "rewrite_chapter", "confirm_chapter_plan"}:
            raise ValueError("当前只支持重试章节规划或章节生成动作")
        root_run_id = str(
            failed.payload.get("root_run_id") or failed.payload.get("run_id")
        )
        related = tuple(
            event
            for event in events
            if event.event_type.startswith("action_")
            and str(event.payload.get("root_run_id") or event.payload.get("run_id"))
            == root_run_id
        )
        if not related or related[-1].event_type != "action_failed":
            raise ValueError("该动作已经成功或正在重试，不能重复执行")
        if related[-1].payload.get("run_id") != normalized_run_id:
            raise ValueError("该失败记录不是最新尝试，请刷新后重试")

        started = next(
            (event for event in related if event.event_type == "action_started"),
            None,
        )
        if started is None:
            raise ValueError("无法找到动作的起始记录")
        session = self.sessions.load_session(session_id)
        original_book_id = started.payload.get("book_id")
        if original_book_id and original_book_id != session.book_id:
            raise ValueError("当前会话已切换作品，请切回原作品后再重试")

        original_message = next(
            (
                event
                for event in reversed(events)
                if event.sequence < started.sequence
                and event.event_type == "message_added"
                and event.payload.get("role") == "user"
                and event.payload.get("action") == action
            ),
            None,
        )
        content = (
            str(original_message.payload.get("content"))
            if original_message is not None
            else ACTION_LABELS[action]
        )
        payload: dict[str, Any] = {}
        if action == "confirm_chapter_plan":
            proposal_id = started.payload.get("proposal_id")
            if not proposal_id:
                raise ValueError("章节生成重试缺少 proposal_id")
            payload["proposal_id"] = str(proposal_id)
        elif action == "rewrite_chapter":
            chapter_number = started.payload.get("chapter_number")
            if not isinstance(chapter_number, int):
                raise ValueError("章节重写重试缺少 chapter_number")
            payload["chapter_number"] = chapter_number
        return action, content, root_run_id, payload

    async def confirm_chapter_plan(
        self,
        session_id: str,
        proposal_id: str,
        *,
        run_id: str | None = None,
    ) -> ChatActionResult:
        self._require_session_proposal(session_id, proposal_id)
        return await self._execute_request(
            session_id,
            content="确认候选计划并生成本章",
            action="confirm_chapter_plan",
            book_id=None,
            payload={"proposal_id": proposal_id},
            append_user_message=False,
            requested_run_id=run_id,
        )

    async def cancel_chapter_plan(
        self,
        session_id: str,
        proposal_id: str,
    ) -> ChatActionResult:
        self._require_session_proposal(session_id, proposal_id)
        return await self._execute_request(
            session_id,
            content="取消候选章节计划",
            action="cancel_chapter_plan",
            book_id=None,
            payload={"proposal_id": proposal_id},
            append_user_message=False,
        )

    async def _dispatch(
        self,
        session: ChatSession,
        *,
        content: str,
        action: str,
        payload: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any], str | None]:
        if action == "chat":
            reply, usage = await self._chat(session)
            return reply, usage, None
        if action == "list_projects":
            projects = self.novels.list_projects()
            if not projects:
                return "还没有小说项目。可以点击“创建小说”开始。", {}, None
            lines = ["我的作品："]
            for item in projects:
                state = self.novels.store.load_state(item.book_id)
                lines.append(
                    f"- {item.title}（{item.book_id}）："
                    f"{state.last_committed_chapter}/{item.target_chapters} 章"
                )
            return "\n".join(lines), {}, None
        if action in {"create_novel", "create_example"}:
            request = self._create_request(payload, example=action == "create_example")
            book_id = CreateNovelPipeline.build_book_id(request)
            try:
                project = await self.novels.create_project(request)
                created = True
            except ProjectAlreadyExistsError:
                project = self.novels.store.load_project(book_id)
                created = False
            prefix = "小说项目创建完成" if created else "小说项目已经存在，已关联到当前会话"
            characters = "、".join(item.name for item in project.foundation.characters)
            reply = (
                f"{prefix}。\n\n"
                f"书名：{project.metadata.title}\n"
                f"book_id：{project.metadata.book_id}\n"
                f"题材：{project.metadata.genre}\n"
                f"角色：{characters}\n"
                f"目标：{project.metadata.target_chapters} 章，"
                f"每章约 {project.metadata.chapter_target_words} 字\n\n"
                f"故事前提：{project.foundation.premise}"
            )
            return reply, {"book_id": book_id, "created": created}, book_id

        selected_book = session.book_id
        if not selected_book:
            raise ValueError("当前会话还没有关联作品，请先选择或创建小说")
        if action == "project_status":
            return self._project_status(selected_book), {"book_id": selected_book}, None
        if action == "latest_chapter":
            state = self.novels.store.load_state(selected_book)
            if state.last_committed_chapter == 0:
                return "当前作品还没有已生成章节。", {"book_id": selected_book}, None
            draft = self.novels.store.load_chapter(
                selected_book,
                state.last_committed_chapter,
            )
            return (
                f"第 {draft.chapter_number} 章 · {draft.title}\n\n{draft.content}"
            ), {"book_id": selected_book, "chapter": draft.chapter_number}, None
        if action == "write_next":
            active = self._active_pending_plan(session.session_id, selected_book)
            if active is not None:
                raise ValueError(
                    f"第 {active.chapter_number} 章已有待确认计划，请先确认、调整或取消"
                )
            instruction = self._write_instruction(content)
            proposal = await self.novels.prepare_next_chapter(
                book_id=selected_book,
                user_instruction=instruction,
            )
            proposal_data = self._chapter_plan_data(proposal)
            reply = (
                f"第 {proposal.chapter_number} 章候选计划已经生成。"
                "请检查计划卡片；确认后才会开始生成正文。"
            )
            return reply, {
                "book_id": selected_book,
                "chapter": proposal.chapter_number,
                "proposal_id": proposal.proposal_id,
                "chapter_plan": proposal_data,
                "_plan_event_type": "chapter_plan_prepared",
            }, None
        if action == "start_chapter_batch":
            active = self._active_pending_plan(session.session_id, selected_book)
            if active is not None:
                raise ValueError(
                    f"第 {active.chapter_number} 章已有待确认计划，请先确认、调整或取消"
                )
            chapter_count = self._batch_chapter_count(payload)
            auto_confirm = self._batch_auto_confirm(payload)
            state = self.novels.store.load_state(selected_book)
            remaining_capacity = (
                self.novels.store.load_project(selected_book).metadata.target_chapters
                - state.last_committed_chapter
            )
            if chapter_count > remaining_capacity:
                raise ValueError(
                    f"当前作品最多还可创作 {remaining_capacity} 章，不能连续创作 {chapter_count} 章"
                )
            instruction = str(payload.get("instruction") or "").strip() or None
            batch = {
                "batch_id": str(uuid4()),
                "target_count": chapter_count,
                "completed_count": 0,
                "start_chapter": state.last_committed_chapter + 1,
                "end_chapter": state.last_committed_chapter + chapter_count,
                "status": "running" if auto_confirm else "waiting_confirmation",
                "instruction": instruction,
                "auto_confirm": auto_confirm,
            }
            proposal = await self.novels.prepare_next_chapter(
                book_id=selected_book,
                user_instruction=instruction,
                batch_context=self._batch_planning_context(
                    batch=batch,
                    current_chapter=state.last_committed_chapter + 1,
                ),
            )
            if auto_confirm:
                return await self._run_automatic_chapter_batch(
                    book_id=selected_book,
                    first_proposal=proposal,
                    batch=batch,
                )
            proposal_data = self._chapter_plan_data(proposal)
            proposal_data["batch"] = batch
            return (
                f"已启动连续创作：计划完成 {chapter_count} 章。\n\n"
                f"这是第 1/{chapter_count} 步：第 {proposal.chapter_number} 章候选计划已经生成。"
                "请确认或调整计划；本章提交后会自动生成下一章计划。",
                {
                    "book_id": selected_book,
                    "chapter": proposal.chapter_number,
                    "proposal_id": proposal.proposal_id,
                    "batch": batch,
                    "chapter_plan": proposal_data,
                    "_plan_event_type": "chapter_plan_prepared",
                },
                None,
            )
        if action == "rewrite_chapter":
            active = self._active_pending_plan(session.session_id, selected_book)
            if active is not None:
                raise ValueError(
                    f"第 {active.chapter_number} 章已有待确认计划，请先确认、调整或取消"
                )
            chapter_number = payload.get("chapter_number")
            if not isinstance(chapter_number, int) or isinstance(chapter_number, bool):
                raise ValueError("重写章节必须提供有效的 chapter_number")
            instruction = self._rewrite_instruction(content, chapter_number)
            record, proposal = await self.novels.prepare_rewrite_chapter(
                book_id=selected_book,
                chapter_number=chapter_number,
                user_instruction=instruction,
            )
            proposal_data = self._chapter_plan_data(proposal)
            archived = record.archived_chapter_numbers
            archived_label = (
                f"第 {archived[0]}～{archived[-1]} 章"
                if len(archived) > 1
                else f"第 {archived[0]} 章"
            )
            return (
                f"{archived_label}的原正史已经安全归档，作品已回退到"
                f"第 {chapter_number - 1} 章。\n\n"
                f"新的第 {chapter_number} 章候选计划已经生成，请检查计划卡片；"
                "确认后才会生成重写正文。",
                {
                    "book_id": selected_book,
                    "chapter": proposal.chapter_number,
                    "proposal_id": proposal.proposal_id,
                    "rewrite_id": record.rewrite_id,
                    "archived_chapters": list(record.archived_chapter_numbers),
                    "chapter_plan": proposal_data,
                    "_timeline_rewrite": {
                        "rewrite_id": record.rewrite_id,
                        "book_id": selected_book,
                        "chapter_number": chapter_number,
                        "previous_last_chapter": record.previous_last_chapter,
                        "archived_chapters": list(record.archived_chapter_numbers),
                    },
                    "_plan_event_type": "chapter_plan_prepared",
                },
                None,
            )
        if action == "revise_chapter_plan":
            proposal_id = str(payload.get("proposal_id") or "").strip()
            proposal = self._require_session_proposal(
                session.session_id,
                proposal_id,
            )
            revised = await self.novels.revise_chapter_plan(
                book_id=selected_book,
                proposal_id=proposal.proposal_id,
                feedback=content,
            )
            plan_data = self._chapter_plan_data(revised)
            batch = self._batch_for_proposal(session.session_id, proposal.proposal_id)
            if batch is not None:
                plan_data["batch"] = batch
            return (
                f"已根据你的意见生成第 {revised.chapter_number} 章计划 V{revised.version}，"
                "请再次确认。",
                {
                    "book_id": selected_book,
                    "chapter": revised.chapter_number,
                    "proposal_id": revised.proposal_id,
                    "batch": batch,
                    "chapter_plan": plan_data,
                    "_plan_event_type": "chapter_plan_revised",
                },
                None,
            )
        if action == "confirm_chapter_plan":
            proposal_id = str(payload.get("proposal_id") or "").strip()
            proposal = self._require_session_proposal(
                session.session_id,
                proposal_id,
            )
            result = await self.novels.confirm_chapter_plan(
                book_id=selected_book,
                proposal_id=proposal.proposal_id,
            )
            current_proposal = self.novels.load_chapter_plan_proposal(
                book_id=selected_book,
                proposal_id=proposal.proposal_id,
            )
            batch = self._batch_for_proposal(session.session_id, proposal.proposal_id)
            if result.committed:
                if result.state_delta is None:  # pragma: no cover - 模型不变量
                    raise RuntimeError("已提交章节缺少状态增量")
                reply = (
                    f"第 {result.chapter_number} 章生成并提交完成。\n\n"
                    f"标题：{result.final_draft.title}\n"
                    f"字数：{result.final_draft.word_count}\n"
                    f"状态：{result.status}\n"
                    f"自动修订：{result.revision_count} 轮\n"
                    f"审查：{result.final_review.summary}\n"
                    f"摘要：{result.state_delta.chapter_summary}\n\n"
                    f"{result.final_draft.content}"
                )
                plan_event_type = "chapter_plan_confirmed"
            else:
                candidate = self.novels.store.load_chapter_candidate(
                    selected_book,
                    result.candidate_id or "",
                )
                reply = (
                    f"第 {result.chapter_number} 章候选正文已经生成，"
                    "但 strict 审稿未通过，因此没有进入小说正史。\n\n"
                    f"候选 ID：{candidate.candidate_id}\n"
                    f"标题：{result.final_draft.title}\n"
                    f"字数：{result.final_draft.word_count}\n"
                    f"自动修订：{result.revision_count} 轮\n"
                    f"审查：{result.final_review.summary}\n"
                    f"拒绝原因：{candidate.reason}\n"
                    "正史状态：未变化；ChapterAnalyzer 未执行。\n\n"
                    "你可以调整当前章节计划后重新生成。\n\n"
                    f"{result.final_draft.content}"
                )
                plan_event_type = "chapter_plan_rejected"
            plan_data = self._chapter_plan_data(current_proposal)
            if not result.committed:
                plan_data.update(
                    {
                        "candidate_id": result.candidate_id,
                        "review_rejection": candidate.reason,
                    }
                )
            metadata: dict[str, Any] = {
                "book_id": selected_book,
                "chapter": result.chapter_number,
                "status": result.status,
                "committed": result.committed,
                "revision_count": result.revision_count,
                "candidate_id": result.candidate_id,
                "proposal_id": proposal.proposal_id,
                "chapter_plan": plan_data,
                "_plan_event_type": plan_event_type,
            }
            if batch is not None:
                metadata["batch"] = batch
                plan_data["batch"] = batch
            if batch is not None and result.committed:
                completed_count = int(batch.get("completed_count") or 0) + 1
                target_count = int(batch.get("target_count") or 0)
                batch = {
                    key: value
                    for key, value in batch.items()
                    if key != "pause_reason"
                } | {
                    "completed_count": completed_count,
                    "status": "completed"
                    if completed_count >= target_count
                    else "running"
                    if batch.get("auto_confirm")
                    else "waiting_confirmation",
                }
                metadata["batch"] = batch
                if completed_count < target_count:
                    if batch.get("auto_confirm"):
                        try:
                            next_proposal = await self.novels.prepare_next_chapter(
                                book_id=selected_book,
                                user_instruction=(
                                    str(batch.get("instruction") or "").strip() or None
                                ),
                                batch_context=self._batch_planning_context(
                                    batch=batch,
                                    current_chapter=result.chapter_number + 1,
                                ),
                            )
                        except Exception as exc:
                            batch = {
                                **batch,
                                "status": "paused",
                                "pause_reason": str(exc),
                            }
                            plan_data["batch"] = batch
                            metadata["batch"] = batch
                            reply += "\n\n本章已提交，但下一章计划生成失败，连续创作已暂停。"
                        else:
                            # strict 暂停后的人工计划调整只处理当前章；一旦该章
                            # 提交，剩余章节重新回到原自动批次，不丢失确认模式。
                            plan_data["batch"] = batch
                            auto_reply, auto_metadata, _ = (
                                await self._run_automatic_chapter_batch(
                                    book_id=selected_book,
                                    first_proposal=next_proposal,
                                    batch=batch,
                                )
                            )
                            auto_events = auto_metadata.pop("_additional_events", ())
                            auto_metadata["_additional_events"] = (
                                {
                                    "event_type": "chapter_plan_confirmed",
                                    "payload": plan_data,
                                },
                                *auto_events,
                            )
                            return (
                                f"第 {result.chapter_number} 章调整后已提交。\n\n{auto_reply}",
                                auto_metadata,
                                None,
                            )
                    else:
                        try:
                            next_proposal = await self.novels.prepare_next_chapter(
                                book_id=selected_book,
                                user_instruction=(
                                    str(batch.get("instruction") or "").strip() or None
                                ),
                                batch_context=self._batch_planning_context(
                                    batch=batch,
                                    current_chapter=result.chapter_number + 1,
                                ),
                            )
                        except Exception as exc:
                            batch = {**batch, "status": "paused", "pause_reason": str(exc)}
                            plan_data["batch"] = batch
                            metadata["batch"] = batch
                            reply += "\n\n本章已提交，但下一章计划生成失败，连续创作已暂停。"
                        else:
                            next_plan_data = self._chapter_plan_data(next_proposal)
                            next_plan_data["batch"] = batch
                            metadata["chapter"] = next_proposal.chapter_number
                            metadata["proposal_id"] = next_proposal.proposal_id
                            metadata["chapter_plan"] = next_plan_data
                            metadata["_plan_event_type"] = "chapter_plan_prepared"
                            metadata["_additional_events"] = (
                                {
                                    "event_type": "chapter_plan_confirmed",
                                    "payload": plan_data,
                                },
                            )
                            reply += (
                                f"\n\n连续创作进度：已完成 {completed_count}/{target_count} 章。"
                                f"第 {next_proposal.chapter_number} 章候选计划已自动生成，等待你确认。"
                            )
                else:
                    plan_data["batch"] = batch
                    reply += f"\n\n连续创作已完成：共提交 {completed_count}/{target_count} 章。"
            elif batch is not None:
                batch = {**batch, "status": "paused", "pause_reason": "本章未通过 strict 审稿"}
                plan_data["batch"] = batch
                metadata["batch"] = batch
                reply += "\n\n连续创作已暂停：请先调整本章计划并重新生成。"
            return reply, metadata, None
        if action == "cancel_chapter_plan":
            proposal_id = str(payload.get("proposal_id") or "").strip()
            proposal = self._require_session_proposal(
                session.session_id,
                proposal_id,
            )
            cancelled = self.novels.cancel_chapter_plan(
                book_id=selected_book,
                proposal_id=proposal.proposal_id,
            )
            return (
                f"已取消第 {cancelled.chapter_number} 章候选计划。",
                {
                    "book_id": selected_book,
                    "chapter": cancelled.chapter_number,
                    "proposal_id": cancelled.proposal_id,
                    "chapter_plan": self._chapter_plan_data(cancelled),
                    "_plan_event_type": "chapter_plan_cancelled",
                },
                None,
            )
        raise ValueError(f"尚未实现动作：{action}")

    @staticmethod
    def resolve_action(*, action: str, content: str) -> str:
        """把明确的自然语言命令映射到预设动作，避免让模型猜测副作用。"""

        if action != "chat":
            return action
        normalized = content.strip()
        if _WRITE_NEXT_COMMAND.fullmatch(normalized):
            return "write_next"
        command = normalized.rstrip("。！!").replace(" ", "")
        return _DIRECT_CHAT_COMMANDS.get(command, action)

    @staticmethod
    def _write_instruction(content: str) -> str | None:
        """从对话命令中提取冒号后的本章要求。"""

        normalized = content.strip()
        if normalized == ACTION_LABELS["write_next"]:
            return None
        matched = _WRITE_NEXT_COMMAND.fullmatch(normalized)
        if matched:
            instruction = matched.group("instruction")
            return instruction.strip() if instruction else None
        return normalized or None

    @staticmethod
    def _batch_chapter_count(payload: Mapping[str, Any]) -> int:
        """校验连续创作的章节数，避免一次任务占用过长。"""

        count = payload.get("chapter_count")
        if not isinstance(count, int) or isinstance(count, bool) or not 2 <= count <= 12:
            raise ValueError("连续创作章节数必须是 2 到 12 的整数")
        return count

    @staticmethod
    def _batch_auto_confirm(payload: Mapping[str, Any]) -> bool:
        """读取连续创作的确认模式，避免浏览器字符串值混入领域状态。"""

        value = payload.get("auto_confirm", False)
        if not isinstance(value, bool):
            raise ValueError("auto_confirm 必须是布尔值")
        return value

    @staticmethod
    def _batch_planning_context(
        *,
        batch: Mapping[str, Any],
        current_chapter: int,
    ) -> BatchPlanningContext:
        """从 UI 批次状态构造仅供本次规划使用的阶段边界。"""

        target_count = int(batch["target_count"])
        completed_count = int(batch.get("completed_count") or 0)
        start_chapter = int(
            batch.get("start_chapter") or current_chapter - completed_count
        )
        end_chapter = int(
            batch.get("end_chapter") or start_chapter + target_count - 1
        )
        instruction = str(batch.get("instruction") or "").strip() or None
        return BatchPlanningContext(
            start_chapter=start_chapter,
            end_chapter=end_chapter,
            current_chapter=current_chapter,
            overall_instruction=instruction,
        )

    async def _run_automatic_chapter_batch(
        self,
        *,
        book_id: str,
        first_proposal: ChapterPlanProposal,
        batch: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any], None]:
        """依次确认候选计划；任一异常或严格审稿拒绝都会安全暂停。"""

        current_proposal = first_proposal
        current_batch = dict(batch)
        completed_summaries: list[str] = []
        first_plan_data = self._chapter_plan_data(first_proposal)
        first_plan_data["batch"] = current_batch
        timeline_events: list[dict[str, Any]] = [
            {"event_type": "chapter_plan_prepared", "payload": first_plan_data}
        ]

        while True:
            try:
                result = await self.novels.confirm_chapter_plan(
                    book_id=book_id,
                    proposal_id=current_proposal.proposal_id,
                )
                persisted_proposal = self.novels.load_chapter_plan_proposal(
                    book_id=book_id,
                    proposal_id=current_proposal.proposal_id,
                )
            except Exception as exc:
                current_batch = {
                    **current_batch,
                    "status": "paused",
                    "pause_reason": str(exc),
                }
                plan_data = self._chapter_plan_data(current_proposal)
                plan_data["batch"] = current_batch
                return self._automatic_batch_reply(
                    batch=current_batch,
                    plan_data=plan_data,
                    completed_summaries=completed_summaries,
                    timeline_events=timeline_events,
                    message=(
                        f"第 {current_proposal.chapter_number} 章执行异常，连续创作已暂停：{exc}"
                    ),
                )

            plan_data = self._chapter_plan_data(persisted_proposal)
            if not result.committed:
                candidate = self.novels.store.load_chapter_candidate(
                    book_id,
                    result.candidate_id or "",
                )
                current_batch = {
                    **current_batch,
                    "status": "paused",
                    "pause_reason": "本章未通过 strict 审稿",
                }
                plan_data.update(
                    {
                        "candidate_id": result.candidate_id,
                        "review_rejection": candidate.reason,
                        "batch": current_batch,
                    }
                )
                timeline_events.append(
                    {"event_type": "chapter_plan_rejected", "payload": plan_data}
                )
                return self._automatic_batch_reply(
                    batch=current_batch,
                    plan_data=plan_data,
                    completed_summaries=completed_summaries,
                    timeline_events=timeline_events,
                    message=(
                        f"第 {result.chapter_number} 章未通过 strict 审稿，连续创作已暂停。"
                    ),
                )

            completed_count = int(current_batch["completed_count"]) + 1
            target_count = int(current_batch["target_count"])
            current_batch = {
                **current_batch,
                "completed_count": completed_count,
                "status": "completed" if completed_count >= target_count else "running",
            }
            plan_data["batch"] = current_batch
            timeline_events.append(
                {"event_type": "chapter_plan_confirmed", "payload": plan_data}
            )
            completed_summaries.append(
                f"第 {result.chapter_number} 章《{result.final_draft.title}》"
                f" · {result.final_draft.word_count} 字 · 审查 {result.final_review.score} 分"
            )
            if completed_count >= target_count:
                return self._automatic_batch_reply(
                    batch=current_batch,
                    plan_data=plan_data,
                    completed_summaries=completed_summaries,
                    timeline_events=timeline_events,
                    message="连续创作已完成。",
                )

            try:
                current_proposal = await self.novels.prepare_next_chapter(
                    book_id=book_id,
                    user_instruction=(
                        str(current_batch.get("instruction") or "").strip() or None
                    ),
                    batch_context=self._batch_planning_context(
                        batch=current_batch,
                        current_chapter=result.chapter_number + 1,
                    ),
                )
            except Exception as exc:
                current_batch = {
                    **current_batch,
                    "status": "paused",
                    "pause_reason": str(exc),
                }
                plan_data["batch"] = current_batch
                return self._automatic_batch_reply(
                    batch=current_batch,
                    plan_data=plan_data,
                    completed_summaries=completed_summaries,
                    timeline_events=timeline_events,
                    message=f"下一章计划生成失败，连续创作已暂停：{exc}",
                )

            next_plan_data = self._chapter_plan_data(current_proposal)
            next_plan_data["batch"] = current_batch
            timeline_events.append(
                {"event_type": "chapter_plan_prepared", "payload": next_plan_data}
            )

    @staticmethod
    def _automatic_batch_reply(
        *,
        batch: Mapping[str, Any],
        plan_data: Mapping[str, Any],
        completed_summaries: list[str],
        timeline_events: list[Mapping[str, Any]],
        message: str,
    ) -> tuple[str, dict[str, Any], None]:
        """生成自动连续创作的精简结果，避免把所有正文重复塞入对话。"""

        lines = [message, "", f"连续创作进度：{batch['completed_count']}/{batch['target_count']} 章。"]
        if completed_summaries:
            lines.extend(["", "已提交章节："])
            lines.extend(f"- {item}" for item in completed_summaries)
        if batch.get("status") == "paused":
            lines.extend(["", f"暂停原因：{batch.get('pause_reason') or '未知原因'}"])
        return (
            "\n".join(lines),
            {
                "book_id": plan_data.get("book_id"),
                "chapter": plan_data.get("chapter_number"),
                "proposal_id": plan_data.get("proposal_id"),
                "chapter_plan": dict(plan_data),
                "batch": dict(batch),
                "_additional_events": tuple(dict(item) for item in timeline_events),
            },
            None,
        )

    def _batch_for_proposal(
        self,
        session_id: str,
        proposal_id: str,
    ) -> dict[str, Any] | None:
        """从事件流恢复某份计划所属的连续创作批次。"""

        for event in reversed(self.sessions.list_events(session_id)):
            if event.event_type != "action_completed" and not event.event_type.startswith(
                "chapter_plan_"
            ):
                continue
            if event.payload.get("proposal_id") != proposal_id:
                continue
            batch = event.payload.get("batch")
            if isinstance(batch, Mapping):
                return dict(batch)
        return None

    @staticmethod
    def _rewrite_instruction(content: str, chapter_number: int) -> str | None:
        """从界面生成的重写命令中提取用户要求。"""

        normalized = content.strip()
        prefix = f"重写第{chapter_number}章"
        if normalized == prefix:
            return None
        if normalized.startswith(prefix):
            remainder = normalized[len(prefix) :].lstrip(" ：:,，")
            return remainder or None
        return normalized or None

    async def _chat(self, session: ChatSession) -> tuple[str, dict[str, Any]]:
        book_context = self._book_chat_context(session.book_id)
        if self.context_manager is not None:
            package = await self.context_manager.build(
                session=session,
                system_prompt=CHAT_SYSTEM_PROMPT,
                book_context=book_context,
            )
            messages = list(package.messages)
            trace_data = asdict(package.trace)
        else:
            # 兼容直接构造应用服务的旧测试与嵌入调用。
            binding_sequence = self.sessions.current_binding_sequence(
                session.session_id
            )
            history = tuple(
                item
                for item in session.messages
                if item.action == "chat" and item.sequence > binding_sequence
            )[-24:]
            messages = [LlmMessage(LlmMessageRole.SYSTEM, CHAT_SYSTEM_PROMPT)]
            if book_context:
                messages.append(LlmMessage(LlmMessageRole.SYSTEM, book_context))
            messages.extend(
                LlmMessage(
                    LlmMessageRole.USER if item.role == "user" else LlmMessageRole.ASSISTANT,
                    item.content,
                )
                for item in history
            )
            trace_data = None
        prompt = "\n\n".join(f"[{item.role.value}]\n{item.content}" for item in messages)
        reply = await self.generate_chat_text(prompt)
        metadata: dict[str, Any] = {
            "usage": None
        }
        if trace_data is not None:
            metadata["context_trace"] = trace_data
        return reply.strip(), metadata

    def _book_chat_context(self, book_id: str | None) -> str | None:
        if not book_id:
            return None
        project = self.novels.store.load_project(book_id)
        return (
            "当前关联作品摘要：\n"
            f"书名：{project.metadata.title}\n"
            f"故事前提：{project.foundation.premise}\n"
            f"当前章节：{project.state.last_committed_chapter}\n"
            f"当前位置：{project.state.current_location}\n"
            f"当前时间：{project.state.current_time}\n"
            "这是小说权威状态的只读摘要；不得声称已通过聊天修改作品文件。"
        )

    async def _extract_long_term_memory(
        self,
        *,
        session: ChatSession,
        user_message: ChatMessage,
        assistant_message: ChatMessage,
    ) -> None:
        if self.memory_extractor is None:
            return
        result = await self.memory_extractor.extract(
            user_message=user_message.content,
            assistant_message=assistant_message.content,
            session_id=session.session_id,
            user_message_id=user_message.message_id,
            book_id=session.book_id,
        )
        self.sessions.append_event(
            session.session_id,
            event_type="memory_extracted",
            payload={
                "memory_ids": [item.memory_id for item in result.records],
                "count": len(result.records),
                "ignored_count": result.ignored_count,
                "error": result.error,
            },
        )
        if self.memory_consolidator is None or not result.records:
            return
        scopes = {(item.scope_type, item.scope_id) for item in result.records}
        for scope_type, scope_id in scopes:
            await self.memory_consolidator.consolidate_if_needed(
                scope_type=scope_type,
                scope_id=scope_id,
            )

    async def post_main_agent_turn(
        self,
        *,
        session_id: str,
        user_message_id: str,
        assistant_message_id: str,
    ) -> None:
        """主 Agent 回复后的低优先级摘要与记忆维护。

        调用者应在后台执行；任何失败都不能影响已经交付给用户的回复。
        """

        session = self.sessions.load_session(session_id)
        messages = {item.message_id: item for item in session.messages}
        user_message = messages.get(user_message_id)
        assistant_message = messages.get(assistant_message_id)
        if user_message is None or assistant_message is None:
            return
        if self.context_manager is not None:
            try:
                await self.context_manager.refresh_summary(
                    session=session,
                    system_prompt=CHAT_SYSTEM_PROMPT,
                    book_context=self._book_chat_context(session.book_id),
                )
            except Exception:
                # 摘要只是上下文优化，不能因辅助任务破坏已完成对话。
                pass
        try:
            await self._extract_long_term_memory(
                session=session,
                user_message=user_message,
                assistant_message=assistant_message,
            )
        except Exception:
            # 记忆提取同样是最佳努力，不影响主回复与会话事件。
            pass

    @staticmethod
    def _compact_action_summary(content: str) -> str:
        normalized = " ".join(content.split())
        return normalized[:320] + ("…" if len(normalized) > 320 else "")

    def _has_conversation_activity(self, session_id: str) -> bool:
        return any(
            event.event_type
            in {
                "message_added",
                "action_started",
                "action_completed",
                "action_failed",
                "tool_called",
                "tool_result",
            }
            for event in self.sessions.list_events(session_id)
        )

    def _project_status(self, book_id: str) -> str:
        project = self.novels.store.load_project(book_id)
        index = self.novels.store.load_chapter_index(book_id)
        hooks = "\n".join(
            (
                f"- {item.display_name} [{self._hook_status_label(item.status)}]"
                + (f"：{item.description}" if item.name.strip() else "")
            )
            for item in project.state.hooks
        ) or "- 暂无伏笔"
        chapters = "\n".join(
            f"- 第 {item.chapter_number} 章 {item.title} · {item.status}"
            for item in index
        ) or "- 尚未生成章节"
        return (
            f"作品：{project.metadata.title}\n"
            f"进度：{project.state.last_committed_chapter}/"
            f"{project.metadata.target_chapters} 章\n"
            f"当前位置：{project.state.current_location}\n"
            f"当前时间：{project.state.current_time}\n"
            f"有效事实：{len(project.state.current_facts)}\n\n"
            f"伏笔：\n{hooks}\n\n章节：\n{chapters}"
        )

    def list_memories(
        self,
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        if self.memory_store is None:
            return []
        records = self.memory_store.list_records(
            scope_type=MemoryScopeType(scope_type) if scope_type else None,
            scope_id=scope_id,
            status=LongTermMemoryStatus(status) if status else None,
        )
        return [self._memory_data(item) for item in records]

    def update_memory(
        self,
        memory_id: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self.memory_store is None:
            raise ValueError("长期记忆功能未启用")
        allowed = {"memory_type", "name", "description", "content", "importance"}
        normalized = {key: value for key, value in changes.items() if key in allowed}
        if set(changes) - allowed:
            raise ValueError("请求包含不可修改的 Memory 字段")
        if not normalized:
            raise ValueError("没有可修改的 Memory 字段")
        return self._memory_data(self.memory_store.update(memory_id, **normalized))

    def disable_memory(self, memory_id: str) -> dict[str, Any]:
        if self.memory_store is None:
            raise ValueError("长期记忆功能未启用")
        return self._memory_data(self.memory_store.disable(memory_id))

    def restore_memory(self, memory_id: str) -> dict[str, Any]:
        if self.memory_store is None:
            raise ValueError("长期记忆功能未启用")
        return self._memory_data(self.memory_store.restore(memory_id))

    def latest_context_trace(self, session_id: str) -> dict[str, Any] | None:
        session = self.sessions.load_session(session_id)
        for message in reversed(session.messages):
            trace = message.metadata.get("context_trace")
            if message.role == "assistant" and isinstance(trace, Mapping):
                return dict(trace)
        return None

    def _require_session_proposal(
        self,
        session_id: str,
        proposal_id: str,
    ) -> ChapterPlanProposal:
        normalized_id = proposal_id.strip()
        if not normalized_id:
            raise ValueError("proposal_id 不能为空")
        session = self.sessions.load_session(session_id)
        if not session.book_id:
            raise ValueError("当前会话没有关联作品")
        events = self.sessions.list_events(session_id)
        boundary = self.sessions.current_binding_sequence(session_id)
        belongs_to_current_timeline = any(
            event.event_type.startswith("chapter_plan_")
            and event.payload.get("proposal_id") == normalized_id
            and event.sequence > boundary
            for event in events
        )
        if not belongs_to_current_timeline:
            belongs_to_history = any(
                event.event_type.startswith("chapter_plan_")
                and event.payload.get("proposal_id") == normalized_id
                for event in events
            )
            if belongs_to_history:
                raise ValueError("该候选计划已随章节重写归档，请重新规划当前章节")
            raise ValueError("候选计划不属于当前会话")
        try:
            proposal = self.novels.load_chapter_plan_proposal(
                book_id=session.book_id,
                proposal_id=normalized_id,
            )
        except ProjectPersistenceError as exc:
            raise ValueError("候选计划已失效或不再存在，请重新规划当前章节") from exc
        if proposal.book_id != session.book_id:
            raise ValueError("候选计划与当前会话绑定作品不一致")
        return proposal

    def _active_pending_plan(
        self,
        session_id: str,
        book_id: str,
    ) -> ChapterPlanProposal | None:
        boundary = self.sessions.current_binding_sequence(session_id)
        seen: set[str] = set()
        for event in reversed(self.sessions.list_events(session_id)):
            if event.sequence <= boundary:
                break
            if not event.event_type.startswith("chapter_plan_"):
                continue
            proposal_id = str(event.payload.get("proposal_id") or "")
            if not proposal_id or proposal_id in seen:
                continue
            seen.add(proposal_id)
            try:
                proposal = self.novels.load_chapter_plan_proposal(
                    book_id=book_id,
                    proposal_id=proposal_id,
                )
            except (KeyError, ValueError, ProjectPersistenceError):
                continue
            if proposal.status == "pending":
                return proposal
        return None

    def _chapter_plan_data(
        self,
        proposal: ChapterPlanProposal,
    ) -> dict[str, Any]:
        project = self.novels.store.load_project(proposal.book_id)
        character_names = {
            item.character_id: item.name for item in project.foundation.characters
        }
        hooks = {item.hook_id: item for item in project.state.hooks}
        plan = proposal.plan
        return {
            "proposal_id": proposal.proposal_id,
            "book_id": proposal.book_id,
            "chapter_number": proposal.chapter_number,
            "base_chapter_number": proposal.base_chapter_number,
            "version": proposal.version,
            "status": proposal.status,
            "goal": plan.goal,
            "location": plan.location,
            "current_time": proposal.base_current_time,
            "participants": [
                {
                    "character_id": character_id,
                    "name": character_names.get(character_id, character_id),
                }
                for character_id in plan.participating_character_ids
            ],
            "required_beats": list(plan.required_beats),
            "forbidden_events": list(plan.forbidden_events),
            "relevant_hooks": [
                {
                    "hook_id": hook_id,
                    "name": (
                        hooks[hook_id].display_name if hook_id in hooks else hook_id
                    ),
                    "description": (
                        hooks[hook_id].description if hook_id in hooks else hook_id
                    ),
                }
                for hook_id in plan.relevant_hook_ids
            ],
            "ending_hook": plan.ending_hook,
            "style_focus": list(plan.style_focus),
            "target_words": project.metadata.chapter_target_words,
            "user_instruction": proposal.user_instruction,
            # 计划持久化的是注入后的指令，这里只投影标识和版本哈希，不向 UI 暴露 Skill 正文。
            "applied_skills": list(applied_skill_markers(proposal.user_instruction)),
            "feedback_history": list(proposal.feedback_history),
            "selected_memories": list(proposal.selected_memory_descriptions),
            "created_at": proposal.created_at,
            "updated_at": proposal.updated_at,
        }

    @staticmethod
    def _hook_status_label(status: str) -> str:
        return {
            "open": "未解",
            "progressing": "推进中",
            "resolved": "已回收",
            "deferred": "暂缓",
        }.get(status, status)

    @staticmethod
    def _create_request(payload: Mapping[str, Any], *, example: bool) -> CreateNovelRequest:
        if example:
            from ..novel_creation.cli import RAINY_HOTEL_REQUEST

            return RAINY_HOTEL_REQUEST
        required = (
            "title",
            "genre",
            "premise",
            "protagonist",
            "central_conflict",
            "tone",
            "target_chapters",
            "chapter_target_words",
        )
        missing = [name for name in required if name not in payload]
        if missing:
            raise ValueError("创建小说缺少字段：" + ", ".join(missing))
        return CreateNovelRequest(
            title=str(payload["title"]),
            genre=str(payload["genre"]),
            premise=str(payload["premise"]),
            protagonist=str(payload["protagonist"]),
            central_conflict=str(payload["central_conflict"]),
            tone=str(payload["tone"]),
            target_chapters=int(payload["target_chapters"]),
            chapter_target_words=int(payload["chapter_target_words"]),
            language=str(payload.get("language", "zh")),
        )

    @staticmethod
    def session_data(session: ChatSession) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "title": session.title,
            "book_id": session.book_id,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "message_count": len(session.messages),
            "messages": [
                {
                    "message_id": item.message_id,
                    "role": item.role,
                    "content": item.content,
                    "created_at": item.created_at,
                    "action": item.action,
                    "metadata": dict(item.metadata),
                    "sequence": item.sequence,
                }
                for item in session.messages
            ],
        }

    @staticmethod
    def timeline_data(event: object) -> dict[str, Any]:
        return {
            "schema_version": getattr(event, "schema_version"),
            "event_id": getattr(event, "event_id"),
            "session_id": getattr(event, "session_id"),
            "sequence": getattr(event, "sequence"),
            "event_type": getattr(event, "event_type"),
            "created_at": getattr(event, "created_at"),
            "payload": dict(getattr(event, "payload")),
        }

    @staticmethod
    def _memory_data(item: object) -> dict[str, Any]:
        return {
            "memory_id": getattr(item, "memory_id"),
            "memory_type": getattr(item, "memory_type").value,
            "scope_type": getattr(item, "scope_type").value,
            "scope_id": getattr(item, "scope_id"),
            "name": getattr(item, "name"),
            "description": getattr(item, "description"),
            "content": getattr(item, "content"),
            "importance": getattr(item, "importance"),
            "source_refs": list(getattr(item, "source_refs")),
            "status": getattr(item, "status").value,
            "supersedes_id": getattr(item, "supersedes_id"),
            "created_at": getattr(item, "created_at"),
            "updated_at": getattr(item, "updated_at"),
        }

    @staticmethod
    def _summary_data(item: object) -> dict[str, Any]:
        return {
            "session_id": getattr(item, "session_id"),
            "title": getattr(item, "title"),
            "book_id": getattr(item, "book_id"),
            "updated_at": getattr(item, "updated_at"),
            "message_count": getattr(item, "message_count"),
        }

    @staticmethod
    def _metadata_data(item: object) -> dict[str, Any]:
        return {
            "book_id": getattr(item, "book_id"),
            "title": getattr(item, "title"),
            "genre": getattr(item, "genre"),
            "target_chapters": getattr(item, "target_chapters"),
        }


def build_chat_workspace(
    settings: NovelApplicationSettings,
    *,
    sessions_directory: str | Path = DEFAULT_SESSIONS_DIRECTORY,
    memory_directory: str | Path | None = None,
    sessions: Any | None = None,
    memory_store: Any | None = None,
    novels: NovelService | None = None,
    run_progress: RunProgressStore | None = None,
) -> ChatWorkspaceApplication:
    """构造对话编排层。

    默认保留旧文件仓储行为，FastAPI 则注入 PostgreSQL 仓储和已组装的
    NovelService，使原有 UI 动作语义可以复用而不再写入 ``data/``。
    """
    provider = OpenAICompatibleProviderSettings(
        base_url=settings.base_url,
        model_name=settings.model,
        api_key=settings.api_key,
    ).create_provider()

    async def generate_memory_text(prompt: str) -> str:
        return await run_text_worker(
            settings=WorkerSettings(
                worker_id="long-term-memory", name="长期记忆",
                instructions="只完成用户给定的记忆任务。",
                model=provider.get_model(settings.model),
                model_settings=ModelSettings(temperature=0.1),
                timeout_seconds=settings.timeout_seconds,
            ),
            prompt=prompt,
        )

    async def generate_context_text(prompt: str) -> str:
        return await run_text_worker(
            settings=WorkerSettings(
                worker_id="session-context", name="会话摘要",
                instructions="只完成用户给定的会话摘要任务。",
                model=provider.get_model(settings.model),
                model_settings=ModelSettings(temperature=0.1),
                timeout_seconds=settings.timeout_seconds,
            ),
            prompt=prompt,
        )

    async def generate_chat_text(prompt: str) -> str:
        return await run_text_worker(
            settings=WorkerSettings(
                worker_id="web-chat", name="编辑助手", instructions=CHAT_SYSTEM_PROMPT,
                model=provider.get_model(settings.model), model_settings=ModelSettings(temperature=settings.temperature),
                timeout_seconds=settings.timeout_seconds,
            ), prompt=prompt,
        )
    resolved_progress = run_progress or RunProgressStore()
    resolved_sessions = sessions or ChatSessionStore(sessions_directory)
    resolved_memory_store = memory_store or JsonLongTermMemoryStore(
        memory_directory or settings.long_term_memory_directory
    )
    resolved_novels = novels or build_novel_service(
        settings,
        observer=NovelRunObserver(output=None, event_sink=resolved_progress.append),
    )
    memory_retriever = LongTermMemoryRetriever(
        generate_text=generate_memory_text,
        store=resolved_memory_store,
    )
    context_manager = SessionContextManager(
        sessions=resolved_sessions,
        generate_text=generate_context_text,
        memory_retriever=memory_retriever,
        policy=ContextPolicy(token_budget=settings.context_token_budget),
    )
    return ChatWorkspaceApplication(
        sessions=resolved_sessions,
        novels=resolved_novels,
        generate_chat_text=generate_chat_text,
        model_name=settings.model,
        context_manager=context_manager,
        memory_store=resolved_memory_store,
        memory_extractor=LongTermMemoryExtractor(
            generate_text=generate_memory_text,
            store=resolved_memory_store,
        ),
        memory_consolidator=LongTermMemoryConsolidator(
            generate_text=generate_memory_text,
            store=resolved_memory_store,
        ),
        run_progress=resolved_progress,
    )
