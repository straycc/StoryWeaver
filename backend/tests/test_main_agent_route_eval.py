"""确保路由评测数据与评分器本身不会漏报危险误判。"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "route_evals" / "evaluate.py"
SPEC = importlib.util.spec_from_file_location("main_agent_route_eval", SCRIPT)
assert SPEC and SPEC.loader
route_eval = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(route_eval)


class MainAgentRouteEvalTests(unittest.TestCase):
    def test_dataset_has_50_valid_distinct_cases(self) -> None:
        _contexts, cases = route_eval.load_cases()
        self.assertEqual(len(cases), 50)
        self.assertEqual(len({case["id"] for case in cases}), 50)
        self.assertEqual(
            {kind: sum(case["expect"]["kind"] == kind for case in cases)
             for kind in ("reply", "query", "action", "clarify")},
            {"reply": 8, "query": 13, "action": 23, "clarify": 6},
        )

    def test_discussion_misrouted_to_writing_is_flagged(self) -> None:
        result = route_eval.score_case(
            {"kind": "action", "action": "creative_discussion"},
            {"kind": "action", "action": "write_batch", "parameters": {"count": 10}},
        )
        self.assertFalse(result["route_ok"])
        self.assertTrue(result["unexpected_workflow"])

    def test_wrong_query_parameter_fails_even_when_route_matches(self) -> None:
        result = route_eval.score_case(
            {"kind": "query", "action": "query_chapter",
             "parameters": {"chapter_number": 3, "include": "content"}},
            {"kind": "query", "action": "query_chapter",
             "parameters": {"chapter_number": 2, "include": "content"}},
        )
        self.assertTrue(result["route_ok"])
        self.assertFalse(result["parameters_ok"])


if __name__ == "__main__":
    unittest.main()
