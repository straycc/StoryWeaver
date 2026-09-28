"""StoryWeaver FastAPI 应用。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, nullcontext
from dataclasses import replace
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..novel_creation.application import PROJECT_ROOT, NovelApplicationSettings, build_novel_service
from ..novel_creation.exceptions import BookBusyError, NovelCreationError
from ..novel_creation.observability import NovelRunObserver
from ..novel_creation.serialization import to_data
from ..persistence import ActionProposalRepository, ContextSnapshotRepository, CreativeControlRepository, Database, DatabaseSettings, DeletionConflictError, JobRepository, SQLAlchemyChatSessionRepository, SQLAlchemyDeletionRepository, SQLAlchemyLongTermMemoryStore, SQLAlchemyStoryProjectRepository
from ..persistence.action_proposals import MAX_FOUNDATION_GENERATION_ATTEMPTS
from ..persistence import SimulationRepository
from ..story_simulation.service import RoleplayService
from ..story_simulation.agent import (CHARACTER_SYSTEM_PROMPT, DIRECTOR_SYSTEM_PROMPT,
                                      CharacterAgent, SceneDirectorAgent)
from ..story_simulation.runtime import RoleplayRuntime
from ..story_simulation.service import public_state
from ..llm import OpenAICompatibleProviderSettings, WorkerSettings
from ..model_config import ModelCatalog, ModelConfigurationError, RoutedModel
from ..model_config.store import ProviderConfig, bind_model
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


class ProviderBody(ProviderConfig):
    api_key: str | None = None
    clear_key: bool = False


class ModelSelectionBody(BaseModel):
    provider_id: str
    model_id: str


class ReasoningSelectionBody(BaseModel):
    level: Literal["default", "off", "low", "medium", "high", "max"]


class ConfirmActionProposalBody(BaseModel):
    proposal_id: str


class ConfirmFoundationProposalBody(BaseModel):
    version: int = Field(ge=1)


class _FoundationEditBody(BaseModel):
    """作者只可提交故事语义字段，拒绝系统结构字段。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CharacterFoundationPatch(_FoundationEditBody):
    character_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=2000)
    personality: list[str] = Field(min_length=1, max_length=20)
    motivation: str = Field(min_length=1, max_length=4000)
    long_term_goal: str = Field(min_length=1, max_length=4000)
    conflict: str = Field(min_length=1, max_length=4000)
    speech_style: str = Field(min_length=1, max_length=2000)
    knowledge_boundaries: list[str] = Field(default_factory=list, max_length=30)


class OutlineFoundationPatch(_FoundationEditBody):
    node_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=300)
    chapter_start: int = Field(ge=1)
    chapter_end: int = Field(ge=1)
    goal: str = Field(min_length=1, max_length=4000)
    expected_changes: list[str] = Field(default_factory=list, max_length=30)


class HookFoundationPatch(_FoundationEditBody):
    hook_id: str = Field(min_length=1)
    name: str = Field(default="", max_length=300)
    description: str = Field(min_length=1, max_length=4000)
    importance: int = Field(ge=1, le=5)
    expected_payoff: str = Field(min_length=1, max_length=4000)


class FoundationPatch(_FoundationEditBody):
    premise: str | None = Field(default=None, min_length=1, max_length=8000)
    world_setting: str | None = Field(default=None, min_length=1, max_length=8000)
    central_conflict: str | None = Field(default=None, min_length=1, max_length=8000)
    ending_direction: str | None = Field(default=None, min_length=1, max_length=8000)
    writing_rules: list[str] | None = Field(default=None, min_length=1, max_length=40)
    characters: list[CharacterFoundationPatch] | None = None
    outline: list[OutlineFoundationPatch] | None = None
    initial_hooks: list[HookFoundationPatch] | None = None


class UpdateFoundationProposalBody(BaseModel):
    version: int = Field(ge=1)
    patch: FoundationPatch


class CreateFoundationRevisionBody(BaseModel):
    session_id: str = Field(min_length=1)
    scope: Literal["outline", "setting"]


def _apply_foundation_patch(foundation: Any, patch: FoundationPatch) -> Any:
    """合并作者可编辑字段，系统 ID、状态及进度始终来自原候选。"""

    updates: dict[str, Any] = {}
    for field_name in (
        "premise",
        "world_setting",
        "central_conflict",
        "ending_direction",
    ):
        value = getattr(patch, field_name)
        if value is not None:
            updates[field_name] = value
    if patch.writing_rules is not None:
        updates["writing_rules"] = tuple(patch.writing_rules)
    if patch.characters is not None:
        existing = {item.character_id: item for item in foundation.characters}
        submitted = {item.character_id for item in patch.characters}
        if submitted != set(existing) or len(submitted) != len(patch.characters):
            raise ValueError("当前版本不支持新增或删除人物，请保留已有全部人物")
        updates["characters"] = tuple(
            replace(
                existing[item.character_id],
                name=item.name,
                role=item.role,
                personality=tuple(item.personality),
                motivation=item.motivation,
                long_term_goal=item.long_term_goal,
                conflict=item.conflict,
                speech_style=item.speech_style,
                knowledge_boundaries=tuple(item.knowledge_boundaries),
            )
            for item in patch.characters
        )
    if patch.outline is not None:
        existing = {item.node_id: item for item in foundation.outline}
        submitted = {item.node_id for item in patch.outline}
        if submitted != set(existing) or len(submitted) != len(patch.outline):
            raise ValueError("当前版本不支持新增或删除总纲节点，请保留已有全部节点")
        updates["outline"] = tuple(
            replace(
                existing[item.node_id],
                title=item.title,
                chapter_start=item.chapter_start,
                chapter_end=item.chapter_end,
                goal=item.goal,
                expected_changes=tuple(item.expected_changes),
            )
            for item in patch.outline
        )
    if patch.initial_hooks is not None:
        existing = {item.hook_id: item for item in foundation.initial_hooks}
        submitted = {item.hook_id for item in patch.initial_hooks}
        if submitted != set(existing) or len(submitted) != len(patch.initial_hooks):
            raise ValueError("当前版本不支持新增或删除伏笔，请保留已有全部伏笔")
        updates["initial_hooks"] = tuple(
            replace(
                existing[item.hook_id],
                name=item.name,
                description=item.description,
                importance=item.importance,
                expected_payoff=item.expected_payoff,
            )
            for item in patch.initial_hooks
        )
    return replace(foundation, **updates)


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


def create_app(*, settings: NovelApplicationSettings, database_url: str, model_catalog: ModelCatalog | None = None) -> FastAPI:
    """创建单实例 API；调用方必须以单 Uvicorn worker 启动。"""

    database = Database(DatabaseSettings(database_url))
    store = SQLAlchemyStoryProjectRepository(database)
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
    sessions = SQLAlchemyChatSessionRepository(database)
    deletions = SQLAlchemyDeletionRepository(database)
    memories = SQLAlchemyLongTermMemoryStore(database)
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
    shared_model = RoutedModel(model_catalog) if model_catalog else OpenAICompatibleProviderSettings(
        base_url=settings.base_url, model_name=settings.model, api_key=settings.api_key,
    ).create_provider().get_model(settings.model)
    service = build_novel_service(
        settings,
        store=store,
        observer=observer,
        creative_control_provider=creative_controls.get,
        context_snapshot_sink=context_snapshots,
        model_override=shared_model,
    )
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
        action_proposals=action_proposals,
        model_override=shared_model,
    )

    def effective_model_selection(session_id: str) -> tuple[str, str] | None:
        selected = sessions.selected_model(session_id)
        if model_catalog is None:
            return selected
        config = model_catalog.public()
        providers = config["providers"]
        if selected and any(item["id"] == selected[0] and selected[1] in item["models"] for item in providers):
            return selected
        if config["default_provider"] and config["default_model"]:
            return str(config["default_provider"]), str(config["default_model"])
        if providers:
            return str(providers[0]["id"]), str(providers[0]["models"][0])
        return None

    def model_binding(payload: Mapping[str, Any]):
        if model_catalog is None:
            return nullcontext()
        session_id = payload.get("session_id")
        selected = effective_model_selection(str(session_id)) if session_id else None
        provider, model_id, _key = model_catalog.selection(*(selected or (None, None)))
        model = model_catalog.create_model(provider.id, model_id)
        level = sessions.selected_reasoning(str(session_id)) if session_id else "default"
        if level not in model_catalog.reasoning_levels(provider, model_id):
            level = "default"
        return bind_model(model, reasoning_level=level, reasoning_family=model_catalog.reasoning_family(provider, model_id))

    supervisor = JobSupervisor(
        service=service, jobs=jobs, workspace=workspace,
        creative_controls=creative_controls, roleplay=roleplay, live_previews=live_previews,
        bind_model_for_payload=model_binding,
    )
    dispatcher = ActionDispatcher(
        proposals=action_proposals, jobs=jobs, workspace=workspace, submit_job=supervisor.submit,
    )
    dispatcher.configure_skills(skills, skill_service)
    workflow_reader = WorkflowContextReader(
        jobs=jobs, proposals=action_proposals, workspace=workspace,
    )
    action_surface = MainAgentActionSurface(
        agent=MainAgent(settings, model=shared_model), proposals=action_proposals, dispatcher=dispatcher, workspace=workspace,
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
        try:
            database.acquire_instance_lock()
            database.create_schema()
            supervisor.interrupt_orphaned_jobs()
            sessions.prune_duplicate_empty_sessions()
            yield
        finally:
            try:
                await supervisor.shutdown()
            finally:
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

    @app.exception_handler(ModelConfigurationError)
    async def model_configuration_error(_request: Request, exc: ModelConfigurationError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/api/v1/models/config")
    async def get_model_config() -> dict[str, Any]:
        return model_catalog.public() if model_catalog else {"providers": [], "default_provider": None, "default_model": None}

    @app.put("/api/v1/models/providers/{provider_id}")
    async def save_model_provider(provider_id: str, body: ProviderBody) -> dict[str, Any]:
        if model_catalog is None:
            raise HTTPException(status_code=409, detail="当前服务未启用模型配置")
        if provider_id != body.id:
            raise HTTPException(status_code=422, detail="供应商 ID 不一致")
        return model_catalog.upsert(ProviderConfig.model_validate(body.model_dump(exclude={"api_key", "clear_key"})), api_key=body.api_key, clear_key=body.clear_key)

    @app.delete("/api/v1/models/providers/{provider_id}")
    async def delete_model_provider(provider_id: str) -> dict[str, Any]:
        if model_catalog is None:
            raise HTTPException(status_code=409, detail="当前服务未启用模型配置")
        try:
            return model_catalog.delete(provider_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="供应商不存在") from exc

    @app.put("/api/v1/models/default")
    async def set_default_model(body: ModelSelectionBody) -> dict[str, Any]:
        if model_catalog is None:
            raise HTTPException(status_code=409, detail="当前服务未启用模型配置")
        return model_catalog.set_default(body.provider_id, body.model_id)

    @app.post("/api/v1/models/test")
    async def test_model(body: ModelSelectionBody) -> dict[str, bool]:
        if model_catalog is None:
            raise HTTPException(status_code=409, detail="当前服务未启用模型配置")
        provider, model_id, key = model_catalog.selection(body.provider_id, body.model_id)
        from openai import AsyncOpenAI
        try:
            async with AsyncOpenAI(base_url=provider.base_url, api_key=key or "local", timeout=15.0) as client:
                await client.chat.completions.create(model=model_id, messages=[{"role": "user", "content": "请回复 OK"}], max_tokens=8)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"连接测试失败：{type(exc).__name__}") from exc
        return {"ok": True}

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
        """旧工作台首次加载所需的完整索引，数据源为 SQLite。"""

        default_model = model_catalog.public()["default_model"] if model_catalog else settings.model
        return {**workspace.bootstrap(), "model": default_model or "未配置模型", "skills": [
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
        session = sessions.create_session(title=body.title, book_id=body.book_id)
        data = _session_data(session)
        selected = sessions.selected_model(session.session_id)
        data["selected_model"] = {"provider_id": selected[0], "model_id": selected[1]} if selected else None
        data["reasoning_level"] = sessions.selected_reasoning(session.session_id)
        return data

    @app.get("/api/v1/sessions/{session_id}")
    async def get_session(session_id: str, before_sequence: int | None = None, limit: int = 50) -> dict[str, Any]:
        try:
            data = _workspace_session_data(workspace, session_id, before_sequence, limit)
            selected = sessions.selected_model(session_id)
            if selected != effective_model_selection(session_id):
                selected = None
            data["selected_model"] = {"provider_id": selected[0], "model_id": selected[1]} if selected else None
            data["reasoning_level"] = sessions.selected_reasoning(session_id)
            return data
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/v1/sessions/{session_id}/model")
    async def select_session_model(session_id: str, body: ModelSelectionBody) -> dict[str, str]:
        if model_catalog is None:
            raise HTTPException(status_code=409, detail="当前服务未启用模型配置")
        sessions.load_session(session_id)
        model_catalog.selection(body.provider_id, body.model_id)
        sessions.append_event(session_id, event_type="session_model_selected", payload=body.model_dump())
        sessions.append_event(session_id, event_type="session_reasoning_selected", payload={"level": "default"})
        return body.model_dump()

    @app.put("/api/v1/sessions/{session_id}/reasoning")
    async def select_session_reasoning(session_id: str, body: ReasoningSelectionBody) -> dict[str, str]:
        if model_catalog is None:
            raise HTTPException(status_code=409, detail="当前服务未启用模型配置")
        sessions.load_session(session_id)
        selected = effective_model_selection(session_id)
        provider, model_id, _key = model_catalog.selection(*(selected or (None, None)))
        if body.level not in model_catalog.reasoning_levels(provider, model_id):
            raise HTTPException(status_code=422, detail="当前供应商不支持这个思考等级")
        sessions.append_event(session_id, event_type="session_reasoning_selected", payload=body.model_dump())
        return body.model_dump()

    @app.patch("/api/v1/sessions/{session_id}")
    async def bind_session_book(session_id: str, body: CreateSessionBody) -> dict[str, Any]:
        try:
            # 用户主动关联作品时，建书讨论等已有消息应保留在同一会话。
            return _session_data(
                workspace.bind_book(session_id, body.book_id, allow_nonempty=True)
            )
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
        with model_binding({"session_id": session_id}):
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
    async def confirm_action_proposal(
        proposal_id: str,
        body: ConfirmFoundationProposalBody | None = None,
    ) -> dict[str, Any]:
        try:
            proposal = await dispatcher.confirm(
                proposal_id,
                expected_version=body.version if body is not None else None,
            )
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

    @app.patch("/api/v1/action-proposals/{proposal_id}/foundation")
    async def update_foundation_proposal(
        proposal_id: str,
        body: UpdateFoundationProposalBody,
    ) -> dict[str, Any]:
        """编辑待确认基础资料，并从编辑结果重建候选初始状态。"""

        try:
            proposal = action_proposals.get(proposal_id)
            if proposal.action_type not in {"confirm_foundation", "apply_foundation_revision"}:
                raise ValueError("该提案不是故事基础资料")
            request_data = proposal.payload.get("request")
            candidate_data = proposal.payload.get("candidate")
            if not isinstance(candidate_data, dict):
                raise ValueError("基础资料提案缺少候选内容")
            from ..novel_creation.models import CreateNovelRequest, NovelProject

            current = service.project_candidate_from_data(candidate_data)
            foundation = _apply_foundation_patch(current.foundation, body.patch)
            if proposal.action_type == "confirm_foundation":
                if not isinstance(request_data, dict):
                    raise ValueError("基础资料提案缺少创作要求")
                candidate = service.rebuild_project_candidate(
                    CreateNovelRequest(**request_data),
                    foundation,
                )
            else:
                # 正式作品修订不重置 state；但仍借用创建期校验检查字段完整性、
                # 角色唯一性和大纲范围。
                service.rebuild_project_candidate(
                    CreateNovelRequest(
                        title=current.metadata.title,
                        genre=current.metadata.genre,
                        premise=current.foundation.premise,
                        protagonist="",
                        central_conflict="",
                        tone="",
                        target_chapters=current.metadata.target_chapters,
                        chapter_target_words=current.metadata.chapter_target_words,
                        language=current.metadata.language,
                    ),
                    foundation,
                )
                committed = current.state.last_committed_chapter
                original_nodes = {item.node_id: item for item in current.foundation.outline}
                revised_nodes = {item.node_id: item for item in foundation.outline}
                if any(
                    revised_nodes[node_id] != node
                    for node_id, node in original_nodes.items()
                    if node.chapter_start <= committed
                ):
                    raise ValueError("已进入 Canon 的大纲节点不能直接修改")
                candidate = NovelProject(
                    metadata=current.metadata,
                    foundation=foundation,
                    state=current.state,
                )
            updated_payload = dict(proposal.payload)
            updated_payload["candidate"] = to_data(candidate)
            updated = action_proposals.replace_pending_payload(
                proposal_id,
                expected_version=body.version,
                payload=updated_payload,
                summary=f"《{candidate.metadata.title}》的故事基础资料已更新，等待确认。",
            )
            workspace.sessions.append_event(
                updated.session_id,
                event_type="action_proposal_pending",
                payload=workspace._action_proposal_data(updated),
            )
            return {"proposal": dispatcher.data(updated)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/v1/books/{book_id}/foundation-revisions")
    async def create_foundation_revision(
        book_id: str,
        body: CreateFoundationRevisionBody,
    ) -> dict[str, Any]:
        """从正式作品创建可编辑修订副本，不直接修改 Canon。"""

        try:
            sessions.load_session(body.session_id)
            project = store.load_project(book_id)
            # 一个范围只保留一份待确认草稿。再次点击编辑应恢复草稿，不能在聊天中
            # 不断产生新的内部确认提案。
            pending_revisions = [
                item
                for item in action_proposals.list_pending(session_id=body.session_id)
                if (
                    item.book_id == book_id
                    and item.action_type == "apply_foundation_revision"
                    and str(item.payload.get("scope") or "") == body.scope
                )
            ]
            if pending_revisions:
                current = pending_revisions[0]
                for stale in pending_revisions[1:]:
                    action_proposals.supersede(stale.proposal_id)
                return {"proposal": dispatcher.data(current)}
            proposal = action_proposals.create(
                session_id=body.session_id,
                book_id=book_id,
                action_type="apply_foundation_revision",
                payload={
                    "version": 1,
                    "scope": body.scope,
                    "candidate": to_data(project),
                },
                summary=(
                    "正在修订后续故事大纲，等待确认。"
                    if body.scope == "outline"
                    else "正在修订故事设定，等待确认。"
                ),
            )
            workspace.sessions.append_event(
                body.session_id,
                event_type="action_proposal_pending",
                payload=dispatcher.data(proposal),
            )
            return {"proposal": dispatcher.data(proposal)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/v1/action-proposals/{proposal_id}/regenerate", status_code=202)
    async def regenerate_foundation_proposal(proposal_id: str) -> dict[str, Any]:
        """用原始创作要求生成新候选；新候选成功后才替代旧候选。"""

        try:
            proposal = action_proposals.get(proposal_id)
            if proposal.status != "pending" or proposal.action_type != "confirm_foundation":
                raise ValueError("只能重新生成待确认的故事基础资料")
            generation_attempt = int(proposal.payload.get("generation_attempt") or 1)
            if generation_attempt >= MAX_FOUNDATION_GENERATION_ATTEMPTS:
                raise ValueError(
                    f"故事基础资料最多生成 {MAX_FOUNDATION_GENERATION_ATTEMPTS} 次；"
                    "请编辑或确认当前方案"
                )
            request_data = proposal.payload.get("request")
            if not isinstance(request_data, dict):
                raise ValueError("基础资料提案缺少创作要求")
            action_payload = {
                **request_data,
                "supersedes_proposal_id": proposal.proposal_id,
                "generation_attempt": generation_attempt + 1,
            }
            if "creative_task_context" in proposal.payload:
                action_payload["creative_task_context"] = proposal.payload["creative_task_context"]
            job = await supervisor.submit(
                job_type="session_action",
                book_id=None,
                payload={
                    "session_id": proposal.session_id,
                    "content": "重新生成故事基础资料",
                    "action": "create_novel",
                    "book_id": None,
                    "action_payload": action_payload,
                },
            )
            return _accepted(job)
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
            SQLAlchemyLongTermMemoryStore._encode(item)
            for item in memories.list_records(
                scope_type=MemoryScopeType(scope_type) if scope_type else None,
                scope_id=scope_id,
                status=LongTermMemoryStatus(status) if status else None,
            )
        ]}

    @app.delete("/api/v1/memories/{memory_id}")
    async def disable_memory(memory_id: str) -> dict[str, Any]:
        return {"memory": SQLAlchemyLongTermMemoryStore._encode(memories.disable(memory_id))}

    @app.post("/api/v1/memories/{memory_id}/restore")
    async def restore_memory(memory_id: str) -> dict[str, Any]:
        return {"memory": SQLAlchemyLongTermMemoryStore._encode(memories.restore(memory_id))}

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
            "memory": SQLAlchemyLongTermMemoryStore._encode(corrected),
            "replaced_memory_id": previous.memory_id,
        }

    @app.post("/api/v1/books", status_code=202)
    async def create_book(body: CreateBookBody) -> dict[str, Any]:
        # 作品必须先作为会话中的 FoundationProposal 供作者确认；保留路由仅为
        # 旧客户端提供明确错误，不能允许该入口绕过确认边界。
        raise HTTPException(
            status_code=409,
            detail="请通过会话中的“创建小说”生成并确认故事基础资料",
        )

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
