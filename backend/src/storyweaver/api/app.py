"""StoryWeaver FastAPI 应用。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from ..novel_creation.application import PROJECT_ROOT, NovelApplicationSettings, build_novel_service
from ..novel_creation.exceptions import BookBusyError, NovelCreationError
from ..novel_creation.observability import NovelRunObserver
from ..novel_creation.pipeline import CreateNovelPipeline
from ..novel_creation.serialization import to_data
from ..persistence import ActionProposalRepository, ContextSnapshotRepository, CreativeControlRepository, Database, DatabaseSettings, DeletionConflictError, JobRepository, PostgresChatSessionRepository, PostgresDeletionRepository, PostgresLongTermMemoryStore, PostgresStoryProjectRepository
from ..persistence import SimulationRepository
from ..story_simulation.service import RoleplayService
from ..story_simulation.agent import (CHARACTER_SYSTEM_PROMPT, DIRECTOR_SYSTEM_PROMPT,
                                      CharacterAgent, SceneDirectorAgent)
from ..story_simulation.runtime import RoleplayRuntime
from ..story_simulation.service import public_state
from ..llm import OpenAICompatibleProviderSettings, WorkerSettings
from agents import ModelSettings
from ..observability import ModelFailureDiagnosticWriter
from ..application.workspace import ChatWorkspaceApplication, build_chat_workspace
from .jobs import JobSupervisor
from .live_preview import LivePreviewHub
from .action_surface import ActionDispatcher, MainAgentActionSurface
from .main_agent import MainAgent
from .main_agent_context import MainAgentContextBuilder, WorkflowContextReader
from .creative_discussion import CreativeDiscussionService
from ..skills import (
    SkillInvocationResolver,
    SkillMaterializer,
    SkillResolver,
    SkillService,
    build_model_implicit_skill_selector,
    build_model_skill_selector,
    creative_task_to_data,
    load_configured_skills,
)


class _CreativeBody(BaseModel):
    skill_ids: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("skill_ids")
    @classmethod
    def validate_skill_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().lower() for item in value]
        if any(not item for item in normalized):
            raise ValueError("skill_ids 不能包含空值")
        if len(normalized) != len(set(normalized)):
            raise ValueError("skill_ids 不能包含重复 Skill")
        return normalized


class CreateBookBody(_CreativeBody):
    title: str
    genre: str
    premise: str
    protagonist: str
    central_conflict: str
    tone: str
    target_chapters: int = Field(gt=0)
    chapter_target_words: int = Field(gt=0)
    language: str = "zh"


class InstructionBody(_CreativeBody):
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


class SimulationCharacterBody(BaseModel):
    character_id: str | None = None
    name: str
    role: str
    goal: str
    secret: str
    public_profile: str | None = None
    origin: str = "sandbox_npc"


class CreateSimulationBody(BaseModel):
    base_chapter_number: int = Field(ge=1)
    mode: str = Field(pattern="^(roleplay|observer)$")
    user_character_id: str | None = None
    canonical_character_ids: list[str] = Field(default_factory=list)
    custom_characters: list[SimulationCharacterBody] = Field(default_factory=list)
    # 场景设定是导演辅助信息：留空时由基准章节状态推导。
    location: str = Field(default="", max_length=500)
    opening_direction: str = Field(default="", max_length=2000)


class SimulationTurnBody(_CreativeBody):
    client_request_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=1)
    input_type: str = Field(pattern="^(speech_action|director_event)$")
    content: str = Field(min_length=1, max_length=5000)
    target_character_id: str | None = Field(default=None, max_length=128)


class SimulationModeBody(BaseModel):
    expected_version: int = Field(ge=1)
    mode: str = Field(pattern="^(roleplay|observer)$")
    user_character_id: str | None = None


def create_app(*, settings: NovelApplicationSettings, database_url: str) -> FastAPI:
    """创建单实例 API；调用方必须以单 Uvicorn worker 启动。"""

    database = Database(DatabaseSettings(database_url))
    store = PostgresStoryProjectRepository(database)
    jobs = JobRepository(database)
    action_proposals = ActionProposalRepository(database)
    context_snapshots = ContextSnapshotRepository(database)
    simulations = SimulationRepository(database)
    skills = load_configured_skills(PROJECT_ROOT)
    skill_service = SkillService(skills)

    def activate_task(request: str, skill_ids: list[str]) -> dict[str, object]:
        try:
            return creative_task_to_data(
                skill_service.activate(request=request, skill_ids=tuple(skill_ids))
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    creative_controls = CreativeControlRepository(database)
    sessions = PostgresChatSessionRepository(database)
    deletions = PostgresDeletionRepository(database)
    memories = PostgresLongTermMemoryStore(database)
    live_previews = LivePreviewHub()

    def emit_run_event(run_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        """模型事件只投影到持久 JobEvent。"""

        try:
            jobs.append_event(run_id, event_type, payload)
        except (KeyError, ValueError):
            pass

    async def emit_live_preview(run_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        """正文流式预览只驻留在当前 FastAPI 进程内。"""

        agent_id = str(payload.get("agent_id") or "unknown")
        field = str(payload.get("field") or "text")
        if event_type == "preview_started":
            await live_previews.start(run_id, agent_id=agent_id, field=field)
        elif event_type == "preview_delta":
            await live_previews.append(run_id, agent_id=agent_id, field=field, delta=str(payload.get("delta") or ""))
        elif event_type == "preview_completed":
            await live_previews.complete(run_id, agent_id=agent_id, field=field)
        elif event_type == "preview_reset":
            await live_previews.reset(run_id, agent_id=agent_id, field=field)

    observer = NovelRunObserver(
        output=None,
        event_sink=emit_run_event,
        live_preview_sink=emit_live_preview,
    )
    service = build_novel_service(
        settings,
        store=store,
        observer=observer,
        creative_control_provider=creative_controls.get,
        context_snapshot_sink=context_snapshots,
    )
    provider = OpenAICompatibleProviderSettings(base_url=settings.base_url, model_name=settings.model, api_key=settings.api_key).create_provider()
    shared_model = provider.get_model(settings.model)
    skill_invocations = SkillInvocationResolver(
        registry=skills,
        selector=build_model_implicit_skill_selector(
            model=shared_model,
            timeout_seconds=settings.timeout_seconds,
        ),
    )

    async def activate_chat_task(
        *,
        session: object,
        request: str,
        explicit_skill_ids: list[str],
    ) -> dict[str, object]:
        # 最近对话只帮助判断“身份线”这类简短延续回答，不成为 Skill 状态源。
        # 每轮仍重新 Resolve，并为当前 Job 创建独立冻结 Snapshot。
        normalized_request = request.strip()
        if normalized_request == "/skills" or normalized_request.startswith("/skill "):
            return activate_task(request, explicit_skill_ids)
        recent_messages = tuple(getattr(session, "messages", ()))[-8:]
        recent_conversation = "\n".join(
            f"{getattr(item, 'role', 'unknown')}："
            f"{str(getattr(item, 'content', '')).strip()[:1200]}"
            for item in recent_messages
        )
        try:
            selected = await skill_invocations.resolve(
                request=request,
                recent_conversation=recent_conversation,
                explicit_skill_ids=tuple(explicit_skill_ids),
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return activate_task(request, list(selected))
    roleplay_skill_materializer = SkillMaterializer(
        SkillResolver(
            selector=build_model_skill_selector(
                model=shared_model,
                timeout_seconds=settings.timeout_seconds,
            )
        )
    )
    extra_body: dict[str, object] = {}
    if settings.thinking is not None:
        extra_body["thinking"] = {"type": settings.thinking}
    if settings.reasoning_effort is not None:
        extra_body["reasoning_effort"] = settings.reasoning_effort
    roleplay = RoleplayService(
        projects=store, simulations=simulations, snapshots=context_snapshots,
        runtime=RoleplayRuntime(
            director=SceneDirectorAgent(WorkerSettings(
                worker_id="roleplay-director", name="角色剧场导演", instructions=DIRECTOR_SYSTEM_PROMPT,
                model=shared_model, model_settings=ModelSettings(temperature=0.45, timeout=settings.timeout_seconds, extra_body=extra_body or None),
                timeout_seconds=settings.timeout_seconds, diagnostic_writer=ModelFailureDiagnosticWriter(settings.model_diagnostics_directory),
            )),
            character=CharacterAgent(WorkerSettings(
                worker_id="roleplay-character", name="角色剧场角色演员", instructions=CHARACTER_SYSTEM_PROMPT,
                model=shared_model, model_settings=ModelSettings(temperature=0.7, timeout=settings.timeout_seconds, extra_body=extra_body or None),
                timeout_seconds=settings.timeout_seconds, diagnostic_writer=ModelFailureDiagnosticWriter(settings.model_diagnostics_directory),
            )),
            snapshots=context_snapshots,
            skill_materializer=roleplay_skill_materializer,
        ),
    )
    workspace = build_chat_workspace(
        settings,
        sessions=sessions,
        memory_store=memories,
        novels=service,
    )
    supervisor = JobSupervisor(
        service=service, jobs=jobs, workspace=workspace,
        creative_controls=creative_controls, roleplay=roleplay, live_previews=live_previews,
    )
    dispatcher = ActionDispatcher(
        proposals=action_proposals, jobs=jobs, workspace=workspace, submit_job=supervisor.submit,
    )
    dispatcher.configure_skills(skills, skill_service)
    workflow_reader = WorkflowContextReader(
        jobs=jobs, proposals=action_proposals, workspace=workspace,
    )
    action_surface = MainAgentActionSurface(
        agent=MainAgent(settings), proposals=action_proposals, dispatcher=dispatcher, workspace=workspace,
        context_builder=MainAgentContextBuilder(
            workspace=workspace,
            creative_controls=creative_controls,
            workflow=workflow_reader,
            # 会话记忆只在 Main Agent 的对话理解阶段按需检索；创作 Pipeline
            # 不会读取它，正文约束仅来自创作控制和正史状态。
            memory_retriever=workspace.context_manager.memory_retriever,
        ),
        creative_discussion=CreativeDiscussionService(
            model=shared_model,
            timeout_seconds=settings.timeout_seconds,
            materializer=roleplay_skill_materializer,
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
    app.state.deletions = deletions
    app.state.memories = memories
    app.state.workspace = workspace
    app.state.action_proposals = action_proposals
    app.state.context_snapshots = context_snapshots
    app.state.creative_controls = creative_controls
    app.state.action_dispatcher = dispatcher
    app.state.skills = skills
    app.state.live_previews = live_previews
    app.state.simulations = simulations
    app.state.roleplay = roleplay

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
            {
                "id": item.skill_id,
                "name": item.name,
                "display_name": item.display_name,
                "description": item.description,
                "short_description": item.short_description,
                "source": item.source,
            }
            for item in dispatcher.available_skills()
        ]}

    @app.get("/api/v1/skills")
    async def list_skills() -> dict[str, Any]:
        return {"skills": [
            {
                "id": item.skill_id,
                "name": item.name,
                "display_name": item.display_name,
                "description": item.description,
                "short_description": item.short_description,
                "source": item.source,
            }
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
        try:
            deletions.delete_session(session_id)
            return {"ok": True}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DeletionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/sessions/{session_id}/messages", status_code=202)
    async def send_session_message(session_id: str, body: SessionMessageBody) -> dict[str, Any]:
        """所有聊天和预设动作都由持久 Job 执行，浏览器不再阻塞等待模型。"""

        session = sessions.load_session(session_id)
        bound_book_id = body.book_id or session.book_id
        # 按钮已经表达了明确意图，直接经过 Dispatcher，不额外消耗一次 Main Agent 调用。
        shortcut_actions = {
            "write_next": "prepare_chapter_plan",
            "run_next_chapter_workflow": "run_next_chapter_workflow",
            "revise_chapter_plan": "revise_chapter_plan",
            "approve_chapter_plan": "approve_chapter_plan",
            "write_from_plan": "write_from_plan",
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
        raw_skill_ids = body.payload.get("skill_ids", [])
        if not isinstance(raw_skill_ids, list):
            raise HTTPException(status_code=422, detail="skill_ids 必须是数组")
        explicit_skill_ids = [str(item) for item in raw_skill_ids]
        creative_task_context = (
            await activate_chat_task(
                session=session,
                request=body.content or "处理当前创作请求",
                explicit_skill_ids=explicit_skill_ids,
            )
            if body.action == "chat"
            else None
        )
        job = await supervisor.submit(
            job_type="session_action",
            book_id=bound_book_id,
            payload={
                "session_id": session_id,
                "content": body.content,
                "action": body.action,
                "book_id": body.book_id,
                "creative_task_context": creative_task_context,
                "action_payload": (
                    {
                        **body.payload,
                        "creative_task_context": activate_task(
                            json.dumps(
                                {
                                    key: value
                                    for key, value in body.payload.items()
                                    if key != "skill_ids"
                                },
                                ensure_ascii=False,
                            ),
                            [str(item) for item in body.payload.get("skill_ids", [])],
                        ),
                    }
                    if body.action == "create_novel"
                    else body.payload
                ),
            },
        )
        return _accepted(job)

    @app.get("/api/v1/sessions/{session_id}/action-proposals")
    async def list_action_proposals(session_id: str) -> dict[str, Any]:
        sessions.load_session(session_id)
        return {"proposals": [dispatcher.data(item) for item in action_proposals.list_pending(session_id=session_id)]}

    @app.post("/api/v1/action-proposals/{proposal_id}/confirm", status_code=202)
    async def confirm_action_proposal(proposal_id: str) -> dict[str, Any]:
        try:
            proposal = await dispatcher.confirm(proposal_id)
            workspace.sessions.append_event(
                proposal.session_id,
                event_type="action_proposal_confirmed",
                payload=dispatcher.data(proposal),
            )
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
            return {"proposal": dispatcher.data(dispatcher.cancel(proposal_id))}
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

    @app.delete("/api/v1/memories/{memory_id}")
    async def disable_memory(memory_id: str) -> dict[str, Any]:
        return {"memory": PostgresLongTermMemoryStore._encode(memories.disable(memory_id))}

    @app.post("/api/v1/memories/{memory_id}/restore")
    async def restore_memory(memory_id: str) -> dict[str, Any]:
        return {"memory": PostgresLongTermMemoryStore._encode(memories.restore(memory_id))}

    @app.delete("/api/v1/memories/{memory_id}/permanent", status_code=204)
    async def delete_memory_permanently(memory_id: str) -> None:
        """永久删除一条会话记忆；普通停用仍使用上方接口。"""

        memories.delete(memory_id)

    @app.post("/api/v1/memories/{memory_id}/corrections")
    async def correct_memory(memory_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """创建更正记录并替代原记录，避免直接篡改自动提取的历史。"""

        from uuid import uuid4

        from ..memory.long_term import LongTermMemoryRecord, LongTermMemoryStatus

        previous = memories.get(memory_id)
        content = str(body.get("content", "")).strip()
        if not content:
            raise HTTPException(status_code=422, detail="更正内容不能为空")
        description = str(body.get("description") or content).strip()
        name = str(body.get("name") or previous.name).strip()
        if not name or not description:
            raise HTTPException(status_code=422, detail="记忆名称和摘要不能为空")
        importance = body.get("importance", previous.importance)
        try:
            importance = max(1, min(5, int(importance)))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="重要性必须是 1 到 5") from exc
        now = memories.timestamp()
        corrected = LongTermMemoryRecord(
            memory_id=str(uuid4()),
            memory_type=previous.memory_type,
            scope_type=previous.scope_type,
            scope_id=previous.scope_id,
            name=name,
            description=description,
            content=content,
            importance=importance,
            source_refs=(*previous.source_refs, f"correction:{previous.memory_id}"),
            fingerprint=memories.fingerprint(content),
            status=LongTermMemoryStatus.ACTIVE,
            created_at=now,
            updated_at=now,
            supersedes_id=previous.memory_id,
        )
        if not memories.save(corrected):
            raise HTTPException(status_code=409, detail="相同内容的生效会话记忆已存在")
        return {
            "memory": PostgresLongTermMemoryStore._encode(corrected),
            "replaced_memory_id": previous.memory_id,
        }

    @app.post("/api/v1/books", status_code=202)
    async def create_book(body: CreateBookBody) -> dict[str, Any]:
        request_data = body.model_dump()
        skill_ids = request_data.pop("skill_ids")
        # 创建任务也按稳定 book_id 参与单活跃约束，避免重复简报并发建书。
        from ..novel_creation.models import CreateNovelRequest
        book_id = CreateNovelPipeline.build_book_id(CreateNovelRequest(**request_data))
        job = await supervisor.submit(job_type="create_book", book_id=book_id, payload={
            "request": request_data,
            "creative_task_context": activate_task(
                json.dumps(request_data, ensure_ascii=False),
                skill_ids,
            ),
        })
        return _accepted(job)

    @app.delete("/api/v1/books/{book_id}")
    async def delete_book(book_id: str) -> dict[str, Any]:
        try:
            result = deletions.delete_book(book_id)
            return {
                "ok": True,
                "book_id": result.book_id,
                "unbound_session_ids": list(result.unbound_session_ids),
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DeletionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/v1/books/{book_id}/metadata")
    async def get_book_metadata(book_id: str) -> dict[str, Any]:
        project = store.load_project(book_id)
        return {
            "metadata": to_data(project.metadata), "foundation": to_data(project.foundation),
            "state": to_data(project.state), "chapters": [to_data(item) for item in store.load_chapter_index(book_id)],
            "creative_control": _creative_control_data(creative_controls.get(book_id)),
        }

    @app.post("/api/v1/books/{book_id}/simulations", status_code=201)
    async def create_simulation(book_id: str, body: CreateSimulationBody) -> dict[str, Any]:
        try:
            result = roleplay.create(
                book_id=book_id, base_chapter_number=body.base_chapter_number, mode=body.mode,
                user_character_id=body.user_character_id, canonical_character_ids=tuple(body.canonical_character_ids),
                custom_characters=tuple(item.model_dump(exclude_none=True) for item in body.custom_characters),
                location=body.location, opening_direction=body.opening_direction,
            )
            return _simulation_data(result)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/v1/books/{book_id}/simulations")
    async def list_simulations(book_id: str) -> dict[str, Any]:
        store.load_metadata(book_id)
        return {"simulations": [_simulation_data(item) for item in simulations.list_for_book(book_id)]}

    @app.get("/api/v1/simulations/{simulation_id}")
    async def get_simulation(simulation_id: str) -> dict[str, Any]:
        try:
            return _simulation_data(simulations.get(simulation_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/simulations/{simulation_id}/turns")
    async def get_simulation_turns(simulation_id: str) -> dict[str, Any]:
        try:
            simulations.get(simulation_id)
            return {"turns": [
                {**_simulation_turn_data(item), "context_snapshot_links": list(simulations.list_context_snapshot_links(item.turn_id))}
                for item in simulations.list_turns(simulation_id)
            ]}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/simulations/{simulation_id}/turns", status_code=202)
    async def submit_simulation_turn(simulation_id: str, body: SimulationTurnBody) -> dict[str, Any]:
        try:
            simulation = simulations.get(simulation_id)
            if simulation.status != "active":
                raise ValueError("角色剧场已暂停或结束，请先恢复或创建新的剧场")
            existing = simulations.find_by_client_request(simulation_id=simulation_id, client_request_id=body.client_request_id)
            if existing is not None and existing.job_id:
                return _accepted(jobs.get(existing.job_id))
            payload = body.model_dump()
            skill_ids = payload.pop("skill_ids")
            job = await supervisor.submit(job_type="roleplay_turn", book_id=simulation.book_id, payload={
                "simulation_id": simulation_id,
                **payload,
                "creative_task_context": activate_task(body.content, skill_ids),
            })
            return _accepted(job)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/simulations/{simulation_id}/continue", status_code=202)
    async def continue_simulation(simulation_id: str, body: SimulationTurnBody) -> dict[str, Any]:
        try:
            simulation = simulations.get(simulation_id)
            if simulation.status != "active":
                raise ValueError("角色剧场已暂停或结束，无法继续推演")
            if simulation.mode != "observer":
                raise ValueError("只有旁观模式可以自动推演")
            existing = simulations.find_by_client_request(simulation_id=simulation_id, client_request_id=body.client_request_id)
            if existing is not None and existing.job_id:
                return _accepted(jobs.get(existing.job_id))
            job = await supervisor.submit(job_type="roleplay_turn", book_id=simulation.book_id, payload={
                "simulation_id": simulation_id,
                "client_request_id": body.client_request_id,
                "expected_version": body.expected_version,
                "input_type": "observer_continue",
                "content": body.content or "继续推演",
                "creative_task_context": activate_task(
                    body.content or "继续推演",
                    body.skill_ids,
                ),
            })
            return _accepted(job)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.patch("/api/v1/simulations/{simulation_id}/mode")
    async def update_simulation_mode(simulation_id: str, body: SimulationModeBody) -> dict[str, Any]:
        try:
            return _simulation_data(roleplay.change_mode(simulation_id=simulation_id, **body.model_dump()))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/simulations/{simulation_id}/{operation}")
    async def simulation_lifecycle(simulation_id: str, operation: str) -> dict[str, Any]:
        if operation not in {"pause", "resume", "finish"}:
            raise HTTPException(status_code=404, detail="不支持的模拟操作")
        try:
            simulation = simulations.get(simulation_id)
            transitions = {
                "pause": ({"active"}, "paused"),
                "resume": ({"paused"}, "active"),
                "finish": ({"active", "paused"}, "completed"),
            }
            allowed, target = transitions[operation]
            if simulation.status not in allowed:
                raise ValueError(
                    "已结束的角色剧场不可恢复" if simulation.status == "completed"
                    else "当前角色剧场状态不支持此操作"
                )
            return _simulation_data(simulations.set_status(simulation_id, target))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/api/v1/simulations/{simulation_id}")
    async def delete_simulation(simulation_id: str) -> dict[str, bool]:
        try:
            simulations.delete(simulation_id)
            return {"ok": True}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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

    @app.get("/api/v1/books/{book_id}")
    async def get_book(book_id: str) -> dict[str, Any]:
        """旧工作台的作品摘要形状，供等价 React UI 直接使用。"""

        project = store.load_project(book_id)
        profiles = {item.character_id: item for item in project.foundation.characters}
        return {
            "metadata": to_data(project.metadata),
            "foundation": to_data(project.foundation),
            "state": to_data(project.state),
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

    @app.get("/api/v1/books/{book_id}/chapters/{chapter_number}/content")
    async def get_chapter_content(book_id: str, chapter_number: int) -> dict[str, Any]:
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
        payload = body.model_dump()
        skill_ids = payload.pop("skill_ids")
        payload["creative_task_context"] = activate_task(
            body.user_instruction or "规划下一章",
            skill_ids,
        )
        return _accepted(await supervisor.submit(job_type="prepare_chapter", book_id=book_id, payload=payload))

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
        payload = body.model_dump()
        skill_ids = payload.pop("skill_ids")
        payload["creative_task_context"] = activate_task(
            body.user_instruction or f"连续创作 {body.count} 章",
            skill_ids,
        )
        return _accepted(await supervisor.submit(job_type="write_batch", book_id=book_id, payload=payload))

    @app.post("/api/v1/books/{book_id}/chapters/{chapter_number}/rewrite", status_code=202)
    async def rewrite(book_id: str, chapter_number: int, body: InstructionBody) -> dict[str, Any]:
        payload = body.model_dump()
        skill_ids = payload.pop("skill_ids")
        payload.update({
            "chapter_number": chapter_number,
            "creative_task_context": activate_task(
                body.user_instruction or f"重写第 {chapter_number} 章",
                skill_ids,
            ),
        })
        return _accepted(await supervisor.submit(job_type="rewrite_chapter", book_id=book_id, payload=payload))

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

    @app.post("/api/v1/jobs/{job_id}/cancel", status_code=202)
    async def cancel_job(job_id: str) -> dict[str, Any]:
        try:
            return _accepted(await supervisor.cancel(job_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/v1/jobs/{job_id}/events")
    async def stream_events(
        job_id: str,
        after: int = 0,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        try:
            jobs.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        cursor = max(after, _event_cursor(last_event_id))
        return StreamingResponse(_sse(jobs, live_previews, job_id, cursor), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


async def _sse(jobs: JobRepository, previews: LivePreviewHub, job_id: str, after: int) -> AsyncIterator[str]:
    cursor = max(0, after)
    # 先订阅 Hub，再回放数据库事件；这样连接建立期间产生的 preview 可以由
    # snapshot 补齐，后续增量则走内存队列。
    async with previews.subscribe(job_id) as (preview_queue, snapshots):
        for snapshot in snapshots:
            yield _sse_event(snapshot)
        while True:
            for event in jobs.events_after(job_id, after_sequence=cursor):
                cursor = event.sequence
                payload = {"sequence": event.sequence, "job_id": event.job_id, "event_type": event.event_type, "created_at": event.created_at, "payload": dict(event.payload)}
                yield _sse_event(payload, event_id=cursor)
            # 先清空已经抵达的实时预览，再检查 Job 终态；否则 Writer 最后一小段
            # 文本可能与成功事件竞争，尚未来得及显示就被 SSE 连接关闭。
            while not preview_queue.empty():
                yield _sse_event(preview_queue.get_nowait())
            job = jobs.get(job_id)
            if job.status in {"succeeded", "failed", "paused", "interrupted", "cancelled"}:
                return
            try:
                preview = await asyncio.wait_for(preview_queue.get(), timeout=0.75)
                yield _sse_event(preview)
            except TimeoutError:
                yield ": keep-alive\n\n"


def _sse_event(payload: Mapping[str, Any], *, event_id: int | None = None) -> str:
    event_type = str(payload.get("event_type") or "message")
    identifier = f"id: {event_id}\n" if event_id is not None else ""
    return f"{identifier}event: {event_type}\ndata: {json.dumps(dict(payload), ensure_ascii=False)}\n\n"


def _event_cursor(value: str | None) -> int:
    """将浏览器自动回传的 Last-Event-ID 安全转换为重放游标。"""

    try:
        return max(0, int(value or 0))
    except ValueError:
        return 0


def _job_data(job: Any) -> dict[str, Any]:
    return {"job_id": job.job_id, "type": job.job_type, "book_id": job.book_id, "lock_scope": job.lock_scope, "status": job.status, "payload": dict(job.payload), "result": dict(job.result) if job.result else None, "error": job.error, "created_at": job.created_at, "updated_at": job.updated_at}


def _accepted(job: Any) -> dict[str, Any]:
    return {"job_id": job.job_id, "status": job.status, "events_url": f"/api/v1/jobs/{job.job_id}/events"}


def _creative_control_data(control: Any) -> dict[str, Any]:
    return {
        "book_id": control.book_id,
        "author_intent": control.author_intent,
        "current_focus": control.current_focus,
        "current_focus_mode": control.current_focus_mode,
        "focus_target_chapter": control.focus_target_chapter,
        "updated_at": control.updated_at,
    }


def _simulation_data(value: Any) -> dict[str, Any]:
    return {**public_state(value), "book_id": value.book_id, "base_book_version": value.base_book_version,
            "base_chapter_number": value.base_chapter_number, "scene_config": dict(value.scene_config),
            "created_at": value.created_at, "updated_at": value.updated_at}


def _simulation_turn_data(value: Any) -> dict[str, Any]:
    return {"turn_id": value.turn_id, "simulation_id": value.simulation_id, "turn_number": value.turn_number,
            "job_id": value.job_id, "client_request_id": value.client_request_id, "mode": value.mode,
            "user_character_id": value.user_character_id, "target_character_id": value.target_character_id,
            "input_type": value.input_type, "user_input": value.user_input,
            "status": value.status, "output": value.output, "state_delta": value.state_delta,
            "context_snapshot_id": value.context_snapshot_id, "created_at": value.created_at}


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
