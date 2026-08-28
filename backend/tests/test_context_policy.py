"""Context Policy、Evidence 编译和统一 Tool Runtime 测试。"""

from __future__ import annotations

import asyncio
import json
import unittest

from pydantic import BaseModel, ConfigDict

from storyweaver.context import (
    ContextBudget,
    ContextPriority,
    EvidenceCompiler,
    default_agent_context_policies,
)
from storyweaver.llm.tool_runtime import (
    ReadToolSpec,
    ToolRuntimePolicy,
    build_read_tools,
)


class _Query(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str


class ContextPolicyTests(unittest.TestCase):
    def test_budget_reserves_output_and_safety_instead_of_using_full_window(self) -> None:
        budget = ContextBudget(
            operational_window=100,
            fixed_context=10,
            initial_dynamic_context=20,
            runtime_tool_context=10,
            evidence_package=10,
            output_reserve=20,
            safety_reserve=20,
        )

        self.assertEqual(budget.research_phase_total, 60)
        self.assertEqual(budget.report_phase_total, 80)
        self.assertEqual(budget.peak_reserved, 80)
        self.assertEqual(budget.unallocated, 20)

    def test_overcommitted_budget_fails_explicitly(self) -> None:
        with self.assertRaisesRegex(ValueError, "超过 operational_window"):
            ContextBudget(
                operational_window=100,
                fixed_context=20,
                initial_dynamic_context=30,
                runtime_tool_context=20,
                evidence_package=20,
                output_reserve=20,
                safety_reserve=20,
            )

    def test_default_policy_declares_agent_execution_modes(self) -> None:
        policies = default_agent_context_policies()

        self.assertTrue(policies["planner"].retrieval.two_phase)
        self.assertTrue(policies["reviewer"].retrieval.enabled)
        self.assertFalse(policies["writer"].retrieval.enabled)
        self.assertEqual(policies["reviewer"].retrieval.max_tool_calls, 6)
        self.assertEqual(
            policies["analyzer"].budget.initial_dynamic_context,
            15_000,
        )
        self.assertLessEqual(
            policies["analyzer"].budget.peak_reserved,
            policies["analyzer"].budget.operational_window,
        )
        self.assertEqual(ContextPriority.PROTECTED, 100)
        self.assertGreater(ContextPriority.REQUIRED, ContextPriority.SUPPORTING)


class EvidenceCompilerTests(unittest.TestCase):
    def test_deduplicates_without_retruncating_runtime_result(self) -> None:
        bounded_result = {"content_excerpt": "证据" * 40, "truncated": True}
        item = {
            "tool_name": "search",
            "arguments": {"query": "密信"},
            "result": bounded_result,
            "succeeded": True,
            "truncated": True,
        }

        package = EvidenceCompiler(
            token_budget=500,
        ).compile([item, item])

        self.assertEqual(package.source_count, 2)
        self.assertEqual(len(package.evidence), 1)
        self.assertEqual(package.truncated_count, 1)
        self.assertTrue(package.evidence[0]["truncated"])
        self.assertEqual(package.evidence[0]["result"], bounded_result)

    def test_oversized_unbounded_result_is_excluded_instead_of_retruncated(self) -> None:
        package = EvidenceCompiler(token_budget=80).compile([{
            "tool_name": "unsafe_source",
            "arguments": {},
            "result": {"content": "未限制结果" * 200},
            "succeeded": True,
            "truncated": False,
        }])

        self.assertEqual(package.evidence, ())
        self.assertEqual(package.excluded_count, 1)
        self.assertEqual(package.truncated_count, 0)

    def test_prefers_successful_evidence_when_total_budget_is_small(self) -> None:
        failed = {
            "tool_name": "first",
            "arguments": {},
            "result": {"error": "失败" * 20},
            "succeeded": False,
        }
        success = {
            "tool_name": "second",
            "arguments": {},
            "result": {"matched": True, "fact": "有效证据"},
            "succeeded": True,
        }

        package = EvidenceCompiler(
            token_budget=100,
        ).compile([failed, success])

        self.assertEqual(package.evidence[0]["tool_name"], "second")


class ToolRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_call_reuses_result_without_consuming_budget(self) -> None:
        executions = 0
        evidence: list[dict[str, object]] = []
        completed: list[tuple[object, ...]] = []

        async def execute(arguments: object) -> object:
            nonlocal executions
            executions += 1
            return {"matched": True, "query": getattr(arguments, "get")("query")}

        async def on_completed(*args: object) -> None:
            completed.append(args)

        tool = build_read_tools(
            specs=(ReadToolSpec("query", "查询", _Query, execute),),
            policy=ToolRuntimePolicy(max_tool_calls=1),
            evidence=evidence,
            on_completed=on_completed,
        )[0]
        first = await tool.on_invoke_tool(None, '{"query":"密信"}')
        second = await tool.on_invoke_tool(None, '{"query":"密信"}')

        self.assertEqual(first, second)
        self.assertEqual(executions, 1)
        self.assertEqual(len(evidence), 1)
        self.assertFalse(bool(completed[0][-2]))
        self.assertTrue(bool(completed[1][-2]))

    async def test_timeout_is_returned_as_unified_failure(self) -> None:
        async def execute(_arguments: object) -> object:
            await asyncio.sleep(0.02)
            return {"matched": True}

        tool = build_read_tools(
            specs=(ReadToolSpec("slow", "慢查询", _Query, execute),),
            policy=ToolRuntimePolicy(
                max_tool_calls=1,
                timeout_seconds=0.001,
                transient_attempts=1,
            ),
        )[0]
        result = json.loads(await tool.on_invoke_tool(None, '{"query":"x"}'))

        self.assertEqual(result["status"], "timeout")
        self.assertIn("error", result)

    async def test_transient_failure_is_retried_inside_runtime(self) -> None:
        attempts = 0

        async def execute(_arguments: object) -> object:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("临时连接失败")
            return {"matched": True, "value": "恢复"}

        tool = build_read_tools(
            specs=(ReadToolSpec("unstable", "临时失败", _Query, execute),),
            policy=ToolRuntimePolicy(
                max_tool_calls=1,
                transient_attempts=2,
            ),
        )[0]
        result = json.loads(await tool.on_invoke_tool(None, '{"query":"x"}'))

        self.assertEqual(attempts, 2)
        self.assertTrue(result["matched"])

    async def test_result_and_call_budget_are_hard_limits(self) -> None:
        async def execute(arguments: object) -> object:
            return {"query": getattr(arguments, "get")("query"), "content": "证据" * 500}

        tool = build_read_tools(
            specs=(ReadToolSpec("bounded", "有限结果", _Query, execute),),
            policy=ToolRuntimePolicy(
                max_tool_calls=1,
                result_token_limit=80,
            ),
        )[0]
        first = json.loads(await tool.on_invoke_tool(None, '{"query":"first"}'))
        second = json.loads(await tool.on_invoke_tool(None, '{"query":"second"}'))

        self.assertTrue(first["truncated"])
        rendered = json.dumps(first, ensure_ascii=False)
        self.assertLessEqual((len(rendered.strip()) + 1) // 2, 80)
        self.assertEqual(second["status"], "budget_exhausted")


if __name__ == "__main__":
    unittest.main()
