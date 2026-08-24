"""作品级创作控制面：不属于正史，也不属于长期记忆。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from .database import Database
from .tables import CreativeControlRow


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
            row = session.get(CreativeControlRow, book_id)
        if row is None:
            return CreativeControl(book_id, "", "", "single_chapter", None, "")
        return self._decode(row)

    def update(self, *, book_id: str, author_intent: str | None = None,
               current_focus: str | None = None, current_focus_mode: str | None = None,
               focus_target_chapter: int | None = None) -> CreativeControl:
        if current_focus_mode is not None and current_focus_mode not in {"single_chapter", "persistent"}:
            raise ValueError("current_focus_mode 只支持 single_chapter 或 persistent")
        with self.database.session() as session:
            with session.begin():
                row = session.get(CreativeControlRow, book_id, with_for_update=True)
                now = datetime.now(timezone.utc).isoformat()
                if row is None:
                    row = CreativeControlRow(
                        book_id=book_id,
                        author_intent=(author_intent or "").strip(),
                        current_focus=(current_focus or "").strip(),
                        current_focus_mode=current_focus_mode or "single_chapter",
                        focus_target_chapter=(focus_target_chapter if (current_focus or "").strip() and (current_focus_mode or "single_chapter") == "single_chapter" else None),
                        updated_at=now,
                    )
                    session.add(row)
                else:
                    if author_intent is not None:
                        row.author_intent = author_intent.strip()
                    if current_focus is not None:
                        row.current_focus = current_focus.strip()
                        mode = current_focus_mode or row.current_focus_mode
                        row.current_focus_mode = mode
                        row.focus_target_chapter = focus_target_chapter if row.current_focus and mode == "single_chapter" else None
                    elif current_focus_mode is not None:
                        row.current_focus_mode = current_focus_mode
                        row.focus_target_chapter = focus_target_chapter if row.current_focus and current_focus_mode == "single_chapter" else None
                    row.updated_at = now
                session.flush()
                return self._decode(row)

    @staticmethod
    def _decode(row: CreativeControlRow) -> CreativeControl:
        return CreativeControl(row.book_id, row.author_intent, row.current_focus, row.current_focus_mode, row.focus_target_chapter, row.updated_at)

    def consume_after_commit(self, *, book_id: str, chapter_number: int) -> CreativeControl:
        """只在目标章节成功进入正史后清理一次性焦点。"""

        with self.database.session() as session:
            with session.begin():
                row = session.get(CreativeControlRow, book_id, with_for_update=True)
                if row is None:
                    return CreativeControl(book_id, "", "", "single_chapter", None, "")
                if row.current_focus and row.current_focus_mode == "single_chapter" and row.focus_target_chapter is not None and chapter_number >= row.focus_target_chapter:
                    row.current_focus = ""
                    row.focus_target_chapter = None
                    row.updated_at = datetime.now(timezone.utc).isoformat()
                session.flush()
                return self._decode(row)
