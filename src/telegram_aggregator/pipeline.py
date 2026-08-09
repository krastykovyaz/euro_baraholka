from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO
import mimetypes
from typing import Any

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from .formatter import ensure_safe_deal_line, format_listing_card
from .gemini import GeminiAnalyzer
from .models import ListingExtraction, ListingResult, SourceMessageBundle
from .storage import Storage
from .telegram_source import fetch_recent_bundles


@dataclass(slots=True)
class ProcessOutcome:
    processed: int = 0
    posted: int = 0
    skipped_non_listing: int = 0
    skipped_duplicate: int = 0


class ListingPipeline:
    def __init__(
        self,
        *,
        settings,
        storage: Storage,
        analyzer: GeminiAnalyzer,
        client: TelegramClient,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.analyzer = analyzer
        self.client = client
        self.bot_username: str | None = None

    def _build_uploads(self, bundle: SourceMessageBundle) -> list[BytesIO]:
        uploads: list[BytesIO] = []
        for index, media in enumerate(bundle.media, start=1):
            upload = BytesIO(media.data)
            extension = mimetypes.guess_extension(media.mime_type) or ".jpg"
            upload.name = f"listing-{bundle.message_id}-{index}{extension}"
            uploads.append(upload)
        return uploads

    async def _send_post(self, text: str, listing_id: int, bundle: SourceMessageBundle | None = None) -> Any | None:
        if listing_id > 0:
            # Основной путь — диплинк на бота. Если бот не поднят
            # (self.bot_username пуст), ensure_safe_deal_line сама
            # подставит запасную ссылку на админа из конфига.
            text = ensure_safe_deal_line(
                text,
                listing_id,
                bot_username=self.bot_username,
                admin_telegram_id=self.settings.admin_telegram_id,
            )
        while True:
            try:
                if bundle and bundle.media:
                    uploads = self._build_uploads(bundle)
                    sent = await self.client.send_file(
                        self.settings.target_channel,
                        uploads if len(uploads) > 1 else uploads[0],
                        caption=text,
                        force_document=False,
                    )
                else:
                    sent = await self.client.send_message(
                        self.settings.target_channel,
                        text,
                        link_preview=False,
                    )
                if self.settings.send_throttle_seconds > 0:
                    await asyncio.sleep(self.settings.send_throttle_seconds)
                return sent
            except FloodWaitError as exc:
                wait_seconds = int(getattr(exc, "seconds", 0) or 0)
                if wait_seconds <= 0:
                    raise
                if wait_seconds > self.settings.max_flood_wait_seconds:
                    print(
                        f"Telegram flood wait {wait_seconds}s exceeds MAX_FLOOD_WAIT_SECONDS="
                        f"{self.settings.max_flood_wait_seconds}; leaving post pending"
                    )
                    return None
                print(f"Telegram flood wait: sleeping {wait_seconds}s before retrying send")
                await asyncio.sleep(wait_seconds)

    def _analyze_bundle(self, bundle: SourceMessageBundle) -> ListingResult | None:
        existing = self.storage.get_listing_record(bundle.chat_id, bundle.message_id)
        if existing:
            if existing["delivery_status"] == "posted":
                return None
            if existing["post_text"]:
                extraction = ListingExtraction.model_validate_json(existing["raw_json"])
                listing_row = self.storage.get_listing_by_id(int(existing["id"]))
                post_text = format_listing_card(
                    extraction,
                    (listing_row["source_link"] if listing_row else None) or bundle.source_link,
                    (listing_row["seller_link"] if listing_row else None) or bundle.seller_link,
                    listing_id=int(existing["id"]),
                    bot_username=self.bot_username,
                    admin_telegram_id=self.settings.admin_telegram_id,
                )
                return ListingResult(
                    listing_id=int(existing["id"]),
                    bundle=bundle,
                    extraction=extraction,
                    post_text=post_text,
                    dedupe_fingerprint=str(existing["listing_fingerprint"]),
                )
            return None

        extraction = self.analyzer.analyze(bundle.text, bundle.media)
        if not extraction.is_listing:
            self.storage.record_event(
                event_type="listing_non_listing",
                telegram_message_id=bundle.message_id,
                metadata={"source_chat": self.settings.source_chat, "has_media": bool(bundle.media)},
            )
            return None

        if not extraction.title:
            extraction.title = "Объявление"

        listing_fingerprint = self.storage.build_listing_fingerprint(extraction)
        duplicate = self.storage.get_listing_by_fingerprint(listing_fingerprint)
        if duplicate is not None:
            self.storage.record_event(
                event_type="listing_duplicate",
                listing_id=int(duplicate["id"]),
                telegram_message_id=bundle.message_id,
                metadata={"listing_fingerprint": listing_fingerprint},
            )
            return None

        listing_id = self.storage.upsert_listing(
            source_chat=self.settings.source_chat,
            source_chat_id=bundle.chat_id,
            source_message_id=bundle.message_id,
            grouped_id=bundle.grouped_id,
            source_fingerprint=self.storage.build_source_fingerprint(bundle.text, len(bundle.media)),
            extraction=extraction,
            raw_json=extraction.model_dump_json(),
            post_text=None,
            seller_link=bundle.seller_link,
            source_link=bundle.source_link,
            delivery_status="analyzed",
        )
        post_text = format_listing_card(
            extraction,
            bundle.source_link,
            bundle.seller_link,
            listing_id=listing_id,
            bot_username=self.bot_username,
            admin_telegram_id=self.settings.admin_telegram_id,
        )
        self.storage.upsert_listing(
            source_chat=self.settings.source_chat,
            source_chat_id=bundle.chat_id,
            source_message_id=bundle.message_id,
            grouped_id=bundle.grouped_id,
            source_fingerprint=self.storage.build_source_fingerprint(bundle.text, len(bundle.media)),
            extraction=extraction,
            raw_json=extraction.model_dump_json(),
            post_text=post_text,
            seller_link=bundle.seller_link,
            source_link=bundle.source_link,
            delivery_status="analyzed",
        )
        self.storage.record_event(
            event_type="listing_analyzed",
            listing_id=listing_id,
            telegram_message_id=bundle.message_id,
            metadata={"category": extraction.category, "media_count": len(bundle.media)},
        )
        return ListingResult(
            listing_id=listing_id,
            bundle=bundle,
            extraction=extraction,
            post_text=post_text,
            dedupe_fingerprint=listing_fingerprint,
        )

    async def run_backfill(self) -> ProcessOutcome:
        outcome = ProcessOutcome()
        bundles = await fetch_recent_bundles(
            self.client,
            self.settings.source_chat,
            self.settings.initial_messages,
            max_age_days=self.settings.max_age_days,
        )
        for bundle in bundles:
            if outcome.processed >= self.settings.max_posts_per_run:
                break
            result = self._analyze_bundle(bundle)
            outcome.processed += 1
            if result is None:
                continue
            if self.settings.is_test:
                print(result.post_text)
                print()
                continue

            sent = await self._send_post(result.post_text, result.listing_id or 0, bundle)
            if sent is None:
                break
            target_message_id = getattr(sent, "id", None)
            if result.listing_id is not None:
                self.storage.set_listing_posted(result.listing_id, target_message_id=target_message_id)
                self.storage.record_event(
                    event_type="listing_published",
                    listing_id=result.listing_id,
                    telegram_message_id=target_message_id,
                    metadata={"source_message_id": bundle.message_id, "has_media": bool(bundle.media)},
                )
            outcome.posted += 1
        return outcome

    async def handle_bundle(self, bundle: SourceMessageBundle) -> ListingResult | None:
        result = self._analyze_bundle(bundle)
        if result is None:
            return None
        if not self.settings.is_test:
            sent = await self._send_post(result.post_text, result.listing_id or 0, bundle)
            if sent is not None and result.listing_id is not None:
                target_message_id = getattr(sent, "id", None)
                self.storage.set_listing_posted(result.listing_id, target_message_id=target_message_id)
                self.storage.record_event(
                    event_type="listing_published",
                    listing_id=result.listing_id,
                    telegram_message_id=target_message_id,
                    metadata={"source_message_id": bundle.message_id, "has_media": bool(bundle.media)},
                )
        return result