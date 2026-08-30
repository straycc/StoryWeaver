"""进程内 Job 调度器。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from ..novel_creation.application import NovelService
from ..application.workspace import ChatWorkspaceApplication
from ..novel_creation.models import BatchPlanningContext, CreateNovelRequest
from ..novel_creation.serialization import to_data
from ..observability import logging_context
from ..persistence.jobs import Job, JobRepository
from ..persistence.action_proposals import ActionProposalRepository
from ..persistence.creative_control import CreativeControlRepository
from ..llm import LlmEvent, LlmEventSink, LlmEventType
from ..skills import CreativeTaskContext, creative_task_from_data
from .live_preview import LivePreviewHub


class _JobLlmEventSink(LlmEventSink):
    """把 Main Agent 的安全文本字段投影到进程内临时预览。"""

    def __init__(self, previews: LivePreviewHub, jobs: JobRepository, job_id: str) -> None:
        self._previews = previews
        self._jobs = jobs
        self._job_id = job_id

    async def on_event(self, event: LlmEvent) -> None:
        event_type = str(event.type)
        data = dict(event.data)
        agent_id = str(event.worker_id)
        field = str(data.get("field") or "reply")
        if event_type == LlmEventType.SKILL_RESOLVED.value:
            self._jobs.append_event(
                self._job_id,
                "skill_resolved",
                {"agent_id": agent_id, **data},
            )
        elif event_type == LlmEventType.STREAM_STARTED.value:
            await self._previews.start(self._job_id, agent_id=agent_id, field=field)
        elif event_type == LlmEventType.TEXT_DELTA.value:
            await self._previews.append(
                self._job_id, agent_id=agent_id, field=field,
                delta=str(data.get("delta") or ""),
            )
        elif event_type == LlmEventType.STREAM_COMPLETED.value:
            await self._previews.complete(self._job_id, agent_id=agent_id, field=field)
        elif event_type == LlmEventType.RUN_FAILED.value:
            await self._previews.reset(self._job_id, agent_id=agent_id, field=field)


class JobSupervisor:
    """单 FastAPI 进程内的长任务调度器。

    Task 引用仅用于当前进程执行；真正状态与事件都在 PostgreSQL，因此浏览器
    断开不会取消任务，服务重启则由启动钩子统一标记为 interrupted。
    """

    def __init__(
        self,
        *,
        service: NovelService,
        jobs: JobRepository,
        workspace: ChatWorkspaceApplication | None = None,
        action_surface: Any | None = None,
        action_proposals: ActionProposalRepository | None = None,
        creative_controls: CreativeControlRepository | None = None,
        roleplay: Any | None = None,
        live_previews: LivePreviewHub | None = None,
    ) -> None:
        self._service = service
        self._jobs = jobs
        self._workspace = workspace
        self._action_surface = action_surface
        self._action_proposals = action_proposals
        self._creative_controls = creative_controls
        self._roleplay = roleplay
        self._live_previews = live_previews or LivePreviewHub()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._guard = asyncio.Lock()

    def interrupt_orphaned_jobs(self) -> int:
        return self._jobs.interrupt_active_jobs()

    @property
    def live_previews(self) -> LivePreviewHub:
        """供 SSE 端点订阅当前进程中的临时文本。"""

        return self._live_previews

    async def submit(
        self,
        *,
        job_type: str,
        book_id: str | None,
        payload: Mapping[str, Any],
    ) -> Job:
        lock_scope = self._lock_scope_for(job_type=job_type, payload=payload)
        job = self._jobs.create(
            job_type=job_type,
            book_id=book_id,
            payload=payload,
            lock_scope=lock_scope,
        )
        async with self._guard:
            self._tasks[job.job_id] = asyncio.create_task(self._run(job.job_id))
        return job

    async def retry(self, job_id: str) -> Job:
        previous = self._jobs.get(job_id)
        if previous.status not in {"failed", "paused", "interrupted"}:
            raise ValueError("只有失败、暂停或中断的 Job 可以重试")
        return await self.submit(
            job_type=previous.job_type,
            book_id=previous.book_id,
            payload=previous.payload,
        )

    async def cancel(self, job_id: str) -> Job:
        """请求取消当前进程中的 Job。

        SDK 调用会收到协程取消信号；状态只在 `_run` 的取消分支落库，避免
        API 与执行协程同时写终态造成事件乱序。
        """

        job = self._jobs.get(job_id)
        if job.status not in {"queued", "running"}:
            raise ValueError("只有排队或执行中的 Job 可以取消")
        async with self._guard:
            task = self._tasks.get(job_id)
        if task is None:
            raise ValueError("该 Job 不在当前进程中，无法安全取消")
        task.cancel()
        # 取消信号只能在协程的下一个 await 边界生效。让出一次事件循环后重新
        # 读取持久状态：若业务事务已先完成，必须如实返回 succeeded，而不是让
        # 客户端误以为已提交章节被取消。
        await asyncio.sleep(0)
        return self._jobs.get(job_id)

    async def shutdown(self) -> None:
        """停止服务时取消本进程 Task；启动时会把它们持久化为 interrupted。"""

        async with self._guard:
            tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def configure_action_surface(self, action_surface: Any, proposals: ActionProposalRepository) -> None:
        """在 API 组装完成后注入主 Agent；避免调度器循环依赖。"""

        self._action_surface = action_surface
        self._action_proposals = proposals

    async def _run(self, job_id: str) -> None:
        job = self._jobs.start(job_id)
        result: dict[str, Any] | None = None
        business_commit_reached = False
        try:
            with logging_context(run_id=job_id, book_id=job.book_id, action=job.job_type):
                result = await self._dispatch(job)
            # 所有会修改作品/沙盒状态的操作都在 `_dispatch()` 内部完成数据库
            # 事务；这里之后不再有 await。这个标记使未来新增异步收尾步骤时仍
            # 不会把已提交事实投影为 cancelled。
            business_commit_reached = True
            self._jobs.succeed(job_id, result=result)
            self._project_action_proposal(job, succeeded=True)
            self._append_action_terminal(job, failed=False)
        except asyncio.CancelledError:
            if business_commit_reached:
                self._jobs.succeed(job_id, result=result)
                self._project_action_proposal(job, succeeded=True)
                self._append_action_terminal(job, failed=False)
                return
            self._jobs.fail(job_id, status="cancelled", error="用户取消了任务")
            self._project_action_proposal(job, succeeded=False)
            self._append_action_terminal(job, failed=True, error="用户取消了任务")
            raise
        except Exception as exc:
            self._jobs.fail(job_id, error=f"{type(exc).__name__}: {exc}")
            self._project_action_proposal(job, succeeded=False)
            self._append_action_terminal(job, failed=True, error=f"{type(exc).__name__}: {exc}")
        finally:
            # 只有所有模型流事件已按顺序处理、且 Job 终态已落库后才释放临时
            # 预览。SSE 可以先排空自己的队列，避免最后一段文本与清理竞争。
            await self._live_previews.clear_job(job_id)
            async with self._guard:
                self._tasks.pop(job_id, None)

    async def _dispatch(self, job: Job) -> dict[str, Any]:
        payload = dict(job.payload)
        if job.job_type == "session_action":
            if str(payload.get("action") or "chat") == "chat" and self._action_surface is not None:
                return await self._action_surface.handle(
                    session_id=str(payload["session_id"]), content=str(payload.get("content") or ""),
                    current_job_id=job.job_id,
                    creative_task=self._creative_task(payload),
                    event_sinks=(
                        _JobLlmEventSink(self._live_previews, self._jobs, job.job_id),
                    ),
                )
            workspace = self._require_workspace()
            result = await workspace.send_message(
                str(payload["session_id"]),
                content=str(payload.get("content") or ""),
                action=str(payload.get("action") or "chat"),
                book_id=payload.get("book_id") if isinstance(payload.get("book_id"), str) else None,
                payload=payload.get("action_payload") if isinstance(payload.get("action_payload"), dict) else {},
                run_id=job.job_id,
            )
            return {
                "session_id": result.session.session_id,
                "message_id": result.assistant_message.message_id,
                "action": result.assistant_message.action,
            }
        if job.job_type == "session_retry":
            workspace = self._require_workspace()
            result = await workspace.retry_action(
                str(payload["session_id"]),
                str(payload["retry_run_id"]),
                attempt_run_id=job.job_id,
            )
            return {
                "session_id": result.session.session_id,
                "message_id": result.assistant_message.message_id,
                "action": result.assistant_message.action,
            }
        if job.job_type == "create_book":
            request = CreateNovelRequest(**payload["request"])
            project = await self._service.create_project(
                request,
                self._creative_task(payload),
            )
            return {"book_id": project.metadata.book_id, "status": "created"}
        if job.job_type == "prepare_chapter":
            proposal = await self._service.prepare_next_chapter(
                book_id=self._require_book(job),
                user_instruction=payload.get("user_instruction"),
                batch_context=self._batch_context(payload.get("batch_context")),
                creative_task=self._creative_task(payload),
            )
            self._append_plan_event(job, "chapter_plan_prepared", proposal)
            return {"proposal_id": proposal.proposal_id, "chapter_number": proposal.chapter_number, "status": proposal.status}
        if job.job_type == "confirm_plan":
            result = await self._service.confirm_chapter_plan(
                book_id=self._require_book(job), proposal_id=str(payload["proposal_id"]),
            )
            # 前端会把所有 chapter_plan_* 事件渲染为完整计划卡，不能只写
            # proposal_id/status；否则会出现一张字段为空的“确认计划”卡片。
            proposal = self._service.load_chapter_plan_proposal(
                book_id=self._require_book(job), proposal_id=str(payload["proposal_id"]),
            )
            self._append_plan_event(job, "chapter_plan_confirmed", proposal)
            if result.committed and self._creative_controls is not None:
                self._creative_controls.consume_after_commit(
                    book_id=self._require_book(job), chapter_number=result.chapter_number,
                )
            self._append_chapter_result_message(job, result)
            return self._chapter_result(result)
        if job.job_type == "approve_plan":
            proposal = self._service.approve_chapter_plan(
                book_id=self._require_book(job),
                proposal_id=str(payload["proposal_id"]),
            )
            self._append_plan_event(job, "chapter_plan_approved", proposal)
            self._expire_related_action_proposals(job, str(payload["proposal_id"]))
            return {
                "proposal_id": proposal.proposal_id,
                "chapter_number": proposal.chapter_number,
                "status": proposal.status,
            }
        if job.job_type == "write_from_plan":
            result = await self._service.write_from_plan(
                book_id=self._require_book(job),
                proposal_id=str(payload["proposal_id"]),
            )
            proposal = self._service.load_chapter_plan_proposal(
                book_id=self._require_book(job),
                proposal_id=str(payload["proposal_id"]),
            )
            self._append_plan_event(job, "chapter_plan_confirmed", proposal)
            if result.committed and self._creative_controls is not None:
                self._creative_controls.consume_after_commit(
                    book_id=self._require_book(job), chapter_number=result.chapter_number,
                )
            self._append_chapter_result_message(job, result)
            return self._chapter_result(result)
        if job.job_type == "revise_plan":
            proposal = await self._service.revise_chapter_plan(
                book_id=self._require_book(job), proposal_id=str(payload["proposal_id"]),
                feedback=str(payload["feedback"]),
            )
            self._append_plan_event(job, "chapter_plan_revised", proposal)
            self._expire_related_action_proposals(job, str(payload["proposal_id"]))
            return {"proposal_id": proposal.proposal_id, "chapter_number": proposal.chapter_number, "status": proposal.status}
        if job.job_type == "cancel_plan":
            proposal = self._service.cancel_chapter_plan(
                book_id=self._require_book(job), proposal_id=str(payload["proposal_id"]),
            )
            self._append_plan_event(job, "chapter_plan_cancelled", proposal)
            self._expire_related_action_proposals(job, str(payload["proposal_id"]))
            return {"proposal_id": proposal.proposal_id, "status": proposal.status}
        if job.job_type == "rewrite_chapter":
            record, proposal = await self._service.prepare_rewrite_chapter(
                book_id=self._require_book(job), chapter_number=int(payload["chapter_number"]),
                user_instruction=payload.get("user_instruction"),
                creative_task=self._creative_task(payload),
            )
            self._append_plan_event(job, "chapter_plan_prepared", proposal)
            return {"rewrite_id": record.rewrite_id, "proposal_id": proposal.proposal_id, "chapter_number": proposal.chapter_number}
        if job.job_type == "write_batch":
            results = await self._service.write_chapters(
                book_id=self._require_book(job), count=int(payload["count"]),
                user_instruction=payload.get("user_instruction"),
                creative_task=self._creative_task(payload),
                on_chapter_committed=(
                    lambda result: self._creative_controls.consume_after_commit(
                        book_id=self._require_book(job), chapter_number=result.chapter_number,
                    ) if self._creative_controls is not None else None
                ),
            )
            return {"completed": len(results), "results": [self._chapter_result(item) for item in results]}
        if job.job_type == "roleplay_turn":
            if self._roleplay is None:
                raise RuntimeError("角色剧场服务尚未配置")
            payload = dict(job.payload)
            turn = self._roleplay.simulations.reserve_turn(
                simulation_id=str(payload["simulation_id"]), expected_version=int(payload["expected_version"]),
                client_request_id=str(payload["client_request_id"]), input_type=str(payload["input_type"]),
                user_input=str(payload.get("content") or "继续推演"), job_id=job.job_id,
                target_character_id=str(payload["target_character_id"]) if payload.get("target_character_id") else None,
            )
            if turn.job_id != job.job_id:
                return {"simulation_id": turn.simulation_id, "turn_id": turn.turn_id, "duplicate_job_id": turn.job_id}
            count = 3 if turn.input_type == "observer_continue" else 1
            simulation = None
            for index in range(count):
                simulation = await self._roleplay.run_turn(
                    turn_id=turn.turn_id,
                    job_id=job.job_id,
                    creative_task=self._creative_task(payload),
                    emit=lambda event_type, event_payload: self._jobs.append_event(job.job_id, event_type, event_payload),
                )
                # 自动旁观只在场景明确结束或达到本次三回合上限时停止；不再维护游戏化紧张度。
                if simulation.current_state.scene_status == "completed" or index + 1 >= count:
                    break
                # 自动旁观的后续子回合也独立持久化，方便恢复与回放。
                turn = self._roleplay.simulations.reserve_turn(
                    simulation_id=simulation.simulation_id, expected_version=simulation.version,
                    client_request_id=f"{payload['client_request_id']}:{index + 2}", input_type="observer_continue",
                    user_input="继续推演场景，推进冲突但不要结束长篇故事。", job_id=job.job_id,
                )
            if simulation is None:
                raise RuntimeError("角色剧场没有生成回合")
            return {"simulation_id": simulation.simulation_id, "turn_id": turn.turn_id, "version": simulation.version, "turns_completed": simulation.current_turn}
        raise ValueError(f"不支持的 Job 类型：{job.job_type}")

    def _project_action_proposal(self, job: Job, *, succeeded: bool) -> None:
        """Job 是执行事实源，Proposal 只投影终态供 UI 恢复。"""

        proposal_id = job.payload.get("action_proposal_id")
        if not proposal_id or self._action_proposals is None:
            return
        try:
            self._action_proposals.project_job_terminal(str(proposal_id), succeeded=succeeded)
        except (KeyError, ValueError):
            # 终态投影失败不应覆盖真正 Job 的执行结果。
            return

    def _append_plan_event(self, job: Job, event_type: str, proposal: Any) -> None:
        if self._workspace is None or not isinstance(job.payload.get("session_id"), str):
            return
        self._workspace.sessions.append_event(
            str(job.payload["session_id"]), event_type=event_type,
            payload=self._workspace._chapter_plan_data(proposal),
        )

    def _append_session_event(self, job: Job, event_type: str, payload: Mapping[str, Any]) -> None:
        if self._workspace is None or not isinstance(job.payload.get("session_id"), str):
            return
        self._workspace.sessions.append_event(str(job.payload["session_id"]), event_type=event_type, payload=payload)

    def _append_action_terminal(self, job: Job, *, failed: bool, error: str | None = None) -> None:
        """Dispatcher 直达 Job 时，仍为刷新后的会话保留可读的生命周期。"""

        if self._workspace is None or job.job_type == "session_action":
            return
        session_id = job.payload.get("session_id")
        if not isinstance(session_id, str):
            return
        # confirm_plan 会在正文助手消息之前主动写入完成事件，以保持“过程在结果前”
        # 的阅读顺序；通用收尾不再重复追加到正文底部。
        if any(
            event.event_type == "action_completed" and event.payload.get("run_id") == job.job_id
            for event in self._workspace.sessions.list_events(session_id)
        ):
            return
        self._workspace.sessions.append_event(session_id, event_type="action_failed" if failed else "action_completed", payload={
            "run_id": job.job_id, "root_run_id": job.job_id, "action": job.job_type,
            "label": job.job_type, **({"error": error} if error else {}),
        })

    def _append_chapter_result_message(self, job: Job, result: Any) -> None:
        """直达 Dispatcher 的写章 Job 也必须在聊天中交付正文。"""

        if self._workspace is None or not isinstance(job.payload.get("session_id"), str):
            return
        session_id = str(job.payload["session_id"])
        # 先闭合执行卡，再输出正文，确保聊天时间线符合用户的阅读顺序。
        self._workspace.sessions.append_event(session_id, event_type="action_completed", payload={
            "run_id": job.job_id, "root_run_id": job.job_id,
            "action": "confirm_chapter_plan", "label": "确认候选计划并生成本章",
            "summary": "章节生成流程已完成。",
        })
        if result.committed:
            chapter_summary = result.state_delta.chapter_summary if result.state_delta else "未提供"
            content = (
                f"第 {result.chapter_number} 章生成并提交完成。\n\n"
                f"标题：{result.final_draft.title}\n"
                f"字数：{result.final_draft.word_count}\n"
                f"状态：{result.status}\n"
                f"自动修订：{result.revision_count} 轮\n"
                f"审查：{result.final_review.summary}\n"
                f"摘要：{chapter_summary}\n\n"
                f"{result.final_draft.content}"
            )
        else:
            content = (
                f"第 {result.chapter_number} 章候选正文已生成，但审稿未通过，未进入正史。\n\n"
                f"标题：{result.final_draft.title}\n"
                f"审查：{result.final_review.summary}\n\n{result.final_draft.content}"
            )
        self._workspace.sessions.append_message(
            session_id, role="assistant", content=content,
            action="confirm_chapter_plan", metadata={
                "chapter_number": result.chapter_number,
                "committed": result.committed,
                # 前端以该投影渲染章节完成卡；content 仍保留为旧客户端兼容的纯文本交付。
                "chapter_result": {
                    "chapter_number": result.chapter_number,
                    "title": result.final_draft.title,
                    "word_count": result.final_draft.word_count,
                    "status": result.status,
                    "revision_count": result.revision_count,
                    "review_summary": result.final_review.summary,
                    "chapter_summary": (
                        result.state_delta.chapter_summary
                        if result.state_delta else "未提供"
                    ),
                    "content": result.final_draft.content,
                    "committed": result.committed,
                },
                # 直达 Job 路径也要把 Writer 的上下文轨迹交给聊天；否则控制面
                # 虽然实际生效，用户却没有地方核验 creative-control 是否被选入。
                "context_trace": to_data(result.context_trace),
            },
        )

    def _expire_related_action_proposals(self, job: Job, chapter_plan_proposal_id: str) -> None:
        if self._action_proposals is None or not job.book_id:
            return
        for proposal in self._action_proposals.expire_for_chapter_plan(
            book_id=job.book_id, chapter_plan_proposal_id=chapter_plan_proposal_id,
        ):
            if self._workspace is not None:
                self._workspace.sessions.append_event(
                    proposal.session_id, event_type="action_proposal_cancelled",
                    payload={"action_proposal_id": proposal.proposal_id, "status": "expired", "summary": proposal.summary},
                )

    def _require_workspace(self) -> ChatWorkspaceApplication:
        if self._workspace is None:
            raise RuntimeError("当前 JobSupervisor 未配置会话工作区")
        return self._workspace

    @staticmethod
    def _require_book(job: Job) -> str:
        if not job.book_id:
            raise ValueError("该 Job 必须绑定 book_id")
        return job.book_id

    @staticmethod
    def _lock_scope_for(*, job_type: str, payload: Mapping[str, Any]) -> str:
        """根据业务动作决定是否占用作品写锁。"""

        direct_write_jobs = {
            "prepare_chapter",
            "approve_plan",
            "write_from_plan",
            "confirm_plan",
            "revise_plan",
            "cancel_plan",
            "rewrite_chapter",
            "write_batch",
        }
        if job_type in direct_write_jobs or job_type == "session_retry":
            return "book_write"
        if job_type != "session_action":
            return "none"
        action = str(payload.get("action") or "chat")
        if action in {
            "write_next",
            "start_chapter_batch",
            "rewrite_chapter",
            "revise_chapter_plan",
            "confirm_chapter_plan",
            "cancel_chapter_plan",
        }:
            return "book_write"
        return "none"

    @staticmethod
    def _batch_context(value: object) -> BatchPlanningContext | None:
        return BatchPlanningContext(**value) if isinstance(value, dict) else None

    @staticmethod
    def _creative_task(payload: Mapping[str, Any]) -> CreativeTaskContext | None:
        value = payload.get("creative_task_context")
        return None if value is None else creative_task_from_data(value)

    @staticmethod
    def _chapter_result(result: Any) -> dict[str, Any]:
        return {
            "chapter_number": result.chapter_number,
            "committed": result.committed,
            "status": result.status,
            "candidate_id": result.candidate_id,
            "revision_count": result.revision_count,
        }
