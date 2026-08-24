"""Action Surface 的唯一动作参数契约。"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field


class _ActionParameters(BaseModel):
    """拒绝未声明字段，避免自然语言和按钮入口产生不同的隐式参数。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PrepareChapterPlanParameters(_ActionParameters):
    instruction: str | None = Field(default=None, max_length=4000)


class ReviseChapterPlanParameters(_ActionParameters):
    feedback: str = Field(min_length=1, max_length=4000)
    proposal_id: str | None = None


class ConfirmAndWriteParameters(_ActionParameters):
    proposal_id: str | None = None


class RewriteChapterParameters(_ActionParameters):
    chapter_number: int = Field(ge=1)
    instruction: str | None = Field(default=None, max_length=4000)


class CancelChapterPlanParameters(_ActionParameters):
    proposal_id: str | None = None


class WriteBatchParameters(_ActionParameters):
    count: int = Field(ge=1, le=50)
    instruction: str | None = Field(default=None, max_length=4000)


class EmptyParameters(_ActionParameters):
    pass


class QueryBookStateParameters(EmptyParameters):
    pass


class QueryStoryProgressParameters(EmptyParameters):
    pass


class QueryCompletedChaptersParameters(_ActionParameters):
    limit: int | None = Field(default=None, ge=1, le=50)


class QueryChapterParameters(_ActionParameters):
    chapter_number: int | None = Field(default=None, ge=1)
    include: Literal["summary", "content", "plan", "review"] = "summary"


class QueryPendingPlanParameters(EmptyParameters):
    pass


class QueryRecentReviewParameters(_ActionParameters):
    chapter_number: int | None = Field(default=None, ge=1)


class QueryOpenForeshadowingsParameters(_ActionParameters):
    limit: int | None = Field(default=None, ge=1, le=30)


class QueryTextParameters(_ActionParameters):
    query: str = Field(default="", max_length=200)


ActionParameters: TypeAlias = (
    PrepareChapterPlanParameters | ReviseChapterPlanParameters | ConfirmAndWriteParameters |
    RewriteChapterParameters | CancelChapterPlanParameters | WriteBatchParameters |
    QueryBookStateParameters | QueryStoryProgressParameters | QueryCompletedChaptersParameters |
    QueryChapterParameters | QueryPendingPlanParameters | QueryRecentReviewParameters |
    QueryOpenForeshadowingsParameters | QueryTextParameters
)


ACTION_PARAMETER_MODELS: dict[str, type[_ActionParameters]] = {
    "prepare_chapter_plan": PrepareChapterPlanParameters,
    "revise_chapter_plan": ReviseChapterPlanParameters,
    "confirm_and_write_chapter": ConfirmAndWriteParameters,
    "rewrite_chapter": RewriteChapterParameters,
    "cancel_chapter_plan": CancelChapterPlanParameters,
    "write_batch": WriteBatchParameters,
    "query_book_state": QueryBookStateParameters,
    "query_story_progress": QueryStoryProgressParameters,
    "query_completed_chapters": QueryCompletedChaptersParameters,
    "query_chapter": QueryChapterParameters,
    "query_pending_plan": QueryPendingPlanParameters,
    "query_recent_review": QueryRecentReviewParameters,
    "query_open_foreshadowings": QueryOpenForeshadowingsParameters,
    "query_character": QueryTextParameters,
    "query_foreshadowing": QueryTextParameters,
    "explain_review": QueryRecentReviewParameters,
}


def validate_action_parameters(action: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """用同一份 Pydantic 模型校验 Main Agent、快捷按钮和确认操作。"""

    model = ACTION_PARAMETER_MODELS.get(action)
    if model is None:
        raise ValueError(f"不支持的业务动作：{action}")
    return model.model_validate(parameters).model_dump(exclude_none=True)
