"""小说 Worker 的 Pydantic 输出契约。

这些 DTO 位于 LLM 边界：SDK 先完成结构校验，随后由小说领域层转换为
dataclass 并执行业务校验。它们不包含任何业务提交或状态变更逻辑。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StrictOutputModel(BaseModel):
    """拒绝模型生成的未知字段，避免结构漂移静默进入领域层。"""

    model_config = ConfigDict(extra="forbid")


class FoundationOutput(StrictOutputModel):
    premise: str
    world_setting: str
    central_conflict: str
    ending_direction: str
    characters: list[dict[str, Any]]
    outline: list[dict[str, Any]]
    writing_rules: list[str]
    initial_hooks: list[dict[str, Any]]


class HookPlanOutput(StrictOutputModel):
    advance_hook_ids: list[str]
    resolve_hook_ids: list[str]
    new_hook_budget: int


class PlanOutput(StrictOutputModel):
    chapter_number: int
    goal: str
    participating_character_ids: list[str]
    location: str
    required_beats: list[str]
    forbidden_events: list[str]
    relevant_hook_ids: list[str]
    ending_hook: str
    style_focus: list[str]
    hook_plan: HookPlanOutput


class DraftOutput(StrictOutputModel):
    chapter_number: int
    title: str
    content: str


class ReviewIssueOutput(StrictOutputModel):
    category: str
    severity: str
    description: str
    suggestion: str
    related_source_ids: list[str] = Field(default_factory=list)


class ReviewOutput(StrictOutputModel):
    passed: bool
    summary: str
    issues: list[ReviewIssueOutput]
    score: int | None = None
    parse_failed: bool = False


class DeltaOutput(StrictOutputModel):
    source_chapter: int
    chapter_summary: str
    character_updates: list[dict[str, Any]]
    new_facts: list[dict[str, Any]]
    invalidated_fact_ids: list[str]
    new_hooks: list[dict[str, Any]]
    hook_updates: list[dict[str, Any]]
    new_time: str | None = None
    new_location: str | None = None


NOVEL_OUTPUT_TYPES: dict[str, type[BaseModel]] = {
    "novel-architect": FoundationOutput,
    "novel-planner": PlanOutput,
    "novel-writer": DraftOutput,
    "novel-reviewer": ReviewOutput,
    "novel-reviewer-verification": ReviewOutput,
    "novel-reviser": DraftOutput,
    "chapter-analyzer": DeltaOutput,
}
