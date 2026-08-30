"""Main Agent、ActionProposal 与 Job 的应用层边界。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
import asyncio
from dataclasses import asdict
import re
from typing import Any

from ..persistence import (
    ActionProposal,
    ActionProposalRepository,
    ContextSnapshotRepository,
    JobRepository,
)
from ..application.workspace import ChatWorkspaceApplication
from .main_agent import ConversationDecision, MainAgent
from .main_agent_context import MainAgentContextBuilder
from .capabilities import get_capability, validate_capability_parameters
from .creative_discussion import CreativeDiscussionService
from ..skills import (
    CreativeTaskContext,
    SkillRegistry,
    SkillService,
    creative_task_from_data,
    creative_task_to_data,
)
from ..llm import LlmEventSink


SubmitJob = Callable[..., Awaitable[Any]]


def should_route_explicit_skill_reply(
    decision: ConversationDecision,
    creative_task: CreativeTaskContext | None,
) -> bool:
    """显式选择 Skill 的普通回复不得绕过专业创作能力。"""

    return bool(
        creative_task is not None
        and creative_task.applied_skills
        and decision.kind == "reply"
        and decision.action is None
    )


class ActionDispatcher:
    """校验、路由与建 Job；这里绝不直接调用 Pipeline。"""

    def __init__(self, *, proposals: ActionProposalRepository, jobs: JobRepository,
                 workspace: ChatWorkspaceApplication, submit_job: SubmitJob) -> None:
        self._proposals = proposals
        self._jobs = jobs
        self._workspace = workspace
        self._submit_job = submit_job
        self._skills: SkillRegistry | None = None
        self._skill_service: SkillService | None = None

    def configure_skills(
        self,
        skills: SkillRegistry,
        skill_service: SkillService,
    ) -> None:
        self._skills = skills
        self._skill_service = skill_service

    def available_skills(self) -> tuple[object, ...]:
        """返回适合创作门户展示的项目级 Skill。

        本机 Agent 的开发辅助 Skill 仍可被注册表发现，但不应混入作者的
        斜杠命令菜单，避免把工具发现、方案质询等能力误展示为写作方法。
        """
        if self._skills is None:
            return ()
        return tuple(item for item in self._skills.list() if item.source == "builtin")

    async def dispatch(self, *, action: str, session_id: str, book_id: str | None,
                       parameters: Mapping[str, Any], action_proposal_id: str | None = None,
                       creative_task: CreativeTaskContext | None = None) -> Any:
        # 所有入口先落到统一 DTO，杜绝按钮和自然语言绕开参数校验。
        normalized_parameters = dict(parameters)
        # 旧批次弹窗仍使用 chapter_count；在边界处一次性兼容，内部只认 count。
        if action == "write_batch" and "count" not in normalized_parameters:
            normalized_parameters["count"] = normalized_parameters.pop("chapter_count", None)
        parameters = validate_capability_parameters(action, normalized_parameters)
        if action == "prepare_chapter_plan":
            instruction = self._text(parameters.get("instruction"))
            return await self._submit_job(job_type="prepare_chapter", book_id=self._require_book(book_id), payload={
                "user_instruction": instruction,
                "creative_task_context": self._snapshot_or_activate(
                    creative_task=creative_task,
                    request=instruction or "规划下一章",
                    skill_ids=parameters.get("skill_ids"),
                ),
                "session_id": session_id,
            })
        if action == "run_next_chapter_workflow":
            instruction = self._text(parameters.get("instruction"))
            return await self._submit_job(
                job_type="prepare_chapter",
                book_id=self._require_book(book_id),
                payload={
                    "user_instruction": instruction,
                    "creative_task_context": self._snapshot_or_activate(
                        creative_task=creative_task,
                        request=instruction or "启动下一章完整工作流",
                        skill_ids=parameters.get("skill_ids"),
                    ),
                    # Proposal 本身就是暂停与恢复边界；该标记用于观测最初
                    # 是单节点规划还是完整工作流请求。
                    "workflow_mode": "run_next_chapter_workflow",
                    "session_id": session_id,
                },
            )
        if action == "revise_chapter_plan":
            proposal_id = self.resolve_plan_id(book_id, parameters.get("proposal_id"), session_id)
            return await self._submit_job(job_type="revise_plan", book_id=self._require_book(book_id), payload={
                "proposal_id": proposal_id, "feedback": self._required_text(parameters.get("feedback"), "feedback"),
                "session_id": session_id,
            })
        if action == "confirm_and_write_chapter":
            proposal_id = self.resolve_plan_id(book_id, parameters.get("proposal_id"), session_id)
            return await self._submit_job(job_type="confirm_plan", book_id=self._require_book(book_id), payload={
                "proposal_id": proposal_id, "session_id": session_id,
                **({"action_proposal_id": action_proposal_id} if action_proposal_id else {}),
            })
        if action == "approve_chapter_plan":
            proposal_id = self.resolve_plan_id(
                book_id, parameters.get("proposal_id"), session_id,
                allowed_statuses=frozenset({"pending"}),
            )
            return await self._submit_job(
                job_type="approve_plan",
                book_id=self._require_book(book_id),
                payload={"proposal_id": proposal_id, "session_id": session_id},
            )
        if action == "write_from_plan":
            proposal_id = self.resolve_plan_id(
                book_id, parameters.get("proposal_id"), session_id,
                allowed_statuses=frozenset({"approved"}),
            )
            return await self._submit_job(
                job_type="write_from_plan",
                book_id=self._require_book(book_id),
                payload={
                    "proposal_id": proposal_id,
                    "session_id": session_id,
                    **(
                        {"action_proposal_id": action_proposal_id}
                        if action_proposal_id
                        else {}
                    ),
                },
            )
        if action == "rewrite_chapter":
            chapter_number = int(parameters.get("chapter_number") or 0)
            if chapter_number < 1:
                raise ValueError("重写操作必须指定有效章节号")
            instruction = self._text(parameters.get("instruction"))
            return await self._submit_job(job_type="rewrite_chapter", book_id=self._require_book(book_id), payload={
                "chapter_number": chapter_number, "user_instruction": instruction,
                "creative_task_context": self._snapshot_or_activate(
                    creative_task=creative_task,
                    request=instruction or f"重写第 {chapter_number} 章",
                    skill_ids=parameters.get("skill_ids"),
                ),
                "session_id": session_id, **({"action_proposal_id": action_proposal_id} if action_proposal_id else {}),
            })
        if action == "cancel_chapter_plan":
            proposal_id = self.resolve_plan_id(
                book_id,
                parameters.get("proposal_id"),
                session_id,
                allowed_statuses=frozenset({"pending", "approved"}),
            )
            return await self._submit_job(job_type="cancel_plan", book_id=self._require_book(book_id), payload={
                "proposal_id": proposal_id, "session_id": session_id,
            })
        if action == "write_batch":
            count = int(parameters.get("count") or parameters.get("chapter_count") or 0)
            if count < 1:
                raise ValueError("连续创作必须指定正整数章节数")
            instruction = self._text(parameters.get("instruction"))
            return await self._submit_job(job_type="write_batch", book_id=self._require_book(book_id), payload={
                "count": count, "user_instruction": instruction,
                "creative_task_context": self._snapshot_or_activate(
                    creative_task=creative_task,
                    request=instruction or f"连续创作 {count} 章",
                    skill_ids=parameters.get("skill_ids"),
                ),
                "session_id": session_id,
            })
        if action == "create_novel":
            frozen_task = self._snapshot_or_activate(
                creative_task=creative_task,
                request=str(parameters.get("premise") or "创建小说"),
                skill_ids=(),
            )
            return await self._submit_job(
                job_type="session_action",
                book_id=None,
                payload={
                    "session_id": session_id,
                    "content": "确认根据上述创作简报创建作品",
                    "action": "create_novel",
                    "book_id": None,
                    "action_payload": {
                        **dict(parameters),
                        "creative_task_context": frozen_task,
                    },
                    **(
                        {"action_proposal_id": action_proposal_id}
                        if action_proposal_id
                        else {}
                    ),
                },
            )
        raise ValueError(f"不支持的业务动作：{action}")

    async def confirm(self, proposal_id: str) -> ActionProposal:
        proposal = self._proposals.get(proposal_id)
        if proposal.status != "pending":
            raise ValueError(f"该操作当前为 {proposal.status}，不能确认")
        self._assert_not_expired(proposal)
        parameters = dict(proposal.payload)
        frozen_task_data = parameters.pop("creative_task_context", None)
        frozen_task = (
            creative_task_from_data(frozen_task_data)
            if frozen_task_data is not None
            else None
        )
        job = await self.dispatch(
            action=proposal.action_type, session_id=proposal.session_id, book_id=proposal.book_id,
            parameters=parameters, action_proposal_id=proposal.proposal_id,
            creative_task=frozen_task,
        )
        return self._proposals.confirm(proposal.proposal_id, job_id=job.job_id)

    def cancel(self, proposal_id: str) -> ActionProposal:
        proposal = self._proposals.cancel(proposal_id)
        self._workspace.sessions.append_event(proposal.session_id, event_type="action_proposal_cancelled", payload=self.data(proposal))
        return proposal

    def _assert_not_expired(self, proposal: ActionProposal) -> None:
        if proposal.action_type not in {"confirm_and_write_chapter", "write_from_plan"}:
            return
        try:
            self.resolve_plan_id(
                proposal.book_id,
                proposal.payload.get("proposal_id"),
                proposal.session_id,
                allowed_statuses=(
                    frozenset({"approved"})
                    if proposal.action_type == "write_from_plan"
                    else frozenset({"pending"})
                ),
            )
        except (KeyError, ValueError):
            self._proposals.expire(proposal.proposal_id)
            raise ValueError("关联章节计划已失效，不能确认旧操作")

    def resolve_plan_id(
        self,
        book_id: str | None,
        value: object,
        session_id: str,
        *,
        allowed_statuses: frozenset[str] = frozenset({"pending"}),
    ) -> str:
        candidate = self._text(value)
        if not candidate:
            candidate = self._latest_plan_id(session_id, allowed_statuses)
        if not candidate:
            raise ValueError("当前没有可确认的候选章节计划，请先生成计划")
        proposal = self._workspace.novels.load_chapter_plan_proposal(book_id=self._require_book(book_id), proposal_id=candidate)
        if proposal.status not in allowed_statuses:
            expected = "、".join(sorted(allowed_statuses))
            raise ValueError(f"候选章节计划状态为 {proposal.status}，需要状态：{expected}")
        return candidate

    def _latest_plan_id(
        self,
        session_id: str,
        allowed_statuses: frozenset[str],
    ) -> str | None:
        for event in reversed(self._workspace.sessions.list_events(session_id)):
            if (
                event.event_type.startswith("chapter_plan_")
                and event.payload.get("status") in allowed_statuses
            ):
                value = event.payload.get("proposal_id")
                if value:
                    return str(value)
        return None

    @staticmethod
    def data(proposal: ActionProposal) -> dict[str, Any]:
        payload = dict(proposal.payload)
        frozen_task_data = payload.pop("creative_task_context", None)
        applied_skills: list[dict[str, str]] = []
        if frozen_task_data is not None:
            task = creative_task_from_data(frozen_task_data)
            applied_skills = [
                {"skill_id": item.skill_id, "content_hash": item.content_hash}
                for item in task.applied_skills
            ]
        return {"action_proposal_id": proposal.proposal_id, "session_id": proposal.session_id,
                "book_id": proposal.book_id, "action_type": proposal.action_type,
                "payload": payload, "applied_skills": applied_skills,
                "summary": proposal.summary, "status": proposal.status,
                "job_id": proposal.job_id, "created_at": proposal.created_at,
                "confirmed_at": proposal.confirmed_at,
                "expires_at": proposal.expires_at, "updated_at": proposal.updated_at}

    @staticmethod
    def _require_book(book_id: str | None) -> str:
        if not book_id:
            raise ValueError("请先在当前会话绑定一部作品")
        return book_id

    @staticmethod
    def _text(value: object) -> str | None:
        value = str(value).strip() if value is not None else ""
        return value or None

    @classmethod
    def _required_text(cls, value: object, field: str) -> str:
        text = cls._text(value)
        if not text:
            raise ValueError(f"{field} 不能为空")
        return text

    def _activate(self, *, request: str, skill_ids: object) -> dict[str, object]:
        if self._skill_service is None:
            raise RuntimeError("SkillService 尚未配置")
        ids = tuple(str(item) for item in skill_ids) if isinstance(skill_ids, list) else ()
        try:
            return creative_task_to_data(
                self._skill_service.activate(request=request, skill_ids=ids)
            )
        except KeyError as exc:
            # Main Agent Action 与直接 API 使用同一种可展示参数错误。
            raise ValueError(str(exc)) from exc

    def _snapshot_or_activate(
        self,
        *,
        creative_task: CreativeTaskContext | None,
        request: str,
        skill_ids: object,
    ) -> dict[str, object]:
        """优先沿用上游 Job Snapshot；只有入口尚未冻结时才 Activate。"""

        if creative_task is not None:
            return creative_task_to_data(creative_task)
        return self._activate(request=request, skill_ids=skill_ids)


class MainAgentActionSurface:
    """把自然语言转换为查询回复、候选 Job 或待确认动作。"""

    def __init__(self, *, agent: MainAgent, proposals: ActionProposalRepository,
                 dispatcher: ActionDispatcher, workspace: ChatWorkspaceApplication,
                 context_builder: MainAgentContextBuilder,
                 creative_discussion: CreativeDiscussionService | None = None,
                 context_snapshots: ContextSnapshotRepository | None = None) -> None:
        self._agent = agent
        self._proposals = proposals
        self._dispatcher = dispatcher
        self._workspace = workspace
        self._context_builder = context_builder
        self._creative_discussion = creative_discussion
        self._context_snapshots = context_snapshots

    async def handle(self, *, session_id: str, content: str,
                     current_job_id: str | None = None,
                     creative_task: CreativeTaskContext | None = None,
                     event_sinks: Sequence[LlmEventSink] = ()) -> dict[str, Any]:
        session = self._workspace.sessions.load_session(session_id)
        # 用户输入先持久化；即使意图模型暂时失败，也不会丢失这次会话事实。
        session = self._workspace.sessions.append_message(
            session_id, role="user", content=content, action="chat",
        )
        user_message = session.messages[-1]
        slash = self._slash_skill_command(content)
        if slash == "list":
            skills = self._dispatcher.available_skills()
            reply = "当前没有可用 Skill。" if not skills else "可用 Skill：\n" + "\n".join(
                f"- {item.display_name} (`{item.skill_id}`)："
                f"{item.short_description}"
                for item in skills
            ) + "\n\n使用：`/skill <skill-id> <创作要求>`"
            self._reply(session_id, reply, user_message=user_message)
            return {"kind": "reply", "reply": reply}
        if isinstance(slash, tuple):
            skill_id, instruction = slash
            if not session.book_id:
                reply = "请先在当前会话绑定一部作品，再使用 Skill 生成章节计划。"
                self._reply(session_id, reply, user_message=user_message)
                return {"kind": "clarify", "reply": reply}
            try:
                job = await self._dispatcher.dispatch(
                    action="prepare_chapter_plan", session_id=session_id, book_id=session.book_id,
                    parameters={"instruction": instruction, "skill_ids": [skill_id]},
                )
            except ValueError as exc:
                reply = f"Skill 命令无效：{exc}"
                self._reply(session_id, reply, user_message=user_message)
                return {"kind": "clarify", "reply": reply}
            reply = f"已启用 Skill `{skill_id}`，正在生成候选章节计划。"
            self._reply(session_id, reply, user_message=user_message)
            return {"kind": "job", "job_id": job.job_id, "reply": reply}
        # 明确的中文查询命令不值得交给模型猜参数；先确定性解析，模糊表达再交 Main Agent。
        explicit = self._explicit_query_decision(content)
        context_trace: dict[str, Any] | None = None
        rendered_context = ""
        if explicit is not None:
            decision = explicit
        else:
            package = await self._context_builder.build(
                session=session,
                current_request=content,
                current_sequence=user_message.sequence,
                current_job_id=current_job_id,
                creative_task=creative_task,
            )
            context_trace = package.trace_data()
            rendered_context = package.rendered_context
            if self._context_snapshots is not None:
                self._context_snapshots.save(
                    agent_role="main_agent",
                    book_id=session.book_id,
                    book_version=package.trace.book_version,
                    policy_version=package.trace.policy_version,
                    renderer_version=package.trace.renderer_version,
                    rendered_context=package.rendered_context,
                    trace=package.trace_data(),
                    job_id=current_job_id,
                )
            decision = await self._agent.decide(
                context=package.rendered_context, event_sinks=event_sinks,
            )
        if decision.action:
            try:
                decision.parameters = validate_capability_parameters(decision.action, decision.parameters)
            except ValueError as exc:
                reply = f"我理解了你的意图，但参数还不完整：{exc}"
                self._reply(session_id, reply,
                            user_message=user_message, context_trace=context_trace)
                return {"kind": "clarify", "reply": reply}
        if decision.kind == "query":
            reply = self._query(session.session_id, session.book_id, decision)
            self._reply(session_id, reply,
                        user_message=user_message, context_trace=context_trace)
            return {"kind": "query", "reply": reply}
        if decision.kind == "action":
            capability = get_capability(str(decision.action))
            if capability.requires_book and not session.book_id:
                self._reply(session_id, "请先在当前会话绑定一部作品，再执行这项创作能力。",
                            user_message=user_message, context_trace=context_trace)
                return {"kind": "clarify"}
            if decision.action == "creative_discussion":
                if self._creative_discussion is None:
                    raise RuntimeError("专业创作讨论能力尚未配置")
                discussion = await self._creative_discussion.discuss(
                    context=rendered_context,
                    objective=str(decision.parameters["objective"]),
                    creative_task=creative_task,
                    event_sinks=event_sinks,
                )
                reply = discussion.reply
                if discussion.skill_resolution is not None:
                    context_trace = {
                        **(context_trace or {}),
                        "skill_resolution": discussion.skill_resolution,
                    }
                self._reply(session_id, reply, user_message=user_message,
                            context_trace=context_trace)
                return {"kind": "reply", "reply": reply}
            if decision.action == "create_novel":
                if session.book_id:
                    reply = "当前会话已经绑定作品。请新建一个会话，再根据新书设定创建另一部作品。"
                    self._reply(session_id, reply, user_message=user_message,
                                context_trace=context_trace)
                    return {"kind": "clarify", "reply": reply}
                frozen_parameters = dict(decision.parameters)
                if creative_task is not None:
                    frozen_parameters["creative_task_context"] = creative_task_to_data(
                        creative_task
                    )
                proposal = self._proposals.create(
                    session_id=session_id,
                    book_id=None,
                    action_type="create_novel",
                    payload=frozen_parameters,
                    summary=f"创建小说《{decision.parameters['title']}》",
                )
                self._workspace.sessions.append_event(
                    session_id,
                    event_type="action_proposal_pending",
                    payload=self._dispatcher.data(proposal),
                )
                reply = f"我已把上述讨论整理成《{decision.parameters['title']}》的创作简报，请确认后创建作品。"
                self._reply(session_id, reply, user_message=user_message,
                            context_trace=context_trace)
                return {"kind": "proposal", "proposal": self._dispatcher.data(proposal)}
            if decision.action in {"prepare_chapter_plan", "revise_chapter_plan"}:
                job = await self._dispatcher.dispatch(action=str(decision.action), session_id=session_id,
                                                      book_id=session.book_id, parameters=decision.parameters,
                                                      creative_task=creative_task)
                reply = "已开始生成候选章节计划。完成后请检查计划卡片，再确认是否写入正文。"
                self._reply(session_id, reply,
                            user_message=user_message, context_trace=context_trace)
                return {"kind": "job", "job_id": job.job_id, "reply": reply}
            if decision.action in {"cancel_chapter_plan", "approve_chapter_plan"}:
                job = await self._dispatcher.dispatch(
                    action=str(decision.action), session_id=session_id,
                    book_id=session.book_id, parameters=decision.parameters,
                )
                reply = (
                    "候选计划已进入批准流程；正文不会自动生成。"
                    if decision.action == "approve_chapter_plan"
                    else "已开始取消当前候选章节计划。"
                )
                self._reply(session_id, reply, user_message=user_message,
                            context_trace=context_trace)
                return {"kind": "job", "job_id": job.job_id, "reply": reply}
            if decision.action == "run_next_chapter_workflow":
                job = await self._dispatcher.dispatch(
                    action="run_next_chapter_workflow", session_id=session_id,
                    book_id=session.book_id, parameters=decision.parameters,
                    creative_task=creative_task,
                )
                reply = "完整章节工作流已启动；我会先生成候选计划，并在计划确认点暂停。"
                self._reply(session_id, reply, user_message=user_message,
                            context_trace=context_trace)
                return {"kind": "job", "job_id": job.job_id, "reply": reply}
            if decision.action in {"confirm_and_write_chapter", "write_from_plan", "rewrite_chapter", "write_batch"}:
                frozen_parameters = dict(decision.parameters)
                if decision.action in {"confirm_and_write_chapter", "write_from_plan"}:
                    # 创建时即解析并冻结候选计划，确认时还会再次检查其仍为 pending。
                    try:
                        frozen_parameters["proposal_id"] = self._dispatcher.resolve_plan_id(
                            session.book_id, frozen_parameters.get("proposal_id"), session_id,
                            allowed_statuses=(
                                frozenset({"approved"})
                                if decision.action == "write_from_plan"
                                else frozenset({"pending"})
                            ),
                        )
                    except ValueError:
                        reply = "当前没有可确认的候选章节计划。你可以先说“生成下一章计划”，或继续讨论创作方向。"
                        self._reply(session_id, reply,
                                    user_message=user_message, context_trace=context_trace)
                        return {"kind": "clarify", "reply": reply}
                elif creative_task is not None:
                    # 重写确认跨越 ActionProposal，必须携带 Chat Job 已冻结的 Snapshot。
                    frozen_parameters["creative_task_context"] = (
                        creative_task_to_data(creative_task)
                    )
                proposal = self._proposals.create(session_id=session_id, book_id=session.book_id,
                    action_type=str(decision.action), payload=frozen_parameters,
                    summary=self._action_summary(decision, session.book_id))
                self._workspace.sessions.append_event(session_id, event_type="action_proposal_pending", payload=self._dispatcher.data(proposal))
                self._reply(session_id, f"我已准备好执行：{proposal.summary}\n请在下方确认后再开始。",
                            user_message=user_message, context_trace=context_trace)
                return {"kind": "proposal", "proposal": self._dispatcher.data(proposal)}
        if should_route_explicit_skill_reply(decision, creative_task):
            if self._creative_discussion is None:
                raise RuntimeError("专业创作讨论能力尚未配置")
            discussion = await self._creative_discussion.discuss(
                context=rendered_context,
                objective=content,
                creative_task=creative_task,
                event_sinks=event_sinks,
            )
            if discussion.skill_resolution is not None:
                context_trace = {
                    **(context_trace or {}),
                    "skill_resolution": discussion.skill_resolution,
                }
            self._reply(
                session_id,
                discussion.reply,
                user_message=user_message,
                context_trace=context_trace,
            )
            return {"kind": "reply", "reply": discussion.reply}
        reply = decision.reply.strip() or "我理解了。你可以继续说明希望推进的人物、冲突或章节目标。"
        self._reply(session_id, reply,
                    user_message=user_message, context_trace=context_trace)
        return {"kind": decision.kind, "reply": reply}

    def _query(self, session_id: str, book_id: str | None, decision: ConversationDecision) -> str:
        if not book_id:
            return "当前会话未绑定作品，请先选择作品后再查询人物、伏笔或审稿结果。"
        project = self._workspace.novels.store.load_project(book_id)
        action = decision.action
        query = str(decision.parameters.get("query") or decision.parameters.get("name") or "").strip()
        if action == "query_book_state":
            return self._book_state_reply(project)
        if action == "query_story_progress":
            return self._story_progress_reply(project, book_id)
        if action == "query_completed_chapters":
            return self._completed_chapters_reply(book_id, decision.parameters.get("limit"))
        if action == "query_chapter":
            return self._chapter_reply(project, book_id, decision.parameters)
        if action == "query_pending_plan":
            return self._pending_plan_reply(session_id, book_id)
        if action == "query_recent_review":
            return self._review_reply(project, book_id, decision.parameters.get("chapter_number"))
        if action == "query_open_foreshadowings":
            return self._open_hooks_reply(project, decision.parameters.get("limit"))
        if action == "query_character":
            names = {item.character_id: item.name for item in project.foundation.characters}
            matches = [item for item in project.state.characters if not query or query in item.character_id or query in names.get(item.character_id, "")]
            if not matches:
                return "没有找到匹配人物。请提供人物名称或更准确的角色标识。"
            return "\n".join(f"{names.get(item.character_id, item.character_id)}：{item.status}，位于{item.location}，当前目标：{item.current_goal}" for item in matches[:5])
        if action == "query_foreshadowing":
            matches = [item for item in project.state.hooks if not query or query in item.name or query in item.description]
            if not matches:
                return "没有找到匹配伏笔。"
            return "\n".join(f"{item.name}：{item.status}。{item.description}" for item in matches[:8])
        if action == "explain_review":
            return self._review_reply(project, book_id, decision.parameters.get("chapter_number"))
        return "我暂时无法执行这项查询。"

    def _book_state_reply(self, project: Any) -> str:
        state, metadata = project.state, project.metadata
        return (
            f"《{metadata.title}》当前已提交第 {state.last_committed_chapter}/{metadata.target_chapters} 章；"
            f"位置：{state.current_location}；时间：{state.current_time}；"
            f"活跃伏笔：{sum(item.status != 'resolved' for item in state.hooks)} 条。"
        )

    def _story_progress_reply(self, project: Any, book_id: str) -> str:
        state, metadata = project.state, project.metadata
        completed = state.last_committed_chapter
        remaining = max(0, metadata.target_chapters - completed)
        summaries = self._workspace.novels.store.load_chapter_summaries(book_id)
        latest = summaries[-1].summary if summaries else "尚未开始正文。"
        return (
            f"《{metadata.title}》进度：{completed}/{metadata.target_chapters} 章，"
            f"约 {completed / metadata.target_chapters:.0%}；剩余 {remaining} 章。\n"
            f"当前状态：{state.current_location}，{state.current_time}。\n"
            f"最近章节摘要：{latest}\n"
            "系统不虚构主线或收尾完成度；如需判断故事结构，请让我基于章节摘要和伏笔另行分析。"
        )

    def _completed_chapters_reply(self, book_id: str, limit_value: object) -> str:
        limit = self._limit(limit_value, default=20, maximum=50)
        chapters = self._workspace.novels.store.load_chapter_index(book_id)
        if not chapters:
            return "当前还没有已提交章节。"
        selected = chapters[-limit:]
        lines = [f"已提交章节共 {len(chapters)} 章（显示最近 {len(selected)} 章）："]
        lines.extend(f"第 {item.chapter_number} 章《{item.title}》· {item.word_count} 字 · {self._chapter_status_label(item.status)}" for item in selected)
        return "\n".join(lines)

    def _chapter_reply(self, project: Any, book_id: str, parameters: Mapping[str, Any]) -> str:
        last = project.state.last_committed_chapter
        chapter_number = self._chapter_number(parameters.get("chapter_number"), default=last)
        if chapter_number < 1 or chapter_number > last:
            return f"第 {chapter_number} 章不在已提交范围内（当前为第 {last} 章）。"
        include = str(parameters.get("include") or "summary").strip().lower()
        if include == "content":
            draft = self._workspace.novels.store.load_chapter(book_id, chapter_number)
            return f"第 {chapter_number} 章《{draft.title}》· {draft.word_count} 字\n\n{draft.content}"
        if include == "plan":
            plan = self._workspace.novels.store.load_plan(book_id, chapter_number)
            return f"第 {chapter_number} 章计划\n目标：{plan.goal}\n地点：{plan.location}\n必须发生：" + "；".join(plan.required_beats) + f"\n结尾悬念：{plan.ending_hook}"
        if include == "review":
            return self._review_reply(project, book_id, chapter_number)
        draft = self._workspace.novels.store.load_chapter(book_id, chapter_number)
        summary = next(item.summary for item in self._workspace.novels.store.load_chapter_summaries(book_id) if item.chapter_number == chapter_number)
        return f"第 {chapter_number} 章《{draft.title}》· {draft.word_count} 字\n摘要：{summary}\n如需全文，可以说“查看第 {chapter_number} 章正文”。"

    def _pending_plan_reply(self, session_id: str, book_id: str) -> str:
        for event in reversed(self._workspace.sessions.list_events(session_id)):
            if (
                not event.event_type.startswith("chapter_plan_")
                or event.payload.get("status") not in {"pending", "approved"}
            ):
                continue
            proposal_id = str(event.payload.get("proposal_id") or "")
            if not proposal_id:
                continue
            proposal = self._workspace.novels.load_chapter_plan_proposal(book_id=book_id, proposal_id=proposal_id)
            if proposal.status in {"pending", "approved"}:
                label = "待确认" if proposal.status == "pending" else "已批准、尚未写作"
                return f"当前{label}：第 {proposal.chapter_number} 章计划 V{proposal.version}\n目标：{proposal.plan.goal}\n结尾悬念：{proposal.plan.ending_hook}"
        return "当前没有待确认或已批准的章节计划。"

    def _review_reply(self, project: Any, book_id: str, chapter_value: object) -> str:
        chapter_number = self._chapter_number(chapter_value, default=project.state.last_committed_chapter)
        if chapter_number < 1:
            return "当前还没有已提交章节，因此没有审稿结果。"
        review = self._workspace.novels.store.load_review(book_id, chapter_number, final=True)
        issues = "；".join(f"{item.severity}/{item.category}：{item.description}" for item in review.issues[:5]) or "无明确问题。"
        return f"第 {chapter_number} 章审查评分 {review.score}，{'通过' if review.passed else '未通过'}。\n{review.summary}\n问题：{issues}"

    def _open_hooks_reply(self, project: Any, limit_value: object) -> str:
        limit = self._limit(limit_value, default=12, maximum=30)
        hooks = sorted((item for item in project.state.hooks if item.status != "resolved"), key=lambda item: (-item.importance, item.opened_chapter, item.hook_id))[:limit]
        if not hooks:
            return "当前没有未解或推进中的伏笔。"
        return "\n".join(f"{item.display_name} · {item.status} · 重要度 {item.importance} · 首现第 {item.opened_chapter} 章\n{item.description}" for item in hooks)

    @staticmethod
    def _explicit_query_decision(content: str) -> ConversationDecision | None:
        """覆盖高频、参数明确的查询，保证“第 N 章”绝不被误读为最新章节。"""

        normalized = re.sub(r"\s+", "", content)
        if re.search(r"(?:所有|全部|已完成).*(?:章节|章)|(?:章节|章).*(?:所有|全部|已完成)", normalized):
            return ConversationDecision(kind="query", action="query_completed_chapters")
        match = re.search(r"第(\d+)章", normalized)
        if match:
            chapter_number = int(match.group(1))
            include = "content" if any(word in normalized for word in ("正文", "内容", "全文")) else "review" if any(word in normalized for word in ("审稿", "审查", "评分")) else "plan" if "计划" in normalized else "summary"
            return ConversationDecision(kind="query", action="query_chapter", parameters={"chapter_number": chapter_number, "include": include})
        if any(word in normalized for word in ("待确认计划", "当前计划", "候选计划")):
            return ConversationDecision(kind="query", action="query_pending_plan")
        if any(word in normalized for word in ("未解伏笔", "活跃伏笔", "伏笔列表")):
            return ConversationDecision(kind="query", action="query_open_foreshadowings")
        if any(word in normalized for word in ("作品进度", "创作进度", "完成进度")):
            return ConversationDecision(kind="query", action="query_story_progress")
        return None

    @staticmethod
    def _slash_skill_command(content: str) -> str | tuple[str, str] | None:
        """Slash Command 只负责用户显式选 Skill，不把它变成另一套执行权限。"""

        normalized = content.strip()
        if normalized == "/skills":
            return "list"
        match = re.fullmatch(r"/skill\s+([A-Za-z][A-Za-z0-9-]{0,63})\s+(.+)", normalized, re.DOTALL)
        if match is None:
            return None
        return match.group(1).lower(), match.group(2).strip()

    @staticmethod
    def _chapter_status_label(status: str) -> str:
        return {
            "ready_for_review": "审查通过，待人工复阅",
            "review_warning": "审查通过（有提示）",
            "draft_rejected": "审查未通过",
        }.get(status, status)

    @staticmethod
    def _limit(value: object, *, default: int, maximum: int) -> int:
        try:
            return min(maximum, max(1, int(value))) if value is not None else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _chapter_number(value: object, *, default: int) -> int:
        try:
            return int(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    def _book_summary(self, book_id: str | None) -> str | None:
        if not book_id:
            return None
        project = self._workspace.novels.store.load_project(book_id)
        return f"《{project.metadata.title}》；当前第 {project.state.last_committed_chapter} 章；地点：{project.state.current_location}；时间：{project.state.current_time}"

    def _reply(self, session_id: str, content: str, *,
               user_message: Any | None = None, context_trace: dict[str, Any] | None = None) -> None:
        metadata: dict[str, Any] = {"main_agent": True}
        if context_trace is not None:
            metadata["context_trace"] = context_trace
        session = self._workspace.sessions.append_message(
            session_id, role="assistant", content=content, action="chat",
            metadata=metadata,
        )
        # 只有真正经过 Main Agent 的自然语言回合才做后处理；明确查询、按钮和
        # Slash Command 不应为“记忆提取”额外消耗一次模型调用。
        if user_message is not None and context_trace is not None:
            assistant_message = session.messages[-1]
            asyncio.create_task(self._workspace.post_main_agent_turn(
                session_id=session_id,
                user_message_id=user_message.message_id,
                assistant_message_id=assistant_message.message_id,
            ))

    @staticmethod
    def _action_summary(decision: ConversationDecision, book_id: str) -> str:
        if decision.action == "confirm_and_write_chapter":
            return "确认当前候选章节计划并生成正文"
        if decision.action == "write_from_plan":
            return "根据已批准计划生成正文"
        if decision.action == "write_batch":
            return f"连续创作 {decision.parameters.get('count')} 章"
        if decision.action == "create_novel":
            return f"创建小说《{decision.parameters.get('title')}》"
        chapter = decision.parameters.get("chapter_number")
        return f"重写第 {chapter} 章" if chapter else "重写指定章节"
