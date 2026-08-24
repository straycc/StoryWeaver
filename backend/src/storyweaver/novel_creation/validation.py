"""小说基础资料的确定性业务校验。"""

from __future__ import annotations

import math
import re
from dataclasses import replace

from .exceptions import (
    ChapterDraftValidationError,
    ChapterPlanValidationError,
    NovelFoundationValidationError,
    SerializationError,
)
from .hook_manager import HookManager
from .models import (
    ChapterContext,
    ChapterDraft,
    ChapterPlan,
    CreateNovelRequest,
    NovelFoundation,
    NovelProject,
)
from .serialization import decode_chapter_plan, loads_json


class NovelFoundationValidator:
    """校验 Architect 输出是否足以创建一个可继续写作的项目。"""

    def validate(
        self,
        *,
        request: CreateNovelRequest,
        foundation: NovelFoundation,
    ) -> None:
        if len(foundation.characters) < 2:
            raise NovelFoundationValidationError("小说基础资料至少需要两个角色")

        normalized_protagonist = self._normalize(request.protagonist)
        character_names = {
            self._normalize(character.name) for character in foundation.characters
        }
        if normalized_protagonist not in character_names:
            raise NovelFoundationValidationError(
                f"小说基础资料缺少指定主角：{request.protagonist}"
            )

        if not foundation.outline:
            raise NovelFoundationValidationError("小说基础资料至少需要一个大纲节点")
        for node in foundation.outline:
            if node.chapter_end > request.target_chapters:
                raise NovelFoundationValidationError(
                    f"大纲节点 {node.node_id} 超出目标章节数 "
                    f"{request.target_chapters}"
                )

        if not foundation.writing_rules:
            raise NovelFoundationValidationError("小说基础资料至少需要一条写作规则")

        invalid_hooks = [
            hook.hook_id
            for hook in foundation.initial_hooks
            if hook.opened_chapter != 0 or hook.last_advanced_chapter != 0
        ]
        if invalid_hooks:
            names = ", ".join(sorted(invalid_hooks))
            raise NovelFoundationValidationError(
                f"初始伏笔的章节号必须为 0：{names}"
            )

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(value.casefold().split())


class ChapterPlanValidator:
    """校验 Planner 输出能否安全用于当前项目的下一章。"""

    def validate(self, *, project: NovelProject, plan: ChapterPlan) -> None:
        expected_chapter = project.state.last_committed_chapter + 1
        if plan.chapter_number != expected_chapter:
            raise ChapterPlanValidationError(
                f"章节计划编号不连续：期望 {expected_chapter}，"
                f"实际 {plan.chapter_number}"
            )
        if plan.chapter_number > project.metadata.target_chapters:
            raise ChapterPlanValidationError(
                f"章节计划超出目标章节数 {project.metadata.target_chapters}"
            )

        character_ids = {
            character.character_id for character in project.foundation.characters
        }
        unknown_characters = set(plan.participating_character_ids) - character_ids
        if unknown_characters:
            names = ", ".join(sorted(unknown_characters))
            raise ChapterPlanValidationError(f"章节计划引用了未知角色：{names}")

        hooks = {hook.hook_id: hook for hook in project.state.hooks}
        unknown_hooks = set(plan.relevant_hook_ids) - set(hooks)
        if unknown_hooks:
            names = ", ".join(sorted(unknown_hooks))
            raise ChapterPlanValidationError(f"章节计划引用了未知伏笔：{names}")
        resolved_hooks = [
            hook_id
            for hook_id in plan.relevant_hook_ids
            if hooks[hook_id].status == "resolved"
        ]
        if resolved_hooks:
            names = ", ".join(sorted(resolved_hooks))
            raise ChapterPlanValidationError(f"章节计划不能推进已解决伏笔：{names}")

        hook_plan_ids = (
            set(plan.hook_plan.advance_hook_ids)
            | set(plan.hook_plan.resolve_hook_ids)
        )
        missing_from_relevant = hook_plan_ids - set(plan.relevant_hook_ids)
        if missing_from_relevant:
            names = ", ".join(sorted(missing_from_relevant))
            raise ChapterPlanValidationError(
                f"hook_plan 伏笔必须同时列入 relevant_hook_ids：{names}"
            )
        unknown_hook_plan_ids = hook_plan_ids - set(hooks)
        if unknown_hook_plan_ids:
            names = ", ".join(sorted(unknown_hook_plan_ids))
            raise ChapterPlanValidationError(f"hook_plan 引用了未知伏笔：{names}")
        if plan.hook_plan.new_hook_budget > HookManager().new_hook_budget(project=project):
            raise ChapterPlanValidationError("当前章节不允许超出伏笔新增预算")

        required = {self._normalize(item) for item in plan.required_beats}
        forbidden = {self._normalize(item) for item in plan.forbidden_events}
        conflicts = required & forbidden
        if conflicts:
            names = ", ".join(sorted(conflicts))
            raise ChapterPlanValidationError(
                f"required_beats 与 forbidden_events 直接冲突：{names}"
            )

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(value.casefold().split())


class ChapterDraftValidator:
    """校验并规范化 Writer 返回的章节正文。"""

    _PLACEHOLDER_TITLES = frozenset(
        {
            "章节",
            "新章节",
            "未命名",
            "未命名章节",
            "正文",
        }
    )
    _NUMBERED_PLACEHOLDER_PATTERN = re.compile(
        r"^(?:第\s*[0-9一二三四五六七八九十百千零〇两]+\s*章|"
        r"章节?\s*[0-9一二三四五六七八九十百千零〇两]+)$",
        re.IGNORECASE,
    )

    def __init__(
        self,
        *,
        minimum_target_ratio: float = 0.5,
        maximum_target_ratio: float = 1.8,
        maximum_title_length: int = 100,
    ) -> None:
        if not 0 < minimum_target_ratio <= 1:
            raise ValueError("minimum_target_ratio 必须在 0 到 1 之间")
        if maximum_target_ratio < 1:
            raise ValueError("maximum_target_ratio 不能小于 1")
        if minimum_target_ratio > maximum_target_ratio:
            raise ValueError("最小目标比例不能大于最大目标比例")
        if maximum_title_length <= 0:
            raise ValueError("maximum_title_length 必须大于 0")
        self.minimum_target_ratio = minimum_target_ratio
        self.maximum_target_ratio = maximum_target_ratio
        self.maximum_title_length = maximum_title_length

    def validate_and_normalize(
        self,
        *,
        context: ChapterContext,
        draft: ChapterDraft,
    ) -> ChapterDraft:
        plan = self._extract_plan(context)
        target_words = self._extract_target_words(context)
        if plan.chapter_number != context.chapter_number:
            raise ChapterDraftValidationError("Context 中的计划章节号不一致")
        if draft.chapter_number != context.chapter_number:
            raise ChapterDraftValidationError(
                f"正文章节号不一致：期望 {context.chapter_number}，"
                f"实际 {draft.chapter_number}"
            )
        if "\n" in draft.title or "\r" in draft.title:
            raise ChapterDraftValidationError("章节标题不能包含换行")
        if len(draft.title.strip()) > self.maximum_title_length:
            raise ChapterDraftValidationError(
                f"章节标题不能超过 {self.maximum_title_length} 个字符"
            )
        normalized_title = self._normalize_title(draft.title)
        if self._is_placeholder_title(normalized_title):
            raise ChapterDraftValidationError(
                f"章节标题不能使用占位名称：{draft.title.strip()}"
            )
        book_title = self._extract_book_title(context)
        if normalized_title == self._normalize_title(book_title):
            raise ChapterDraftValidationError("章节标题不能与书名相同")

        actual_words = self.count_text_units(draft.content)
        if actual_words <= 0:
            raise ChapterDraftValidationError("章节正文不包含可计数文本")
        minimum_words = max(1, math.ceil(target_words * self.minimum_target_ratio))
        maximum_words = max(minimum_words, math.floor(target_words * self.maximum_target_ratio))
        if actual_words < minimum_words:
            raise ChapterDraftValidationError(
                f"章节正文过短：实际 {actual_words}，最低要求 {minimum_words}"
            )
        if actual_words > maximum_words:
            raise ChapterDraftValidationError(
                f"章节正文过长：实际 {actual_words}，最高允许 {maximum_words}"
            )
        return replace(draft, word_count=actual_words)

    @staticmethod
    def count_text_units(content: str) -> int:
        """按 CJK 单字及英文/数字词计算稳定的跨语言文本单位。"""

        units = re.findall(
            r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]|[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*",
            content,
        )
        return len(units)

    @staticmethod
    def _extract_plan(context: ChapterContext) -> ChapterPlan:
        entries = [
            entry for entry in context.entries if entry.source_type == "chapter_plan"
        ]
        if len(entries) != 1:
            raise ChapterDraftValidationError("Context 必须包含一个章节计划")
        try:
            return decode_chapter_plan(loads_json(entries[0].content))
        except SerializationError as exc:
            raise ChapterDraftValidationError(
                f"Context 中的章节计划无法解析：{exc}"
            ) from exc

    @staticmethod
    def _extract_target_words(context: ChapterContext) -> int:
        data = ChapterDraftValidator._extract_book_constraints(context)
        target_words = data.get("chapter_target_words")
        if not isinstance(target_words, int) or isinstance(target_words, bool):
            raise ChapterDraftValidationError("书籍约束缺少有效 chapter_target_words")
        if target_words <= 0:
            raise ChapterDraftValidationError("chapter_target_words 必须大于 0")
        return target_words

    @staticmethod
    def _extract_book_title(context: ChapterContext) -> str:
        data = ChapterDraftValidator._extract_book_constraints(context)
        book_title = data.get("title")
        if not isinstance(book_title, str) or not book_title.strip():
            raise ChapterDraftValidationError("书籍约束缺少有效 title")
        return book_title

    @staticmethod
    def _extract_book_constraints(context: ChapterContext) -> dict[str, object]:
        entries = [
            entry for entry in context.entries if entry.source_type == "book_constraints"
        ]
        if len(entries) != 1:
            raise ChapterDraftValidationError("Context 必须包含一个书籍约束条目")
        try:
            data = loads_json(entries[0].content)
        except SerializationError as exc:
            raise ChapterDraftValidationError(
                f"Context 中的书籍约束无法解析：{exc}"
            ) from exc
        if not isinstance(data, dict):
            raise ChapterDraftValidationError("书籍约束必须是 JSON 对象")
        return data

    @classmethod
    def _is_placeholder_title(cls, normalized_title: str) -> bool:
        return (
            normalized_title in cls._PLACEHOLDER_TITLES
            or cls._NUMBERED_PLACEHOLDER_PATTERN.fullmatch(normalized_title)
            is not None
        )

    @staticmethod
    def _normalize_title(title: str) -> str:
        """仅折叠空白和大小写，避免改变有文学意义的标题符号。"""

        return "".join(title.casefold().split())
