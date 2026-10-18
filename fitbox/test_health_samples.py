import json
import tempfile
import unittest
from pathlib import Path
from health_samples import backfill_sample_metrics


class HealthSamplesTests(unittest.TestCase):
    def test_mean_dedup_updates_and_official_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            rows=[{'start':f'2025-01-15T0{h}:00:00+08:00','end':f'2025-01-15T0{h}:01:00+08:00','qty':v,'source':'demo'} for h,v in [(1,20),(3,40)]]
            payload={'data':{'metrics':[{'name':'heart_rate_variability','units':'ms','data':rows}]}}
            for name in ('a.json','b.json'):(root/name).write_text(json.dumps(payload))
            d=backfill_sample_metrics(root,{})
            self.assertEqual(d['2025-01-15']['heart_rate_variability'],30)
            self.assertEqual(d['2025-01-15']['_sample_metrics']['heart_rate_variability']['count'],2)
            official={'2025-01-15':{'heart_rate_variability':35}}
            self.assertEqual(backfill_sample_metrics(root,official)['2025-01-15']['heart_rate_variability'],35)

    def test_units_and_missing_are_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'a.json'
            p.write_text(json.dumps({'data':{'metrics':[{'name':'heart_rate_variability','units':'s','data':[{'start':'2025-01-15T01:00:00','end':'2025-01-15T01:01:00','qty':.04}]}]}}))
            self.assertEqual(backfill_sample_metrics(p.parent,{}),{})

    def test_raw_only_cumulative_replay_correction_and_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def save(file, points):
                (root/file).write_text(json.dumps({'data': {'metrics': [
                    {'name': 'active_energy', 'units': 'kcal', 'data': [
                        {'date': '2025-01-15T'+t+':00+08:00', 'qty': v, 'source': 'watch'} for t,v in points]}]}}))
            save('a.json', [('01:00', 10), ('02:00', 20)])
            save('b.json', [('02:00', 25), ('03:00', 5)])
            d = backfill_sample_metrics(root, {})
            self.assertAlmostEqual(d['2025-01-15']['active_energy'], 40*4.184)
            self.assertEqual(d, backfill_sample_metrics(root, d))
            self.assertEqual(d['2025-01-15']['_sample_metrics']['active_energy']['count'], 3)
            self.assertEqual(d['2025-01-15']['_sample_metrics']['active_energy']['aggregation'], 'observed_sum')

    def test_conflicting_sources_do_not_double_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'a.json'
            p.write_text(json.dumps({'data': {'metrics': [{'name': 'step_count', 'units': 'count', 'data': [
                {'date':'2025-01-15T01:00:00+08:00', 'qty': 10, 'source': source} for source in ['phone', 'watch']]}]}}))
            d = backfill_sample_metrics(p.parent, {})
            self.assertNotIn('step_count', d['2025-01-15'])
