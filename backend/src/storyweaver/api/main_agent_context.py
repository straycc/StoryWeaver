"""Studio Chat 主 Agent 的轻量、只读上下文组装。

这里刻意不调用模型、不写摘要、不写长期记忆。它只把已经持久化的
会话、作品与工作流事实压缩为 Main Agent 可以安全消费的上下文包。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..application.models import ChatSession
from ..application.workspace import ChatWorkspaceApplication
from ..context_management import (
    ContextAssemblyTrace,
    ContextCandidate,
    ContextItem,
    ContextPolicy,
    ContextTraceV2,
    source_ref,
    trace_from_candidates,
)
from ..persistence import ActionProposalRepository, CreativeControlRepository, JobRepository


@dataclass(frozen=True, slots=True)
class MainAgentContextPackage:
    """Main Agent 的动态上下文与可审计选择记录。"""

    current_request: str
    rendered_context: str
    trace: ContextAssemblyTrace
    trace_v2: ContextTraceV2

    def trace_data(self) -> dict[str, Any]:
        return asdict(self.trace)

    def trace_v2_data(self) -> dict[str, Any]:
        return self.trace_v2.to_data()


@dataclass(frozen=True, slots=True)
class WorkflowDigest:
    active_jobs: tuple[str, ...]
    pending_actions: tuple[str, ...]
    pending_plan: str | None
    latest_action: str | None


class WorkflowContextReader:
    """从持久化事实中读取工作流状态，不解析前端 Timeline 细节。"""

    def __init__(
        self,
        *,
        jobs: JobRepository,
        proposals: ActionProposalRepository,
        workspace: ChatWorkspaceApplication,
    ) -> None:
        self._jobs = jobs
        self._proposals = proposals
        self._workspace = workspace

    def read(
        self,
        *,
        session_id: str,
        book_id: str | None,
        exclude_job_id: str | None,
    ) -> WorkflowDigest:
        active_jobs = tuple(
            f"{item.job_type}（{item.status}）"
            for item in self._jobs.list_active(book_id=book_id)
            if item.job_id != exclude_job_id
        )
        pending_actions = tuple(
            item.summary
            for item in self._proposals.list_pending(session_id=session_id)
        )
        pending_plan = self._pending_plan(session_id=session_id, book_id=book_id)
        latest_action = self._latest_action(session_id)
        return WorkflowDigest(active_jobs, pending_actions, pending_plan, latest_action)

    def _pending_plan(self, *, session_id: str, book_id: str | None) -> str | None:
        if not book_id:
            return None
        for event in reversed(self._workspace.sessions.list_events(session_id)):
            if not event.event_type.startswith("chapter_plan_"):
                continue
            proposal_id = str(event.payload.get("proposal_id") or "")
            if not proposal_id:
                continue
            try:
                proposal = self._workspace.novels.load_chapter_plan_proposal(
                    book_id=book_id, proposal_id=proposal_id,
                )
            except (KeyError, ValueError):
                continue
            if proposal.status == "pending":
                return (
                    f"第 {proposal.chapter_number} 章候选计划 V{proposal.version}："
                    f"{self._compact(proposal.plan.goal, 280)}"
                )
        return None

    def _latest_action(self, session_id: str) -> str | None:
        for event in reversed(self._workspace.sessions.list_events(session_id)):
            if event.event_type not in {"action_completed", "action_failed"}:
                continue
            label = str(event.payload.get("label") or event.payload.get("action") or "动作")
            status = "失败" if event.event_type == "action_failed" else "完成"
            return f"最近动作：{label}（{status}）"
        return None

    @staticmethod
    def _compact(value: str, limit: int) -> str:
        normalized = " ".join(value.split())
        return normalized[:limit] + ("…" if len(normalized) > limit else "")


class MainAgentContextBuilder:
    """选择 Main Agent 所需的最小会话上下文。

    固定指令、动作 Schema 与输出预留不由这里计算；本类的预算只覆盖动态
    Package，默认控制在 4K Token 左右。
    """

    def __init__(
        self,
        *,
        workspace: ChatWorkspaceApplication,
        creative_controls: CreativeControlRepository,
        workflow: WorkflowContextReader,
        policy: ContextPolicy | None = None,
    ) -> None:
        self._workspace = workspace
        self._creative_controls = creative_controls
        self._workflow = workflow
        self._policy = policy or ContextPolicy(token_budget=4000, recent_message_limit=12)

    def build(
        self,
        *,
        session: ChatSession,
        current_request: str,
        current_sequence: int,
        current_job_id: str | None,
    ) -> MainAgentContextPackage:
        if current_sequence < 1:
            raise ValueError("current_sequence 必须为正数")
        items: list[ContextItem] = []
        excluded: list[str] = []
        notes = ["Main Agent 动态上下文；固定 Instructions、Schema 与输出预留不计入本预算"]

        self._append(
            items, "request:current", "current_request", current_request,
            protected=True, priority=100, reason="本轮用户请求只能出现一次",
        )
        self._append_session_summary(items, session)
        self._append_book_digest(items, session.book_id)
        workflow = self._workflow.read(
            session_id=session.session_id,
            book_id=session.book_id,
            exclude_job_id=current_job_id,
        )
        self._append_workflow(items, workflow)
        self._append_creative_control(items, session.book_id)

        # 显式排除当前消息；不能依赖“append 前读取 session”的调用顺序。
        history = [
            item for item in session.messages
            if item.sequence < current_sequence and item.action == "chat"
        ][-self._policy.recent_message_limit :]
        for message in history:
            self._append(
                items,
                f"message:{message.message_id}",
                "recent_message",
                f"{message.role}：{message.content}",
                protected=False,
                priority=75 if message.role == "user" else 65,
                reason="最近未压缩的会话消息",
            )

        book_version = self._book_version(session.book_id)
        candidates = tuple(
            ContextCandidate(
                source=source_ref(
                    source_id=item.source_id,
                    source_type=item.source_type,
                    content=item.content,
                    book_version=book_version,
                    revision=(str(self._summary_sequence(session)) if item.source_type == "conversation_summary" else None),
                ),
                content=item.content,
                reason=item.reason,
                protected=item.protected,
                priority=item.priority,
                digest=self._protected_digest(item),
            )
            for item in items
        )
        selected_candidates, trace_v2 = trace_from_candidates(
            agent_role="main_agent",
            policy_version="main-agent-context-v2.1",
            book_version=book_version,
            token_budget=self._policy.token_budget,
            candidates=candidates,
            notes=tuple(notes),
        )
        # 必须使用 Budgeter 实际返回的 content。若 protected 来源切换为
        # Digest，不能再回头从原始 ContextItem 取文本，否则 Prompt 与 Trace
        # 的 hash 会失真。
        originals = {item.source_id: item for item in items}
        selected = [
            ContextItem(
                source_id=candidate.source.source_id,
                source_type=candidate.source.source_type,
                content=candidate.content,
                protected=candidate.protected,
                priority=candidate.priority,
                estimated_tokens=max(1, (len(candidate.content) + 3) // 4),
                reason=candidate.reason,
            )
            for candidate in selected_candidates
        ]
        selected_ids = {item.source_id for item in selected}
        excluded = [item.source_id for item in items if item.source_id not in selected_ids]
        compressed_ids = tuple(
            candidate.source.source_id
            for candidate in selected_candidates
            if candidate.content != originals[candidate.source.source_id].content
        )
        used = trace_v2.estimated_tokens
        notes.append(f"选择 {len(selected)} 个来源，估算 {used}/{self._policy.token_budget} Token")
        trace = ContextAssemblyTrace(
            budget=self._policy.token_budget,
            estimated_tokens=used,
            selected_source_ids=tuple(item.source_id for item in selected),
            excluded_source_ids=tuple(excluded),
            protected_source_ids=tuple(item.source_id for item in selected if item.protected),
            compressed_source_ids=compressed_ids,
            selected_memory_ids=(),
            summary_sequence=self._summary_sequence(session),
            notes=tuple(notes),
            source_reasons=tuple((item.source_id, item.reason) for item in selected)
            + tuple((source_id, "超过动态上下文预算") for source_id in excluded),
        )
        rendered = "\n\n".join(
            f"[{item.source_type}]\n{item.content}" for item in selected
        )
        return MainAgentContextPackage(current_request, rendered, trace, trace_v2)

    def _append_session_summary(self, items: list[ContextItem], session: ChatSession) -> None:
        summary = self._workspace.sessions.latest_summary(session.session_id)
        if summary is None:
            return
        payload = summary.payload
        if payload.get("book_id") not in {None, session.book_id}:
            return
        parts: list[str] = []
        for key, label in (
            ("current_goal", "当前目标"), ("confirmed_decisions", "已确认决定"),
            ("user_constraints", "用户约束"), ("completed_work", "已完成"),
            ("pending_work", "待处理"),
        ):
            values = payload.get(key)
            if isinstance(values, list) and values:
                parts.append(f"{label}：" + "；".join(str(item) for item in values[:6]))
        if parts:
            self._append(items, f"summary:{summary.sequence}", "conversation_summary", "\n".join(parts),
                         protected=False, priority=85, reason="已有滚动会话摘要")

    def _append_book_digest(self, items: list[ContextItem], book_id: str | None) -> None:
        if not book_id:
            self._append(items, "book:none", "book_digest", "当前会话未绑定作品。",
                         protected=True, priority=100, reason="作品绑定状态")
            return
        project = self._workspace.novels.store.load_project(book_id)
        summaries = self._workspace.novels.store.load_chapter_summaries(book_id)
        latest = summaries[-1].summary if summaries else "尚未开始正文。"
        open_hooks = sum(item.status != "resolved" for item in project.state.hooks)
        content = (
            f"书名：{project.metadata.title}\n类型：{project.metadata.genre}\n"
            f"进度：{project.state.last_committed_chapter}/{project.metadata.target_chapters} 章\n"
            f"地点/时间：{project.state.current_location} / {project.state.current_time}\n"
            f"最近章节摘要：{self._compact(latest, 700)}\n未解伏笔：{open_hooks} 条"
        )
        self._append(items, f"book:{book_id}", "book_digest", content,
                     protected=True, priority=98, reason="当前作品的轻量权威摘要")

    def _append_workflow(self, items: list[ContextItem], workflow: WorkflowDigest) -> None:
        lines: list[str] = []
        if workflow.active_jobs:
            lines.append("其他活跃任务：" + "；".join(workflow.active_jobs))
        if workflow.pending_actions:
            lines.append("待确认操作：" + "；".join(workflow.pending_actions[:3]))
        if workflow.pending_plan:
            lines.append("待确认计划：" + workflow.pending_plan)
        if workflow.latest_action:
            lines.append(workflow.latest_action)
        if lines:
            self._append(items, "workflow:current", "workflow_digest", "\n".join(lines),
                         protected=True, priority=97, reason="防止重复创建计划或误判进行中工作")

    def _append_creative_control(self, items: list[ContextItem], book_id: str | None) -> None:
        if not book_id:
            return
        control = self._creative_controls.get(book_id)
        if not control.author_intent and not control.current_focus:
            return
        content = (
            f"作者意图：{self._compact(control.author_intent, 500) or '未设置'}\n"
            f"当前焦点：{self._compact(control.current_focus, 500) or '未设置'}\n"
            f"焦点模式：{control.current_focus_mode}"
            + (f"；目标章节：{control.focus_target_chapter}" if control.focus_target_chapter else "")
        )
        self._append(items, f"creative-control:{book_id}", "creative_control", content,
                     protected=True, priority=96, reason="作者设置的当前创作控制面（摘要版）")

    @staticmethod
    def _append(items: list[ContextItem], source_id: str, source_type: str, content: str,
                *, protected: bool, priority: int, reason: str) -> None:
        normalized = content.strip()
        if not normalized:
            return
        items.append(ContextItem(source_id, source_type, normalized, protected, priority,
                                 max(1, (len(normalized) + 3) // 4), reason))

    def _summary_sequence(self, session: ChatSession) -> int | None:
        summary = self._workspace.sessions.latest_summary(session.session_id)
        return summary.sequence if summary else None

    def _book_version(self, book_id: str | None) -> int | None:
        if not book_id:
            return None
        loader = getattr(self._workspace.novels.store, "load_project_with_version", None)
        if callable(loader):
            _, version = loader(book_id)
            return int(version)
        # 文件仓储没有事务版本；使用当前提交章号作为稳定降级基线。
        return self._workspace.novels.store.load_project(book_id).state.last_committed_chapter

    @staticmethod
    def _protected_digest(item: ContextItem) -> str | None:
        """只为可安全压缩的保护控制面提供 Digest。"""

        if item.source_type == "creative_control" and len(item.content) > 900:
            return item.content[:900] + "…"
        return None

    @staticmethod
    def _compact(value: str, limit: int) -> str:
        normalized = " ".join(value.split())
        return normalized[:limit] + ("…" if len(normalized) > limit else "")
