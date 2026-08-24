"""M4 使用的固定创作简报。"""

from __future__ import annotations

from ..novel_creation.models import CreateNovelRequest
from .models import EvaluationCase


EVALUATION_CASES: tuple[EvaluationCase, ...] = (
    EvaluationCase(
        case_id="rainy-hotel",
        description="封闭空间失踪案，重点观察线索连续性和知识边界。",
        request=CreateNovelRequest(
            title="雨夜旅馆",
            genre="悬疑",
            premise="年轻侦探林默进入废弃旅馆，调查十年前发生的失踪案。",
            protagonist="林默",
            central_conflict="林默寻找真相，旅馆中的神秘人试图把他引向错误线索。",
            tone="克制、压迫、有限视角",
            target_chapters=6,
            chapter_target_words=1200,
            language="zh",
        ),
    ),
    EvaluationCase(
        case_id="tidal-amnesia",
        description="近未来记忆悬疑，重点观察身份谜团和伏笔推进。",
        request=CreateNovelRequest(
            title="潮汐失忆症",
            genre="近未来悬疑",
            premise=(
                "海滨城市会定期清理居民记忆，记忆修复师许澄却收到一段"
                "来自明天、记录自己死亡现场的记忆。"
            ),
            protagonist="许澄",
            central_conflict=(
                "许澄必须在下一次记忆清理前查明死亡记忆的来源，同时判断"
                "同事穆青是否值得信任。"
            ),
            tone="冷静、简洁、潮湿而疏离",
            target_chapters=6,
            chapter_target_words=1200,
            language="zh",
        ),
    ),
    EvaluationCase(
        case_id="last-letter",
        description="现实人物关系故事，重点观察动机变化和情感克制。",
        request=CreateNovelRequest(
            title="没有寄出的最后一封信",
            genre="现实情感",
            premise=(
                "多年未归乡的程砚整理父亲遗物时，发现一封写给失联姐姐、"
                "却从未寄出的信。"
            ),
            protagonist="程砚",
            central_conflict=(
                "程砚想借信寻找姐姐并修复家庭关系，母亲却坚持让过去保持沉默。"
            ),
            tone="克制、生活化、避免煽情",
            target_chapters=6,
            chapter_target_words=1200,
            language="zh",
        ),
    ),
)


def get_evaluation_case(case_id: str) -> EvaluationCase:
    normalized = case_id.strip()
    for item in EVALUATION_CASES:
        if item.case_id == normalized:
            return item
    available = "、".join(item.case_id for item in EVALUATION_CASES)
    raise KeyError(f"未知评测案例 {case_id!r}；可选：{available}")
