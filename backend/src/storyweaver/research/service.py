"""调研编排：MCP 会话 → 检索 → 来源登记 → 报告 → Markdown。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from ..context.evidence import EvidenceCompiler, EvidencePackage
from ..llm.events import LlmEventSink, LlmEventType
from ..llm.sdk import WorkerSettings, _emit, run_text_worker
from ..llm.tool_runtime import ToolRuntimePolicy
from ..llm.two_phase import run_research
from .config import ResearchSettings
from .mcp_provider import open_mcp_session
from .provider import ResearchAuthError, ResearchNotConfigured, ResearchTransportError
from .report import SourceRegistry, finalize_report, short_topic
from .tool import SearchRecorder, build_web_search_tool

_FAILURE_TEXT: dict[str, str] = {
    "not_configured": "当前还没有配置联网搜索，暂时无法检索外部资料。",
    "auth": "联网搜索鉴权失败，请检查搜索服务的密钥是否有效。",
    "unavailable": "联网搜索暂时不可用，本次没有得到外部资料，请稍后重试。",
    "timeout": "检索超时，未能及时拿到外部资料。可以缩小问题范围后重试。",
    "empty": "这次没有检索到可用资料。可以换一种说法，或补充更具体的时间、地域与对象。",
    "error": "调研过程中出现异常，本次没有得到外部资料。",
}

# 报告阶段输出纯 Markdown，前端才能边生成边渲染。写法约束按「人能顺着读
# 下去的简报」来定：先给判断，再给支撑细节，小节按主题而不是按事实/建议来划分。
_REPORT_STYLE = """## 报告写法（必须遵守）

1. 直接输出 Markdown 正文。不要用代码块包裹，不要写「以下是」「希望对你
   有帮助」「综上所述」这类前言、后语或总结段。
2. 第一行是 `## 标题`：短名词短语，例如「宋代官制：官、职、差遣与二府三司」，
   不要写成一句话，也不要带序号。
3. 标题下面先用 1-2 句导语说明整体判断、或读者最该注意的一点。
4. 主体写 2-6 个 `### 一、小节标题`，序号用中文数字（一、二、三……）。
   小节按内容逻辑划分：可以按时间阶段、按机构/层级、按概念辨析、按常见误解，
   由资料本身决定。不要用「资料摘要」「要点一」「事实梳理」这类空标签，
   也不要按「事实 / 建议 / 结论」机械分类。
5. 每节先写判断句，再展开支撑细节。以段落为主；并列概念、机构对照、名词
   差异适合用表格；只在确实更清楚时用无序列表，不要整节都是 bullet。
6. 引用：每个来自资料的关键事实后面标 `[3]`（方括号内是来源编号），一处可以
   标多个（`[3][5]`）。不要写网址，不要写「资料来源」「参考链接」清单，
   不要复述检索过程。
7. 资料之间冲突、或只覆盖了一部分时，把分歧写清楚（「A 处说 X，B 处说 Y，
   差别在于……」），无法确认的部分放进最后的 `### 有待进一步核实`（无序列表）。
8. 结尾可以写 `### 对创作的启示`，2-4 条能直接落到小说里的用法：可感的细节、
   称呼与礼仪、可制造的矛盾点，避免「要注意考据」这类空话。
9. 语言是中文书面语，紧凑、客观。篇幅跟着资料走：资料少就写短，不要为了
   凑小节而注水。"""


@dataclass(frozen=True, slots=True)
class ResearchOutcome:
    """一次调研的最终产物；失败也返回可展示文案。"""

    status: str
    topic: str
    markdown: str
    searches: tuple[Mapping[str, object], ...] = ()
    sources: tuple[Mapping[str, object], ...] = ()
    elapsed_seconds: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"complete", "partial"}

    def to_metadata(self) -> dict[str, object]:
        data: dict[str, object] = {
            "status": self.status,
            "topic": self.topic,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "searches": [dict(item) for item in self.searches],
            "sources": [dict(item) for item in self.sources],
        }
        if self.error:
            data["error"] = self.error
        return {"research": data}


def _report_model_settings(model_settings: object) -> object:
    """报告阶段沿用结构化时期的低强度推理：正文长，思考过久会挤掉成稿时间。"""

    if model_settings is None:
        return None
    extra_body = dict(getattr(model_settings, "extra_body", None) or {})
    extra_body.setdefault("reasoning_effort", "low")
    return replace(model_settings, extra_body=extra_body)


class ResearchService:
    """一次调用完成一轮调研，不持有跨请求状态。"""

    def __init__(
        self,
        *,
        settings: ResearchSettings,
        worker_settings: WorkerSettings,
        event_sinks: Sequence[LlmEventSink] = (),
        tracing_enabled: bool = False,
    ) -> None:
        self._settings = settings
        self._worker_settings = worker_settings
        self._event_sinks = tuple(event_sinks)
        self._tracing_enabled = tracing_enabled

    async def run(self, *, topic: str, prompt: str) -> ResearchOutcome:
        settings = self._settings
        started_at = time.perf_counter()
        if not settings.configured:
            return self._failure("not_configured", topic, started_at)

        recorder = SearchRecorder(limit=settings.max_searches)
        registry = SourceRegistry(max_sources=settings.max_sources)
        evidence: list[dict[str, object]] = []
        search_timed_out = False

        # 阶段事件让前端在首个检索结果返回前就有明确反馈，
        # 而不是一直停在“正在思考”。
        await self._phase("search", 0)

        async def on_tool_completed(
            _tool_name: str,
            call_index: int,
            succeeded: bool,
            exhausted: bool,
            elapsed_seconds: float,
            error: str | None,
            duplicate: bool,
            arguments: Mapping[str, object],
        ) -> None:
            if duplicate or not self._event_sinks:
                return
            latest = recorder.records[-1] if len(recorder) else None
            hits = () if latest is None else latest.hits
            await _emit(
                self._event_sinks,
                LlmEventType.TOOL_COMPLETED,
                self._worker_settings.worker_id,
                {
                    "tool_name": "web_search",
                    "call_index": call_index,
                    "query": str(arguments.get("query") or ""),
                    "status": "success" if succeeded and not exhausted else "failed",
                    # 预算耗尽不是“检索了一次没结果”，前端要把它和真实检索分开计数。
                    "exhausted": exhausted,
                    "hit_count": len(hits),
                    "hits": [
                        {"title": hit["title"], "url": hit["url"], "site": hit["site"]}
                        for hit in hits
                    ],
                    "elapsed_seconds": round(elapsed_seconds, 2),
                    "error": error or ("本次检索次数已用尽" if exhausted else None),
                },
            )

        try:
            async with open_mcp_session(settings) as session:
                tool = build_web_search_tool(
                    session=session,
                    recorder=recorder,
                    policy=ToolRuntimePolicy(
                        max_tool_calls=settings.max_searches,
                        timeout_seconds=settings.per_search_timeout_seconds + 2,
                        result_token_limit=settings.result_token_limit,
                    ),
                    default_max_results=settings.max_results_per_search,
                    evidence=evidence,
                    on_completed=on_tool_completed,
                )
                search_settings = replace(
                    self._worker_settings,
                    timeout_seconds=settings.search_budget_seconds,
                )
                try:
                    await asyncio.wait_for(
                        run_research(
                            settings=search_settings,
                            prompt=prompt,
                            read_tools=[tool],
                            max_research_turns=settings.max_research_turns,
                            event_sinks=self._event_sinks,
                            tracing_enabled=self._tracing_enabled,
                        ),
                        timeout=settings.search_budget_seconds + 2,
                    )
                except TimeoutError:
                    search_timed_out = True
        except ResearchNotConfigured:
            return self._failure("not_configured", topic, started_at, recorder)
        except ResearchAuthError:
            return self._failure("auth", topic, started_at, recorder)
        except ResearchTransportError:
            return self._failure("unavailable", topic, started_at, recorder)
        except TimeoutError:
            search_timed_out = True
        except Exception:
            return self._failure("error", topic, started_at, recorder)

        for record in recorder.records:
            if record.hits:
                registry.register(record.hits)

        if not len(registry):
            kind = "timeout" if search_timed_out else "empty"
            return self._failure(kind, topic, started_at, recorder)

        await self._phase("report", len(recorder))
        report_settings = replace(
            self._worker_settings,
            name=f"{self._worker_settings.name}（报告）",
            instructions=_REPORT_STYLE,
            model_settings=_report_model_settings(  # type: ignore[arg-type]
                self._worker_settings.model_settings
            ),
            timeout_seconds=settings.report_reserve_seconds,
            max_turns=1,
        )
        try:
            package = EvidenceCompiler(
                token_budget=settings.evidence_token_budget
            ).compile(evidence)
            # 报告阶段直接流式产出 Markdown：TEXT_DELTA 会被 Job 事件投影到
            # LivePreviewHub，前端因此可以边生成边显示，而不是等整篇结束。
            raw = await asyncio.wait_for(
                run_text_worker(
                    settings=report_settings,
                    prompt=self._report_prompt(prompt, registry, package),
                    event_sinks=self._event_sinks,
                    tracing_enabled=self._tracing_enabled,
                ),
                timeout=settings.report_reserve_seconds,
            )
            report = finalize_report(raw, registry, topic=topic)
        except Exception as exc:
            # 报告阶段失败不能把原因吞掉：执行记录里要能看出是超时
            # 还是模型报错，否则只能反复重跑猜原因。
            return self._fallback(topic, registry, recorder, started_at, reason=str(exc))

        elapsed = time.perf_counter() - started_at
        # 没有一处引用说明模型漏标了来源：正文仍可读，但要如实标记为不完整，
        # 并把登记表里的来源全部列出，至少保住可追溯性。
        markdown = report.markdown
        status = "complete"
        if not report.has_citations:
            status = "partial"
            markdown = markdown.rstrip() + (
                "\n\n> 这份整理没有逐条标注来源，事实细节建议点开下方原文核对。\n"
            )
        return ResearchOutcome(
            status=status,
            topic=topic,
            markdown=markdown,
            searches=tuple(recorder.to_data()),
            # 只有正文引用过的网页进入「来源」汇总，避免堆砌未被使用的命中。
            sources=tuple(
                source.to_data()
                for source in (report.cited or registry.sources)
            ),
            elapsed_seconds=elapsed,
        )

    async def _phase(self, phase: str, search_count: int) -> None:
        """上报调研阶段切换；没有事件订阅者时是空操作。"""

        if not self._event_sinks:
            return
        await _emit(
            self._event_sinks,
            LlmEventType.PHASE_STARTED,
            self._worker_settings.worker_id,
            {"phase": phase, "search_count": search_count},
        )

    def _report_prompt(
        self,
        prompt: str,
        registry: SourceRegistry,
        package: EvidencePackage,
    ) -> str:
        sources = "\n".join(
            f"[{source.index}] {source.title} — {source.url}"
            for source in registry.sources
        )
        evidence = json.dumps(
            package.to_data(),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return (
            f"{prompt}\n\n## 可引用来源\n{sources}\n\n"
            f"## 检索到的资料\n{evidence}\n\n"
            "检索已经结束，不要再调用工具：直接根据上述资料写出报告正文。"
            "只允许引用「可引用来源」里的编号；不要编造未列出的链接或标题，"
            "也不要在正文里写网址。"
        )

    def _failure(
        self,
        kind: str,
        topic: str,
        started_at: float,
        recorder: SearchRecorder | None = None,
    ) -> ResearchOutcome:
        searches = recorder.to_data() if recorder is not None else []
        return ResearchOutcome(
            status="failed",
            topic=topic,
            markdown=_FAILURE_TEXT.get(kind, _FAILURE_TEXT["error"]),
            searches=tuple(searches),
            elapsed_seconds=time.perf_counter() - started_at,
            error=_FAILURE_TEXT.get(kind, _FAILURE_TEXT["error"]),
        )

    def _fallback(
        self,
        topic: str,
        registry: SourceRegistry,
        recorder: SearchRecorder,
        started_at: float,
        reason: str | None = None,
    ) -> ResearchOutcome:
        """报告阶段失败但已有来源时，退化为来源清单而不是整体失败。

        正文只给主题与提示；来源由前端「N 个来源」折叠块渲染，
        不在这里重复长列表。失败原因写进 error，便于定位是超时还是校验。
        """

        elapsed = time.perf_counter() - started_at
        lines = [
            f"## {short_topic(topic)}",
            "",
            "> 自动整理未完成，本次检索到的网页已收进下方来源列表，"
            "可先行查阅；这些链接尚未归纳成结论，建议结合原文判断。",
            "",
            "> 调研结果不完整：请重试一次，或缩小问题范围后再检索。",
        ]
        markdown = "\n".join(lines)
        detail = (reason or "报告整理未完成").strip()[:200]
        return ResearchOutcome(
            status="partial",
            topic=topic,
            markdown=markdown,
            searches=tuple(recorder.to_data()),
            sources=tuple(registry.to_data()),
            elapsed_seconds=elapsed,
            error=f"报告整理未完成：{detail}" if reason else detail,
        )
