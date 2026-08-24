"""裸模型与 StoryWeaver Pipeline 的可重复 A/B 运行器。"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from ..llm import LlmUsage, WorkerRetryPolicy, WorkerSettings, run_text_worker, run_with_retry
from ..novel_creation.application import NovelService
from ..novel_creation.models import ChapterResult, CreateNovelRequest
from ..novel_creation.observability import NovelRunObserver, WorkerRunMetric
from .models import (
    BareChapterOutput,
    EvaluationCase,
    EvaluationRunResult,
    GroupRunResult,
    QualityChapterMetrics,
)
from .storage import EvaluationStore


class NovelServiceFactory(Protocol):
    """为每个案例创建隔离小说项目的工厂。"""

    def __call__(
        self,
        case_directory: Path,
        observer: NovelRunObserver,
    ) -> NovelService:
        ...


class BareChapterGenerationError(RuntimeError):
    """裸模型在有限重试后仍无法生成正文。"""

    def __init__(
        self,
        message: str,
        *,
        model_calls: int,
        failed_model_calls: int,
    ) -> None:
        super().__init__(message)
        self.model_calls = model_calls
        self.failed_model_calls = failed_model_calls


class BareNovelWriter:
    """不使用 Planner、Memory、Reviewer 或状态管理的单调用基线。"""

    SYSTEM_PROMPT = (
        "你是一名中文小说作者。根据用户给出的原始创作简报和已有正文继续写作。"
        "只输出本章正文，不输出标题、提纲、解释、JSON 或 Markdown 标记。"
        "不要总结前文，不要讨论创作过程。"
    )

    def __init__(
        self,
        settings: WorkerSettings,
        *,
        clock: Callable[[], float] = time.perf_counter,
        retry_policy: WorkerRetryPolicy | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._retry_policy = retry_policy or WorkerRetryPolicy()

    async def write_chapter(
        self,
        *,
        case: EvaluationCase,
        chapter_number: int,
        previous_chapters: Sequence[str],
    ) -> BareChapterOutput:
        prompt = self.build_prompt(
            request=case.request,
            chapter_number=chapter_number,
            previous_chapters=previous_chapters,
        )
        started = self._clock()
        attempt_count = 0
        failed_model_calls = 0
        total_usage = LlmUsage()

        async def generate(_context):
            nonlocal attempt_count, failed_model_calls, total_usage
            attempt_count += 1
            try:
                return await run_text_worker(
                    settings=self._settings,
                    prompt=self.SYSTEM_PROMPT + "\n\n" + prompt,
                )
            except Exception:
                failed_model_calls += 1
                raise

        try:
            output = await run_with_retry(
                worker_name="bare-writer",
                operation=generate,
                policy=self._retry_policy,
            )
        except Exception as exc:
            raise BareChapterGenerationError(
                str(exc),
                model_calls=attempt_count,
                failed_model_calls=failed_model_calls,
            ) from exc
        elapsed = max(0.0, self._clock() - started)
        return BareChapterOutput(
            chapter_number=chapter_number,
            content=output,
            prompt=prompt,
            usage=total_usage,
            elapsed_seconds=elapsed,
            model_calls=attempt_count,
            failed_model_calls=failed_model_calls,
            retry_count=max(0, attempt_count - 1),
        )

    @staticmethod
    def build_prompt(
        *,
        request: CreateNovelRequest,
        chapter_number: int,
        previous_chapters: Sequence[str],
    ) -> str:
        if chapter_number <= 0:
            raise ValueError("chapter_number 必须大于 0")
        expected_previous = chapter_number - 1
        if len(previous_chapters) != expected_previous:
            raise ValueError(
                f"第 {chapter_number} 章应提供 {expected_previous} 章前文"
            )
        brief = (
            f"书名：{request.title}\n"
            f"题材：{request.genre}\n"
            f"故事前提：{request.premise}\n"
            f"主角：{request.protagonist}\n"
            f"核心冲突：{request.central_conflict}\n"
            f"叙事基调：{request.tone}\n"
            f"目标总章数：{request.target_chapters}\n"
            f"本章目标字数：约 {request.chapter_target_words} 字\n"
            f"当前任务：创作第 {chapter_number} 章。"
        )
        if not previous_chapters:
            history = "尚无前文。请自然建立人物、冲突和第一个推进事件。"
        else:
            history = "\n\n".join(
                f"【第 {index} 章】\n{content.rstrip()}"
                for index, content in enumerate(previous_chapters, start=1)
            )
        return f"{brief}\n\n已有正文：\n{history}\n\n请直接输出第 {chapter_number} 章正文。"


class EvaluationRunner:
    """顺序运行两个实验组并导出可盲评产物。"""

    def __init__(
        self,
        *,
        bare_writer: BareNovelWriter,
        service_factory: NovelServiceFactory,
        store: EvaluationStore,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._bare_writer = bare_writer
        self._service_factory = service_factory
        self._store = store
        self._clock = clock

    async def run_case(
        self,
        case: EvaluationCase,
        *,
        chapters: int = 3,
        run_id: str | None = None,
    ) -> EvaluationRunResult:
        if chapters <= 0:
            raise ValueError("chapters 必须大于 0")
        if chapters > case.request.target_chapters:
            raise ValueError("评测章节数不能超过案例目标章节数")
        resolved_run_id = run_id or self._new_run_id(case.case_id)
        run_directory = self._store.create_run_directory(resolved_run_id)
        case_directory = run_directory / "case"
        case_directory.mkdir()
        self._store.save_case(case_directory, case)
        self._store.write_manifest(
            run_directory,
            self._manifest(
                run_id=resolved_run_id,
                case=case,
                chapters=chapters,
                status="running",
            ),
        )

        try:
            bare = await self._run_bare(case, case_directory, chapters)
            storyweaver = await self._run_storyweaver(
                case,
                case_directory,
                chapters,
            )
            self._store.export_blind_samples(
                case_directory,
                case_id=case.case_id,
                run_id=resolved_run_id,
                bare=bare,
                storyweaver=storyweaver,
            )
            result = EvaluationRunResult(
                run_id=resolved_run_id,
                case_id=case.case_id,
                requested_chapters=chapters,
                bare=bare,
                storyweaver=storyweaver,
                output_directory=str(run_directory.resolve()),
            )
            self._store.write_manifest(
                run_directory,
                {
                    **self._manifest(
                        run_id=resolved_run_id,
                        case=case,
                        chapters=chapters,
                        status=result.status,
                    ),
                    "result": result,
                },
            )
            return result
        except Exception as exc:
            self._store.write_manifest(
                run_directory,
                {
                    **self._manifest(
                        run_id=resolved_run_id,
                        case=case,
                        chapters=chapters,
                        status="failed",
                    ),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            raise

    async def _run_bare(
        self,
        case: EvaluationCase,
        case_directory: Path,
        chapters: int,
    ) -> GroupRunResult:
        contents: list[str] = []
        metrics: list[QualityChapterMetrics] = []
        for chapter_number in range(1, chapters + 1):
            fallback_prompt = self._bare_writer.build_prompt(
                request=case.request,
                chapter_number=chapter_number,
                previous_chapters=contents,
            )
            self._store.save_prompt(
                case_directory,
                group="bare",
                chapter_number=chapter_number,
                prompt=fallback_prompt,
            )
            started = self._clock()
            try:
                output = await self._bare_writer.write_chapter(
                    case=case,
                    chapter_number=chapter_number,
                    previous_chapters=contents,
                )
            except Exception as exc:
                model_calls = self._positive_integer_attribute(
                    exc,
                    "model_calls",
                    default=1,
                )
                failed_model_calls = self._positive_integer_attribute(
                    exc,
                    "failed_model_calls",
                    default=model_calls,
                )
                metrics.append(
                    QualityChapterMetrics(
                        group="bare",
                        chapter_number=chapter_number,
                        succeeded=False,
                        committed=None,
                        status="failed",
                        model_calls=model_calls,
                        failed_model_calls=failed_model_calls,
                        retry_count=max(0, model_calls - 1),
                        input_tokens=0,
                        output_tokens=0,
                        elapsed_seconds=max(0.0, self._clock() - started),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                break
            contents.append(output.content)
            self._store.save_chapter(
                case_directory,
                group="bare",
                chapter_number=chapter_number,
                title=f"第 {chapter_number} 章",
                content=output.content,
            )
            metrics.append(
                QualityChapterMetrics(
                    group="bare",
                    chapter_number=chapter_number,
                    succeeded=True,
                    committed=None,
                    status="generated",
                    model_calls=output.model_calls,
                    failed_model_calls=output.failed_model_calls,
                    retry_count=output.retry_count,
                    input_tokens=output.usage.input_tokens,
                    output_tokens=output.usage.output_tokens,
                    elapsed_seconds=output.elapsed_seconds,
                )
            )
        result = GroupRunResult(
            group="bare",
            chapters=tuple(contents),
            metrics=tuple(metrics),
        )
        self._store.save_metrics(case_directory, result)
        return result

    async def _run_storyweaver(
        self,
        case: EvaluationCase,
        case_directory: Path,
        chapters: int,
    ) -> GroupRunResult:
        observer = NovelRunObserver(output=None)
        service = self._service_factory(case_directory, observer)
        setup_mark = observer.mark()
        setup_started = self._clock()
        project = await service.create_project(case.request)
        setup_summary = observer.summarize(since=setup_mark)
        self._store.write_json(
            case_directory / "storyweaver" / "setup-metrics.json",
            {
                "book_id": project.metadata.book_id,
                "model_calls": setup_summary.run_count,
                "input_tokens": setup_summary.input_tokens,
                "output_tokens": setup_summary.output_tokens,
                "elapsed_seconds": max(0.0, self._clock() - setup_started),
            },
        )

        contents: list[str] = []
        metrics: list[QualityChapterMetrics] = []
        for chapter_number in range(1, chapters + 1):
            mark = observer.mark()
            started = self._clock()
            try:
                result = await service.write_next_chapter(
                    book_id=project.metadata.book_id,
                )
            except Exception as exc:
                records = observer.records[mark:]
                metrics.append(
                    self._failed_pipeline_metrics(
                        chapter_number=chapter_number,
                        records=records,
                        elapsed_seconds=max(0.0, self._clock() - started),
                        error=exc,
                    )
                )
                break
            records = observer.records[mark:]
            contents.append(result.final_draft.content)
            self._store.save_chapter(
                case_directory,
                group="storyweaver",
                chapter_number=chapter_number,
                title=result.final_draft.title,
                content=result.final_draft.content,
            )
            metrics.append(
                self._pipeline_metrics(
                    result=result,
                    records=records,
                    elapsed_seconds=max(0.0, self._clock() - started),
                )
            )
            if not result.committed:
                break
        group = GroupRunResult(
            group="storyweaver",
            chapters=tuple(contents),
            metrics=tuple(metrics),
        )
        self._store.save_metrics(case_directory, group)
        return group

    @staticmethod
    def _pipeline_metrics(
        *,
        result: ChapterResult,
        records: Sequence[WorkerRunMetric],
        elapsed_seconds: float,
    ) -> QualityChapterMetrics:
        return QualityChapterMetrics(
            group="storyweaver",
            chapter_number=result.chapter_number,
            succeeded=True,
            committed=result.committed,
            status=result.status,
            model_calls=sum(item.model_calls for item in records),
            failed_model_calls=sum(not item.succeeded for item in records),
            retry_count=EvaluationRunner._retry_count(records),
            input_tokens=sum(item.input_tokens for item in records),
            output_tokens=sum(item.output_tokens for item in records),
            elapsed_seconds=elapsed_seconds,
            revised=result.revised,
            initial_issue_count=len(result.initial_review.issues),
            final_issue_count=len(result.final_review.issues),
            initial_critical_count=sum(
                item.severity == "critical" for item in result.initial_review.issues
            ),
            final_critical_count=sum(
                item.severity == "critical" for item in result.final_review.issues
            ),
            candidate_id=result.candidate_id,
        )

    @staticmethod
    def _failed_pipeline_metrics(
        *,
        chapter_number: int,
        records: Sequence[WorkerRunMetric],
        elapsed_seconds: float,
        error: Exception,
    ) -> QualityChapterMetrics:
        return QualityChapterMetrics(
            group="storyweaver",
            chapter_number=chapter_number,
            succeeded=False,
            committed=False,
            status="failed",
            model_calls=len(records),
            failed_model_calls=sum(not item.succeeded for item in records),
            retry_count=EvaluationRunner._retry_count(records),
            input_tokens=sum(item.input_tokens for item in records),
            output_tokens=sum(item.output_tokens for item in records),
            elapsed_seconds=elapsed_seconds,
            error=f"{type(error).__name__}: {error}",
        )

    @staticmethod
    def _retry_count(records: Sequence[WorkerRunMetric]) -> int:
        """排除正常复审后，统计同一 Worker 的额外运行次数。"""

        normal_limits = {
            "novel-planner": 1,
            "novel-writer": 1,
            "novel-reviewer": 2,
            "novel-reviser": 1,
            "chapter-analyzer": 1,
        }
        counts: dict[str, int] = {}
        for item in records:
            counts[item.agent_id] = counts.get(item.agent_id, 0) + 1
        repeated_runs = sum(
            max(0, count - normal_limits.get(agent_id, 1))
            for agent_id, count in counts.items()
        )
        return repeated_runs + sum(item.retry_count for item in records)

    @staticmethod
    def _positive_integer_attribute(
        value: object,
        name: str,
        *,
        default: int,
    ) -> int:
        candidate = getattr(value, name, default)
        if (
            isinstance(candidate, int)
            and not isinstance(candidate, bool)
            and candidate > 0
        ):
            return candidate
        return default

    @staticmethod
    def _manifest(
        *,
        run_id: str,
        case: EvaluationCase,
        chapters: int,
        status: str,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "run_id": run_id,
            "case_id": case.case_id,
            "requested_chapters": chapters,
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _new_run_id(case_id: str) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{timestamp}-{case_id}-{uuid4().hex[:8]}"
