import io
import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

from app.cli import main
from app.collector import Collector
from app.config import SearchConfig
from app.hh_client import ApiError, HHClient
from app.storage import Storage
from app.env import load_env


NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def vacancy(vid="1", title="Python developer"):
    return {"id": vid, "name": title, "employer": {"id": "10", "name": "Example"},
            "published_at": "2026-11-14T10:00:00+0300", "alternate_url": "https://rabota.by/vacancy/" + vid}


def page(items, pages=1, found=None):
    return {"items": items, "pages": pages, "found": len(items) if found is None else found}


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def search(self, params):
        self.calls.append(params)
        return self.handler(params)


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "search.db"
        self.storage = Storage(self.path)

    def tearDown(self):
        self.storage.close()
        self.temp.cleanup()

    def test_pagination_dedup_and_persistence(self):
        client = FakeClient(lambda p: page([vacancy(str(p["page"] + 1))], pages=2, found=2))
        collector = Collector(client, self.storage, SearchConfig())
        self.assertEqual(collector.collect(now=NOW).new, 2)
        self.assertEqual(collector.collect(now=NOW + timedelta(minutes=15)).new, 0)
        self.storage.close()
        self.storage = Storage(self.path)
        self.assertEqual(len(self.storage.results(collector.key)), 2)
        self.assertEqual(self.storage.cursor(collector.key), (NOW + timedelta(minutes=15)).isoformat())
        self.assertEqual(client.calls[2]["date_from"], "2026-11-14T11:55:00+0000")

    def test_failed_page_keeps_saved_items_but_not_cursor(self):
        def handler(params):
            if params["page"]:
                raise ApiError("temporary failure")
            return page([vacancy()], pages=2, found=2)
        collector = Collector(FakeClient(handler), self.storage, SearchConfig())
        with self.assertRaises(ApiError):
            collector.collect(now=NOW)
        self.assertIsNone(self.storage.cursor(collector.key))
        self.assertEqual(len(self.storage.results(collector.key)), 1)

    def test_overflow_splits_windows_and_deduplicates_boundaries(self):
        def handler(params):
            left = datetime.strptime(params["date_from"], "%Y-%m-%dT%H:%M:%S%z")
            right = datetime.strptime(params["date_to"], "%Y-%m-%dT%H:%M:%S%z")
            return page([], pages=20, found=2100) if right-left > timedelta(hours=12) else page([vacancy()])
        collector = Collector(FakeClient(handler), self.storage, SearchConfig())
        stats = collector.collect(now=NOW)
        self.assertEqual(stats.requests, 3)
        self.assertEqual(stats.received, 1)

    def test_unsplittable_overflow_and_request_limit_fail_visibly(self):
        for limit in (1, 500):
            collector = Collector(FakeClient(lambda p: page([], pages=20, found=3000)),
                                  self.storage, SearchConfig(), max_requests=limit)
            with self.assertRaises(ApiError):
                collector.collect(now=NOW)
            self.assertIsNone(self.storage.cursor(collector.key))

    def test_filters_are_scoped_to_search(self):
        client = FakeClient(lambda p: page([vacancy()]))
        excluded = Collector(client, self.storage, SearchConfig(excluded_title_words=("python",)))
        self.assertEqual(excluded.collect(now=NOW).rejected, 1)
        accepted = Collector(client, self.storage, SearchConfig())
        accepted.collect(now=NOW)
        self.assertEqual(self.storage.results(excluded.key), [])
        self.assertEqual(len(self.storage.results(accepted.key)), 1)

    def test_request_budget_does_not_advance_cursor(self):
        collector = Collector(FakeClient(lambda p: page([vacancy()], pages=2, found=2)),
                              self.storage, SearchConfig(), max_requests=1)
        with self.assertRaises(ApiError):
            collector.collect(now=NOW)
        self.assertIsNone(self.storage.cursor(collector.key))


class HttpTests(unittest.TestCase):
    def test_optional_token_is_only_in_header(self):
        requests = []
        def opener(request, **kwargs):
            requests.append(request)
            return io.BytesIO(json.dumps(page([])).encode())
        HHClient('Test/1.0 (test@example.com)', access_token='private-token', opener=opener).search({})
        self.assertEqual(requests[0].get_header('Authorization'), 'Bearer private-token')
        self.assertNotIn('private-token', requests[0].full_url)
        self.assertNotIn('Authorization', requests[0].headers)
        HHClient('Test/1.0 (test@example.com)', opener=opener).search({})
        self.assertIsNone(requests[1].get_header('Authorization'))

    def test_only_get_fixed_endpoint_and_retries_429(self):
        calls, delays = [], []
        def opener(request, **kwargs):
            calls.append(request)
            if len(calls) == 1:
                raise HTTPError(request.full_url, 429, "limit", {"Retry-After": "2"}, None)
            return io.BytesIO(json.dumps(page([])).encode())
        client = HHClient("Tests/1.0 (test@example.com)", opener=opener, sleep=delays.append)
        client.search({"area": ("16", "1002"), "text": "Python & SQL"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].get_method(), "GET")
        self.assertTrue(calls[0].full_url.startswith("https://api.hh.ru/vacancies?"))
        self.assertIn("area=16&area=1002", calls[0].full_url)
        self.assertGreaterEqual(delays[0], 2)

    def test_permanent_error_is_not_retried(self):
        calls = []
        def opener(request, **kwargs):
            calls.append(request)
            raise HTTPError(request.full_url, 403, "forbidden", {}, None)
        with self.assertRaisesRegex(ApiError, "403"):
            HHClient("Tests/1.0 (test@example.com)", opener=opener).search({})
        self.assertEqual(len(calls), 1)

    def test_bad_response_and_retry_exhaustion(self):
        client = HHClient("Tests/1.0 (test@example.com)", opener=lambda *a, **k: io.BytesIO(b'{}'))
        with self.assertRaises(ApiError):
            client.search({})
        delays = []
        def timeout(*args, **kwargs):
            raise TimeoutError()
        client = HHClient("Tests/1.0 (test@example.com)", opener=timeout, sleep=delays.append)
        with self.assertRaises(ApiError):
            client.search({})
        self.assertEqual(len(delays), 2)


class ConfigTests(unittest.TestCase):
    def test_env_loading_and_environment_priority(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('# comment\nHH_USER_AGENT="App/1.0 (me@example.com)"\nSEARCH_CONFIG=custom.json\n', encoding='utf-8')
            with patch.dict('os.environ', {'SEARCH_CONFIG': 'override.json'}, clear=True):
                result = load_env(path)
                self.assertEqual(result['HH_USER_AGENT'], 'App/1.0 (me@example.com)')
                self.assertEqual(result['SEARCH_CONFIG'], 'override.json')

    def test_env_does_not_expose_invalid_line_content(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('SECRET_WITHOUT_EQUALS', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'строке 1') as error:
                load_env(path)
            self.assertNotIn('SECRET', str(error.exception))

    def test_invalid_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for value in ({"unexpected": True}, {"areas": "16"}, {"salary": -1}, {"experience": "expert"}):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ValueError):
                    SearchConfig.load(str(path))

    def test_cli_local_listing_and_export(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            config = path / "search.json"
            config.write_text('{"text": "Python"}', encoding="utf-8")
            common = ["--config", str(config), "--db", str(path / "db.sqlite3")]
            self.assertEqual(main(common + ["list"]), 0)
            output = path / "out.json"
            self.assertEqual(main(common + ["export", "--output", str(output)]), 0)
            self.assertEqual(json.loads(output.read_text()), [])


if __name__ == "__main__":
    unittest.main()
