import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from .hh_client import ApiError


def search_key(config):
    data = asdict(config)
    # Preserve keys of existing searches when the new optional filter is unset.
    if not data.get("work_formats"):
        data.pop("work_formats", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def floor_window(value):
    value = value.astimezone(timezone.utc)
    return value.replace(minute=value.minute // 5 * 5, second=0, microsecond=0)


@dataclass
class Stats:
    received: int = 0
    new: int = 0
    rejected: int = 0
    requests: int = 0
    partial: bool = False


class Collector:
    def __init__(self, client, storage, config, *, max_requests=500, should_stop=None):
        self.client, self.storage, self.config = client, storage, config
        self.key = search_key(config)
        self.max_requests = max_requests
        self.should_stop = should_stop or (lambda: False)

    def collect(self, *, days=1, now=None, full=False):
        now = now or datetime.now(timezone.utc)
        end = floor_window(now)
        cursor = None if full else self.storage.cursor(self.key)
        start = datetime.fromisoformat(cursor) - timedelta(minutes=5) if cursor else end - timedelta(days=days)
        stats = Stats()
        seen = set()

        def fetch(left, right, page=0):
            if self.should_stop():
                raise ApiError("Сбор остановлен; курсор не продвинут")
            if stats.requests >= self.max_requests:
                raise ApiError("Достигнут лимит запросов за запуск; курсор не продвинут. Сузьте поиск.")
            stats.requests += 1
            return self.client.search({**self.config.params(), "per_page": 100, "page": page,
                                       "date_from": left.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                       "date_to": right.strftime("%Y-%m-%dT%H:%M:%S%z")})

        def window(left, right):
            data = fetch(left, right)
            if data["found"] > 2000 or data["pages"] > 20:
                if right - left <= timedelta(minutes=5):
                    raise ApiError("Более 2000 вакансий за 5 минут: сузьте фильтры; курсор не продвинут")
                middle = floor_window(left + (right - left) / 2)
                window(left, middle)
                window(middle, right)
                return
            for page in range(max(1, data["pages"])):
                batch = data if page == 0 else fetch(left, right, page)
                if batch["found"] > 2000 or batch["pages"] > 20:
                    raise ApiError("Выдача изменилась во время сбора; повторите запуск")
                for item in batch["items"]:
                    vid = str(item["id"])
                    if vid in seen:
                        continue
                    seen.add(vid)
                    rejection = self.config.rejection(item)
                    stats.new += self.storage.save(item, self.key, rejection, now.isoformat())
                    stats.received += 1
                    stats.rejected += rejection is not None

        if start < end:
            window(start, end)
            self.storage.advance(self.key, end.isoformat())
        return stats
