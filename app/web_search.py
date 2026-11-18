"""Read public search HTML; no internal API, login, cookies or anti-bot bypass."""

import json
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .collector import Stats, search_key
from .hh_client import ApiError


class WebSearchError(ApiError):
    @property
    def public_message(self):
        return str(self)  # This class only contains controlled local messages.


class StateParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.active = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "template" and dict(attrs).get("id") == "HH-Lux-InitialState":
            self.active = True

    def handle_endtag(self, tag):
        if tag == "template":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)


def parse_search(document):
    parser = StateParser()
    parser.feed(document)
    try:
        state = json.loads("".join(parser.parts))
        result = state["vacancySearchResult"]
        vacancies = result["vacancies"]
        total = result["totalResults"]
        paging = result["paging"]
        next_page = {"disabled": True} if paging is None else paging["next"]
        if (not isinstance(vacancies, list) or type(total) is not int or total < 0
                or not isinstance(next_page.get("disabled"), bool)
                or any(not isinstance(v, dict) or not str(v.get("vacancyId", "")).isdigit()
                       or not isinstance(v.get("name"), str) for v in vacancies)):
            raise ValueError()
        if total > 0 and not vacancies:
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise WebSearchError("Не удалось прочитать страницу поиска rabota.by: возможно, изменилась разметка, требуется вход или проверка CAPTCHA. Автоматический обход проверки не выполняется.") from None
    items = []
    for vacancy in vacancies:
        company = vacancy.get("company") or {}
        compensation = vacancy.get("compensation") or {}
        salary = None
        if compensation.get("from") is not None or compensation.get("to") is not None:
            salary = {"from": compensation.get("from"), "to": compensation.get("to"),
                      "currency": compensation.get("currencyCode"), "gross": compensation.get("gross")}
        area = vacancy.get("area") or {}
        publication = vacancy.get("publicationTime") or {}
        vid = str(vacancy["vacancyId"])
        items.append({"id": vid, "name": vacancy["name"],
                      "employer": {"id": str(company["id"]) if company.get("id") is not None else None,
                                   "name": company.get("visibleName") or company.get("name")},
                      "area": {"id": str(area.get("@id", "")), "name": area.get("name")},
                      "salary": salary, "published_at": publication.get("$") if isinstance(publication, dict) else None,
                      "alternate_url": "https://rabota.by/vacancy/" + vid,
                      "source": "rabota.by/web"})
    return {"items": items, "has_next": not next_page["disabled"], "found": total}


class WebSearchClient:
    def __init__(self, *, opener=urlopen, sleep=time.sleep, delay=3):
        self.opener, self.sleep, self.delay = opener, sleep, delay
        self.last_request = None

    def search(self, config, *, days=1, page=0):
        if self.last_request is not None:
            self.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        params = {"text": config.text, "page": page,
                  "order_by": "publication_time", "search_period": days}
        if config.areas:
            params["area"] = config.areas
        if config.experience:
            params["experience"] = config.experience
        if config.work_formats:
            params["work_format"] = config.work_formats
        if config.salary is not None:
            params.update(salary=config.salary, currency_code=config.currency)
        request = Request("https://rabota.by/search/vacancy?" + urlencode(params, doseq=True),
                          headers={"User-Agent": "RabotaCopilot/0.1", "Accept": "text/html"})
        self.last_request = time.monotonic()
        try:
            with self.opener(request, timeout=20) as response:
                content = response.read(8 * 1024 * 1024 + 1)
            if len(content) > 8 * 1024 * 1024:
                raise WebSearchError("Страница rabota.by слишком велика; сбор остановлен.")
            return parse_search(content.decode("utf-8"))
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise WebSearchError(f"rabota.by вернул HTTP {status}. Сбор остановлен; повторите позднее или откройте поиск вручную. Проверки доступа не обходятся.") from None
        except (URLError, TimeoutError, OSError):
            raise WebSearchError("Не удалось подключиться к публичному поиску rabota.by. Проверьте сеть и повторите позднее.") from None
        except UnicodeError:
            raise WebSearchError("Не удалось декодировать страницу rabota.by; сбор остановлен.") from None


class WebCollector:
    def __init__(self, client, storage, config, *, max_pages=3, should_stop=None):
        self.client, self.storage, self.config = client, storage, config
        self.max_pages = max_pages
        self.should_stop = should_stop or (lambda: False)
        self.key = search_key(config)

    def collect(self, *, days=1, now=None, full=False, on_match=None):
        # HTML search has no reliable historical cursor: rescan the bounded period.
        now = now or datetime.now(timezone.utc)
        stats = Stats()
        seen = set()
        for page in range(self.max_pages):
            if self.should_stop():
                raise WebSearchError("Сбор остановлен. Сохранённые вакансии остаются в базе.")
            data = self.client.search(self.config, days=days, page=page)
            stats.requests += 1
            for item in data["items"]:
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                rejection = self.config.rejection(item)
                stats.new += self.storage.save(item, self.key, rejection, now.isoformat(), on_match=on_match)
                stats.received += 1
                stats.rejected += rejection is not None
            stats.partial = data["has_next"]
            if not stats.partial:
                break
        self.storage.advance("web:" + self.key, now.isoformat())
        return stats
