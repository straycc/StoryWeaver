"""小说 A/B 评测命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from agents import ModelSettings
from sqlalchemy import inspect, text

from ..llm import OpenAICompatibleProviderSettings, WorkerSettings
from ..observability import ModelFailureDiagnosticWriter
from ..novel_creation.application import (
    PROJECT_ROOT,
    NovelApplicationSettings,
    build_novel_service,
)
from ..model_config import ModelCatalog
from ..persistence.database import database_url_from_env
from ..persistence import Database, DatabaseSettings, SQLAlchemyStoryProjectRepository
from .case_loader import load_evaluation_case
from .graders import PairwiseModelGrader, build_quality_summary
from .models import EvaluationCase, EvaluationRunResult
from .report import build_operational_summary
from .runner import BareNovelWriter, EvaluationRunner
from .storage import EvaluationStore


DEFAULT_CASE = PROJECT_ROOT / "backend" / "evals" / "cases" / "novel_ab_v1.yaml"
DEFAULT_OUTPUT = PROJECT_ROOT / "backend" / "evals" / "results"
JUDGE_OUTPUT_TOKEN_LIMIT = 16_000


def _progress(message: str) -> None:
    timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
    print(f"{timestamp} {message}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用同一模型运行裸模型与 StoryWeaver 的连续小说 A/B 评测"
    )
    parser.add_argument("--case", type=Path, default=DEFAULT_CASE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--chapters",
        type=int,
        default=3,
        help="本次顺序生成的章节数；先用 3 章 Pilot，正式运行传 12",
    )
    parser.add_argument(
        "--judge-passes",
        type=int,
        choices=(0, 1, 2),
        default=1,
        help="0=只导出人工盲评材料，1=单次模型盲评，2=交换位置复评",
    )
    parser.add_argument(
        "--judge-model",
        default=None,
        help="可选的独立 Judge 模型；默认复用生成模型，仅建议用于 Pilot",
    )
    parser.add_argument(
        "--judge-only",
        type=Path,
        default=None,
        metavar="RUN_DIRECTORY",
        help="只评审一个已有运行目录，不重新生成章节，也不需要连接数据库",
    )
    parser.add_argument("--run-id", default=None)
    return parser


def _build_judge(
    *,
    settings: NovelApplicationSettings,
    model: object,
    extra_body: dict[str, object],
) -> PairwiseModelGrader:
    """构造与生成 Worker 隔离的低思考 Judge。"""

    return PairwiseModelGrader(
        WorkerSettings(
            worker_id="evaluation-pairwise-judge",
            name="小说 A/B 盲评员",
            instructions=PairwiseModelGrader.SYSTEM_PROMPT,
            model=model,
            model_settings=ModelSettings(
                temperature=0.1,
                max_tokens=JUDGE_OUTPUT_TOKEN_LIMIT,
                timeout=settings.timeout_seconds,
                extra_body=extra_body or None,
            ),
            timeout_seconds=settings.timeout_seconds,
            diagnostic_writer=ModelFailureDiagnosticWriter(
                settings.model_diagnostics_directory
            ),
        ),
        progress=_progress,
    )


async def _grade_result(
    *,
    judge: PairwiseModelGrader,
    case: EvaluationCase,
    result: EvaluationRunResult,
    passes: int,
    store: EvaluationStore,
) -> bool:
    """评分失败时保存独立终态，不破坏已经完成的生成结果。"""

    run_directory = Path(result.output_directory)
    try:
        records = await judge.grade(
            case=case,
            bare=result.bare,
            storyweaver=result.storyweaver,
            passes=passes,
        )
        quality = build_quality_summary(
            case=case,
            records=records,
            bare=result.bare,
            storyweaver=result.storyweaver,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        quality = {
            "schema_version": 1,
            "case_id": case.case_id,
            "status": "model_grading_failed",
            "error": error,
            "note": "生成结果仍然有效；可使用 --judge-only 重新评分。",
        }
        store.write_json(
            run_directory / "case" / "grading" / "quality-summary.json",
            quality,
        )
        summary = build_operational_summary(result)
        summary["quality"] = quality
        store.write_json(run_directory / "summary.json", summary)
        store.update_grading_status(
            run_directory,
            {"status": "failed", "passes": passes, "error": error},
        )
        _progress(f"[judge] 评分失败，已保留生成结果 · {error}")
        _progress(
            "[judge] 可单独重试：python -m storyweaver.evaluation "
            f"--judge-only {run_directory} --judge-passes {passes}"
        )
        return False

    store.write_json(
        run_directory / "case" / "grading" / "model-grades.json",
        [item.to_data() for item in records],
    )
    store.write_json(
        run_directory / "case" / "grading" / "quality-summary.json",
        quality,
    )
    summary = build_operational_summary(result)
    summary["quality"] = quality
    store.write_json(run_directory / "summary.json", summary)
    store.update_grading_status(
        run_directory,
        {"status": "completed", "passes": passes},
    )
    return True


async def run(args: argparse.Namespace) -> int:
    case = load_evaluation_case(args.case)
    if case.experiment.skill_ids:
        raise SystemExit(
            "A/B v1 要求 skill_ids 为空；否则裸模型未获得同一 Skill 正文，比较不公平"
        )
    if (
        args.judge_only is None
        and (args.chapters <= 0 or args.chapters > case.request.target_chapters)
    ):
        raise SystemExit(
            f"--chapters 必须在 1 到 {case.request.target_chapters} 之间"
        )

    configured_provider, configured_model, configured_key = ModelCatalog().selection()
    settings = replace(
        NovelApplicationSettings(base_url=configured_provider.base_url, model=configured_model, api_key=configured_key),
        writer_temperature=case.experiment.temperature,
    )
    provider = OpenAICompatibleProviderSettings(
        base_url=settings.base_url,
        model_name=settings.model,
        api_key=settings.api_key,
    ).create_provider()
    model = provider.get_model(settings.model)
    judge_model_name = (
        args.judge_model
        or os.getenv("STORYWEAVER_EVAL_JUDGE_MODEL", "").strip()
        or settings.model
    )
    judge_model = provider.get_model(judge_model_name)
    extra_body: dict[str, object] = {}
    if settings.thinking is not None:
        extra_body["thinking"] = {"type": settings.thinking}
    if settings.reasoning_effort is not None:
        extra_body["reasoning_effort"] = settings.reasoning_effort

    store = EvaluationStore(args.output)
    judge = (
        _build_judge(
            settings=settings,
            model=judge_model,
            extra_body=extra_body,
        )
        if args.judge_passes
        else None
    )
    if args.judge_only is not None:
        if judge is None:
            raise SystemExit("--judge-only 要求 --judge-passes 为 1 或 2")
        result = store.load_run_result(args.judge_only)
        if result.case_id != case.case_id:
            raise SystemExit(
                "已有运行与 --case 不匹配："
                f"{result.case_id} != {case.case_id}"
            )
        _progress(f"[judge] 复用已有生成结果：{result.output_directory}")
        succeeded = await _grade_result(
            judge=judge,
            case=case,
            result=result,
            passes=args.judge_passes,
            store=store,
        )
        _progress(f"评测目录：{result.output_directory}")
        _progress(f"评分状态：{'completed' if succeeded else 'failed'}")
        return 0 if succeeded else 2

    database_url = database_url_from_env(evaluation=True)

    bare_writer = BareNovelWriter(
        WorkerSettings(
            worker_id="evaluation-bare-writer",
            name="裸模型小说作者",
            instructions=BareNovelWriter.SYSTEM_PROMPT,
            model=model,
            model_settings=ModelSettings(
                temperature=case.experiment.temperature,
                max_tokens=settings.writer_output_token_limit,
                timeout=settings.timeout_seconds,
                extra_body=extra_body or None,
            ),
            timeout_seconds=settings.timeout_seconds,
        ),
        progress=_progress,
    )
    database = Database(DatabaseSettings(database_url))
    _progress("[preflight] 检查 SQLite 连接与数据库结构")
    try:
        database.acquire_instance_lock()
        database.create_schema()
        with database.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        if not inspect(database.engine).has_table("books"):
            raise RuntimeError("缺少 books 表，请先执行数据库迁移")
    except Exception as exc:
        database.dispose()
        raise SystemExit(
            "SQLite 预检失败，评测尚未发起任何模型调用："
            f"{type(exc).__name__}: {exc}"
        ) from exc
    _progress("[preflight] SQLite 可用，开始评测")
    projects = SQLAlchemyStoryProjectRepository(database)

    def service_factory(_case_directory: Path, observer):
        return build_novel_service(
            settings,
            observer=observer,
            store=projects,
        )

    runner = EvaluationRunner(
        bare_writer=bare_writer,
        service_factory=service_factory,
        store=store,
        runtime_metadata={
            "model": settings.model,
            "base_url": settings.base_url,
            "temperature": case.experiment.temperature,
            "judge_passes": args.judge_passes,
            "judge_model": judge_model_name if args.judge_passes else None,
            "case_file": str(args.case.resolve()),
        },
        progress=_progress,
    )
    try:
        all_completed = True
        for repetition in range(1, case.experiment.repetitions + 1):
            requested_run_id = args.run_id
            if requested_run_id and case.experiment.repetitions > 1:
                requested_run_id = f"{requested_run_id}-r{repetition:02d}"
            result = await runner.run_case(
                case,
                chapters=args.chapters,
                run_id=requested_run_id,
            )
            run_directory = Path(result.output_directory)
            if judge is not None:
                grading_completed = await _grade_result(
                    judge=judge,
                    case=case,
                    result=result,
                    passes=args.judge_passes,
                    store=store,
                )
                all_completed = all_completed and grading_completed
            _progress(
                f"评测完成（{repetition}/{case.experiment.repetitions}）："
                f"{run_directory}"
            )
            _progress(f"运行状态：{result.status}")
            all_completed = all_completed and result.status == "completed"
        return 0 if all_completed else 2
    finally:
        database.dispose()


def main() -> None:
    raise SystemExit(asyncio.run(run(build_parser().parse_args())))


if __name__ == "__main__":
    main()
