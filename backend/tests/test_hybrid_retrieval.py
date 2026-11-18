"""使用真实 sqlite-vec 与假 Embedding 服务验证混合召回及故障边界。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from novel_fixtures import create_foundation, create_initial_state, create_metadata
from storyweaver.novel_creation.models import ChapterSummary, NovelProject
from storyweaver.novel_creation.review_tools import CanonEvidenceSearchTool, ReviewSnapshot, build_sdk_read_tools
from storyweaver.retrieval import HybridRetriever, RetrievalDocument
from storyweaver.retrieval.embeddings import ApiEmbedder, EmbeddingSettings, validate_vectors
from storyweaver.retrieval.runtime import build_retriever
from storyweaver.retrieval.vector_cache import VectorCache


class FakeEmbedder:
    profile = "model-v1"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        # 语义相近但不共享关键词的表达映射到同一方向。
        return [[1.0, 0.0] if any(word in text for word in ("遭到背叛", "泄露行踪")) else [0.0, 1.0] for text in texts]


class HybridRetrievalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "retrieval.db"
        self.embedder = FakeEmbedder()
        self.cache = VectorCache(self.path)
        self.retriever = HybridRetriever(embedder=self.embedder, cache=self.cache, minimum_similarity=0.8)
        self.documents = (
            RetrievalDocument("exact", "主角遭到背叛"),
            RetrievalDocument("semantic", "同伴向敌方泄露行踪"),
            RetrievalDocument("irrelevant", "林默购买食物"),
        )

    async def test_both_channels_recall_and_deduplicate(self) -> None:
        result = await self.retriever.search("遭到背叛", self.documents)
        self.assertEqual(result.mode, "hybrid")
        self.assertEqual(result.document_ids, ("exact", "semantic"))
        self.assertIsNone(result.degraded_reason)

    async def test_keywords_recall_even_when_semantic_returns_other_documents(self) -> None:
        result = await self.retriever.search("林默", self.documents)
        self.assertIn("irrelevant", result.document_ids)
        self.assertEqual(len(result.document_ids), len(set(result.document_ids)))

    async def test_keywords_work_without_api_including_two_character_names(self) -> None:
        result = await HybridRetriever().search("林默", self.documents)
        self.assertEqual(result.document_ids, ("irrelevant",))
        self.assertEqual(result.mode, "keyword")
        self.assertEqual(result.degraded_reason, "embedding_unconfigured")
        empty = await HybridRetriever().search("完全无关", self.documents)
        self.assertEqual(empty.document_ids, ())

    async def test_cache_survives_restart_and_reuses_query_vectors(self) -> None:
        first = await self.retriever.search("遭到背叛", self.documents)
        restarted = HybridRetriever(embedder=self.embedder, cache=VectorCache(self.path), minimum_similarity=0.8)
        second = await restarted.search("遭到背叛", self.documents)
        self.assertEqual(first, second)
        self.assertEqual(len(self.embedder.calls), 1)

    async def test_changed_content_and_model_are_reembedded(self) -> None:
        await self.retriever.search("遭到背叛", self.documents)
        changed = (RetrievalDocument("semantic", "林默现在购买食物"),)
        result = await self.retriever.search("遭到背叛", changed)
        self.assertEqual(result.document_ids, ())
        self.assertEqual(self.embedder.calls[-1], [changed[0].text])
        # 相同文本可跨记录复用，模型空间则严格分开。
        other = FakeEmbedder()
        other.profile = "model-v2"
        await HybridRetriever(embedder=other, cache=self.cache).search("遭到背叛", self.documents)
        self.assertTrue(other.calls)
        self.assertEqual(len(other.calls[0]), 4)

    async def test_cache_does_not_expand_snapshot_or_leak_other_books(self) -> None:
        await self.retriever.search("遭到背叛", self.documents)
        result = await self.retriever.search("遭到背叛", (self.documents[-1],))
        self.assertEqual(result.document_ids, ())
        replacement = (RetrievalDocument("new-book-record", self.documents[1].text),)
        result = await self.retriever.search("遭到背叛", replacement)
        self.assertEqual(result.document_ids, ("new-book-record",))

    async def test_timeout_falls_back_and_parent_cancellation_propagates(self) -> None:
        started = asyncio.Event()

        async def slow(texts):
            started.set()
            await asyncio.sleep(10)

        self.embedder.embed = slow
        retriever = HybridRetriever(embedder=self.embedder, cache=self.cache, timeout_seconds=0.02)
        result = await retriever.search("遭到背叛", self.documents)
        self.assertEqual(result.document_ids, ("exact",))
        self.assertEqual(result.degraded_reason, "semantic_timeout")
        started.clear()
        task = asyncio.create_task(self.retriever.search("遭到背叛", self.documents))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_api_and_cache_errors_fall_back_without_exposing_secrets(self) -> None:
        async def broken(texts):
            raise RuntimeError("secret-key-and-private-text")

        self.embedder.embed = broken
        with self.assertLogs("storyweaver.retrieval.hybrid", level="WARNING") as logs:
            result = await self.retriever.search("遭到背叛", self.documents)
        self.assertEqual(result.document_ids, ("exact",))
        self.assertEqual(result.degraded_reason, "semantic_error")
        self.assertNotIn("secret-key", str(logs.output))
        with patch.object(self.cache, "existing", side_effect=OSError("locked")):
            result = await self.retriever.search("遭到背叛", self.documents)
        self.assertEqual(result.document_ids, ("exact",))

    async def test_invalid_vectors_fall_back(self) -> None:
        async def invalid(texts):
            return [[float("nan"), 0] for _ in texts]

        self.embedder.embed = invalid
        result = await self.retriever.search("遭到背叛", self.documents)
        self.assertEqual(result.document_ids, ("exact",))
        self.assertEqual(result.degraded_reason, "semantic_error")

    async def test_changed_dimensions_under_same_model_fall_back(self) -> None:
        await self.retriever.search("遭到背叛", self.documents)

        async def changed_model(texts):
            return [[1, 0, 0] for _ in texts]

        self.embedder.embed = changed_model
        result = await self.retriever.search("林默", self.documents)
        self.assertEqual(result.mode, "keyword")
        self.assertEqual(result.document_ids, ("irrelevant",))
        self.assertEqual(result.degraded_reason, "semantic_error")

    async def test_semantic_budget_is_smaller_than_outer_tool_timeout(self) -> None:
        async def slow(texts):
            await asyncio.sleep(10)

        self.embedder.embed = slow
        project = NovelProject(metadata=create_metadata(), foundation=create_foundation(), state=create_initial_state())
        tools = build_sdk_read_tools(
            snapshot=ReviewSnapshot(project, ()), retriever=self.retriever, max_tool_calls=1,
            timeout_seconds=0.2, tool_names=("search_canon_evidence",), result_token_limit=4000,
        )
        result = json.loads(await tools[0].on_invoke_tool(None, '{"query":"地下室"}'))
        self.assertEqual(result["retrieval"]["degraded_reason"], "semantic_timeout")
        self.assertTrue(result["hooks"])

    async def test_partial_batches_are_reused_after_timeout(self) -> None:
        calls = 0

        async def partial(texts):
            nonlocal calls
            calls += 1
            if calls == 2:
                await asyncio.sleep(10)
            return [[1.0, 0.0] for _ in texts]

        self.embedder.embed = partial
        documents = tuple(RetrievalDocument(str(index), f"资料{index}") for index in range(40))
        retriever = HybridRetriever(embedder=self.embedder, cache=self.cache, timeout_seconds=0.2)
        first = await retriever.search("资料", documents)
        self.assertEqual(first.degraded_reason, "semantic_timeout")
        self.assertEqual(len(self.cache.existing(self.embedder.profile, [item.content_hash for item in documents])), 32)
        second = await self.retriever.search("资料", documents)
        self.assertEqual(second.mode, "hybrid")
        self.assertEqual(calls, 3)

    async def test_scope_filtered_memory_uses_hybrid_without_chat_selector(self) -> None:
        from test_memory_services import _MemoryStore, _record
        from storyweaver.memory import LongTermMemoryRetriever, LongTermMemoryStatus, MemoryScopeType

        selected = _record("allowed", name="线索", description="同伴", content="向敌方泄露行踪")
        disabled = replace(selected, memory_id="disabled", status=LongTermMemoryStatus.DISABLED)
        other_book = replace(selected, memory_id="other", scope_type=MemoryScopeType.BOOK, scope_id="another-book")

        async def never_called(prompt):
            raise AssertionError("混合检索不应调用聊天模型选择记忆")

        memories = LongTermMemoryRetriever(generate_text=never_called,
            store=_MemoryStore((selected, disabled, other_book)), retriever=self.retriever)
        result = await memories.retrieve(query="遭到背叛", book_id="current-book")
        self.assertEqual(tuple(item.memory_id for item in result), ("allowed",))

    async def test_canon_tool_returns_summary_sources_and_original_fact_status(self) -> None:
        state = create_initial_state()
        historical = replace(state.facts[0], value="同伴向敌方泄露行踪", valid_until_chapter=1)
        project = NovelProject(metadata=create_metadata(), foundation=create_foundation(), state=replace(state, facts=(historical,)))
        snapshot = ReviewSnapshot(project, (ChapterSummary(1, "同伴向敌方泄露行踪"),))
        tool = CanonEvidenceSearchTool(snapshot, self.retriever)
        result = await tool.execute({"query": "遭到背叛", "limit": 5})
        self.assertEqual(result["facts"][0]["status"], "superseded")
        self.assertEqual(result["facts"][0]["source_chapter"], historical.source_chapter)
        self.assertEqual(result["chapter_summaries"][0]["source_id"], "chapter:1")
        self.assertEqual(result["retrieval"]["mode"], "hybrid")
        hooks = await tool.execute({"query": "遭到背叛", "foreshadowing_only": True})
        self.assertEqual(hooks["facts"], [])
        self.assertEqual(hooks["chapter_summaries"], [])
        tools = build_sdk_read_tools(snapshot=snapshot, retriever=self.retriever, max_tool_calls=1,
                                     result_token_limit=4000, tool_names=("search_canon_evidence",))
        invoked = json.loads(await tools[0].on_invoke_tool(None, '{"query":"遭到背叛"}'))
        self.assertEqual(invoked["retrieval"]["mode"], "hybrid")


class EmbeddingApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_sends_explicit_config_and_respects_response_indices(self) -> None:
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"data": [
                {"index": 1, "embedding": [0, 2]}, {"index": 0, "embedding": [3, 0]},
            ]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        settings = EmbeddingSettings("https://example.invalid/v1", "test-model", "private", dimensions=2)
        with patch("storyweaver.retrieval.embeddings.httpx.AsyncClient", return_value=client):
            result = await ApiEmbedder(settings).embed(["甲", "乙"])
        self.assertEqual(result, [[1, 0], [0, 1]])
        self.assertEqual(str(requests[0].url), "https://example.invalid/v1/embeddings")
        self.assertEqual(requests[0].headers["authorization"], "Bearer private")
        self.assertEqual(json.loads(requests[0].content)["dimensions"], 2)
        self.assertNotIn("private", repr(settings))

    async def test_duplicate_response_indices_are_rejected(self) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": [
            {"index": 0, "embedding": [1, 0]}, {"index": 0, "embedding": [0, 1]},
        ]})))
        with patch("storyweaver.retrieval.embeddings.httpx.AsyncClient", return_value=client):
            with self.assertRaises(ValueError):
                await ApiEmbedder(EmbeddingSettings("https://example.invalid/v1", "model")).embed(["甲", "乙"])

    def test_vector_validation_rejects_count_dimension_and_nonfinite_values(self) -> None:
        for vectors, count in (([], 1), ([[1, 0], [1]], 2), ([[0, 0]], 1), ([[float("inf")]], 1), ([[True]], 1)):
            with self.subTest(vectors=vectors), self.assertRaises(ValueError):
                validate_vectors(vectors, count)

    async def test_runtime_uses_catalog_and_ignores_old_embedding_environment(self) -> None:
        from storyweaver.model_config import ModelCatalog
        from storyweaver.model_config.store import EmbeddingConfig

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
            "STORYWEAVER_EMBEDDING_BASE_URL": "https://old.invalid/v1",
            "STORYWEAVER_EMBEDDING_MODEL": "old-model",
            "STORYWEAVER_EMBEDDING_API_KEY": "old-key",
        }):
            path = Path(directory) / "storyweaver.db"
            self.assertIsNone(build_retriever(path).embedder)
            catalog = ModelCatalog(Path(directory) / "config")
            runtime = build_retriever(path, catalog)
            documents = (RetrievalDocument("record", "林默的故事"),)
            unconfigured = await runtime.search("林默", documents)
            self.assertEqual(unconfigured.degraded_reason, "embedding_unconfigured")
            self.assertFalse(path.with_name("storyweaver.retrieval.db").exists())
            catalog.set_embedding(EmbeddingConfig(base_url="http://127.0.0.1/v1", model_id="embed"))
            with patch("storyweaver.retrieval.runtime.VectorCache", side_effect=RuntimeError("missing extension")):
                fallback = await runtime.search("林默", documents)
            self.assertEqual(fallback.degraded_reason, "retrieval_configuration_error")
            self.assertEqual(fallback.document_ids, ("record",))

    async def test_catalog_changes_apply_next_search_and_running_search_keeps_snapshot(self) -> None:
        from storyweaver.model_config import ModelCatalog
        from storyweaver.model_config.store import EmbeddingConfig

        started = asyncio.Event()
        release = asyncio.Event()
        instances = []

        class ConfiguredFake(FakeEmbedder):
            def __init__(self, settings):
                super().__init__()
                self.settings = settings
                self.profile = settings.profile
                instances.append(self)

            async def embed(self, texts):
                if self.settings.model == "embed-a":
                    started.set()
                    await release.wait()
                return await super().embed(texts)

        with tempfile.TemporaryDirectory() as directory:
            catalog = ModelCatalog(Path(directory) / "config")
            catalog.set_embedding(EmbeddingConfig(base_url="https://first.invalid/v1", model_id="embed-a"), api_key="first-secret")
            runtime = build_retriever(Path(directory) / "storyweaver.db", catalog)
            documents = (RetrievalDocument("semantic", "同伴向敌方泄露行踪"),)
            with patch("storyweaver.retrieval.runtime.ApiEmbedder", ConfiguredFake):
                first = asyncio.create_task(runtime.search("遭到背叛", documents))
                await started.wait()
                catalog.set_embedding(EmbeddingConfig(base_url="https://second.invalid/v1", model_id="embed-b"), api_key="second-secret")
                second = await runtime.search("遭到背叛", documents)
                release.set()
                first_result = await first
                self.assertEqual(first_result.mode, "hybrid")
                self.assertEqual(second.document_ids, ("semantic",))
                self.assertEqual(instances[0].settings.api_key, "first-secret")
                self.assertEqual(instances[1].settings.api_key, "second-secret")
                self.assertNotEqual(instances[0].profile, instances[1].profile)
                catalog.set_embedding(None)
                disabled = await runtime.search("遭到背叛", documents)
                self.assertEqual(disabled.mode, "keyword")
                self.assertEqual(disabled.document_ids, ())


if __name__ == "__main__":
    unittest.main()
