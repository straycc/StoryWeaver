"""联网授权链路：请求体白名单、Job 透传、调研分支与记忆隔离。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from storyweaver.api import create_app
from storyweaver.api.action_surface import MainAgentActionSurface
from storyweaver.api.app import ALLOWED_RESEARCH_TOOLS, SessionMessageBody
from storyweaver.api.jobs import JobSupervisor, _JobLlmEventSink
from storyweaver.api.main_agent import ConversationDecision, MainAgent
from storyweaver.llm.events import LlmEvent, LlmEventType
from storyweaver.novel_creation.application import NovelApplicationSettings
from storyweaver.persistence import Database, DatabaseSettings
from storyweaver.research.config import ResearchSettings
from storyweaver.research.service import ResearchOutcome, ResearchService


class RequestPayloadTests(unittest.TestCase):
    def test_unknown_tool_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            SessionMessageBody(content="查一下", payload={"tools": ["shell"]})

    def test_duplicate_tools_are_collapsed(self) -> None:
        body = SessionMessageBody(
            content="查一下", payload={"tools": ["web_search", "web_search"]}
        )
        self.assertEqual(body.payload["tools"], ["web_search"])

    def test_authorized_id_matches_whitelist(self) -> None:
        self.assertEqual(ALLOWED_RESEARCH_TOOLS, frozenset({"web_search"}))

    def test_job_payload_filters_non_whitelisted_entries(self) -> None:
        self.assertEqual(
            JobSupervisor._research_tools({"research_tools": ["web_search", "shell"]}),
            ("web_search",),
        )
        self.assertEqual(JobSupervisor._research_tools({}), ())


class ApiBoundaryTests(unittest.TestCase):
    def test_message_endpoint_rejects_unknown_tool(self) -> None:
        """越权的工具名在 HTTP 边界就被拒绝，不会落到 Job。"""

        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite+pysqlite:///{Path(directory) / 'storyweaver.db'}"
            database = Database(DatabaseSettings(url))
            database.create_schema()
            database.dispose()
            app = create_app(
                settings=NovelApplicationSettings(
                    base_url="https://example.invalid/v1", model="test", api_key="test"
                ),
                database_url=url,
            )
            with TestClient(app) as client:
                session = client.post("/api/v1/sessions", json={}).json()
                response = client.post(
                    f"/api/v1/sessions/{session['session_id']}/messages",
                    json={
                        "content": "查一下",
                        "action": "chat",
                        "payload": {"tools": ["shell"]},
                    },
                )
                self.assertEqual(response.status_code, 422)


class _Sessions:
    def __init__(self) -> None:
        self.messages: list[SimpleNamespace] = []
        self.events: list[tuple[str, dict]] = []

    def load_session(self, _session_id: str) -> SimpleNamespace:
        return SimpleNamespace(book_id="book-1", messages=self.messages)

    def append_message(self, _session_id: str, *, role: str, content: str,
                       action: str, metadata: dict | None = None) -> SimpleNamespace:
        self.messages.append(SimpleNamespace(
            role=role, content=content, action=action, metadata=metadata,
            message_id=f"message-{len(self.messages) + 1}", sequence=len(self.messages) + 1,
        ))
        return self.load_session(_session_id)

    def append_event(self, _session_id: str, *, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


def _outcome() -> ResearchOutcome:
    return ResearchOutcome(
        status="complete",
        topic="宋代县衙",
        markdown="### 关于「宋代县衙」的资料整理\n\n- 设知县一人[来源 1]",
        searches=({"index": 1, "query": "宋代 县衙", "hit_count": 1, "status": "success"},),
        sources=({"index": 1, "title": "页面甲", "url": "https://a.test/1", "site": "a.test"},),
        elapsed_seconds=3.2,
    )


class ResearchBranchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.sessions = _Sessions()
        self.post_turn = AsyncMock()
        self.workspace = SimpleNamespace(
            sessions=self.sessions, post_main_agent_turn=self.post_turn
        )
        self.dispatcher = SimpleNamespace(
            dispatch=AsyncMock(),
            resolve_plan_id=lambda *_a, **_k: (_ for _ in ()).throw(ValueError("none")),
            data=lambda proposal: {"action_proposal_id": proposal.proposal_id},
        )
        self.proposals = SimpleNamespace(create=lambda **_: SimpleNamespace(proposal_id="p-1"))
        self.context_builder = SimpleNamespace(build=AsyncMock(return_value=SimpleNamespace(
            rendered_context="[book_digest]\n书名：灰城",
            trace_data=lambda: {"policy_version": "v1"},
        )))
        self.discussion = SimpleNamespace(
            discuss=AsyncMock(return_value=SimpleNamespace(reply="讨论回复", skill_resolution=None))
        )
        self.research_run = AsyncMock(return_value=_outcome())

    def _surface(self, decision: ConversationDecision, *, with_research: bool = True,
                 decide: AsyncMock | None = None) -> MainAgentActionSurface:
        factory = None
        if with_research:
            factory = lambda _sinks: SimpleNamespace(run=self.research_run)  # noqa: E731
        return MainAgentActionSurface(
            agent=SimpleNamespace(decide=decide or AsyncMock(return_value=decision)),
            proposals=self.proposals, dispatcher=self.dispatcher,
            workspace=self.workspace, context_builder=self.context_builder,
            creative_discussion=self.discussion,
            research_factory=factory,
        )

    async def test_unauthorized_turn_reaches_main_agent_as_false(self) -> None:
        decide = AsyncMock(return_value=ConversationDecision(kind="reply", reply="好"))
        surface = self._surface(ConversationDecision(kind="reply", reply="好"), decide=decide)
        await surface.handle(session_id="s-1", content="你好")
        self.assertIs(decide.await_args.kwargs["research_authorized"], False)

    async def test_authorized_turn_reaches_main_agent_as_true(self) -> None:
        decide = AsyncMock(return_value=ConversationDecision(kind="reply", reply="好"))
        surface = self._surface(ConversationDecision(kind="reply", reply="好"), decide=decide)
        await surface.handle(session_id="s-1", content="你好", research_tools=("web_search",))
        self.assertIs(decide.await_args.kwargs["research_authorized"], True)

    async def test_authorized_discussion_runs_research_and_records_metadata(self) -> None:
        decision = ConversationDecision(
            kind="action", action="creative_discussion",
            parameters={"objective": "宋代县衙的人员设置"},
        )
        surface = self._surface(decision)
        result = await surface.handle(
            session_id="s-1", content="查一下宋代县衙", research_tools=("web_search",),
        )
        self.assertEqual(result["kind"], "reply")
        self.assertIn("资料整理", result["reply"])
        self.research_run.assert_awaited_once()
        self.discussion.discuss.assert_not_awaited()
        assistant = self.sessions.messages[-1]
        self.assertEqual(assistant.metadata["research"]["status"], "complete")
        self.assertEqual(assistant.metadata["research"]["sources"][0]["url"], "https://a.test/1")

    async def test_unauthorized_discussion_never_calls_research(self) -> None:
        decision = ConversationDecision(
            kind="action", action="creative_discussion",
            parameters={"objective": "宋代县衙的人员设置"},
        )
        surface = self._surface(decision)
        await surface.handle(session_id="s-1", content="聊聊县衙")
        self.research_run.assert_not_awaited()
        self.discussion.discuss.assert_awaited_once()

    async def test_authorized_turn_without_research_service_degrades_to_discussion(self) -> None:
        decision = ConversationDecision(
            kind="action", action="creative_discussion",
            parameters={"objective": "宋代县衙的人员设置"},
        )
        surface = self._surface(decision, with_research=False)
        await surface.handle(
            session_id="s-1", content="查一下", research_tools=("web_search",),
        )
        self.discussion.discuss.assert_awaited_once()

    async def test_research_turn_skips_memory_extraction(self) -> None:
        decision = ConversationDecision(
            kind="action", action="creative_discussion",
            parameters={"objective": "宋代县衙的人员设置"},
        )
        surface = self._surface(decision)
        with patch(
            "storyweaver.api.action_surface.asyncio.create_task",
            side_effect=lambda coro: coro.close(),
        ) as create_task:
            await surface.handle(
                session_id="s-1", content="查一下", research_tools=("web_search",),
            )
        create_task.assert_not_called()
        self.post_turn.assert_not_awaited()

    async def test_plain_discussion_still_triggers_memory_extraction(self) -> None:
        decision = ConversationDecision(
            kind="action", action="creative_discussion",
            parameters={"objective": "人物动机"},
        )
        surface = self._surface(decision)
        with patch(
            "storyweaver.api.action_surface.asyncio.create_task",
            side_effect=lambda coro: coro.close(),
        ) as create_task:
            await surface.handle(session_id="s-1", content="聊聊人物动机")
        create_task.assert_called_once()


class MainAgentInstructionTests(unittest.IsolatedAsyncioTestCase):
    def _agent(self) -> MainAgent:
        settings = NovelApplicationSettings(
            base_url="http://localhost:11434/v1", model="test"
        )
        return MainAgent(settings, model=object())

    async def test_authorized_instruction_mentions_search(self) -> None:
        decision = ConversationDecision(kind="reply", reply="好")
        with patch(
            "storyweaver.api.main_agent.run_structured_worker",
            new_callable=AsyncMock, return_value=decision,
        ) as worker:
            await self._agent().decide(context="x", research_authorized=True)
        instructions = worker.await_args.kwargs["settings"].instructions
        self.assertIn("联网搜索", instructions)
        self.assertIn("creative_discussion", instructions)

    async def test_unauthorized_instruction_guides_user_to_toggle(self) -> None:
        decision = ConversationDecision(kind="reply", reply="好")
        with patch(
            "storyweaver.api.main_agent.run_structured_worker",
            new_callable=AsyncMock, return_value=decision,
        ) as worker:
            await self._agent().decide(context="x", research_authorized=False)
        instructions = worker.await_args.kwargs["settings"].instructions
        self.assertIn("没有开启联网搜索", instructions)
        self.assertIn("不要声称自己已经搜索过", instructions)


class ResearchProgressEventTests(unittest.IsolatedAsyncioTestCase):
    """进行中反馈：阶段切换与逐次检索结果必须投影成 Job 事件。"""

    async def test_phase_and_search_events_reach_job_stream(self) -> None:
        recorded: list[tuple[str, dict]] = []

        class _Jobs:
            def append_event(self, _job_id: str, name: str, payload=None) -> None:
                recorded.append((name, dict(payload or {})))

        sink = _JobLlmEventSink(SimpleNamespace(), _Jobs(), "job-1")
        await sink.on_event(
            LlmEvent(
                LlmEventType.PHASE_STARTED,
                "creative-research",
                {"phase": "report", "search_count": 2},
            )
        )
        await sink.on_event(
            LlmEvent(
                LlmEventType.TOOL_COMPLETED,
                "creative-research",
                {
                    "tool_name": "web_search",
                    "query": "宋代 官制",
                    "status": "success",
                    "hit_count": 1,
                    "hits": [{"title": "页面甲", "url": "https://a.test/1", "site": "a.test"}],
                },
            )
        )
        self.assertEqual([name for name, _ in recorded], ["research_phase", "research_search_completed"])
        self.assertEqual(recorded[0][1]["phase"], "report")
        self.assertEqual(recorded[1][1]["query"], "宋代 官制")
        self.assertEqual(recorded[1][1]["hits"][0]["url"], "https://a.test/1")

    async def test_service_emits_phase_before_each_stage(self) -> None:
        class _Sink:
            def __init__(self) -> None:
                self.events: list[LlmEvent] = []

            async def on_event(self, event: LlmEvent) -> None:
                self.events.append(event)

        sink = _Sink()
        service = ResearchService(
            settings=ResearchSettings.from_env(),
            worker_settings=SimpleNamespace(worker_id="creative-research"),
            event_sinks=(sink,),
        )
        await service._phase("search", 0)
        await service._phase("report", 3)
        self.assertEqual(
            [event.data["phase"] for event in sink.events], ["search", "report"]
        )
        self.assertEqual(sink.events[1].data["search_count"], 3)
        self.assertEqual(sink.events[0].type, LlmEventType.PHASE_STARTED)


if __name__ == "__main__":
    unittest.main()
