"""Validation and labels for the Telegram search filter editor."""

from dataclasses import replace
import re

from .tg_ui import CURRENCY_LABELS, esc, fmt_int

FIELDS = {
    "text": ("🔎 Запрос", "Введите запрос (например, Python backend). До 500 символов."),
    "areas": (
        "📍 Регион",
        "Выберите регион кнопкой ниже или введите Беларусь, Минск либо ID регионов через запятую (16 — Беларусь, 1002 — Минск).",
    ),
    "experience": ("💼 Опыт", "Выберите требуемый опыт кнопкой ниже или введите: любой, без опыта, 1–3, 3–6, более 6."),
    "work_formats": ("🏠 Формат работы", "Отметьте подходящие форматы кнопками и нажмите «Сохранить». Пустой выбор — любой формат."),
    "salary": ("💰 Зарплата", "Выберите сумму или введите число от 0 до 1000000000 в текущей валюте (либо «нет» для сброса)."),
    "currency": ("💱 Валюта", "Выберите валюту кнопкой ниже: BYN, USD, EUR или RUB."),
    "excluded_title_words": ("🚫 Стоп-слова", "Введите подстроки названий через запятую (например: senior, lead). Для очистки — «нет»."),
    "excluded_employer_ids": ("🏢 Искл. компании", "Введите ID компаний через запятую (ID виден в ссылке на работодателя). Для очистки — «нет»."),
}

EXPERIENCE = {
    None: "любой",
    "noExperience": "без опыта",
    "between1And3": "1–3 года",
    "between3And6": "3–6 лет",
    "moreThan6": "более 6 лет",
}

EXPERIENCE_PRESETS = [
    ("Любой опыт", "любой"),
    ("Без опыта", "без опыта"),
    ("1–3 года", "1-3"),
    ("3–6 лет", "3-6"),
    ("Более 6 лет", "более 6"),
]

CURRENCY_PRESETS = [
    ("BYN", "BYN"),
    ("USD", "USD"),
    ("EUR", "EUR"),
    ("RUB", "RUB"),
]

REGION_PRESETS = [
    ("🇧🇾 Беларусь", "16"),
    ("🏙 Минск", "1002"),
    ("🌍 Любой регион", "нет"),
]

WORK_FORMATS = {
    "ON_SITE": "На месте работодателя",
    "REMOTE": "Удалённо",
    "HYBRID": "Гибрид",
    "FIELD_WORK": "Разъездной",
}


def edit_filter(config, field, text):
    if field not in FIELDS:
        raise ValueError("Неизвестный фильтр")
    text = text.strip()
    if (not text and field != "areas") or len(text) > 500:
        raise ValueError("Введите текст длиной от 1 до 500 символов.")
    lower = text.casefold()
    clear = lower in ("нет", "любой", "убрать", "очистить", "-") or (field == "areas" and not text)
    if field == "text":
        value = text
    elif field in ("areas", "excluded_employer_ids"):
        if field == "areas":
            text = {"беларусь": "16", "минск": "1002"}.get(lower, text)
        parts = [x.strip() for x in text.split(",")]
        if clear:
            value = ()
        elif len(parts) > 20 or any(not re.fullmatch(r"[1-9][0-9]{0,11}", x) for x in parts):
            raise ValueError("Нужны положительные числовые ID через запятую (до 20 значений).")
        else:
            value = tuple(dict.fromkeys(parts))
    elif field == "work_formats":
        aliases = {label.casefold(): code for code, label in WORK_FORMATS.items()}
        aliases.update({"удаленно": "REMOTE", "офис": "ON_SITE"})
        parts = [x.strip() for x in text.split(",")]
        codes = [part.upper() if part.upper() in WORK_FORMATS else aliases.get(part.casefold()) for part in parts]
        if clear:
            value = ()
        elif any(code is None for code in codes):
            raise ValueError("Выберите формат кнопками или введите: на месте работодателя, удалённо, гибрид, разъездной.")
        else:
            value = tuple(code for code in WORK_FORMATS if code in codes)
    elif field == "experience":
        aliases = {
            "любой": None,
            "нет": None,
            "без опыта": "noExperience",
            "0": "noExperience",
            "1-3": "between1And3",
            "3-6": "between3And6",
            "более 6": "moreThan6",
            "6+": "moreThan6",
        }
        key = lower.replace("–", "-").replace("—", "-")
        if key not in aliases:
            raise ValueError("Выберите: любой, без опыта, 1–3, 3–6 или более 6.")
        value = aliases[key]
    elif field == "salary":
        normalized = text.replace(" ", "").replace("\u00a0", "")
        if clear:
            value = None
        elif not re.fullmatch(r"[0-9]{1,10}", normalized) or int(normalized) > 1000000000:
            raise ValueError("Введите неотрицательную целую сумму до 1000000000 или «нет».")
        else:
            value = int(normalized)
    elif field == "currency":
        value = {"BYN": "BYR", "RUB": "RUR"}.get(text.upper(), text.upper())
        if value not in ("BYR", "RUR", "USD", "EUR"):
            raise ValueError("Поддерживаются BYN, RUB, USD и EUR.")
    else:
        words = [x.strip() for x in text.split(",")]
        if clear:
            value = ()
        elif len(words) > 50 or any(not word or len(word) > 100 for word in words):
            raise ValueError("Введите до 50 непустых подстрок через запятую, каждая до 100 символов.")
        else:
            value = tuple(dict.fromkeys(words))
    return replace(config, **{field: value})


def describe_filters(config, source):
    regions = ", ".join({"16": "Беларусь", "1002": "Минск"}.get(x, x) for x in config.areas) or "любой"
    formats = ", ".join(WORK_FORMATS[x] for x in config.work_formats) or "любой"
    cur = CURRENCY_LABELS.get(config.currency, config.currency)
    salary_str = f"от {fmt_int(config.salary)} {esc(cur)}" if config.salary is not None else f"не задана ({esc(cur)})"
    stop_words = ", ".join(config.excluded_title_words) or "нет"
    employers = ", ".join(config.excluded_employer_ids) or "нет"
    return (
        "🎛 <b>Поисковые фильтры</b>\n\n"
        f"🔎 <b>Запрос:</b> <code>{esc(config.text, 250)}</code>\n"
        f"📍 <b>Регион:</b> {esc(regions, 200)}\n"
        f"💼 <b>Опыт:</b> {esc(EXPERIENCE.get(config.experience, config.experience))}\n"
        f"🏠 <b>Формат:</b> {esc(formats)}\n"
        f"💰 <b>Зарплата:</b> {salary_str}\n"
        f"🚫 <b>Стоп-слова:</b> {esc(stop_words, 300)}\n"
        f"🏢 <b>Искл. компании:</b> {esc(employers, 200)}\n"
        f"🌐 <b>Источник:</b> <code>{esc(source)}</code>"
    )
