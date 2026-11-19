"""搜索能力的最小协议与失败分类。

只暴露「一次查询 → 若干结构化命中」，不泄漏底层传输形态。
异常一律不携带原始响应体与请求头，避免密钥出现在事件、日志或回复里。
"""

from __future__ import annotations

from typing import Protocol, TypedDict


class RawHit(TypedDict):
    """一条可登记的检索命中。"""

    title: str
    url: str
    site: str
    snippet: str


class SearchProvider(Protocol):
    """检索能力契约；实现可以是 MCP、HTTP 或测试替身。"""

    async def search(self, *, query: str, max_results: int) -> list[RawHit]:
        ...


class ResearchError(RuntimeError):
    """调研链路的失败基类；message 必须可直接展示给用户。"""


class ResearchNotConfigured(ResearchError):
    """未配置搜索服务，整条链路不启动。"""


class ResearchAuthError(ResearchError):
    """凭证无效或无权限。"""


class ResearchTransportError(ResearchError):
    """网络、协议或返回结构异常。"""
