"""把结构化 ChapterContext 确定性渲染为 Writer 输入。"""

from __future__ import annotations

from .models import ChapterContext


class ChapterContextRenderer:
    """只渲染已被 ContextBuilder 选中的条目。"""

    def render(self, context: ChapterContext) -> str:
        sections = [
            f"请根据以下有限上下文创作第 {context.chapter_number} 章。",
            "不得使用上下文之外的信息补写既定事实；可以创造不违反约束的场景细节。",
            self.render_entries(context),
            "只输出章节 JSON，不要复述上下文，不要输出分析过程。",
        ]
        return "\n\n".join(sections)

    def render_entries(self, context: ChapterContext) -> str:
        """渲染可供 Reviewer、Reviser 等复用的纯 Context 条目。"""

        sections: list[str] = []
        for entry in context.entries:
            layer = "受保护" if entry.protected else "参考"
            sections.append(
                "\n".join(
                    (
                        f"## [{layer}] {entry.source_type} / {entry.source_id}",
                        f"选入原因：{entry.reason}",
                        entry.content,
                    )
                )
            )
        return "\n\n".join(sections)
