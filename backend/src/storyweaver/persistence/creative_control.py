"""作品级创作控制面：不属于正史，也不属于长期记忆。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .database import Database
from .tables import BookRow


@dataclass(frozen=True, slots=True)
class CreativeControl:
    book_id: str
    author_intent: str
    current_focus: str
    current_focus_mode: str
    focus_target_chapter: int | None
    updated_at: str


class CreativeControlRepository:
    """作者可随时修改的输入治理层，Writer 只能读取编译后的版本。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, book_id: str) -> CreativeControl:
        with self.database.session() as session:
            row = session.get(BookRow, book_id)
        if row is None:
            return CreativeControl(book_id, "", "", "single_chapter", None, "")
        return self._decode(book_id, row.creative_control_json)

    def update(self, *, book_id: str, author_intent: str | None = None,
               current_focus: str | None = None, current_focus_mode: str | None = None,
               focus_target_chapter: int | None = None) -> CreativeControl:
        if current_focus_mode is not None and current_focus_mode not in {"single_chapter", "persistent"}:
            raise ValueError("current_focus_mode 只支持 single_chapter 或 persistent")
        with self.database.session() as session:
            with self.database.write_transaction(session):
                row = session.get(BookRow, book_id)
                now = datetime.now(timezone.utc).isoformat()
                if row is None:
                    raise KeyError(f"作品不存在：{book_id}")
                control = dict(row.creative_control_json or {})
                control.setdefault("author_intent", "")
                control.setdefault("current_focus", "")
                control.setdefault("current_focus_mode", "single_chapter")
                control.setdefault("focus_target_chapter", None)
                if author_intent is not None:
                    control["author_intent"] = author_intent.strip()
                if current_focus is not None:
                    control["current_focus"] = current_focus.strip()
                    mode = current_focus_mode or str(control["current_focus_mode"])
                    control["current_focus_mode"] = mode
                    control["focus_target_chapter"] = focus_target_chapter if control["current_focus"] and mode == "single_chapter" else None
                elif current_focus_mode is not None:
                    control["current_focus_mode"] = current_focus_mode
                    control["focus_target_chapter"] = focus_target_chapter if control["current_focus"] and current_focus_mode == "single_chapter" else None
                control["updated_at"] = now
                row.creative_control_json = control
                session.flush()
                return self._decode(book_id, control)

    @staticmethod
    def _decode(book_id: str, data: dict) -> CreativeControl:
        return CreativeControl(
            book_id, str(data.get("author_intent", "")), str(data.get("current_focus", "")),
            str(data.get("current_focus_mode", "single_chapter")), data.get("focus_target_chapter"),
            str(data.get("updated_at", "")),
        )

    def consume_after_commit(self, *, book_id: str, chapter_number: int) -> CreativeControl:
        """只在目标章节成功进入正史后清理一次性焦点。"""

        with self.database.session() as session:
            with self.database.write_transaction(session):
                row = session.get(BookRow, book_id)
                if row is None:
                    return CreativeControl(book_id, "", "", "single_chapter", None, "")
                control = dict(row.creative_control_json or {})
                if (control.get("current_focus") and control.get("current_focus_mode") == "single_chapter"
                        and control.get("focus_target_chapter") is not None
                        and chapter_number >= int(control["focus_target_chapter"])):
                    control["current_focus"] = ""
                    control["focus_target_chapter"] = None
                    control["updated_at"] = datetime.now(timezone.utc).isoformat()
                    row.creative_control_json = control
                session.flush()
                return self._decode(book_id, control)
