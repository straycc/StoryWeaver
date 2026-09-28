"""裸模型与 StoryWeaver Pipeline 的可重复 A/B 运行器。"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from ..llm import (
    LlmEvent,
    LlmEventType,
    LlmUsage,
    WorkerRetryPolicy,
    WorkerSettings,
    run_text_worker,
    run_with_retry,
)
from ..novel_creation.application import NovelService
from ..novel_creation.models import ChapterResult
from ..novel_creation.observability import NovelRunObserver, WorkerRunMetric
from .models import (
    BareChapterOutput,
    EvaluationCase,
    EvaluationRunResult,
    GroupRunResult,
    QualityChapterMetrics,
)
from .report import build_operational_summary
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
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._retry_policy = retry_policy or WorkerRetryPolicy()
        self._progress = progress

    async def write_chapter(
        self,
        *,
        case: EvaluationCase,
        chapter_number: int,
        previous_chapters: Sequence[str],
    ) -> BareChapterOutput:
        prompt = self.build_prompt(
            case=case,
            chapter_number=chapter_number,
            previous_chapters=previous_chapters,
        )
        started = self._clock()
        attempt_count = 0
        failed_model_calls = 0
        usage_sink = _UsageSink()

        async def generate(retry_context):
            nonlocal attempt_count, failed_model_calls
            attempt_count += 1
            try:
                return await run_text_worker(
                    settings=replace(self._settings, instructions=self.SYSTEM_PROMPT),
                    prompt=prompt,
                    event_sinks=(usage_sink,),
                )
            except Exception as exc:
                failed_model_calls += 1
                self._emit(
                    "[bare] 模型调用失败 · "
                    f"attempt={retry_context.attempt}/{retry_context.max_attempts} · "
                    f"{type(exc).__name__}: {exc}"
                )
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
            usage=usage_sink.usage,
            elapsed_seconds=elapsed,
            model_calls=attempt_count,
            failed_model_calls=failed_model_calls,
            retry_count=max(0, attempt_count - 1),
        )

    def _emit(self, message: str) -> None:
        if self._progress is not None:
            self._progress(message)

    @staticmethod
    def build_prompt(
        *,
        case: EvaluationCase,
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
        brief = case.render_shared_brief()
        request = case.request
        chapter_spec = case.chapter(chapter_number)
        chapter_task = (
            chapter_spec.input.render()
            if chapter_spec is not None
            else "请依据全书设定自然推进本章。"
        )
        if not previous_chapters:
            history = "尚无前文。请自然建立人物、冲突和第一个推进事件。"
        else:
            history = "\n\n".join(
                f"【第 {index} 章】\n{content.rstrip()}"
                for index, content in enumerate(previous_chapters, start=1)
            )
        return (
            f"{brief}\n\n本章目标字数：约 {request.chapter_target_words} 字\n"
            f"当前任务：创作第 {chapter_number} 章。\n\n"
            f"本章任务：\n{chapter_task}\n\n"
            f"已有正文：\n{history}\n\n"
            f"请直接输出第 {chapter_number} 章正文。"
        )


class _UsageSink:
    """仅累计一次裸模型章节调用产生的 SDK Token 事件。"""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0

    @property
    def usage(self) -> LlmUsage:
        return LlmUsage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
        )

    async def on_event(self, event: LlmEvent) -> None:
        if event.type != LlmEventType.MODEL_COMPLETED:
            return
        self.input_tokens += self._integer(event.data.get("input_tokens"))
        self.output_tokens += self._integer(event.data.get("output_tokens"))

    @staticmethod
    def _integer(value: object) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) else 0


class EvaluationRunner:
    """顺序运行两个实验组并导出可盲评产物。"""

    def __init__(
        self,
        *,
        bare_writer: BareNovelWriter,
        service_factory: NovelServiceFactory,
        store: EvaluationStore,
        clock: Callable[[], float] = time.perf_counter,
        runtime_metadata: dict[str, object] | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self._bare_writer = bare_writer
        self._service_factory = service_factory
        self._store = store
        self._clock = clock
        self._runtime_metadata = dict(runtime_metadata or {})
        self._progress = progress

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
        self._emit(
            f"[evaluation] 开始运行 {case.case_id} · {chapters} 章 · "
            f"run_id={resolved_run_id}"
        )
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
            self._emit(
                f"[bare] 阶段结束 · 已生成 {len(bare.chapters)}/{chapters} 章"
            )
            storyweaver = await self._run_storyweaver(
                self._execution_case(case, resolved_run_id),
                case_directory,
                chapters,
            )
            self._emit(
                "[storyweaver] 阶段结束 · "
                f"已生成 {len(storyweaver.chapters)}/{chapters} 章"
            )
            self._store.export_blind_samples(
                case_directory,
                case_id=case.case_id,
                run_id=resolved_run_id,
                bare=bare,
                storyweaver=storyweaver,
                chapter_specs=case.chapters[:chapters],
            )
            result = EvaluationRunResult(
                run_id=resolved_run_id,
                case_id=case.case_id,
                requested_chapters=chapters,
                bare=bare,
                storyweaver=storyweaver,
                output_directory=str(run_directory.resolve()),
            )
            self._store.write_json(
                run_directory / "summary.json",
                build_operational_summary(result),
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
            self._emit(f"[evaluation] 生成阶段完成 · status={result.status}")
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
            self._emit(f"[evaluation] 运行失败 · {type(exc).__name__}: {exc}")
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
            self._emit(f"[bare] 开始生成第 {chapter_number}/{chapters} 章")
            fallback_prompt = self._bare_writer.build_prompt(
                case=case,
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
                self._store.save_metrics(
                    case_directory,
                    GroupRunResult(
                        group="bare",
                        chapters=tuple(contents),
                        metrics=tuple(metrics),
                    ),
                )
                self._emit(
                    f"[bare] 第 {chapter_number}/{chapters} 章失败 · "
                    f"{type(exc).__name__}: {exc}"
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
            self._store.save_metrics(
                case_directory,
                GroupRunResult(
                    group="bare",
                    chapters=tuple(contents),
                    metrics=tuple(metrics),
                ),
            )
            self._emit(
                f"[bare] 完成第 {chapter_number}/{chapters} 章 · "
                f"{output.elapsed_seconds:.2f}s · "
                f"Token {output.usage.total_tokens:,} · "
                f"调用 {output.model_calls} · 重试 {output.retry_count}"
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
        observer = NovelRunObserver(
            output=lambda message: self._emit(f"[storyweaver] {message}")
        )
        service = self._service_factory(case_directory, observer)
        setup_mark = observer.mark()
        setup_started = self._clock()
        self._emit("[storyweaver] 开始创建隔离评测作品")
        try:
            project = await service.create_project(case.request)
        except Exception as exc:
            setup_summary = observer.summarize(since=setup_mark)
            setup_records = observer.records[setup_mark:]
            setup_elapsed = max(0.0, self._clock() - setup_started)
            setup_error = f"{type(exc).__name__}: {exc}"
            self._store.write_json(
                case_directory / "storyweaver" / "setup-metrics.json",
                {
                    "model_calls": sum(item.model_calls for item in setup_records),
                    "failed_model_calls": sum(
                        item.model_calls for item in setup_records if not item.succeeded
                    ),
                    "retry_count": self._retry_count(setup_records),
                    "input_tokens": setup_summary.input_tokens,
                    "output_tokens": setup_summary.output_tokens,
                    "elapsed_seconds": setup_elapsed,
                    "error": setup_error,
                },
            )
            failed = GroupRunResult(
                group="storyweaver",
                chapters=(),
                metrics=(
                    QualityChapterMetrics(
                        group="storyweaver",
                        chapter_number=1,
                        succeeded=False,
                        committed=False,
                        status="setup_failed",
                        model_calls=0,
                        failed_model_calls=0,
                        retry_count=0,
                        input_tokens=0,
                        output_tokens=0,
                        elapsed_seconds=0.0,
                        error=setup_error,
                    ),
                ),
                setup_model_calls=sum(item.model_calls for item in setup_records),
                setup_failed_model_calls=sum(
                    item.model_calls for item in setup_records if not item.succeeded
                ),
                setup_retry_count=self._retry_count(setup_records),
                setup_input_tokens=setup_summary.input_tokens,
                setup_output_tokens=setup_summary.output_tokens,
                setup_elapsed_seconds=setup_elapsed,
                setup_error=setup_error,
            )
            self._store.save_metrics(case_directory, failed)
            self._emit(f"[storyweaver] 创建作品失败 · {setup_error}")
            return failed
        setup_summary = observer.summarize(since=setup_mark)
        setup_records = observer.records[setup_mark:]
        setup_elapsed = max(0.0, self._clock() - setup_started)
        self._store.write_json(
            case_directory / "storyweaver" / "setup-metrics.json",
            {
                "book_id": project.metadata.book_id,
                "model_calls": sum(item.model_calls for item in setup_records),
                "failed_model_calls": sum(
                    item.model_calls for item in setup_records if not item.succeeded
                ),
                "retry_count": self._retry_count(setup_records),
                "input_tokens": setup_summary.input_tokens,
                "output_tokens": setup_summary.output_tokens,
                "elapsed_seconds": setup_elapsed,
            },
        )
        self._emit(
            "[storyweaver] 完成作品创建 · "
            f"{setup_elapsed:.2f}s · Token {setup_summary.total_tokens:,}"
        )

        contents: list[str] = []
        metrics: list[QualityChapterMetrics] = []
        for chapter_number in range(1, chapters + 1):
            self._emit(
                f"[storyweaver] 开始生成第 {chapter_number}/{chapters} 章"
            )
            mark = observer.mark()
            started = self._clock()
            try:
                result = await self._write_approved_storyweaver_chapter(
                    service=service,
                    book_id=project.metadata.book_id,
                    user_instruction=self._chapter_instruction(case, chapter_number),
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
                self._store.save_metrics(
                    case_directory,
                    GroupRunResult(
                        group="storyweaver",
                        chapters=tuple(contents),
                        metrics=tuple(metrics),
                    ),
                )
                self._emit(
                    f"[storyweaver] 第 {chapter_number}/{chapters} 章失败 · "
                    f"{type(exc).__name__}: {exc}"
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
            self._store.save_metrics(
                case_directory,
                GroupRunResult(
                    group="storyweaver",
                    chapters=tuple(contents),
                    metrics=tuple(metrics),
                ),
            )
            chapter_metric = metrics[-1]
            self._emit(
                f"[storyweaver] 完成第 {chapter_number}/{chapters} 章 · "
                f"{chapter_metric.elapsed_seconds:.2f}s · "
                f"Token {chapter_metric.total_tokens:,} · "
                f"调用 {chapter_metric.model_calls} · 重试 {chapter_metric.retry_count} · "
                f"status={chapter_metric.status}"
            )
            if not result.committed:
                break
        group = GroupRunResult(
            group="storyweaver",
            chapters=tuple(contents),
            metrics=tuple(metrics),
            setup_model_calls=sum(item.model_calls for item in setup_records),
            setup_failed_model_calls=sum(
                item.model_calls for item in setup_records if not item.succeeded
            ),
            setup_retry_count=self._retry_count(setup_records),
            setup_input_tokens=setup_summary.input_tokens,
            setup_output_tokens=setup_summary.output_tokens,
            setup_elapsed_seconds=setup_elapsed,
        )
        self._store.save_metrics(case_directory, group)
        return group

    @staticmethod
    async def _write_approved_storyweaver_chapter(
        *,
        service: NovelService,
        book_id: str,
        user_instruction: str | None,
    ) -> ChapterResult:
        """在无人值守评测中模拟用户批准，再执行正式章节流水线。"""

        proposal = await service.prepare_next_chapter(
            book_id=book_id,
            user_instruction=user_instruction,
        )
        approved = service.approve_chapter_plan(
            book_id=book_id,
            proposal_id=proposal.proposal_id,
        )
        return await service.write_from_plan(
            book_id=book_id,
            proposal_id=approved.proposal_id,
        )

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

    def _manifest(
        self,
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
            "runtime": dict(self._runtime_metadata),
        }

    @staticmethod
    def _new_run_id(case_id: str) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{timestamp}-{case_id}-{uuid4().hex[:8]}"

    @staticmethod
    def _execution_case(case: EvaluationCase, run_id: str) -> EvaluationCase:
        """为 SQLite 中的评测作品生成独立 ID，并给 Architect 同源资料。"""

        suffix = run_id[-8:]
        supplemental = case.render_supplemental_brief()
        request = replace(
            case.request,
            premise=(
                f"{case.request.premise}\n\n"
                "以下固定评测资料与章节框架必须遵守：\n"
                f"{supplemental}\n\n"
                f"评测隔离标识：{suffix}（仅用于数据隔离，不属于故事内容，"
                "不得写入设定或正文）。"
            )
            if supplemental
            else (
                f"{case.request.premise}\n\n评测隔离标识：{suffix}"
                "（仅用于数据隔离，不属于故事内容，不得写入设定或正文）。"
            ),
        )
        return replace(case, request=request)

    @staticmethod
    def _chapter_instruction(case: EvaluationCase, chapter_number: int) -> str | None:
        chapter = case.chapter(chapter_number)
        if chapter is None:
            return None
        return (
            f"本章标题建议：《{chapter.title}》\n\n"
            f"{chapter.input.render()}\n\n"
            "这些是本章明确要求；请在不违反既有正史的前提下完成。"
        )

    def _emit(self, message: str) -> None:
        if self._progress is not None:
            self._progress(message)
