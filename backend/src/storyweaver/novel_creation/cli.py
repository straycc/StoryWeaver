"""StoryWeaver 小说创作命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path

from .application import (
    DEFAULT_BOOKS_DIRECTORY,
    PROJECT_ROOT,
    NovelApplicationSettings,
    NovelService,
    build_novel_service,
    load_env_file,
)
from .models import ChapterResult, CreateNovelRequest, NovelProject
from .observability import NovelRunObserver, UsageSummary
from .project_store import NovelProjectStore


ModelFactory = Callable[[NovelApplicationSettings], object]


RAINY_HOTEL_REQUEST = CreateNovelRequest(
    title="雨夜旅馆",
    genre="悬疑",
    premise="年轻侦探林默进入废弃旅馆，调查十年前发生的失踪案。",
    protagonist="林默",
    central_conflict="林默寻找真相，旅馆中的神秘人试图把他引向错误线索。",
    tone="克制、压迫、有限视角",
    target_chapters=6,
    chapter_target_words=1200,
    language="zh",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="storyweaver-novel",
        description="使用真实模型创建和连续写作 StoryWeaver 小说项目。",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=PROJECT_ROOT / ".env",
        help=".env 文件路径（默认：项目根目录/.env）",
    )
    parser.add_argument(
        "--books-dir",
        type=Path,
        help="小说项目目录（默认读取 STORYWEAVER_BOOKS_DIR 或 data/books）",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="失败时显示完整异常堆栈",
    )

    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="从创作简报创建小说项目")
    create.add_argument("--example", action="store_true", help="使用“雨夜旅馆”示例")
    create.add_argument("--title", help="小说标题")
    create.add_argument("--genre", help="题材")
    create.add_argument("--premise", help="核心创意或故事前提")
    create.add_argument("--protagonist", help="主角名称")
    create.add_argument("--conflict", help="核心冲突")
    create.add_argument("--tone", help="叙事基调")
    create.add_argument("--target-chapters", type=int, help="目标章节数")
    create.add_argument("--chapter-words", type=int, help="每章目标字数")
    create.add_argument("--language", default="zh", help="创作语言，默认 zh")
    create.add_argument(
        "--write",
        type=int,
        default=0,
        metavar="N",
        help="建书后立即连续生成 N 章",
    )
    create.add_argument("--instruction", help="立即写章时应用的用户要求")
    create.add_argument(
        "--no-content",
        action="store_true",
        help="写章后不在终端打印完整正文",
    )

    commands.add_parser("list", help="列出本地小说项目")

    status = commands.add_parser("status", help="查看项目设定和当前状态")
    status.add_argument("book_id", help="项目 book_id")

    write = commands.add_parser("write", help="为已有项目写下一章")
    write.add_argument("book_id", help="项目 book_id")
    write.add_argument("--chapters", type=int, default=1, help="连续生成章数")
    write.add_argument("--instruction", help="本次写作附加要求")
    write.add_argument(
        "--no-content",
        action="store_true",
        help="不在终端打印完整正文",
    )

    show = commands.add_parser("show", help="查看已经落盘的章节正文")
    show.add_argument("book_id", help="项目 book_id")
    show.add_argument("--chapter", type=int, help="章节号，默认显示最新章")
    return parser


def _required_value(value: str | None, prompt: str) -> str:
    if value is not None and value.strip():
        return value.strip()
    while True:
        entered = input(f"{prompt}：").strip()
        if entered:
            return entered
        print("该项不能为空。")


def _positive_int(value: int | None, prompt: str, default: int) -> int:
    if value is not None:
        if value <= 0:
            raise ValueError(f"{prompt}必须大于 0")
        return value
    while True:
        entered = input(f"{prompt} [{default}]：").strip()
        if not entered:
            return default
        try:
            parsed = int(entered)
        except ValueError:
            print("请输入整数。")
            continue
        if parsed > 0:
            return parsed
        print("请输入大于 0 的整数。")


def request_from_args(args: argparse.Namespace) -> CreateNovelRequest:
    """从参数或交互输入构造创作简报。"""

    if args.example:
        return RAINY_HOTEL_REQUEST
    return CreateNovelRequest(
        title=_required_value(args.title, "小说标题"),
        genre=_required_value(args.genre, "题材"),
        premise=_required_value(args.premise, "核心创意/故事前提"),
        protagonist=_required_value(args.protagonist, "主角"),
        central_conflict=_required_value(args.conflict, "核心冲突"),
        tone=_required_value(args.tone, "叙事基调"),
        target_chapters=_positive_int(args.target_chapters, "目标章节数", 6),
        chapter_target_words=_positive_int(args.chapter_words, "每章目标字数", 1200),
        language=_required_value(args.language, "创作语言"),
    )


def _books_directory(args: argparse.Namespace) -> Path:
    configured = os.getenv("STORYWEAVER_BOOKS_DIR")
    return Path(args.books_dir or configured or DEFAULT_BOOKS_DIRECTORY).expanduser()


def _build_model_service(
    args: argparse.Namespace,
    observer: NovelRunObserver,
    model_factory: ModelFactory | None,
) -> tuple[NovelApplicationSettings, NovelService]:
    settings = NovelApplicationSettings.from_env(
        books_directory=_books_directory(args),
    )
    service = build_novel_service(settings, observer=observer)
    print(f"模型：{settings.model}")
    print(f"接口：{settings.base_url}")
    print(f"项目目录：{settings.books_directory}")
    return settings, service


def _print_project(project: NovelProject) -> None:
    print("\n=== 小说项目已创建 ===")
    print(f"book_id：{project.metadata.book_id}")
    print(f"标题：{project.metadata.title}")
    print(f"题材：{project.metadata.genre}")
    print(f"目标：{project.metadata.target_chapters} 章，"
          f"每章约 {project.metadata.chapter_target_words} 字")
    print(f"故事前提：{project.foundation.premise}")
    print(f"世界设定：{project.foundation.world_setting}")
    print(f"核心冲突：{project.foundation.central_conflict}")
    print("角色：" + "、".join(item.name for item in project.foundation.characters))
    print("大纲：")
    for node in project.foundation.outline:
        print(
            f"  - 第 {node.chapter_start}-{node.chapter_end} 章 "
            f"{node.title}：{node.goal}"
        )


def _print_chapter_result(result: ChapterResult, *, show_content: bool) -> None:
    heading = "生成完成（已提交）" if result.committed else "候选正文未提交"
    print(f"\n=== 第 {result.chapter_number} 章{heading} ===")
    print(f"标题：{result.final_draft.title}")
    print(f"字数：{result.final_draft.word_count}")
    print(f"状态：{result.status}")
    revised_label = "否" if result.revision_count == 0 else f"是（{result.revision_count} 轮）"
    print(f"是否修订：{revised_label}")
    score = result.final_review.score
    print(f"审查：{result.final_review.summary}"
          + (f"（{score} 分）" if score is not None else ""))
    if result.committed:
        if result.state_delta is None:  # pragma: no cover - 模型不变量
            raise RuntimeError("已提交章节缺少状态增量")
        print(f"摘要：{result.state_delta.chapter_summary}")
    else:
        print(f"候选 ID：{result.candidate_id}")
        print("正史状态：未变化；请处理审稿问题后重新生成。")
    if show_content:
        print("\n--- 正文 ---")
        print(result.final_draft.content)
        print("--- 正文结束 ---")


def _print_usage(summary: UsageSummary) -> None:
    print("\n=== 本次模型用量 ===")
    print(f"Worker 调用：{summary.run_count}")
    print(f"输入 Token：{summary.input_tokens}")
    print(f"输出 Token：{summary.output_tokens}")
    print(f"合计 Token：{summary.total_tokens}")
    print(f"模型阶段累计耗时：{summary.elapsed_seconds:.2f}s")


async def _run_create(
    args: argparse.Namespace,
    *,
    model_factory: ModelFactory | None,
) -> None:
    if args.write < 0:
        raise ValueError("--write 不能小于 0")
    observer = NovelRunObserver()
    marker = observer.mark()
    try:
        _, service = _build_model_service(args, observer, model_factory)
        project = await service.create_project(request_from_args(args))
        _print_project(project)
        if args.write:
            results = await service.write_chapters(
                book_id=project.metadata.book_id,
                count=args.write,
                user_instruction=args.instruction,
            )
            for result in results:
                _print_chapter_result(result, show_content=not args.no_content)
    finally:
        summary = observer.summarize(since=marker)
        if summary.run_count:
            _print_usage(summary)


async def _run_write(
    args: argparse.Namespace,
    *,
    model_factory: ModelFactory | None,
) -> None:
    observer = NovelRunObserver()
    marker = observer.mark()
    try:
        _, service = _build_model_service(args, observer, model_factory)
        results = await service.write_chapters(
            book_id=args.book_id,
            count=args.chapters,
            user_instruction=args.instruction,
        )
        for result in results:
            _print_chapter_result(result, show_content=not args.no_content)
    finally:
        summary = observer.summarize(since=marker)
        if summary.run_count:
            _print_usage(summary)


def _run_list(store: NovelProjectStore) -> None:
    projects = store.list_projects()
    if not projects:
        print("还没有小说项目。")
        return
    print("book_id\t进度\t标题")
    for metadata in projects:
        state = store.load_state(metadata.book_id)
        print(
            f"{metadata.book_id}\t"
            f"{state.last_committed_chapter}/{metadata.target_chapters}\t"
            f"{metadata.title}"
        )


def _run_status(store: NovelProjectStore, book_id: str) -> None:
    project = store.load_project(book_id)
    index = store.load_chapter_index(book_id)
    print(f"标题：{project.metadata.title}")
    print(f"book_id：{project.metadata.book_id}")
    print(
        f"进度：{project.state.last_committed_chapter}/"
        f"{project.metadata.target_chapters} 章"
    )
    print(f"当前时间：{project.state.current_time}")
    print(f"当前位置：{project.state.current_location}")
    print(f"有效事实：{len(project.state.current_facts)}")
    print("角色状态：")
    for character in project.state.characters:
        print(
            f"  - {character.character_id} · {character.location} · "
            f"{character.status} · {character.current_goal}"
        )
    print("伏笔：")
    for hook in project.state.hooks:
        print(f"  - {hook.hook_id} [{hook.status}] {hook.description}")
    print("章节：")
    for chapter in index:
        print(
            f"  - 第 {chapter.chapter_number} 章 {chapter.title} · "
            f"{chapter.word_count} 字 · {chapter.status}"
        )


def _run_show(
    store: NovelProjectStore,
    book_id: str,
    chapter_number: int | None,
) -> None:
    if chapter_number is not None and chapter_number <= 0:
        raise ValueError("--chapter 必须大于 0")
    selected = chapter_number
    if selected is None:
        selected = store.load_state(book_id).last_committed_chapter
        if selected == 0:
            raise ValueError("项目还没有已生成章节")
    draft = store.load_chapter(book_id, selected)
    print(f"# 第 {draft.chapter_number} 章 {draft.title}\n")
    print(draft.content)


def main(
    argv: Sequence[str] | None = None,
    *,
    model_factory: ModelFactory | None = None,
) -> int:
    """执行 CLI；``model_factory`` 仅用于无网络测试和嵌入调用。"""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        load_env_file(args.env_file)
        store = NovelProjectStore(_books_directory(args))
        if args.command == "create":
            asyncio.run(_run_create(args, model_factory=model_factory))
        elif args.command == "write":
            asyncio.run(_run_write(args, model_factory=model_factory))
        elif args.command == "list":
            _run_list(store)
        elif args.command == "status":
            _run_status(store, args.book_id)
        elif args.command == "show":
            _run_show(store, args.book_id, args.chapter)
        else:  # pragma: no cover - argparse 已限制命令集合
            parser.error(f"未知命令：{args.command}")
        return 0
    except KeyboardInterrupt:
        print("\n操作已取消。", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 的统一错误边界
        print(f"执行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
