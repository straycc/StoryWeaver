"""StoryWeaver 的应用服务：会话、工作区和运行进度投影。"""

from typing import TYPE_CHECKING, Any

from .models import ChatMessage, ChatSession, ChatSessionSummary
from .session_store import ChatSessionStore

if TYPE_CHECKING:
    from .workspace import ChatWorkspaceApplication


def __getattr__(name: str) -> Any:
    """延迟加载工作区，避免 Context 模型与应用服务循环导入。"""

    if name in {"ChatWorkspaceApplication", "build_chat_workspace"}:
        from .workspace import ChatWorkspaceApplication, build_chat_workspace

        return {
            "ChatWorkspaceApplication": ChatWorkspaceApplication,
            "build_chat_workspace": build_chat_workspace,
        }[name]
    raise AttributeError(name)


__all__ = [
    "ChatMessage",
    "ChatSession",
    "ChatSessionStore",
    "ChatSessionSummary",
    "ChatWorkspaceApplication",
    "build_chat_workspace",
]
