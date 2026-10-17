"""离线验证旧固件早晨成功后跳过晚间，以及两种成功标记的隔离。"""
import json
from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fitbox import cpap_pull as cpap


class LegacyScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Path(self.tmp.name) / 'cpap.jsonl'
        self.today = date(2026, 10, 17)
        self.yesterday = '20261016'
        clock = patch.object(cpap, 'date').start()
        clock.today.return_value = self.today
        patch.object(cpap, 'log').start()
        self.addCleanup(patch.stopall)

    def result(self, **overrides):
        value = {'window_complete': True, 'errors': [],
                 'treatment_complete': True, 'treatment_dates': [self.yesterday]}
        value.update(overrides)
        return value

    def marker(self):
        return self.journal.with_suffix('.legacy-success.json')

    def test_verified_last_night_skips_later_today(self):
        cpap.record_legacy_success(self.journal, self.result())
        self.assertTrue(cpap.legacy_pulled_today(self.journal))
        value = json.loads(self.marker().read_text())
        self.assertEqual(value['scope'], 'str-session-coverage')
        self.assertFalse(value['mirror_complete'])

    def test_old_night_does_not_skip(self):
        cpap.record_legacy_success(self.journal,
                                   self.result(treatment_dates=['20261015']))
        self.assertFalse(cpap.legacy_pulled_today(self.journal))
        self.assertFalse(self.marker().exists())

    def test_success_marker_expires_next_day(self):
        cpap.record_legacy_success(self.journal, self.result())
        cpap.date.today.return_value = self.today + timedelta(days=1)
        self.assertFalse(cpap.legacy_pulled_today(self.journal))

    def test_partial_or_failed_retry_clears_success(self):
        for changes in ({'window_complete': False}, {'errors': ['timeout']},
                        {'treatment_dates': [], 'treatment_complete': False}):
            with self.subTest(changes=changes):
                cpap.record_legacy_success(self.journal, self.result())
                cpap.record_legacy_success(self.journal, self.result(**changes))
                self.assertFalse(cpap.legacy_pulled_today(self.journal))
                self.assertFalse(self.marker().exists())

    def test_last_night_verified_independently_of_older_nights(self):
        # treatment_dates 是逐夜核验结果；更旧夜晚失败不否定昨夜的证据。
        cpap.record_legacy_success(self.journal,
                                   self.result(treatment_complete=False))
        self.assertTrue(cpap.legacy_pulled_today(self.journal))

    def test_full_marker_does_not_skip_legacy(self):
        self.journal.with_suffix('.success.json').write_text(json.dumps(
            {'date': str(self.today), 'files': 5}))
        self.assertTrue(cpap.pulled_today(self.journal))
        self.assertFalse(cpap.legacy_pulled_today(self.journal))

    def test_legacy_marker_does_not_skip_full_sync(self):
        cpap.record_legacy_success(self.journal, self.result())
        self.assertTrue(cpap.legacy_pulled_today(self.journal))
        self.assertFalse(cpap.pulled_today(self.journal))

    def test_failed_legacy_run_preserves_full_marker(self):
        full = self.journal.with_suffix('.success.json')
        full.write_text(json.dumps({'date': str(self.today), 'files': 5}))
        original = full.read_bytes()
        cpap.record_legacy_success(self.journal, self.result(errors=['timeout']))
        self.assertEqual(full.read_bytes(), original)

    def test_missing_malformed_or_wrong_scope_marker_stays_retryable(self):
        self.assertFalse(cpap.legacy_pulled_today(self.journal))
        for value in ('invalid JSON', '[]', 'null', json.dumps({
                'date': str(self.today), 'night': self.yesterday,
                'scope': 'full-directory'})):
            with self.subTest(value=value):
                self.marker().write_text(value)
                self.assertFalse(cpap.legacy_pulled_today(self.journal))

    def test_main_skips_before_any_network_action(self):
        cpap.record_legacy_success(self.journal, self.result())
        argv = ['cpap_pull.py', '--dest', str(Path(self.tmp.name) / 'data'),
                '--journal', str(self.journal), '--legacy-scan', '--if-missing']
        with patch('sys.argv', argv), patch.object(cpap, 'pick_net') as network:
            self.assertEqual(cpap.main(), 0)
            network.assert_not_called()

    def test_existing_legacy_cache_skips_without_explicit_mode_flag(self):
        dest = Path(self.tmp.name) / 'data'
        dest.mkdir()
        (dest / '.legacy-probes.json').write_text('{}')
        cpap.record_legacy_success(self.journal, self.result())
        argv = ['cpap_pull.py', '--dest', str(dest), '--journal',
                str(self.journal), '--if-missing']
        with patch('sys.argv', argv), patch.object(cpap, 'pick_net') as network:
            self.assertEqual(cpap.main(), 0)
            network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
