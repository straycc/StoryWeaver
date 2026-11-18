"""MainAgent 参数修复和自然语言动作关口的回归测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from storyweaver.api.action_surface import ActionDispatcher, MainAgentActionSurface
from storyweaver.api.main_agent import ConversationDecision, MainAgent
from storyweaver.novel_creation.application import NovelApplicationSettings


class MainAgentDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_parameters_receive_one_targeted_repair(self) -> None:
        settings = NovelApplicationSettings(base_url="http://localhost:11434/v1", model="test")
        agent = MainAgent(settings, model=object())
        invalid = ConversationDecision(
            kind="query", action="query_chapter", parameters={"chapter": 3},
        )
        valid = ConversationDecision(
            kind="query", action="query_chapter", parameters={"chapter_number": 3},
        )
        with patch(
            "storyweaver.api.main_agent.run_structured_worker",
            new_callable=AsyncMock,
            side_effect=[invalid, valid],
        ) as worker:
            decision = await agent.decide(context="[current_request]\n第三章摘要")
        self.assertEqual(worker.await_count, 2)
        self.assertIn("chapter_number", worker.await_args_list[1].kwargs["settings"].instructions)
        self.assertIn("Extra inputs", worker.await_args_list[1].kwargs["prompt"])
        self.assertEqual(decision.parameters["chapter_number"], 3)


class _Sessions:
    def __init__(self) -> None:
        self.messages: list[SimpleNamespace] = []
        self.events: list[tuple[str, dict]] = []

    def load_session(self, _session_id: str) -> SimpleNamespace:
        return SimpleNamespace(book_id="book-1", messages=self.messages)

    def append_message(self, _session_id: str, *, role: str, content: str,
                       action: str, metadata: dict | None = None) -> SimpleNamespace:
        self.messages.append(SimpleNamespace(
            role=role, content=content, action=action,
            message_id=f"message-{len(self.messages) + 1}", sequence=len(self.messages) + 1,
        ))
        return self.load_session(_session_id)

    def append_event(self, _session_id: str, *, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


class MainAgentActionGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.sessions = _Sessions()
        self.workspace = SimpleNamespace(sessions=self.sessions)
        self.dispatcher = SimpleNamespace(
            dispatch=AsyncMock(),
            resolve_plan_id=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ValueError("当前没有可确认的候选章节计划")
            ),
            data=lambda proposal: {"action_proposal_id": proposal.proposal_id},
        )
        self.proposals = SimpleNamespace(
            create=lambda **_kwargs: SimpleNamespace(proposal_id="proposal-1")
        )
        self.context_builder = SimpleNamespace(build=AsyncMock(return_value=SimpleNamespace(
            rendered_context="[book_digest]\n书名：灰城",
            trace_data=lambda: None,
        )))

    def surface(self, decision: ConversationDecision) -> MainAgentActionSurface:
        return MainAgentActionSurface(
            agent=SimpleNamespace(decide=AsyncMock(return_value=decision)),
            proposals=self.proposals, dispatcher=self.dispatcher,
            workspace=self.workspace, context_builder=self.context_builder,
        )

    async def test_approve_without_pending_plan_does_not_dispatch(self) -> None:
        result = await self.surface(ConversationDecision(
            kind="action", action="approve_chapter_plan",
        )).handle(session_id="session-1", content="批准刚才的计划")
        self.assertEqual(result["kind"], "clarify")
        self.dispatcher.dispatch.assert_not_awaited()

    async def test_natural_language_workflow_requires_confirmation(self) -> None:
        result = await self.surface(ConversationDecision(
            kind="action", action="run_next_chapter_workflow",
        )).handle(session_id="session-1", content="下一章吧")
        self.assertEqual(result["kind"], "proposal")
        self.assertEqual(self.sessions.events[0][0], "action_proposal_pending")
        self.dispatcher.dispatch.assert_not_awaited()

    async def test_confirmed_workflow_proposal_reaches_dispatcher(self) -> None:
        proposal = SimpleNamespace(
            proposal_id="proposal-1", session_id="session-1", book_id="book-1",
            action_type="run_next_chapter_workflow", payload={"instruction": "写下一章"},
            status="pending",
        )
        proposals = SimpleNamespace(
            get=lambda _proposal_id: proposal,
            confirm=lambda _proposal_id, *, job_id: proposal,
        )
        dispatcher = ActionDispatcher(
            proposals=proposals, jobs=SimpleNamespace(), workspace=self.workspace,
            submit_job=AsyncMock(),
        )
        dispatcher.dispatch = AsyncMock(return_value=SimpleNamespace(job_id="job-1"))
        confirmed = await dispatcher.confirm("proposal-1")
        self.assertIs(confirmed, proposal)
        self.assertEqual(
            dispatcher.dispatch.await_args.kwargs["action"],
            "run_next_chapter_workflow",
        )
        self.assertEqual(
            dispatcher.dispatch.await_args.kwargs["parameters"],
            {"instruction": "写下一章"},
        )


if __name__ == "__main__":
    unittest.main()
