"""Action Surface 的唯一动作参数契约。"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _ActionParameters(BaseModel):
    """拒绝未声明字段，避免自然语言和按钮入口产生不同的隐式参数。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _CreativeActionParameters(_ActionParameters):
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


class PrepareChapterPlanParameters(_CreativeActionParameters):
    instruction: str | None = Field(default=None, max_length=4000)


class ReviseChapterPlanParameters(_ActionParameters):
    feedback: str = Field(min_length=1, max_length=4000)
    proposal_id: str | None = None


class ConfirmAndWriteParameters(_ActionParameters):
    proposal_id: str | None = None


class ApproveChapterPlanParameters(_ActionParameters):
    proposal_id: str | None = None


class WriteFromPlanParameters(_ActionParameters):
    proposal_id: str | None = None


class RewriteChapterParameters(_CreativeActionParameters):
    chapter_number: int = Field(ge=1)
    instruction: str | None = Field(default=None, max_length=4000)


class CancelChapterPlanParameters(_ActionParameters):
    proposal_id: str | None = None


class WriteBatchParameters(_CreativeActionParameters):
    count: int = Field(ge=1, le=50)
    instruction: str | None = Field(default=None, max_length=4000)


class CreativeDiscussionParameters(_ActionParameters):
    objective: str = Field(min_length=1, max_length=4000)


class CreateNovelParameters(_ActionParameters):
    title: str = Field(min_length=1, max_length=200)
    genre: str = Field(min_length=1, max_length=100)
    premise: str = Field(min_length=1, max_length=4000)
    protagonist: str = Field(min_length=1, max_length=1000)
    central_conflict: str = Field(min_length=1, max_length=2000)
    tone: str = Field(min_length=1, max_length=500)
    target_chapters: int = Field(ge=1, le=10000)
    chapter_target_words: int = Field(ge=200, le=100000)
    language: str = Field(default="zh", min_length=1, max_length=32)


class RunNextChapterWorkflowParameters(_CreativeActionParameters):
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
    ApproveChapterPlanParameters | WriteFromPlanParameters |
    RewriteChapterParameters | CancelChapterPlanParameters | WriteBatchParameters |
    QueryBookStateParameters | QueryStoryProgressParameters | QueryCompletedChaptersParameters |
    QueryChapterParameters | QueryPendingPlanParameters | QueryRecentReviewParameters |
    QueryOpenForeshadowingsParameters | QueryTextParameters |
    CreativeDiscussionParameters | CreateNovelParameters |
    RunNextChapterWorkflowParameters
)
