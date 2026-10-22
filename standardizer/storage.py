from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from standardizer.models import ReviewStatus, StandardizationResult


class ResultStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS standardization_result (
                    id TEXT PRIMARY KEY,
                    source_hash TEXT NOT NULL,
                    source_filename TEXT NOT NULL,
                    doc_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    quality_score INTEGER NOT NULL,
                    blocked INTEGER NOT NULL,
                    output_path TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    reviewer TEXT,
                    review_note TEXT,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_standardizer_status "
                "ON standardization_result(status, quality_score)"
            )

    def save(self, result: StandardizationResult) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO standardization_result (
                    id, source_hash, source_filename, doc_type, status, quality_score,
                    blocked, output_path, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    doc_type=excluded.doc_type,
                    quality_score=excluded.quality_score,
                    blocked=excluded.blocked,
                    output_path=excluded.output_path,
                    result_json=excluded.result_json
                """,
                (
                    result.id,
                    result.source.content_hash,
                    result.source.filename,
                    result.proposal.doc_type.value,
                    result.review_status.value,
                    result.quality_score,
                    int(result.is_blocked),
                    result.output_relative_path,
                    result.model_dump_json(),
                    now,
                ),
            )

    def get(self, result_id: str) -> StandardizationResult | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT result_json, status FROM standardization_result WHERE id = ?",
                (result_id,),
            ).fetchone()
        if row is None:
            return None
        result = StandardizationResult.model_validate_json(row["result_json"])
        result.review_status = ReviewStatus(row["status"])
        return result

    def list(self, status: ReviewStatus | None = None, limit: int = 200) -> list[StandardizationResult]:
        query = "SELECT result_json, status FROM standardization_result"
        params: list[object] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status.value)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        results = []
        for row in rows:
            result = StandardizationResult.model_validate_json(row["result_json"])
            result.review_status = ReviewStatus(row["status"])
            results.append(result)
        return results

    def list_all(self) -> list[StandardizationResult]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT result_json, status FROM standardization_result ORDER BY created_at DESC"
            ).fetchall()
        results = []
        for row in rows:
            result = StandardizationResult.model_validate_json(row["result_json"])
            result.review_status = ReviewStatus(row["status"])
            results.append(result)
        return results

    def decide(self, result_id: str, status: ReviewStatus, reviewer: str, note: str) -> bool:
        if status == ReviewStatus.PENDING:
            raise ValueError("A review decision must be approved or rejected")
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE standardization_result
                SET status = ?, reviewer = ?, review_note = ?, reviewed_at = ?
                WHERE id = ?
                """,
                (status.value, reviewer, note, now, result_id),
            )
        return cursor.rowcount == 1
