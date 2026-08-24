"""Review QualityGate 的确定性决策测试。"""

from __future__ import annotations

import unittest

from storyweaver.novel_creation import (
    ReviewDecision,
    ReviewIssue,
    ReviewQualityGate,
    ReviewQualityGatePolicy,
    ReviewReport,
)


PASSING_REVIEW = ReviewReport(
    passed=True,
    summary="正文符合要求",
    issues=(),
    score=92,
)
WORLD_WARNING = ReviewIssue(
    category="world_continuity",
    severity="warning",
    description="人物靠在墙上，但刻痕出现在炉门上。",
    suggestion="统一人物动作与刻痕位置。",
    related_source_ids=("plan:5",),
)


class ReviewQualityGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = ReviewQualityGate()

    def test_passing_review_inside_target_range_is_accepted(self) -> None:
        result = self.gate.evaluate(
            review=PASSING_REVIEW,
            actual_words=1080,
            target_words=1200,
            revision_round=0,
        )

        self.assertIs(result.decision, ReviewDecision.ACCEPT)

    def test_default_length_upper_bound_allows_twenty_percent_variation(self) -> None:
        result = self.gate.evaluate(
            review=PASSING_REVIEW,
            actual_words=1440,
            target_words=1200,
            revision_round=0,
        )

        self.assertIs(result.decision, ReviewDecision.ACCEPT)

    def test_default_length_soft_upper_bound_is_accepted_with_observation(self) -> None:
        result = self.gate.evaluate(
            review=PASSING_REVIEW,
            actual_words=1900,
            target_words=1200,
            revision_round=0,
        )

        self.assertIs(result.decision, ReviewDecision.ACCEPT)
        self.assertTrue(any("建议值" in reason for reason in result.reasons))

    def test_default_length_hard_upper_bound_requires_revision(self) -> None:
        result = self.gate.evaluate(
            review=PASSING_REVIEW,
            actual_words=2161,
            target_words=1200,
            revision_round=0,
        )

        self.assertIs(result.decision, ReviewDecision.REVISE)
        self.assertTrue(any(issue.category == "structure" for issue in result.actionable_issues))

    def test_default_gate_allows_moderate_score_and_length_deviation(self) -> None:
        result = self.gate.evaluate(
            review=ReviewReport(
                passed=True,
                summary="计划与连续性均通过",
                issues=(),
                score=85,
            ),
            actual_words=1501,
            target_words=1200,
            revision_round=0,
        )

        self.assertIs(result.decision, ReviewDecision.ACCEPT)

    def test_default_gate_accepts_configured_score_and_length_boundaries(self) -> None:
        result = self.gate.evaluate(
            review=ReviewReport(
                passed=True,
                summary="计划与连续性均通过",
                issues=(),
                score=80,
            ),
            actual_words=1900,
            target_words=1200,
            revision_round=0,
        )

        self.assertIs(result.decision, ReviewDecision.ACCEPT)
        self.assertTrue(any("建议值" in reason for reason in result.reasons))

    def test_default_gate_accepts_score_at_eighty(self) -> None:
        result = self.gate.evaluate(
            review=ReviewReport(
                passed=True,
                summary="满足基本质量要求",
                issues=(),
                score=80,
            ),
            actual_words=1080,
            target_words=1200,
            revision_round=0,
        )

        self.assertIs(result.decision, ReviewDecision.ACCEPT)

    def test_blocking_warning_and_low_score_trigger_revision(self) -> None:
        review = ReviewReport(
            passed=True,
            summary="存在空间连续性问题",
            issues=(WORLD_WARNING,),
            score=82,
        )

        result = self.gate.evaluate(
            review=review,
            actual_words=783,
            target_words=1200,
            revision_round=0,
        )

        self.assertIs(result.decision, ReviewDecision.REVISE)
        self.assertIn(WORLD_WARNING, result.actionable_issues)
        self.assertFalse(
            any(issue.category == "structure" for issue in result.actionable_issues)
        )

    def test_info_only_does_not_trigger_revision(self) -> None:
        review = ReviewReport(
            passed=True,
            summary="仅有可选润色",
            issues=(
                ReviewIssue(
                    category="style",
                    severity="info",
                    description="可以替换一处环境描写。",
                    suggestion="按需润色。",
                ),
            ),
            score=90,
        )

        result = self.gate.evaluate(
            review=review,
            actual_words=1125,
            target_words=1200,
            revision_round=0,
        )

        self.assertIs(result.decision, ReviewDecision.ACCEPT)

    def test_low_score_and_nonblocking_warnings_are_observations(self) -> None:
        review = ReviewReport(
            passed=False,
            summary="节奏仍可优化",
            issues=(
                ReviewIssue(
                    category="style",
                    severity="warning",
                    description="环境描写略重复。",
                    suggestion="后续按需压缩。",
                ),
                ReviewIssue(
                    category="structure",
                    severity="warning",
                    description="过场略长。",
                    suggestion="后续章节注意节奏。",
                ),
                ReviewIssue(
                    category="style",
                    severity="warning",
                    description="个别句式重复。",
                    suggestion="后续按需润色。",
                ),
            ),
            score=72,
        )

        result = self.gate.evaluate(
            review=review,
            actual_words=1080,
            target_words=1200,
            revision_round=0,
        )

        self.assertIs(result.decision, ReviewDecision.ACCEPT)
        self.assertEqual(result.actionable_issues, ())
        self.assertTrue(any("低于阈值" in reason for reason in result.reasons))

    def test_parse_failure_is_rejected_without_blind_revision(self) -> None:
        review = ReviewReport(
            passed=False,
            summary="审稿解析失败",
            issues=(),
            parse_failed=True,
        )

        result = self.gate.evaluate(
            review=review,
            actual_words=1080,
            target_words=1200,
            revision_round=0,
        )

        self.assertIs(result.decision, ReviewDecision.REJECT)
        self.assertEqual(result.actionable_issues, ())

    def test_same_blocking_issue_in_two_reviews_is_rejected(self) -> None:
        review = ReviewReport(
            passed=True,
            summary="空间问题仍存在",
            issues=(WORLD_WARNING,),
            score=90,
        )

        result = self.gate.evaluate(
            review=review,
            actual_words=1080,
            target_words=1200,
            revision_round=1,
            previous_review=review,
        )

        self.assertIs(result.decision, ReviewDecision.REJECT)
        self.assertIn("连续两轮", result.reasons[0])

    def test_max_revision_rounds_is_hard_limit(self) -> None:
        gate = ReviewQualityGate(
            ReviewQualityGatePolicy(max_revision_rounds=2)
        )
        review = ReviewReport(
            passed=False,
            summary="仍存在角色知识越界",
            issues=(
                ReviewIssue(
                    category="knowledge_boundary",
                    severity="critical",
                    description="角色使用了尚未获知的密令。",
                    suggestion="删除或补足获知密令的剧情。",
                ),
            ),
            score=79,
        )

        result = gate.evaluate(
            review=review,
            actual_words=1080,
            target_words=1200,
            revision_round=2,
        )

        self.assertIs(result.decision, ReviewDecision.REJECT)
        self.assertTrue(any("最大修订轮数" in reason for reason in result.reasons))


if __name__ == "__main__":
    unittest.main()
