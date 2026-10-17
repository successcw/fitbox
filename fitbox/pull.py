#!/usr/bin/env python3
"""Mac side: pull new weigh-ins from fitbox, append them to the rehab log and push a notification.

Safe to run often: records already ingested (by timestamp) are skipped.
Every notification goes through notify.py — this module never talks to Telegram, and never
touches a proxy: 出网交给系统层（Clash TUN）。
"""
import json, subprocess, sys
from pathlib import Path
from config import DATA_ROOT, HOSTS, ssh_target

import notify

FIT = DATA_ROOT
DATA = FIT / '数据' / 'fitbox'
LOG = FIT / '康复日志' / 'log.jsonl'
SEEN = DATA / 'ingested.txt'



def fetch():
    DATA.mkdir(parents=True, exist_ok=True)
    err = ''
    for host in HOSTS:
        r = subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                            f'{ssh_target(host)}:fitdata/scale.jsonl', str(DATA / 'scale.jsonl')],
                           capture_output=True, text=True)
        if r.returncode == 0 or 'No such file' in r.stderr:
            return
        err = r.stderr.strip()
    sys.exit(f'fetch failed: {err}')


def last_weight():
    w = None
    for line in LOG.read_text().splitlines() if LOG.exists() else []:
        r = json.loads(line)
        if r.get('weight'):
            w = (r['date'], r['weight'])
    return w


def ingest(rec):
    """Append one weigh-in to the rehab log and push a notification; returns False if already ingested."""
    seen = set(SEEN.read_text().split()) if SEEN.exists() else set()
    if rec['ts'] in seen:
        return False
    prev = last_weight()
    date, time = rec['ts'][:10], rec['ts'][11:16]
    entry = {'date': date, 'time': time, 'source': 'scale-ble', 'weight': rec['weight'],
             'pain_hip': None, 'stiff_hip': None, 'done': [], 'food': None,
             'note': '体脂秤蓝牙自动记录（fitbox）', 'raw': None, 'impedance': rec.get('impedance'), 'scale_final': rec.get('final')}
    if rec.get('battery_guess'):
        entry['battery_guess'] = rec['battery_guess']
    if rec.get('impedance'):
        from body_metrics import metrics
        entry['body'] = metrics(rec['weight'], rec['impedance'])     # 开源小米算法，体脂/脂肪/肌肉/骨量/BMR
    with open(LOG, 'a') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    with open(SEEN, 'a') as f:
        f.write(rec['ts'] + '\n')
    diff = f"，比上次（{prev[0][5:]} {prev[1]}）{rec['weight'] - prev[1]:+.2f}" if prev else ''
    b = entry.get('body')
    comp = f"\n体脂 {b['fat_pct']}% · 脂肪 {b['fat_kg']} kg · 肌肉 {b['muscle_kg']} kg" if b else ''
    bg = rec.get('battery_guess') or {}
    if bg.get('rt_byte11') is not None and bg['rt_byte11'] <= 20:      # suspected battery %, unconfirmed
        comp += f"\n🔋 秤的电量可能只剩 {bg['rt_byte11']}%，记得充电"
    notify.send(f"⚖️ 体重 {rec['weight']:.2f} kg（{time} 自动记录{diff}）{comp}")
    return True


def main():
    fetch()
    src = DATA / 'scale.jsonl'
    if src.exists():
        for line in open(src):
            ingest(json.loads(line))


if __name__ == '__main__':
    main()
