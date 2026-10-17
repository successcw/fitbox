#!/usr/bin/env python3
"""Render a fully synthetic report without SSH, real data or Telegram requests."""
import argparse
import json
import os
import sys
from pathlib import Path
from datetime import date, timedelta

root = Path(__file__).resolve().parents[1]
# Ignore real configuration, including the running project's files.
for key in list(os.environ):
    if key.startswith('FITBOX_') or key.startswith('TELEGRAM_'):
        del os.environ[key]
os.environ['FITBOX_CONFIG'] = str(root / 'examples/no-real-config.env')
os.environ['FITBOX_DATA_ROOT'] = str(root / 'runtime/demo')
sys.path.insert(0, str(root / 'fitbox'))
import health_daily as h
import health_card
from sleep_stages import LABELS

parser = argparse.ArgumentParser()
parser.add_argument('--html-only', action='store_true', help='Do not launch Chrome')
args = parser.parse_args()
today = date(2025, 1, 15)
data = {}
for offset in range(14):
    day = today - timedelta(days=offset)
    data[str(day)] = {'sleep_analysis': {'sleepStart': f'{day} 00:00:00 +0800',
        'sleepEnd': f'{day} 07:00:00 +0800', 'totalSleep': 7, 'deep': 1,
        'rem': 2, 'core': 4, 'awake': 0}, 'resting_heart_rate': 58,
        'heart_rate_variability': 45, 'active_energy': 1400, 'basal_energy_burned': 6500,
        'apple_exercise_time': 25, 'step_count': 6000}
h.LOG.parent.mkdir(parents=True, exist_ok=True)
h.LOG.write_text(json.dumps({'date': str(today-timedelta(days=1)), 'source': 'synthetic',
    'done': ['演示动作 8次×2组'], 'food': None, 'note': '虚构数据，不是个人健康记录'}, ensure_ascii=False)+'\n')
_, verdict, _, ex = h.build(data, today)
ex['cpap'] = {'min':420, 'ahi':1.5, 'leak95':9, 'rr50':16,
              'rr_baseline':16,'rr_baseline_n':7,'rr_low':15,'rr_high':17}
ex['cpap_brief'] = h.cpap_brief(ex['cpap'])
segments = [('core','00:00','01:00'),('deep','01:00','02:00'),('core','02:00','05:00'),('rem','05:00','07:00')]
ex['stage_comparison'] = {'ready':True,'segments':[
    {'stage':k,'start':f'{today}T{a}:00','end':f'{today}T{b}:00'} for k,a,b in segments],
    'sessions':[{'start':f'{today}T00:00:00','end':f'{today}T07:00:00'}],
    'rows':[{'stage':k,'label':LABELS[k],'minutes':m,'matched_minutes':m,'coverage':1,'rr':r,'leak':l}
            for k,m,r,l in [('deep',60,15,6),('rem',120,17,9),('core',240,16,8)]],
    'summary':'全部数值均为合成演示，不代表任何人的健康状况。'}
verdict = '合成示例 · 不用于健康判断'
health_card.OUT.mkdir(parents=True, exist_ok=True)
if args.html_only:
    path=health_card.OUT/'demo.html'
    path.write_text(health_card.html(data,today,verdict,False,None,ex))
    print(path)
else:
    print(health_card.render(data,today,verdict,False,None,ex))
