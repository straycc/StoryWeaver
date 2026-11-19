"""联网调研配置，支持从项目根目录的 .env 读取 Tavily 密钥。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_MCP_URL = "https://mcp.tavily.com/mcp/"
_DOTENV_PATH = Path(__file__).resolve().parents[4] / ".env"
_TAVILY_KEY_NAME = "STORYWEAVER_TAVILY_API_KEY"


def _env_text(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _tavily_api_key() -> str | None:
    if _TAVILY_KEY_NAME in os.environ:
        return _env_text(_TAVILY_KEY_NAME)
    values = dotenv_values(_DOTENV_PATH, interpolate=False, encoding="utf-8-sig")
    return (values.get(_TAVILY_KEY_NAME) or "").strip() or None


def _env_float(name: str, default: float) -> float:
    raw = _env_text(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = _env_text(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True, slots=True)
class ResearchSettings:
    """一次调研链路的预算与接入参数。

    预算按「常规搜索工具」取值：单次调用 30 秒、单轮最多 5 次检索、
    单次结果 4000 token。相较 Agent 内部工具更宽松，因为联网检索的
    网络抖动显著大于本地只读查询。
    """

    api_key: str | None = field(default=None, repr=False)
    mcp_url: str = DEFAULT_MCP_URL
    max_searches: int = 5
    per_search_timeout_seconds: float = 30.0
    total_timeout_seconds: float = 150.0
    # 分节报告比条目清单长得多，报告阶段要给足时间，否则会退化成
    # 「自动整理未完成」的来源清单。
    report_reserve_seconds: float = 55.0
    max_results_per_search: int = 5
    result_token_limit: int = 4000
    evidence_token_budget: int = 12_000
    max_sources: int = 12

    def __post_init__(self) -> None:
        if self.max_searches < 1:
            raise ValueError("max_searches 必须大于 0")
        if self.per_search_timeout_seconds <= 0:
            raise ValueError("per_search_timeout_seconds 必须大于 0")
        if self.report_reserve_seconds >= self.total_timeout_seconds:
            raise ValueError("report_reserve_seconds 必须小于 total_timeout_seconds")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def search_budget_seconds(self) -> float:
        return self.total_timeout_seconds - self.report_reserve_seconds

    @property
    def max_research_turns(self) -> int:
        """检索阶段的模型回合上限：每次检索一轮，外加一轮收尾。"""

        return self.max_searches + 1

    @classmethod
    def from_env(cls) -> ResearchSettings:
        return cls(
            api_key=_tavily_api_key(),
            mcp_url=_env_text("STORYWEAVER_TAVILY_MCP_URL") or DEFAULT_MCP_URL,
            max_searches=_env_int("STORYWEAVER_RESEARCH_MAX_SEARCHES", 5),
            per_search_timeout_seconds=_env_float(
                "STORYWEAVER_RESEARCH_TIMEOUT_SECONDS", 30.0
            ),
            total_timeout_seconds=_env_float("STORYWEAVER_RESEARCH_TOTAL_SECONDS", 150.0),
            report_reserve_seconds=_env_float(
                "STORYWEAVER_RESEARCH_REPORT_RESERVE_SECONDS", 55.0
            ),
            max_results_per_search=_env_int("STORYWEAVER_RESEARCH_MAX_RESULTS", 5),
            result_token_limit=_env_int("STORYWEAVER_RESEARCH_RESULT_TOKENS", 4000),
            evidence_token_budget=_env_int("STORYWEAVER_RESEARCH_EVIDENCE_TOKENS", 12_000),
            max_sources=_env_int("STORYWEAVER_RESEARCH_MAX_SOURCES", 12),
        )
