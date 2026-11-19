"""来源登记与报告后处理。

报告阶段直接流式输出 Markdown，前端因此能边生成边阅读；`finalize_report`
在文本落地前做三件事，把「自由写作」重新约束回可信内容：

1. 引用编号只能来自本次登记表，未知编号与自造链接都会被清掉；
2. `[来源 3]` / `[3]` 统一改写成指向真实网页的角标 `[3](url)`；
3. 去掉模型自作主张追加的「资料来源」清单——来源汇总由前端折叠块承担。

标题与链接始终由 `SourceRegistry` 回填，模型没有伪造来源的通道。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .provider import RawHit

# 模型写 `[来源 3]`、`[来源 3、5]` 或 `[3]` 都接受；后面紧跟 `(` 的是
# 正常 Markdown 链接，不能当成引用标记吃掉。来源上限 12，编号只取 1-2 位，
# 避免把 `[1082]` 这类纪年误认成引用。
_CITATION_RE = re.compile(r"\[(?:来源\s*)?(\d{1,2}(?:\s*[,，、]\s*\d{1,2})*)\](?!\()")
_FENCE_RE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*?)\n```\s*$", re.DOTALL)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]*)\]\((https?://[^)\s]+)\)")
_BARE_URL_RE = re.compile(r"https?://[^\s)>\]]+")
# 模型偶尔会照旧习惯补一段「资料来源」；这段由前端折叠块接管，正文里要删掉。
_SOURCE_SECTION_RE = re.compile(
    r"^#{1,6}\s*(?:资料)?(?:来源|参考来源|参考链接|来源清单)\s*$"
)


@dataclass(frozen=True, slots=True)
class RegisteredSource:
    """一条已编号的真实来源。"""

    index: int
    title: str
    url: str
    site: str

    def to_data(self) -> dict[str, object]:
        return {
            "index": self.index,
            "title": self.title,
            "url": self.url,
            "site": self.site,
        }


class SourceRegistry:
    """按 URL 去重并按命中顺序编号的来源登记表。"""

    def __init__(self, *, max_sources: int = 12) -> None:
        if max_sources < 1:
            raise ValueError("max_sources 必须大于 0")
        self._max_sources = max_sources
        self._items: list[RegisteredSource] = []
        self._by_url: dict[str, RegisteredSource] = {}

    def register(self, hits: Sequence[RawHit]) -> list[RegisteredSource]:
        added: list[RegisteredSource] = []
        for hit in hits:
            url = str(hit.get("url") or "").strip()
            if not url or url in self._by_url:
                continue
            if len(self._items) >= self._max_sources:
                break
            source = RegisteredSource(
                index=len(self._items) + 1,
                title=str(hit.get("title") or url),
                url=url,
                site=str(hit.get("site") or ""),
            )
            self._by_url[url] = source
            self._items.append(source)
            added.append(source)
        return added

    @property
    def sources(self) -> tuple[RegisteredSource, ...]:
        return tuple(self._items)

    def has(self, index: int) -> bool:
        return 1 <= index <= len(self._items)

    def get(self, index: int) -> RegisteredSource | None:
        if not self.has(index):
            return None
        return self._items[index - 1]

    def to_data(self) -> list[dict[str, object]]:
        return [source.to_data() for source in self._items]

    def __len__(self) -> int:
        return len(self._items)


@dataclass(frozen=True, slots=True)
class FinalReport:
    """流式正文落地前的最终形态。"""

    markdown: str
    title: str
    cited: tuple[RegisteredSource, ...] = ()

    @property
    def has_citations(self) -> bool:
        return bool(self.cited)


def finalize_report(
    text: str,
    registry: SourceRegistry,
    *,
    topic: str = "",
) -> FinalReport:
    """把流式生成的 Markdown 收敛成可展示、可追溯的正文。

    这里不再要求模型交付结构化对象：模型写的是给人读的 Markdown，
    校验与来源回填都在文本层完成，流式输出才有可能实现。
    """

    body = _strip_fence(str(text or "")).strip()
    if not body:
        raise ValueError("报告正文为空")

    body = _strip_source_section(body)
    body = _neutralize_urls(body, registry)
    cited = _cited_sources(body, registry)
    body = _render_citations(body, registry)
    body, title = _ensure_title(body, topic)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return FinalReport(
        markdown=body.rstrip() + "\n",
        title=title,
        cited=cited,
    )


def _strip_fence(text: str) -> str:
    """模型偶尔会用 ```markdown 包住整篇；去掉围栏再渲染。"""

    match = _FENCE_RE.match(text)
    return match.group(1) if match else text


def _strip_source_section(text: str) -> str:
    """删掉模型追加的「资料来源 / 参考链接」小节。"""

    kept: list[str] = []
    for line in text.splitlines():
        if _SOURCE_SECTION_RE.match(line.strip()):
            break
        kept.append(line)
    return "\n".join(kept).rstrip()


def _neutralize_urls(text: str, registry: SourceRegistry) -> str:
    """清掉模型自造的链接：未登记 URL 只保留可读文字。"""

    allowed = {source.url for source in registry.sources}

    def replace_link(match: re.Match[str]) -> str:
        url = match.group(2)
        return match.group(0) if url in allowed else match.group(1)

    text = _MARKDOWN_LINK_RE.sub(replace_link, text)
    return _BARE_URL_RE.sub(
        lambda match: match.group(0) if match.group(0) in allowed else "",
        text,
    )


def _cited_sources(text: str, registry: SourceRegistry) -> tuple[RegisteredSource, ...]:
    """正文实际引用到的来源，按编号升序去重。"""

    used: set[int] = set()
    for match in _CITATION_RE.finditer(text):
        for part in re.split(r"[,，、]", match.group(1)):
            part = part.strip()
            if part.isdigit() and registry.has(int(part)):
                used.add(int(part))
    return tuple(source for source in registry.sources if source.index in used)


def _render_citations(text: str, registry: SourceRegistry) -> str:
    """把 `[来源 3]` / `[3]` 换成可点击的数字角标，链接取自登记表。"""

    def replace(match: re.Match[str]) -> str:
        links: list[str] = []
        for part in re.split(r"[,，、]", match.group(1)):
            part = part.strip()
            if not part.isdigit():
                continue
            source = registry.get(int(part))
            if source is None:
                continue
            links.append(f"[{int(part)}]({source.url})")
        # 未知编号直接丢弃，避免正文留下指向不存在来源的标记。
        return "".join(links)

    return _CITATION_RE.sub(replace, text)


def _ensure_title(text: str, topic: str) -> tuple[str, str]:
    """保证正文以 `## 标题` 开头；模型漏写时用调研主题补一个。"""

    lines = text.splitlines()
    for position, line in enumerate(lines):
        match = re.match(r"^(#{1,2})\s+(.+?)\s*$", line)
        if match is None:
            continue
        title = match.group(2).strip()
        if match.group(1) == "#":
            lines[position] = f"## {title}"
        return "\n".join(lines).strip(), title

    fallback = short_topic(topic)
    return f"## {fallback}\n\n{text}".strip(), fallback


def short_topic(topic: str, limit: int = 28) -> str:
    """把用户 objective 压成可展示的调研主题。

    objective 往往是一整句指令（“请检索并核实宋代……的资料整理”），直接当标题
    会把提示词原文泄漏到正文。这里去掉指令性前缀并截到第一句。
    """

    value = " ".join(str(topic or "").split())
    for prefix in (
        "请检索并核实", "请检索", "请帮我查", "帮我查一下", "帮我查",
        "请查询", "查询", "检索",
    ):
        if value.startswith(prefix):
            value = value[len(prefix):].lstrip("：: ，,")
            break
    for separator in ("。", "；", ";"):
        if separator in value:
            value = value.split(separator, 1)[0]
            break
    value = value.strip()
    if len(value) > limit:
        value = value[:limit].rstrip("，,、 ") + "…"
    return value or "本次检索"
