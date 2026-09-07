"""质量评测的数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..llm import LlmUsage
from ..novel_creation.models import CreateNovelRequest


EVALUATION_GROUPS = frozenset({"bare", "storyweaver"})


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是非空字符串")


def _require_text_tuple(value: tuple[str, ...], field_name: str) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} 必须是 tuple")
    for item in value:
        _require_text(item, field_name)
    if len(value) != len(set(value)):
        raise ValueError(f"{field_name} 不能包含重复值")


@dataclass(frozen=True, slots=True)
class EvaluationExperiment:
    """一次评测数据集声明的固定运行参数。"""

    title: str
    temperature: float = 0.6
    repetitions: int = 1
    skill_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.title, "experiment.title")
        if not 0 <= self.temperature <= 2:
            raise ValueError("experiment.temperature 必须在 0 到 2 之间")
        if self.repetitions <= 0:
            raise ValueError("experiment.repetitions 必须大于 0")
        _require_text_tuple(self.skill_ids, "experiment.skill_ids")


@dataclass(frozen=True, slots=True)
class EvaluationCharacter:
    """两组生成器都能看到的固定人物资料。"""

    character_id: str
    name: str
    description: str

    def __post_init__(self) -> None:
        _require_text(self.character_id, "character.id")
        _require_text(self.name, "character.name")
        _require_text(self.description, "character.description")


@dataclass(frozen=True, slots=True)
class EvaluationCanonFact:
    """两组生成器都能看到的初始正史事实。"""

    fact_id: str
    content: str

    def __post_init__(self) -> None:
        _require_text(self.fact_id, "canon.id")
        _require_text(self.content, "canon.content")


@dataclass(frozen=True, slots=True)
class EvaluationChapterInput:
    """本章生成时允许暴露给两组模型的输入。"""

    objective: str
    required_beats: tuple[str, ...]
    constraints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.objective, "chapter.input.objective")
        _require_text_tuple(self.required_beats, "chapter.input.required_beats")
        if not self.required_beats:
            raise ValueError("chapter.input.required_beats 至少包含一项")
        _require_text_tuple(self.constraints, "chapter.input.constraints")

    def render(self) -> str:
        beats = "\n".join(f"{index}. {item}" for index, item in enumerate(self.required_beats, 1))
        constraints = (
            "\n".join(f"- {item}" for item in self.constraints)
            if self.constraints
            else "- 无额外限制"
        )
        return (
            f"本章目标：\n{self.objective}\n\n"
            f"必须完成的情节点：\n{beats}\n\n"
            f"本章限制：\n{constraints}"
        )


@dataclass(frozen=True, slots=True)
class EvaluationStateUpdate:
    """供评分器检查的新旧状态变化，不作为额外生成提示。"""

    subject: str
    field: str
    old_value: str
    new_value: str

    def __post_init__(self) -> None:
        for field_name in ("subject", "field", "old_value", "new_value"):
            _require_text(getattr(self, field_name), f"state_update.{field_name}")


@dataclass(frozen=True, slots=True)
class EvaluationExpectations:
    """只提供给评分器的隐藏检查标准。"""

    required_facts: tuple[str, ...] = ()
    forbidden_events: tuple[str, ...] = ()
    continuity_sources: tuple[str, ...] = ()
    focus: tuple[str, ...] = ()
    state_updates: tuple[EvaluationStateUpdate, ...] = ()

    def __post_init__(self) -> None:
        _require_text_tuple(self.required_facts, "expectations.required_facts")
        _require_text_tuple(self.forbidden_events, "expectations.forbidden_events")
        _require_text_tuple(self.continuity_sources, "expectations.continuity_sources")
        _require_text_tuple(self.focus, "expectations.focus")
        if not isinstance(self.state_updates, tuple):
            raise TypeError("expectations.state_updates 必须是 tuple")


@dataclass(frozen=True, slots=True)
class EvaluationChapterSpec:
    """一章的公开任务与隐藏评测标准。"""

    number: int
    title: str
    input: EvaluationChapterInput
    expectations: EvaluationExpectations = field(default_factory=EvaluationExpectations)

    def __post_init__(self) -> None:
        if self.number <= 0:
            raise ValueError("chapter.number 必须大于 0")
        _require_text(self.title, "chapter.title")


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    """一个可重复运行的固定创作简报。"""

    case_id: str
    description: str
    request: CreateNovelRequest
    experiment: EvaluationExperiment = field(
        default_factory=lambda: EvaluationExperiment(title="小说创作 A/B 评测")
    )
    characters: tuple[EvaluationCharacter, ...] = ()
    initial_canon: tuple[EvaluationCanonFact, ...] = ()
    chapters: tuple[EvaluationChapterSpec, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        _require_text(self.description, "description")
        if not isinstance(self.characters, tuple):
            raise TypeError("characters 必须是 tuple")
        if not isinstance(self.initial_canon, tuple):
            raise TypeError("initial_canon 必须是 tuple")
        if not isinstance(self.chapters, tuple):
            raise TypeError("chapters 必须是 tuple")
        character_ids = tuple(item.character_id for item in self.characters)
        canon_ids = tuple(item.fact_id for item in self.initial_canon)
        if len(character_ids) != len(set(character_ids)):
            raise ValueError("character.id 不能重复")
        if len(canon_ids) != len(set(canon_ids)):
            raise ValueError("canon.id 不能重复")
        if self.chapters:
            numbers = tuple(item.number for item in self.chapters)
            expected = tuple(range(1, len(self.chapters) + 1))
            if numbers != expected:
                raise ValueError("chapters 必须从 1 开始连续排列")
            if len(self.chapters) != self.request.target_chapters:
                raise ValueError("chapters 数量必须等于 request.target_chapters")
            known_sources = set(canon_ids)
            for chapter in self.chapters:
                unknown = set(chapter.expectations.continuity_sources) - known_sources
                if unknown:
                    raise ValueError(
                        f"第 {chapter.number} 章引用未知 continuity_sources："
                        + "、".join(sorted(unknown))
                    )

    def chapter(self, chapter_number: int) -> EvaluationChapterSpec | None:
        """返回固定章节规格；旧内置 Case 没有逐章规格时保持兼容。"""

        if not self.chapters:
            return None
        if chapter_number <= 0 or chapter_number > len(self.chapters):
            raise ValueError(f"章节号超出评测 Case：{chapter_number}")
        return self.chapters[chapter_number - 1]

    def render_shared_brief(self) -> str:
        """生成两组都能看到的同源作品资料。"""

        parts = [
            f"书名：{self.request.title}",
            f"题材：{self.request.genre}",
            f"故事前提：{self.request.premise}",
            f"主角：{self.request.protagonist}",
            f"核心冲突：{self.request.central_conflict}",
            f"叙事基调：{self.request.tone}",
            f"目标总章数：{self.request.target_chapters}",
        ]
        supplemental = self.render_supplemental_brief()
        if supplemental:
            parts.append(supplemental)
        return "\n\n".join(parts)

    def render_supplemental_brief(self) -> str:
        """返回 CreateNovelRequest 基础字段之外的固定资料。"""

        parts: list[str] = []
        if self.characters:
            parts.append(
                "人物资料：\n"
                + "\n".join(
                    f"- {item.name}（{item.character_id}）：{item.description}"
                    for item in self.characters
                )
            )
        if self.initial_canon:
            parts.append(
                "初始正史：\n"
                + "\n".join(
                    f"- [{item.fact_id}] {item.content}" for item in self.initial_canon
                )
            )
        if self.chapters:
            parts.append(
                "全书章节框架：\n"
                + "\n".join(
                    f"- 第 {item.number} 章《{item.title}》：{item.input.objective}"
                    for item in self.chapters
                )
            )
        return "\n\n".join(parts)


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
    setup_model_calls: int = 0
    setup_failed_model_calls: int = 0
    setup_retry_count: int = 0
    setup_input_tokens: int = 0
    setup_output_tokens: int = 0
    setup_elapsed_seconds: float = 0.0
    setup_error: str | None = None

    def __post_init__(self) -> None:
        if self.group not in EVALUATION_GROUPS:
            raise ValueError(f"不支持的评测组：{self.group}")
        if len(self.chapters) > len(self.metrics):
            raise ValueError("章节正文数量不能超过指标数量")
        for field_name in (
            "setup_model_calls",
            "setup_failed_model_calls",
            "setup_retry_count",
            "setup_input_tokens",
            "setup_output_tokens",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} 必须是非负整数")
        if self.setup_elapsed_seconds < 0:
            raise ValueError("setup_elapsed_seconds 不能小于 0")
        if self.setup_failed_model_calls > self.setup_model_calls:
            raise ValueError("setup_failed_model_calls 不能超过 setup_model_calls")
        if self.setup_error is not None and not self.setup_error.strip():
            raise ValueError("setup_error 必须为非空字符串或 None")


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
