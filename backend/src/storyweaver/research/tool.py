"""把 MCP 搜索包装成受治理的 FunctionTool，并记录真实执行轨迹。

执行记录取自本模块的 `SearchRecorder`，而不是模型可见的（可能被截断的）
工具返回文本，因此来源登记不会因为 token 预算而丢字段。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass

from agents.tool import FunctionTool
from pydantic import BaseModel, Field

from ..llm.tool_runtime import (
    ReadToolSpec,
    ToolCompletedCallback,
    ToolRuntimePolicy,
    build_read_tools,
)
from .mcp_provider import TavilyMcpSession
from .provider import RawHit

_MAX_RECORDS = 12


class WebSearchArguments(BaseModel):
    """检索工具的唯一入参形态；参数由后端定义，模型无法扩展。"""

    query: str = Field(
        min_length=2,
        max_length=200,
        description=(
            "检索词，写一句自然语言问题或完整短语，一次只问一件事，"
            "例如「刘慈欣写过哪些科幻小说」「元丰改制后宰执的名称有什么变化」。"
            "不要用空格把多个关键词堆在一起，也不要在一次检索里塞进多个问题"
        ),
    )
    max_results: int = Field(
        default=5,
        ge=1,
        le=8,
        description="返回条数，默认 5。",
    )


@dataclass(frozen=True, slots=True)
class SearchRecord:
    """一次检索的真实执行结果。"""

    index: int
    query: str
    status: str
    elapsed_seconds: float
    hits: tuple[RawHit, ...] = ()
    error: str | None = None
    # 预算耗尽被拒绝的调用不是一次真实检索，前端统计时要排除。
    exhausted: bool = False

    def to_data(self) -> dict[str, object]:
        return {
            "index": self.index,
            "query": self.query,
            "status": self.status,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "hit_count": len(self.hits),
            "hits": [
                {"title": hit["title"], "url": hit["url"], "site": hit["site"]}
                for hit in self.hits
            ],
            "error": self.error,
            "exhausted": self.exhausted,
        }


class SearchRecorder:
    """按调用顺序累积执行记录，供消息 metadata 与来源登记使用。"""

    def __init__(self, *, limit: int = _MAX_RECORDS) -> None:
        self._limit = max(1, limit)
        self._records: list[SearchRecord] = []

    def add_success(
        self, query: str, hits: list[RawHit], elapsed_seconds: float
    ) -> SearchRecord:
        return self._append(
            SearchRecord(
                index=len(self._records) + 1,
                query=query,
                status="success" if hits else "empty",
                elapsed_seconds=elapsed_seconds,
                hits=tuple(hits),
            )
        )

    def add_failure(
        self,
        query: str,
        error: str | None,
        elapsed_seconds: float,
        *,
        exhausted: bool = False,
    ) -> SearchRecord:
        return self._append(
            SearchRecord(
                index=len(self._records) + 1,
                query=query,
                status="failed",
                elapsed_seconds=elapsed_seconds,
                error=error,
                exhausted=exhausted,
            )
        )

    @property
    def records(self) -> tuple[SearchRecord, ...]:
        return tuple(self._records)

    def to_data(self) -> list[dict[str, object]]:
        return [record.to_data() for record in self._records]

    def __len__(self) -> int:
        return len(self._records)

    def _append(self, record: SearchRecord) -> SearchRecord:
        if len(self._records) < self._limit:
            self._records.append(record)
        return record


def build_web_search_tool(
    *,
    session: TavilyMcpSession,
    recorder: SearchRecorder,
    policy: ToolRuntimePolicy,
    default_max_results: int = 5,
    evidence: list[dict[str, object]] | None = None,
    on_completed: ToolCompletedCallback | None = None,
) -> FunctionTool:
    """构造唯一对外暴露的 `web_search` 工具。"""

    async def execute(arguments: Mapping[str, object]) -> object:
        query = str(arguments.get("query") or "").strip()
        raw_limit = arguments.get("max_results")
        max_results = int(raw_limit) if isinstance(raw_limit, int) else default_max_results
        started_at = time.perf_counter()
        hits = await session.search(query=query, max_results=max_results)
        recorder.add_success(query, hits, time.perf_counter() - started_at)
        return {
            "query": query,
            "count": len(hits),
            "results": [
                {
                    "title": hit["title"],
                    "url": hit["url"],
                    "site": hit["site"],
                    "snippet": hit["snippet"],
                }
                for hit in hits
            ],
        }

    async def handle_completed(
        tool_name: str,
        call_index: int,
        succeeded: bool,
        exhausted: bool,
        elapsed_seconds: float,
        error: str | None,
        duplicate: bool,
        arguments: Mapping[str, object],
    ) -> None:
        if not duplicate:
            if exhausted:
                recorder.add_failure(
                    str(arguments.get("query") or ""),
                    "本次调研的检索次数已用尽",
                    elapsed_seconds,
                    exhausted=True,
                )
            elif not succeeded:
                recorder.add_failure(
                    str(arguments.get("query") or ""), error or "检索失败", elapsed_seconds
                )
        if on_completed is not None:
            await on_completed(
                tool_name,
                call_index,
                succeeded,
                exhausted,
                elapsed_seconds,
                error,
                duplicate,
                arguments,
            )

    spec = ReadToolSpec(
        name="web_search",
        description=(
            "联网检索公开网页，返回标题、链接与摘要。仅用于补充真实世界资料，"
            "不用于改写用户已有作品内容。一次只提交一个检索问题；"
            "同一问题换关键词反复检索不会带来新资料。"
        ),
        input_model=WebSearchArguments,
        execute=execute,
    )
    tools = build_read_tools(
        specs=(spec,),
        policy=policy,
        on_completed=handle_completed,
        evidence=evidence,
    )
    return tools[0]
