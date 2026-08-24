"""章节审稿结果的确定性质量门禁。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .models import REVIEW_CATEGORIES, ReviewIssue, ReviewReport


class ReviewDecision(StrEnum):
    """质量门禁对当前正文作出的执行决定。"""

    ACCEPT = "accept"
    REVISE = "revise"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class ReviewQualityGatePolicy:
    """Review Loop V2 的稳定阈值。"""

    minimum_score: int = 80
    minimum_target_ratio: float = 0.5
    soft_maximum_target_ratio: float = 1.5
    maximum_target_ratio: float = 1.8
    # 普通 warning 与总分是审查观察项，不能单独触发昂贵的自动重写。
    # 此字段保留用于兼容已有配置，并仅控制观察信息的汇总阈值。
    warning_count_threshold: int = 3
    max_revision_rounds: int = 1
    blocking_warning_categories: frozenset[str] = frozenset(
        {
            "character_consistency",
            "knowledge_boundary",
            "world_continuity",
            "plan_following",
            "hook_consistency",
        }
    )

    def __post_init__(self) -> None:
        if not 0 <= self.minimum_score <= 100:
            raise ValueError("minimum_score 必须在 0 到 100 之间")
        if not 0 < self.minimum_target_ratio <= 1:
            raise ValueError("minimum_target_ratio 必须在 0 到 1 之间")
        if self.maximum_target_ratio < 1:
            raise ValueError("maximum_target_ratio 不能小于 1")
        if self.minimum_target_ratio > self.maximum_target_ratio:
            raise ValueError("最小目标比例不能大于最大目标比例")
        if not self.minimum_target_ratio <= self.soft_maximum_target_ratio <= self.maximum_target_ratio:
            raise ValueError("软长度上限必须介于最小和最大目标比例之间")
        if self.warning_count_threshold <= 0:
            raise ValueError("warning_count_threshold 必须大于 0")
        if self.max_revision_rounds < 0:
            raise ValueError("max_revision_rounds 不能小于 0")
        unknown_categories = self.blocking_warning_categories - REVIEW_CATEGORIES
        if unknown_categories:
            names = ", ".join(sorted(unknown_categories))
            raise ValueError(f"存在未知阻断审查类别：{names}")


@dataclass(frozen=True, slots=True)
class ReviewGateResult:
    """门禁决定、可审计原因及交给 Reviser 的问题。"""

    decision: ReviewDecision
    reasons: tuple[str, ...]
    actionable_issues: tuple[ReviewIssue, ...] = ()

    def __post_init__(self) -> None:
        if not self.reasons:
            raise ValueError("QualityGate 结果必须包含原因")


class ReviewQualityGate:
    """把模型审稿报告转换为可复现的 ACCEPT/REVISE/REJECT。"""

    def __init__(self, policy: ReviewQualityGatePolicy | None = None) -> None:
        self.policy = policy or ReviewQualityGatePolicy()

    def evaluate(
        self,
        *,
        review: ReviewReport,
        actual_words: int,
        target_words: int,
        revision_round: int,
        previous_review: ReviewReport | None = None,
    ) -> ReviewGateResult:
        if actual_words <= 0 or target_words <= 0:
            raise ValueError("actual_words 和 target_words 必须大于 0")
        if revision_round < 0:
            raise ValueError("revision_round 不能小于 0")

        if review.parse_failed:
            return ReviewGateResult(
                decision=ReviewDecision.REJECT,
                reasons=("最终审稿结果解析失败，无法生成可靠修订指令",),
            )

        actionable: list[ReviewIssue] = []
        reasons: list[str] = []
        observations: list[str] = []
        critical_issues = tuple(
            issue for issue in review.issues if issue.severity == "critical"
        )
        if critical_issues:
            actionable.extend(critical_issues)
            reasons.append(f"存在 {len(critical_issues)} 个 critical 问题")

        blocking_warnings = tuple(
            issue
            for issue in review.issues
            if issue.severity == "warning"
            and issue.category in self.policy.blocking_warning_categories
        )
        if blocking_warnings:
            actionable.extend(blocking_warnings)
            categories = "、".join(
                sorted({issue.category for issue in blocking_warnings})
            )
            reasons.append(f"存在阻断类别 warning：{categories}")

        warnings = tuple(issue for issue in review.issues if issue.severity == "warning")
        if len(warnings) >= self.policy.warning_count_threshold:
            observations.append(
                f"普通 warning 数量 {len(warnings)} 达到观察阈值 "
                f"{self.policy.warning_count_threshold}，未自动触发修订"
            )

        if review.score is not None and review.score < self.policy.minimum_score:
            observations.append(
                f"审稿分数 {review.score} 低于阈值 {self.policy.minimum_score}"
            )

        length_ratio = actual_words / target_words
        if length_ratio < self.policy.minimum_target_ratio:
            reasons.append(
                f"正文长度比例 {length_ratio:.2f} 低于 "
                f"{self.policy.minimum_target_ratio:.2f}"
            )
            actionable.append(
                ReviewIssue(
                    category="structure",
                    severity="warning",
                    description=(
                        f"正文实际 {actual_words} 字，低于目标 {target_words} 字的"
                        f" {self.policy.minimum_target_ratio:.0%}。"
                    ),
                    suggestion="补足必要场景、动作和因果展开，不新增计划外关键事实。",
                    related_source_ids=("book-constraints",),
                )
            )
        elif length_ratio > self.policy.maximum_target_ratio:
            reasons.append(
                f"正文长度比例 {length_ratio:.2f} 高于 "
                f"{self.policy.maximum_target_ratio:.2f}"
            )
            actionable.append(
                ReviewIssue(
                    category="structure",
                    severity="warning",
                    description=(
                        f"正文实际 {actual_words} 字，高于目标 {target_words} 字的"
                        f" {self.policy.maximum_target_ratio:.0%}。"
                    ),
                    suggestion="精简重复描写和非必要过场，保留计划要求的剧情节拍。",
                    related_source_ids=("book-constraints",),
                )
            )
        elif length_ratio > self.policy.soft_maximum_target_ratio:
            observations.append(
                f"正文长度比例 {length_ratio:.2f} 高于建议值 "
                f"{self.policy.soft_maximum_target_ratio:.2f}，但未超过硬上限"
            )

        actionable_issues = self._unique_issues(actionable)
        if not reasons:
            return ReviewGateResult(
                decision=ReviewDecision.ACCEPT,
                reasons=(
                    "未发现需要自动修订的硬问题",
                    *observations,
                ),
            )

        if previous_review is not None:
            previous_keys = {
                self.issue_key(issue)
                for issue in previous_review.issues
                if self._is_blocking_issue(issue)
            }
            repeated = tuple(
                issue
                for issue in actionable_issues
                if self.issue_key(issue) in previous_keys
                and self._is_blocking_issue(issue)
            )
            if repeated:
                descriptions = "；".join(issue.description for issue in repeated)
                return ReviewGateResult(
                    decision=ReviewDecision.REJECT,
                    reasons=(
                        "相同 critical 或阻断 warning 连续两轮仍存在："
                        f"{descriptions}",
                    ),
                    actionable_issues=actionable_issues,
                )

        if revision_round >= self.policy.max_revision_rounds:
            return ReviewGateResult(
                decision=ReviewDecision.REJECT,
                reasons=(
                    *tuple(reasons),
                    f"已达到最大修订轮数 {self.policy.max_revision_rounds}",
                ),
                actionable_issues=actionable_issues,
            )

        return ReviewGateResult(
            decision=ReviewDecision.REVISE,
            reasons=tuple(reasons),
            actionable_issues=actionable_issues,
        )

    def _is_blocking_issue(self, issue: ReviewIssue) -> bool:
        return issue.severity == "critical" or (
            issue.severity == "warning"
            and issue.category in self.policy.blocking_warning_categories
        )

    @staticmethod
    def issue_key(issue: ReviewIssue) -> tuple[str, tuple[str, ...], str]:
        """生成跨轮次稳定的问题标识，优先使用来源 ID。"""

        normalized_description = re.sub(r"\W+", "", issue.description.casefold())
        description_key = normalized_description[:120]
        return issue.category, tuple(sorted(issue.related_source_ids)), description_key

    @classmethod
    def _unique_issues(cls, issues: list[ReviewIssue]) -> tuple[ReviewIssue, ...]:
        selected: dict[tuple[str, tuple[str, ...], str], ReviewIssue] = {}
        for issue in issues:
            selected.setdefault(cls.issue_key(issue), issue)
        return tuple(selected.values())
