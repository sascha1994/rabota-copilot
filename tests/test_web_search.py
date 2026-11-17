import html
import io
import json
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError

from app.config import SearchConfig
from app.sources import make_collector
from app.web_search import WebCollector, WebSearchClient, WebSearchError, parse_search


def document(vacancies=None, paging=None, total=1):
    vacancy = {"vacancyId": 123, "name": "Python разработчик", "company": {"id": 9, "visibleName": "Компания"},
               "area": {"@id": 1002, "name": "Минск"}, "compensation": {"from": 5000, "currencyCode": "BYR", "gross": False},
               "publicationTime": {"$": "2026-11-14T12:00:00+03:00"}}
    state = {"vacancySearchResult": {"vacancies": [vacancy] if vacancies is None else vacancies,
                                    "totalResults": total, "paging": paging}}
    return '<template id="HH-Lux-InitialState">' + html.escape(json.dumps(state, ensure_ascii=False)) + '</template>'


class WebTests(unittest.TestCase):
    def test_actual_one_page_shape_and_salary(self):
        result = parse_search(document())
        self.assertFalse(result['has_next'])
        item = result['items'][0]
        self.assertEqual(item['name'], 'Python разработчик')
        self.assertEqual(item['salary']['from'], 5000)
        self.assertEqual(item['salary']['currency'], 'BYR')
        self.assertEqual(item['alternate_url'], 'https://rabota.by/vacancy/123')

    def test_empty_result_is_distinct_from_changed_markup(self):
        self.assertEqual(parse_search(document(vacancies=[], total=0))['items'], [])
        for text in ('<html>captcha</html>', document(vacancies=[], total=1)):
            with self.assertRaises(WebSearchError):
                parse_search(text)

    def test_public_request_filters_and_no_credentials(self):
        requests = []
        def opener(request, **kwargs):
            requests.append(request)
            return io.BytesIO(document().encode())
        client = WebSearchClient(opener=opener, sleep=Mock())
        client.search(SearchConfig(salary=1000,work_formats=('REMOTE','HYBRID')), days=7)
        for value in ('search_period=7', 'order_by=publication_time', 'currency_code=BYR', 'salary=1000', 'work_format=REMOTE', 'work_format=HYBRID'):
            self.assertIn(value, requests[0].full_url)
        self.assertIsNone(requests[0].get_header('Authorization'))
        self.assertIsNone(requests[0].get_header('Cookie'))

    def test_denial_has_no_retry_or_raw_body(self):
        opener = Mock(side_effect=HTTPError('https://rabota.by',403,'secret',{},None))
        with self.assertRaises(WebSearchError) as caught:
            WebSearchClient(opener=opener).search(SearchConfig())
        self.assertIn('403', caught.exception.public_message)
        self.assertNotIn('secret', caught.exception.public_message)
        opener.assert_called_once()

    def test_empty_region_is_not_sent_to_site(self):
        requests = []
        def opener(request, **kwargs):
            requests.append(request)
            return io.BytesIO(document().encode())
        WebSearchClient(opener=opener).search(SearchConfig(areas=()))
        self.assertNotIn('area=', requests[0].full_url)

    def test_page_limit_dedup_and_separate_cursor(self):
        client, storage = Mock(), Mock()
        client.search.return_value = parse_search(document(paging={'next': {'disabled': False}}))
        storage.save.return_value = True
        stats = WebCollector(client, storage, SearchConfig(), max_pages=2).collect()
        self.assertTrue(stats.partial)
        self.assertEqual(stats.requests, 2)
        self.assertEqual(stats.received, 1)
        storage.save.assert_called_once()
        self.assertTrue(storage.advance.call_args.args[0].startswith('web:'))

    def test_failure_does_not_advance_cursor(self):
        client, storage = Mock(), Mock()
        client.search.side_effect = WebSearchError('unavailable')
        with self.assertRaises(WebSearchError):
            WebCollector(client,storage,SearchConfig()).collect()
        storage.advance.assert_not_called()

    def test_default_source_needs_no_hh_registration(self):
        self.assertIsInstance(make_collector({}, Mock(), SearchConfig()), WebCollector)
        for value in ('0', '100', 'abc'):
            with self.assertRaises(ValueError):
                make_collector({'WEB_MAX_PAGES':value},Mock(),SearchConfig())
