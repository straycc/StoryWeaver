"""Context V2 的预算、版本与工具证据协议测试。"""

from __future__ import annotations

import unittest

from storyweaver.context_management import (
    ContextBudgetExceededError,
    ContextCandidate,
    source_ref,
    trace_from_candidates,
    with_tool_results,
)


class ContextV2Tests(unittest.TestCase):
    def _candidate(
        self,
        source_id: str,
        content: str,
        *,
        protected: bool,
        digest: str | None = None,
    ) -> ContextCandidate:
        return ContextCandidate(
            source=source_ref(
                source_id=source_id,
                source_type="test",
                content=content,
                book_version=7,
            ),
            content=content,
            reason="测试来源",
            protected=protected,
            priority=10,
            digest=digest,
        )

    def test_protected_source_uses_digest_and_hashes_rendered_content(self) -> None:
        original = "原文" * 100
        digest = "摘要" * 5
        _, trace = trace_from_candidates(
            agent_role="writer",
            policy_version="writer-context-v2.1",
            book_version=7,
            token_budget=30,
            candidates=(self._candidate("plan:8", original, protected=True, digest=digest),),
        )
        selected = [item for item in trace.selected if item.disposition == "selected"]
        self.assertEqual(selected[0].rendered_content, digest)
        self.assertNotEqual(selected[0].source.content_hash, source_ref(
            source_id="plan:8", source_type="test", content=original, book_version=7,
        ).content_hash)

    def test_protected_source_without_digest_fails_explicitly(self) -> None:
        with self.assertRaises(ContextBudgetExceededError):
            trace_from_candidates(
                agent_role="writer",
                policy_version="writer-context-v2.1",
                book_version=7,
                token_budget=10,
                candidates=(self._candidate("plan:8", "约束" * 100, protected=True),),
            )

    def test_tool_evidence_is_retained_in_trace(self) -> None:
        _, trace = trace_from_candidates(
            agent_role="reviewer",
            policy_version="reviewer-context-v2.1",
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
                "succeeded": True,
                "truncated": True,
            }],
        )
        self.assertEqual(len(traced.tool_results), 1)
        record = traced.tool_results[0]
        self.assertEqual(record.source.locator, "search_canon_evidence")
        self.assertEqual(record.tool_arguments, {"query": "密信"})
        self.assertEqual(record.raw_tool_arguments, '{"query":"密信"}')
        self.assertTrue(record.truncated)


if __name__ == "__main__":
    unittest.main()
