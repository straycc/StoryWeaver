"""Main Agent 轻量上下文的关键边界测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from storyweaver.api.main_agent_context import MainAgentContextBuilder, WorkflowContextReader
from storyweaver.application.models import ChatMessage, ChatSession
from storyweaver.persistence.creative_control import CreativeControl


class _Sessions:
    def __init__(self) -> None:
        self._summary = None
        self._events = ()

    def latest_summary(self, _session_id: str):
        return self._summary

    def list_events(self, _session_id: str):
        return self._events


class _Store:
    def load_project(self, _book_id: str):
        return SimpleNamespace(
            metadata=SimpleNamespace(title="测试作品", genre="武侠", target_chapters=12),
            state=SimpleNamespace(
                last_committed_chapter=3,
                current_location="客栈",
                current_time="入夜",
                hooks=(SimpleNamespace(status="open"), SimpleNamespace(status="resolved")),
            ),
        )

    def load_chapter_summaries(self, _book_id: str):
        return (SimpleNamespace(summary="第三章摘要：主角在客栈发现密信。"),)


class _Workspace:
    def __init__(self) -> None:
        self.sessions = _Sessions()
        self.novels = SimpleNamespace(store=_Store())


class _Jobs:
    def list_active(self, *, book_id=None):
        return (
            SimpleNamespace(job_id="current", job_type="session_action", status="running"),
            SimpleNamespace(job_id="other", job_type="prepare_chapter", status="running"),
        )


class _Proposals:
    def list_pending(self, *, session_id: str):
        return (SimpleNamespace(summary="确认当前候选计划并写作"),)


class _CreativeControls:
    def get(self, book_id: str):
        return CreativeControl(book_id, "保持江湖悬疑感", "下一章先不揭穿师父", "persistent", None, "now")


class _MemoryRetriever:
    def __init__(self, memories=()) -> None:
        self._memories = memories

    async def retrieve(self, *, query: str, book_id: str | None):
        del query, book_id
        return self._memories


class MainAgentContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.workspace = _Workspace()
        self.workflow = WorkflowContextReader(
            jobs=_Jobs(), proposals=_Proposals(), workspace=self.workspace,  # type: ignore[arg-type]
        )
        self.builder = MainAgentContextBuilder(
            workspace=self.workspace,  # type: ignore[arg-type]
            creative_controls=_CreativeControls(),  # type: ignore[arg-type]
            workflow=self.workflow,
            memory_retriever=_MemoryRetriever(),  # type: ignore[arg-type]
        )

    async def test_current_request_only_appears_once_and_is_not_history(self) -> None:
        session = ChatSession(
            session_id="session-1", title="测试", created_at="now", updated_at="now", book_id="book-1",
            messages=(
                ChatMessage("old-user", "user", "刚才的方向保持克制", "now", sequence=2),
                ChatMessage("old-assistant", "assistant", "我会保持克制。", "now", sequence=3),
                ChatMessage("current", "user", "按刚才那个计划写吧", "now", sequence=4),
            ),
        )

        package = await self.builder.build(
            session=session,
            current_request="按刚才那个计划写吧",
            current_sequence=4,
            current_job_id="current",
        )

        self.assertEqual(package.rendered_context.count("按刚才那个计划写吧"), 1)
        self.assertNotIn("session_action（running）", package.rendered_context)
        self.assertIn("prepare_chapter（running）", package.rendered_context)
        self.assertIn("作者意图：保持江湖悬疑感", package.rendered_context)
        self.assertIn("待确认操作：确认当前候选计划并写作", package.rendered_context)
        self.assertIn("request:current", package.trace.protected_source_ids)

    async def test_conversation_memory_is_traceable_but_not_creative_constraint(self) -> None:
        self.builder._memory_retriever = _MemoryRetriever((  # type: ignore[assignment]
            SimpleNamespace(
                memory_id="memory-1",
                description="偏好克制悬疑",
                content="讨论剧情时优先保留悬念，不急于揭晓真相。",
            ),
        ))
        session = ChatSession(
            session_id="session-1", title="测试", created_at="now", updated_at="now", book_id="book-1",
            messages=(ChatMessage("current", "user", "之后保持悬疑感", "now", sequence=1),),
        )

        package = await self.builder.build(
            session=session,
            current_request="之后保持悬疑感",
            current_sequence=1,
            current_job_id="current",
        )

        self.assertIn("会话记忆：偏好克制悬疑", package.rendered_context)
        self.assertEqual(package.trace.selected_memory_ids, ("memory-1",))
        self.assertNotIn(
            "conversation-memory:memory-1",
            package.trace.protected_source_ids,
        )


if __name__ == "__main__":
    unittest.main()
