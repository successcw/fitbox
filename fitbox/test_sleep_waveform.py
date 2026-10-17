import struct
import tempfile
import unittest
from pathlib import Path

from fitbox.sleep_waveform import analyze_stop_windows


def fixture(path, unit='L/s', stamp='17.10.26', time='01.00.00', count=300):
    labels = ['Leak.2s', 'Press.2s', 'FlowLim.2s']
    def field(values, width):
        return b''.join(str(v).encode().ljust(width) for v in values)
    head = (b'0       '+b' '*160+stamp.encode()+time.encode()+b'1024    '+b' '*44+
            field([count],8)+b'2       '+b'3   ')
    signals = (field(labels,16)+field(['']*3,80)+field([unit,'cmH2O',''],8)+
               field([0]*3,8)+field([100]*3,8)+field([0]*3,8)+field([10000]*3,8)+
               field(['']*3,80)+field([1]*3,8)+field(['']*3,32))
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(head+signals+b''.join(struct.pack('<hhh',50,1000,20) for _ in range(count)))


def event_fixture(path):
    def f(value, width):
        return str(value).encode().ljust(width)
    tal = (b'+0\x14Recording starts\x14\0+0\x1510\x14Obstructive Apnea\x14\0'
           b'+599\x1510\x14Hypopnea\x14\0+600\x1510\x14Central Apnea\x14\0')
    tal = tal.ljust(256,b'\0')
    header = b'0       '+b' '*160+b'17.10.26'+b'01.00.00'+f(512,8)+b' '*44+f(1,8)+f(0,8)+f(1,4)
    signal = (f('EDF Annotations',16)+f('',80)+f('',8)+f(-1,8)+f(1,8)+
              f(-32768,8)+f(32767,8)+f('',80)+f(128,8)+f('',32))
    path.write_bytes(header+signal+tal)


class StopWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root/'DATALOG/20261016/20261017_010000_PLD.edf'
        self.sessions = [{'start':'2026-10-17T01:00:00','end':'2026-10-17T01:10:00'}]

    def tearDown(self):
        self.tmp.cleanup()

    def result(self):
        return analyze_stop_windows(self.root,self.sessions)[0]

    def test_units_and_full_coverage(self):
        fixture(self.path)
        result = self.result()
        self.assertEqual(result['leak']['p95'],30)
        self.assertEqual(result['leak']['coverage_seconds'],600)
        self.assertEqual(result['leak']['samples'],300)
        self.assertEqual(result['pressure']['p50'],10)
        self.assertEqual(result['flow_limit']['p50'],.2)
        self.assertIsNone(result['events']['counts'])
        self.assertFalse(result['details_ready'])

    def test_liters_per_minute_not_multiplied(self):
        fixture(self.path,unit='L/min')
        self.assertEqual(self.result()['leak']['p50'],.5)

    def test_eve_half_open_window_counts_and_complete_status(self):
        fixture(self.path)
        event_fixture(self.path.with_name('test_EVE.edf'))
        result = self.result()
        self.assertEqual(result['status'],'complete')
        self.assertTrue(result['details_ready'])
        self.assertEqual(result['events']['counts']['OA'],1)
        self.assertEqual(result['events']['counts']['H'],1)
        self.assertEqual(result['events']['counts']['CA'],0)

    def test_unsupported_unit_is_unknown(self):
        fixture(self.path,unit='ml/s')
        self.assertIsNone(self.result()['leak']['p50'])

    def test_misplaced_file_uses_header_not_directory(self):
        fixture(self.path,stamp='18.10.26')
        result = self.result()
        self.assertEqual(result['status'],'unknown')
        self.assertTrue(any('头部' in x for x in result['issues']))

    def test_missing_and_truncated_are_not_zero(self):
        self.assertIsNone(self.result()['leak']['p50'])
        fixture(self.path)
        self.path.write_bytes(self.path.read_bytes()[:-1])
        self.assertIsNone(self.result()['leak']['p50'])
        event_fixture(self.path.with_name('test_EVE.edf'))
        self.assertFalse(self.result()['details_ready'])

    def test_str_minute_boundary_tolerance_preserves_partial_coverage(self):
        fixture(self.path)
        event_fixture(self.path.with_name('test_EVE.edf'))
        self.sessions[0]['end'] = '2026-10-17T01:10:43'
        result = self.result()
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['leak']['coverage_seconds'],557)
        self.assertTrue(result['details_ready'])
        self.sessions[0]['end'] = '2026-10-17T01:11:01'
        self.assertFalse(self.result()['details_ready'])

    def test_internal_gap_not_covered_by_boundary_tolerance(self):
        fixture(self.path,count=150)
        fixture(self.path.with_name('second_PLD.edf'),time='01.05.43',count=150)
        event_fixture(self.path.with_name('test_EVE.edf'))
        result = self.result()
        self.assertEqual(result['leak']['coverage_seconds'],557)
        self.assertFalse(result['details_ready'])

    def test_bad_file_or_units_blocks_readiness_despite_valid_coverage(self):
        fixture(self.path)
        event_fixture(self.path.with_name('test_EVE.edf'))
        fixture(self.path.with_name('wrong_PLD.edf'),unit='ml/s')
        self.assertFalse(self.result()['details_ready'])
        fixture(self.path.with_name('wrong_PLD.edf'),stamp='18.10.26')
        self.assertFalse(self.result()['details_ready'])

    def test_partial_window_and_duplicates_do_not_inflate_coverage(self):
        fixture(self.path,time='01.05.00')
        duplicate = self.path.with_name('duplicate_PLD.edf')
        duplicate.write_bytes(self.path.read_bytes())
        result = self.result()
        self.assertEqual(result['leak']['coverage_seconds'],300)
        self.assertEqual(result['leak']['coverage_fraction'],.5)
        self.assertEqual(result['leak']['samples'],150)

    def test_short_session_window_and_invalid_range(self):
        fixture(self.path)
        self.sessions[0]['end'] = '2026-10-17T01:02:00'
        self.assertEqual(self.result()['window_seconds'],120)
        self.sessions[0]['end'] = '2026-10-17T00:00:00'
        self.assertEqual(self.result()['status'],'unknown')


if __name__ == '__main__':
    unittest.main()
