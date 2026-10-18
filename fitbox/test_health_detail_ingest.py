"""Raw Auto Export samples must not overwrite the separate daily summary feed."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import health_daily as h


class DetailIngestTests(unittest.TestCase):
    def test_raw_sleep_and_numeric_samples_preserve_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / 'daily.json'
            original = {'2026-10-17': {'sleep_analysis': {'totalSleep': 6.54},
                                      'heart_rate_variability': 42, 'step_count': 3000}}
            store.write_text(json.dumps(original))
            raw = root / 'raw.json'
            payload = {'data': {'metrics': [
                {'name': 'sleep_analysis', 'data': [
                    {'startDate': '2026-10-17 01:42:21 +0800',
                     'endDate': '2026-10-17 01:44:21 +0800', 'value': '深度', 'qty': .0333}]},
                {'name': 'heart_rate_variability', 'data': [
                    {'date': '2026-10-17 08:00:00 +0800', 'qty': 12}]}]}}
            raw.write_text(json.dumps(payload))
            with patch.object(h, 'STORE', store):
                self.assertEqual(h.merge(raw), original)
                self.assertEqual(h.merge(raw), original)
            self.assertEqual(json.loads(raw.read_text()), payload)

    def test_raw_numeric_only_and_daily_summary_are_distinguished(self):
        self.assertTrue(h.is_detailed_metrics([{'name': 'step_count', 'data': [
            {'date': '2026-10-17 00:00:00 +0800', 'qty': 1},
            {'date': '2026-10-17 00:01:00 +0800', 'qty': 2}]}]))
        self.assertFalse(h.is_detailed_metrics([{'name': 'sleep_analysis', 'data': [
            {'date': '2026-10-17 00:00:00 +0800', 'totalSleep': 6.54},
            {'date': '2026-10-16 00:00:00 +0800', 'totalSleep': 6.5}]}]))


class RawOnlyEndToEndTests(unittest.TestCase):
    def test_raw_only_to_report_with_missing_and_zero(self):
        from datetime import date
        import health_card
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); raw = root/'raw'; raw.mkdir()
            path = raw/'a.json'
            path.write_text(json.dumps({'data': {'metrics': [
                {'name':'sleep_analysis', 'data':[{'value':'Core', 'start':'2025-01-15T01:00:00+08:00', 'end':'2025-01-15T08:00:00+08:00'}]},
                {'name':'step_count', 'units':'count', 'data':[{'date':'2025-01-14T12:00:00+08:00', 'qty':0}]},
                {'name':'apple_exercise_time', 'units':'min', 'data':[{'date':'2025-01-14T12:00:00+08:00', 'qty':0}]}]}}))
            with patch.object(h, 'STORE', root/'daily.json'), patch.object(h,'LOG',root/'log.jsonl'), patch.object(h,'todays_weight',return_value=None), patch.object(h,'cpap_night',return_value=None):
                d = h.merge(path)
                self.assertEqual(d, h.merge(path))
                text, verdict, sleep, extra = h.build(d, date(2025,1,15))
                self.assertIsNone(extra['total'])
                self.assertIn('暂不判断', verdict)
                self.assertIn('未同步完整', text)
                self.assertTrue(extra['activity_partial'])
                page = health_card.html(d, date(2025,1,15),verdict,False,None,extra)
                self.assertIn('0 步',page)
                self.assertNotIn('总消耗 0',page)



class ThreeFeedTests(unittest.TestCase):
    def test_summary_sleep_workout_arrival_orders_and_replay(self):
        from itertools import permutations
        payloads = [
            {'metrics': [
                {'name':'step_count', 'units':'count', 'data':[{'date':'2025-01-14 00:00:00 +0800','qty':8621}]},
                {'name':'active_energy', 'units':'kJ', 'data':[{'date':'2025-01-14 00:00:00 +0800','qty':713.74}]}]},
            {'metrics': [
                {'name':'sleep_analysis','data':[{'value':'Core','start':'2025-01-15T01:00:00+08:00','end':'2025-01-15T08:00:00+08:00'}]},
                {'name':'step_count','units':'count','data':[{'date':'2025-01-15T07:00:00+08:00','qty':16}]},
                {'name':'heart_rate_variability','units':'ms','data':[{'start':'2025-01-15T06:00:00+08:00','end':'2025-01-15T06:01:00+08:00','qty':41}]}]},
            {'workouts':[{'id':'demo-basketball','name':'Basketball','start':'2025-01-14T18:00:00+08:00','end':'2025-01-14T19:30:00+08:00','duration':5400,'activeEnergyBurned':{'qty':400,'units':'kcal'}}]}
        ]
        for order in permutations(range(3)):
            with self.subTest(order=order), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); raw=root/'raw'; raw.mkdir()
                with patch.object(h,'STORE',root/'daily.json'):
                    for i in order:
                        p=raw/f'{i}.json';p.write_text(json.dumps({'data':payloads[i]}));d=h.merge(p)
                    before=json.loads(json.dumps(d))
                    for i in order: d=h.merge(raw/f'{i}.json')
                self.assertEqual(d,before)
                self.assertEqual(d['2025-01-14']['step_count'],8621)
                self.assertEqual(d['2025-01-14']['active_energy'],713.74)
                self.assertNotIn('step_count',d['2025-01-14'].get('_sample_metrics',{}))
                self.assertEqual(d['2025-01-15']['sleep_analysis']['totalSleep'],7)
                self.assertEqual(d['2025-01-15']['heart_rate_variability'],41)
                self.assertEqual(d['2025-01-15']['step_count'],16)
                self.assertEqual(len(d['2025-01-14']['workouts']),1)
                self.assertEqual(d['2025-01-14']['workouts']['demo-basketball']['min'],90)

if __name__ == '__main__':
    unittest.main()
