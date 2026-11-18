"""MainAgent 路由评测：读取固定用例，调用真实模型，但不执行业务动作。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from storyweaver.api.capabilities import get_capability, validate_capability_parameters
from storyweaver.api.main_agent import MainAgent
from storyweaver.model_config import ModelCatalog
from storyweaver.novel_creation.application import NovelApplicationSettings


CASE_PATH = Path(__file__).with_name("cases.yaml")
RESULT_DIRECTORY = Path(__file__).resolve().parents[2] / "runtime" / "route_evals"


def load_cases(path: Path = CASE_PATH) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """先校验数据集，避免花费模型调用后才发现标准答案写错。"""

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("用例文件必须是 version: 1 的 YAML 对象")
    contexts, cases = data.get("contexts"), data.get("cases")
    if not isinstance(contexts, dict) or not isinstance(cases, list):
        raise ValueError("缺少 contexts 或 cases")
    if not all(isinstance(value, str) for value in contexts.values()):
        raise ValueError("contexts 的内容必须是字符串")
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("每个用例必须是对象")
        case_id, context, user = case.get("id"), case.get("context"), case.get("user")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError(f"用例 ID 无效或重复：{case_id!r}")
        seen.add(case_id)
        if context not in contexts or not isinstance(user, str) or not user.strip():
            raise ValueError(f"{case_id}: 上下文不存在或用户输入为空")
        expected = case.get("expect")
        if not isinstance(expected, dict) or expected.get("kind") not in {"reply", "query", "action", "clarify"}:
            raise ValueError(f"{case_id}: expect.kind 无效")
        action = expected.get("action")
        if expected["kind"] in {"reply", "clarify"}:
            if action is not None:
                raise ValueError(f"{case_id}: reply/clarify 不能指定 action")
        else:
            if not isinstance(action, str):
                raise ValueError(f"{case_id}: query/action 必须指定 action")
            capability = get_capability(action)
            if expected["kind"] == "query" and capability.category != "query":
                raise ValueError(f"{case_id}: query 的能力类别不匹配")
            if expected["kind"] == "action" and capability.category == "query":
                raise ValueError(f"{case_id}: action 的能力类别不匹配")
        parameters = expected.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ValueError(f"{case_id}: expect.parameters 必须是对象")
        if action:
            unknown = set(parameters) - set(get_capability(action).parameter_model.model_fields)
            if unknown:
                raise ValueError(f"{case_id}: 未定义的参数 {sorted(unknown)}")
        elif parameters:
            raise ValueError(f"{case_id}: 无 action 时不能指定参数")
    return contexts, cases


def score_case(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    """路由与参数分开计分；错误的写作动作单独计为风险。"""

    route_ok = actual.get("kind") == expected["kind"] and actual.get("action") == expected.get("action")
    actual_action = actual.get("action")
    expected_action = expected.get("action")
    parameters_ok = route_ok
    parameter_error: str | None = None
    if route_ok and actual_action:
        try:
            normalized = validate_capability_parameters(actual_action, actual.get("parameters", {}))
            parameters_ok = all(normalized.get(key) == value for key, value in expected.get("parameters", {}).items())
            if not parameters_ok:
                parameter_error = "关键参数与标准答案不符"
        except (TypeError, ValueError) as exc:
            parameters_ok = False
            parameter_error = str(exc)
    try:
        actual_category = get_capability(actual_action).category if actual_action else None
    except ValueError:
        actual_category = None
    actual_workflow = actual_category == "workflow"
    expected_workflow = bool(expected_action and get_capability(expected_action).category == "workflow") if expected_action else False
    return {
        "route_ok": route_ok,
        "parameters_ok": parameters_ok,
        "invalid_action": bool(actual_action and actual_category is None),
        "unexpected_workflow": actual_workflow and not expected_workflow,
        "wrong_workflow": actual_workflow and expected_workflow and actual_action != expected_action,
        "parameter_error": parameter_error,
    }


async def evaluate(cases: list[dict[str, Any]], contexts: dict[str, str], agent: MainAgent) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for number, case in enumerate(cases, 1):
        context = f"[current_request]\n{case['user']}\n\n{contexts[case['context']].strip()}"
        started = time.perf_counter()
        try:
            decision = await agent.decide(context=context)
            actual = decision.model_dump()
            scoring = score_case(case["expect"], actual)
            error = None
        except Exception as exc:
            actual = None
            scoring = {"route_ok": False, "parameters_ok": False,
                       "invalid_action": False, "unexpected_workflow": False, "wrong_workflow": False,
                       "parameter_error": None}
            error = f"{type(exc).__name__}: {exc}"
        elapsed = round(time.perf_counter() - started, 3)
        result = {
            "id": case["id"], "context": case["context"], "user": case["user"],
            "expected": case["expect"], "actual": actual, **scoring,
            "error": error, "seconds": elapsed,
        }
        results.append(result)
        status = "PASS" if scoring["route_ok"] and scoring["parameters_ok"] else "FAIL"
        print(f"[{number:02d}/{len(cases):02d}] {status} {case['id']} {elapsed:.1f}s", flush=True)
    return results


def summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(results)
    by_kind: dict[str, dict[str, int]] = {}
    for kind in ("reply", "query", "action", "clarify"):
        rows = [row for row in results if row["expected"]["kind"] == kind]
        by_kind[kind] = {"total": len(rows), "route_correct": sum(row["route_ok"] for row in rows)}
    return {
        "total": count,
        "route_correct": sum(row["route_ok"] for row in results),
        "route_accuracy": round(sum(row["route_ok"] for row in results) / count, 4) if count else 0,
        "parameters_correct": sum(row["parameters_ok"] for row in results),
        "unexpected_workflows": sum(row["unexpected_workflow"] for row in results),
        "wrong_workflows": sum(row["wrong_workflow"] for row in results),
        "invalid_actions": sum(row["invalid_action"] for row in results),
        "model_errors": sum(bool(row["error"]) for row in results),
        "seconds_total": round(sum(row["seconds"] for row in results), 3),
        "by_kind": by_kind,
        "confusion": dict(Counter(
            f"{row['expected']['kind']}:{row['expected'].get('action') or '-'} -> "
            f"{(row['actual'] or {}).get('kind') or 'error'}:{(row['actual'] or {}).get('action') or '-'}"
            for row in results if not row["route_ok"]
        )),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="StoryWeaver MainAgent 路由评测")
    parser.add_argument("--check", action="store_true", help="只校验用例，不调用模型")
    parser.add_argument("--case-file", type=Path, default=CASE_PATH)
    parser.add_argument("--provider", help="覆盖默认供应商 ID")
    parser.add_argument("--model", help="覆盖默认模型 ID")
    parser.add_argument("--id", action="append", dest="case_ids", help="按用例 ID 选择，可重复指定")
    parser.add_argument("--limit", type=int, help="只运行前 N 条，适合试跑")
    parser.add_argument("--output", type=Path, help="结果 JSON 路径，默认存入 runtime/route_evals")
    args = parser.parse_args()
    try:
        contexts, cases = load_cases(args.case_file)
        if args.case_ids:
            requested = set(args.case_ids)
            available = {case["id"] for case in cases}
            unknown = requested - available
            if unknown:
                parser.error(f"未知用例 ID：{', '.join(sorted(unknown))}")
            cases = [case for case in cases if case["id"] in requested]
        if args.limit is not None:
            if args.limit < 1:
                parser.error("--limit 必须大于 0")
            cases = cases[:args.limit]
        if args.check:
            print(f"用例校验通过：{len(cases)} 条；类别：{dict(Counter(c['expect']['kind'] for c in cases))}")
            return 0
        catalog = ModelCatalog()
        provider, model_id, key = catalog.selection(args.provider, args.model)
        settings = NovelApplicationSettings(base_url=provider.base_url, model=model_id, api_key=key)
        agent = MainAgent(settings, model=catalog.create_model(provider.id, model_id))
        results = asyncio.run(evaluate(cases, contexts, agent))
        report = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model": {"provider": provider.id, "model": model_id},
            "case_file": str(args.case_file), "summary": summary(results), "results": results,
        }
        target = args.output or RESULT_DIRECTORY / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        print(f"逐条结果：{target}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"评测无法启动：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
