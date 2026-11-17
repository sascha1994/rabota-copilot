import unittest
from unittest.mock import Mock,patch
from urllib.error import HTTPError
from urllib.request import Request

import httpx

from app.assessment import AssessmentError,make_openai_opener


class ProxyTests(unittest.TestCase):
    def test_proxy_is_scoped_and_tls_verification_stays_enabled(self):
        for proxy in ('http://user:password@127.0.0.1:8080','https://127.0.0.1:8443',
                      'socks5://127.0.0.1:1080','socks5h://127.0.0.1:1080',''):
            response=Mock(status_code=200,content=b'{"ok":true}')
            with patch('app.assessment.httpx.Client') as factory:
                client=factory.return_value.__enter__.return_value
                client.request.return_value=response
                req=Request('https://api.openai.com/v1/responses',data=b'{}',method='POST')
                req.add_unredirected_header('Authorization','Bearer secret')
                with make_openai_opener(proxy)(req,timeout=20) as result:
                    self.assertIn(b'ok',result.read())
                factory.assert_called_once_with(proxy=proxy or None,verify=True,trust_env=False,follow_redirects=False,timeout=20)
                kwargs=client.request.call_args.kwargs
                self.assertEqual(kwargs['headers']['Authorization'],'Bearer secret')
                self.assertNotIn('Proxy-Authorization',kwargs['headers'])

    def test_invalid_proxy_has_no_credential_leak(self):
        for proxy in ('localhost:8080','ftp://user:secret@host:123','http://host:99999',
                      'http://host/path','socks5://host','http://user:secret@host?password=x'):
            with self.assertRaises(AssessmentError) as caught:make_openai_opener(proxy)
            self.assertNotIn('secret',str(caught.exception))

    def test_no_direct_fallback_after_proxy_failure(self):
        with patch('app.assessment.httpx.Client') as factory:
            factory.return_value.__enter__.return_value.request.side_effect=httpx.ProxyError('user:secret@proxy')
            with self.assertRaises(AssessmentError) as caught:
                make_openai_opener('http://localhost:8080')(Request('https://api.openai.com/v1/models'))
            factory.assert_called_once()
            self.assertNotIn('secret',str(caught.exception))

    def test_denial_body_preserved_for_safe_error_mapper(self):
        with patch('app.assessment.httpx.Client') as factory:
            factory.return_value.__enter__.return_value.request.return_value=Mock(
                status_code=403,reason_phrase='Forbidden',headers={},content=b'{"error":{"code":"unsupported_country_region_territory"}}')
            with self.assertRaises(HTTPError) as caught:
                make_openai_opener('http://localhost:8080')(Request('https://api.openai.com/v1/models'))
            self.assertIn(b'unsupported_country',caught.exception.read())
            caught.exception.close()

    def test_credentials_never_sent_to_other_destination(self):
        with patch('app.assessment.httpx.Client') as factory:
            with self.assertRaises(AssessmentError):
                make_openai_opener('')(Request('https://other.example/v1/responses'))
            factory.assert_not_called()
