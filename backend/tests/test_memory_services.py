"""长期记忆提取与按需检索的上下文边界测试。"""

from __future__ import annotations

import unittest
from hashlib import sha256

from storyweaver.memory import (
    LongTermMemoryExtractor,
    LongTermMemoryRecord,
    LongTermMemoryRetriever,
    LongTermMemoryStatus,
    LongTermMemoryType,
    MemoryScopeType,
    format_recent_dialogue,
)


class _MemoryStore:
    def __init__(self, records: tuple[LongTermMemoryRecord, ...] = ()) -> None:
        self.records = list(records)

    @staticmethod
    def timestamp() -> str:
        return "2026-01-01T00:00:00+00:00"

    @staticmethod
    def fingerprint(content: str) -> str:
        return sha256(content.encode("utf-8")).hexdigest()

    def save(self, record: LongTermMemoryRecord) -> bool:
        self.records.append(record)
        return True

    def list_records(self, *, scope_type=None, scope_id=None, status=None):
        return tuple(
            item
            for item in self.records
            if (scope_type is None or item.scope_type == scope_type)
            and (scope_id is None or item.scope_id == scope_id)
            and (status is None or item.status == status)
        )


def _record(
    memory_id: str,
    *,
    name: str,
    description: str,
    content: str,
) -> LongTermMemoryRecord:
    return LongTermMemoryRecord(
        memory_id=memory_id,
        memory_type=LongTermMemoryType.USER_PREFERENCE,
        scope_type=MemoryScopeType.GLOBAL,
        scope_id="default",
        name=name,
        description=description,
        content=content,
        importance=4,
        source_refs=("message:test",),
        fingerprint=sha256(content.encode("utf-8")).hexdigest(),
        status=LongTermMemoryStatus.ACTIVE,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


class MemoryServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_recent_dialogue_keeps_newest_messages_with_a_hard_limit(self) -> None:
        rendered = format_recent_dialogue(
            (("user", f"消息{index}") for index in range(10)),
            max_messages=3,
            max_chars=100,
        )

        self.assertNotIn("消息6", rendered)
        self.assertIn("消息7", rendered)
        self.assertIn("消息9", rendered)
        self.assertLessEqual(len(rendered), 100)

    async def test_extractor_marks_history_as_reference_only(self) -> None:
        prompts: list[str] = []

        async def generate(prompt: str) -> str:
            prompts.append(prompt)
            return "[]"

        extractor = LongTermMemoryExtractor(
            generate_text=generate,
            store=_MemoryStore(),  # type: ignore[arg-type]
        )
        await extractor.extract(
            user_message="就按第二个",
            assistant_message="好的，采用身份线。",
            session_id="session-1",
            user_message_id="message-3",
            book_id="book-1",
            recent_context="assistant：第二个方向是被长辈遮瞒的身份线。",
        )

        self.assertEqual(len(prompts), 1)
        self.assertIn("最近对话只用于理解当前用户消息", prompts[0])
        self.assertIn("不得把最近对话本身当作本轮新增记忆来源", prompts[0])
        self.assertIn("当前目标用户消息：就按第二个", prompts[0])

    async def test_retriever_uses_recent_context_for_semantic_selection(self) -> None:
        prompts: list[str] = []

        async def generate(prompt: str) -> str:
            prompts.append(prompt)
            return '["memory-1"]'

        memory = _record(
            "memory-1",
            name="身份线方向",
            description="主角身世被长辈长期遮瞒",
            content="创作讨论优先沿身份谜团推进。",
        )
        retriever = LongTermMemoryRetriever(
            generate_text=generate,
            store=_MemoryStore((memory,)),  # type: ignore[arg-type]
        )

        selected = await retriever.retrieve(
            query="就按第二个",
            book_id=None,
            recent_context="assistant：第二个方向是被长辈遮瞒的身份线。",
        )

        self.assertEqual(selected, (memory,))
        self.assertIn("当前请求是主要判断依据", prompts[0])
        self.assertIn("第二个方向是被长辈遮瞒的身份线", prompts[0])

    async def test_retriever_fallback_can_use_recent_context(self) -> None:
        async def fail(_prompt: str) -> str:
            raise TimeoutError("selector timeout")

        memory = _record(
            "memory-1",
            name="克制悬疑",
            description="保持克制悬疑的叙事方向",
            content="不要过早揭晓谜底。",
        )
        retriever = LongTermMemoryRetriever(
            generate_text=fail,
            store=_MemoryStore((memory,)),  # type: ignore[arg-type]
        )

        selected = await retriever.retrieve(
            query="就按第二个",
            book_id=None,
            recent_context="assistant：第二个方案是保持克制悬疑，不提前揭晓谜底。",
        )

        self.assertEqual(selected, (memory,))


if __name__ == "__main__":
    unittest.main()
