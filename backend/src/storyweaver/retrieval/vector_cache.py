"""可重建的 SQLite 向量缓存，使用 sqlite-vec 做精确余弦检索。"""

from __future__ import annotations

import sqlite3
import struct
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec


class VectorCache:
    """只存内容哈希和向量；候选范围由调用方的有效快照决定。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS embeddings ("
                "profile TEXT NOT NULL, content_hash TEXT NOT NULL, dimensions INTEGER NOT NULL, "
                "vector BLOB NOT NULL, PRIMARY KEY (profile, content_hash))"
            )

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=0.25)
        try:
            connection.enable_load_extension(True)
            try:
                sqlite_vec.load(connection)
            finally:
                connection.enable_load_extension(False)
            with connection:
                yield connection
        finally:
            connection.close()

    def existing(self, profile: str, hashes: list[str]) -> set[str]:
        result: set[str] = set()
        with self._connection() as connection:
            for offset in range(0, len(hashes), 400):
                batch = hashes[offset:offset + 400]
                placeholders = ",".join("?" for _ in batch)
                result.update(row[0] for row in connection.execute(
                    f"SELECT content_hash FROM embeddings WHERE profile=? AND content_hash IN ({placeholders})",
                    [profile, *batch],
                ))
        return result

    def put(self, profile: str, entries: list[tuple[str, list[float]]]) -> None:
        if not entries:
            return
        with self._connection() as connection:
            # 先获取写锁再检查，防止并发写入把同一模型空间变成混合维度。
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT dimensions FROM embeddings WHERE profile=? LIMIT 1", (profile,)).fetchone()
            dimensions = previous[0] if previous else len(entries[0][1])
            if any(len(vector) != dimensions for _, vector in entries):
                raise ValueError("同一 Embedding 模型空间的维度发生变化，需要重建缓存")
            connection.executemany(
                "INSERT OR REPLACE INTO embeddings (profile, content_hash, dimensions, vector) VALUES (?, ?, ?, ?)",
                [(profile, digest, len(vector), sqlite_vec.serialize_float32(vector)) for digest, vector in entries],
            )

    def get(self, profile: str, digest: str) -> list[float]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT dimensions, vector FROM embeddings WHERE profile=? AND content_hash=?",
                (profile, digest),
            ).fetchone()
        if row is None:
            raise ValueError("查询向量尚未生成")
        return list(struct.unpack(f"{row[0]}f", row[1]))

    def search(self, profile: str, hashes: list[str], query: list[float], limit: int, minimum_similarity: float) -> list[str]:
        """先限制有效候选，再计算距离；不会让其他作品挤占 Top K。"""
        matches: list[tuple[float, str]] = []
        encoded = sqlite_vec.serialize_float32(query)
        with self._connection() as connection:
            for offset in range(0, len(hashes), 400):
                batch = hashes[offset:offset + 400]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    "SELECT content_hash, vec_distance_cosine(vector, ?) AS distance FROM embeddings "
                    f"WHERE profile=? AND dimensions=? AND content_hash IN ({placeholders}) "
                    "ORDER BY distance, content_hash LIMIT ?",
                    [encoded, profile, len(query), *batch, limit],
                )
                matches.extend((distance, digest) for digest, distance in rows if distance <= 1 - minimum_similarity)
        return [digest for _, digest in sorted(matches)[:limit]]
