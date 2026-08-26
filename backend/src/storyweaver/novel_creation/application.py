"""小说创作应用服务和真实模型 Worker 组装。"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from agents import ModelSettings

from ..llm.errors import ConfigurationError
from ..observability import ModelFailureDiagnosticWriter
from ..llm import OpenAICompatibleProviderSettings, WorkerSettings
from .agents import (
    ArchitectAgent,
    ChapterAnalyzerAgent,
    PlannerAgent,
    ReviewerAgent,
    ReviserAgent,
    WriterAgent,
)
from .agents.architect import ARCHITECT_SYSTEM_PROMPT
from .agents.chapter_analyzer import CHAPTER_ANALYZER_SYSTEM_PROMPT
from .agents.reviser import REVISER_SYSTEM_PROMPT
from .agents.writer import WRITER_SYSTEM_PROMPT
from .agents.planner import PLANNER_SYSTEM_PROMPT
from .agents.reviewer import REVIEWER_SYSTEM_PROMPT
from .context_builder import ChapterContextBuilder
from .execution_lock import BookExecutionLockManager
from .models import (
    BatchPlanningContext,
    BookMetadata,
    ChapterPlanProposal,
    ChapterRewriteRecord,
    ChapterResult,
    CreateNovelRequest,
    NovelProject,
)
from .observability import NovelRunObserver
from .pipeline import CreateNovelPipeline, WriteNextChapterPipeline
from .repository import StoryProjectRepository
from .quality_gate import ReviewQualityGate, ReviewQualityGatePolicy
from .sdk_tracing import configure_local_sdk_tracing


PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_MODEL_DIAGNOSTICS_DIRECTORY = (
    PROJECT_ROOT / "runtime" / "diagnostics" / "model_failures"
)
DEFAULT_AGENT_TRACE_DIRECTORY = PROJECT_ROOT / "runtime" / "traces"
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_env_file(path: str | Path = PROJECT_ROOT / ".env") -> bool:
    """用标准库加载简单 ``.env``，且不覆盖进程已有变量。"""

    env_path = Path(path)
    if not env_path.is_file():
        return False
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ConfigurationError(f"无法读取配置文件 {env_path}：{exc}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError(
                f"{env_path} 第 {line_number} 行缺少等号"
            )
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not _ENV_NAME_PATTERN.fullmatch(key):
            raise ConfigurationError(
                f"{env_path} 第 {line_number} 行变量名不合法"
            )
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return True


def _read_env(primary_name: str, legacy_name: str | None = None) -> str | None:
    value = os.getenv(primary_name)
    if value is not None:
        return value
    return os.getenv(legacy_name) if legacy_name else None


def _read_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是数字") from exc


def _read_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是整数") from exc


@dataclass(frozen=True, slots=True)
class NovelApplicationSettings:
    """正式 Web 运行时所需配置。"""

    base_url: str
    model: str
    api_key: str | None = field(default=None, repr=False)
    temperature: float = 0.8
    timeout_seconds: float = 180.0
    reviewer_turn_timeout_seconds: float = 60.0
    reasoning_effort: str | None = None
    thinking: str | None = None
    json_mode: str = "auto"
    architect_temperature: float = 0.4
    planner_temperature: float = 0.3
    writer_temperature: float = 0.8
    reviewer_temperature: float = 0.2
    reviser_temperature: float = 0.6
    analyzer_temperature: float = 0.1
    context_token_budget: int = 6000
    review_policy: str = "strict"
    review_minimum_score: int = 80
    review_minimum_target_ratio: float = 0.5
    review_maximum_target_ratio: float = 1.8
    review_warning_count_threshold: int = 3
    review_max_revision_rounds: int = 1
    model_diagnostics_directory: Path = DEFAULT_MODEL_DIAGNOSTICS_DIRECTORY
    agent_trace_directory: Path = DEFAULT_AGENT_TRACE_DIRECTORY

    def __post_init__(self) -> None:
        if not self.base_url.strip():
            raise ConfigurationError("模型 base_url 不能为空")
        if not self.model.strip():
            raise ConfigurationError(
                "缺少 STORYWEAVER_LLM_MODEL，请在 .env 中配置模型名称"
            )
        if not isinstance(self.model_diagnostics_directory, Path):
            raise TypeError("model_diagnostics_directory 必须是 Path")
        if not isinstance(self.agent_trace_directory, Path):
            raise TypeError("agent_trace_directory 必须是 Path")
        if not 0 <= self.temperature <= 2:
            raise ConfigurationError("temperature 必须在 0 到 2 之间")
        if self.timeout_seconds <= 0:
            raise ConfigurationError("模型超时时间必须大于 0")
        if self.reviewer_turn_timeout_seconds <= 0:
            raise ConfigurationError("Reviewer 单回合超时时间必须大于 0")
        if self.reasoning_effort is not None and not self.reasoning_effort.strip():
            raise ConfigurationError("STORYWEAVER_LLM_REASONING_EFFORT 不能为空")
        if self.thinking not in {None, "enabled", "disabled"}:
            raise ConfigurationError(
                "STORYWEAVER_LLM_THINKING 只支持 enabled 或 disabled"
            )
        if self.json_mode not in {"auto", "enabled", "disabled"}:
            raise ConfigurationError(
                "STORYWEAVER_LLM_JSON_MODE 只支持 auto、enabled 或 disabled"
            )
        for field_name in (
            "architect_temperature",
            "planner_temperature",
            "writer_temperature",
            "reviewer_temperature",
            "reviser_temperature",
            "analyzer_temperature",
        ):
            value = getattr(self, field_name)
            if not 0 <= value <= 2:
                raise ConfigurationError(f"{field_name} 必须在 0 到 2 之间")
        if self.context_token_budget <= 0:
            raise ConfigurationError("Context Token 预算必须大于 0")
        if self.review_policy not in WriteNextChapterPipeline.REVIEW_POLICIES:
            raise ConfigurationError(
                "STORYWEAVER_REVIEW_POLICY 只支持 strict 或 auto"
            )
        try:
            ReviewQualityGatePolicy(
                minimum_score=self.review_minimum_score,
                minimum_target_ratio=self.review_minimum_target_ratio,
                maximum_target_ratio=self.review_maximum_target_ratio,
                warning_count_threshold=self.review_warning_count_threshold,
                max_revision_rounds=self.review_max_revision_rounds,
            )
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"Review QualityGate 配置无效：{exc}") from exc

    @classmethod
    def from_env(
        cls,
    ) -> "NovelApplicationSettings":
        base_url = _read_env(
            "STORYWEAVER_LLM_BASE_URL",
            "LLM_BASE_URL",
        ) or "http://localhost:11434/v1"
        model = _read_env("STORYWEAVER_LLM_MODEL", "LLM_MODEL_ID") or ""
        api_key = _read_env("STORYWEAVER_LLM_API_KEY", "LLM_API_KEY")
        reasoning_effort = os.getenv("STORYWEAVER_LLM_REASONING_EFFORT")
        thinking = os.getenv("STORYWEAVER_LLM_THINKING")
        json_mode = os.getenv("STORYWEAVER_LLM_JSON_MODE", "auto")
        configured_diagnostics = os.getenv("STORYWEAVER_DIAGNOSTICS_DIR")
        configured_traces = os.getenv("STORYWEAVER_TRACE_DIR")
        return cls(
            base_url=base_url,
            model=model,
            api_key=api_key,
            temperature=_read_float("STORYWEAVER_LLM_TEMPERATURE", 0.8),
            timeout_seconds=_read_float("STORYWEAVER_LLM_TIMEOUT", 180.0),
            reviewer_turn_timeout_seconds=_read_float(
                "STORYWEAVER_REVIEWER_TURN_TIMEOUT",
                60.0,
            ),
            reasoning_effort=reasoning_effort.strip() if reasoning_effort else None,
            thinking=thinking.strip().lower() if thinking else None,
            json_mode=json_mode.strip().lower(),
            architect_temperature=_read_float("STORYWEAVER_ARCHITECT_TEMPERATURE", 0.4),
            planner_temperature=_read_float("STORYWEAVER_PLANNER_TEMPERATURE", 0.3),
            writer_temperature=_read_float("STORYWEAVER_WRITER_TEMPERATURE", 0.8),
            reviewer_temperature=_read_float("STORYWEAVER_REVIEWER_TEMPERATURE", 0.2),
            reviser_temperature=_read_float("STORYWEAVER_REVISER_TEMPERATURE", 0.6),
            analyzer_temperature=_read_float("STORYWEAVER_ANALYZER_TEMPERATURE", 0.1),
            context_token_budget=_read_int(
                "STORYWEAVER_CONTEXT_TOKEN_BUDGET",
                6000,
            ),
            review_policy=os.getenv("STORYWEAVER_REVIEW_POLICY", "strict").strip(),
            review_minimum_score=_read_int(
                "STORYWEAVER_REVIEW_MINIMUM_SCORE",
                80,
            ),
            review_minimum_target_ratio=_read_float(
                "STORYWEAVER_REVIEW_MINIMUM_TARGET_RATIO",
                0.5,
            ),
            review_maximum_target_ratio=_read_float(
                "STORYWEAVER_REVIEW_MAXIMUM_TARGET_RATIO",
                1.8,
            ),
            review_warning_count_threshold=_read_int(
                "STORYWEAVER_REVIEW_WARNING_COUNT_THRESHOLD",
                3,
            ),
            review_max_revision_rounds=_read_int(
                "STORYWEAVER_REVIEW_MAX_REVISION_ROUNDS",
                1,
            ),
            model_diagnostics_directory=Path(
                configured_diagnostics or DEFAULT_MODEL_DIAGNOSTICS_DIRECTORY
            ).expanduser(),
            agent_trace_directory=Path(
                configured_traces or DEFAULT_AGENT_TRACE_DIRECTORY
            ).expanduser(),
        )

class NovelService:
    """FastAPI 与 JobSupervisor 共用的小说创作应用入口。"""

    def __init__(
        self,
        *,
        store: StoryProjectRepository,
        create_pipeline: CreateNovelPipeline,
        write_pipeline: WriteNextChapterPipeline,
        execution_locks: BookExecutionLockManager | None = None,
    ) -> None:
        self.store = store
        self._create_pipeline = create_pipeline
        self._write_pipeline = write_pipeline
        self._execution_locks = execution_locks or BookExecutionLockManager()

    async def create_project(self, request: CreateNovelRequest) -> NovelProject:
        return await self._create_pipeline.run(request)

    async def write_next_chapter(
        self,
        *,
        book_id: str,
        user_instruction: str | None = None,
        batch_context: BatchPlanningContext | None = None,
    ) -> ChapterResult:
        with self._execution_locks.acquire(book_id):
            return await self._write_pipeline.run(
                book_id=book_id,
                user_instruction=user_instruction,
                batch_context=batch_context,
            )

    async def prepare_next_chapter(
        self,
        *,
        book_id: str,
        user_instruction: str | None = None,
        batch_context: BatchPlanningContext | None = None,
    ) -> ChapterPlanProposal:
        proposal = await self._write_pipeline.prepare(
            book_id=book_id,
            user_instruction=user_instruction,
            batch_context=batch_context,
        )
        self.store.save_plan_proposal(proposal)
        return proposal

    async def prepare_rewrite_chapter(
        self,
        *,
        book_id: str,
        chapter_number: int,
        user_instruction: str | None = None,
    ) -> tuple[ChapterRewriteRecord, ChapterPlanProposal]:
        """安全回退到指定章节之前，并生成新的候选章节计划。"""

        with self._execution_locks.acquire(book_id):
            record = self.store.begin_chapter_rewrite(book_id, chapter_number)
            try:
                proposal = await self._write_pipeline.prepare(
                    book_id=book_id,
                    user_instruction=user_instruction,
                )
                if proposal.chapter_number != chapter_number:
                    raise ValueError(
                        f"重写计划章节号应为 {chapter_number}，"
                        f"实际为 {proposal.chapter_number}"
                    )
                self.store.save_plan_proposal(proposal)
            except Exception:
                self.store.rollback_chapter_rewrite(record)
                raise
            self.store.finalize_chapter_rewrite(record)
            return record, proposal

    async def revise_chapter_plan(
        self,
        *,
        book_id: str,
        proposal_id: str,
        feedback: str,
    ) -> ChapterPlanProposal:
        proposal = self._load_pending_proposal(book_id, proposal_id)
        revised = await self._write_pipeline.revise(proposal, feedback=feedback)
        self.store.save_plan_proposal(revised)
        return revised

    async def confirm_chapter_plan(
        self,
        *,
        book_id: str,
        proposal_id: str,
        quality_gate: ReviewQualityGate | None = None,
    ) -> ChapterResult:
        with self._execution_locks.acquire(book_id):
            # 必须在获得作品锁后重新加载基线，防止两个确认请求同时通过预检。
            proposal = self._load_pending_proposal(book_id, proposal_id)
            result = await self._write_pipeline.execute(
                proposal,
                quality_gate=quality_gate,
            )
            if result.committed:
                self.store.save_plan_proposal(
                    replace(
                        proposal,
                        status="confirmed",
                        updated_at=datetime.now(timezone.utc).isoformat(),
                    )
                )
            return result

    def cancel_chapter_plan(
        self,
        *,
        book_id: str,
        proposal_id: str,
    ) -> ChapterPlanProposal:
        proposal = self._load_pending_proposal(book_id, proposal_id)
        cancelled = replace(
            proposal,
            status="cancelled",
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self.store.save_plan_proposal(cancelled)
        return cancelled

    def load_chapter_plan_proposal(
        self,
        *,
        book_id: str,
        proposal_id: str,
    ) -> ChapterPlanProposal:
        return self.store.load_plan_proposal(book_id, proposal_id)

    def _load_pending_proposal(
        self,
        book_id: str,
        proposal_id: str,
    ) -> ChapterPlanProposal:
        proposal = self.store.load_plan_proposal(book_id, proposal_id)
        if proposal.status != "pending":
            raise ValueError(f"候选计划状态为 {proposal.status}，不能继续操作")
        state = self.store.load_state(book_id)
        if state.last_committed_chapter != proposal.base_chapter_number:
            expired = replace(
                proposal,
                status="expired",
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            self.store.save_plan_proposal(expired)
            raise ValueError("作品状态已变化，候选计划已过期；请重新规划下一章")
        return proposal

    async def write_chapters(
        self,
        *,
        book_id: str,
        count: int,
        user_instruction: str | None = None,
        on_chapter_committed: Callable[[ChapterResult], None] | None = None,
    ) -> tuple[ChapterResult, ...]:
        if count <= 0:
            raise ValueError("连续写章数量必须大于 0")
        project = self.store.load_project(book_id)
        remaining = (
            project.metadata.target_chapters
            - project.state.last_committed_chapter
        )
        if count > remaining:
            raise ValueError(
                f"项目只剩 {remaining} 章可写，不能继续生成 {count} 章"
            )

        results: list[ChapterResult] = []
        for _ in range(count):
            result = await self.write_next_chapter(
                book_id=book_id,
                user_instruction=user_instruction,
            )
            results.append(result)
            if result.committed and on_chapter_committed is not None:
                # 批次内部逐章提交后立即通知宿主，供一次性控制面等边界状态消费。
                on_chapter_committed(result)
            if not result.committed:
                break
        return tuple(results)

    def list_projects(self) -> tuple[BookMetadata, ...]:
        return self.store.list_projects()


def build_novel_service(
    settings: NovelApplicationSettings,
    *,
    observer: NovelRunObserver | None = None,
    creative_control_provider: object | None = None,
    store: StoryProjectRepository,
    context_snapshot_sink: object | None = None,
) -> NovelService:
    """使用一个共享 Runtime 组装真实模型小说创作服务。"""

    configure_local_sdk_tracing(settings.agent_trace_directory)
    diagnostic_writer = ModelFailureDiagnosticWriter(settings.model_diagnostics_directory)
    hooks = (observer,) if observer is not None else ()
    # 生产小说 Worker 全部直接使用 SDK，并共用同一 Provider 配置。
    sdk_model = None
    sdk_provider = OpenAICompatibleProviderSettings(
        base_url=settings.base_url,
        model_name=settings.model,
        api_key=settings.api_key,
    ).create_provider()
    sdk_model = sdk_provider.get_model(settings.model)

    def sdk_worker_settings(
        *,
        worker_id: str,
        name: str,
        instructions: str,
        temperature: float,
        timeout_seconds: float | None = None,
    ) -> WorkerSettings | None:
        extra_body: dict[str, object] = {}
        if settings.thinking is not None:
            extra_body["thinking"] = {"type": settings.thinking}
        if settings.reasoning_effort is not None:
            extra_body["reasoning_effort"] = settings.reasoning_effort
        return WorkerSettings(
            worker_id=worker_id,
            name=name,
            instructions=instructions,
            model=sdk_model,
            model_settings=ModelSettings(
                temperature=temperature,
                timeout=timeout_seconds or settings.timeout_seconds,
                extra_body=extra_body or None,
            ),
            timeout_seconds=timeout_seconds or settings.timeout_seconds,
            diagnostic_writer=diagnostic_writer,
        )
    architect = ArchitectAgent(
        sdk_settings=sdk_worker_settings(
            worker_id="novel-architect",
            name="小说架构师",
            instructions=ARCHITECT_SYSTEM_PROMPT,
            temperature=settings.architect_temperature,
        ),
        event_sinks=hooks,
    )
    planner = PlannerAgent(
        store=store,
        context_snapshot_sink=context_snapshot_sink,
        sdk_settings=sdk_worker_settings(
            worker_id="novel-planner", name="章节规划师",
            instructions=PLANNER_SYSTEM_PROMPT, temperature=settings.planner_temperature,
        ),
        event_sinks=hooks,
    )
    writer = WriterAgent(
        sdk_settings=sdk_worker_settings(
            worker_id="novel-writer",
            name="小说正文作者",
            instructions=WRITER_SYSTEM_PROMPT,
            temperature=settings.writer_temperature,
        ),
        event_sinks=hooks,
    )
    reviewer = ReviewerAgent(
        store=store,
        context_snapshot_sink=context_snapshot_sink,
        sdk_settings=sdk_worker_settings(
            worker_id="novel-reviewer", name="章节审查员",
            instructions=REVIEWER_SYSTEM_PROMPT, temperature=settings.reviewer_temperature,
            timeout_seconds=settings.reviewer_turn_timeout_seconds,
        ),
        event_sinks=hooks,
    )
    reviser = ReviserAgent(
        sdk_settings=sdk_worker_settings(
            worker_id="novel-reviser",
            name="章节修订者",
            instructions=REVISER_SYSTEM_PROMPT,
            temperature=settings.reviser_temperature,
        ),
        event_sinks=hooks,
    )
    analyzer = ChapterAnalyzerAgent(
        sdk_settings=sdk_worker_settings(
            worker_id="chapter-analyzer",
            name="章节状态分析器",
            instructions=CHAPTER_ANALYZER_SYSTEM_PROMPT,
            temperature=settings.analyzer_temperature,
        ),
        event_sinks=hooks,
    )

    return NovelService(
        store=store,
        create_pipeline=CreateNovelPipeline(
            architect=architect,
            store=store,
        ),
        write_pipeline=WriteNextChapterPipeline(
            store=store,
            planner=planner,
            context_builder=ChapterContextBuilder(
                token_budget=settings.context_token_budget,
                creative_control_provider=creative_control_provider if callable(creative_control_provider) else None,
            ),
            writer=writer,
            reviewer=reviewer,
            reviser=reviser,
            analyzer=analyzer,
            creative_control_provider=creative_control_provider if callable(creative_control_provider) else None,
            context_snapshot_sink=context_snapshot_sink,
            review_policy=settings.review_policy,
            quality_gate=ReviewQualityGate(
                ReviewQualityGatePolicy(
                    minimum_score=settings.review_minimum_score,
                    minimum_target_ratio=settings.review_minimum_target_ratio,
                    maximum_target_ratio=settings.review_maximum_target_ratio,
                    warning_count_threshold=(
                        settings.review_warning_count_threshold
                    ),
                    max_revision_rounds=settings.review_max_revision_rounds,
                )
            ),
        ),
    )
