"""手表分期与 PAP 实测值的时间对齐；不从呼吸波形推断睡眠阶段。"""
import json
import math
from datetime import timedelta
from pathlib import Path
from html import escape
from sleep_analysis import _time, _union
from sleep_waveform import _load, _day

STAGES = {'Deep': 'deep', '深度': 'deep', '深睡': 'deep',
          'REM': 'rem', '快速动眼期': 'rem', '快速眼动': 'rem',
          'Core': 'core', '核心': 'core', 'Awake': 'awake', '清醒': 'awake'}
LABELS = {'deep': '深睡', 'rem': '快速眼动', 'core': '核心', 'awake': '清醒'}
COLORS = {'deep': '#b48cf2', 'rem': '#4cc9f0', 'core': '#507be5', 'awake': '#f59e42'}


def load_segments(raw_root, sleep):
    left, right = _time(sleep.get('sleepStart')), _time(sleep.get('sleepEnd'))
    if not left or not right or right <= left:
        return [], ['缺少整晚起止时间']
    grouped, issues = {}, []
    for path in sorted(Path(raw_root).glob('*.json')):
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for metric in payload.get('data', {}).get('metrics', []):
            if metric.get('name') != 'sleep_analysis':
                continue
            for row in metric.get('data') or []:
                stage = STAGES.get(row.get('value'))
                if not stage:
                    continue
                try:
                    a = _time(row.get('startDate') or row.get('start'))
                    b = _time(row.get('endDate') or row.get('end'))
                except (ValueError, TypeError):
                    continue
                if not a or not b or b <= a or b <= left or a >= right:
                    continue
                grouped.setdefault(stage, []).append((max(a,left), min(b,right)))
    segments = sorted((a,b,k) for k,v in grouped.items() for a,b in _union(v))
    for i,(a,b,k) in enumerate(segments):
        if any(c < b and d > a and other != k for c,d,other in segments[i+1:]):
            return [], ['手表分期存在重叠冲突，暂不统计']
    return segments, issues


def weighted_stats(points):
    # 重复 EDF 或重叠文件只计一次；不以采样条数代替时间权重。
    clean, until = [], None
    for a,b,v in sorted(set(points)):
        start = max(a,until) if until else a
        if b > start:
            clean.append((v,(b-start).total_seconds()))
            until = b
    total = sum(w for _,w in clean)
    def quantile(q):
        accum = 0
        for v,w in sorted(clean):
            accum += w
            if accum >= total*q:
                return round(v,2)
    return {'seconds': total, 'median': quantile(.5), 'p95': quantile(.95)}


def analyze_stages(raw_root, cpap_root, sleep, sessions):
    segments, issues = load_segments(raw_root,sleep)
    result = {'ready': False, 'rows': [], 'segments': [], 'sessions': sessions, 'issues': issues}
    if not segments:
        return result
    result['segments'] = [{'start':a.isoformat(),'end':b.isoformat(),'stage':k} for a,b,k in segments]
    windows = _union([(_time(s['start']),_time(s['end'])) for s in sessions])
    intervals = {k:[] for k in LABELS}
    for a,b,k in segments:
        for c,d in windows:
            if min(b,d)>max(a,c): intervals[k].append((max(a,c),min(b,d)))
    points = {k:{'rr':[],'leak':[]} for k in LABELS}
    days = {_day(a) for a,b,_ in segments}
    for day in sorted(days):
        for path in sorted((Path(cpap_root)/'DATALOG'/day).glob('*')):
            if not path.name.upper().endswith('_PLD.EDF'): continue
            try:
                edf,units = _load(path)
                if _day(edf['start']) != day or edf['duration'] <= 0:
                    raise ValueError('EDF 时间不属于治疗日')
            except (ValueError,OSError,UnicodeError,OverflowError) as exc:
                issues.append(f'{path.name}: {exc}')
                continue
            for channel,label in [('rr','RespRate.2s'),('leak','Leak.2s')]:
                unit=units.get(label)
                factor = (1 if unit in ('bpm','1/min') else None) if channel=='rr' else {'L/s':60,'L/min':1,'l/s':60,'l/min':1}.get(unit)
                if factor is None:
                    issues.append(f'{label} 单位缺失或不支持'); continue
                for i,record in enumerate(edf['records']):
                    values=record.get(label) or []
                    if not values: continue
                    step=edf['duration']/len(values)
                    base=edf['start']+timedelta(seconds=i*edf['duration'])
                    for j,v in enumerate(values):
                        v*=factor
                        if not math.isfinite(v) or v<0 or (channel=='rr' and v==0): continue
                        a=base+timedelta(seconds=j*step); b=a+timedelta(seconds=step)
                        for k,ranges in intervals.items():
                            for c,d in ranges:
                                if min(b,d)>max(a,c): points[k][channel].append((max(a,c),min(b,d),v))
    for k in LABELS:
        duration=sum((b-a).total_seconds() for a,b,stage in segments if stage==k)
        if not duration: continue
        rr,leak = weighted_stats(points[k]['rr']),weighted_stats(points[k]['leak'])
        rr_intervals = _union([(a,b) for a,b,_ in points[k]['rr']])
        leak_intervals = _union([(a,b) for a,b,_ in points[k]['leak']])
        covered = sum(max(0, (min(b,d)-max(a,c)).total_seconds())
                      for a,b in rr_intervals for c,d in leak_intervals)
        result['rows'].append({'stage':k,'label':LABELS[k],'minutes':duration/60,
                               'matched_minutes':covered/60,'coverage':covered/duration,
                               'rr':rr['median'],'leak':leak['p95']})
    total=sum((b-a).total_seconds() for a,b,_ in segments)
    expected=(_time(sleep['sleepEnd'])-_time(sleep['sleepStart'])).total_seconds()
    result['stage_coverage']=total/expected
    result['ready']=not issues and result['stage_coverage']>=.95 and all(
        r['coverage']>=.8 for r in result['rows'])
    usable={r['stage']:r for r in result['rows'] if r['coverage']>=.8 and r['matched_minutes']>=5 and r['rr'] is not None}
    result['summary']=''
    if 'deep' in usable and 'rem' in usable:
        deep,rem=usable['deep']['rr'],usable['rem']['rr']
        delta=deep-rem
        result['summary']=f'深睡呼吸频率 {deep:.1f}，快速眼动 {rem:.1f} 次/分；本晚深睡'+(f'低 {abs(delta):.1f}' if delta<0 else f'高 {delta:.1f}' if delta>0 else '相同')+(' 次/分。' if delta else '。')
    if all(k in usable and usable[k]['leak'] is not None for k in ('deep','rem','core')):
        d,r,c = (usable[k]['leak'] for k in ('deep','rem','core'))
        result['summary'] += f' 漏气 P95：深睡 {d:.1f}、快速眼动 {r:.1f}、核心 {c:.1f} L/min。'
    return result


def render_stages(result):
    if not result or not result.get('segments'):
        return '<div class="note">睡眠分期明细未齐，暂无法按阶段对照。</div>'
    seg=result['segments']; left=min(_time(x['start']) for x in seg); right=max(_time(x['end']) for x in seg)
    span=(right-left).total_seconds()
    def x(t):return 72+max(0,min(1,(_time(t)-left).total_seconds()/span))*600
    bars=[]
    for s in seg:
        a,b=x(s['start']),x(s['end'])
        bars.append(f'<rect x="{a:.2f}" y="8" width="{b-a:.2f}" height="16" fill="{COLORS[s["stage"]]}"/>')
    for s in result['sessions']:
        a,b=x(s['start']),x(s['end'])
        if b>a:bars.append(f'<rect x="{a:.2f}" y="32" width="{b-a:.2f}" height="10" fill="#4ade80"/>')
    chart='<svg viewBox="0 0 680 64" role="img" aria-label="手表睡眠分期与呼吸机使用时间对照" style="width:100%;margin-top:12px">'+''.join(bars)+f'<g fill="#8aa0b8" font-size="11"><text x="0" y="20">手表分期</text><text x="0" y="42">呼吸机</text><text x="72" y="60">{left:%H:%M}</text><text x="672" y="60" text-anchor="end">{right:%H:%M}</text></g></svg>'
    rows=''
    for r in result['rows']:
        usable=r['coverage']>=.8 and r['matched_minutes']>=5
        rr=f'{r["rr"]:.1f}' if usable and r['rr'] is not None else '—'
        leak=f'{r["leak"]:.1f}' if usable and r['leak'] is not None else '—'
        duration_text = f'{r["matched_minutes"]:.2f} / {r["minutes"]:.2f}' if r['minutes'] < 5 else f'{r["matched_minutes"]:.0f} / {r["minutes"]:.0f}'
        rows+=f'<tr><td style="color:{COLORS[r["stage"]]}">{r["label"]}</td><td>{duration_text} 分</td><td>{rr}</td><td>{leak}</td></tr>'
    return chart+'<table style="width:100%;font-size:12px;text-align:left;border-spacing:0 7px"><thead style="color:#8aa0b8"><tr><th>手表阶段</th><th>匹配 / 阶段时长</th><th>呼吸频率中位数</th><th>漏气 P95</th></tr></thead><tbody>'+rows+'</tbody></table>'+f'<div style="font-size:13px">{escape(result.get("summary", ""))}</div><div class="note">手表估计分期；仅统计与呼吸机重叠时段。单位：次/分、L/min。匹配不足 80% 或不足 5 分钟显示 —；单晚差异不代表规律。</div>'


def backfill_sleep_summaries(raw_root, daily):
    """仅为缺少官方汇总的夜间生成已同步分段汇总；重复推送不重复累计。

    使用本地中午分界归到睡醒日；不把未知阶段或冲突区间当作睡眠。
    这是已同步样本的汇总，不保证导出窗口之外没有其他睡眠。
    """
    nights = {}
    for path in sorted(Path(raw_root).glob('*.json')):
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for metric in payload.get('data', {}).get('metrics', []):
            if metric.get('name') != 'sleep_analysis':
                continue
            for row in metric.get('data') or []:
                stage = STAGES.get(row.get('value'))
                if not stage:
                    continue
                try:
                    a = _time(row.get('startDate') or row.get('start'))
                    b = _time(row.get('endDate') or row.get('end'))
                except (ValueError, TypeError):
                    continue
                if not a or not b or b <= a:
                    continue
                day = (a + timedelta(hours=12)).date().isoformat()
                if (b - timedelta(microseconds=1) + timedelta(hours=12)).date().isoformat() != day:
                    continue  # 跨越治疗日中午边界的异常/超长记录不自动归夜。
                nights.setdefault(day, {}).setdefault(stage, []).append((a,b))
    for day, groups in nights.items():
        current = daily.get(day, {}).get('sleep_analysis') or {}
        if current.get('sleepStart') and current.get('totalSleep') is not None and current.get('origin') != 'segments':
            continue
        segments = sorted((a,b,k) for k,v in groups.items() for a,b in _union(v))
        if any(c < b and d > a and other != k for i,(a,b,k) in enumerate(segments)
               for c,d,other in segments[i+1:]):
            continue
        asleep = [(a,b) for a,b,k in segments if k != 'awake']
        if not asleep:
            continue
        totals = {k:sum((b-a).total_seconds()/3600 for a,b,stage in segments if stage == k) for k in LABELS}
        summary = {**totals, 'totalSleep':sum(totals[k] for k in ('deep','rem','core')),
                   'sleepStart':min(a for a,b in asleep).isoformat(),
                   'sleepEnd':max(b for a,b in asleep).isoformat(),
                   'origin':'segments', 'scope':'已同步分段，未假定导出窗口外没有睡眠'}
        daily.setdefault(day, {})['sleep_analysis'] = summary
    return daily
