from .collector import Collector
from .hh_client import HHClient
from .web_search import WebCollector, WebSearchClient


def source_name(env):
    source = env.get("VACANCY_SOURCE", "web").strip().lower()
    if source not in ("web", "api"):
        raise ValueError("VACANCY_SOURCE должен быть web или api")
    return source


def make_collector(env, storage, config, *, should_stop=None):
    if source_name(env) == "api":
        return Collector(HHClient(env.get("HH_USER_AGENT", ""), access_token=env.get("HH_ACCESS_TOKEN", "")),
                         storage, config, should_stop=should_stop)
    try:
        pages = int(env.get("WEB_MAX_PAGES", "3"))
    except ValueError:
        raise ValueError("WEB_MAX_PAGES должен быть целым числом от 1 до 5") from None
    if not 1 <= pages <= 5:
        raise ValueError("WEB_MAX_PAGES должен быть от 1 до 5")
    return WebCollector(WebSearchClient(), storage, config, max_pages=pages, should_stop=should_stop)
