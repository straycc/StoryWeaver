"""联网调研：来源登记、工具治理与编排降级。"""

from __future__ import annotations

import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from storyweaver.llm.sdk import WorkerSettings
from storyweaver.llm.tool_runtime import ToolRuntimePolicy
from storyweaver.research.config import ResearchSettings
from storyweaver.research.mcp_provider import (
    _normalize_hits,
    _payload_from_result,
    open_mcp_session,
)
from storyweaver.research.provider import RawHit, ResearchTransportError
from storyweaver.research.report import (
    SourceRegistry,
    finalize_report,
    short_topic,
)
from storyweaver.research.service import ResearchService
from storyweaver.research.tool import SearchRecorder, build_web_search_tool


def _hit(url: str, title: str = "示例页面") -> RawHit:
    return {"title": title, "url": url, "site": "example.com", "snippet": "页面摘要"}


class _FakeSession:
    """按脚本返回结果的检索替身。"""

    def __init__(self, responses: list[object]) -> None:
        self.calls: list[tuple[str, int]] = []
        self._responses = list(responses)

    async def search(self, *, query: str, max_results: int) -> list[RawHit]:
        self.calls.append((query, max_results))
        outcome = self._responses.pop(0) if self._responses else []
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


def _settings(**overrides: object) -> ResearchSettings:
    base: dict[str, object] = {
        "api_key": "tvly-test",
        "max_searches": 5,
        "per_search_timeout_seconds": 30.0,
        "total_timeout_seconds": 60.0,
        "report_reserve_seconds": 15.0,
        "result_token_limit": 4000,
    }
    base.update(overrides)
    return ResearchSettings(**base)  # type: ignore[arg-type]


class ResultParsingTests(unittest.TestCase):
    def test_structured_content_wins_over_text_blocks(self) -> None:
        result = SimpleNamespace(
            is_error=False,
            structured_content={"results": [{"title": "A", "url": "https://a.test/x"}]},
            content=[],
        )
        payload = _payload_from_result(result)
        self.assertEqual(payload["results"][0]["url"], "https://a.test/x")

    def test_text_block_json_is_decoded(self) -> None:
        text = json.dumps({"results": [{"title": "B", "url": "https://b.test/y"}]})
        result = SimpleNamespace(
            is_error=False,
            structured_content=None,
            content=[SimpleNamespace(text=text)],
        )
        hits = _normalize_hits(_payload_from_result(result))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["site"], "b.test")

    def test_error_result_is_rejected(self) -> None:
        result = SimpleNamespace(is_error=True, structured_content=None, content=[])
        with self.assertRaises(ResearchTransportError):
            _payload_from_result(result)

    def test_non_http_urls_and_duplicates_are_dropped(self) -> None:
        hits = _normalize_hits(
            {
                "results": [
                    {"title": "ok", "url": "https://a.test/1"},
                    {"title": "dup", "url": "https://a.test/1"},
                    {"title": "bad", "url": "javascript:alert(1)"},
                    {"title": "no url"},
                ]
            }
        )
        self.assertEqual([hit["url"] for hit in hits], ["https://a.test/1"])


class SourceRegistryTests(unittest.TestCase):
    def test_registry_numbers_by_first_seen_and_dedupes(self) -> None:
        registry = SourceRegistry(max_sources=3)
        registry.register([_hit("https://a.test/1"), _hit("https://b.test/2")])
        registry.register([_hit("https://a.test/1"), _hit("https://c.test/3")])
        self.assertEqual([source.index for source in registry.sources], [1, 2, 3])

    def test_registry_respects_max_sources(self) -> None:
        registry = SourceRegistry(max_sources=1)
        registry.register([_hit("https://a.test/1"), _hit("https://b.test/2")])
        self.assertEqual(len(registry), 1)

    def test_unknown_source_id_is_dropped(self) -> None:
        registry = SourceRegistry(max_sources=5)
        registry.register([_hit("https://a.test/1")])
        report = finalize_report("## 宋代县衙\n\n设知县一人[来源 1][来源 9]。", registry)
        self.assertIn("[1](https://a.test/1)", report.markdown)
        self.assertNotIn("[9]", report.markdown)
        self.assertEqual([source.index for source in report.cited], [1])

    def test_streamed_markdown_keeps_structure_and_citations(self) -> None:
        registry = SourceRegistry(max_sources=5)
        registry.register([_hit("https://a.test/1", "页面甲"), _hit("https://b.test/2", "页面乙")])
        text = (
            "## 宋代县衙：编制与职掌\n\n"
            "县衙以知县为长官，佐官分掌具体事务。\n\n"
            "### 一、长官与佐贰\n\n"
            "设知县一人，掌一县之政[1]。\n\n"
            "### 二、属官分工\n\n"
            "| 职位 | 职掌 |\n| --- | --- |\n| 主簿 | 勾稽簿籍[2、1] |\n\n"
            "### 对创作的启示\n\n"
            "- 可写知县与主簿在账目上的争执[2]\n"
        )
        report = finalize_report(text, registry)
        # 流式正文原样保留标题、导语与分节，不再由后端重排。
        self.assertTrue(report.markdown.startswith("## 宋代县衙：编制与职掌"))
        self.assertIn("### 一、长官与佐贰", report.markdown)
        self.assertIn("### 二、属官分工", report.markdown)
        self.assertIn("### 对创作的启示", report.markdown)
        # 引用变成数字角标，链接取自登记表。
        self.assertIn("[1](https://a.test/1)", report.markdown)
        self.assertIn("[2](https://b.test/2)", report.markdown)
        self.assertIn("| 主簿 |", report.markdown)
        self.assertNotIn("资料来源", report.markdown)
        self.assertNotIn("资料摘要", report.markdown)
        self.assertEqual(report.title, "宋代县衙：编制与职掌")

    def test_bare_url_in_report_is_removed(self) -> None:
        registry = SourceRegistry(max_sources=5)
        registry.register([_hit("https://a.test/1")])
        report = finalize_report(
            "## 宋代县衙\n\n见 https://fake.test/x 的说明，另见 [这个链接](https://fake.test/y)。",
            registry,
        )
        # 自造链接既不能留下可点的 URL，也不能变成正文里的纯网址。
        self.assertNotIn("fake.test", report.markdown)
        self.assertIn("这个链接", report.markdown)

    def test_cited_sources_only_contains_referenced_ones(self) -> None:
        registry = SourceRegistry(max_sources=5)
        registry.register([
            _hit("https://a.test/1", "页面甲"),
            _hit("https://b.test/2", "页面乙"),
            _hit("https://c.test/3", "页面丙"),
        ])
        report = finalize_report(
            "## 宋代县衙\n\n设知县一人[1]。",
            registry,
        )
        self.assertEqual([source.title for source in report.cited], ["页面甲"])
        self.assertNotIn("页面乙", report.markdown)
        self.assertNotIn("页面丙", report.markdown)

    def test_model_added_source_list_is_stripped(self) -> None:
        registry = SourceRegistry(max_sources=5)
        registry.register([_hit("https://a.test/1", "页面甲")])
        report = finalize_report(
            "## 宋代县衙\n\n设知县一人[1]。\n\n### 资料来源\n- 页面甲 https://a.test/1\n",
            registry,
        )
        self.assertNotIn("资料来源", report.markdown)
        self.assertNotIn("页面甲", report.markdown)

    def test_missing_title_is_prepended_from_topic(self) -> None:
        registry = SourceRegistry(max_sources=5)
        registry.register([_hit("https://a.test/1", "页面甲")])
        report = finalize_report(
            "县衙设知县一人[1]。",
            registry,
            topic="请检索宋代县衙的编制",
        )
        self.assertTrue(report.markdown.startswith("## 宋代县衙的编制"))
        self.assertEqual(report.title, "宋代县衙的编制")

    def test_code_fence_is_unwrapped(self) -> None:
        registry = SourceRegistry(max_sources=5)
        registry.register([_hit("https://a.test/1", "页面甲")])
        report = finalize_report(
            "```markdown\n## 宋代县衙\n\n设知县一人[1]。\n```",
            registry,
        )
        self.assertTrue(report.markdown.startswith("## 宋代县衙"))
        self.assertNotIn("```", report.markdown)

    def test_short_topic_strips_instruction_prefix(self) -> None:
        topic = short_topic(
            "请检索并核实宋代（以北宋中期为主，兼顾南宋差异）官制的准确事实。并标注时代差异。"
        )
        self.assertFalse(topic.startswith("请检索"))
        self.assertLessEqual(len(topic), 29)
        self.assertEqual(short_topic(""), "本次检索")


class McpConnectRetryTests(unittest.IsolatedAsyncioTestCase):
    """首次建连最容易失败（DNS + TLS + initialize + tools/list）。"""

    async def test_first_connect_failure_is_retried_once(self) -> None:
        attempts = {"count": 0}

        class FakeServer:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def connect(self) -> None:
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise OSError("connection reset by peer")

            async def list_tools(self) -> list[SimpleNamespace]:
                return [SimpleNamespace(name="tavily_search")]

            async def cleanup(self) -> None:
                pass

        with patch("agents.mcp.MCPServerStreamableHttp", FakeServer), patch(
            "storyweaver.research.mcp_provider.asyncio.sleep", AsyncMock()
        ):
            async with open_mcp_session(_settings()) as session:
                self.assertEqual(session.tool_name, "tavily_search")

        self.assertEqual(attempts["count"], 2)

    async def test_persistent_failure_raises_transport_error(self) -> None:
        attempts = {"count": 0}

        class FakeServer:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def connect(self) -> None:
                attempts["count"] += 1
                raise OSError("connection reset by peer")

            async def list_tools(self) -> list[object]:
                return []

            async def cleanup(self) -> None:
                pass

        with patch("agents.mcp.MCPServerStreamableHttp", FakeServer), patch(
            "storyweaver.research.mcp_provider.asyncio.sleep", AsyncMock()
        ):
            with self.assertRaises(ResearchTransportError):
                async with open_mcp_session(_settings()):
                    pass

        self.assertEqual(attempts["count"], 2)


class SearchToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_call_records_hits(self) -> None:
        recorder = SearchRecorder(limit=5)
        session = _FakeSession([[_hit("https://a.test/1"), _hit("https://b.test/2")]])
        tool = build_web_search_tool(
            session=session,  # type: ignore[arg-type]
            recorder=recorder,
            policy=ToolRuntimePolicy(max_tool_calls=3, timeout_seconds=5),
        )
        raw = await tool.on_invoke_tool(None, json.dumps({"query": "宋代 县衙", "max_results": 2}))
        payload = json.loads(raw)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(len(recorder), 1)
        self.assertEqual(recorder.records[0].status, "success")
        self.assertEqual(session.calls[0], ("宋代 县衙", 2))

    async def test_transport_failure_is_recorded_and_returns_error_json(self) -> None:
        recorder = SearchRecorder(limit=5)
        session = _FakeSession([ResearchTransportError("boom")])
        tool = build_web_search_tool(
            session=session,  # type: ignore[arg-type]
            recorder=recorder,
            policy=ToolRuntimePolicy(max_tool_calls=3, timeout_seconds=5),
        )
        raw = await tool.on_invoke_tool(None, json.dumps({"query": "宋代 县衙"}))
        payload = json.loads(raw)
        self.assertIn("error", payload)
        self.assertEqual(recorder.records[-1].status, "failed")

    async def test_extra_calls_are_budget_limited(self) -> None:
        recorder = SearchRecorder(limit=5)
        session = _FakeSession([[_hit("https://a.test/1")], [_hit("https://b.test/2")]])
        tool = build_web_search_tool(
            session=session,  # type: ignore[arg-type]
            recorder=recorder,
            policy=ToolRuntimePolicy(max_tool_calls=1, timeout_seconds=5),
        )
        await tool.on_invoke_tool(None, json.dumps({"query": "第一次"}))
        raw = await tool.on_invoke_tool(None, json.dumps({"query": "第二次"}))
        self.assertEqual(json.loads(raw)["status"], "budget_exhausted")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(recorder.records[-1].status, "failed")


class ResearchServiceTests(unittest.IsolatedAsyncioTestCase):
    def _worker(self) -> WorkerSettings:
        return WorkerSettings(
            worker_id="research-test",
            name="调研",
            instructions="",
            model=None,
            model_settings=None,
            timeout_seconds=10,
        )

    async def test_missing_key_fails_without_touching_network(self) -> None:
        service = ResearchService(settings=_settings(api_key=None), worker_settings=self._worker())
        outcome = await service.run(topic="宋代县衙", prompt="查一下宋代县衙")
        self.assertEqual(outcome.status, "failed")
        self.assertIn("还没有配置联网搜索", outcome.markdown)
        self.assertEqual(outcome.to_metadata()["research"]["status"], "failed")

    async def test_full_path_registers_sources_and_renders(self) -> None:
        hits = [_hit("https://a.test/1", "页面甲")]

        @asynccontextmanager
        async def fake_open(_settings: ResearchSettings):
            yield _FakeSession([hits])

        async def fake_research(*, read_tools, **_kwargs: object) -> None:
            await read_tools[0].on_invoke_tool(
                None, json.dumps({"query": "宋代 县衙", "max_results": 3})
            )

        async def fake_report(**_kwargs: object) -> str:
            return (
                "## 宋代县衙\n\n"
                "县衙以知县为长官。\n\n"
                "### 一、县衙编制\n\n"
                "县衙设知县一人[1]。\n"
            )

        with patch("storyweaver.research.service.open_mcp_session", fake_open), patch(
            "storyweaver.research.service.run_research", fake_research
        ), patch("storyweaver.research.service.run_text_worker", fake_report):
            service = ResearchService(settings=_settings(), worker_settings=self._worker())
            outcome = await service.run(topic="宋代县衙", prompt="查一下宋代县衙")

        self.assertEqual(outcome.status, "complete")
        self.assertIn("[1](https://a.test/1)", outcome.markdown)
        self.assertEqual(len(outcome.sources), 1)
        metadata = outcome.to_metadata()["research"]
        self.assertEqual(metadata["searches"][0]["hit_count"], 1)

    async def test_no_hits_fails_with_actionable_text(self) -> None:
        @asynccontextmanager
        async def fake_open(_settings: ResearchSettings):
            yield _FakeSession([[]])

        async def fake_research(*, read_tools, **_kwargs: object) -> None:
            await read_tools[0].on_invoke_tool(None, json.dumps({"query": "生僻词"}))

        with patch("storyweaver.research.service.open_mcp_session", fake_open), patch(
            "storyweaver.research.service.run_research", fake_research
        ):
            service = ResearchService(settings=_settings(), worker_settings=self._worker())
            outcome = await service.run(topic="生僻词", prompt="查一下生僻词")

        self.assertEqual(outcome.status, "failed")
        self.assertIn("换一种说法", outcome.markdown)

    async def test_report_failure_degrades_to_source_list(self) -> None:
        @asynccontextmanager
        async def fake_open(_settings: ResearchSettings):
            yield _FakeSession([[_hit("https://a.test/1", "页面甲")]])

        async def fake_research(*, read_tools, **_kwargs: object) -> None:
            await read_tools[0].on_invoke_tool(None, json.dumps({"query": "宋代 县衙"}))

        async def failing_report(**_kwargs: object) -> object:
            raise TimeoutError("报告阶段超时")

        with patch("storyweaver.research.service.open_mcp_session", fake_open), patch(
            "storyweaver.research.service.run_research", fake_research
        ), patch("storyweaver.research.service.run_text_worker", failing_report):
            service = ResearchService(settings=_settings(), worker_settings=self._worker())
            outcome = await service.run(topic="宋代县衙", prompt="查一下宋代县衙")

        self.assertEqual(outcome.status, "partial")
        # 正文不再堆来源；链接由前端「N 个来源」块渲染，数据仍在 metadata。
        self.assertEqual(outcome.sources[0]["url"], "https://a.test/1")
        self.assertNotIn("https://a.test/1", outcome.markdown)
        self.assertIn("调研结果不完整", outcome.markdown)


if __name__ == "__main__":
    unittest.main()
