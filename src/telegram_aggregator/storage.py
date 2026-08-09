from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import ACTIVE_APPLICATION_STATUSES, ListingExtraction


class Storage:
    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(database_path)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _cutoff(days: int | None) -> str | None:
        if days is None:
            return None
        return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _clean_text(value: str | None) -> str | None:
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    def _ensure_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                grouped_id INTEGER,
                source_fingerprint TEXT NOT NULL,
                listing_fingerprint TEXT NOT NULL,
                is_listing INTEGER NOT NULL,
                model_name TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                post_text TEXT,
                posted_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, message_id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                message_id INTEGER,
                grouped_id INTEGER,
                source_fingerprint TEXT NOT NULL,
                listing_fingerprint TEXT NOT NULL UNIQUE,
                is_listing INTEGER NOT NULL,
                model_name TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                post_text TEXT,
                seller_link TEXT,
                source_link TEXT,
                delivery_status TEXT NOT NULL DEFAULT 'analyzed',
                posted_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS safe_deal_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_username TEXT,
                target_message_id INTEGER,
                role TEXT NOT NULL DEFAULT 'buyer',
                status TEXT NOT NULL DEFAULT 'NEW',
                admin_message_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS application_status_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL,
                old_status TEXT,
                new_status TEXT NOT NULL,
                changed_by INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                listing_id INTEGER,
                telegram_message_id INTEGER,
                target_message_id INTEGER,
                source_chat_id INTEGER,
                source_message_id INTEGER,
                user_id INTEGER,
                application_id INTEGER,
                metadata TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS listing_fingerprints (
                fingerprint TEXT PRIMARY KEY,
                first_message_id INTEGER NOT NULL,
                first_chat_id INTEGER NOT NULL,
                first_seen_at TEXT NOT NULL
            )
            """
        )

        self._ensure_columns("listings", {
            "chat_id": "INTEGER",
            "message_id": "INTEGER",
            "source_chat": "TEXT",
            "source_chat_id": "INTEGER",
            "source_message_id": "INTEGER",
            "title": "TEXT",
            "category": "TEXT",
            "price": "REAL",
            "currency": "TEXT",
            "location": "TEXT",
            "condition": "TEXT",
            "description": "TEXT",
            "contact_info": "TEXT",
            "target_message_id": "INTEGER",
            "delivery_status": "TEXT NOT NULL DEFAULT 'analyzed'",
            "posted_at": "TEXT",
        })
        self._ensure_columns("safe_deal_applications", {
            "user_username": "TEXT",
            "target_message_id": "INTEGER",
            "role": "TEXT NOT NULL DEFAULT 'buyer'",
            "status": "TEXT NOT NULL DEFAULT 'NEW'",
            "admin_message_id": "INTEGER",
            "created_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
            "updated_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
        })
        self._ensure_columns("application_status_history", {
            "old_status": "TEXT",
            "new_status": "TEXT NOT NULL",
            "changed_by": "INTEGER",
            "created_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
        })
        self._ensure_columns("events", {
            "listing_id": "INTEGER",
            "telegram_message_id": "INTEGER",
            "target_message_id": "INTEGER",
            "source_chat_id": "INTEGER",
            "source_message_id": "INTEGER",
            "user_id": "INTEGER",
            "application_id": "INTEGER",
            "metadata": "TEXT",
            "created_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
        })

        listing_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(listings)").fetchall()
        }
        if "chat_id" in listing_columns and "message_id" in listing_columns:
            # Backfill listings created before the expanded schema.
            self._conn.execute(
                """
                UPDATE listings
                SET source_chat_id = COALESCE(source_chat_id, chat_id),
                    source_message_id = COALESCE(source_message_id, message_id)
                WHERE source_chat_id IS NULL OR source_message_id IS NULL
                """
            )
        self._conn.commit()

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {
            row["name"]
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, definition in columns.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @staticmethod
    def build_source_fingerprint(text: str, media_count: int) -> str:
        payload = json.dumps({"text": text.strip(), "media_count": media_count}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def build_listing_fingerprint(extraction: ListingExtraction) -> str:
        payload = json.dumps(
            {
                "is_listing": extraction.is_listing,
                "listing_type": extraction.listing_type,
                "title": (extraction.title or "").strip().lower(),
                "category": extraction.category,
                "price": extraction.price,
                "currency": extraction.currency,
                "location": (extraction.location or "").strip().lower(),
                "condition": (extraction.condition or "").strip().lower(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # Legacy helpers kept for compatibility with the existing pipeline.
    def has_processed_message(self, chat_id: int, message_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed_messages WHERE chat_id = ? AND message_id = ? AND posted_at IS NOT NULL",
            (chat_id, message_id),
        ).fetchone()
        return row is not None

    def get_message_record(self, chat_id: int, message_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM processed_messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ).fetchone()

    def record_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        grouped_id: int | None,
        source_fingerprint: str,
        listing_fingerprint: str,
        is_listing: bool,
        model_name: str,
        raw_json: str,
        post_text: str | None,
        posted: bool,
    ) -> None:
        now = self._now()
        self._conn.execute(
            """
            INSERT INTO processed_messages (
                chat_id, message_id, grouped_id, source_fingerprint, listing_fingerprint,
                is_listing, model_name, raw_json, post_text, posted_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, message_id) DO UPDATE SET
                grouped_id = excluded.grouped_id,
                source_fingerprint = excluded.source_fingerprint,
                listing_fingerprint = excluded.listing_fingerprint,
                is_listing = excluded.is_listing,
                model_name = excluded.model_name,
                raw_json = excluded.raw_json,
                post_text = excluded.post_text,
                posted_at = excluded.posted_at
            """,
            (
                chat_id,
                message_id,
                grouped_id,
                source_fingerprint,
                listing_fingerprint,
                int(is_listing),
                model_name,
                raw_json,
                post_text,
                now if posted else None,
                now,
            ),
        )
        if is_listing and not self.has_listing_fingerprint(listing_fingerprint):
            self._conn.execute(
                """
                INSERT OR IGNORE INTO listing_fingerprints (
                    fingerprint, first_message_id, first_chat_id, first_seen_at
                ) VALUES (?, ?, ?, ?)
                """,
                (listing_fingerprint, message_id, chat_id, now),
            )
        self._conn.commit()

    def mark_posted(self, chat_id: int, message_id: int) -> None:
        now = self._now()
        self._conn.execute(
            """
            UPDATE processed_messages
            SET posted_at = ?
            WHERE chat_id = ? AND message_id = ?
            """,
            (now, chat_id, message_id),
        )
        self._conn.commit()

    def has_listing_fingerprint(self, fingerprint: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM listing_fingerprints WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        return row is not None

    def get_listing_record(self, chat_id: int, message_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM listings WHERE source_chat_id = ? AND source_message_id = ?",
            (chat_id, message_id),
        ).fetchone()

    def get_listing_by_id(self, listing_id: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()

    def get_listing_by_fingerprint(self, listing_fingerprint: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM listings WHERE listing_fingerprint = ?",
            (listing_fingerprint,),
        ).fetchone()

    def upsert_listing(
        self,
        *,
        source_chat: str,
        source_chat_id: int,
        source_message_id: int,
        grouped_id: int | None,
        source_fingerprint: str,
        extraction: ListingExtraction,
        raw_json: str,
        post_text: str | None,
        seller_link: str | None,
        source_link: str | None,
        delivery_status: str,
        target_message_id: int | None = None,
    ) -> int:
        now = self._now()
        listing_fingerprint = self.build_listing_fingerprint(extraction)
        existing = self.get_listing_by_fingerprint(listing_fingerprint) or self.get_listing_record(
            source_chat_id, source_message_id
        )

        row_values = (
            source_chat_id,
            source_message_id,
            source_chat,
            source_chat_id,
            source_message_id,
            grouped_id,
            source_fingerprint,
            listing_fingerprint,
            int(extraction.is_listing),
            "gemini",
            self._clean_text(extraction.title),
            self._clean_text(extraction.category),
            extraction.price,
            self._clean_text(extraction.currency),
            self._clean_text(extraction.location),
            self._clean_text(extraction.condition),
            self._clean_text(extraction.description),
            self._clean_text(extraction.contact_info),
            raw_json,
            post_text,
            seller_link,
            source_link,
            target_message_id,
            delivery_status,
            now if delivery_status == "posted" else None,
        )

        if existing:
            listing_id = int(existing["id"])
            self._conn.execute(
                """
                UPDATE listings SET
                    chat_id = ?,
                    message_id = ?,
                    source_chat = ?,
                    source_chat_id = ?,
                    source_message_id = ?,
                    grouped_id = ?,
                    source_fingerprint = ?,
                    listing_fingerprint = ?,
                    is_listing = ?,
                    model_name = ?,
                    title = ?,
                    category = ?,
                    price = ?,
                    currency = ?,
                    location = ?,
                    condition = ?,
                    description = ?,
                    contact_info = ?,
                    raw_json = ?,
                    post_text = ?,
                    seller_link = ?,
                    source_link = ?,
                    target_message_id = ?,
                    delivery_status = ?,
                    posted_at = ?
                WHERE id = ?
                """,
                (*row_values, listing_id),
            )
        else:
            cur = self._conn.execute(
                """
                INSERT INTO listings (
                    chat_id, message_id, source_chat, source_chat_id, source_message_id, grouped_id, source_fingerprint,
                    listing_fingerprint, is_listing, model_name, title, category, price, currency,
                    location, condition, description, contact_info, raw_json, post_text, seller_link,
                    source_link, target_message_id, delivery_status, posted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row_values,
            )
            listing_id = int(cur.lastrowid)

        self._conn.commit()
        return listing_id

    def set_listing_posted(self, listing_id: int, *, target_message_id: int | None = None) -> None:
        now = self._now()
        self._conn.execute(
            """
            UPDATE listings
            SET delivery_status = 'posted',
                posted_at = ?,
                target_message_id = COALESCE(?, target_message_id)
            WHERE id = ?
            """,
            (now, target_message_id, listing_id),
        )
        self._conn.commit()

    def record_event(
        self,
        *,
        event_type: str,
        listing_id: int | None = None,
        telegram_message_id: int | None = None,
        target_message_id: int | None = None,
        source_chat_id: int | None = None,
        source_message_id: int | None = None,
        user_id: int | None = None,
        application_id: int | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO events (
                event_type, listing_id, telegram_message_id, target_message_id, source_chat_id, source_message_id,
                user_id, application_id, metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                listing_id,
                telegram_message_id,
                target_message_id,
                source_chat_id,
                source_message_id,
                user_id,
                application_id,
                json.dumps(metadata, ensure_ascii=False) if metadata is not None else None,
                self._now(),
            ),
        )
        self._conn.commit()

    def get_active_application(self, listing_id: int, user_id: int) -> sqlite3.Row | None:
        placeholders = ",".join("?" for _ in ACTIVE_APPLICATION_STATUSES)
        return self._conn.execute(
            f"""
            SELECT * FROM safe_deal_applications
            WHERE listing_id = ? AND user_id = ? AND status IN ({placeholders})
            ORDER BY id DESC
            LIMIT 1
            """,
            (listing_id, user_id, *ACTIVE_APPLICATION_STATUSES),
        ).fetchone()

    def get_application(self, application_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM safe_deal_applications WHERE id = ?",
            (application_id,),
        ).fetchone()

    def create_application(
        self,
        *,
        listing_id: int,
        user_id: int,
        target_message_id: int | None,
        user_username: str | None = None,
        role: str = "buyer",
        status: str = "NEW",
    ) -> tuple[int, bool]:
        existing = self.get_active_application(listing_id, user_id)
        if existing is not None:
            return int(existing["id"]), False
        now = self._now()
        cur = self._conn.execute(
            """
            INSERT INTO safe_deal_applications (
                listing_id, user_id, user_username, target_message_id, role, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id,
                user_id,
                user_username,
                target_message_id,
                role,
                status,
                now,
                now,
            ),
        )
        application_id = int(cur.lastrowid)
        self._conn.execute(
            """
            INSERT INTO application_status_history (
                application_id, old_status, new_status, changed_by, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (application_id, None, status, user_id, now),
        )
        self._conn.commit()
        return application_id, True

    def update_application_status(self, application_id: int, new_status: str, changed_by: int | None) -> sqlite3.Row | None:
        application = self.get_application(application_id)
        if application is None:
            return None
        old_status = str(application["status"])
        if old_status == new_status:
            return application
        now = self._now()
        self._conn.execute(
            """
            UPDATE safe_deal_applications
            SET status = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_status, now, application_id),
        )
        self._conn.execute(
            """
            INSERT INTO application_status_history (
                application_id, old_status, new_status, changed_by, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (application_id, old_status, new_status, changed_by, now),
        )
        self._conn.commit()
        return self.get_application(application_id)

    def get_listing_stats(self, listing_id: int) -> dict[str, int]:
        counts = self._zero_counts(
            ["buy_clicks", "safe_deal_clicks", "applications", "deals_started", "completed", "cancelled", "disputed"]
        )
        for row in self._conn.execute(
            """
            SELECT event_type, COUNT(*) AS total
            FROM events
            WHERE listing_id = ?
            GROUP BY event_type
            """,
            (listing_id,),
        ).fetchall():
            counts[row["event_type"]] = int(row["total"])

        counts["applications"] = int(
            self._conn.execute(
                "SELECT COUNT(*) AS total FROM safe_deal_applications WHERE listing_id = ?",
                (listing_id,),
            ).fetchone()["total"]
        )
        return counts

    def get_funnel_stats(self, days: int | None = None) -> dict[str, Any]:
        cutoff = self._cutoff(days)
        listing_where = "WHERE created_at >= ?" if cutoff else ""
        event_where = "WHERE created_at >= ?" if cutoff else ""
        listing_args = (cutoff,) if cutoff else ()
        event_args = (cutoff,) if cutoff else ()

        total_listings = int(
            self._conn.execute(
                f"SELECT COUNT(*) AS total FROM listings {listing_where}",
                listing_args,
            ).fetchone()["total"]
        )
        events = self._event_counts(event_where, event_args)
        applications = int(
            self._conn.execute(
                f"SELECT COUNT(*) AS total FROM safe_deal_applications {listing_where}",
                listing_args,
            ).fetchone()["total"]
        )

        return {
            "listings": total_listings,
            "buy_clicks": events["buy_click"],
            "safe_deal_clicks": events["safe_deal_click"],
            "applications": applications,
            "deals_started": events["deal_started"],
            "completed": events["deal_completed"],
            "cancelled": events["deal_cancelled"],
            "disputed": events["deal_disputed"],
            "buy_to_safe": self._ratio(events["buy_click"], events["safe_deal_click"]),
            "safe_to_application": self._ratio(events["safe_deal_click"], applications),
            "application_to_deal": self._ratio(applications, events["deal_started"]),
            "deal_to_completed": self._ratio(events["deal_started"], events["deal_completed"]),
        }

    def get_category_stats(self, days: int | None = None) -> list[dict[str, Any]]:
        cutoff = self._cutoff(days)
        listing_where = "WHERE l.created_at >= ?" if cutoff else ""
        listing_args = (cutoff,) if cutoff else ()
        rows = self._conn.execute(
            f"""
            WITH e AS (
                SELECT listing_id,
                    SUM(CASE WHEN event_type = 'safe_deal_click' THEN 1 ELSE 0 END) AS safe_deal_clicks,
                    SUM(CASE WHEN event_type = 'buy_click' THEN 1 ELSE 0 END) AS buy_clicks,
                    SUM(CASE WHEN event_type = 'deal_completed' THEN 1 ELSE 0 END) AS completed
                FROM events
                {"WHERE created_at >= ?" if cutoff else ""}
                GROUP BY listing_id
            ),
            a AS (
                SELECT listing_id, COUNT(*) AS applications
                FROM safe_deal_applications
                {"WHERE created_at >= ?" if cutoff else ""}
                GROUP BY listing_id
            )
            SELECT
                COALESCE(l.category, 'Другое') AS category,
                COUNT(*) AS listings,
                COALESCE(SUM(COALESCE(e.safe_deal_clicks, 0)), 0) AS safe_deal_clicks,
                COALESCE(SUM(COALESCE(a.applications, 0)), 0) AS applications,
                COALESCE(SUM(COALESCE(e.completed, 0)), 0) AS completed
            FROM listings l
            LEFT JOIN e ON e.listing_id = l.id
            LEFT JOIN a ON a.listing_id = l.id
            {listing_where}
            GROUP BY COALESCE(l.category, 'Другое')
            ORDER BY listings DESC, category ASC
            """,
            (cutoff, cutoff, *listing_args) if cutoff else (),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_source_stats(self, days: int | None = None) -> list[dict[str, Any]]:
        cutoff = self._cutoff(days)
        listing_where = "WHERE l.created_at >= ?" if cutoff else ""
        rows = self._conn.execute(
            f"""
            WITH e AS (
                SELECT listing_id,
                    SUM(CASE WHEN event_type = 'buy_click' THEN 1 ELSE 0 END) AS buy_clicks,
                    SUM(CASE WHEN event_type = 'safe_deal_click' THEN 1 ELSE 0 END) AS safe_deal_clicks,
                    SUM(CASE WHEN event_type = 'deal_completed' THEN 1 ELSE 0 END) AS completed
                FROM events
                {"WHERE created_at >= ?" if cutoff else ""}
                GROUP BY listing_id
            )
            SELECT
                COALESCE(l.source_chat, 'unknown') AS source,
                COUNT(*) AS listings,
                COALESCE(SUM(COALESCE(e.buy_clicks, 0)), 0) AS buy_clicks,
                COALESCE(SUM(COALESCE(e.safe_deal_clicks, 0)), 0) AS safe_deal_clicks,
                COALESCE(SUM(COALESCE(e.completed, 0)), 0) AS completed
            FROM listings l
            LEFT JOIN e ON e.listing_id = l.id
            {listing_where}
            GROUP BY COALESCE(l.source_chat, 'unknown')
            ORDER BY listings DESC, source ASC
            """,
            (cutoff, cutoff) if cutoff else (),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_price_bucket_stats(self, days: int | None = None) -> list[dict[str, Any]]:
        cutoff = self._cutoff(days)
        listing_where = "WHERE l.created_at >= ?" if cutoff else ""
        rows = self._conn.execute(
            f"""
            WITH e AS (
                SELECT listing_id,
                    SUM(CASE WHEN event_type = 'safe_deal_click' THEN 1 ELSE 0 END) AS safe_deal_clicks,
                    SUM(CASE WHEN event_type = 'deal_completed' THEN 1 ELSE 0 END) AS completed
                FROM events
                {"WHERE created_at >= ?" if cutoff else ""}
                GROUP BY listing_id
            ),
            a AS (
                SELECT listing_id, COUNT(*) AS applications
                FROM safe_deal_applications
                {"WHERE created_at >= ?" if cutoff else ""}
                GROUP BY listing_id
            )
            SELECT
                CASE
                    WHEN COALESCE(l.price, 0) <= 50 THEN '€0–50'
                    WHEN COALESCE(l.price, 0) <= 100 THEN '€50–100'
                    WHEN COALESCE(l.price, 0) <= 250 THEN '€100–250'
                    WHEN COALESCE(l.price, 0) <= 500 THEN '€250–500'
                    ELSE '€500+'
                END AS price_bucket,
                COUNT(*) AS listings,
                COALESCE(SUM(COALESCE(e.safe_deal_clicks, 0)), 0) AS safe_deal_requests,
                COALESCE(SUM(COALESCE(a.applications, 0)), 0) AS applications,
                COALESCE(SUM(COALESCE(e.completed, 0)), 0) AS completed
            FROM listings l
            LEFT JOIN e ON e.listing_id = l.id
            LEFT JOIN a ON a.listing_id = l.id
            {listing_where}
            GROUP BY price_bucket
            ORDER BY
                CASE price_bucket
                    WHEN '€0–50' THEN 1
                    WHEN '€50–100' THEN 2
                    WHEN '€100–250' THEN 3
                    WHEN '€250–500' THEN 4
                    ELSE 5
                END
            """,
            (cutoff, cutoff, cutoff) if cutoff else (),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_listing_export_rows(self, days: int | None = None) -> list[dict[str, Any]]:
        cutoff = self._cutoff(days)
        listing_where = "WHERE l.created_at >= ?" if cutoff else ""
        rows = self._conn.execute(
            f"""
            WITH e AS (
                SELECT listing_id,
                    SUM(CASE WHEN event_type = 'buy_click' THEN 1 ELSE 0 END) AS buy_clicks,
                    SUM(CASE WHEN event_type = 'safe_deal_click' THEN 1 ELSE 0 END) AS safe_deal_clicks,
                    SUM(CASE WHEN event_type = 'deal_started' THEN 1 ELSE 0 END) AS deals_started,
                    SUM(CASE WHEN event_type = 'deal_completed' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN event_type = 'deal_cancelled' THEN 1 ELSE 0 END) AS cancelled,
                    SUM(CASE WHEN event_type = 'deal_disputed' THEN 1 ELSE 0 END) AS disputed
                FROM events
                {"WHERE created_at >= ?" if cutoff else ""}
                GROUP BY listing_id
            ),
            a AS (
                SELECT listing_id, COUNT(*) AS applications
                FROM safe_deal_applications
                {"WHERE created_at >= ?" if cutoff else ""}
                GROUP BY listing_id
            )
            SELECT
                l.id AS listing_id,
                l.source_chat AS source,
                COALESCE(l.category, 'Другое') AS category,
                l.price,
                l.currency,
                COALESCE(e.buy_clicks, 0) AS buy_clicks,
                COALESCE(e.safe_deal_clicks, 0) AS safe_deal_clicks,
                COALESCE(a.applications, 0) AS applications,
                COALESCE(e.deals_started, 0) AS deals_started,
                COALESCE(e.completed, 0) AS completed,
                COALESCE(e.cancelled, 0) AS cancelled,
                COALESCE(e.disputed, 0) AS disputed,
                l.created_at
            FROM listings l
            LEFT JOIN e ON e.listing_id = l.id
            LEFT JOIN a ON a.listing_id = l.id
            {listing_where}
            ORDER BY l.id ASC
            """,
            (cutoff, cutoff, cutoff) if cutoff else (),
        ).fetchall()
        return [dict(row) for row in rows]

    def _event_counts(self, where_clause: str = "", args: tuple[Any, ...] = ()) -> dict[str, int]:
        counts = {
            "buy_click": 0,
            "safe_deal_click": 0,
            "deal_started": 0,
            "deal_completed": 0,
            "deal_cancelled": 0,
            "deal_disputed": 0,
        }
        rows = self._conn.execute(
            f"""
            SELECT event_type, COUNT(*) AS total
            FROM events
            {where_clause}
            GROUP BY event_type
            """,
            args,
        ).fetchall()
        for row in rows:
            if row["event_type"] in counts:
                counts[row["event_type"]] = int(row["total"])
        return counts

    @staticmethod
    def _zero_counts(keys: list[str]) -> dict[str, int]:
        return {key: 0 for key in keys}

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> float | None:
        if denominator == 0:
            return None
        return round((numerator / denominator) * 100, 1)
    