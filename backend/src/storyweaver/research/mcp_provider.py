"""Tavily 远端 MCP 的会话封装。

这里只借用 SDK 的 MCP 传输与 JSON-RPC 编解码，**不**把服务器工具清单
交给模型（那需要 `Agent(mcp_servers=[...])`，工具面由模型决定）。检索
由后端显式调用 `call_tool`，因此工具面恒为一个搜索函数，预算与截断
仍由 `ToolRuntimePolicy` 治理。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from .config import ResearchSettings
from .provider import (
    RawHit,
    ResearchAuthError,
    ResearchNotConfigured,
    ResearchTransportError,
)

_logger = logging.getLogger(__name__)

_SEARCH_TOOL_CANDIDATES = ("tavily_search", "tavily-search")
_SNIPPET_LIMIT = 300
_AUTH_TOKENS = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "invalid api key",
    "authentication",
    "permission",
)


class TavilyMcpSession:
    """一次调研复用一个 MCP 会话；构造前必须已完成连接。"""

    def __init__(self, server: Any, tool_name: str) -> None:
        self._server = server
        self.tool_name = tool_name

    async def search(self, *, query: str, max_results: int) -> list[RawHit]:
        try:
            result = await self._server.call_tool(
                self.tool_name,
                {"query": query, "max_results": max_results},
            )
        except ResearchTransportError:
            raise
        except Exception as exc:
            raise _translate_error(exc) from None
        payload = _payload_from_result(result)
        return _normalize_hits(payload)


def _translate_error(exc: BaseException) -> BaseException:
    if isinstance(exc, TimeoutError):
        return TimeoutError("单次检索超时")
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(token in text for token in _AUTH_TOKENS):
        return ResearchAuthError("搜索服务鉴权失败，请检查 API Key 是否有效")
    return ResearchTransportError("搜索服务暂时不可用，请稍后重试")


def _payload_from_result(result: Any) -> Mapping[str, Any]:
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        raise ResearchTransportError("搜索服务返回错误结果")

    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if isinstance(structured, Mapping):
        return structured

    for block in getattr(result, "content", None) or ():
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        decoded = _decode_json_object(text)
        if decoded is not None:
            return decoded

    raise ResearchTransportError("搜索结果格式无法解析")


def _decode_json_object(text: str) -> Mapping[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            decoded = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return decoded if isinstance(decoded, Mapping) else None


def _normalize_hits(payload: Mapping[str, Any]) -> list[RawHit]:
    raw_items = payload.get("results")
    if not isinstance(raw_items, list):
        raise ResearchTransportError("搜索结果缺少 results 字段")

    hits: list[RawHit] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        seen.add(url)
        title = str(item.get("title") or "").strip() or parsed.netloc
        snippet = str(item.get("content") or item.get("snippet") or "").strip()
        hits.append(
            RawHit(
                title=title,
                url=url,
                site=parsed.netloc.removeprefix("www."),
                snippet=snippet[:_SNIPPET_LIMIT],
            )
        )
    return hits


async def _resolve_search_tool(server: Any) -> str:
    try:
        tools = await server.list_tools()
    except Exception as exc:
        raise _translate_error(exc) from None
    names = [str(getattr(tool, "name", "")) for tool in tools]
    for candidate in _SEARCH_TOOL_CANDIDATES:
        if candidate in names:
            return candidate
    for name in names:
        if "search" in name.lower():
            return name
    raise ResearchTransportError("搜索服务未提供可用的搜索工具")


def _build_server(settings: ResearchSettings, http_cls: Any) -> Any:
    """构造 MCP 服务器对象；旧版 SDK 缺新参数时退回最小配置。"""

    common = {
        "name": "tavily",
        "params": {
            "url": settings.mcp_url,
            "headers": {"Authorization": f"Bearer {settings.api_key}"},
        },
    }
    try:
        return http_cls(
            **common,
            cache_tools_list=True,
            client_session_timeout_seconds=settings.per_search_timeout_seconds,
            # 流式 HTTP 的偶发断连由 SDK 自己退避重试；首次建连最脆弱，
            # 冷启动要做 DNS + TLS + initialize + tools/list 四步。
            max_retry_attempts=2,
            retry_backoff_seconds_base=0.6,
        )
    except TypeError:
        return http_cls(**common)


async def _quiet_cleanup(server: Any) -> None:
    try:
        await server.cleanup()
    except Exception:  # noqa: BLE001 - 清理失败不应掩盖原始错误
        _logger.debug("MCP 会话清理失败", exc_info=True)


@asynccontextmanager
async def open_mcp_session(settings: ResearchSettings) -> AsyncIterator[TavilyMcpSession]:
    """按需建立 MCP 会话；未配置或依赖缺失时给出可展示的失败。

    建连阶段单独重试一次：实测首次连接约 5.4 秒（DNS + TLS + initialize +
    tools/list），第二次起 1.5 秒；冷启动偶发失败会直接退化成「本次没有
    得到外部资料」，而重试的成本只有一两秒。
    """

    if not settings.configured:
        raise ResearchNotConfigured("尚未配置联网搜索服务")

    try:
        from agents.mcp import MCPServerStreamableHttp
    except ImportError:
        raise ResearchNotConfigured("联网检索依赖未安装") from None

    for attempt in (1, 2):
        server = _build_server(settings, MCPServerStreamableHttp)
        try:
            await server.connect()
            tool_name = await _resolve_search_tool(server)
        except Exception as exc:
            await _quiet_cleanup(server)
            translated = _translate_error(exc)
            if isinstance(translated, ResearchAuthError):
                raise translated from None
            # 真实异常只进日志，不回给用户；否则排查时只能看到“暂时不可用”。
            _logger.warning(
                "MCP 建连失败 attempt=%d/%d error=%s: %s",
                attempt, 2, type(exc).__name__, exc,
            )
            if attempt == 1:
                await asyncio.sleep(0.8)
                continue
            raise translated from None

        try:
            yield TavilyMcpSession(server, tool_name)
        finally:
            await _quiet_cleanup(server)
        return
