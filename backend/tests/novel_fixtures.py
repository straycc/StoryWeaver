"""小说创作第一阶段测试使用的固定假数据。"""

from __future__ import annotations

from storyweaver.novel_creation.models import (
    BookMetadata,
    ChapterDraft,
    ChapterPlan,
    CharacterProfile,
    CharacterState,
    CharacterStateUpdate,
    CreateNovelRequest,
    FactRecord,
    HookUpdate,
    NovelFoundation,
    OutlineNode,
    StoryHook,
    StoryState,
    StoryStateDelta,
)


BOOK_ID = "rainy-hotel"


def create_novel_request() -> CreateNovelRequest:
    return CreateNovelRequest(
        title="雨夜旅馆",
        genre="悬疑",
        premise="年轻侦探林默进入废弃旅馆，调查十年前的失踪案。",
        protagonist="林默",
        central_conflict="林默寻找真相，神秘人试图把他引向错误线索。",
        tone="克制、压迫、有限视角",
        target_chapters=6,
        chapter_target_words=1200,
        language="zh",
    )


def create_metadata() -> BookMetadata:
    return BookMetadata(
        schema_version=1,
        book_id=BOOK_ID,
        title="雨夜旅馆",
        genre="悬疑",
        target_chapters=6,
        chapter_target_words=1200,
        language="zh",
        created_at="2026-08-13T00:00:00+00:00",
        updated_at="2026-08-13T00:00:00+00:00",
    )


def create_foundation() -> NovelFoundation:
    return NovelFoundation(
        premise="年轻侦探林默进入废弃旅馆调查十年前的失踪案。",
        world_setting="临海城郊外，暴雨切断了废弃旅馆与外界的联系。",
        central_conflict="林默寻找真相，神秘人试图把他引向错误线索。",
        ending_direction="林默识破误导，并发现失踪案与旅馆旧主人有关。",
        characters=(
            CharacterProfile(
                character_id="lin-mo",
                name="林默",
                role="主角 / 年轻侦探",
                personality=("谨慎", "执着"),
                motivation="查清十年前失踪案",
                long_term_goal="找到失踪者或确认其命运",
                conflict="既缺少证据，也无法信任旅馆中的陌生人",
                speech_style="简短、克制，习惯追问细节",
                knowledge_boundaries=("不知道地下室的真实用途",),
            ),
            CharacterProfile(
                character_id="stranger",
                name="神秘人",
                role="阻碍者 / 旅馆知情人",
                personality=("警惕", "善于误导"),
                motivation="阻止林默接近地下室",
                long_term_goal="隐藏自己与旧案的关系",
                conflict="必须引导林默离开，又不能暴露身份",
                speech_style="含混、低沉，避免直接回答",
                knowledge_boundaries=("知道地下室入口", "知道失踪案关键证据"),
            ),
        ),
        outline=(
            OutlineNode(
                node_id="arrival",
                title="进入旅馆",
                chapter_start=1,
                chapter_end=2,
                goal="林默进入旅馆并确认有人仍在活动",
                expected_changes=("获得第一条地下室线索",),
            ),
        ),
        writing_rules=("采用林默的有限视角", "线索必须通过行动或环境呈现"),
        initial_hooks=(
            StoryHook(
                hook_id="basement-door",
                description="地下室铁门被人为锁住",
                status="open",
                importance=5,
                opened_chapter=0,
                last_advanced_chapter=0,
                expected_payoff="揭示地下室与十年前失踪案的联系",
            ),
        ),
    )


def create_initial_state() -> StoryState:
    return StoryState(
        schema_version=1,
        book_id=BOOK_ID,
        last_committed_chapter=0,
        current_time="深夜十一点",
        current_location="废弃旅馆门外",
        characters=(
            CharacterState(
                character_id="lin-mo",
                location="废弃旅馆门外",
                status="正常",
                current_goal="进入旅馆寻找线索",
                emotion="警惕",
                possessions=("手电筒",),
                known_fact_ids=("hotel-abandoned",),
            ),
            CharacterState(
                character_id="stranger",
                location="旅馆二楼",
                status="隐藏身份",
                current_goal="阻止林默接近地下室",
                emotion="戒备",
                possessions=("旧钥匙串",),
                known_fact_ids=("hotel-abandoned",),
            ),
        ),
        facts=(
            FactRecord(
                fact_id="hotel-abandoned",
                subject_id="hotel",
                predicate="status",
                value="abandoned",
                valid_from_chapter=0,
                valid_until_chapter=None,
                source_chapter=0,
                importance=3,
            ),
        ),
        hooks=create_foundation().initial_hooks,
    )


def create_chapter_plan() -> ChapterPlan:
    return ChapterPlan(
        chapter_number=1,
        goal="林默进入旅馆并接触第一条地下室线索",
        participating_character_ids=("lin-mo", "stranger"),
        location="废弃旅馆大厅",
        required_beats=("林默进入大厅", "林默发现前台登记册中的异常"),
        forbidden_events=("直接揭晓神秘人的身份",),
        relevant_hook_ids=("basement-door",),
        ending_hook="楼上传来刻意制造的脚步声",
        style_focus=("压迫感", "有限视角"),
    )


def create_chapter_draft() -> ChapterDraft:
    content = (
        "林默推开旅馆大门，潮湿的木头气味迎面压来。"
        "前台登记册最后一页被撕去，夹缝中却卡着一把标有地下室编号的铜钥匙。"
        "他刚把钥匙收进口袋，二楼便响起了不紧不慢的脚步声。"
    )
    return ChapterDraft(
        chapter_number=1,
        title="雨中的来客",
        content=content,
        word_count=len(content),
    )


def create_chapter_delta() -> StoryStateDelta:
    return StoryStateDelta(
        source_chapter=1,
        chapter_summary="林默进入废弃旅馆，取得地下室铜钥匙，并发现二楼有人活动。",
        character_updates=(
            CharacterStateUpdate(
                character_id="lin-mo",
                location="废弃旅馆大厅",
                current_goal="查明铜钥匙对应的地下室入口",
                emotion="紧张",
                add_possessions=("地下室铜钥匙",),
                learned_fact_ids=("register-key",),
            ),
        ),
        new_facts=(
            FactRecord(
                fact_id="register-key",
                subject_id="basement-key",
                predicate="location",
                value="林默手中",
                valid_from_chapter=1,
                valid_until_chapter=None,
                source_chapter=1,
                importance=5,
            ),
        ),
        invalidated_fact_ids=(),
        new_hooks=(),
        hook_updates=(
            HookUpdate(
                hook_id="basement-door",
                status="progressing",
                note="林默获得标有地下室编号的铜钥匙",
            ),
        ),
        new_time="深夜十一点十分",
        new_location="废弃旅馆大厅",
    )
