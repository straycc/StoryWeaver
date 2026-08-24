"""StoryWeaver 小说创作质量评测。"""

from .cases import EVALUATION_CASES, get_evaluation_case
from .models import (
    BareChapterOutput,
    EvaluationCase,
    EvaluationRunResult,
    GroupRunResult,
    QualityChapterMetrics,
)
from .runner import BareNovelWriter, EvaluationRunner

__all__ = [
    "BareChapterOutput",
    "BareNovelWriter",
    "EVALUATION_CASES",
    "EvaluationCase",
    "EvaluationRunResult",
    "EvaluationRunner",
    "GroupRunResult",
    "QualityChapterMetrics",
    "get_evaluation_case",
]
