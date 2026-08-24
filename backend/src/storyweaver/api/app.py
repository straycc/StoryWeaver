"""StoryWeaver FastAPI 应用。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..novel_creation.application import PROJECT_ROOT, NovelApplicationSettings, build_novel_service
from ..novel_creation.exceptions import BookBusyError, NovelCreationError
from ..novel_creation.observability import NovelRunObserver
from ..novel_creation.pipeline import CreateNovelPipeline
from ..novel_creation.serialization import to_data
from ..persistence import ActionProposalRepository, ContextSnapshotRepository, CreativeControlRepository, Database, DatabaseSettings, JobRepository, PostgresChatSessionStore, PostgresLongTermMemoryStore, PostgresNovelProjectStore
from ..application.workspace import ChatWorkspaceApplication, build_chat_workspace
from ..application.run_progress import RunProgressStore, progress_event_data
from .jobs import JobSupervisor
from .action_surface import ActionDispatcher, MainAgentActionSurface
from .main_agent import MainAgent
from .main_agent_context import MainAgentContextBuilder, WorkflowContextReader
from ..skills import load_configured_skills


class CreateBookBody(BaseModel):
    title: str
    genre: str
    premise: str
    protagonist: str
    central_conflict: str
    tone: str
    target_chapters: int = Field(gt=0)
    chapter_target_words: int = Field(gt=0)
    language: str = "zh"


class InstructionBody(BaseModel):
    user_instruction: str | None = None


class ConfirmPlanBody(BaseModel):
    proposal_id: str


class RevisePlanBody(BaseModel):
    feedback: str


class BatchBody(InstructionBody):
    count: int = Field(gt=0)


class CreateSessionBody(BaseModel):
    title: str = "新对话"
    book_id: str | None = None


class SessionMessageBody(BaseModel):
    content: str = ""
    action: str = "chat"
    book_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ConfirmActionProposalBody(BaseModel):
    proposal_id: str


class CreativeControlBody(BaseModel):
    author_intent: str | None = Field(default=None, max_length=8000)
    current_focus: str | None = Field(default=None, max_length=8000)
    current_focus_mode: str | None = Field(default=None, pattern="^(single_chapter|persistent)$")


def create_app(*, settings: NovelApplicationSettings, database_url: str) -> FastAPI:
    """创建单实例 API；调用方必须以单 Uvicorn worker 启动。"""

    database = Database(DatabaseSettings(database_url))
    store = PostgresNovelProjectStore(database)
    jobs = JobRepository(database)
    action_proposals = ActionProposalRepository(database)
    context_snapshots = ContextSnapshotRepository(database)
    skills = load_configured_skills(PROJECT_ROOT)
    creative_controls = CreativeControlRepository(database)
    sessions = PostgresChatSessionStore(database)
    memories = PostgresLongTermMemoryStore(database)
    run_progress = RunProgressStore()

    def emit_run_event(run_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        """同一模型事件同时投影到 Job 与旧 UI 兼容的运行进度流。"""

        try:
            jobs.append_event(run_id, event_type, payload)
        except (KeyError, ValueError):
            pass
        try:
            run_progress.append(run_id, event_type, payload)
        except KeyError:
            pass

    observer = NovelRunObserver(
        output=None,
        event_sink=emit_run_event,
    )
    service = build_novel_service(
        settings,
        store=store,
        memory_store=memories,
        observer=observer,
        creative_control_provider=creative_controls.get,
        context_snapshot_sink=context_snapshots,
    )
    workspace = build_chat_workspace(
        settings,
        sessions=sessions,
        memory_store=memories,
        novels=service,
        run_progress=run_progress,
    )
    supervisor = JobSupervisor(
        service=service, jobs=jobs, workspace=workspace,
        creative_controls=creative_controls,
    )
    dispatcher = ActionDispatcher(
        proposals=action_proposals, jobs=jobs, workspace=workspace, submit_job=supervisor.submit,
    )
    dispatcher.configure_skills(skills)
    workflow_reader = WorkflowContextReader(
        jobs=jobs, proposals=action_proposals, workspace=workspace,
    )
    action_surface = MainAgentActionSurface(
        agent=MainAgent(settings), proposals=action_proposals, dispatcher=dispatcher, workspace=workspace,
        context_builder=MainAgentContextBuilder(
            workspace=workspace, creative_controls=creative_controls, workflow=workflow_reader,
        ),
        context_snapshots=context_snapshots,
    )
    supervisor.configure_action_surface(action_surface, action_proposals)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        supervisor.interrupt_orphaned_jobs()
        yield
        await supervisor.shutdown()
        database.dispose()

    app = FastAPI(title="StoryWeaver API", version="1", lifespan=lifespan)
    app.state.database = database
    app.state.jobs = jobs
    app.state.supervisor = supervisor
    app.state.novels = service
    app.state.sessions = sessions
    app.state.memories = memories
    app.state.workspace = workspace
    app.state.action_proposals = action_proposals
    app.state.context_snapshots = context_snapshots
    app.state.creative_controls = creative_controls
    app.state.action_dispatcher = dispatcher
    app.state.skills = skills
    app.state.run_progress = run_progress

    @app.exception_handler(BookBusyError)
    async def book_busy(_request: Request, exc: BookBusyError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(NovelCreationError)
    async def novel_error(_request: Request, exc: NovelCreationError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/bootstrap")
    async def bootstrap() -> dict[str, Any]:
        """旧工作台首次加载所需的完整索引，数据源为 PostgreSQL。"""

        return {**workspace.bootstrap(), "skills": [
            {"id": item.skill_id, "name": item.name, "description": item.description, "source": item.source}
            for item in dispatcher.available_skills()
        ]}

    @app.get("/api/v1/skills")
    async def list_skills() -> dict[str, Any]:
        return {"skills": [
            {"id": item.skill_id, "name": item.name, "description": item.description, "source": item.source}
            for item in dispatcher.available_skills()
        ]}

    @app.get("/api/v1/books")
    async def list_books() -> dict[str, list[dict[str, Any]]]:
        return {"books": [to_data(item) for item in service.list_projects()]}

    @app.get("/api/v1/sessions")
    async def list_sessions() -> dict[str, Any]:
        return {"sessions": [
            {"session_id": item.session_id, "title": item.title, "updated_at": item.updated_at, "book_id": item.book_id, "message_count": item.message_count}
            for item in sessions.list_sessions()
        ]}

    @app.post("/api/v1/sessions", status_code=201)
    async def create_session(body: CreateSessionBody) -> dict[str, Any]:
        if body.book_id:
            store.load_metadata(body.book_id)
        return _session_data(sessions.create_session(title=body.title, book_id=body.book_id))

    @app.get("/api/v1/sessions/{session_id}")
    async def get_session(session_id: str, before_sequence: int | None = None, limit: int = 50) -> dict[str, Any]:
        try:
            return _workspace_session_data(workspace, session_id, before_sequence, limit)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/api/v1/sessions/{session_id}")
    async def bind_session_book(session_id: str, body: CreateSessionBody) -> dict[str, Any]:
        try:
            return _session_data(workspace.bind_book(session_id, body.book_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/v1/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, bool]:
        sessions.delete_session(session_id)
        return {"ok": True}

    @app.post("/api/v1/sessions/{session_id}/messages", status_code=202)
    async def send_session_message(session_id: str, body: SessionMessageBody) -> dict[str, Any]:
        """所有聊天和预设动作都由持久 Job 执行，浏览器不再阻塞等待模型。"""

        session = sessions.load_session(session_id)
        bound_book_id = body.book_id or session.book_id
        # 按钮已经表达了明确意图，直接经过 Dispatcher，不额外消耗一次 Main Agent 调用。
        shortcut_actions = {
            "write_next": "prepare_chapter_plan",
            "revise_chapter_plan": "revise_chapter_plan",
            "confirm_chapter_plan": "confirm_and_write_chapter",
            "cancel_chapter_plan": "cancel_chapter_plan",
            "rewrite_chapter": "rewrite_chapter",
            "start_chapter_batch": "write_batch",
        }
        if body.action in shortcut_actions:
            content = body.content.strip() or workspace.bootstrap()["actions"].get(body.action, body.action)
            parameters = dict(body.payload)
            if body.action == "write_next":
                parameters.setdefault("instruction", body.content.strip() or None)
            elif body.action == "revise_chapter_plan":
                parameters.setdefault("feedback", body.content.strip())
            elif body.action == "rewrite_chapter":
                parameters.setdefault("instruction", body.content.strip() or None)
            sessions.append_message(session_id, role="user", content=content, action=body.action)
            try:
                job = await dispatcher.dispatch(
                    action=shortcut_actions[body.action], session_id=session_id, book_id=bound_book_id,
                    parameters=parameters,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            sessions.append_event(session_id, event_type="action_started", payload={
                "run_id": job.job_id, "root_run_id": job.job_id, "action": body.action,
                "label": workspace.bootstrap()["actions"].get(body.action, body.action),
            })
            return _accepted(job)
        job = await supervisor.submit(
            job_type="session_action",
            book_id=bound_book_id,
            payload={
                "session_id": session_id,
                "content": body.content,
                "action": body.action,
                "book_id": body.book_id,
                "action_payload": body.payload,
            },
        )
        return _accepted(job)

    @app.get("/api/v1/sessions/{session_id}/action-proposals")
    async def list_action_proposals(session_id: str) -> dict[str, Any]:
        sessions.load_session(session_id)
        return {"proposals": [_action_proposal_data(item) for item in action_proposals.list_pending(session_id=session_id)]}

    @app.post("/api/v1/action-proposals/{proposal_id}/confirm", status_code=202)
    async def confirm_action_proposal(proposal_id: str) -> dict[str, Any]:
        try:
            proposal = await dispatcher.confirm(proposal_id)
            workspace.sessions.append_event(proposal.session_id, event_type="action_proposal_confirmed", payload=_action_proposal_data(proposal))
            if not proposal.job_id:
                raise RuntimeError("确认操作未创建 Job")
            return _accepted(jobs.get(proposal.job_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/v1/action-proposals/{proposal_id}/cancel")
    async def cancel_action_proposal(proposal_id: str) -> dict[str, Any]:
        try:
            return {"proposal": _action_proposal_data(dispatcher.cancel(proposal_id))}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/v1/sessions/{session_id}/actions/{run_id}/retry", status_code=202)
    async def retry_session_action(session_id: str, run_id: str) -> dict[str, Any]:
        session = sessions.load_session(session_id)
        job = await supervisor.submit(
            job_type="session_retry",
            book_id=session.book_id,
            payload={"session_id": session_id, "retry_run_id": run_id},
        )
        return _accepted(job)

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        try:
            return {
                **run_progress.run_metadata(run_id),
                "events": [progress_event_data(item) for item in run_progress.snapshot(run_id)],
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/runs/{run_id}/events")
    async def stream_run_events(run_id: str, after: int = 0) -> StreamingResponse:
        return StreamingResponse(
            _run_sse(run_progress, run_id, after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/sessions/{session_id}/trace")
    async def latest_context_trace(session_id: str) -> dict[str, Any]:
        """返回当前会话最近一条助手消息的上下文轨迹。"""

        try:
            return {"trace": workspace.latest_context_trace(session_id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/memories")
    async def list_memories(scope_type: str | None = None, scope_id: str | None = None, status: str | None = None) -> dict[str, Any]:
        from ..memory.long_term import LongTermMemoryStatus, MemoryScopeType
        return {"memories": [
            PostgresLongTermMemoryStore._encode(item)
            for item in memories.list_records(
                scope_type=MemoryScopeType(scope_type) if scope_type else None,
                scope_id=scope_id,
                status=LongTermMemoryStatus(status) if status else None,
            )
        ]}

    @app.patch("/api/v1/memories/{memory_id}")
    async def update_memory(memory_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"memory": PostgresLongTermMemoryStore._encode(memories.update(memory_id, **body))}

    @app.delete("/api/v1/memories/{memory_id}")
    async def disable_memory(memory_id: str) -> dict[str, Any]:
        return {"memory": PostgresLongTermMemoryStore._encode(memories.disable(memory_id))}

    @app.post("/api/v1/memories/{memory_id}/restore")
    async def restore_memory(memory_id: str) -> dict[str, Any]:
        return {"memory": PostgresLongTermMemoryStore._encode(memories.restore(memory_id))}

    @app.post("/api/v1/books", status_code=202)
    async def create_book(body: CreateBookBody) -> dict[str, Any]:
        request_data = body.model_dump()
        # 创建任务也按稳定 book_id 参与单活跃约束，避免重复简报并发建书。
        from ..novel_creation.models import CreateNovelRequest
        book_id = CreateNovelPipeline.build_book_id(CreateNovelRequest(**request_data))
        job = await supervisor.submit(job_type="create_book", book_id=book_id, payload={"request": request_data})
        return _accepted(job)

    @app.get("/api/v1/books/{book_id}")
    async def get_book(book_id: str) -> dict[str, Any]:
        project = store.load_project(book_id)
        return {
            "metadata": to_data(project.metadata), "foundation": to_data(project.foundation),
            "state": to_data(project.state), "chapters": [to_data(item) for item in store.load_chapter_index(book_id)],
            "creative_control": _creative_control_data(creative_controls.get(book_id)),
        }

    @app.get("/api/v1/books/{book_id}/creative-control")
    async def get_creative_control(book_id: str) -> dict[str, Any]:
        store.load_metadata(book_id)
        return _creative_control_data(creative_controls.get(book_id))

    @app.put("/api/v1/books/{book_id}/creative-control")
    async def update_creative_control(book_id: str, body: CreativeControlBody) -> dict[str, Any]:
        """控制面是作者输入，不是正史；修改后从下一次规划/写作开始生效。"""

        store.load_metadata(book_id)
        project = store.load_project(book_id)
        data = body.model_dump()
        if data.get("current_focus") is not None or data.get("current_focus_mode") == "single_chapter":
            data["focus_target_chapter"] = project.state.last_committed_chapter + 1
        return _creative_control_data(creative_controls.update(**data, book_id=book_id))

    @app.get("/api/v1/projects/{book_id}")
    async def get_project_compat(book_id: str) -> dict[str, Any]:
        """旧工作台的作品摘要形状，供等价 React UI 直接使用。"""

        project = store.load_project(book_id)
        profiles = {item.character_id: item for item in project.foundation.characters}
        return {
            "book_id": project.metadata.book_id,
            "title": project.metadata.title,
            "genre": project.metadata.genre,
            "target_chapters": project.metadata.target_chapters,
            "chapter_target_words": project.metadata.chapter_target_words,
            "last_committed_chapter": project.state.last_committed_chapter,
            "current_location": project.state.current_location,
            "current_time": project.state.current_time,
            "chapters": [to_data(item) for item in store.load_chapter_index(book_id)],
            "hooks": [to_data(item) for item in project.state.hooks],
            "characters": [
                {
                    "character_id": item.character_id,
                    "name": profiles.get(item.character_id).name if item.character_id in profiles else item.character_id,
                    "location": item.location,
                    "status": item.status,
                    "emotion": item.emotion,
                    "goal": item.current_goal,
                }
                for item in project.state.characters
            ],
            "creative_control": _creative_control_data(creative_controls.get(book_id)),
        }

    @app.get("/api/v1/projects/{book_id}/chapters/{chapter_number}")
    async def get_project_chapter_compat(book_id: str, chapter_number: int) -> dict[str, Any]:
        """旧阅读器所需的扁平章节数据。"""

        draft = store.load_chapter(book_id, chapter_number)
        return {
            "chapter_number": draft.chapter_number,
            "title": draft.title,
            "content": draft.content,
            "word_count": draft.word_count,
        }

    @app.get("/api/v1/books/{book_id}/chapters/{chapter_number}")
    async def get_chapter(book_id: str, chapter_number: int) -> dict[str, Any]:
        return {
            "draft": to_data(store.load_chapter(book_id, chapter_number)),
            "plan": to_data(store.load_plan(book_id, chapter_number)),
            "review": to_data(store.load_review(book_id, chapter_number, final=True)),
            "delta": to_data(store.load_delta(book_id, chapter_number)),
        }

    @app.get("/api/v1/books/{book_id}/context-snapshots")
    async def list_context_snapshots(book_id: str, limit: int = 30) -> dict[str, Any]:
        """查询已冻结的 Agent Context，供 Trace 抽屉和离线评测复盘。"""

        store.load_metadata(book_id)
        bounded_limit = min(max(limit, 1), 100)
        return {
            "snapshots": [
                {
                    "snapshot_id": item.snapshot_id,
                    "job_id": item.job_id,
                    "book_id": item.book_id,
                    "agent_role": item.agent_role,
                    "book_version": item.book_version,
                    "policy_version": item.policy_version,
                    "renderer_version": item.renderer_version,
                    "rendered_context": item.rendered_context,
                    "trace": dict(item.trace),
                    "created_at": item.created_at,
                }
                for item in context_snapshots.list_for_book(book_id, limit=bounded_limit)
            ]
        }

    @app.post("/api/v1/books/{book_id}/plans", status_code=202)
    async def prepare_plan(book_id: str, body: InstructionBody) -> dict[str, Any]:
        return _accepted(await supervisor.submit(job_type="prepare_chapter", book_id=book_id, payload=body.model_dump()))

    @app.get("/api/v1/books/{book_id}/proposals/{proposal_id}")
    async def get_proposal(book_id: str, proposal_id: str) -> dict[str, Any]:
        return to_data(service.load_chapter_plan_proposal(book_id=book_id, proposal_id=proposal_id))

    @app.post("/api/v1/books/{book_id}/chapters", status_code=202)
    async def confirm_plan(book_id: str, body: ConfirmPlanBody) -> dict[str, Any]:
        return _accepted(await supervisor.submit(job_type="confirm_plan", book_id=book_id, payload=body.model_dump()))

    @app.post("/api/v1/books/{book_id}/proposals/{proposal_id}/revise", status_code=202)
    async def revise_plan(book_id: str, proposal_id: str, body: RevisePlanBody) -> dict[str, Any]:
        return _accepted(await supervisor.submit(job_type="revise_plan", book_id=book_id, payload={"proposal_id": proposal_id, **body.model_dump()}))

    @app.post("/api/v1/books/{book_id}/proposals/{proposal_id}/cancel", status_code=202)
    async def cancel_plan(book_id: str, proposal_id: str) -> dict[str, Any]:
        return _accepted(await supervisor.submit(job_type="cancel_plan", book_id=book_id, payload={"proposal_id": proposal_id}))

    @app.post("/api/v1/books/{book_id}/chapters/batch", status_code=202)
    async def write_batch(book_id: str, body: BatchBody) -> dict[str, Any]:
        return _accepted(await supervisor.submit(job_type="write_batch", book_id=book_id, payload=body.model_dump()))

    @app.post("/api/v1/books/{book_id}/chapters/{chapter_number}/rewrite", status_code=202)
    async def rewrite(book_id: str, chapter_number: int, body: InstructionBody) -> dict[str, Any]:
        return _accepted(await supervisor.submit(job_type="rewrite_chapter", book_id=book_id, payload={"chapter_number": chapter_number, **body.model_dump()}))

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        try:
            return _job_data(jobs.get(job_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/jobs/{job_id}/retry", status_code=202)
    async def retry_job(job_id: str) -> dict[str, Any]:
        try:
            return _accepted(await supervisor.retry(job_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/jobs/{job_id}/events")
    async def stream_events(job_id: str, after: int = 0) -> StreamingResponse:
        try:
            jobs.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return StreamingResponse(_sse(jobs, job_id, after), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


async def _sse(jobs: JobRepository, job_id: str, after: int) -> AsyncIterator[str]:
    cursor = max(0, after)
    while True:
        for event in jobs.events_after(job_id, after_sequence=cursor):
            cursor = event.sequence
            payload = {"sequence": event.sequence, "job_id": event.job_id, "event_type": event.event_type, "created_at": event.created_at, "payload": dict(event.payload)}
            yield f"id: {cursor}\nevent: {event.event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        job = jobs.get(job_id)
        if job.status in {"succeeded", "failed", "paused", "interrupted"}:
            return
        yield ": keep-alive\n\n"
        await asyncio.sleep(0.75)


async def _run_sse(run_progress: RunProgressStore, run_id: str, after: int) -> AsyncIterator[str]:
    """把旧 UI 进度模型映射为 FastAPI SSE，支持页面刷新后按序号继续。"""

    # Job 在 HTTP 响应之后才会取得执行权；SSE 先连上是正常竞态，短暂等待
    # start_run 而不是把这类请求误报为 404。
    available = await asyncio.to_thread(run_progress.wait_for_run, run_id, timeout=3.0)
    if not available:
        yield "event: unavailable\ndata: {}\n\n"
        return
    cursor = max(0, after)
    while True:
        events, terminal = await asyncio.to_thread(
            run_progress.wait_after,
            run_id,
            after_sequence=cursor,
            timeout=0.75,
        )
        for event in events:
            cursor = event.sequence
            payload = progress_event_data(event)
            yield f"id: {cursor}\nevent: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        if terminal:
            return
        if not events:
            yield ": keep-alive\n\n"


def _job_data(job: Any) -> dict[str, Any]:
    return {"job_id": job.job_id, "type": job.job_type, "book_id": job.book_id, "lock_scope": job.lock_scope, "status": job.status, "payload": dict(job.payload), "result": dict(job.result) if job.result else None, "error": job.error, "created_at": job.created_at, "updated_at": job.updated_at}


def _accepted(job: Any) -> dict[str, Any]:
    return {"job_id": job.job_id, "status": job.status, "events_url": f"/api/v1/jobs/{job.job_id}/events"}


def _action_proposal_data(proposal: Any) -> dict[str, Any]:
    return {
        "action_proposal_id": proposal.proposal_id, "session_id": proposal.session_id,
        "book_id": proposal.book_id, "action_type": proposal.action_type,
        "payload": dict(proposal.payload), "summary": proposal.summary, "status": proposal.status,
        "job_id": proposal.job_id, "created_at": proposal.created_at,
        "confirmed_at": proposal.confirmed_at, "expires_at": proposal.expires_at,
        "updated_at": proposal.updated_at,
    }


def _creative_control_data(control: Any) -> dict[str, Any]:
    return {
        "book_id": control.book_id,
        "author_intent": control.author_intent,
        "current_focus": control.current_focus,
        "current_focus_mode": control.current_focus_mode,
        "focus_target_chapter": control.focus_target_chapter,
        "updated_at": control.updated_at,
    }


def _session_data(session: Any) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "title": session.title,
        "book_id": session.book_id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
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


def _workspace_session_data(
    workspace: ChatWorkspaceApplication,
    session_id: str,
    before_sequence: int | None,
    limit: int,
) -> dict[str, Any]:
    """沿用旧 app.js 的 session + timeline + paging 响应形状。"""

    session = workspace.sessions.load_session(session_id)
    page = workspace.sessions.load_timeline(
        session_id,
        before_sequence=before_sequence,
        limit=limit,
    )
    data = workspace.session_data(session)
    visible_messages = {
        event.sequence for event in page.events if event.event_type == "message_added"
    }
    data["messages"] = [
        item for item in data["messages"] if item["sequence"] in visible_messages
    ]
    data["timeline"] = [workspace.timeline_data(event) for event in page.events]
    data["paging"] = {
        "has_more": page.has_more,
        "next_before_sequence": page.next_before_sequence,
    }
    return data
