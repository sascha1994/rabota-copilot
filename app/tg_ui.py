"""Shared HTML formatting, visual indicators and safe message editing for Telegram UI."""

from datetime import datetime, timezone
import html

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

MONTHS_RU = (
    "", "янв", "фев", "мар", "апр", "мая", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
)

CURRENCY_LABELS = {"BYR": "BYN", "RUR": "RUB", "BYN": "BYN", "RUB": "RUB", "USD": "USD", "EUR": "EUR"}

JOB_STATE_BADGES = {
    "QUEUED": ("⏳", "Ожидает обработки"),
    "WAITING_AI": ("⏸", "Ожидает обновления лимита OpenAI"),
    "PREPARING": ("⚙️", "Подготовка"),
    "REVIEW": ("🟡", "Нужна проверка"),
    "READY": ("🟢", "Готово к отправке"),
    "BLOCKED": ("⛔", "Отправка остановлена"),
    "SENDING": ("📤", "Отправляется"),
    "SENT": ("✅", "Отправлено"),
    "UNKNOWN": ("⚠️", "Исход неизвестен"),
    "UNKNOWN_DISMISSED": ("🗃", "Снято с проверки · исход неизвестен"),
    "REJECTED": ("✖️", "Отклонено"),
    "UNSUITABLE": ("📉", "Не подошло"),
    "FAILED": ("❌", "Ошибка подготовки"),
    "STALE": ("🔄", "Пакет устарел"),
}


def esc(value, limit=None) -> str:
    """Escape untrusted text for Telegram HTML mode, truncating before escaping."""
    text = str(value if value is not None else "")
    if limit is not None and len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return html.escape(text, quote=True)


def bar(current: int, total: int, width: int = 10) -> str:
    """Render a compact unicode progress bar."""
    if total <= 0:
        return "▱" * width
    ratio = min(max(current / total, 0.0), 1.0)
    filled = round(ratio * width)
    return "▰" * filled + "▱" * (width - filled)


def fmt_int(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


def fmt_dt(raw, tz=None, *, suffix: str = "") -> str:
    """Format ISO-8601 timestamps into concise Russian date-time strings."""
    if not raw:
        return "—"
    text = str(raw).strip()
    try:
        normalized = text.replace("Z", "+00:00")
        # Handle +0300 without colon returned by HH APIs
        if len(normalized) >= 5 and normalized[-5] in ("+", "-") and normalized[-4:].isdigit():
            normalized = normalized[:-2] + ":" + normalized[-2:]
        dt = datetime.fromisoformat(normalized)
        if tz is not None and dt.tzinfo is not None:
            dt = dt.astimezone(tz)
        stamp = f"{dt.day} {MONTHS_RU[dt.month]}, {dt:%H:%M}"
        if suffix:
            stamp += f" {suffix}"
        return stamp
    except (ValueError, IndexError):
        return esc(text, 32)


def fmt_salary(salary) -> str:
    """Format HH/rabota.by salary dictionary with readable currency and spacing."""
    if not isinstance(salary, dict) or (not salary.get("from") and not salary.get("to")):
        return "не указана"
    low, high = salary.get("from"), salary.get("to")
    raw_cur = str(salary.get("currency") or "").upper()
    cur = esc(CURRENCY_LABELS.get(raw_cur, raw_cur), 12)
    if low and high:
        amount = fmt_int(low) if low == high else f"{fmt_int(low)} – {fmt_int(high)}"
    elif low:
        amount = f"от {fmt_int(low)}"
    else:
        amount = f"до {fmt_int(high)}"
    gross = salary.get("gross")
    tax = " (до вычета)" if gross is True else (" (на руки)" if gross is False else "")
    return f"{amount} {cur}{tax}".strip()


def score_badge(score: int) -> str:
    icon = "🟢" if score >= 80 else ("🟡" if score >= 60 else "🔴")
    return f"{icon} <b>{int(score)}/100</b>"


def expandable(text: str, limit: int = 2400) -> str:
    """Wrap plain untrusted text in a native Telegram collapsible blockquote."""
    return f"<blockquote expandable>{esc(text, limit)}</blockquote>"


def inline_kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
            if row
        ]
    )


def _can_edit(message) -> bool:
    if message is None:
        return False
    if "edit_text" in getattr(message, "__dict__", {}):
        return True
    return getattr(message, "bot", None) is not None and hasattr(message, "edit_text")


async def render(message, text: str, *, edit: bool = False, reply_markup=None,
                 parse_mode: str | None = "HTML", disable_preview: bool = True):
    """Send a new message or smoothly edit the current message in-place when possible."""
    kwargs = {"parse_mode": parse_mode}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    if disable_preview:
        kwargs["link_preview_options"] = LinkPreviewOptions(is_disabled=True)
    if edit and _can_edit(message):
        try:
            return await message.edit_text(text, **kwargs)
        except TelegramAPIError as exc:
            if "message is not modified" in str(exc).lower():
                return message
    return await message.answer(text, **kwargs)
