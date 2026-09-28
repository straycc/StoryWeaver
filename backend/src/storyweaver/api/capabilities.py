"""Main Agent 可选择的统一业务能力目录。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from .action_schemas import (
    ApproveChapterPlanParameters,
    ApplyFoundationRevisionParameters,
    CancelChapterPlanParameters,
    ConfirmAndWriteParameters,
    ConfirmFoundationParameters,
    CreateNovelParameters,
    CreativeDiscussionParameters,
    PrepareChapterPlanParameters,
    QueryBookStateParameters,
    QueryChapterParameters,
    QueryCompletedChaptersParameters,
    QueryOpenForeshadowingsParameters,
    QueryPendingPlanParameters,
    QueryRecentReviewParameters,
    QueryStoryProgressParameters,
    QueryTextParameters,
    ReviseChapterPlanParameters,
    RewriteChapterParameters,
    RunNextChapterWorkflowParameters,
    WriteBatchParameters,
    WriteFromPlanParameters,
)


CapabilityCategory = Literal["query", "creative", "workflow"]


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    capability_id: str
    category: CapabilityCategory
    description: str
    parameter_model: type[BaseModel]
    requires_book: bool
    requires_proposal: bool = False
    requires_confirmation: bool = False


_DEFINITIONS = (
    CapabilityDefinition("query_book_state", "query", "查看当前作品基础状态", QueryBookStateParameters, True),
    CapabilityDefinition("query_story_progress", "query", "查看作品创作进度", QueryStoryProgressParameters, True),
    CapabilityDefinition("query_completed_chapters", "query", "列出已完成章节", QueryCompletedChaptersParameters, True),
    CapabilityDefinition("query_chapter", "query", "查看指定或最近章节的摘要、正文、计划或审查", QueryChapterParameters, True),
    CapabilityDefinition("query_pending_plan", "query", "查看当前待处理章节计划", QueryPendingPlanParameters, True),
    CapabilityDefinition("query_recent_review", "query", "查看最近章节审查", QueryRecentReviewParameters, True),
    CapabilityDefinition("query_open_foreshadowings", "query", "查看未解伏笔", QueryOpenForeshadowingsParameters, True),
    CapabilityDefinition("query_character", "query", "查询人物资料", QueryTextParameters, True),
    CapabilityDefinition("query_foreshadowing", "query", "检索伏笔", QueryTextParameters, True),
    CapabilityDefinition("explain_review", "query", "解释章节审查结果", QueryRecentReviewParameters, True),
    CapabilityDefinition("creative_discussion", "creative", "借助本次选择的 Skill 讨论创作问题，但不修改作品状态", CreativeDiscussionParameters, False),
    CapabilityDefinition("create_novel", "workflow", "根据已讨论的创作简报创建新作品", CreateNovelParameters, False, requires_confirmation=True),
    CapabilityDefinition("confirm_foundation", "workflow", "确认当前故事基础资料并创建作品", ConfirmFoundationParameters, False),
    CapabilityDefinition("apply_foundation_revision", "workflow", "确认故事基础资料修订", ApplyFoundationRevisionParameters, True),
    CapabilityDefinition("prepare_chapter_plan", "creative", "只生成下一章候选计划，等待用户后续决定", PrepareChapterPlanParameters, True),
    CapabilityDefinition("revise_chapter_plan", "creative", "根据反馈修订当前候选计划", ReviseChapterPlanParameters, True, True),
    CapabilityDefinition("cancel_chapter_plan", "workflow", "取消当前候选计划", CancelChapterPlanParameters, True, True),
    CapabilityDefinition("approve_chapter_plan", "workflow", "批准当前候选计划但暂不生成正文", ApproveChapterPlanParameters, True, True),
    CapabilityDefinition("write_from_plan", "workflow", "根据已批准的候选计划生成正文并完成审查提交", WriteFromPlanParameters, True, True, True),
    CapabilityDefinition("confirm_and_write_chapter", "workflow", "兼容旧入口：确认候选计划并生成正文", ConfirmAndWriteParameters, True, True, True),
    CapabilityDefinition("rewrite_chapter", "workflow", "回退并重写指定章节", RewriteChapterParameters, True, requires_confirmation=True),
    CapabilityDefinition("write_batch", "workflow", "连续创作指定数量章节", WriteBatchParameters, True, requires_confirmation=True),
    CapabilityDefinition("run_next_chapter_workflow", "workflow", "启动可暂停的下一章完整工作流，先产出候选计划并等待确认", RunNextChapterWorkflowParameters, True),
)


CAPABILITY_MANIFEST = {item.capability_id: item for item in _DEFINITIONS}


def get_capability(capability_id: str) -> CapabilityDefinition:
    try:
        return CAPABILITY_MANIFEST[capability_id]
    except KeyError as exc:
        raise ValueError(f"不支持的业务能力：{capability_id}") from exc


def validate_capability_parameters(
    capability_id: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """所有入口通过 Manifest 指向的同一 DTO 完成参数校验。"""

    definition = get_capability(capability_id)
    return definition.parameter_model.model_validate(parameters).model_dump(
        exclude_none=True
    )


def render_main_agent_manifest() -> str:
    """生成供 Main Agent 选择能力的精简目录。"""

    sections: list[str] = []
    for category, title in (
        ("query", "Query"),
        ("creative", "Creative"),
        ("workflow", "Workflow"),
    ):
        sections.append(f"{title} 能力：")
        sections.extend(
            f"- {item.capability_id}：{item.description}"
            for item in _DEFINITIONS
            if item.category == category
        )
    return "\n".join(sections)
