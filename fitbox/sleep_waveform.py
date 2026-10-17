"""停机前 PLD/EVE 观察窗口；不从波形推断觉醒、摘机原因或治疗建议。"""
import math
from datetime import datetime, timedelta
from pathlib import Path

try:
    from .cpap_legacy import read_edf
except ImportError:
    from cpap_legacy import read_edf

EVENTS = {'Obstructive Apnea': 'OA', 'Central Apnea': 'CA',
          'Apnea': 'UA', 'Hypopnea': 'H', 'RERA': 'RERA'}
CHANNELS = {'leak': 'Leak.2s', 'pressure': 'Press.2s', 'flow_limit': 'FlowLim.2s'}


def _day(t):
    return (t - timedelta(hours=12)).strftime('%Y%m%d')


def _load(path):
    # 先执行完整长度及 TAL 校验，再使用物理量记录；额外拒绝非法比例系数。
    edf = read_edf(path)
    raw = path.read_bytes()
    n = int(raw[252:256])
    sh = raw[256:256*(n+1)]
    def fields(offset, width):
        return [sh[offset*n+i*width:offset*n+(i+1)*width].decode('ascii').strip()
                for i in range(n)]
    labels, units = fields(0, 16), fields(96, 8)
    if len(labels) != len(set(labels)):
        raise ValueError('重复信号标签')
    for i, label in enumerate(labels):
        if label == 'EDF Annotations':
            continue
        pmin, pmax, dmin, dmax = [float(fields(o, 8)[i]) for o in (104,112,120,128)]
        if not all(math.isfinite(x) for x in (pmin,pmax,dmin,dmax)) or dmax <= dmin or pmax <= pmin:
            raise ValueError('无效物理量比例系数')
    if not math.isfinite(edf['duration']):
        raise ValueError('无效记录时长')
    return edf, dict(zip(labels, units))


def _factor(channel, unit):
    if channel == 'leak':
        return {'L/s': 60., 'l/s': 60., 'L/min': 1., 'l/min': 1.}.get(unit)
    if channel == 'pressure':
        return 1. if unit == 'cmH2O' else None
    return 1. if unit in ('', '1') else None


def _summary(points, seconds, unit, window_start, window_end):
    if not points:
        return {'status': 'unknown', 'unit': unit, 'p50': None, 'p95': None,
                'samples': 0, 'coverage_seconds': 0., 'coverage_fraction': 0.,
                'boundary_ready': False}
    # 时间加权分位数；重叠文件/采样冲突时保守丢弃重叠部分，不重复累计。
    ordered = sorted(points)
    clean, until = [], None
    for start, end, value in ordered:
        left = max(start, until) if until is not None else start
        if end > left:
            clean.append((left, end, value))
            until = end
    total = sum((b-a).total_seconds() for a,b,_ in clean)
    # STR 会话边界仅精确到分钟；只容忍两端各一分钟，内部缺口仍不就绪。
    boundary_ready = ((clean[0][0]-window_start).total_seconds() <= 60
                      and (window_end-clean[-1][1]).total_seconds() <= 60
                      and all(a <= previous_end for (_, previous_end, _), (a, _, _)
                              in zip(clean, clean[1:])))
    values = sorted((v,(b-a).total_seconds()) for a,b,v in clean)
    def quantile(q):
        acc = 0.
        for value, weight in values:
            acc += weight
            if acc >= q*total:
                return round(value, 4)
    return {'status': 'complete' if total >= seconds-0.01 else 'partial',
            'unit': unit, 'p50': quantile(.5), 'p95': quantile(.95),
            'first': round(clean[0][2],4), 'last': round(clean[-1][2],4),
            'change': round(clean[-1][2]-clean[0][2],4), 'samples': len(clean),
            'coverage_seconds': round(total,3), 'coverage_fraction': round(total/seconds,4),
            'boundary_ready': boundary_ready}


def analyze_stop_windows(cpap_root, sessions):
    """sessions 的 start/end 是同一当地时区的 ISO 时间；EDF 使用当地墙钟。

    事件数只代表有效 EVE 中观察到的事件起点，不证明整个窗口无遗漏。
    """
    output, cache = [], {}
    for session in sessions:
        item = {'session_start': session.get('start'), 'session_end': session.get('end'),
                'issues': [], 'events': {'status': 'unknown', 'counts': None},
                'details_ready': False}
        output.append(item)
        try:
            start = datetime.fromisoformat(session['start'])
            end = datetime.fromisoformat(session['end'])
            if start.utcoffset() != end.utcoffset():
                raise ValueError('会话时区不一致')
            start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
            if end <= start:
                raise ValueError('无效会话范围')
        except (ValueError, KeyError, TypeError) as exc:
            item['issues'].append(str(exc))
            item['status'] = 'unknown'
            continue
        left = max(start, end-timedelta(minutes=10))
        seconds = (end-left).total_seconds()
        item.update(window_start=left.isoformat(), window_end=end.isoformat(), window_seconds=seconds)
        points = {k: [] for k in CHANNELS}
        day = _day(start)
        folder = Path(cpap_root)/'DATALOG'/day
        paths = sorted(p for p in folder.glob('*') if p.name.upper().endswith(('_PLD.EDF','_EVE.EDF')))
        if not paths:
            item['issues'].append('同治疗日缺少 PLD/EVE 文件')
        events, valid_eve = set(), False
        for path in paths:
            if path not in cache:
                try:
                    cache[path] = _load(path)
                except (ValueError, OSError, UnicodeError, OverflowError) as exc:
                    cache[path] = str(exc)
            loaded = cache[path]
            if isinstance(loaded, str):
                item['issues'].append(f'{path.name}: {loaded}')
                continue
            edf, units = loaded
            if _day(edf['start']) != day:
                item['issues'].append(f'{path.name}: EDF 头部不属于本治疗日，已排除')
                continue
            if path.name.upper().endswith('_EVE.EDF'):
                if edf['start'] > end:
                    continue
                if not edf['records'] or not any(a['label'] == 'Recording starts' and a['onset'] == 0
                                                 for a in edf['annotations']):
                    item['issues'].append(f'{path.name}: EVE 缺少记录起点')
                    continue
                valid_eve = True
                for event in edf['annotations']:
                    t = edf['start']+timedelta(seconds=event['onset'])
                    if event['onset'] < 0 or _day(t) != day:
                        item['issues'].append(f'{path.name}: 事件时间超出治疗日')
                        continue
                    if left <= t < end and event['label'] in EVENTS:
                        events.add((t, event['label'], event['duration']))
                continue
            duration = edf['duration']
            file_end = edf['start']+timedelta(seconds=duration*len(edf['records']))
            if duration <= 0 or file_end <= left or edf['start'] >= end:
                continue
            for channel, label in CHANNELS.items():
                factor = _factor(channel, units.get(label))
                if factor is None:
                    item['issues'].append(f'{path.name}: {label} 缺失或单位不支持')
                    continue
                for index, record in enumerate(edf['records']):
                    values = record.get(label, [])
                    if not values:
                        continue
                    step = duration/len(values)
                    base = edf['start']+timedelta(seconds=index*duration)
                    if base >= end or base+timedelta(seconds=duration) <= left:
                        continue
                    for j, value in enumerate(values):
                        a = base+timedelta(seconds=j*step)
                        b = a+timedelta(seconds=step)
                        value *= factor
                        if (b > left and a < end and math.isfinite(value) and value >= 0
                                and (channel != 'flow_limit' or value <= 1)):
                            points[channel].append((max(a,left),min(b,end),value))
        integrity_ok = not item['issues']
        for channel, unit in [('leak','L/min'),('pressure','cmH2O'),('flow_limit','1')]:
            item[channel] = _summary(points[channel], seconds, unit, left, end)
            if item[channel]['status'] != 'complete':
                item['issues'].append(f'{channel}: 窗口数据覆盖不足')
        if valid_eve:
            counts = {v: 0 for v in EVENTS.values()}
            for _, label, _ in events:
                counts[EVENTS[label]] += 1
            item['events'] = {'status': 'observed', 'counts': counts,
                              'coverage': 'unknown', 'basis': '窗口内事件起点；不证明无遗漏'}
        else:
            item['issues'].append('缺少时间匹配的有效 EVE')
        item['details_ready'] = (integrity_ok and valid_eve
                                 and all(item[channel]['boundary_ready'] for channel in CHANNELS))
        item['status'] = 'complete' if not item['issues'] else ('partial' if any(points.values()) else 'unknown')
    return output
