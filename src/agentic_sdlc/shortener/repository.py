"""Persistence for short links and click events."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..db import Database
from ..errors import Conflict


@dataclass(frozen=True)
class Link:
    id: int
    code: str
    target_url: str
    created_at: str
    expires_at: str | None
    deleted_at: str | None
    created_by: str | None
    idempotency_key: str | None
    click_count: int
    metadata: dict[str, Any]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Link:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        return cls(
            id=row["id"],
            code=row["code"],
            target_url=row["target_url"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            deleted_at=row["deleted_at"],
            created_by=row["created_by"],
            idempotency_key=row["idempotency_key"],
            click_count=row["click_count"],
            metadata=metadata,
        )


class LinkRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        *,
        code: str,
        target_url: str,
        created_at: str,
        expires_at: str | None,
        created_by: str | None,
        idempotency_key: str | None,
        metadata: dict[str, Any],
    ) -> Link:
        try:
            with self.db.tx() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO links
                        (code, target_url, created_at, expires_at, created_by,
                         idempotency_key, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        code,
                        target_url,
                        created_at,
                        expires_at,
                        created_by,
                        idempotency_key,
                        json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                    ),
                )
                link_id = int(cursor.lastrowid)
                row = conn.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("short code or idempotency key already exists") from exc
        return Link.from_row(row)

    def find_by_code(self, code: str) -> Link | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM links WHERE code = ?", (code,)).fetchone()
        return Link.from_row(row) if row else None

    def find_by_idempotency_key(self, key: str) -> Link | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM links WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return Link.from_row(row) if row else None

    def record_click(
        self,
        *,
        link_id: int,
        code: str,
        clicked_at: str,
        referrer: str | None,
        user_agent: str | None,
        visitor_hash: str,
        country: str | None,
    ) -> None:
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO click_events
                    (link_id, code, clicked_at, referrer, user_agent, visitor_hash, country)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (link_id, code, clicked_at, referrer, user_agent, visitor_hash, country),
            )
            conn.execute(
                "UPDATE links SET click_count = click_count + 1 WHERE id = ?", (link_id,)
            )

    def soft_delete(self, code: str, deleted_at: str) -> bool:
        with self.db.tx() as conn:
            cursor = conn.execute(
                "UPDATE links SET deleted_at = ? WHERE code = ? AND deleted_at IS NULL",
                (deleted_at, code),
            )
            return cursor.rowcount > 0

    def list_links(self, *, limit: int = 50, offset: int = 0) -> tuple[list[Link], int]:
        with self.db.read() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM links").fetchone()["n"]
            rows = conn.execute(
                "SELECT * FROM links ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [Link.from_row(row) for row in rows], int(total)

    # -- analytics ---------------------------------------------------------

    def analytics(self, code: str, since: str | None, until: str | None) -> dict[str, Any]:
        clauses = ["code = ?"]
        params: list[Any] = [code]
        if since:
            clauses.append("clicked_at >= ?")
            params.append(since)
        if until:
            clauses.append("clicked_at <= ?")
            params.append(until)
        where = " AND ".join(clauses)
        with self.db.read() as conn:
            summary = conn.execute(
                f"""
                SELECT COUNT(*) AS total,
                       COUNT(DISTINCT visitor_hash) AS unique_visitors,
                       MIN(clicked_at) AS first_click_at,
                       MAX(clicked_at) AS last_click_at
                FROM click_events WHERE {where}
                """,
                params,
            ).fetchone()
            daily = conn.execute(
                f"""
                SELECT substr(clicked_at, 1, 10) AS day, COUNT(*) AS clicks
                FROM click_events WHERE {where}
                GROUP BY day ORDER BY day
                """,
                params,
            ).fetchall()
            referrers = conn.execute(
                f"""
                SELECT COALESCE(NULLIF(referrer, ''), 'direct') AS referrer,
                       COUNT(*) AS clicks
                FROM click_events WHERE {where}
                GROUP BY referrer ORDER BY clicks DESC, referrer LIMIT 10
                """,
                params,
            ).fetchall()
            countries = conn.execute(
                f"""
                SELECT COALESCE(NULLIF(country, ''), 'unknown') AS country,
                       COUNT(*) AS clicks
                FROM click_events WHERE {where}
                GROUP BY country ORDER BY clicks DESC, country LIMIT 10
                """,
                params,
            ).fetchall()
        return {
            "total_clicks": int(summary["total"] or 0),
            "unique_visitors": int(summary["unique_visitors"] or 0),
            "first_click_at": summary["first_click_at"],
            "last_click_at": summary["last_click_at"],
            "daily": [dict(row) for row in daily],
            "top_referrers": [dict(row) for row in referrers],
            "top_countries": [dict(row) for row in countries],
        }
