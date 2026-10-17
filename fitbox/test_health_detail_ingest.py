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


if __name__ == '__main__':
    unittest.main()
