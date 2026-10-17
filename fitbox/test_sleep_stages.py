import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import sleep_stages as ss


class SleepStagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw = self.root / 'raw'
        self.raw.mkdir()
        self.start = datetime(2026, 10, 16, 23, 55)
        self.end = self.start + timedelta(minutes=10)
        self.sleep = {'sleepStart': self.start.isoformat(), 'sleepEnd': self.end.isoformat()}
        self.sessions = [{'start': self.start.isoformat(), 'end': self.end.isoformat()}]

    def write_rows(self, rows, name='one.json'):
        (self.raw / name).write_text(json.dumps({'data': {'metrics': [
            {'name': 'sleep_analysis', 'data': rows}]}}))

    def row(self, stage='Deep', start=None, end=None):
        return {'value': stage, 'startDate': (start or self.start).isoformat(),
                'endDate': (end or self.end).isoformat()}

    def pld(self, duplicate=False):
        directory = self.root / 'DATALOG' / '20261016'
        directory.mkdir(parents=True)
        (directory / 'test_PLD.edf').touch()
        if duplicate:
            (directory / 'duplicate_PLD.edf').touch()
        # Two unequal-value 5-minute blocks; statistics must use overlap duration.
        return ({'start': self.start, 'duration': 300,
                 'records': [{'RespRate.2s': [10], 'Leak.2s': [.1]},
                             {'RespRate.2s': [20], 'Leak.2s': [.2]}]},
                {'RespRate.2s': 'bpm', 'Leak.2s': 'L/s'})

    def test_midnight_timezone_and_repeated_sync_are_deduplicated(self):
        row = {'value': 'Deep', 'startDate': '2026-10-16T15:55:00Z',
               'endDate': '2026-10-16T16:05:00Z'}
        self.write_rows([row, row])
        self.write_rows([row], 'repeat.json')
        segments, issues = ss.load_segments(self.raw, self.sleep)
        self.assertFalse(issues)
        self.assertEqual(segments, [(self.start, self.end, 'deep')])

    def test_conflicting_stages_are_not_arbitrarily_resolved(self):
        self.write_rows([self.row(), self.row('REM', self.start + timedelta(minutes=3))])
        result = ss.analyze_stages(self.raw, self.root, self.sleep, self.sessions)
        self.assertFalse(result['ready'])
        self.assertEqual(result['rows'], [])
        self.assertTrue(result['issues'])

    def test_stage_boundaries_and_duplicate_pld_keep_real_duration(self):
        boundary = self.start + timedelta(minutes=3)
        self.write_rows([self.row(end=boundary), self.row('REM', start=boundary)])
        with patch.object(ss, '_load', return_value=self.pld(duplicate=True)):
            result = ss.analyze_stages(self.raw, self.root, self.sleep, self.sessions)
        rows = {r['stage']: r for r in result['rows']}
        self.assertTrue(result['ready'])
        self.assertEqual(rows['deep']['matched_minutes'], 3)
        self.assertEqual(rows['rem']['matched_minutes'], 7)
        self.assertEqual(rows['deep']['rr'], 10)
        self.assertEqual(rows['rem']['rr'], 20)
        self.assertEqual(rows['rem']['leak'], 12)
        self.assertEqual(rows['rem']['coverage'], 1)

    def test_partial_session_is_not_presented_as_full_night(self):
        self.write_rows([self.row()])
        self.sessions[0]['start'] = (self.start + timedelta(minutes=4)).isoformat()
        with patch.object(ss, '_load', return_value=self.pld()):
            result = ss.analyze_stages(self.raw, self.root, self.sleep, self.sessions)
        row = result['rows'][0]
        self.assertFalse(result['ready'])
        self.assertEqual(row['minutes'], 10)
        self.assertEqual(row['matched_minutes'], 6)
        self.assertEqual(row['coverage'], .6)
        self.assertEqual(row['rr'], 20)
        self.assertIn('<td>—</td>', ss.render_stages(result))

    def test_missing_pld_does_not_become_zero_breathing_or_leak(self):
        self.write_rows([self.row()])
        result = ss.analyze_stages(self.raw, self.root, self.sleep, self.sessions)
        self.assertFalse(result['ready'])
        self.assertIsNone(result['rows'][0]['rr'])
        self.assertIsNone(result['rows'][0]['leak'])


if __name__ == '__main__':
    unittest.main()
