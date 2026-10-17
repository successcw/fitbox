"""离线验证同步失败重试和可变汇总文件，不操作真实网络。"""
import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from fitbox import cpap_pull as cpap


def edf(sample=b'\x01\x00'):
    """最小合法 EDF：一个通道、一条记录、一个采样点。"""
    header = bytearray(b' ' * 512)
    header[0:8] = b'0       '
    header[184:192] = b'512     '
    header[236:244] = b'1       '
    header[252:256] = b'1   '
    header[472:480] = b'1       '
    return bytes(header) + sample


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name) / 'cpap'
        self.journal = Path(self.tmp.name) / 'cpap.jsonl'
        self.night = cpap.date.today().strftime('%Y%m%d')
        self.log = patch.object(cpap, 'log').start()
        self.addCleanup(patch.stopall)

    def entry(self, name, root=False):
        rel = name if root else f'DATALOG/{self.night}/{name}'
        return cpap.Entry(name, False, 1024, rel)

    def run_sync(self, entries, failures=(), deadline=None):
        def download(remote, dst, base):
            if remote.replace('\\', '/') in failures:
                raise OSError('模拟下载中断')
            dst.write_bytes(edf())
            return dst.stat().st_size

        with patch.object(cpap, 'collect_wanted', return_value=entries), \
                patch.object(cpap, 'download', side_effect=download):
            return cpap.sync(self.dest, None, None, cpap.BASE,
                             journal=self.journal, deadline=deadline)

    def test_partial_download_failure_stays_retryable(self):
        first, second = self.entry('one.edf'), self.entry('two.edf')
        self.assertEqual(self.run_sync([first, second], failures=[second.rel]), -1)
        self.assertTrue((self.dest / first.rel).exists())
        self.assertTrue(self.journal.exists())
        self.assertFalse(cpap.pulled_today(self.journal))

    def test_deadline_after_first_download_stays_retryable(self):
        entries = [self.entry('one.edf'), self.entry('two.edf')]
        with patch.object(cpap, 'time') as clock:
            clock.time.side_effect = [1, 3]
            self.assertEqual(self.run_sync(entries, deadline=2), -1)
        self.assertTrue((self.dest / entries[0].rel).exists())
        self.assertFalse(cpap.pulled_today(self.journal))

    def test_complete_night_can_skip_later_run(self):
        self.assertEqual(self.run_sync([self.entry('one.edf')]), 1)
        self.assertTrue(cpap.pulled_today(self.journal))

    def test_root_only_does_not_mark_night_complete(self):
        self.assertEqual(self.run_sync([self.entry('STR.edf', root=True)]), 1)
        self.assertFalse(cpap.pulled_today(self.journal))

    def test_same_size_summary_is_refreshed(self):
        self.dest.mkdir()
        summary = self.dest / 'STR.edf'
        summary.write_bytes(edf(b'\x02\x00'))
        self.assertTrue(cpap.edf_complete(summary))
        self.assertEqual(self.run_sync([self.entry('STR.edf', root=True)]), 1)
        self.assertEqual(summary.read_bytes(), edf())
        record = json.loads(self.journal.read_text())
        self.assertEqual(record['sha256'], hashlib.sha256(edf()).hexdigest())

    def test_login_page_is_rejected(self):
        for page in (b'<form name="REDIRECTFORM"></form>',
                     b'<input name="loginPass">'):
            with self.subTest(page=page), patch.object(cpap, '_fetch', return_value=page):
                with self.assertRaises(RuntimeError):
                    cpap.listdir(cpap.ROOT)

    def test_legacy_summary_does_not_claim_full_sync(self):
        with patch.object(cpap, '_fetch', return_value=edf()) as fetch:
            self.assertEqual(cpap.sync_legacy_summary(self.dest, self.journal), 0)
        self.assertIn('fname=STR.EDF&fdir=..', fetch.call_args.args[0])
        self.assertEqual((self.dest / 'STR.edf').read_bytes(), edf())
        self.assertFalse(cpap.pulled_today(self.journal))

    def test_legacy_bad_download_preserves_existing_summary(self):
        self.dest.mkdir()
        path = self.dest / 'STR.edf'
        path.write_bytes(edf())
        with patch.object(cpap, '_fetch', return_value=b'<html>error</html>'):
            with self.assertRaises(RuntimeError):
                cpap.sync_legacy_summary(self.dest, self.journal)
        self.assertEqual(path.read_bytes(), edf())
        self.assertFalse(self.journal.exists())

    def test_mac_unknown_original_network_refuses_to_switch(self):
        result = subprocess.CompletedProcess([], 0, 'You are not associated with an AirPort network.', '')
        with patch.object(cpap.shutil, 'which', return_value='/mock/networksetup'), \
                patch.object(cpap, '_run', return_value=result) as run:
            with self.assertRaises(SystemExit):
                cpap.net_up('macos', 'en0', 'ez Share', '88888888')
            run.assert_called_once_with(['networksetup', '-getairportnetwork', 'en0'])

    def test_mac_zero_exit_with_error_output_is_failure(self):
        for message in ('Failed to join network', 'Error: network unavailable'):
            with self.subTest(message=message):
                def run(cmd):
                    if '-getairportnetwork' in cmd:
                        return subprocess.CompletedProcess(cmd, 0, 'Current Wi-Fi Network: home', '')
                    output = message if 'ez Share' in cmd else ''
                    return subprocess.CompletedProcess(cmd, 0, output, '')

                with patch.object(cpap.shutil, 'which', return_value='/mock/networksetup'), \
                        patch.object(cpap, '_run', side_effect=run) as calls:
                    with self.assertRaises(RuntimeError):
                        cpap.net_up('macos', 'en0', 'ez Share', '88888888')
                    calls.assert_any_call(['networksetup', '-setairportnetwork', 'en0', 'home'])

    def test_pi_restore_only_cancels_watchdog_on_success(self):
        for failed in (True, False):
            with self.subTest(failed=failed):
                def nm(*args):
                    if args[:2] == ('-g', 'GENERAL.CONNECTION'):
                        return subprocess.CompletedProcess(args, 0, 'home', '')
                    if args == ('--wait', '20', 'con', 'up', 'home'):
                        return subprocess.CompletedProcess(args, int(failed), '', '')
                    return subprocess.CompletedProcess(args, 0, '', '')

                with patch.object(cpap.shutil, 'which', return_value='/mock/nmcli'), \
                        patch.object(cpap, '_nm', side_effect=nm), \
                        patch.object(cpap, '_run', return_value=subprocess.CompletedProcess([], 0)) as run, \
                        patch.object(cpap, '_arm_watchdog', return_value='recovery'):
                    restore = cpap.net_up('nm', 'wlan0', 'ez Share', '88888888')
                    restore()
                    self.assertFalse(any('fit-netwatch' in str(call) for call in run.call_args_list))
                    cancellations = [c for c in run.call_args_list if 'recovery.timer' in c.args[0]]
                    self.assertEqual(len(cancellations), 0 if failed else 1)

    def test_pi_activation_timeout_checks_state_before_retry(self):
        for connected, cancel_fails, final_state, expected_attempts in (
                (True, False, None, 1), (False, False, None, 3),
                (False, True, None, 1),
                (False, True, '100 (connected)\nhome', 3),
                (False, True, '30 (disconnected)\n--', 3),
                (False, True, '120 (failed)\n--', 3)):
            with self.subTest(connected=connected, cancel_fails=cancel_fails, final_state=final_state):
                calls = []

                def nm(*args):
                    calls.append(args)
                    if args[:2] == ('-g', 'GENERAL.CONNECTION'):
                        return subprocess.CompletedProcess(args, 0, 'home', '')
                    if args[:2] == ('-g', 'GENERAL.STATE,GENERAL.CONNECTION'):
                        value = final_state or ('100 (connected)\nezshare' if connected else '50 (connecting)\nezshare')
                        return subprocess.CompletedProcess(args, 0, value, '')
                    if args == ('--wait', '60', 'con', 'up', 'ezshare'):
                        return subprocess.CompletedProcess(args, 3, '', 'Timeout expired')
                    if args == ('--wait', '20', 'con', 'down', 'ezshare'):
                        return subprocess.CompletedProcess(args, int(cancel_fails), '', '')
                    return subprocess.CompletedProcess(args, 0, '', '')

                with patch.object(cpap.shutil, 'which', return_value='/mock/nmcli'), \
                        patch.object(cpap, '_nm', side_effect=nm), \
                        patch.object(cpap, '_run', return_value=subprocess.CompletedProcess([], 0)), \
                        patch.object(cpap, '_arm_watchdog', return_value='recovery'):
                    cpap.net_up('nm', 'wlan0', 'ez Share', '88888888')
                ups = [i for i, args in enumerate(calls) if args == ('--wait', '60', 'con', 'up', 'ezshare')]
                downs = [i for i, args in enumerate(calls) if args == ('--wait', '20', 'con', 'down', 'ezshare')]
                self.assertEqual(len(ups), expected_attempts)
                self.assertEqual(len(downs), 0 if connected else expected_attempts)
                for previous, following in zip(ups, ups[1:]):
                    self.assertTrue(any(previous < i < following for i in downs))

    def test_watchdog_uses_independent_unique_systemd_timer(self):
        with patch.object(cpap, '_run', return_value=subprocess.CompletedProcess([], 0)) as run, \
                patch.object(cpap.subprocess, 'Popen') as popen:
            first = cpap._arm_watchdog('home network', 60)
            second = cpap._arm_watchdog('home network', 60)
            self.assertNotEqual(first, second)
            command = run.call_args_list[0].args[0]
            self.assertIn('systemd-run', command)
            self.assertIn('--on-active=60s', command)
            self.assertIn('--unit=' + first, command)
            self.assertIn('nmcli --wait 20 con up', command[-1])
            self.assertNotIn('fit-netwatch', command[-1])
            popen.assert_not_called()

    def test_watchdog_creation_failure_leaves_network_untouched(self):
        with patch.object(cpap.shutil, 'which', return_value='/mock/nmcli'), \
                patch.object(cpap, '_nm', return_value=subprocess.CompletedProcess([], 0, 'home', '')) as nm, \
                patch.object(cpap, '_run', return_value=subprocess.CompletedProcess([], 1, '', 'denied')) as run:
            with self.assertRaisesRegex(RuntimeError, '取消切网'):
                cpap.net_up('nm', 'wlan0', 'ez Share', '88888888')
            nm.assert_called_once_with('-g', 'GENERAL.CONNECTION', 'dev', 'show', 'wlan0')
            self.assertEqual(run.call_count, 1)
            self.assertIn('systemd-run', run.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
