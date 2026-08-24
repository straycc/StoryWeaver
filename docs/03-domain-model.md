# StoryWeaver 领域与数据模型

文档状态：MVP 数据契约  
最后更新：2026-08-14

## 1. 建模原则

- 使用稳定 ID，不依赖名称作为唯一标识。
- LLM 输入输出使用结构化模型并执行严格校验。
- 权威状态、历史记录和检索索引分离。
- 章节正文确定后，才能提取和提交状态变化。
- 所有状态变化都具有来源章节。
- 持久化模型包含 `schema_version`，便于后续迁移。

代码阶段优先使用 Python `dataclass` 和已有模型转换能力；是否引入 Pydantic 在真正需要复杂校验时再单独决定，不在需求文档中修改依赖。

## 2. 创建小说模型

```python
@dataclass(frozen=True, slots=True)
class CreateNovelRequest:
    title: str
    genre: str
    premise: str
    protagonist: str
    central_conflict: str
    tone: str
    target_chapters: int
    chapter_target_words: int
    language: str = "zh"
```

```python
@dataclass(frozen=True, slots=True)
class CharacterProfile:
    character_id: str
    name: str
    role: str
    personality: tuple[str, ...]
    motivation: str
    long_term_goal: str
    conflict: str
    speech_style: str
    knowledge_boundaries: tuple[str, ...]
```

```python
@dataclass(frozen=True, slots=True)
class OutlineNode:
    node_id: str
    title: str
    chapter_start: int
    chapter_end: int
    goal: str
    expected_changes: tuple[str, ...]
```

```python
@dataclass(frozen=True, slots=True)
class StoryHook:
    hook_id: str
    description: str
    status: str              # open / progressing / resolved / deferred
    importance: int
    opened_chapter: int
    last_advanced_chapter: int
    expected_payoff: str
```

```python
@dataclass(frozen=True, slots=True)
class NovelFoundation:
    premise: str
    world_setting: str
    central_conflict: str
    ending_direction: str
    characters: tuple[CharacterProfile, ...]
    outline: tuple[OutlineNode, ...]
    writing_rules: tuple[str, ...]
    initial_hooks: tuple[StoryHook, ...]
```

## 3. 章节计划模型

```python
@dataclass(frozen=True, slots=True)
class ChapterPlan:
    chapter_number: int
    goal: str
    participating_character_ids: tuple[str, ...]
    location: str
    required_beats: tuple[str, ...]
    forbidden_events: tuple[str, ...]
    relevant_hook_ids: tuple[str, ...]
    ending_hook: str
    style_focus: tuple[str, ...] = ()
```

必须满足：

- `chapter_number` 等于项目下一章编号。
- 所有参与人物和伏笔 ID 可解析。
- `required_beats` 至少包含一项。
- `required_beats` 与 `forbidden_events` 不能明显冲突。

## 4. Context 模型

```python
@dataclass(frozen=True, slots=True)
class ChapterSummary:
    chapter_number: int
    summary: str
```

```python
@dataclass(frozen=True, slots=True)
class ContextEntry:
    source_id: str
    source_type: str
    content: str
    reason: str
    protected: bool
    priority: int
```

```python
@dataclass(frozen=True, slots=True)
class ChapterContext:
    chapter_number: int
    entries: tuple[ContextEntry, ...]
    estimated_tokens: int
```

```python
@dataclass(frozen=True, slots=True)
class ContextTrace:
    chapter_number: int
    selected_source_ids: tuple[str, ...]
    excluded_source_ids: tuple[str, ...]
    protected_source_ids: tuple[str, ...]
    budget: int
    notes: tuple[str, ...]
```

`ContextTrace` 用于调试和评测，不向模型暴露系统内部推理过程。

## 5. 章节正文和审查模型

```python
@dataclass(frozen=True, slots=True)
class ChapterDraft:
    chapter_number: int
    title: str
    content: str
    word_count: int
```

`word_count` 是确定性派生值，不采用模型自报结果。第一版按一个 CJK 字符或一个英文/数字词计为一个文本单位，合格范围为项目每章目标字数的 60%～140%。标题不得包含换行，正文必须包含可计数文本。

```python
@dataclass(frozen=True, slots=True)
class ReviewIssue:
    category: str
    severity: str             # info / warning / critical
    description: str
    suggestion: str
    related_source_ids: tuple[str, ...] = ()
```

```python
@dataclass(frozen=True, slots=True)
class ReviewReport:
    passed: bool
    summary: str
    issues: tuple[ReviewIssue, ...]
    score: int | None = None
    parse_failed: bool = False
```

第一版审查类别至少包括：

- `plan_following`
- `character_consistency`
- `knowledge_boundary`
- `world_continuity`
- `hook_consistency`
- `structure`
- `style`

## 6. 状态模型

```python
@dataclass(frozen=True, slots=True)
class CharacterState:
    character_id: str
    location: str
    status: str
    current_goal: str
    emotion: str
    possessions: tuple[str, ...]
    known_fact_ids: tuple[str, ...]
```

```python
@dataclass(frozen=True, slots=True)
class FactRecord:
    fact_id: str
    subject_id: str
    predicate: str
    value: str
    valid_from_chapter: int
    valid_until_chapter: int | None
    source_chapter: int
    importance: int
```

```python
@dataclass(frozen=True, slots=True)
class StoryState:
    schema_version: int
    book_id: str
    last_committed_chapter: int
    current_time: str
    current_location: str
    characters: tuple[CharacterState, ...]
    facts: tuple[FactRecord, ...]
    hooks: tuple[StoryHook, ...]
```

## 7. 状态增量模型

```python
@dataclass(frozen=True, slots=True)
class CharacterStateUpdate:
    character_id: str
    location: str | None = None
    status: str | None = None
    current_goal: str | None = None
    emotion: str | None = None
    add_possessions: tuple[str, ...] = ()
    remove_possessions: tuple[str, ...] = ()
    learned_fact_ids: tuple[str, ...] = ()
```

```python
@dataclass(frozen=True, slots=True)
class HookUpdate:
    hook_id: str
    status: str
    note: str
```

```python
@dataclass(frozen=True, slots=True)
class StoryStateDelta:
    source_chapter: int
    chapter_summary: str
    character_updates: tuple[CharacterStateUpdate, ...]
    new_facts: tuple[FactRecord, ...]
    invalidated_fact_ids: tuple[str, ...]
    new_hooks: tuple[StoryHook, ...]
    hook_updates: tuple[HookUpdate, ...]
    new_time: str | None = None
    new_location: str | None = None
```

StateReducer 的最低不变量：

- `source_chapter == state.last_committed_chapter + 1`。
- `FactRecord.source_chapter == source_chapter`。
- 不存在的角色不能被静默更新。
- 同一角色不能同时新增和移除同一物品。
- 已失效事实不能继续作为当前事实返回。
- 已解决伏笔不能无理由回退。
- 规范化后的重复事实只保留一条当前记录。

## 8. 章节结果模型

```python
@dataclass(frozen=True, slots=True)
class ChapterResult:
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
    committed: bool
    candidate_id: str | None
```

建议状态：

```text
planning
writing
reviewing
revising
settling
ready_for_review
review_warning
failed
```

只有 `ready_for_review` 和允许提交的 `review_warning` 可以成为已提交章节。
`draft_rejected` 表示 strict 审稿门禁拒绝的候选正文：必须满足
`committed=false`、`state_delta=null` 且包含 `candidate_id`，不得推进
`last_committed_chapter`。

## 9. 导入模型

```python
@dataclass(frozen=True, slots=True)
class ImportedChapter:
    source_index: int
    title: str
    content: str
```

```python
@dataclass(frozen=True, slots=True)
class ImportProgress:
    book_id: str
    total_chapters: int
    last_completed_chapter: int
    status: str
    error: str | None = None
```

## 10. 模拟模型

```python
@dataclass(frozen=True, slots=True)
class SimulationSession:
    simulation_id: str
    source_book_id: str
    source_chapter: int
    current_turn: int
    state: StoryState
    status: str
```

```python
@dataclass(frozen=True, slots=True)
class SimulationTurn:
    turn: int
    user_input: str
    interpreted_action: str
    scene_text: str
    state_delta: StoryStateDelta
```

模拟状态增量需要使用独立的 `source_turn` 语义；代码实现时可以定义专用 `SimulationStateDelta`，避免滥用章节号。

## 11. 持久化结构

```text
books/<book_id>/
├── book.json
├── foundation.json
├── story_state.json
├── chapter_index.json
├── facts.jsonl
├── chapters/
│   ├── 0001-title.md
│   └── 0002-title.md
├── plans/
│   ├── 0001.json
│   └── 0002.json
├── reviews/
│   ├── 0001-initial.json
│   ├── 0001-final.json
│   └── 0001-revised.md
├── deltas/
│   ├── 0001.json
│   └── 0002.json
├── summaries/
│   └── chapters.jsonl
├── traces/
│   └── 0001-context.json
├── snapshots/
│   ├── 0000.json
│   └── 0001.json
├── history/
│   └── rewrites/<rewrite_id>/
│       ├── rewrite.json
│       ├── authority/
│       └── artifacts/
└── simulations/
    └── <simulation_id>/
```

第一版采用 JSON、JSONL 和 Markdown，原因是便于人工查看、测试和版本迁移。SQLite 和向量检索不是 MVP 前置条件。

## 12. 序列化约定

- 所有 JSON 使用 UTF-8。
- 日期时间采用 ISO 8601 UTC 字符串。
- 文件中的枚举使用小写蛇形命名。
- 可选值使用 `null`，不使用空字符串代替缺失。
- 元组序列化为 JSON 数组。
- 未知字段的兼容策略由模型版本决定；权威状态默认拒绝未知字段。
