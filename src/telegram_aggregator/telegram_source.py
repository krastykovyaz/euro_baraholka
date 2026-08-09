from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Iterable

from telethon import TelegramClient, events

from .gemini import guess_mime_type
from .models import SourceMedia, SourceMessageBundle


def _message_text(message: object) -> str:
    text = getattr(message, "raw_text", None) or getattr(message, "message", None) or ""
    return str(text).strip()


def _message_id(message: object) -> int:
    return int(getattr(message, "id"))


def _grouped_id(message: object) -> int | None:
    value = getattr(message, "grouped_id", None)
    return int(value) if value else None


def _build_source_link(message: object) -> str | None:
    link = getattr(message, "link", None)
    if link:
        return str(link)
    chat = getattr(message, "chat", None)
    username = getattr(chat, "username", None) if chat else None
    if username:
        return f"https://t.me/{username}/{_message_id(message)}"
    return None


def _build_seller_link(message: object, sender: object | None) -> str | None:
    if sender is None:
        return None

    username = getattr(sender, "username", None)
    if username:
        return f"https://t.me/{username}"

    sender_id = getattr(sender, "id", None) or getattr(message, "sender_id", None)
    if sender_id:
        return f"tg://user?id={sender_id}"

    return None


def group_messages(messages: Iterable[object]) -> list[list[object]]:
    grouped: list[list[object]] = []
    current: list[object] = []
    current_grouped_id: int | None = None

    for message in messages:
        gid = _grouped_id(message)
        if gid is not None and current and gid == current_grouped_id:
            current.append(message)
            continue
        if current:
            grouped.append(current)
        current = [message]
        current_grouped_id = gid

    if current:
        grouped.append(current)
    return grouped


async def download_group_media(client: TelegramClient, messages: list[object]) -> list[SourceMedia]:
    media: list[SourceMedia] = []
    with tempfile.TemporaryDirectory(prefix="benelux-media-") as temp_dir:
        for message in messages:
            if not getattr(message, "media", None):
                continue
            path = await message.download_media(file=temp_dir)
            if not path:
                continue
            media.append(SourceMedia(data=Path(path).read_bytes(), mime_type=guess_mime_type(str(path))))
    return media


async def bundle_messages(client: TelegramClient, messages: list[object]) -> list[SourceMessageBundle]:
    bundles: list[SourceMessageBundle] = []
    for group in group_messages(messages):
        texts = []
        raw_ids = []
        for message in group:
            raw_ids.append(_message_id(message))
            text = _message_text(message)
            if text and text not in texts:
                texts.append(text)
        media = await download_group_media(client, group)
        leader = group[0]
        sender = None
        try:
            sender = await leader.get_sender()
        except Exception:
            sender = getattr(leader, "sender", None)
        bundles.append(
            SourceMessageBundle(
                message_id=_message_id(leader),
                chat_id=int(getattr(getattr(leader, "chat", None), "id", getattr(leader, "chat_id", 0))),
                grouped_id=_grouped_id(leader),
                text="\n".join(texts).strip(),
                media=media,
                seller_link=_build_seller_link(leader, sender),
                source_link=_build_source_link(leader),
                raw_message_ids=raw_ids,
            )
        )
    return bundles


async def fetch_recent_bundles(client: TelegramClient, source_chat: str, limit: int, *, max_age_days: int = 3) -> list[SourceMessageBundle]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    messages = []
    async for message in client.iter_messages(source_chat, limit=limit):
        message_date = getattr(message, "date", None)
        if message_date is None:
            continue
        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=timezone.utc)
        else:
            message_date = message_date.astimezone(timezone.utc)
        if message_date < cutoff:
            break
        messages.append(message)
    messages.reverse()
    return await bundle_messages(client, messages)


@dataclass(slots=True)
class TelegramEvent:
    chat_id: int
    message_id: int


def should_accept_event(event: events.NewMessage.Event, source_chat: str) -> bool:
    chat = getattr(event, "chat", None)
    username = getattr(chat, "username", None) if chat else None
    return username == source_chat.lstrip("@") or str(getattr(event, "chat_id", "")) == source_chat
