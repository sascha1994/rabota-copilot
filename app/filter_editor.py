"""Validation and labels for the Telegram search filter editor."""

import re
from dataclasses import replace

FIELDS = {
    "text": ("Поисковый запрос", "Введите запрос, например Python backend. До 500 символов."),
    "areas": ("Регион", "Введите Беларусь, Минск или ID регионов через запятую (16 — Беларусь, 1002 — Минск). Чтобы искать без ограничения по региону, нажмите «Любой регион» или отправьте «нет»."),
    "experience": ("Опыт", "Введите: любой, без опыта, 1–3, 3–6 или более 6."),
    "work_formats": ("Формат работы", "Отметьте подходящие форматы кнопками и нажмите «Сохранить». Пустой выбор означает любой формат."),
    "salary": ("Зарплата", "Введите сумму от 0 до 1000000000 в текущей валюте или «нет», чтобы убрать фильтр."),
    "currency": ("Валюта", "Введите BYN, RUB, USD или EUR. BYN и RUB сохраняются как коды сайта BYR и RUR."),
    "excluded_title_words": ("Стоп-слова", "Введите подстроки названий через запятую. Например: senior, lead. Для очистки — «нет»."),
    "excluded_employer_ids": ("Исключённые компании", "Введите ID компаний через запятую. ID можно увидеть в ссылке на компанию на сайте. Для очистки — «нет»."),
}
EXPERIENCE = {None: "любой", "noExperience": "без опыта", "between1And3": "1–3 года",
              "between3And6": "3–6 лет", "moreThan6": "более 6 лет"}
WORK_FORMATS = {"ON_SITE": "На месте работодателя", "REMOTE": "Удалённо", "HYBRID": "Гибрид", "FIELD_WORK": "Разъездной"}


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
        aliases = {"любой": None, "нет": None, "без опыта": "noExperience", "0": "noExperience",
                   "1-3": "between1And3", "3-6": "between3And6", "более 6": "moreThan6", "6+": "moreThan6"}
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
    return (f"Источник: {source}\nЗапрос: {config.text}\nРегион: {regions}\n"
            f"Опыт: {EXPERIENCE.get(config.experience, config.experience)}\n"
            f"Формат работы: {', '.join(WORK_FORMATS[x] for x in config.work_formats) or 'любой'}\n"
            f"Зарплата: {config.salary if config.salary is not None else 'не задана'} {config.currency}\n"
            f"Стоп-слова: {', '.join(config.excluded_title_words) or 'нет'}\n"
            f"Исключённые компании: {', '.join(config.excluded_employer_ids) or 'нет'}")[:3300]
