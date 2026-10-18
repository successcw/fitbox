import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import training_ingest as ingest
import tg_bot


class TrainingIngestTests(unittest.TestCase):
    def record(self, text, update_id=42, ts='2026-10-18T07:50:46'):
        return {'update_id': update_id, 'ts': ts, 'kind': 'voice', 'text': text, 'date': ts[:10], 'time': ts[11:16]}

    def test_duration_variants(self):
        for duration in ('1.5小时', '一个半小时', '一个小时半', '90分钟'):
            row = ingest.parse_training(self.record(f'昨天打了{duration}篮球,记录一下。'))
            self.assertEqual(row['done'], ['篮球90分钟'])
            self.assertEqual(row['date'], '2026-10-17')
            self.assertIsNone(row['time'])

    def test_dates_use_message_not_processing_time(self):
        row = ingest.parse_training(self.record('昨天打了篮球90分钟', ts='2026-01-01T00:01:00'))
        self.assertEqual(row['date'], '2025-12-31')
        row = ingest.parse_training(self.record('今天我打了篮球90分钟', ts='2026-01-01T00:01:00'))
        self.assertEqual(row['date'], '2026-01-01')

    def test_uncertain_messages_remain_pending(self):
        for text in ('昨天打了90分钟篮球吗', '昨天没打90分钟篮球', '今天打算打90分钟篮球',
                     '明天打90分钟篮球', '昨天打了90分钟篮球？', '昨天打了篮球',
                     '昨天打了90分钟篮球，今天计划跑步', '今天打了0分钟篮球',
                     '昨天打了90分钟篮球，没完成', '今天练了死虫10*3'):
            self.assertIsNone(ingest.parse_training(self.record(text)), text)

    def test_idempotent_replay(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'log.jsonl'
            rec = self.record('昨天打了一个半小时篮球,记录一下。')
            first = ingest.ingest(rec, path)
            self.assertEqual(ingest.ingest(rec, path), first)
            self.assertEqual(len(path.read_text().splitlines()), 1)
            self.assertEqual(first['raw'], rec['text'])
            self.assertEqual(first['source'], 'telegram')

    def test_jsonl_ids_and_pending_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            inbox, done, logfile = root/'inbox.jsonl', root/'done.txt', root/'log.jsonl'
            inbox.write_text('\n'.join(json.dumps(r) for r in [
                self.record('昨天打了一个半小时篮球,记录一下。'),
                self.record('昨天打了90分钟篮球吗', 43)]) + '\n')
            with patch.object(tg_bot, 'INBOX', inbox), patch.object(tg_bot, 'DONE', done), patch.object(ingest, 'LOG', logfile):
                self.assertEqual(tg_bot._ids(inbox), {'42', '43'})
                self.assertEqual(len(tg_bot.replay_pending()), 1)
                self.assertEqual(tg_bot.replay_pending(), [])
                self.assertEqual(tg_bot._ids(done), {'42'})
                self.assertEqual([r['update_id'] for r in tg_bot.pending()], [43])
                self.assertEqual(len(logfile.read_text().splitlines()), 1)

    def test_poll_records_before_success_ack(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            rec = self.record('昨天打了90分钟篮球')
            with patch.multiple(tg_bot, SPOOL=root, INBOX=root/'inbox.jsonl', DONE=root/'done.txt', OFFSET=root/'offset.txt'), patch.object(ingest, 'LOG', root/'log.jsonl'), patch.object(tg_bot.notify, 'api', return_value={'result': [{'update_id': 42}]}), patch.object(tg_bot, 'handle', return_value=('待整理', rec)), patch.object(tg_bot.notify, 'send') as send, patch.object(tg_bot, 'refresh_report') as refresh:
                tg_bot.poll(once=True)
                self.assertIn('已记 2026-10-17 训练：篮球90分钟', send.call_args.args[0])
                tg_bot.poll(once=True)
                self.assertEqual(send.call_count, 1)
                self.assertEqual(refresh.call_count, 1)
                self.assertEqual(len((root/'log.jsonl').read_text().splitlines()), 1)


if __name__ == '__main__':
    unittest.main()
