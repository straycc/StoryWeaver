"""统一 Context Selector 与轻量 Trace 测试。"""

from __future__ import annotations

import unittest

from storyweaver.context import (
    ContextBudgetExceededError,
    ContextCandidate,
    select_context,
    with_tool_results,
)


class ContextSelectionTests(unittest.TestCase):
    @staticmethod
    def _candidate(
        source_id: str,
        content: str,
        *,
        protected: bool,
        digest: str | None = None,
    ) -> ContextCandidate:
        return ContextCandidate(
            source_id=source_id,
            source_type="test",
            content=content,
            reason="测试来源",
            protected=protected,
            priority=10,
            digest=digest,
        )

    def test_protected_source_uses_digest_without_entering_trace_content(self) -> None:
        selected, trace = select_context(
            agent_role="writer",
            policy_version="context-policy-v1",
            book_version=7,
            token_budget=30,
            candidates=(
                self._candidate(
                    "plan:8", "原文" * 100, protected=True, digest="摘要" * 5,
                ),
            ),
        )

        self.assertEqual(selected[0].content, "摘要" * 5)
        self.assertEqual(trace.compressed_source_ids, ("plan:8",))
        self.assertNotIn("原文", str(trace.to_data()))

    def test_protected_source_without_digest_fails_explicitly(self) -> None:
        with self.assertRaises(ContextBudgetExceededError):
            select_context(
                agent_role="writer",
                policy_version="context-policy-v1",
                book_version=7,
                token_budget=10,
                candidates=(
                    self._candidate("plan:8", "约束" * 100, protected=True),
                ),
            )

    def test_tool_trace_only_retains_stable_source_id(self) -> None:
        _, trace = select_context(
            agent_role="reviewer",
            policy_version="context-policy-v1",
            book_version=7,
            token_budget=100,
            candidates=(self._candidate("review:prompt", "最小上下文", protected=True),),
        )
        traced = with_tool_results(
            trace,
            evidence=[{
                "tool_name": "search_canon_evidence",
                "arguments": {"query": "密信"},
                "raw_arguments": '{"query":"密信"}',
                "result": {"facts": ["第3章存在密信"]},
            }],
        )

        persisted = traced.to_data()
        self.assertEqual(len(persisted["tool_source_ids"]), 1)
        self.assertNotIn("tool_results", persisted)
        self.assertNotIn("raw_arguments", str(persisted))
        self.assertNotIn("第3章存在密信", str(persisted))


if __name__ == "__main__":
    unittest.main()
