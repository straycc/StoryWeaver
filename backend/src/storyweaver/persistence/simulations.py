"""角色剧场 SQLite 仓储。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..story_simulation.models import SimulationSession, SimulationState, SimulationTurn
from ..story_simulation.serialization import session_from_row, turn_from_row
from .database import Database
from .tables import ContextSnapshotRow, SimulationSessionRow, SimulationTurnRow


def _data(value: object) -> dict[str, Any]:
    return json.loads(json.dumps(asdict(value), ensure_ascii=False))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_hash(state: SimulationState) -> str:
    return "sha256:" + sha256(json.dumps(_data(state), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class SimulationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, session_value: SimulationSession) -> SimulationSession:
        with self.database.session() as session:
            with self.database.write_transaction(session):
                session.add(SimulationSessionRow(
                    simulation_id=session_value.simulation_id, book_id=session_value.book_id,
                    base_book_version=session_value.base_book_version, base_chapter_number=session_value.base_chapter_number,
                    mode=session_value.mode, user_character_id=session_value.user_character_id, status=session_value.status,
                    current_turn=session_value.current_turn, version=session_value.version,
                    base_snapshot_json=dict(session_value.base_snapshot), scene_config_json=dict(session_value.scene_config),
                    current_state_json=_data(session_value.current_state), created_at=session_value.created_at, updated_at=session_value.updated_at,
                ))
        return self.get(session_value.simulation_id)

    def get(self, simulation_id: str) -> SimulationSession:
        with self.database.session() as session:
            row = session.get(SimulationSessionRow, simulation_id)
        if row is None:
            raise KeyError("角色剧场不存在")
        return session_from_row(row)

    def list_for_book(self, book_id: str) -> tuple[SimulationSession, ...]:
        with self.database.session() as session:
            rows = session.scalars(select(SimulationSessionRow).where(SimulationSessionRow.book_id == book_id).order_by(SimulationSessionRow.updated_at.desc())).all()
        return tuple(session_from_row(row) for row in rows)

    def list_turns(self, simulation_id: str) -> tuple[SimulationTurn, ...]:
        with self.database.session() as session:
            rows = session.scalars(select(SimulationTurnRow).where(SimulationTurnRow.simulation_id == simulation_id).order_by(SimulationTurnRow.turn_number)).all()
        return tuple(turn_from_row(row) for row in rows)

    def list_context_snapshot_links(self, turn_id: str) -> tuple[dict[str, object], ...]:
        with self.database.session() as session:
            rows = session.scalars(
                select(ContextSnapshotRow)
                .where(ContextSnapshotRow.simulation_turn_id == turn_id)
                .order_by(ContextSnapshotRow.simulation_ordinal)
            ).all()
        return tuple({
            "stage": row.simulation_stage, "snapshot_id": row.snapshot_id,
            "character_id": row.simulation_character_id, "ordinal": row.simulation_ordinal,
        } for row in rows)

    def find_by_client_request(self, *, simulation_id: str, client_request_id: str) -> SimulationTurn | None:
        with self.database.session() as session:
            row = session.scalar(select(SimulationTurnRow).where(
                SimulationTurnRow.simulation_id == simulation_id,
                SimulationTurnRow.client_request_id == client_request_id,
            ))
        return turn_from_row(row) if row is not None else None

    def reserve_turn(self, *, simulation_id: str, expected_version: int, client_request_id: str,
                     input_type: str, user_input: str, job_id: str,
                     target_character_id: str | None = None) -> SimulationTurn:
        with self.database.session() as session:
            try:
                with self.database.write_transaction(session):
                    existing = session.scalar(select(SimulationTurnRow).where(
                        SimulationTurnRow.simulation_id == simulation_id,
                        SimulationTurnRow.client_request_id == client_request_id,
                    ))
                    if existing is not None:
                        return turn_from_row(existing)
                    owner = session.get(SimulationSessionRow, simulation_id)
                    if owner is None:
                        raise KeyError("角色剧场不存在")
                    if owner.status != "active":
                        raise ValueError("当前模拟未处于可互动状态")
                    if owner.version != expected_version:
                        raise ValueError("模拟状态已变化，请刷新后重试")
                    latest_turn = session.scalar(select(func.max(SimulationTurnRow.turn_number)).where(
                        SimulationTurnRow.simulation_id == simulation_id,
                    )) or 0
                    row = SimulationTurnRow(
                        turn_id=str(uuid4()), simulation_id=simulation_id, turn_number=int(latest_turn) + 1,
                        job_id=job_id, client_request_id=client_request_id, mode=owner.mode,
                        user_character_id=owner.user_character_id, target_character_id=target_character_id,
                        input_type=input_type, user_input=user_input,
                        status="queued", output_json=None, state_delta_json=None, state_before_hash=state_hash(session_from_row(owner).current_state),
                        state_after_hash=None, context_snapshot_id=None, created_at=_now(),
                    )
                    session.add(row)
                    session.flush()
                    return turn_from_row(row)
            except IntegrityError as exc:
                raise ValueError("该角色剧场已有正在执行的回合") from exc

    def start_turn(self, turn_id: str) -> SimulationTurn:
        with self.database.session() as session:
            with self.database.write_transaction(session):
                row = session.get(SimulationTurnRow, turn_id)
                if row is None:
                    raise KeyError("模拟回合不存在")
                if row.status == "queued":
                    row.status = "running"
                return turn_from_row(row)

    def commit_turn(self, *, turn_id: str, expected_version: int, state: SimulationState,
                    output: dict[str, object], delta: dict[str, object], context_snapshot_id: str | None,
                    snapshot_links: tuple[tuple[str, str | None, str | None, int], ...] = ()) -> SimulationSession:
        with self.database.session() as session:
            with self.database.write_transaction(session):
                turn = session.get(SimulationTurnRow, turn_id)
                if turn is None:
                    raise KeyError("模拟回合不存在")
                owner = session.get(SimulationSessionRow, turn.simulation_id)
                if owner is None or owner.version != expected_version:
                    raise ValueError("模拟版本冲突")
                owner.current_state_json = _data(state)
                owner.current_turn = turn.turn_number
                owner.version += 1
                owner.updated_at = _now()
                turn.status = "completed"; turn.output_json = dict(output); turn.state_delta_json = dict(delta)
                turn.context_snapshot_id = context_snapshot_id; turn.state_after_hash = state_hash(state)
                # 回合、沙盒状态与 Context 审计关联必须同时成功，避免假失败造成重复推进。
                session.query(ContextSnapshotRow).filter(
                    ContextSnapshotRow.simulation_turn_id == turn_id
                ).update({
                    ContextSnapshotRow.simulation_stage: None,
                    ContextSnapshotRow.simulation_character_id: None,
                    ContextSnapshotRow.simulation_ordinal: None,
                })
                for stage, snapshot_id, character_id, ordinal in snapshot_links:
                    if snapshot_id is not None:
                        snapshot = session.get(ContextSnapshotRow, snapshot_id)
                        if snapshot is not None:
                            snapshot.simulation_turn_id = turn_id
                            snapshot.simulation_stage = stage
                            snapshot.simulation_character_id = character_id
                            snapshot.simulation_ordinal = ordinal
                return session_from_row(owner)

    def replace_context_snapshots(self, *, turn_id: str,
                                  links: tuple[tuple[str, str | None, str | None, int], ...]) -> None:
        """写入 V2 回合的完整快照关联；旧主快照字段仍由 commit_turn 维护。"""
        with self.database.session() as session:
            with self.database.write_transaction(session):
                turn = session.get(SimulationTurnRow, turn_id)
                if turn is None:
                    raise KeyError("模拟回合不存在")
                session.query(ContextSnapshotRow).filter(
                    ContextSnapshotRow.simulation_turn_id == turn_id
                ).update({
                    ContextSnapshotRow.simulation_turn_id: None,
                    ContextSnapshotRow.simulation_stage: None,
                    ContextSnapshotRow.simulation_character_id: None,
                    ContextSnapshotRow.simulation_ordinal: None,
                })
                for stage, snapshot_id, character_id, ordinal in links:
                    if snapshot_id is not None:
                        snapshot = session.get(ContextSnapshotRow, snapshot_id)
                        if snapshot is not None:
                            snapshot.simulation_turn_id = turn_id
                            snapshot.simulation_stage = stage
                            snapshot.simulation_character_id = character_id
                            snapshot.simulation_ordinal = ordinal

    def fail_turn(self, turn_id: str) -> None:
        with self.database.session() as session:
            with self.database.write_transaction(session):
                row = session.get(SimulationTurnRow, turn_id)
                if row is not None and row.status in {"queued", "running"}:
                    row.status = "failed"

    def update_mode(self, *, simulation_id: str, expected_version: int, mode: str, user_character_id: str | None) -> SimulationSession:
        with self.database.session() as session:
            with self.database.write_transaction(session):
                row = session.get(SimulationSessionRow, simulation_id)
                if row is None:
                    raise KeyError("角色剧场不存在")
                if row.version != expected_version:
                    raise ValueError("模拟状态已变化，请刷新后重试")
                row.mode = mode; row.user_character_id = user_character_id; row.version += 1; row.updated_at = _now()
                return session_from_row(row)

    def set_status(self, simulation_id: str, status: str) -> SimulationSession:
        with self.database.session() as session:
            with self.database.write_transaction(session):
                row = session.get(SimulationSessionRow, simulation_id)
                if row is None:
                    raise KeyError("角色剧场不存在")
                active_turn = session.scalar(
                    select(SimulationTurnRow.turn_id).where(
                        SimulationTurnRow.simulation_id == simulation_id,
                        SimulationTurnRow.status.in_(("queued", "running")),
                    )
                )
                if active_turn is not None:
                    raise ValueError("当前回合仍在执行，请等待完成后再暂停或结束剧场")
                row.status = status; row.version += 1; row.updated_at = _now()
                return session_from_row(row)

    def delete(self, simulation_id: str) -> None:
        with self.database.session() as session:
            with self.database.write_transaction(session):
                row = session.get(SimulationSessionRow, simulation_id)
                if row is None:
                    raise KeyError("角色剧场不存在")
                session.delete(row)
