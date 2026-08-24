"""质量评测的数据契约。"""

from __future__ import annotations

from dataclasses import dataclass

from ..llm import LlmUsage
from ..novel_creation.models import CreateNovelRequest


EVALUATION_GROUPS = frozenset({"bare", "storyweaver"})


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是非空字符串")


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    """一个可重复运行的固定创作简报。"""

    case_id: str
    description: str
    request: CreateNovelRequest

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        _require_text(self.description, "description")


@dataclass(frozen=True, slots=True)
class BareChapterOutput:
    """裸模型单次章节调用的正文和原始指标。"""

    chapter_number: int
    content: str
    prompt: str
    usage: LlmUsage
    elapsed_seconds: float
    model_calls: int = 1
    failed_model_calls: int = 0
    retry_count: int = 0

    def __post_init__(self) -> None:
        if self.chapter_number <= 0:
            raise ValueError("chapter_number 必须大于 0")
        _require_text(self.content, "content")
        _require_text(self.prompt, "prompt")
        if self.elapsed_seconds < 0:
            raise ValueError("elapsed_seconds 不能小于 0")
        for field_name in ("model_calls", "failed_model_calls", "retry_count"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} 必须是非负整数")
        if self.model_calls < 1:
            raise ValueError("model_calls 必须大于 0")
        if self.failed_model_calls > self.model_calls:
            raise ValueError("failed_model_calls 不能超过 model_calls")
        if self.retry_count != self.model_calls - 1:
            raise ValueError("retry_count 必须等于 model_calls - 1")


@dataclass(frozen=True, slots=True)
class QualityChapterMetrics:
    """两组均可导出的单章客观运行指标。"""

    group: str
    chapter_number: int
    succeeded: bool
    committed: bool | None
    status: str
    model_calls: int
    failed_model_calls: int
    retry_count: int
    input_tokens: int
    output_tokens: int
    elapsed_seconds: float
    revised: bool | None = None
    initial_issue_count: int | None = None
    final_issue_count: int | None = None
    initial_critical_count: int | None = None
    final_critical_count: int | None = None
    candidate_id: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.group not in EVALUATION_GROUPS:
            raise ValueError(f"不支持的评测组：{self.group}")
        if self.chapter_number <= 0:
            raise ValueError("chapter_number 必须大于 0")
        if not isinstance(self.succeeded, bool):
            raise TypeError("succeeded 必须是 bool")
        if self.committed is not None and not isinstance(self.committed, bool):
            raise TypeError("committed 必须是 bool 或 None")
        _require_text(self.status, "status")
        for field_name in (
            "model_calls",
            "failed_model_calls",
            "retry_count",
            "input_tokens",
            "output_tokens",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} 必须是非负整数")
        if self.elapsed_seconds < 0:
            raise ValueError("elapsed_seconds 不能小于 0")
        if self.error is not None and not self.error.strip():
            raise ValueError("error 必须为非空字符串或 None")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class GroupRunResult:
    """一个案例中某个实验组的执行结果。"""

    group: str
    chapters: tuple[str, ...]
    metrics: tuple[QualityChapterMetrics, ...]

    def __post_init__(self) -> None:
        if self.group not in EVALUATION_GROUPS:
            raise ValueError(f"不支持的评测组：{self.group}")
        if len(self.chapters) > len(self.metrics):
            raise ValueError("章节正文数量不能超过指标数量")


@dataclass(frozen=True, slots=True)
class EvaluationRunResult:
    """单个固定案例的完整 A/B 结果。"""

    run_id: str
    case_id: str
    requested_chapters: int
    bare: GroupRunResult
    storyweaver: GroupRunResult
    output_directory: str

    def __post_init__(self) -> None:
        _require_text(self.run_id, "run_id")
        _require_text(self.case_id, "case_id")
        _require_text(self.output_directory, "output_directory")
        if self.requested_chapters <= 0:
            raise ValueError("requested_chapters 必须大于 0")
        if self.bare.group != "bare" or self.storyweaver.group != "storyweaver":
            raise ValueError("EvaluationRunResult 的实验组不正确")

    @property
    def status(self) -> str:
        """区分达到目标的完整结果和只生成部分章节的结果。"""

        bare_completed = (
            len(self.bare.chapters) == self.requested_chapters
            and len(self.bare.metrics) == self.requested_chapters
            and all(item.succeeded for item in self.bare.metrics)
        )
        storyweaver_completed = (
            len(self.storyweaver.chapters) == self.requested_chapters
            and len(self.storyweaver.metrics) == self.requested_chapters
            and all(
                item.succeeded and item.committed is True
                for item in self.storyweaver.metrics
            )
        )
        return "completed" if bare_completed and storyweaver_completed else "partial"
