import io
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fitbox import cpap_legacy as legacy


class Response(io.BytesIO):
    pass


class Opener:
    def __init__(self, body=None, code=None):
        self.body, self.code = body, code
    def open(self, request, timeout):
        if self.code:
            raise urllib.error.HTTPError(request.full_url, self.code, 'test', {}, io.BytesIO())
        return Response(self.body)


class LegacyTests(unittest.TestCase):
    def test_midnight_filename_stays_in_treatment_day(self):
        sessions = [{'day':'20261015', 'on':datetime(2026,10,16,0,0), 'off':None}]
        paths = list(legacy.candidate_paths(sessions, before=1, after=0))
        self.assertIn('DATALOG/20261015/20261015_235959_PLD.edf', paths)
        self.assertIn('DATALOG/20261015/20261016_000000_PLD.edf', paths)
        self.assertEqual(len(paths), len(set(paths)))

    def test_protocol_uses_long_name_and_no_ftype(self):
        url = legacy.legacy_url('http://card', 'DATALOG/20261015/20261015_233600_PLD.edf')
        self.assertIn('fname=20261015_233600_PLD.edf', url)
        self.assertIn('fdir=..%5CDATALOG%5C20261015', url)
        self.assertNotIn('ftype', url)
        self.assertIn('fname=STR.EDF', legacy.legacy_url('http://card','STR.edf'))
        with self.assertRaises(ValueError):
            legacy.legacy_url('http://card', '../outside')

    def test_only_404_means_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'test'
            self.assertIsNone(legacy.download('http://card', 'a', path, time.monotonic()+5, Opener(code=404)))
            for status in (401, 500, 503):
                with self.assertRaises(urllib.error.HTTPError):
                    legacy.download('http://card','a',path,time.monotonic()+5,Opener(code=status))
            self.assertFalse(path.exists())

    def test_transient_error_retries_but_404_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/'file'
            with patch.object(legacy,'_download_once',side_effect=[TimeoutError('radio'),3]) as fetch:
                self.assertEqual(legacy.download('http://card','a',target,time.monotonic()+60),3)
                self.assertEqual(fetch.call_count,2)
            with patch.object(legacy,'_download_once',return_value=None) as fetch:
                self.assertIsNone(legacy.download('http://card','a',target,time.monotonic()+60))
                self.assertEqual(fetch.call_count,1)

    def test_str_404_falls_back_to_root_and_auth_errors_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/'STR.edf.part'
            opener = Opener()
            with patch.object(opener, 'open', side_effect=[
                    urllib.error.HTTPError('http://card/download',404,'test',{},io.BytesIO()),
                    Response(b'summary bytes')]) as fetch:
                self.assertEqual(legacy.download('http://card/','STR.edf',target,time.monotonic()+5,opener),13)
                self.assertEqual(target.read_bytes(),b'summary bytes')
                self.assertEqual([call.args[0].full_url for call in fetch.call_args_list],[
                    'http://card/download?fname=STR.EDF&fdir=..', 'http://card/STR.EDF'])
            for relative, status in [('STR.edf',401),('DATALOG/20261016/test.edf',404)]:
                with patch.object(opener,'open',side_effect=urllib.error.HTTPError('http://card',status,'test',{},io.BytesIO())) as fetch:
                    if status == 401:
                        with self.assertRaises(urllib.error.HTTPError):
                            legacy.download('http://card',relative,target,time.monotonic()+5,opener)
                    else:
                        self.assertIsNone(legacy.download('http://card',relative,target,time.monotonic()+5,opener))
                    self.assertEqual(fetch.call_count,1 if status == 401 else 2)

    def test_datalog_fallback_quotes_path_and_missing_redirect(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/'file.part'
            opener = Opener()
            relative = 'DATALOG/20261016/space #?.edf'
            for body, expected in [(b'file bytes',10),(legacy.WELCOME_REDIRECT+b'\r\n',None)]:
                with patch.object(opener,'open',side_effect=[
                        urllib.error.HTTPError('http://card/download',404,'test',{},io.BytesIO()),
                        Response(body)]) as fetch:
                    self.assertEqual(legacy.download('http://card',relative,target,time.monotonic()+5,opener),expected)
                    self.assertEqual(fetch.call_args_list[1].args[0].full_url,
                                     'http://card/DATALOG/20261016/space%20%23%3F.edf')
                if expected is None:
                    self.assertFalse(target.exists())

    def test_str_root_fallback_still_validates_edf_before_replacing(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            (dest/'STR.edf').write_bytes(b'original')
            opener = Opener()
            with patch.object(opener,'open',side_effect=[
                    urllib.error.HTTPError('http://card/download',404,'test',{},io.BytesIO()),
                    Response(b'<html>not EDF</html>')]):
                result = legacy.sync(dest,dest/'journal.jsonl',opener=opener,log=lambda _:None)
            self.assertTrue(result['errors'])
            self.assertEqual(result['downloaded'],0)
            self.assertEqual((dest/'STR.edf').read_bytes(),b'original')
            self.assertFalse((dest/'STR.edf.part').exists())
            self.assertFalse((dest/'journal.jsonl').exists())

    def test_expired_deadline_does_not_make_request(self):
        with patch.object(legacy.OPENER,'open') as fetch:
            with self.assertRaises(TimeoutError):
                legacy.download('http://card','a',Path('unused'),time.monotonic()-1)
            fetch.assert_not_called()

    def test_bad_summary_keeps_original_and_no_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            (dest/'STR.edf').write_bytes(b'original')
            result = legacy.sync(dest,dest/'cpap.jsonl',opener=Opener(b'<html>wrong card</html>'))
            self.assertTrue(result['errors'])
            self.assertFalse(result['mirror_complete'])
            self.assertFalse(result['window_complete'])
            self.assertEqual((dest/'STR.edf').read_bytes(),b'original')
            self.assertFalse((dest/'STR.edf.part').exists())
            self.assertFalse((dest/'cpap.success.json').exists())

    def test_cache_resumes_and_new_summary_invalidates_missing(self):
        class Card:
            summary = b'summary one'
            calls = []
            def open(self, request, timeout):
                from urllib.parse import parse_qs, urlparse
                url = urlparse(request.full_url)
                name = parse_qs(url.query).get('fname',[Path(url.path).name])[0]
                self.calls.append(name)
                if name == 'STR.EDF': return Response(self.summary)
                if name.endswith('_PLD.edf'): return Response(b'valid mocked edf')
                if name.endswith('_PLD.crc'): return Response(b'12345678')
                raise urllib.error.HTTPError(request.full_url,404,'test',{},io.BytesIO())
        card = Card()
        sessions = [{'day':'20200101','on':datetime(2020,1,1,23,0),'off':datetime(2020,1,2,1,0)}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(legacy,'read_edf'), patch.object(legacy,'sessions_from_str',return_value=sessions), patch.object(legacy,'assess_treatment',return_value={'treatment_complete':False,'treatment_dates':[]}):
            dest = Path(tmp)
            kwargs = dict(since='20200101', before=0,after=0,include_off=False,opener=card,log=lambda _:None)
            first = legacy.sync(dest,dest/'journal.jsonl',**kwargs)
            self.assertEqual(first['downloaded'],3)
            self.assertEqual(first['missing'],4)
            self.assertTrue(first['window_complete'])
            self.assertFalse(first['mirror_complete'])
            card.calls = []
            second = legacy.sync(dest,dest/'journal.jsonl',**kwargs)
            self.assertEqual(card.calls,['STR.EDF'])
            self.assertEqual(second['cached'],6)
            # 旧协议的 404 缓存不能阻止探测新增的直接路径入口。
            import json
            cache_path = dest/'.legacy-probes.json'
            cache = json.loads(cache_path.read_text())
            cache['generation'].pop()
            cache_path.write_text(json.dumps(cache))
            card.calls = []
            legacy.sync(dest,dest/'journal.jsonl',**kwargs)
            self.assertEqual(len(card.calls),11)
            card.summary = b'summary two'
            card.calls = []
            third = legacy.sync(dest,dest/'journal.jsonl',**kwargs)
            self.assertEqual(third['missing'],4)
            self.assertEqual(len(card.calls),11)
            self.assertFalse((dest/'journal.success.json').exists())

    def test_recent_closed_day_reuses_missing_but_open_session_does_not(self):
        from datetime import timedelta
        now = datetime.now()
        day = now.strftime('%Y%m%d')
        sessions = [{'day':day,'on':now-timedelta(hours=1),'off':now-timedelta(minutes=10)}]
        calls = []
        def card(base, relative, target, deadline, opener):
            calls.append(relative)
            if relative == 'STR.edf':
                target.write_bytes(b'same summary')
                return 12
            return None
        with tempfile.TemporaryDirectory() as tmp, patch.object(legacy,'download',side_effect=card), patch.object(legacy,'read_edf'), patch.object(legacy,'sessions_from_str',return_value=sessions), patch.object(legacy,'assess_treatment',return_value={'treatment_complete':False,'treatment_dates':[]}):
            kwargs = dict(since=day,before=0,after=0,include_off=False,log=lambda _:None)
            legacy.sync(tmp,None,**kwargs)
            self.assertEqual(len(calls),6)
            calls.clear()
            legacy.sync(tmp,None,**kwargs)
            self.assertEqual(calls,['STR.edf'])
            sessions[0]['off'] = None
            calls.clear()
            legacy.sync(tmp,None,**kwargs)
            self.assertEqual(len(calls),6)

    def test_reconnect_can_recover_download(self):
        calls = []
        def card(base, relative, target, deadline, opener):
            calls.append(relative)
            if len(calls) == 1:
                raise urllib.error.URLError('temporary no route')
            target.write_bytes(b'summary')
            return 7
        with tempfile.TemporaryDirectory() as tmp, patch.object(legacy,'download',side_effect=card), patch.object(legacy,'read_edf'), patch.object(legacy,'sessions_from_str',return_value=[]):
            result = legacy.sync(tmp,None,reconnect=lambda remaining:True,log=lambda _:None)
            self.assertEqual(calls,['STR.edf','STR.edf'])
            self.assertEqual(result['reconnections'],1)
            self.assertEqual(result['downloaded'],1)

    def test_reconnect_retries_same_file_and_is_globally_bounded(self):
        calls, reconnects = [], []
        def broken(base, relative, target, deadline, opener):
            calls.append(relative)
            raise urllib.error.URLError('No route to host')
        with tempfile.TemporaryDirectory() as tmp, patch.object(legacy,'download',side_effect=broken):
            result = legacy.sync(tmp,None,reconnect=lambda remaining: reconnects.append(remaining),log=lambda _:None)
            self.assertEqual(calls,['STR.edf']*3)
            self.assertEqual(len(reconnects),2)
            self.assertEqual(result['reconnections'],2)
            self.assertTrue(result['errors'])
            self.assertFalse(result['treatment_complete'])
        with tempfile.TemporaryDirectory() as tmp, patch.object(legacy,'download',return_value=None):
            reconnects.clear()
            result = legacy.sync(tmp,None,reconnect=lambda remaining: reconnects.append(remaining),log=lambda _:None)
            self.assertEqual(reconnects,[])

    def test_reconnect_does_not_retry_http_or_disk_errors(self):
        for error in (urllib.error.HTTPError('http://card',500,'broken',{},io.BytesIO()), OSError(28,'disk full')):
            with tempfile.TemporaryDirectory() as tmp, patch.object(legacy,'download',side_effect=error), patch('builtins.print'):
                reconnects = []
                result = legacy.sync(tmp,None,reconnect=lambda remaining: reconnects.append(remaining))
                self.assertTrue(result['errors'])
                self.assertEqual(reconnects,[])

    def test_interrupted_window_never_claims_treatment_complete(self):
        sessions = [{'day':'20200101','on':datetime(2020,1,1,23,0),'off':None}]
        def partial(base, relative, target, deadline, opener):
            if relative == 'STR.edf':
                target.write_bytes(b'mocked summary')
                return 14
            raise TimeoutError('window exhausted')
        with tempfile.TemporaryDirectory() as tmp, patch.object(legacy,'download',side_effect=partial), patch.object(legacy,'read_edf'), patch.object(legacy,'sessions_from_str',return_value=sessions):
            result = legacy.sync(Path(tmp),None,since='20200101',log=lambda _:None)
            self.assertTrue(result['errors'])
            self.assertFalse(result['window_complete'])
            self.assertFalse(result['treatment_complete'])
            self.assertFalse(result['mirror_complete'])
            self.assertTrue((Path(tmp)/'.legacy-probes.json').exists())

    def test_treatment_coverage_types_sessions_and_quantization(self):
        from datetime import timedelta
        start = datetime(2020,1,1,23,0)
        sessions = [{'day':'20200101','on':start,'off':start+timedelta(hours=1),
                     'mask_events':2,'slots_saturated':False,'duration_minutes':60,
                     'indices':dict.fromkeys(('AHI','AI','CAI','OAI','UAI','HI'),0),
                     'summary_start':datetime(2020,1,1,12,0)}]
        files = [Path('20200101/20200101_230000_'+k+'.edf') for k in legacy.KINDS]
        def parse(path):
            kind = Path(path).stem.rsplit('_',1)[-1]
            return {'start':start+timedelta(seconds=30 if kind in ('BRP','PLD','SAD') else -5),
                    'duration':60 if kind in ('BRP','PLD','SAD') else 0,
                    'records':[{}]*59,
                    'annotations':[{'onset':0,'duration':0,'label':'Recording starts'}] if kind in ('CSL','EVE') else []}
        with patch.object(legacy,'read_edf',side_effect=parse), patch.object(Path,'read_bytes',return_value=b'12345678'):
            report = legacy.assess_treatment(sessions,files)
            self.assertTrue(report['treatment_complete'])
            self.assertFalse(report['mirror_complete'])
            self.assertFalse(legacy.assess_treatment(sessions,files,verified_crc=set())['treatment_complete'])
            self.assertFalse(legacy.assess_treatment(sessions,files[1:])['treatment_complete'])
            more = [dict(sessions[0],mask_events=4),dict(sessions[0],on=start+timedelta(hours=2),off=start+timedelta(hours=3),mask_events=4)]
            self.assertFalse(legacy.assess_treatment(more,files)['treatment_complete'])
            self.assertFalse(legacy.assess_treatment([dict(sessions[0],slots_saturated=True)],files)['treatment_complete'])
            self.assertFalse(legacy.assess_treatment([dict(sessions[0],off=None)],files)['treatment_complete'])
            self.assertFalse(legacy.assess_treatment(sessions,files,tolerance=10)['treatment_complete'])

    def test_shared_daily_events_verify_multiple_sessions_and_reject_missing(self):
        from datetime import timedelta
        start = datetime(2020,1,1,23,0)
        metadata = {'day':'20200101','mask_events':4,'slots_saturated':False,'duration_minutes':120,
                    'summary_start':datetime(2020,1,1,12),
                    'indices':{'AHI':1.0,'AI':1.0,'CAI':0,'OAI':1.0,'UAI':0,'HI':0}}
        sessions = [dict(metadata,on=start,off=start+timedelta(hours=1)),
                    dict(metadata,on=start+timedelta(hours=2),off=start+timedelta(hours=3))]
        files = [Path('20200101/one_'+kind+'.edf') for kind in legacy.KINDS]
        files += [Path('20200101/two_'+kind+'.edf') for kind in ('BRP','PLD','SAD')]
        events = [{'onset':0,'duration':0,'label':'Recording starts'},
                  {'onset':1000,'duration':20,'label':'Obstructive Apnea'},
                  {'onset':8000,'duration':20,'label':'Obstructive Apnea'}]
        shift = timedelta(0)
        def parse(path):
            path=Path(path)
            kind=path.stem.rsplit('_',1)[-1]
            is_event=kind in ('CSL','EVE')
            return {'start':start+ (shift if is_event else timedelta(seconds=10,hours=2 if path.name.startswith('two') else 0)),
                    'duration':0 if is_event else 60,'records':[{}]*60,
                    'annotations':list(events) if kind=='EVE' else events[:1] if kind=='CSL' else []}
        with patch.object(legacy,'read_edf',side_effect=parse), patch.object(Path,'read_bytes',return_value=b'12345678'):
            self.assertTrue(legacy.assess_treatment(sessions,files)['treatment_complete'])
            last=events.pop()
            report=legacy.assess_treatment(sessions,files)
            self.assertFalse(report['treatment_complete'])
            self.assertEqual(report['days']['20200101']['event_validation']['observed']['AHI'],1)
            events.append(last)
            shift=timedelta(days=1)
            report=legacy.assess_treatment(sessions,files)
            self.assertFalse(report['treatment_complete'])
            self.assertTrue(any('超出' in reason for reason in report['days']['20200101']['reasons']))
            shift=timedelta(0)
            self.assertFalse(legacy.assess_treatment(sessions,files+[Path('20200101/one_EVE.edf')])['treatment_complete'])

    def test_event_count_requires_unique_integer(self):
        session={'day':'20200101','on':datetime(2020,1,1,23),'off':datetime(2020,1,2,1),
                 'duration_minutes':1200,'indices':dict.fromkeys(('AHI','AI','CAI','OAI','UAI','HI'),0),
                 'summary_start':datetime(2020,1,1,12)}
        report=legacy.assess_daily_events([session],[])
        self.assertFalse(report['complete'])
        self.assertTrue(any('唯一' in reason for reason in report['reasons']))

    def test_edf_rejects_truncated_and_unfinished(self):
        # 最小有效 EDF，单信号，单个样本。
        h = bytearray(b' '*512)
        for a,b,text in ((0,8,'0'),(168,176,'15.10.26'),(176,184,'12.00.00'),
                         (184,192,'512'),(236,244,'1'),(244,252,'1'),(252,256,'1'),
                         (256,272,'MaskOn'),(360,368,'-1'),(368,376,'1440'),
                         (376,384,'-1'),(384,392,'1440'),(472,480,'1')):
            h[a:b] = text.encode().ljust(b-a)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'valid.edf'
            p.write_bytes(h+b'\0\0')
            self.assertEqual(legacy.read_edf(p)['records'][0]['MaskOn'],[0.0])
            h[244:252] = b'0       '
            p.write_bytes(h+b'\0\0')
            self.assertEqual(legacy.read_edf(p)['duration'],0)
            p.write_bytes(h+b'\0')
            with self.assertRaises(ValueError): legacy.read_edf(p)
            h[236:244] = b'-1      '
            p.write_bytes(h+b'\0\0')
            with self.assertRaises(ValueError): legacy.read_edf(p)
            h[236:244] = b'1       '
            h[256:272] = b'EDF Annotations '
            h[472:480] = b'32      '
            p.write_bytes(h+b'+0\x14\x14\0+60\x1514\x14Central Apnea\x14\0'.ljust(64,b'\0'))
            self.assertEqual(legacy.read_edf(p)['annotations'],[{'onset':60.0,'duration':14.0,'label':'Central Apnea'}])


if __name__ == '__main__':
    unittest.main()
