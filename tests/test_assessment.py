import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError

from app.assessment import AssessmentService,AssessmentError,LocalQuotaExceeded,Evaluation,load_description,professional_facts,openai_http_error
from app.candidate_profile import ProfileStore
from app.storage import Storage
from test_search import vacancy


def output(fact_id='skill'):
    return {'score':80,'resume':'graphic','summary':'Соответствует графическим задачам.',
        'matches':[{'reason':'Есть опыт','fact_ids':[fact_id]}],'gaps':[],'questions':['Уточните график.']}


class AssessmentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'db.sqlite3'
        store=Storage(self.path);store.save(vacancy(),'test',None,'2026-11-14');store.close()
        profile=ProfileStore(self.path)
        profile.import_draft({'facts':[{'id':'skill','category':'Навыки','value':'Графический дизайн',
            'sources':[{'document':'cv','page':1,'quote':'Графический дизайн'}]}]})
        profile.update('skill',1,'confirm',42);profile.close()
        self.calls=[]
        self.result=output()
        def opener(request,**kwargs):
            self.calls.append(request)
            return io.BytesIO(json.dumps({'status':'completed','usage':{'total_tokens':500},
                'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(self.result)}]}]}).encode())
        self.service=AssessmentService(self.path,{'OPENAI_API_KEY':'test-secret'},opener=opener,
                                       details=lambda vid:'Подробные требования графического дизайнера.')

    def tearDown(self):
        self.temp.cleanup()

    def test_success_uses_model_and_cache(self):
        evaluation,cached=self.service.evaluate('1')
        self.assertFalse(cached);self.assertEqual(evaluation.resume,'graphic')
        self.assertTrue(self.service.evaluate('1')[1]);self.assertEqual(len(self.calls),1)
        body=json.loads(self.calls[0].data)
        self.assertEqual(body['model'],'gpt-5.4-mini');self.assertFalse(body['store'])
        self.assertEqual(body['text']['format']['type'],'json_schema')
        self.assertNotIn('test-secret',self.calls[0].full_url)

    def test_application_generates_only_a_letter_and_caches_it(self):
        self.result = {'text':'Есть опыт графического дизайна.','fact_ids':['skill'],'questions':[]}
        result,cached = self.service.evaluate('1',application_direction='graphic')
        self.assertFalse(cached)
        self.assertEqual(result.fact_ids,['skill'])
        schema = json.loads(self.calls[0].data)['text']['format']['schema']
        self.assertNotIn('resume',schema['properties'])
        self.assertTrue(self.service.evaluate('1',application_direction='graphic')[1])
        self.assertEqual(len(self.calls),1)

    def test_letter_uses_at_most_two_confirmed_matches_from_same_description(self):
        self.result['matches'] = [
            {'reason': reason, 'fact_ids': ['skill']}
            for reason in ('Первая задача', 'Вторая задача', 'Третья задача')
        ]
        evaluation, _ = self.service.evaluate('1')
        options = dict(application_direction='graphic', assessment_matches=evaluation.matches,
                       assessed_description_id=self.service.description_id)
        self.result = {'text':'Есть опыт графического дизайна.','fact_ids':['skill'],'questions':[]}
        self.service.evaluate('1', **options)
        payload = json.loads(json.loads(self.calls[-1].data)['input'][1]['content'])
        self.assertEqual(payload['assessment_matches'], [m.model_dump() for m in evaluation.matches[:2]])
        self.assertTrue(self.service.evaluate('1', **options)[1])
        self.assertEqual(len(self.calls), 2)

        self.service.details = lambda vid: 'Другое подробное описание вакансии.'
        self.service.evaluate('1', **options)
        payload = json.loads(json.loads(self.calls[-1].data)['input'][1]['content'])
        self.assertNotIn('assessment_matches', payload)

    def test_application_package_rejects_invented_letter_fact(self):
        self.result = {'text':'Руковожу командой.','fact_ids':['invented'],'questions':[]}
        with self.assertRaises(AssessmentError):
            self.service.evaluate('1',application_direction='graphic')

    def test_profile_change_invalidates_cache(self):
        self.service.evaluate('1')
        p=ProfileStore(self.path);p.update('skill',2,'edit',42,'Другой навык');p.update('skill',3,'confirm',42);p.close()
        self.assertFalse(self.service.evaluate('1')[1]);self.assertEqual(len(self.calls),2)

    def test_unconfirmed_and_contacts_not_transmitted(self):
        facts=[{'id':'candidate_name','status':'CONFIRMED','value':'Name'},
               {'id':'excluded','status':'REJECTED','value':'skill'},
               {'id':'pending','status':'UNCONFIRMED','value':'skill'},
               {'id':'valid','status':'CONFIRMED','revision':1,'category':'Навыки',
                'value':'Figma contact@example.com https://example.com +375 (29) 123-45-67'}]
        result=professional_facts(facts)
        self.assertEqual([f['id'] for f in result],['valid'])
        self.assertNotIn('example.com',result[0]['value']);self.assertNotIn('+375',result[0]['value'])

    def test_unknown_fact_id_is_rejected(self):
        self.result=output('invented')
        with self.assertRaises(AssessmentError):self.service.evaluate('1')

    def test_missing_key_and_local_quota_prevent_request(self):
        self.service.env['OPENAI_API_KEY']=''
        with self.assertRaises(AssessmentError):self.service.evaluate('1')
        self.assertEqual(self.calls,[])
        self.service.env.update(OPENAI_API_KEY='test',AI_DAILY_CALL_LIMIT='1')
        self.service.evaluate('1')
        self.service.details=lambda vid:'Другое подробное описание вакансии.'
        with self.assertRaises(LocalQuotaExceeded) as caught:self.service.evaluate('1')
        self.assertTrue(caught.exception.retry_at.endswith('T00:00:00+00:00'))
        self.assertEqual(len(self.calls),1)

    def test_incomplete_response_never_caches(self):
        self.service.opener=lambda *a,**kw:io.BytesIO(b'{"status":"incomplete","output":[]}')
        with self.assertRaises(AssessmentError):self.service.evaluate('1')

    def test_region_denial_is_explicit_and_releases_token_reservation(self):
        body=json.dumps({'error':{'code':'unsupported_country_region_territory','message':'SECRET'}}).encode()
        self.service.opener=Mock(side_effect=HTTPError('https://api.openai.com/v1/responses',403,'denied',{},io.BytesIO(body)))
        with self.assertRaises(AssessmentError) as caught:self.service.evaluate('1')
        self.assertIn('unsupported_country_region_territory',str(caught.exception))
        self.assertNotIn('SECRET',str(caught.exception))
        self.service.opener.assert_called_once()
        store=Storage(self.path)
        try:
            tokens,status=store.db.execute('SELECT tokens,status FROM ai_call_usage').fetchone()
            self.assertEqual(tokens,0);self.assertEqual(status,'rejected')
        finally:store.close()

    def test_unknown_http_body_is_not_forwarded(self):
        for body in (b'<html>SECRET</html>',b'{"error":"SECRET"}',b'{"error":{"code":"SECRET"}}'):
            result=openai_http_error(403,body)
            self.assertNotIn('SECRET',result)
            self.assertNotIn('unsupported_country',result)


class DescriptionTests(unittest.TestCase):
    def test_full_visible_description_and_access_challenge(self):
        html='<div>Другой текст</div><div data-qa="vacancy-description"><p>Подробные требования вакансии графического дизайнера</p><ul><li>Figma</li></ul></div><div>Не описание</div>'
        text=load_description('123',opener=lambda *a,**k:io.BytesIO(html.encode()))
        self.assertIn('Figma',text);self.assertNotIn('Другой текст',text)
        with self.assertRaises(AssessmentError):
            load_description('123',opener=lambda *a,**k:io.BytesIO(b'<html>captcha</html>'))
