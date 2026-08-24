"""StoryWeaver 小说质量 A/B 评测命令行。"""

from __future__ import annotations

import argparse
import asyncio
import sys
import traceback
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from agents import ModelSettings
from ..llm import OpenAICompatibleProviderSettings, WorkerSettings
from ..novel_creation.application import (
    PROJECT_ROOT,
    NovelApplicationSettings,
    build_novel_service,
    load_env_file,
)
from .cases import EVALUATION_CASES, get_evaluation_case
from .models import EvaluationCase, EvaluationRunResult
from .runner import BareNovelWriter, EvaluationRunner
from .storage import EvaluationStore


DEFAULT_EVALUATION_DIRECTORY = PROJECT_ROOT / "data" / "evaluations"
ModelFactory = Callable[[NovelApplicationSettings], object]


def build_parser() -> argparse.ArgumentParser:
    case_ids = tuple(item.case_id for item in EVALUATION_CASES)
    parser = argparse.ArgumentParser(
        prog="storyweaver-eval",
        description="使用同一模型对比裸 Prompt 与 StoryWeaver Pipeline。",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=PROJECT_ROOT / ".env",
        help=".env 文件路径（默认：项目根目录/.env）",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_EVALUATION_DIRECTORY,
        help="评测输出目录（默认：data/evaluations）",
    )
    parser.add_argument(
        "--case",
        choices=(*case_ids, "all"),
        default="rainy-hotel",
        help="固定评测案例；all 会依次运行全部案例",
    )
    parser.add_argument(
        "--chapters",
        type=int,
        default=2,
        help="每个实验组连续生成的章节数（默认：2）",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="失败时输出完整异常堆栈",
    )
    return parser


def selected_cases(value: str) -> tuple[EvaluationCase, ...]:
    if value == "all":
        return EVALUATION_CASES
    return (get_evaluation_case(value),)


async def run_evaluation(
    args: argparse.Namespace,
    *,
    model_factory: ModelFactory | None = None,
) -> tuple[EvaluationRunResult, ...]:
    if args.chapters <= 0:
        raise ValueError("--chapters 必须大于 0")
    settings = NovelApplicationSettings.from_env()
    provider = OpenAICompatibleProviderSettings(
        base_url=settings.base_url, model_name=settings.model, api_key=settings.api_key,
    ).create_provider()
    store = EvaluationStore(args.output_dir.expanduser())

    def service_factory(case_directory, observer):
        scoped_settings = replace(
            settings,
            books_directory=case_directory / "storyweaver" / "books",
            long_term_memory_directory=(
                case_directory / "storyweaver" / "long_term_memory"
            ),
            model_diagnostics_directory=(
                case_directory / "storyweaver" / "diagnostics"
            ),
        )
        return build_novel_service(
            scoped_settings,
            observer=observer,
        )

    runner = EvaluationRunner(
        bare_writer=BareNovelWriter(WorkerSettings(
            worker_id="bare-writer", name="裸模型作者",
            instructions=BareNovelWriter.SYSTEM_PROMPT,
            model=provider.get_model(settings.model), model_settings=ModelSettings(temperature=settings.writer_temperature),
            timeout_seconds=settings.timeout_seconds,
        )),
        service_factory=service_factory,
        store=store,
    )
    results = []
    for case in selected_cases(args.case):
        print(f"\n=== 开始评测：{case.case_id} · {case.request.title} ===")
        print(f"裸模型 {args.chapters} 章 + StoryWeaver {args.chapters} 章")
        result = await runner.run_case(case, chapters=args.chapters)
        results.append(result)
        _print_result(result)
    return tuple(results)


def _print_result(result: EvaluationRunResult) -> None:
    status_label = "评测完成" if result.status == "completed" else "评测部分完成"
    print(f"{status_label}：{result.run_id}")
    print(f"输出目录：{result.output_directory}")
    _print_group(result.bare, requested_chapters=result.requested_chapters)
    _print_group(result.storyweaver, requested_chapters=result.requested_chapters)
    print("盲评文件：case/blind/sample-A.md、sample-B.md、scorecard.csv")


def _print_group(group, *, requested_chapters: int) -> None:
    total_tokens = sum(item.total_tokens for item in group.metrics)
    elapsed = sum(item.elapsed_seconds for item in group.metrics)
    failures = sum(not item.succeeded for item in group.metrics)
    retries = sum(item.retry_count for item in group.metrics)
    print(
        f"- {group.group}: 目标 {requested_chapters} 章，"
        f"成功 {len(group.chapters)} 章，"
        f"Token {total_tokens}，耗时 {elapsed:.2f}s，"
        f"失败 {failures}，重试 {retries}"
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    model_factory: ModelFactory | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        load_env_file(args.env_file)
        asyncio.run(run_evaluation(args, model_factory=model_factory))
        return 0
    except KeyboardInterrupt:
        print("评测已由用户中断。", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"评测失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
