import json
import tempfile
import unittest
from pathlib import Path
from sleep_stages import backfill_sleep_summaries


class SleepSummaryFallbackTests(unittest.TestCase):
    def test_cross_midnight_duplicate_pushes_and_official_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            rows=[{'value':'深度','startDate':'2025-01-14 23:30:00 +0800','endDate':'2025-01-15 00:30:00 +0800'},
                  {'value':'核心','startDate':'2025-01-15 00:30:00 +0800','endDate':'2025-01-15 01:30:00 +0800'}]
            payload={'data':{'metrics':[{'name':'sleep_analysis','data':rows}]}}
            for name in ('a.json','b.json'):(root/name).write_text(json.dumps(payload))
            d=backfill_sleep_summaries(root,{})
            s=d['2025-01-15']['sleep_analysis']
            self.assertEqual(s['totalSleep'],2)
            self.assertEqual(s['deep'],1)
            self.assertEqual(s['origin'],'segments')
            self.assertEqual(backfill_sleep_summaries(root,d),d)
            official={'totalSleep':2.1,'sleepStart':'2025-01-14T23:25:00'}
            d['2025-01-15']['sleep_analysis']=official
            self.assertEqual(backfill_sleep_summaries(root,d)['2025-01-15']['sleep_analysis'],official)

    def test_conflicts_do_not_create_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            rows=[{'value':k,'startDate':'2025-01-15T01:00:00+08:00','endDate':'2025-01-15T02:00:00+08:00'} for k in ('Deep','REM')]
            (root/'a.json').write_text(json.dumps({'data':{'metrics':[{'name':'sleep_analysis','data':rows}]}}))
            self.assertEqual(backfill_sleep_summaries(root,{}),{})
