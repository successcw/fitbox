"""晨报发送结果、迟到的呼吸机数据与并发重放；不连接通知服务。"""
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import health_daily as daily


class MorningReportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.today = date(2026, 10, 17)
        self.cp = None
        self.comparison = None
        self.sleep = {'sleepStart': '2026-10-17T01:00', 'sleepEnd': '2026-10-17T08:00'}
        self.render = Mock(return_value=(root / 'card.png', None))
        patches = [
            patch.object(daily, 'DONE', root / 'reported.txt'),
            patch.object(daily, 'LOG', root / 'log.jsonl'),
            patch.object(daily, 'load', return_value={str(self.today): {'sleep_analysis': self.sleep}}),
            patch.object(daily, 'cpap_night', side_effect=lambda _: self.cp),
            patch.object(daily, 'build', side_effect=lambda *args: ('📝 日报', '正常', self.sleep, {'cpap': self.cp, **({'sleep_comparison': self.comparison} if self.comparison is not None else {})})),
            patch.object(daily, 'todays_weight', return_value=None),
            patch.dict(sys.modules, {'health_card': SimpleNamespace(render=self.render)}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.image = patch.object(daily.notify, 'send_image', return_value=True).start()
        self.text = patch.object(daily.notify, 'send', return_value=False).start()
        self.addCleanup(patch.stopall)

    def report(self, **kwargs):
        return daily.morning_report(self.today, wait_cpap=False, **kwargs)

    def test_late_stage_details_supplement_once(self):
        self.cp = {'min': 407}
        self.comparison = {'stop_windows': [{'details_ready': True}]}
        built = ('日报', '正常', self.sleep,
                 {'cpap': self.cp, 'sleep_comparison': self.comparison,
                  'stage_comparison': {'ready': False}})
        with patch.object(daily, 'build', return_value=built):
            self.assertTrue(self.report())
            self.assertFalse(self.report())
            built[3]['stage_comparison']['ready'] = True
            self.assertTrue(self.report())
            self.assertFalse(self.report())
        self.assertEqual(self.image.call_count, 2)

    def test_failed_delivery_has_no_success_mark_and_retries(self):
        self.image.return_value = False
        self.assertFalse(self.report())
        self.assertFalse(daily.DONE.exists())
        self.assertFalse(daily.DONE.with_suffix('.json').exists())
        self.assertFalse(daily.LOG.exists())
        self.text.return_value = True
        self.assertTrue(self.report())
        self.assertEqual(self.text.call_count, 2)
        self.assertEqual(json.loads(daily.DONE.with_suffix('.json').read_text())[str(self.today)]['channel'], 'text')
        self.assertEqual(daily.DONE.read_text().split(), [str(self.today)])

    def test_late_cpap_gets_one_update_only(self):
        self.assertTrue(self.report())
        self.assertFalse(self.report())
        self.cp = {'min': 407}
        self.assertTrue(self.report())
        self.assertFalse(self.report())
        self.assertEqual(self.image.call_count, 2)
        self.assertTrue(json.loads(daily.DONE.with_suffix('.json').read_text())[str(self.today)]['cpap'])
        self.assertEqual(daily.DONE.read_text().split(), [str(self.today)])

    def test_failed_update_preserves_retry(self):
        self.assertTrue(self.report())
        self.cp = {'min': 407}
        self.image.return_value = False
        self.assertFalse(self.report())
        self.assertFalse(json.loads(daily.DONE.with_suffix('.json').read_text())[str(self.today)]['cpap'])
        self.image.return_value = True
        self.assertTrue(self.report())

    def test_legacy_mark_needs_explicit_force(self):
        daily.DONE.write_text(str(self.today) + '\n')
        self.cp = {'min': 407}
        self.assertFalse(self.report())
        self.image.assert_not_called()
        self.assertTrue(self.report(force=True))
        self.assertFalse(self.report())

    def test_render_failure_falls_back_to_text(self):
        self.render.side_effect = RuntimeError('render failed')
        self.text.return_value = True
        self.assertTrue(self.report())
        self.image.assert_not_called()
        self.text.assert_called_once_with('📝 日报')

    def test_concurrent_callbacks_send_once(self):
        self.cp = {'min': 407}
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.report(), range(8)))
        self.assertEqual(sum(results), 1)
        self.image.assert_called_once()


    def at_time(self, hour):
        date_mock = patch.object(daily, 'date').start()
        date_mock.today.return_value = self.today
        clock_mock = patch.object(daily, 'datetime').start()
        clock_mock.now.return_value = datetime(2026, 10, 17, hour, 0)

    def test_str_before_waveforms_waits_before_deadline(self):
        self.at_time(8)
        self.cp = {'min': 407}
        for windows in ([], [{'status': 'unknown'}], [{'status': 'partial'}]):
            self.comparison = {'stop_windows': windows}
            self.assertFalse(daily.morning_report(self.today))
        self.image.assert_not_called()
        self.comparison = {'stop_windows': [{'status': 'complete'}]}
        self.assertTrue(daily.morning_report(self.today))

    def test_deadline_partial_then_final_waveform_updates_once(self):
        self.at_time(10)
        self.cp = {'min': 407}
        self.comparison = {'stop_windows': [{'status': 'partial'}, {'status': 'complete'}]}
        self.assertTrue(daily.morning_report(self.today))
        state = json.loads(daily.DONE.with_suffix('.json').read_text())[str(self.today)]
        self.assertTrue(state['cpap'])
        self.assertFalse(state['details_ready'])
        self.assertFalse(daily.morning_report(self.today))
        self.comparison['stop_windows'][0]['status'] = 'complete'
        self.assertTrue(daily.morning_report(self.today))
        self.assertFalse(daily.morning_report(self.today))
        self.assertEqual(self.image.call_count, 2)
        self.assertTrue(json.loads(daily.DONE.with_suffix('.json').read_text())[str(self.today)]['details_ready'])

    def test_old_cpap_state_does_not_trigger_new_analysis(self):
        daily.DONE.with_suffix('.json').write_text(json.dumps({str(self.today): {'cpap': True}}))
        self.cp = {'min': 407}
        self.comparison = {'stop_windows': [{'status': 'complete'}]}
        self.assertFalse(self.report())
        daily.build.assert_not_called()
        self.image.assert_not_called()

    def test_force_allows_partial_before_deadline(self):
        self.at_time(8)
        self.cp = {'min': 407}
        self.comparison = {'stop_windows': [{'status': 'partial'}]}
        self.assertTrue(daily.morning_report(self.today, force=True))
        self.assertFalse(json.loads(daily.DONE.with_suffix('.json').read_text())[str(self.today)]['details_ready'])


    def test_partial_window_with_verified_edge_tolerance_is_ready(self):
        self.at_time(8)
        self.cp = {'min': 407}
        self.comparison = {'stop_windows': [{'status': 'partial', 'details_ready': True}]}
        self.assertTrue(daily.morning_report(self.today))
        self.assertTrue(json.loads(daily.DONE.with_suffix('.json').read_text())[str(self.today)]['details_ready'])

    def test_explicit_not_ready_overrides_complete_status(self):
        self.at_time(8)
        self.cp = {'min': 407}
        self.comparison = {'stop_windows': [{'status': 'complete', 'details_ready': False}]}
        self.assertFalse(daily.morning_report(self.today))


class CpapSummaryTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 10, 16)
        self.record = {key: [1] for key in ('Duration', 'MaskEvents', 'AHI', 'OAI', 'CAI',
                      'Leak.50', 'Leak.95', 'Leak.Max', 'MaskPress.50', 'MaskPress.95',
                      'S.HumLevel', 'S.SmartStart')}
        self.record.update(Duration=[407], MaskEvents=[2], MaskOn=[787, -1], MaskOff=[1194, -1])
        self.edf = {'start': datetime(2026, 10, 16, 12), 'duration': 86400, 'records': [self.record]}

    def summary(self):
        with patch('cpap_legacy.read_edf', return_value=self.edf):
            return daily.cpap_night(self.today)

    def test_valid_closed_summary_and_noon_start(self):
        result = self.summary()
        self.assertEqual(result['on'], ['01:07'])
        self.assertEqual(result['off'], ['07:54'])
        self.assertEqual(result['min'], 407)

    def test_corrupt_str_is_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / '呼吸机'
            path.mkdir()
            (path / 'STR.edf').write_bytes(b'broken EDF')
            with patch.object(daily, 'FIT', Path(root)):
                self.assertIsNone(daily.cpap_night(self.today))

    def test_unclosed_session_is_ignored(self):
        self.record['MaskOff'] = [-1, -1]
        self.assertIsNone(self.summary())

    def test_event_count_mismatch_is_ignored(self):
        self.record['MaskEvents'] = [4]
        self.assertIsNone(self.summary())

    def test_saturated_slots_are_ignored(self):
        self.record.update(MaskOn=[787], MaskOff=[1194])
        self.assertIsNone(self.summary())

    def test_unknown_signal_is_ignored(self):
        self.record.pop('AHI')
        self.assertIsNone(self.summary())


if __name__ == '__main__':
    unittest.main()
