"""双路召回、排名融合及有界的语义降级。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Protocol

from .embeddings import validate_vectors
from .vector_cache import VectorCache

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RetrievalDocument:
    document_id: str
    text: str

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    document_ids: tuple[str, ...]
    mode: str
    degraded_reason: str | None = None

    def metadata(self) -> dict[str, object]:
        return {"mode": self.mode, "degraded_reason": self.degraded_reason}


class Embedder(Protocol):
    profile: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text.casefold())


def _terms(text: str) -> set[str]:
    terms = set(re.findall(r"[a-z0-9_]+", text.casefold()))
    for block in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(block[index:index + 2] for index in range(len(block) - 1))
        if len(block) == 1:
            terms.add(block)
    return terms


def keyword_rank(query: str, documents: tuple[RetrievalDocument, ...]) -> list[str]:
    """优先完整子串，其次中文双字词与英文词交集，保留短人名召回。"""
    normalized = _normalize(query)
    terms = _terms(query)
    scored = []
    for index, document in enumerate(documents):
        overlap = len(terms & _terms(document.text))
        exact = bool(normalized and normalized in _normalize(document.text))
        if exact or overlap:
            scored.append((int(exact), overlap / max(1, len(terms)), -index, document.document_id))
    return [document_id for _, _, _, document_id in sorted(scored, reverse=True)]


class HybridRetriever:
    """关键词始终参与排名；未配置、超时或索引失败时保留关键词结果。"""

    def __init__(self, *, embedder: Embedder | None = None, cache: VectorCache | None = None,
                 timeout_seconds: float = 5.0, minimum_similarity: float = 0.35,
                 unavailable_reason: str = "embedding_unconfigured") -> None:
        self.embedder = embedder
        self.cache = cache
        self.timeout_seconds = timeout_seconds
        self.minimum_similarity = minimum_similarity
        self.unavailable_reason = unavailable_reason
        self._semantic_lock = asyncio.Lock()

    async def search(self, query: str, documents: tuple[RetrievalDocument, ...], *, limit: int = 5,
                     timeout_seconds: float | None = None) -> RetrievalResult:
        if limit < 1:
            raise ValueError("检索 limit 必须大于 0")
        if len({item.document_id for item in documents}) != len(documents):
            raise ValueError("检索候选 ID 不能重复")
        if not query.strip() or not documents:
            return RetrievalResult((), "keyword")
        keywords = keyword_rank(query, documents)
        fallback = tuple(keywords[:limit])
        if self.embedder is None or self.cache is None:
            return RetrievalResult(fallback, "keyword", self.unavailable_reason)
        reason = None
        try:
            # 锁等待、查询向量和首次索引共用预算；取消信号继续传到调用方。
            budget = min(self.timeout_seconds, timeout_seconds) if timeout_seconds is not None else self.timeout_seconds
            async with asyncio.timeout(budget):
                async with self._semantic_lock:
                    semantic = await self._semantic_search(query, documents, max(20, limit * 4))
        except TimeoutError:
            reason = "semantic_timeout"
        except Exception as exc:
            reason = "semantic_error"
            # 不输出异常正文，避免 API 地址、密钥或资料内容进入日志。
            _LOGGER.warning("语义检索降级为关键词：%s", type(exc).__name__)
        if reason:
            return RetrievalResult(fallback, "keyword", reason)
        # RRF 按排名融合，不直接相加不同量纲的关键词得分与余弦距离。
        scores: dict[str, float] = {}
        for ranking, weight in ((keywords[:max(20, limit * 4)], 1.25), (semantic, 1.0)):
            for rank, document_id in enumerate(ranking, 1):
                scores[document_id] = scores.get(document_id, 0.0) + weight / (60 + rank)
        positions = {item.document_id: index for index, item in enumerate(documents)}
        ranked = sorted(scores, key=lambda key: (-scores[key], positions[key]))
        return RetrievalResult(tuple(ranked[:limit]), "hybrid")

    async def _semantic_search(self, query: str, documents: tuple[RetrievalDocument, ...], limit: int) -> list[str]:
        assert self.embedder is not None and self.cache is not None
        # 查询也使用内容寻址缓存；文档或模型变更不会复用旧空间的向量。
        query_document = RetrievalDocument("query", query)
        all_documents = {item.content_hash: item for item in (*documents, query_document)}
        profile = self.embedder.profile
        cached = await asyncio.to_thread(self.cache.existing, profile, list(all_documents))
        missing = [item for digest, item in all_documents.items() if digest not in cached]
        dimensions = None
        for offset in range(0, len(missing), 32):
            batch = missing[offset:offset + 32]
            vectors = validate_vectors(await self.embedder.embed([item.text for item in batch]), len(batch), dimensions)
            dimensions = len(vectors[0])
            await asyncio.to_thread(self.cache.put, profile, [(item.content_hash, vector) for item, vector in zip(batch, vectors)])
        query_vector = await asyncio.to_thread(self.cache.get, profile, query_document.content_hash)
        hashes = list(dict.fromkeys(item.content_hash for item in documents))
        ranked_hashes = await asyncio.to_thread(self.cache.search, profile, hashes, query_vector, limit, self.minimum_similarity)
        by_hash: dict[str, list[str]] = {}
        for item in documents:
            by_hash.setdefault(item.content_hash, []).append(item.document_id)
        return [document_id for digest in ranked_hashes for document_id in by_hash[digest]][:limit]
