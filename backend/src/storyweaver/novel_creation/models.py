"""小说创作主线的领域数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field, fields


HOOK_STATUSES = frozenset({"open", "progressing", "resolved", "deferred"})
CHAPTER_STATUSES = frozenset({"ready_for_review", "review_warning"})
CHAPTER_RESULT_STATUSES = CHAPTER_STATUSES | frozenset({"draft_rejected"})
CHAPTER_CANDIDATE_STATUSES = frozenset({"review_rejected"})
REVIEW_CATEGORIES = frozenset(
    {
        "plan_following",
        "character_consistency",
        "knowledge_boundary",
        "world_continuity",
        "hook_consistency",
        "structure",
        "style",
    }
)
REVIEW_SEVERITIES = frozenset({"info", "warning", "critical"})


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是非空字符串")


def _require_string_tuple(
    value: tuple[str, ...],
    field_name: str,
    *,
    allow_empty: bool = True,
) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} 必须是 tuple")
    if not allow_empty and not value:
        raise ValueError(f"{field_name} 至少包含一项")
    for item in value:
        _require_text(item, field_name)


def _require_unique(values: tuple[str, ...], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} 不能包含重复值")


@dataclass(frozen=True, slots=True)
class CreateNovelRequest:
    """用户创建小说时提交的创作简报。"""

    title: str
    genre: str
    premise: str
    protagonist: str
    central_conflict: str
    tone: str
    target_chapters: int
    chapter_target_words: int
    language: str = "zh"

    def __post_init__(self) -> None:
        for field_name in (
            "title",
            "genre",
            "premise",
            "protagonist",
            "central_conflict",
            "tone",
            "language",
        ):
            _require_text(getattr(self, field_name), field_name)
        if self.target_chapters <= 0:
            raise ValueError("target_chapters 必须大于 0")
        if self.chapter_target_words <= 0:
            raise ValueError("chapter_target_words 必须大于 0")


@dataclass(frozen=True, slots=True)
class BatchPlanningContext:
    """连续创作时传给 Planner 的阶段边界，不属于持久化正史。"""

    start_chapter: int
    end_chapter: int
    current_chapter: int
    overall_instruction: str | None = None

    def __post_init__(self) -> None:
        if self.start_chapter <= 0 or self.end_chapter < self.start_chapter:
            raise ValueError("批次章节范围无效")
        if not self.start_chapter <= self.current_chapter <= self.end_chapter:
            raise ValueError("current_chapter 必须位于批次章节范围内")
        if self.overall_instruction is not None and not self.overall_instruction.strip():
            raise ValueError("overall_instruction 必须为非空字符串或 None")

    @property
    def is_final_chapter(self) -> bool:
        return self.current_chapter == self.end_chapter

    @property
    def remaining_chapters(self) -> int:
        return self.end_chapter - self.current_chapter


@dataclass(frozen=True, slots=True)
class BookMetadata:
    """项目级元数据，不包含由模型生成的小说设定。"""

    schema_version: int
    book_id: str
    title: str
    genre: str
    target_chapters: int
    chapter_target_words: int
    language: str
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if self.schema_version <= 0:
            raise ValueError("schema_version 必须大于 0")
        for field_name in (
            "book_id",
            "title",
            "genre",
            "language",
            "created_at",
            "updated_at",
        ):
            _require_text(getattr(self, field_name), field_name)
        if self.target_chapters <= 0:
            raise ValueError("target_chapters 必须大于 0")
        if self.chapter_target_words <= 0:
            raise ValueError("chapter_target_words 必须大于 0")


@dataclass(frozen=True, slots=True)
class CharacterProfile:
    """角色的长期稳定设定。"""

    character_id: str
    name: str
    role: str
    personality: tuple[str, ...]
    motivation: str
    long_term_goal: str
    conflict: str
    speech_style: str
    knowledge_boundaries: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "character_id",
            "name",
            "role",
            "motivation",
            "long_term_goal",
            "conflict",
            "speech_style",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_string_tuple(self.personality, "personality", allow_empty=False)
        _require_string_tuple(self.knowledge_boundaries, "knowledge_boundaries")


@dataclass(frozen=True, slots=True)
class OutlineNode:
    """小说大纲中的一个阶段，而不是单章正文。"""

    node_id: str
    title: str
    chapter_start: int
    chapter_end: int
    goal: str
    expected_changes: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in ("node_id", "title", "goal"):
            _require_text(getattr(self, field_name), field_name)
        if self.chapter_start <= 0:
            raise ValueError("chapter_start 必须大于 0")
        if self.chapter_end < self.chapter_start:
            raise ValueError("chapter_end 不能小于 chapter_start")
        _require_string_tuple(self.expected_changes, "expected_changes")


@dataclass(frozen=True, slots=True)
class StoryHook:
    """需要跨章节推进或回收的伏笔。"""

    hook_id: str
    description: str
    status: str
    importance: int
    opened_chapter: int
    last_advanced_chapter: int
    expected_payoff: str
    # 面向作者展示的名称；hook_id 仍仅作为内部稳定标识。
    name: str = ""

    def __post_init__(self) -> None:
        for field_name in ("hook_id", "description", "expected_payoff"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.name, str):
            raise TypeError("name 必须是字符串")
        if self.status not in HOOK_STATUSES:
            raise ValueError(f"不支持的伏笔状态：{self.status}")
        if not 1 <= self.importance <= 5:
            raise ValueError("importance 必须在 1 到 5 之间")
        if self.opened_chapter < 0:
            raise ValueError("opened_chapter 不能小于 0")
        if self.last_advanced_chapter < self.opened_chapter:
            raise ValueError("last_advanced_chapter 不能早于 opened_chapter")

    @property
    def display_name(self) -> str:
        """返回 UI 使用的名称，兼容尚未命名的历史伏笔。"""
        return self.name.strip() or self.description


@dataclass(frozen=True, slots=True)
class HookPlan:
    """章节计划中对伏笔推进、回收和新增的明确意图。"""

    advance_hook_ids: tuple[str, ...] = ()
    resolve_hook_ids: tuple[str, ...] = ()
    new_hook_budget: int = 1

    def __post_init__(self) -> None:
        _require_string_tuple(self.advance_hook_ids, "advance_hook_ids")
        _require_string_tuple(self.resolve_hook_ids, "resolve_hook_ids")
        _require_unique(self.advance_hook_ids, "advance_hook_ids")
        _require_unique(self.resolve_hook_ids, "resolve_hook_ids")
        overlap = set(self.advance_hook_ids) & set(self.resolve_hook_ids)
        if overlap:
            names = ", ".join(sorted(overlap))
            raise ValueError(f"同一伏笔不能同时推进和回收：{names}")
        if self.new_hook_budget not in {0, 1}:
            raise ValueError("new_hook_budget 只能为 0 或 1")


@dataclass(frozen=True, slots=True)
class NovelFoundation:
    """Architect 生成并由用户审阅的小说基础资料。"""

    premise: str
    world_setting: str
    central_conflict: str
    ending_direction: str
    characters: tuple[CharacterProfile, ...]
    outline: tuple[OutlineNode, ...]
    writing_rules: tuple[str, ...]
    initial_hooks: tuple[StoryHook, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "premise",
            "world_setting",
            "central_conflict",
            "ending_direction",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.characters, tuple) or not self.characters:
            raise ValueError("characters 至少包含一个角色")
        if not isinstance(self.outline, tuple) or not self.outline:
            raise ValueError("outline 至少包含一个节点")
        _require_string_tuple(self.writing_rules, "writing_rules", allow_empty=False)
        if not isinstance(self.initial_hooks, tuple):
            raise TypeError("initial_hooks 必须是 tuple")
        _require_unique(
            tuple(character.character_id for character in self.characters),
            "character_id",
        )
        _require_unique(tuple(node.node_id for node in self.outline), "outline node_id")
        _require_unique(tuple(hook.hook_id for hook in self.initial_hooks), "hook_id")


@dataclass(frozen=True, slots=True)
class ChapterPlan:
    """写作 Worker 必须遵守的单章计划。"""

    chapter_number: int
    goal: str
    participating_character_ids: tuple[str, ...]
    location: str
    required_beats: tuple[str, ...]
    forbidden_events: tuple[str, ...]
    relevant_hook_ids: tuple[str, ...]
    ending_hook: str
    style_focus: tuple[str, ...] = ()
    hook_plan: HookPlan = field(default_factory=HookPlan)

    def __post_init__(self) -> None:
        if self.chapter_number <= 0:
            raise ValueError("chapter_number 必须大于 0")
        for field_name in ("goal", "location", "ending_hook"):
            _require_text(getattr(self, field_name), field_name)
        _require_string_tuple(
            self.participating_character_ids,
            "participating_character_ids",
            allow_empty=False,
        )
        _require_string_tuple(self.required_beats, "required_beats", allow_empty=False)
        _require_string_tuple(self.forbidden_events, "forbidden_events")
        _require_string_tuple(self.relevant_hook_ids, "relevant_hook_ids")
        _require_string_tuple(self.style_focus, "style_focus")
        _require_unique(self.participating_character_ids, "participating_character_ids")
        _require_unique(self.relevant_hook_ids, "relevant_hook_ids")


CHAPTER_PLAN_PROPOSAL_STATUSES = frozenset(
    {"pending", "confirmed", "cancelled", "expired"}
)


@dataclass(frozen=True, slots=True)
class ChapterPlanProposal:
    """等待用户确认的单章计划及其修订历史摘要。"""

    proposal_id: str
    book_id: str
    chapter_number: int
    base_chapter_number: int
    base_current_time: str
    version: int
    status: str
    plan: ChapterPlan
    user_instruction: str | None
    feedback_history: tuple[str, ...]
    selected_memory_ids: tuple[str, ...]
    selected_memory_descriptions: tuple[str, ...]
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        for field_name in (
            "proposal_id",
            "book_id",
            "base_current_time",
            "created_at",
            "updated_at",
        ):
            _require_text(getattr(self, field_name), field_name)
        if self.chapter_number <= 0:
            raise ValueError("chapter_number 必须大于 0")
        if self.base_chapter_number < 0:
            raise ValueError("base_chapter_number 不能小于 0")
        if self.chapter_number != self.base_chapter_number + 1:
            raise ValueError("候选计划章节号必须紧接作品基线章节")
        if self.plan.chapter_number != self.chapter_number:
            raise ValueError("候选计划与 ChapterPlan 章节号不一致")
        if self.version < 1:
            raise ValueError("候选计划 version 必须大于 0")
        if self.status not in CHAPTER_PLAN_PROPOSAL_STATUSES:
            raise ValueError(f"不支持的候选计划状态：{self.status}")
        if self.user_instruction is not None and not self.user_instruction.strip():
            raise ValueError("user_instruction 必须为非空字符串或 None")
        _require_string_tuple(self.feedback_history, "feedback_history")
        _require_string_tuple(self.selected_memory_ids, "selected_memory_ids")
        _require_string_tuple(
            self.selected_memory_descriptions,
            "selected_memory_descriptions",
        )
        if len(self.selected_memory_ids) != len(self.selected_memory_descriptions):
            raise ValueError("长期记忆 ID 与描述数量必须一致")
        _require_unique(self.selected_memory_ids, "selected_memory_ids")


@dataclass(frozen=True, slots=True)
class ChapterDraft:
    """一章确定版本的正文。"""

    chapter_number: int
    title: str
    content: str
    word_count: int

    def __post_init__(self) -> None:
        if self.chapter_number <= 0:
            raise ValueError("chapter_number 必须大于 0")
        _require_text(self.title, "title")
        _require_text(self.content, "content")
        if self.word_count <= 0:
            raise ValueError("word_count 必须大于 0")


@dataclass(frozen=True, slots=True)
class ReviewIssue:
    """Reviewer 发现的一项可定位问题。"""

    category: str
    severity: str
    description: str
    suggestion: str
    related_source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.category not in REVIEW_CATEGORIES:
            raise ValueError(f"不支持的审查类别：{self.category}")
        if self.severity not in REVIEW_SEVERITIES:
            raise ValueError(f"不支持的审查严重程度：{self.severity}")
        _require_text(self.description, "description")
        _require_text(self.suggestion, "suggestion")
        _require_string_tuple(self.related_source_ids, "related_source_ids")
        _require_unique(self.related_source_ids, "related_source_ids")


@dataclass(frozen=True, slots=True)
class ReviewReport:
    """一次章节审查的结构化结果。"""

    passed: bool
    summary: str
    issues: tuple[ReviewIssue, ...]
    score: int | None = None
    parse_failed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise TypeError("passed 必须是 bool")
        if not isinstance(self.parse_failed, bool):
            raise TypeError("parse_failed 必须是 bool")
        _require_text(self.summary, "summary")
        if not isinstance(self.issues, tuple):
            raise TypeError("issues 必须是 tuple")
        if any(not isinstance(issue, ReviewIssue) for issue in self.issues):
            raise TypeError("issues 只能包含 ReviewIssue")
        if self.score is not None:
            if not isinstance(self.score, int) or isinstance(self.score, bool):
                raise TypeError("score 必须是 int 或 None")
            if not 0 <= self.score <= 100:
                raise ValueError("score 必须在 0 到 100 之间或为 None")
        if self.parse_failed and self.passed:
            raise ValueError("解析失败的审查不能标记为 passed")
        if self.passed and any(issue.severity == "critical" for issue in self.issues):
            raise ValueError("包含 critical 问题的审查不能标记为 passed")


@dataclass(frozen=True, slots=True)
class ChapterSummary:
    """供后续章节检索使用的已提交章节摘要。"""

    chapter_number: int
    summary: str

    def __post_init__(self) -> None:
        if self.chapter_number <= 0:
            raise ValueError("chapter_number 必须大于 0")
        _require_text(self.summary, "summary")


@dataclass(frozen=True, slots=True)
class ContextEntry:
    """进入章节 Prompt 的一个可追踪信息来源。"""

    source_id: str
    source_type: str
    content: str
    reason: str
    protected: bool
    priority: int

    def __post_init__(self) -> None:
        for field_name in ("source_id", "source_type", "content", "reason"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.protected, bool):
            raise TypeError("protected 必须是 bool")
        if not 0 <= self.priority <= 100:
            raise ValueError("priority 必须在 0 到 100 之间")


@dataclass(frozen=True, slots=True)
class ChapterContext:
    """Writer 单章调用使用的有限上下文。"""

    chapter_number: int
    entries: tuple[ContextEntry, ...]
    estimated_tokens: int
    book_id: str | None = None

    def __post_init__(self) -> None:
        if self.chapter_number <= 0:
            raise ValueError("chapter_number 必须大于 0")
        if not isinstance(self.entries, tuple):
            raise TypeError("entries 必须是 tuple")
        if self.estimated_tokens < 0:
            raise ValueError("estimated_tokens 不能小于 0")
        if self.book_id is not None:
            _require_text(self.book_id, "book_id")
        _require_unique(
            tuple(entry.source_id for entry in self.entries),
            "entries.source_id",
        )


@dataclass(frozen=True, slots=True)
class ContextTrace:
    """记录 ContextBuilder 的选择结果，仅用于调试和评测。"""

    chapter_number: int
    selected_source_ids: tuple[str, ...]
    excluded_source_ids: tuple[str, ...]
    protected_source_ids: tuple[str, ...]
    budget: int
    notes: tuple[str, ...]
    # 旧已提交章节的 JSON 未含此字段，默认 0 以保证可反序列化。
    estimated_tokens: int = 0

    def __post_init__(self) -> None:
        if self.chapter_number <= 0:
            raise ValueError("chapter_number 必须大于 0")
        for field_name in (
            "selected_source_ids",
            "excluded_source_ids",
            "protected_source_ids",
            "notes",
        ):
            _require_string_tuple(getattr(self, field_name), field_name)
        if self.budget <= 0:
            raise ValueError("budget 必须大于 0")
        if self.estimated_tokens < 0:
            raise ValueError("estimated_tokens 不能小于 0")
        _require_unique(self.selected_source_ids, "selected_source_ids")
        _require_unique(self.excluded_source_ids, "excluded_source_ids")
        _require_unique(self.protected_source_ids, "protected_source_ids")
        if set(self.selected_source_ids) & set(self.excluded_source_ids):
            raise ValueError("同一来源不能同时被选择和排除")
        if not set(self.protected_source_ids) <= set(self.selected_source_ids):
            raise ValueError("protected_source_ids 必须是已选择来源的子集")


@dataclass(frozen=True, slots=True)
class CharacterState:
    """角色在最后一个已提交章节结束时的动态状态。"""

    character_id: str
    location: str
    status: str
    current_goal: str
    emotion: str
    possessions: tuple[str, ...]
    known_fact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "character_id",
            "location",
            "status",
            "current_goal",
            "emotion",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_string_tuple(self.possessions, "possessions")
        _require_string_tuple(self.known_fact_ids, "known_fact_ids")
        _require_unique(self.possessions, "possessions")
        _require_unique(self.known_fact_ids, "known_fact_ids")


@dataclass(frozen=True, slots=True)
class FactRecord:
    """具有生效区间和来源章节的结构化故事事实。"""

    fact_id: str
    subject_id: str
    predicate: str
    value: str
    valid_from_chapter: int
    valid_until_chapter: int | None
    source_chapter: int
    importance: int

    def __post_init__(self) -> None:
        for field_name in ("fact_id", "subject_id", "predicate", "value"):
            _require_text(getattr(self, field_name), field_name)
        if self.valid_from_chapter < 0 or self.source_chapter < 0:
            raise ValueError("事实章节号不能小于 0")
        if self.valid_until_chapter is not None:
            if self.valid_until_chapter < self.valid_from_chapter:
                raise ValueError("事实失效章节不能早于生效章节")
        if not 1 <= self.importance <= 5:
            raise ValueError("importance 必须在 1 到 5 之间")

    @property
    def is_current(self) -> bool:
        """该事实当前是否仍然有效。"""

        return self.valid_until_chapter is None


@dataclass(frozen=True, slots=True)
class StoryState:
    """小说项目唯一的权威当前状态。"""

    schema_version: int
    book_id: str
    last_committed_chapter: int
    current_time: str
    current_location: str
    characters: tuple[CharacterState, ...]
    facts: tuple[FactRecord, ...]
    hooks: tuple[StoryHook, ...]

    def __post_init__(self) -> None:
        if self.schema_version <= 0:
            raise ValueError("schema_version 必须大于 0")
        _require_text(self.book_id, "book_id")
        _require_text(self.current_time, "current_time")
        _require_text(self.current_location, "current_location")
        if self.last_committed_chapter < 0:
            raise ValueError("last_committed_chapter 不能小于 0")
        if not isinstance(self.characters, tuple):
            raise TypeError("characters 必须是 tuple")
        if not isinstance(self.facts, tuple):
            raise TypeError("facts 必须是 tuple")
        if not isinstance(self.hooks, tuple):
            raise TypeError("hooks 必须是 tuple")
        _require_unique(
            tuple(character.character_id for character in self.characters),
            "character_id",
        )
        _require_unique(tuple(fact.fact_id for fact in self.facts), "fact_id")
        _require_unique(tuple(hook.hook_id for hook in self.hooks), "hook_id")

    @property
    def current_facts(self) -> tuple[FactRecord, ...]:
        """返回尚未失效的事实，历史事实仍保留在 ``facts`` 中。"""

        return tuple(fact for fact in self.facts if fact.is_current)


@dataclass(frozen=True, slots=True)
class CharacterStateUpdate:
    """一章结束后对单个角色状态的候选修改。"""

    character_id: str
    location: str | None = None
    status: str | None = None
    current_goal: str | None = None
    emotion: str | None = None
    add_possessions: tuple[str, ...] = ()
    remove_possessions: tuple[str, ...] = ()
    learned_fact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.character_id, "character_id")
        for field_name in ("location", "status", "current_goal", "emotion"):
            value = getattr(self, field_name)
            if value is not None:
                _require_text(value, field_name)
        _require_string_tuple(self.add_possessions, "add_possessions")
        _require_string_tuple(self.remove_possessions, "remove_possessions")
        _require_string_tuple(self.learned_fact_ids, "learned_fact_ids")
        _require_unique(self.add_possessions, "add_possessions")
        _require_unique(self.remove_possessions, "remove_possessions")
        _require_unique(self.learned_fact_ids, "learned_fact_ids")


@dataclass(frozen=True, slots=True)
class HookUpdate:
    """伏笔状态推进记录。"""

    hook_id: str
    status: str
    note: str

    def __post_init__(self) -> None:
        _require_text(self.hook_id, "hook_id")
        _require_text(self.note, "note")
        if self.status not in HOOK_STATUSES:
            raise ValueError(f"不支持的伏笔状态：{self.status}")


@dataclass(frozen=True, slots=True)
class StoryStateDelta:
    """ChapterAnalyzer 产出的候选状态变化。"""

    source_chapter: int
    chapter_summary: str
    character_updates: tuple[CharacterStateUpdate, ...]
    new_facts: tuple[FactRecord, ...]
    invalidated_fact_ids: tuple[str, ...]
    new_hooks: tuple[StoryHook, ...]
    hook_updates: tuple[HookUpdate, ...]
    new_time: str | None = None
    new_location: str | None = None

    def __post_init__(self) -> None:
        if self.source_chapter <= 0:
            raise ValueError("source_chapter 必须大于 0")
        _require_text(self.chapter_summary, "chapter_summary")
        for field_name in (
            "character_updates",
            "new_facts",
            "invalidated_fact_ids",
            "new_hooks",
            "hook_updates",
        ):
            if not isinstance(getattr(self, field_name), tuple):
                raise TypeError(f"{field_name} 必须是 tuple")
        _require_string_tuple(self.invalidated_fact_ids, "invalidated_fact_ids")
        if self.new_time is not None:
            _require_text(self.new_time, "new_time")
        if self.new_location is not None:
            _require_text(self.new_location, "new_location")
        _require_unique(
            tuple(update.character_id for update in self.character_updates),
            "character_updates.character_id",
        )
        _require_unique(tuple(fact.fact_id for fact in self.new_facts), "new_facts.fact_id")
        _require_unique(self.invalidated_fact_ids, "invalidated_fact_ids")
        _require_unique(tuple(hook.hook_id for hook in self.new_hooks), "new_hooks.hook_id")
        _require_unique(
            tuple(update.hook_id for update in self.hook_updates),
            "hook_updates.hook_id",
        )


@dataclass(frozen=True, slots=True)
class ChapterCandidateMetadata:
    """未进入正史的章节候选产物索引。"""

    schema_version: int
    candidate_id: str
    proposal_id: str
    book_id: str
    chapter_number: int
    title: str
    word_count: int
    original_title: str
    original_word_count: int
    status: str
    reason: str
    revised: bool
    created_at: str

    def __post_init__(self) -> None:
        if self.schema_version <= 0:
            raise ValueError("schema_version 必须大于 0")
        for field_name in (
            "candidate_id",
            "proposal_id",
            "book_id",
            "title",
            "original_title",
            "reason",
            "created_at",
        ):
            _require_text(getattr(self, field_name), field_name)
        if self.chapter_number <= 0:
            raise ValueError("chapter_number 必须大于 0")
        if self.word_count <= 0:
            raise ValueError("word_count 必须大于 0")
        if self.original_word_count <= 0:
            raise ValueError("original_word_count 必须大于 0")
        if self.status not in CHAPTER_CANDIDATE_STATUSES:
            raise ValueError(f"不支持的候选草稿状态：{self.status}")
        if not isinstance(self.revised, bool):
            raise TypeError("revised 必须是 bool")


@dataclass(frozen=True, slots=True)
class ChapterResult:
    """完整写下一章 Pipeline 的返回结果。"""

    chapter_number: int
    plan: ChapterPlan
    draft: ChapterDraft
    final_draft: ChapterDraft
    initial_review: ReviewReport
    final_review: ReviewReport
    revised: bool
    state_delta: StoryStateDelta | None
    context_trace: ContextTrace
    status: str
    committed: bool = True
    candidate_id: str | None = None
    revision_count: int = 0
    draft_history: tuple[ChapterDraft, ...] = ()
    review_history: tuple[ReviewReport, ...] = ()

    def __post_init__(self) -> None:
        if self.chapter_number <= 0:
            raise ValueError("chapter_number 必须大于 0")
        chapter_numbers = {
            self.chapter_number,
            self.plan.chapter_number,
            self.draft.chapter_number,
            self.final_draft.chapter_number,
            self.context_trace.chapter_number,
        }
        if self.state_delta is not None:
            chapter_numbers.add(self.state_delta.source_chapter)
        if chapter_numbers != {self.chapter_number}:
            raise ValueError("ChapterResult 中的章节号必须一致")
        if not isinstance(self.revised, bool):
            raise TypeError("revised 必须是 bool")
        if self.revision_count < 0:
            raise ValueError("revision_count 不能小于 0")
        if not isinstance(self.draft_history, tuple):
            raise TypeError("draft_history 必须是 tuple")
        if not isinstance(self.review_history, tuple):
            raise TypeError("review_history 必须是 tuple")
        if any(not isinstance(item, ChapterDraft) for item in self.draft_history):
            raise TypeError("draft_history 只能包含 ChapterDraft")
        if any(not isinstance(item, ReviewReport) for item in self.review_history):
            raise TypeError("review_history 只能包含 ReviewReport")
        if bool(self.draft_history) != bool(self.review_history):
            raise ValueError("draft_history 和 review_history 必须同时提供")
        if self.draft_history:
            if len(self.draft_history) != len(self.review_history):
                raise ValueError("每一轮正文都必须对应一份审稿报告")
            if self.draft_history[0] != self.draft:
                raise ValueError("draft_history 第一项必须是初稿")
            if self.draft_history[-1] != self.final_draft:
                raise ValueError("draft_history 最后一项必须是最终稿")
            if self.review_history[0] != self.initial_review:
                raise ValueError("review_history 第一项必须是初审")
            if self.review_history[-1] != self.final_review:
                raise ValueError("review_history 最后一项必须是终审")
            if self.revision_count != len(self.draft_history) - 1:
                raise ValueError("revision_count 与 draft_history 不一致")
        if self.revised != (self.revision_count > 0):
            # 旧调用方尚未提供历史时，继续允许 revised=True 的兼容结果。
            if self.draft_history or not self.revised:
                raise ValueError("revised 与 revision_count 不一致")
        if not isinstance(self.initial_review, ReviewReport) or not isinstance(
            self.final_review,
            ReviewReport,
        ):
            raise TypeError("ChapterResult 审查字段必须是 ReviewReport")
        if self.status not in CHAPTER_RESULT_STATUSES:
            raise ValueError(f"不支持的章节结果状态：{self.status}")
        if not isinstance(self.committed, bool):
            raise TypeError("committed 必须是 bool")
        if self.committed:
            if self.state_delta is None:
                raise ValueError("已提交章节必须包含状态增量")
            if self.status not in CHAPTER_STATUSES:
                raise ValueError("已提交章节必须使用正式章节状态")
            if self.candidate_id is not None:
                raise ValueError("已提交章节不能携带 candidate_id")
        else:
            if self.status != "draft_rejected":
                raise ValueError("未提交章节必须标记为 draft_rejected")
            if self.state_delta is not None:
                raise ValueError("未提交章节不能携带状态增量")
            if self.candidate_id is None:
                raise ValueError("未提交章节必须携带 candidate_id")
            _require_text(self.candidate_id, "candidate_id")


@dataclass(frozen=True, slots=True)
class ChapterMetadata:
    """章节索引中的轻量记录。"""

    chapter_number: int
    title: str
    file_name: str
    word_count: int
    status: str
    created_at: str

    def __post_init__(self) -> None:
        if self.chapter_number <= 0:
            raise ValueError("chapter_number 必须大于 0")
        for field_name in ("title", "file_name", "created_at"):
            _require_text(getattr(self, field_name), field_name)
        if self.word_count <= 0:
            raise ValueError("word_count 必须大于 0")
        if self.status not in CHAPTER_STATUSES:
            raise ValueError(f"不支持的章节状态：{self.status}")


@dataclass(frozen=True, slots=True)
class ChapterRewriteRecord:
    """一次章节时间线回退的可审计记录。"""

    schema_version: int
    rewrite_id: str
    book_id: str
    chapter_number: int
    previous_last_chapter: int
    archived_chapter_numbers: tuple[int, ...]
    created_at: str

    def __post_init__(self) -> None:
        if self.schema_version <= 0:
            raise ValueError("schema_version 必须大于 0")
        for field_name in ("rewrite_id", "book_id", "created_at"):
            _require_text(getattr(self, field_name), field_name)
        if self.chapter_number <= 0:
            raise ValueError("chapter_number 必须大于 0")
        if self.previous_last_chapter < self.chapter_number:
            raise ValueError("previous_last_chapter 不能早于重写章节")
        if not isinstance(self.archived_chapter_numbers, tuple):
            raise TypeError("archived_chapter_numbers 必须是 tuple")
        expected = tuple(range(self.chapter_number, self.previous_last_chapter + 1))
        if self.archived_chapter_numbers != expected:
            raise ValueError("归档章节编号必须连续覆盖重写章节及其后续章节")


@dataclass(frozen=True, slots=True)
class NovelProject:
    """一次完整加载得到的小说项目聚合。"""

    metadata: BookMetadata
    foundation: NovelFoundation
    state: StoryState


PERSISTED_MODEL_TYPES: tuple[type[object], ...] = (
    BookMetadata,
    CharacterProfile,
    OutlineNode,
    StoryHook,
    HookPlan,
    NovelFoundation,
    ChapterPlan,
    ChapterPlanProposal,
    ChapterDraft,
    ReviewIssue,
    ReviewReport,
    ChapterSummary,
    ContextEntry,
    ChapterContext,
    ContextTrace,
    CharacterState,
    FactRecord,
    StoryState,
    CharacterStateUpdate,
    HookUpdate,
    StoryStateDelta,
    ChapterCandidateMetadata,
    ChapterResult,
    ChapterMetadata,
)


def persisted_field_names(model_type: type[object]) -> frozenset[str]:
    """返回模型允许持久化的字段，用于严格拒绝未知字段。"""

    if model_type not in PERSISTED_MODEL_TYPES:
        raise TypeError(f"未注册的持久化模型：{model_type.__name__}")
    return frozenset(field.name for field in fields(model_type))
