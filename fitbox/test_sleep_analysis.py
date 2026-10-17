import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
try:
    from . import sleep_analysis as sa
except ImportError:
    import sleep_analysis as sa


class SleepAnalysisTests(unittest.TestCase):
    def run_analysis(self, ons=(660,), offs=(1080,), events=2, sleep=None):
        row = {'Duration': [420], 'MaskEvents': [events], 'MaskOn': list(ons) + [-1] * 8,
               'MaskOff': list(offs) + [-1] * 8, 'AHI': [2.3]}
        edf = {'start': datetime(2026, 10, 16, 12), 'duration': 86400, 'records': [row]}
        sleep = sleep or {'sleepStart': '2026-10-16 23:00:00 +0800',
                          'sleepEnd': '2026-10-17 07:00:00 +0800',
                          'totalSleep': 7, 'deep': 1, 'rem': 2, 'awake': 1}
        with patch.object(sa, 'read_edf', return_value=edf) as reader:
            result = sa.analyze({'2026-10-17': {'sleep_analysis': sleep}}, '2026-10-17', Path('.'))
            reader.assert_called_once()
            return result

    def test_midnight_and_tail_are_window_not_sleep(self):
        r = self.run_analysis(); n = r['latest']
        self.assertEqual(n['overlapping_pap_min'], 420)
        self.assertEqual(n['tail_uncovered_min'], 60)
        self.assertEqual(n['sleep_min'], 420)
        self.assertEqual(n['watch_window_min'], 480)
        self.assertAlmostEqual(n['deep_pct'], 100 / 7)
        self.assertNotIn('stages', n)
        self.assertEqual(r['averages']['14d']['sleep_min']['n'], 1)

    def test_overlapping_sessions_union(self):
        n = self.run_analysis((660, 900), (1020, 1080), 4)['latest']
        self.assertEqual(n['overlapping_pap_min'], 420)
        self.assertEqual(n['uncovered_window_min'], 60)

    def test_utc_to_local(self):
        n = self.run_analysis(sleep={'sleepStart': '2026-10-16T15:00:00+00:00',
                                    'sleepEnd': '2026-10-16T23:00:00+00:00'})['latest']
        self.assertEqual(n['watch_start'], '2026-10-16T23:00:00')
        self.assertEqual(n['tail_uncovered_min'], 60)

    def test_missing_is_unknown(self):
        with patch.object(sa, 'read_edf', side_effect=FileNotFoundError):
            n = sa.analyze({}, '2026-10-17', Path('.'))['latest']
        self.assertIsNone(n['window_coverage_pct'])
        self.assertIsNone(n['ahi'])
        self.assertEqual(n['pap_status'], 'unknown')

    def test_broken_edf_is_unknown(self):
        with patch.object(sa, 'read_edf', side_effect=ValueError('truncated EDF')):
            n = sa.analyze({}, '2026-10-17', Path('.'))['latest']
        self.assertEqual(n['pap_status'], 'unknown')
        self.assertIsNone(n['tail_uncovered_min'])

    def test_no_watch_still_keeps_pap(self):
        n = self.run_analysis(sleep={'totalSleep': 7})['latest']
        self.assertEqual(n['pap_status'], 'available')
        self.assertEqual(len(n['sessions']), 1)
        self.assertIsNone(n['window_coverage_pct'])

    def test_unclosed_session(self):
        n = self.run_analysis(offs=(-1,))['latest']
        self.assertEqual(n['pap_status'], 'uncertain')
        self.assertIsNone(n['overlapping_pap_min'])
        self.assertIsNone(n['sessions'][0]['end'])

    def test_session_count_mismatch(self):
        n = self.run_analysis(events=4)['latest']
        self.assertEqual(n['pap_status'], 'uncertain')
        self.assertIsNone(n['tail_uncovered_min'])


if __name__ == '__main__':
    unittest.main()
