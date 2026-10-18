#!/usr/bin/env python3
"""Telegram inbound — voice notes and text messages from the phone into the rehab workspace.

Runs as a thread inside watch.py and long-polls getUpdates, so a message sent while the Mac
was asleep is not lost: the offset is persisted and the backlog is drained on reconnect.
Every message becomes one line in 数据/fitbox/tg_inbox.jsonl:

    {"update_id": 123, "ts": "...", "date": "2026-10-14", "time": "08:22",
     "kind": "voice"|"text"|"photo", "seconds": 12,
     "file": "数据/fitbox/tg_media/20261014-082200.oga", "text": "<转写原文/消息正文>"}

Explicit completed exercise messages are recorded automatically. Other messages stay
queued for a session to review and mark done:  python3 tg_bot.py pending    列出待整理
              python3 tg_bot.py done <update_id>   标记已记录
An ack goes back to the phone immediately, so silence is never ambiguous.
"""
import json, subprocess, sys, time
from datetime import datetime, timedelta
from pathlib import Path

import notify
import training_ingest

from config import DATA_ROOT, PROJECT
FIT = DATA_ROOT
SPOOL = FIT / '数据' / 'fitbox'
INBOX = SPOOL / 'tg_inbox.jsonl'
OFFSET = SPOOL / 'tg_offset.txt'
DONE = SPOOL / 'tg_done.txt'
MEDIA = SPOOL / 'tg_media'
TRANS = PROJECT / 'scripts' / 'transcribe.sh'


def log(msg):
    print(f'{datetime.now():%m-%d %H:%M:%S} tg: {msg}', flush=True)


def _ids(path):
    ids = set()
    for line in path.read_text().splitlines() if path.exists() else []:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            value = value.get('update_id')
        if isinstance(value, (int, str)):
            ids.add(str(value))
    return ids


def pending():
    """Inbox items not yet written into the rehab log — what a session has to work through."""
    out = []
    for line in INBOX.read_text().splitlines() if INBOX.exists() else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if str(r['update_id']) not in _ids(DONE):
            out.append(r)
    return out


def mark_done(update_id):
    DONE.parent.mkdir(parents=True, exist_ok=True)
    if str(update_id) not in _ids(DONE):
        with open(DONE, 'a') as f:
            f.write(str(update_id) + '\n')


def process_record(record):
    row = training_ingest.ingest(record)
    if row is not None:
        mark_done(record['update_id'])
    return row


def replay_pending():
    """Recover queued exercise records after a restart; no Telegram messages are sent."""
    recorded = []
    for record in pending():
        row = process_record(record)
        if row is not None:
            recorded.append(row)
    return recorded


def refresh_report(row):
    """昨天的训练补记后检查日报；发送状态按训练内容去重。"""
    if row and row['date'] == (datetime.now().date()-timedelta(days=1)).isoformat():
        try:
            import health_daily
            health_daily.morning_report()
        except Exception as exc:
            log(f'report refresh failed: {exc}')


def transcribe(path):
    """scripts/transcribe.sh（mlx-whisper 优先，回退 openai-whisper）→ 中文文本，失败返回 ''"""
    try:
        r = subprocess.run(['bash', str(TRANS), str(path)], capture_output=True, text=True, timeout=420)
        return r.stdout.strip()
    except Exception as e:
        log(f'transcribe failed: {e}')
        return ''


def fetch_file(file_id, dest):
    path = notify.api('getFile', {'file_id': file_id})['result']['file_path']
    notify.download(path, dest)
    return dest


def handle(upd):
    """One update → (ack text, inbox record) or (None, None) if it should be ignored."""
    msg = upd.get('message') or upd.get('edited_message') or {}
    chat = str((msg.get('chat') or {}).get('id', ''))
    if not chat or chat != str(notify.config()['chat']):        # only the owner's chat may inject
        log(f'ignored update {upd.get("update_id")} from chat {chat or "?"}')
        return None, None
    ts = datetime.fromtimestamp(msg.get('date') or time.time())
    rec = {'update_id': upd['update_id'], 'ts': ts.isoformat(timespec='seconds'),
           'date': f'{ts:%Y-%m-%d}', 'time': f'{ts:%H:%M}', 'text': '', 'file': None, 'seconds': None}
    MEDIA.mkdir(parents=True, exist_ok=True)
    stem = f'{ts:%Y%m%d-%H%M%S}-{upd["update_id"]}'

    if msg.get('voice') or msg.get('audio'):
        a = msg.get('voice') or msg.get('audio')
        f = fetch_file(a['file_id'], MEDIA / f'{stem}.oga')
        text = transcribe(f)
        rec |= {'kind': 'voice', 'seconds': a.get('duration'), 'file': str(f.relative_to(FIT)), 'text': text}
        if not text:
            return f'🎙 收到语音（{a.get("duration")} 秒），但转写失败，文件已留存，稍后人工处理。', rec
        return f'🎙 收到语音（{a.get("duration")} 秒），转写：\n{text}\n\n（待按标准名记入康复日志）', rec

    if msg.get('photo'):
        f = fetch_file(msg['photo'][-1]['file_id'], MEDIA / f'{stem}.jpg')
        cap = (msg.get('caption') or '').strip()
        rec |= {'kind': 'photo', 'file': str(f.relative_to(FIT)), 'text': cap}
        return f'📷 收到图片{f"，说明：{cap}" if cap else ""}（已存 {f.name}）', rec

    if msg.get('text'):
        t = msg['text'].strip()
        rec |= {'kind': 'text', 'text': t}
        return f'📝 收到：{t}\n\n（待整理）', rec

    return None, None


def poll(once=False):
    """Long-poll getUpdates; persist the offset so a reconnect drains the backlog exactly once."""
    SPOOL.mkdir(parents=True, exist_ok=True)
    recovered = replay_pending()
    for row in recovered:
        refresh_report(row)
    offset = int(OFFSET.read_text().strip()) if OFFSET.exists() else 0
    while True:
        r = notify.api('getUpdates', {'offset': offset, 'timeout': 0 if once else 50, 'limit': 10,
                                      'allowed_updates': ['message', 'edited_message']}, timeout=75)
        for upd in r.get('result') or []:
            offset = max(offset, upd['update_id'] + 1)
            if str(upd['update_id']) in _ids(INBOX):        # already queued; just advance
                OFFSET.write_text(f'{offset}\n')
                continue
            try:
                ack, rec = handle(upd)
            except Exception as e:
                log(f'handle {upd["update_id"]} failed: {e}')
                continue
            if rec is None:
                OFFSET.write_text(f'{offset}\n')
                continue
            SPOOL.mkdir(parents=True, exist_ok=True)
            with open(INBOX, 'a') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            OFFSET.write_text(f'{offset}\n')                # queued first: never lose an item
            log(f"queued {rec['kind']} {rec['update_id']} {rec['date']} {rec['time']}")
            row = process_record(rec)
            if row is not None:
                ack = f"✅ 已记 {row['date']} 训练：{'、'.join(row['done'])}"
            if ack:
                notify.send(ack)
            if row is not None:
                refresh_report(row)
        if once:
            return
        time.sleep(1)


def run():
    """Thread entry point: never let a network hiccup kill the watcher."""
    log(f"listening (chat {notify.config()['chat']}, {len(pending())} pending)")
    while True:
        try:
            poll()
        except Exception as e:
            log(f'poll failed: {e}')
            time.sleep(15)


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'pending'
    if cmd == 'pending':
        items = pending()
        print(f'{len(items)} 条待整理')
        for r in items:
            print(json.dumps(r, ensure_ascii=False))
    elif cmd == 'done':
        mark_done(sys.argv[2])
        print('marked done:', sys.argv[2])
    elif cmd == 'once':
        poll(once=True)
        print('drained')
    elif cmd == 'test':
        # 用本地音频走一遍转写链，不需要真发 TG
        print(repr(transcribe(Path(sys.argv[2]))))
    else:
        run()
