"""检索运行时从现有模型目录读取配置，下次检索自动生效。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .embeddings import ApiEmbedder, EmbeddingSettings
from .hybrid import HybridRetriever, RetrievalDocument, RetrievalResult
from .vector_cache import VectorCache


if TYPE_CHECKING:
    from ..model_config import ModelCatalog

_LOGGER = logging.getLogger(__name__)


class CatalogRetriever(HybridRetriever):
    """每次查询解析配置，运行中的查询继续使用已选定的模型快照。"""

    def __init__(self, database_path: Path, catalog: ModelCatalog) -> None:
        super().__init__()
        self.path = database_path.with_name(database_path.stem + ".retrieval.db")
        self.catalog = catalog
        self._settings: EmbeddingSettings | None = None
        self._active: HybridRetriever = HybridRetriever()
        self._cache: VectorCache | None = None

    async def search(self, query: str, documents: tuple[RetrievalDocument, ...], *, limit: int = 5,
                     timeout_seconds: float | None = None) -> RetrievalResult:
        try:
            settings = self.catalog.embedding_settings()
            if settings is None:
                self._settings = None
                self._active = HybridRetriever()
            elif settings != self._settings:
                # 缓存只在首次启用语义检索时初始化，不修改作品数据库。
                if self._cache is None:
                    self._cache = VectorCache(self.path)
                self._active = HybridRetriever(
                    embedder=ApiEmbedder(settings), cache=self._cache,
                    timeout_seconds=settings.timeout_seconds, minimum_similarity=settings.minimum_similarity,
                )
                self._settings = settings
            active = self._active
        except Exception as exc:
            # 配置或扩展加载失败均不得中断创作，且不记录异常中的密钥和地址。
            _LOGGER.warning("检索配置不可用，使用关键词：%s", type(exc).__name__)
            active = HybridRetriever(unavailable_reason="retrieval_configuration_error")
        return await active.search(query, documents, limit=limit, timeout_seconds=timeout_seconds)


def build_retriever(database_path: Path | None, catalog: ModelCatalog | None = None) -> HybridRetriever:
    if catalog is None:
        return HybridRetriever()
    if database_path is None:
        return HybridRetriever(unavailable_reason="persistent_cache_unavailable")
    return CatalogRetriever(database_path, catalog)
