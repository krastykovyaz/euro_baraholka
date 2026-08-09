from __future__ import annotations

from .models import ListingExtraction


CATEGORY_EMOJI = {
    "Дом": "🏠",
    "Мебель": "🪑",
    "Электроника": "💻",
    "Одежда": "👕",
    "Детское": "🧸",
    "Спорт": "🏃",
    "Авто": "🚗",
    "Услуги": "🛠",
    "Другое": "📦",
}

CONDITION_LABELS = {
    "new": "новое",
    "used": "б/у",
    "like new": "почти новое",
    "almost new": "почти новое",
}


def _format_price(price: float | None, currency: str | None) -> str | None:
    if price is None:
        return None
    if currency == "EUR":
        if float(price).is_integer():
            return f"€{int(price)}"
        return f"€{price:g}"
    if currency:
        if float(price).is_integer():
            return f"{int(price)} {currency}"
        return f"{price:g} {currency}"
    if float(price).is_integer():
        return f"{int(price)}"
    return f"{price:g}"


def _format_condition(condition: str | None) -> str | None:
    if not condition:
        return None
    normalized = condition.strip().lower()
    return CONDITION_LABELS.get(normalized, condition.strip())


def safe_deal_start_param(listing_id: int) -> str:
    return f"sd_{listing_id}"


def safe_deal_url(bot_username: str, listing_id: int) -> str:
    username = bot_username.lstrip("@")
    return f"https://t.me/{username}?start={safe_deal_start_param(listing_id)}"


def safe_deal_admin_url(admin_telegram_id: int) -> str:
    """Запасная ссылка на админа, когда бот не настроен/не поднят."""
    return f"tg://user?id={admin_telegram_id}"


def parse_safe_deal_start(payload: str | None) -> int | None:
    if not payload:
        return None
    payload = payload.strip()
    if not payload.startswith("sd_"):
        return None
    try:
        return int(payload[3:])
    except ValueError:
        return None


def format_safe_deal_line(
    listing_id: int,
    *,
    bot_username: str | None = None,
    admin_telegram_id: int | None = None,
) -> str | None:
    """Строит строку «🛡 Безопасная сделка» со ссылкой.

    Основной путь — диплинк на бота (start=sd_<id>): заводит покупателя
    в флоу safe_deal -> safe_apply -> safe_sent через кнопки в боте.

    Если бот не настроен (bot_username пуст, например бот сейчас лежит
    или запущен не в bot-режиме) — используется запасной вариант:
    прямая ссылка на админа из конфига, чтобы кнопка в объявлении не
    пропадала молча, как это происходило раньше.
    """
    if bot_username:
        return f"🛡 [Безопасная сделка]({safe_deal_url(bot_username, listing_id)})"
    if admin_telegram_id:
        return f"🛡 [Безопасная сделка]({safe_deal_admin_url(admin_telegram_id)})"
    return None


def ensure_safe_deal_line(
    post_text: str,
    listing_id: int,
    *,
    bot_username: str | None = None,
    admin_telegram_id: int | None = None,
) -> str:
    if "Безопасная сделка](" in post_text:
        return post_text
    line = format_safe_deal_line(listing_id, bot_username=bot_username, admin_telegram_id=admin_telegram_id)
    if not line:
        return post_text
    lines = post_text.split("\n")
    insert_at = len(lines)
    for index, row in enumerate(lines):
        if row.startswith("👤") or row.startswith("🔗"):
            insert_at = index
            break
    lines.insert(insert_at, line)
    return "\n".join(lines)


def format_listing_card(
    extraction: ListingExtraction,
    source_link: str | None = None,
    seller_link: str | None = None,
    *,
    listing_id: int | None = None,
    bot_username: str | None = None,
    admin_telegram_id: int | None = None,
) -> str:
    lines: list[str] = []

    category = extraction.category or "Другое"
    emoji = CATEGORY_EMOJI.get(category, "📦")
    title = extraction.title or "Объявление"
    lines.append(f"{emoji} {title}")

    price = _format_price(extraction.price, extraction.currency)
    if price:
        lines.append(f"💶 {price}")

    if extraction.location:
        lines.append(f"📍 {extraction.location}")

    condition = _format_condition(extraction.condition)
    if condition:
        lines.append(f"🟢 Состояние: {condition}")

    if extraction.description:
        lines.append(extraction.description)

    if listing_id is not None:
        safe_deal_line = format_safe_deal_line(
            listing_id,
            bot_username=bot_username,
            admin_telegram_id=admin_telegram_id,
        )
        if safe_deal_line:
            lines.append(safe_deal_line)

    if seller_link:
        lines.append(f"👤 [Продавец]({seller_link})")

    if source_link:
        lines.append(f"🔗 [Оригинальное объявление]({source_link})")
    else:
        lines.append("🔗 Оригинальное объявление")

    return "\n".join(lines)