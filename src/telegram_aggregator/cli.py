from __future__ import annotations

import argparse
import asyncio
import csv
import logging
from pathlib import Path
from tempfile import NamedTemporaryFile

from telethon import Button, TelegramClient, events

from .config import load_settings
from .formatter import parse_safe_deal_start
from .gemini import GeminiAnalyzer
from .models import STATUS_EVENT_MAP
from .pipeline import ListingPipeline
from .storage import Storage
from .telegram_source import bundle_messages


ACTIVE_APPLICATION_STATUSES = {"NEW", "CONTACTED", "SELLER_CONTACTED", "SELLER_INTERESTED", "DEAL_IN_PROGRESS"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Telegram listings aggregator MVP")
    parser.add_argument("--once", action="store_true", help="Run a single backfill pass and exit")
    return parser


def _listing_url(target_channel: str, target_message_id: int | None) -> str | None:
    if not target_message_id:
        return None
    channel = target_channel.lstrip("@")
    if not channel:
        return None
    return f"https://t.me/{channel}/{target_message_id}"


def _price_text(value: float | None, currency: str | None) -> str:
    if value is None:
        return "n/a"
    if currency == "EUR":
        return f"€{int(value)}" if float(value).is_integer() else f"€{value:g}"
    return f"{value:g} {currency or ''}".strip()


def _admin_application_buttons(application_id: int) -> list[list[Button]]:
    return [
        [
            Button.inline("Взял в работу", data=f"app_status:{application_id}:CONTACTED".encode("utf-8")),
            Button.inline("Связался", data=f"app_status:{application_id}:SELLER_CONTACTED".encode("utf-8")),
        ],
        [
            Button.inline("Сделка началась", data=f"app_status:{application_id}:DEAL_IN_PROGRESS".encode("utf-8")),
        ],
        [
            Button.inline("Завершена", data=f"app_status:{application_id}:COMPLETED".encode("utf-8")),
            Button.inline("Отменена", data=f"app_status:{application_id}:CANCELLED".encode("utf-8")),
            Button.inline("Проблема", data=f"app_status:{application_id}:DISPUTED".encode("utf-8")),
        ],
    ]


def _buyer_feedback_buttons(application_id: int) -> list[list[Button]]:
    return [
        [
            Button.inline("👍 Всё хорошо", data=f"deal_feedback:{application_id}:good".encode("utf-8")),
            Button.inline("👎 Была проблема", data=f"deal_feedback:{application_id}:bad".encode("utf-8")),
        ]
    ]


def _safe_deal_buttons(listing_id: int, safe_state: str) -> list[list[Button]]:
    if safe_state == "safe_deal":
        button = Button.inline("🛡 Безопасная сделка", data=f"safe_deal:{listing_id}".encode("utf-8"))
    elif safe_state == "safe_apply":
        button = Button.inline("Оставить заявку", data=f"safe_apply:{listing_id}".encode("utf-8"))
    elif safe_state == "safe_sent":
        button = Button.inline("✅ Заявка отправлена", data=f"safe_sent:{listing_id}".encode("utf-8"))
    else:
        raise ValueError(f"unknown safe_state: {safe_state}")
    return [[button]]


def _format_stats_block(title: str, rows: list[dict[str, object]], key_field: str) -> str:
    lines = [title]
    for row in rows:
        label = str(row.get(key_field, "n/a"))
        parts = [label]
        for k, v in row.items():
            if k == key_field:
                continue
            parts.append(f"{k}: {v}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _format_funnel(stats: dict[str, object]) -> str:
    lines = [
        "📊 SAFE DEAL FUNNEL",
        f"Объявлений: {stats['listings']}",
        f"💬 Хочу купить: {stats['buy_clicks']}",
        f"🛡 Безопасная сделка: {stats['safe_deal_clicks']}",
        f"📝 Заявок: {stats['applications']}",
        f"🤝 Сделок начато: {stats['deals_started']}",
        f"✅ Завершено: {stats['completed']}",
        f"❌ Отменено: {stats['cancelled']}",
        f"⚠️ Проблем: {stats['disputed']}",
        "",
        f"Buy → Safe Deal: {stats['buy_to_safe'] if stats['buy_to_safe'] is not None else 'n/a'}%",
        f"Safe Deal → Application: {stats['safe_to_application'] if stats['safe_to_application'] is not None else 'n/a'}%",
        f"Application → Deal: {stats['application_to_deal'] if stats['application_to_deal'] is not None else 'n/a'}%",
        f"Deal → Completed: {stats['deal_to_completed'] if stats['deal_to_completed'] is not None else 'n/a'}%",
    ]
    return "\n".join(lines)


async def _run(once: bool) -> None:
    settings = load_settings()
    storage = Storage(settings.database_path)
    analyzer = GeminiAnalyzer(settings.gemini_api_key, settings.gemini_model)

    # user_client: авторизуется по телефону (TELEGRAM_SESSION). Читает
    # SOURCE_CHAT и постит карточки в TARGET_CHANNEL.
    user_client = TelegramClient(settings.telegram_session, settings.telegram_api_id, settings.telegram_api_hash)

    # bot_client: авторизуется по токену, ОБЯЗАТЕЛЬНО в отдельном
    # session-файле (TELEGRAM_BOT_SESSION). Нельзя переиспользовать
    # session_file юзер-клиента — Telethon в этом случае оставит уже
    # существующую юзер-авторизацию и молча проигнорирует bot_token,
    # из-за чего бот фактически работал от лица личного аккаунта.
    bot_client: TelegramClient | None = None
    if settings.telegram_bot_token:
        bot_client = TelegramClient(
            settings.telegram_bot_session, settings.telegram_api_id, settings.telegram_api_hash
        )

    pipeline = ListingPipeline(settings=settings, storage=storage, analyzer=analyzer, client=user_client)

    await user_client.start()
    logging.info("User client started (session=%s)", settings.telegram_session)

    if bot_client is not None:
        await bot_client.start(bot_token=settings.telegram_bot_token)
        me = await bot_client.get_me()
        pipeline.bot_username = getattr(me, "username", None)
        logging.info("Bot client started as @%s (session=%s)", pipeline.bot_username, settings.telegram_bot_session)
    else:
        logging.warning("TELEGRAM_BOT_TOKEN is missing: callbacks and DM flows will not work")

    async def _send_admin_notification(application_id: int) -> None:
        if not settings.admin_telegram_id or bot_client is None:
            return
        application = storage.get_application(application_id)
        listing = storage.get_listing_by_id(int(application["listing_id"]))
        if application is None or listing is None:
            return
        buyer_username = f"@{application['user_username']}" if application["user_username"] else f"ID {application['user_id']}"
        text = (
            "🛡 НОВАЯ ЗАЯВКА\n"
            f"Объявление:\n{listing['title'] or 'Объявление'}\n"
            f"Цена:\n{_price_text(listing['price'], listing['currency'])}\n"
            f"Локация:\n{listing['location'] or 'n/a'}\n"
            f"Покупатель:\n{buyer_username}\n"
            f"Telegram ID:\n{application['user_id']}\n"
            f"Application:\n#{application_id}\n"
            f"Статус:\n{application['status']}"
        )
        target_message_id = (
            int(application["target_message_id"])
            if application["target_message_id"] is not None
            else None
        )
        listing_url = (
            _listing_url(settings.target_channel, target_message_id)
            or listing["source_link"]
            or f"https://t.me/{settings.target_channel.lstrip('@')}"
        )
        contact_button = (
            [Button.url("Связаться с покупателем", f"https://t.me/{application['user_username']}")]
            if application["user_username"]
            else []
        )
        buttons = [
            [Button.url("Открыть объявление", listing_url)],
            contact_button,
            _admin_application_buttons(application_id)[0],
            _admin_application_buttons(application_id)[1],
            _admin_application_buttons(application_id)[2],
        ]
        buttons = [row for row in buttons if row]
        await bot_client.send_message(settings.admin_telegram_id, text, buttons=buttons)

    async def _send_buyer_message(user_id: int, application_id: int, status: str) -> None:
        if bot_client is None:
            return
        if status == "COMPLETED":
            await bot_client.send_message(
                user_id,
                "✅ Сделка отмечена как завершённая.\nСпасибо! Как всё прошло?",
                buttons=_buyer_feedback_buttons(application_id),
            )
        elif status == "CANCELLED":
            await bot_client.send_message(user_id, "Сделка не состоялась.\nСпасибо за обратную связь.")
        elif status == "DISPUTED":
            await bot_client.send_message(user_id, "⚠️ Мы зафиксировали проблему.\nМы свяжемся с вами.")

    def _record_safe_deal_click(listing_id: int, sender_id: int) -> None:
        listing = storage.get_listing_by_id(listing_id)
        if listing is None:
            return
        storage.record_event(
            event_type="safe_deal_click",
            listing_id=listing_id,
            telegram_message_id=int(listing["target_message_id"]) if listing["target_message_id"] else None,
            target_message_id=int(listing["target_message_id"]) if listing["target_message_id"] else None,
            source_chat_id=int(listing["source_chat_id"]) if listing["source_chat_id"] is not None else None,
            source_message_id=int(listing["source_message_id"]) if listing["source_message_id"] is not None else None,
            user_id=sender_id,
        )

    async def _set_safe_deal_state(event: events.CallbackQuery.Event, listing_id: int, state: str) -> None:
        try:
            await event.edit(buttons=_safe_deal_buttons(listing_id, state))
        except Exception:
            pass

    try:
        if settings.is_test:
            logging.warning("MODE=TEST: cards will be printed only and will not be sent to %s", settings.target_channel)
        else:
            logging.info("MODE=LIVE: cards will be posted to %s", settings.target_channel)
        await pipeline.run_backfill()
        if once or settings.is_test:
            return

        @user_client.on(events.NewMessage(chats=settings.source_chat))
        async def _source_handler(event: events.NewMessage.Event) -> None:
            if not event.message:
                return
            bundled = await bundle_messages(user_client, [event.message])
            for bundle in bundled:
                result = await pipeline.handle_bundle(bundle)
                if result is None:
                    continue

        if bot_client is not None:

            @bot_client.on(events.NewMessage(pattern=r"^/start(?:\s+(.+))?$"))
            async def _start_handler(event: events.NewMessage.Event) -> None:
                listing_id = parse_safe_deal_start(event.pattern_match.group(1))
                if listing_id is None:
                    return
                listing = storage.get_listing_by_id(listing_id)
                if listing is None:
                    await event.reply("Объявление не найдено")
                    return

                sender_id = int(event.sender_id or 0)
                title = listing["title"] or "Объявление"

                # если у пользователя уже есть активная заявка по этому объявлению —
                # сразу показываем финальное состояние, не даём кликать заново
                active = storage.get_active_application(listing_id, sender_id)
                initial_state = "safe_sent" if active is not None else "safe_deal"

                await event.reply(
                    f"🛡 Безопасная сделка\n\n{title}",
                    buttons=_safe_deal_buttons(listing_id, initial_state),
                )

            @bot_client.on(events.CallbackQuery)
            async def _callback_handler(event: events.CallbackQuery.Event) -> None:
                data = (event.data or b"").decode("utf-8", errors="ignore")
                sender_id = int(event.sender_id or 0)
                sender = await event.get_sender()
                sender_username = getattr(sender, "username", None)

                # --- Клик 1: 🛡 Безопасная сделка ---
                if data.startswith("safe_deal:"):
                    listing_id = int(data.split(":", 1)[1])
                    listing = storage.get_listing_by_id(listing_id)
                    if listing is None:
                        await event.answer("Объявление не найдено", alert=True)
                        return

                    _record_safe_deal_click(listing_id, sender_id)

                    await _set_safe_deal_state(event, listing_id, "safe_apply")
                    await event.answer()
                    return

                # --- Клик 2: Оставить заявку ---
                if data.startswith("safe_apply:"):
                    listing_id = int(data.split(":", 1)[1])
                    listing = storage.get_listing_by_id(listing_id)
                    if listing is None:
                        await event.answer("Объявление не найдено", alert=True)
                        return

                    active = storage.get_active_application(listing_id, sender_id)
                    if active is not None:
                        # дубль: заявку не создаём, просто переводим кнопку в финальное состояние
                        await _set_safe_deal_state(event, listing_id, "safe_sent")
                        await event.answer("У вас уже есть активная заявка по этому объявлению", alert=True)
                        return

                    app_id, created = storage.create_application(
                        listing_id=listing_id,
                        user_id=sender_id,
                        target_message_id=int(listing["target_message_id"]) if listing["target_message_id"] else None,
                        user_username=sender_username,
                        role="buyer",
                        status="NEW",
                    )
                    if created:
                        storage.record_event(
                            event_type="safe_deal_application",
                            listing_id=listing_id,
                            telegram_message_id=int(listing["target_message_id"]) if listing["target_message_id"] else None,
                            target_message_id=int(listing["target_message_id"]) if listing["target_message_id"] else None,
                            source_chat_id=int(listing["source_chat_id"]) if listing["source_chat_id"] is not None else None,
                            source_message_id=int(listing["source_message_id"]) if listing["source_message_id"] is not None else None,
                            user_id=sender_id,
                            application_id=app_id,
                        )
                        await _send_admin_notification(app_id)

                    await _set_safe_deal_state(event, listing_id, "safe_sent")
                    await event.answer("Заявка получена. Мы свяжемся с вами.", alert=True)
                    return

                # --- Клик по уже финальному состоянию ---
                if data.startswith("safe_sent:"):
                    await event.answer("Заявка уже отправлена", alert=True)
                    return

                # --- Админ меняет статус заявки ---
                if data.startswith("app_status:"):
                    if not settings.admin_telegram_id or sender_id != settings.admin_telegram_id:
                        await event.answer("Нет доступа", alert=True)
                        return
                    _, app_id_str, new_status = data.split(":", 2)
                    application_id = int(app_id_str)
                    updated = storage.update_application_status(application_id, new_status, sender_id)
                    if updated is None:
                        await event.answer("Заявка не найдена", alert=True)
                        return
                    storage.record_event(
                        event_type=STATUS_EVENT_MAP.get(new_status, "application_status_changed"),
                        listing_id=int(updated["listing_id"]),
                        telegram_message_id=int(updated["target_message_id"]) if updated["target_message_id"] else None,
                        user_id=int(updated["user_id"]),
                        application_id=application_id,
                        metadata={"new_status": new_status},
                    )
                    await event.answer(f"Статус обновлен: {new_status}", alert=True)
                    if new_status in {"COMPLETED", "CANCELLED", "DISPUTED"}:
                        await _send_buyer_message(int(updated["user_id"]), application_id, new_status)
                    return

                # --- Покупатель оставляет фидбек по сделке ---
                if data.startswith("deal_feedback:"):
                    _, app_id_str, feedback = data.split(":", 2)
                    application_id = int(app_id_str)
                    storage.record_event(
                        event_type=f"deal_feedback_{feedback}",
                        application_id=application_id,
                        user_id=sender_id,
                    )
                    await event.answer("Спасибо за обратную связь", alert=True)
                    return


            @bot_client.on(events.NewMessage(pattern=r"^/stats(?:\s+(\d+))?$"))
            async def _stats_handler(event: events.NewMessage.Event) -> None:
                if not settings.admin_telegram_id or event.sender_id != settings.admin_telegram_id:
                    return
                days = int(event.pattern_match.group(1)) if event.pattern_match.group(1) else None
                stats = storage.get_funnel_stats(days)
                lines = [_format_funnel(stats)]
                category_rows = storage.get_category_stats(days)
                if category_rows:
                    lines.append("")
                    lines.append("📊 CATEGORY")
                    for row in category_rows:
                        lines.append(
                            f"{row['category']}\n"
                            f"Listings: {row['listings']}\n"
                            f"Safe Deal: {row['safe_deal_clicks']}\n"
                            f"Applications: {row['applications']}\n"
                            f"Completed: {row['completed']}"
                        )
                source_rows = storage.get_source_stats(days)
                if source_rows:
                    lines.append("")
                    lines.append("📊 SOURCES")
                    for row in source_rows:
                        lines.append(
                            f"{row['source']}\n"
                            f"Listings: {row['listings']}\n"
                            f"Buy clicks: {row['buy_clicks']}\n"
                            f"Safe Deal: {row['safe_deal_clicks']}\n"
                            f"Completed: {row['completed']}"
                        )
                price_rows = storage.get_price_bucket_stats(days)
                if price_rows:
                    lines.append("")
                    lines.append("📊 PRICE BUCKETS")
                    for row in price_rows:
                        lines.append(
                            f"{row['price_bucket']}\n"
                            f"Listings: {row['listings']}\n"
                            f"Safe Deal: {row['safe_deal_requests']}\n"
                            f"Applications: {row['applications']}\n"
                            f"Completed: {row['completed']}"
                        )
                await event.reply("\n\n".join(lines))

            @bot_client.on(events.NewMessage(pattern=r"^/export(?:\s+(\d+))?$"))
            async def _export_handler(event: events.NewMessage.Event) -> None:
                if not settings.admin_telegram_id or event.sender_id != settings.admin_telegram_id:
                    return
                days = int(event.pattern_match.group(1)) if event.pattern_match.group(1) else None
                rows = storage.get_listing_export_rows(days)
                with NamedTemporaryFile("w", delete=False, suffix=".csv", newline="", encoding="utf-8") as tmp:
                    writer = csv.DictWriter(
                        tmp,
                        fieldnames=[
                            "listing_id",
                            "source",
                            "category",
                            "price",
                            "currency",
                            "buy_clicks",
                            "safe_deal_clicks",
                            "applications",
                            "deals_started",
                            "completed",
                            "cancelled",
                            "disputed",
                            "created_at",
                        ],
                    )
                    writer.writeheader()
                    writer.writerows(rows)
                    tmp_path = Path(tmp.name)
                await event.reply(file=tmp_path, message="Экспорт аналитики CSV")

        print("Listening for new messages...")
        clients_to_run = [user_client.run_until_disconnected()]
        if bot_client is not None:
            clients_to_run.append(bot_client.run_until_disconnected())
        await asyncio.gather(*clients_to_run)
    finally:
        storage.close()
        if user_client.is_connected():
            await user_client.disconnect()
        if bot_client is not None and bot_client.is_connected():
            await bot_client.disconnect()


def main() -> None:
    logging.basicConfig(
        format="[%(levelname)s %(asctime)s] %(name)s: %(message)s",
        level=logging.INFO,
    )
    args = build_parser().parse_args()
    asyncio.run(_run(args.once))


if __name__ == "__main__":
    main()