"""Evidence-based daily suggestions. Never infer candidate abilities from vacancies."""

from datetime import datetime, timezone
import hashlib
import json
import re

from .automation import AutomationStore, MINSK
from .candidate_profile import ProfileStore
from .tg_ui import bar, esc, expandable, inline_kb, render

SKILLS = {
    "Figma": r"\bfigma\b|фигм",
    "Adobe Photoshop": r"\bphotoshop\b|фотошоп",
    "Adobe Illustrator": r"\billustrator\b|иллюстратор",
    "Adobe InDesign": r"\bindesign\b",
    "After Effects": r"\bafter\s+effects\b",
    "Blender": r"\bblender\b",
    "Прототипирование": r"прототип",
    "Auto Layout": r"\bauto\s*layout\b",
    "Дизайн-системы": r"дизайн[ -]систем",
    "Типографика": r"типографик",
    "Инфографика": r"инфографик",
    "Подготовка к печати": r"допечат|предпечат|подготовк\w*\s+к\s+печати",
    "Midjourney": r"\bmidjourney\b",
    "Stable Diffusion": r"\bstable\s+diffusion\b",
    "UX-исследования": r"ux[ -]исслед|пользовательск\w*\s+исслед",
    "HTML/CSS": r"\bhtml\b|\bcss\b",
    "Tilda": r"\btilda\b|тильд",
}
CRITERIA = {
    "Удалённая работа": r"удал[её]н|\bremote\b",
    "Гибридный формат": r"гибрид",
    "Наличие портфолио": r"портфолио|\bportfolio\b",
    "Английский язык": r"английск|\benglish\b",
    "Тестовое задание": r"тестов\w*\s+задан",
    "Руководство командой": r"управлени\w*\s+команд|руковод\w*\s+команд",
}


def _is_known_item(item, confirmed_text: str) -> bool:
    if item.get("known"):
        return True
    label = item["label"]
    if item["kind"] == "skill":
        pattern = SKILLS.get(label) or (r"(?<!\w)" + re.escape(label) + r"(?!\w)")
        return bool(re.search(pattern, confirmed_text, re.I))
    return f"— {label}".casefold() in confirmed_text.casefold()


class DailyInsights:
    def __init__(self, path):
        self.path = path
        store = AutomationStore(path)
        try:
            store.db.execute("CREATE TABLE IF NOT EXISTS daily_insights(day TEXT PRIMARY KEY,payload TEXT NOT NULL)")
            store.db.commit()
        finally:
            store.close()

    def build(self, now):
        local = now.astimezone(MINSK)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        end = now.astimezone(timezone.utc).isoformat()
        store = AutomationStore(self.path)
        profile = ProfileStore(self.path)
        try:
            rows = store.db.execute(
                """SELECT DISTINCT v.* FROM vacancies v
                WHERE (v.first_seen>=? AND v.first_seen<=? AND EXISTS
                    (SELECT 1 FROM search_results s WHERE s.vacancy_id=v.id AND s.rejection IS NULL))
                OR EXISTS (SELECT 1 FROM application_events e JOIN application_jobs j ON e.job_id=j.id
                    WHERE j.vacancy_id=v.id AND e.state IN ('PREPARING','PREPARING_QUOTA')
                    AND e.created_at>=? AND e.created_at<=?)""",
                (start, end, start, end),
            ).fetchall()
            confirmed = "\n".join(f["value"] for f in profile.confirmed())
            found = {}
            covered = 0
            for row in rows:
                desc = store.db.execute(
                    "SELECT text FROM vacancy_descriptions WHERE vacancy_id=? AND first_seen<=? ORDER BY id DESC LIMIT 1",
                    (row["id"], end),
                ).fetchone()
                raw = json.loads(row["raw_json"])
                explicit = [s.get("name", "") for s in raw.get("key_skills", []) if isinstance(s, dict)]
                text = "\n".join([row["title"], desc[0] if desc else "", *explicit])
                covered += bool(desc)
                for kind, terms in [("skill", SKILLS), ("criterion", CRITERIA)]:
                    for label, pattern in terms.items():
                        if re.search(pattern, text, re.I):
                            key = (kind, label)
                            entry = found.setdefault(
                                key,
                                {
                                    "kind": kind,
                                    "label": label,
                                    "sources": [],
                                    "known": bool(kind == "skill" and re.search(pattern, confirmed, re.I)),
                                },
                            )
                            entry["sources"].append(
                                {
                                    "id": row["id"],
                                    "title": row["title"],
                                    "url": "https://rabota.by/vacancy/" + str(row["id"]),
                                }
                            )
                for label in set(explicit):
                    label = " ".join(label.split())[:70]
                    if not label or any(re.search(p, label, re.I) for p in SKILLS.values()):
                        continue
                    key = ("skill", label.casefold())
                    entry = found.setdefault(
                        key,
                        {
                            "kind": "skill",
                            "label": label,
                            "sources": [],
                            "known": bool(re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", confirmed, re.I)),
                        },
                    )
                    if not any(s["id"] == row["id"] for s in entry["sources"]):
                        entry["sources"].append(
                            {
                                "id": row["id"],
                                "title": row["title"],
                                "url": "https://rabota.by/vacancy/" + str(row["id"]),
                            }
                        )
            items = []
            for kind, limit in [("skill", 6), ("criterion", 4)]:
                ranked = [v for v in found.values() if v["kind"] == kind and len(v["sources"]) >= 2]
                ranked.sort(key=lambda v: (-len(v["sources"]), v["label"]))
                items.extend(ranked[:limit])
            return {"day": local.date().isoformat(), "total": len(rows), "covered": covered, "items": items}
        finally:
            store.close()
            profile.close()

    def report(self, now=None):
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(MINSK).date().isoformat()
        store = AutomationStore(self.path)
        try:
            row = store.db.execute("SELECT payload FROM daily_insights WHERE day=?", (day,)).fetchone()
            return json.loads(row[0]) if row else self.build(now)
        finally:
            store.close()

    async def show(self, message, report=None, *, edit=False):
        report = report or self.report()
        # Persist the exact displayed suggestions first so button indices remain stable.
        store = AutomationStore(self.path)
        profile = ProfileStore(self.path)
        try:
            with store.db:
                store.db.execute(
                    "INSERT OR IGNORE INTO daily_insights VALUES (?,?)",
                    (report["day"], json.dumps(report, ensure_ascii=False)),
                )
            confirmed_text = "\n".join(f["value"] for f in profile.confirmed())
        finally:
            store.close()
            profile.close()

        lines = [
            f"📈 <b>Сводка навыков и критериев · {esc(report['day'])}</b>",
            f"Вакансий за день: <b>{report['total']}</b> · полных описаний: <b>{report['covered']}</b>",
        ]
        rows = []
        total = max(1, int(report["total"] or 1))
        for index, item in enumerate(report["items"]):
            count = len(item["sources"])
            known = _is_known_item(item, confirmed_text)
            icon = "🛠" if item["kind"] == "skill" else "📌"
            badge = " · ✅ <i>в профиле</i>" if known else ""
            lines.append(
                f"\n{icon} <b>{esc(item['label'], 70)}</b> — <code>{bar(count, total, 6)}</code> "
                f"<b>{count}/{report['total']}</b>{badge}"
            )
            sample_links = []
            for s in item["sources"][:2]:
                url = str(s.get("url") or "")
                title = esc(s.get("title", "Вакансия"), 48)
                if url.startswith("https://rabota.by/vacancy/"):
                    sample_links.append(f'<a href="{esc(url)}">{title}</a>')
                else:
                    sample_links.append(title)
            if sample_links:
                lines.append("   ↳ " + " · ".join(sample_links))
            if not known:
                prefix = "➕ Умею: " if item["kind"] == "skill" else "➕ Критерий: "
                rows.append([(prefix + item["label"][:38], f"insight:add:{report['day']}:{index}")])
        if not report["items"]:
            lines.append("\n<i>Повторяющихся навыков и критериев (минимум в 2 вакансиях) пока недостаточно.</i>")
        lines.append(
            "\n" + expandable(
                "Частота — число разных вакансий за день с упоминанием пункта. "
                "Кнопка «➕ Умею» подтверждает навык в профиле, а «➕ Критерий» сохраняет ориентир для оценки ИИ "
                "(поисковые фильтры при этом не меняются).",
                400,
            )
        )
        await render(message, "\n".join(lines), edit=edit, reply_markup=inline_kb(rows) if rows else None)

    async def due(self, message_factory, now=None):
        now = now or datetime.now(timezone.utc)
        store = AutomationStore(self.path)
        try:
            local = now.astimezone(MINSK)
            settings = store.settings()
            if local.strftime("%H:%M") < settings["insights_time"]:
                return
            if store.db.execute("SELECT 1 FROM daily_insights WHERE day=?", (local.date().isoformat(),)).fetchone():
                return
        finally:
            store.close()
        await self.show(message_factory, self.report(now))

    async def add(self, callback, actor):
        _, action, day, index = callback.data.split(":")
        if action != "add":
            raise ValueError("Неизвестное действие.")
        store = AutomationStore(self.path)
        try:
            row = store.db.execute("SELECT payload FROM daily_insights WHERE day=?", (day,)).fetchone()
            if not row:
                raise ValueError("Сводка не найдена.")
            payload = json.loads(row[0])
            items = payload["items"]
            index = int(index)
            if not 0 <= index < len(items):
                raise ValueError("Предложение не найдено.")
            item = items[index]
        finally:
            store.close()
        value = item["label"] if item["kind"] == "skill" else "Предпочтение: учитывать при оценке вакансий — " + item["label"]
        ident = "trend_" + hashlib.sha256((item["kind"] + item["label"]).encode()).hexdigest()[:18]
        profile = ProfileStore(self.path)
        try:
            profile.import_draft(
                {
                    "facts": [
                        {
                            "id": ident,
                            "category": "Навыки" if item["kind"] == "skill" else "Критерии поиска",
                            "value": value,
                            "sources": [{"document": "Подтверждение пользователя по сводке " + day, "page": 1, "quote": value}],
                        }
                    ]
                }
            )
            fact = profile.get(ident)
            if fact["status"] != "CONFIRMED":
                profile.update(ident, fact["revision"], "confirm", actor)
        finally:
            profile.close()
        await callback.answer(f"Добавлено: {item['label']}"[:180])
        if getattr(callback.message, "bot", None) is not None:
            await self.show(callback.message, payload, edit=True)
        else:
            await render(
                callback.message,
                f"✅ <b>Сохранено в профиль:</b> {esc(value, 300)}\n"
                "<i>Изменение профиля может потребовать пересборки ранее подготовленных пакетов.</i>",
            )
