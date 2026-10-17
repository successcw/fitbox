#!/usr/bin/env python3
"""Health Auto Export pushes → per-day store + one morning report.

merge(file): fold daily summaries into 数据/健康/daily.json; keep detailed pushes in raw/.
morning_report(): once per day, as soon as last night's sleep is present, send a summary of the night,
recovery signals vs the 7-day average, yesterday's activity and this morning's weight; also log it.
The report is pushed through notify.py — this module knows nothing about the channel.
"""
import json, sys
from threading import Lock
from datetime import date, datetime, timedelta

import notify
from pull import FIT, LOG
from config import WEIGHT_GOAL

STORE = FIT / '数据' / '健康' / 'daily.json'
ADDITIVE = {'step_count', 'active_energy', 'basal_energy_burned', 'apple_exercise_time', 'apple_stand_time',
            'apple_stand_hour', 'flights_climbed', 'walking_running_distance', 'time_in_daylight'}
DONE = FIT / '数据' / '健康' / 'reported.txt'
REPORT_LOCK = Lock()
REPORT_DEADLINE = '09:30'        # 等呼吸机数据的最晚时刻；过了就照发（见 morning_report）


def load():
    if not STORE.exists():
        return {}
    with STORE.open() as f:
        return json.load(f)


def is_detailed_metrics(metrics):
    """A raw export is a separate feed, never a replacement for daily totals.

    Auto Export sends both feeds to the same endpoint. Raw sleep uses localized
    stage names; raw numeric samples may omit start/end but repeat a day.
    The original JSON remains in raw/ for timestamp-aligned analysis.
    """
    for metric in metrics:
        days = set()
        for row in metric.get('data') or []:
            if ('startDate' in row and 'endDate' in row) or ('start' in row and 'end' in row):
                return True
            stamp = row.get('date', '')
            day = stamp[:10]
            if day in days:
                return True
            days.add(day)
    return False


def merge(path):
    daily = load()
    with open(path) as f:
        payload = json.load(f).get('data', {})
    metrics = payload.get('metrics') or []
    if is_detailed_metrics(metrics):
        metrics = []  # Keep the separate daily-summary feed authoritative.
    for m in metrics:
        for row in m.get('data') or []:
            day = row['date'][:10]
            if m['name'] == 'sleep_analysis':
                val = {k: row[k] for k in ('sleepStart', 'sleepEnd', 'totalSleep', 'deep', 'rem', 'core', 'awake') if k in row}
            elif m['name'] == 'heart_rate':
                val = {k: row.get(k) for k in ('Min', 'Avg', 'Max')}
            else:
                val = row.get('qty')
            cur = daily.setdefault(day, {}).get(m['name'])
            if m['name'] in ADDITIVE and isinstance(cur, (int, float)) and isinstance(val, (int, float)):
                val = max(cur, val)     # a 'since last sync' push carries only the increment; never shrink a day total
            daily[day][m['name']] = val
    for w in payload.get('workouts') or []:
        e = w.get('activeEnergyBurned') or {}
        kcal = e.get('qty', 0) / (4.184 if e.get('units') == 'kJ' else 1)
        daily.setdefault(w['start'][:10], {}).setdefault('workouts', {})[w.get('id') or w['start']] = {
            'name': w.get('name'), 'start': w['start'][11:16], 'end': w['end'][11:16],
            'min': round(w.get('duration', 0) / 60), 'kcal': round(kcal), 'avg_hr': round((w.get('avgHeartRate') or {}).get('qty') or 0)}
    STORE.parent.mkdir(parents=True, exist_ok=True)
    with STORE.open('w') as f:
        json.dump(dict(sorted(daily.items())), f, ensure_ascii=False, indent=1)
    return daily


def avg7(daily, today, key, field=None):
    vals = []
    for k in range(1, 8):
        v = daily.get(str(today - timedelta(days=k)), {}).get(key)
        if isinstance(v, dict):
            v = v.get(field)
        if isinstance(v, (int, float)):
            vals.append(v)
    return sum(vals) / len(vals) if vals else None


def todays_weight(today):
    w = None
    for line in LOG.read_text().splitlines() if LOG.exists() else []:
        r = json.loads(line)
        if r.get('date') == str(today) and r.get('source') == 'scale-ble' and r.get('weight'):
            w = (r['time'], r['weight'], r.get('body'))
    return w


def log_rows():
    return [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()] if LOG.exists() else []


def morning_weights(rows, today, days=14):
    """{date: (weight, fat_kg)} — first reading before 10:00 each day (scale or voice), last `days` days"""
    out = {}
    for r in rows:
        if not r.get('weight') or (r.get('time') or '99') >= '10:00':
            continue
        d = date.fromisoformat(r['date'])
        if 0 <= (today - d).days < days and d not in out:
            out[d] = (r['weight'], (r.get('body') or {}).get('fat_kg'))
    return dict(sorted(out.items()))


def slope_per_week(points):
    """least-squares slope (kg/week) of [(date, value)]; None if too few points"""
    if len(points) < 4:
        return None
    xs = [(d - points[0][0]).days for d, _ in points]; ys = [v for _, v in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return None if den == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den * 7


def trend(rows, today, goal=WEIGHT_GOAL):
    mw = morning_weights(rows, today)
    w_pts = [(d, v[0]) for d, v in mw.items()]
    f_pts = [(d, v[1]) for d, v in mw.items() if v[1]]
    wk, fk = slope_per_week(w_pts), slope_per_week(f_pts)
    rate = fk if fk is not None else wk                  # fat mass trend when available, else weight
    t = {'points': w_pts, 'weight_wk': wk, 'fat_wk': fk, 'latest': w_pts[-1][1] if w_pts else None}
    if rate is None:
        t['status'], t['say'] = 'unknown', '数据还不够（需要至少 4 个早晨的体重）'
        return t
    t['deficit'] = round(-rate / 7 * 7700)              # kcal/day implied by the trend
    if rate <= -1.0:
        t['status'], t['say'] = 'fast', '降得偏快，注意别掉肌肉'
    elif rate <= -0.2:
        t['status'], t['say'] = 'on', '在轨道上'
    elif rate < 0.2:
        t['status'], t['say'] = 'slow', '几乎没动，热量缺口不够'
    else:
        t['status'], t['say'] = 'off', '在回升，这几天吃的比消耗多'
    if goal is not None and wk and wk < -0.05 and t['latest'] and t['latest'] > goal:
        t['eta'] = today + timedelta(weeks=(t['latest'] - goal) / -wk)
    return t


def training(rows, day):
    return [x for r in rows if r.get('date') == str(day) and r.get('source') != 'health-morning' for x in (r.get('done') or [])]


RULE_BAD = ('酒', '啤酒', '可乐', '奶茶', '果汁', '含糖', '甜饮', '雪碧', '芬达')


def food(rows, day):
    meals = [(r.get('time') or '', str(r['food'])) for r in rows if r.get('date') == str(day) and r.get('food')]
    bad = sorted({k for _, f in meals for k in RULE_BAD if k in f and '无糖' not in f})
    return meals, bad


def cpap_night(night):
    """Last night's CPAP summary from the ResMed STR.edf (row for the evening date), or None."""
    import math
    from cpap_legacy import read_edf
    path = FIT / '呼吸机' / 'STR.edf'
    try:
        edf = read_edf(path)
        if edf['duration'] != 86400:
            return None
        i = (night - edf['start'].date()).days
        if not 0 <= i < len(edf['records']):
            return None
        record = edf['records'][i]
        def one(key):
            value = record[key][0]
            if not math.isfinite(value) or value < 0:
                raise ValueError('STR 摘要缺失或无效')
            return value
        duration, events = one('Duration'), one('MaskEvents')
        ons, offs = record['MaskOn'], record['MaskOff']
        if not 0 < duration <= 1440 or not ons or len(ons) != len(offs):
            return None
        sessions = []
        for on, off in zip(ons, offs):
            if not math.isfinite(on) or not math.isfinite(off):
                return None
            if on < 0 and off < 0:
                continue                        # 未用槽位；单侧负值则是尚未封口
            if not 0 <= on < off <= 1440:
                return None
            if sessions and on < sessions[-1][1]:
                return None
            sessions.append((on, off))
        if not sessions or events != 2 * len(sessions) or len(sessions) >= len(ons):
            return None                         # 槽位满时不能排除遗漏会话
        def optional(row, key):
            values = row.get(key) or []
            value = values[0] if values else None
            return value if isinstance(value, (int, float)) and math.isfinite(value) and value > 0 else None
        prior_rates = [optional(r, 'RespRate.50') for r in edf['records'][max(0, i-7):i]
                       if optional(r, 'Duration') is not None]
        prior_rates = [v for v in prior_rates if v is not None]
        clock = lambda minute: (edf['start'] + timedelta(days=i, minutes=minute)).strftime('%H:%M')
        return {'rr50': optional(record, 'RespRate.50'),
                'rr_baseline': sum(prior_rates)/len(prior_rates) if prior_rates else None,
                'rr_baseline_n': len(prior_rates),
                'rr_low': min(prior_rates) if prior_rates else None,
                'rr_high': max(prior_rates) if prior_rates else None, 'min': duration, 'on': [clock(on) for on, _ in sessions],
                'off': [clock(off) for _, off in sessions],
                'ahi': one('AHI'), 'oai': one('OAI'), 'cai': one('CAI'),
                'leak50': one('Leak.50') * 60, 'leak95': one('Leak.95') * 60,
                'leakmax': one('Leak.Max') * 60, 'p50': one('MaskPress.50'),
                'p95': one('MaskPress.95'), 'hum': one('S.HumLevel'), 'smartstart': one('S.SmartStart')}
    except (OSError, ValueError, KeyError, IndexError, TypeError, OverflowError):
        return None                             # 坏文件/未封口不能阻止截止后的兜底日报


def cpap_indicators(cp):
    """治疗目标与个人参考范围分开；缺值不亮绿灯。"""
    import math
    def valid(v):
        return isinstance(v, (int, float)) and math.isfinite(v) and v >= 0
    cp = cp or {}
    result = []
    for key, title, unit, limit in [('ahi','治疗期 AHI','次/时',5),('leak95','漏气 P95','L/min',24)]:
        value = cp.get(key)
        good = valid(value) and (value < limit if key == 'ahi' else value <= limit)
        result.append({'title':title,'value':f'{value:.1f}' if valid(value) else '—','unit':unit,
                       'level':'good' if good else ('warn' if valid(value) else 'unknown'),
                       'status':('达标' if key=='ahi' else '未超警戒线') if good else ('需留意' if valid(value) else '暂无数据'),
                       'reference':f'目标 < {limit}' if key=='ahi' else '警戒线 24 L/min'})
    rr, lo, hi = cp.get('rr50'), cp.get('rr_low'), cp.get('rr_high')
    comparable = valid(rr) and rr > 0 and valid(lo) and valid(hi) and lo <= hi and cp.get('rr_baseline_n',0)>=5
    within = comparable and lo-1e-6 <= rr <= hi+1e-6
    result.insert(1, {'title':'呼吸频率 · 中位数','value':f'{rr:.1f}' if valid(rr) and rr > 0 else '—','unit':'次/分',
                     'level':'good' if within else ('warn' if comparable else 'unknown'),
                     'status':'个人范围内' if within else ('超出个人范围' if comparable else '基线不足'),
                     'reference':f'前 {cp["rr_baseline_n"]} 夜 {lo:.1f}–{hi:.1f}' if comparable else '至少需 5 夜有效记录'})
    return result


def cpap_brief(cp):
    """一项数据分析；呼吸频率按个人基线比较，不虚构瑞思迈正常阈值。"""
    if not cp:
        return {'metrics': '昨晚呼吸机数据未到。', 'assessment': '', 'action': ''}
    rr = cp.get('rr50')
    rate = f'呼吸频率中位数 {rr:.1f} 次/分' if rr is not None else '呼吸频率暂无数据'
    baseline = cp.get('rr_baseline')
    if rr is not None and baseline is not None:
        rate += f'（较前 {cp["rr_baseline_n"]} 夜均值 {rr-baseline:+.1f}）'
    metrics = f'{rate}；使用 {hm_(cp["min"]/60)}。'
    assessment = (f'治疗期 AHI {cp["ahi"]:.1f} 次/时'
                  + ('，低于瑞思迈治疗目标 5' if cp['ahi'] < 5 else '，未低于瑞思迈治疗目标 5')
                  + f'；漏气 95 百分位 {cp["leak95"]:.1f} L/min。')
    if cp['ahi'] >= 5:
        action = '若 AHI 持续不低于 5，请联系睡眠医生复核治疗。'
    elif cp['leak95'] >= 24:
        action = '今晚先检查面罩密合与管路连接；反复漏气请联系设备服务人员。'
    else:
        action = '今晚睡前先清醒戴罩适应气流、检查密合，尽量戴到醒来。'
    return {'metrics': metrics, 'assessment': assessment, 'action': action}


def leak_say(cp):
    """漏气说人话 → (短语, 'good'|'warn'|'bad')。

    「漏气 95% 73 L/min」是机器口径 —— 95 百分位、原文件单位还是 L/s。主人要判断的其实只有
    一件事：面罩要不要动一下。所以只给结论，数字留在 cpap_nights.csv 里备查。
    24 L/min 是 ResMed 的大漏气线（同口径，P95）。
    """
    p95 = cp['leak95']
    if p95 < 24:
        return '面罩密封好', 'good'
    if p95 < 45:
        return '面罩有漏气', 'warn'
    return '面罩漏气多，查下头带', 'bad'


def hm_(hours):
    """0.775 h → '46分', 1.976 h → '1小时59分' (Apple Health style)"""
    m = round(hours * 60)
    return f'{m // 60}小时{m % 60:02d}分' if m >= 60 else f'{m}分'


def build(daily, today):
    """→ (text, verdict, sleep, extra) — extra carries 练/吃/趋势 for the card"""
    rows = log_rows()
    yd = today - timedelta(days=1)
    d, y = daily.get(str(today), {}), daily.get(str(yd), {})
    s = d['sleep_analysis']
    tot, avg_tot = s.get('totalSleep', 0), avg7(daily, today, 'sleep_analysis', 'totalSleep')
    rhr, hrv = d.get('resting_heart_rate'), d.get('heart_rate_variability')
    a_rhr, a_hrv = avg7(daily, today, 'resting_heart_rate'), avg7(daily, today, 'heart_rate_variability')
    act_kcal = (y.get('active_energy') or 0) / 4.184
    bas_kcal = (y.get('basal_energy_burned') or 0) / 4.184
    workouts = list((y.get('workouts') or {}).values())
    train = training(rows, yd)
    meals, bad = food(rows, yd)
    tr = trend(rows, today)
    w = todays_weight(today)
    cp = cpap_night(yd)
    ex = {'act': act_kcal, 'bas': bas_kcal, 'total': act_kcal + bas_kcal, 'ex_min': y.get('apple_exercise_time'),
          'steps': y.get('step_count'), 'workouts': workouts, 'train': train, 'meals': meals, 'bad': bad, 'trend': tr, 'cpap': cp}
    ex['cpap_brief'] = cpap_brief(cp)
    # 时间交集只代表睡眠窗口覆盖，不把手表分期估计当作诊断。
    from sleep_analysis import analyze
    ex['sleep_comparison'] = analyze(daily, today, FIT / '呼吸机')
    from sleep_waveform import analyze_stop_windows
    ex['sleep_comparison']['stop_windows'] = analyze_stop_windows(
        FIT / '呼吸机', (ex['sleep_comparison'].get('latest') or {}).get('sessions', []))
    from sleep_stages import analyze_stages
    ex['stage_comparison'] = analyze_stages(FIT / '数据/健康/raw', FIT / '呼吸机', s,
        (ex['sleep_comparison'].get('latest') or {}).get('sessions', []))
    # one-line summary: 练 · 吃 · 趋势
    parts = []
    if ex['total']:
        parts.append(f"昨天消耗约 {ex['total']:.0f} 千卡" + (f"、锻炼 {ex['ex_min']:.0f} 分钟" if ex['ex_min'] else ''))
    parts.append(('饮食出现 ' + '、'.join(bad)) if bad else ('饮食守住了规则' if meals else '饮食没记录'))
    if tr['status'] != 'unknown':
        r = tr['fat_wk'] if tr['fat_wk'] is not None else tr['weight_wk']
        parts.append(f"近 2 周{'脂肪' if tr['fat_wk'] is not None else '体重'}每周 {r:+.1f} kg，{tr['say']}")
    summary = '；'.join(parts) + '。'
    ex['summary'] = summary
    flags = []
    if tot < 6: flags.append('睡得少')
    if rhr and a_rhr and rhr >= a_rhr + 4: flags.append('静息心率偏高')
    if hrv and a_hrv and hrv <= a_hrv * 0.8: flags.append('HRV 偏低')
    if flags:
        verdict = '、'.join(flags) + '，今天训练减量、早点睡'
    elif tr['status'] in ('slow', 'off'):
        verdict = '恢复正常；减脂偏慢，今天晚饭主食减半、多走 3000 步'
    else:
        verdict = '状态正常，按计划训练'
    lines = [f"📝 {summary}", '',
             f"🌙 睡：{s['sleepStart'][11:16]}–{s['sleepEnd'][11:16]}，{hm_(tot)}" + (f"（7 天均 {hm_(avg_tot)}）" if avg_tot else ''),
             f"   深睡 {hm_(s.get('deep', 0))} · 快速眼动 {hm_(s.get('rem', 0))} · 核心 {hm_(s.get('core', 0))} · 清醒 {hm_(s.get('awake', 0))}"]
    brief = ex['cpap_brief']
    lines.append('   呼吸机分析：' + brief['metrics'] + brief['assessment'] + brief['action'])
    if ex['stage_comparison'].get('summary'):
        lines.append('   分期对照：' + ex['stage_comparison']['summary'])
    if rhr: lines.append(f"   静息心率 {rhr:.0f}" + (f"（均 {a_rhr:.0f}）" if a_rhr else '') + (f"，HRV {hrv:.0f}" if hrv else ''))
    lines.append(f"🏋️ 练：总消耗 {ex['total']:.0f} 千卡（活动 {act_kcal:.0f} + 基础 {bas_kcal:.0f}）" + (f"，步数 {ex['steps']:.0f}" if ex['steps'] else ''))
    for wo in workouts: lines.append(f"   {wo['start']} {wo['name']} {wo['min']} 分钟 · {wo['kcal']} 千卡")
    if train: lines.append('   ' + '、'.join(train))
    lines.append('🍚 吃：' + ('；'.join(f"{t} {f[:30]}" for t, f in meals) if meals else '昨天没记录，吃饭时拍照发我'))
    if w: lines.append(f"⚖️ 今早 {w[1]:.2f} kg" + (f"，体脂 {w[2]['fat_pct']}%、脂肪 {w[2]['fat_kg']} kg、肌肉 {w[2]['muscle_kg']} kg" if w[2] else ''))
    if tr.get('eta'): lines.append(f"📈 按现在速度，约 {tr['eta']:%m-%d} 到 {WEIGHT_GOAL:g} kg")
    lines += ['', '👉 ' + verdict]
    return '\n'.join(lines), verdict, s, ex


def morning_report(today=None, force=False, wait_cpap=True, deadline=REPORT_DEADLINE):
    """Once per day, as soon as last night's sleep lands — and, by request, last night's CPAP too.

    wait_cpap: 昨夜呼吸机那行是主人要看的，所以卡数据没到就先压着不发（返回 False），
    等 watch.on_cpap 把 STR 和波形搬回来时会再调一次。过了 deadline 就不再等 —— 卡没插、
    树莓派没网也会照发，只少那一行；波形详析随后完整时允许补发一次日报。
    """
    with REPORT_LOCK:
        return _morning_report(today or date.today(), force, wait_cpap, deadline)


def _morning_report(today, force, wait_cpap, deadline):
    done = set(DONE.read_text().split()) if DONE.exists() else set()
    state_path = DONE.with_suffix('.json')
    try:
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    previous = state.get(str(today))
    updating = not force and (str(today) in done or previous is not None)
    if updating:
        # 旧标记无法判断详析是否已发，保守不补发；新标记只允许补齐一次。
        if not isinstance(previous, dict):
            return False
        if previous.get('cpap') is not False and previous.get('details_ready') is not False and previous.get('stages_ready') is not False:
            return False
    daily = load()
    if 'sleep_analysis' not in daily.get(str(today), {}):
        return False
    text, verdict, s, ex = build(daily, today)
    details_ready = bool(ex.get('cpap'))
    if 'sleep_comparison' in ex:
        comparison = ex.get('sleep_comparison') or {}
        windows = comparison.get('stop_windows')
        details_ready = details_ready and isinstance(windows, list) and bool(windows) and all(
            isinstance(window, dict) and window.get('details_ready', window.get('status') == 'complete') for window in windows)
    stages_ready = ex.get('stage_comparison', {}).get('ready', 'stage_comparison' not in ex)
    if updating and previous.get('details_ready') is True and previous.get('cpap') is True and not stages_ready:
        return False  # 分期明细迟到时只在完整对照就绪后补发一次。
    if updating and not details_ready:
        return False                            # 只为完整详析补发，不逐批追发未齐的文件
    if (not force and wait_cpap and today == date.today() and not (details_ready and stages_ready)
            and datetime.now().strftime('%H:%M') < deadline):
        return False
    caption = text.split('\n')[0][2:] + '\n👉 ' + verdict
    try:
        from health_card import render
        png, _ = render(daily, today, verdict, any(k in verdict for k in ('减量', '偏慢')), todays_weight(today), ex)
        sent = notify.send_image(png, caption=caption)
    except Exception as e:                       # the card is a nicety; never lose the report itself
        print('card failed:', e)
        sent = False
    channel = 'image'
    if not sent:
        channel = 'text'
        sent = notify.send(text)                # the text always carries the whole report
    if not sent:
        return False                            # 未送达不记已发，下次事件/定时检查可重试
    state[str(today)] = {'cpap': bool(ex.get('cpap')), 'details_ready': details_ready, 'stages_ready': stages_ready, 'channel': channel, 'sent_at': datetime.now().isoformat(timespec='seconds')}
    DONE.parent.mkdir(parents=True, exist_ok=True)
    temp = state_path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    temp.replace(state_path)
    if str(today) not in done:
        with open(DONE, 'a') as f:
            f.write(str(today) + '\n')
    entry = {'date': str(today), 'time': datetime.now().strftime('%H:%M'), 'source': 'health-morning', 'weight': None,
             'pain_hip': None, 'stiff_hip': None, 'done': [], 'food': None,
             'sleep': f"手表：{s['sleepStart'][11:16]}–{s['sleepEnd'][11:16]}，睡着 {s.get('totalSleep', 0):.1f}h，深睡 {s.get('deep', 0):.1f}，快速眼动 {s.get('rem', 0):.1f}，清醒 {s.get('awake', 0) * 60:.0f} 分",
             'note': '晨报：' + text.split('\n')[0][2:] + ' 结论：' + verdict, 'raw': None}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, 'a') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    return True


if __name__ == '__main__':
    # 手动：python3 health_daily.py merge <file> | report [YYYY-MM-DD]
    if sys.argv[1] == 'merge':
        merge(sys.argv[2])
    else:
        print(build(load(), date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date.today())[0])
