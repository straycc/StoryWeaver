"""从模型目录解析的 OpenAI 兼容 Embedding API。"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

import httpx


@dataclass(frozen=True, slots=True)
class EmbeddingSettings:
    base_url: str
    model: str
    api_key: str = field(default="", repr=False)
    dimensions: int | None = None
    timeout_seconds: float = 5.0
    minimum_similarity: float = 0.35

    def __post_init__(self) -> None:
        if self.dimensions is not None and self.dimensions < 1:
            raise ValueError("Embedding dimensions 必须大于 0")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Embedding 超时必须为有限正数")
        if not math.isfinite(self.minimum_similarity) or not -1 <= self.minimum_similarity <= 1:
            raise ValueError("最小语义相似度必须位于 -1 到 1")

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def profile(self) -> str:
        # 地址和模型共同确定向量空间；密钥不进入索引和日志。
        value = json.dumps([self.base_url.rstrip("/"), self.model, self.dimensions])
        return hashlib.sha256(value.encode()).hexdigest()


def validate_vectors(vectors: list[list[float]], count: int, dimensions: int | None = None) -> list[list[float]]:
    """拒绝缺项、混合维度、零向量及无法存入 float32 的数值。"""
    if len(vectors) != count:
        raise ValueError("Embedding 返回数量与输入不一致")
    result = []
    for vector in vectors:
        if not vector or (dimensions is not None and len(vector) != dimensions):
            raise ValueError("Embedding 返回维度不一致")
        dimensions = len(vector)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in vector):
            raise ValueError("Embedding 必须包含数值")
        values = [float(value) for value in vector]
        if any(not math.isfinite(value) or abs(value) > 3.4028234e38 for value in values):
            raise ValueError("Embedding 包含无效数值")
        if not any(abs(value) >= 1.1754944e-38 for value in values):
            raise ValueError("Embedding 不能是零向量")
        norm = math.hypot(*values)
        result.append([value / norm for value in values])
    return result


class ApiEmbedder:
    """每次调用独立关闭连接，不自动重试以免超出检索时间预算。"""

    def __init__(self, settings: EmbeddingSettings) -> None:
        self.settings = settings
        self.profile = settings.profile

    async def embed(self, texts: list[str]) -> list[list[float]]:
        body: dict[str, object] = {"model": self.settings.model, "input": texts, "encoding_format": "float"}
        if self.settings.dimensions is not None:
            body["dimensions"] = self.settings.dimensions
        headers = {"Authorization": f"Bearer {self.settings.api_key}"} if self.settings.api_key else {}
        async with httpx.AsyncClient(timeout=self.settings.timeout_seconds) as client:
            response = await client.post(f"{self.settings.base_url.rstrip('/')}/embeddings", json=body, headers=headers)
            response.raise_for_status()
            data = response.json()["data"]
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError("Embedding 响应格式错误")
        indices = [item.get("index") for item in data]
        if any(type(index) is not int for index in indices) or sorted(indices) != list(range(len(texts))):
            raise ValueError("Embedding 响应索引缺失或重复")
        ordered = sorted(data, key=lambda item: item["index"])
        return validate_vectors([item["embedding"] for item in ordered], len(texts), self.settings.dimensions)
