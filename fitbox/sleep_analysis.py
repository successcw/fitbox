"""睡眠汇总与 PAP 治疗窗口对照；不推断睡眠分期或停机原因。"""
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import math

try:
    from .cpap_legacy import read_edf
except ImportError:
    from cpap_legacy import read_edf

try:
    from .config import TIMEZONE
except ImportError:
    from config import TIMEZONE
LOCAL = ZoneInfo(TIMEZONE)


def _number(value):
    return isinstance(value, (int, float)) and math.isfinite(value) and value >= 0


def _first(record, field):
    values = record.get(field) or []
    return values[0] if values else -1


def _time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = datetime.strptime(value, '%Y-%m-%d %H:%M:%S %z')
    return parsed.astimezone(LOCAL).replace(tzinfo=None) if parsed.tzinfo else parsed


def _union(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _minutes(start, end):
    return (end - start).total_seconds() / 60


def analyze(daily, today, cpap_root: Path, days=14):
    """today 是睡醒日；EDF 时钟按本地时间解释，未知数据保持 None。"""
    today = date.fromisoformat(today) if isinstance(today, str) else today
    if isinstance(today, datetime):
        today = today.date()
    rows, error = {}, None
    try:
        edf = read_edf(Path(cpap_root) / 'STR.edf')
        if edf['duration'] != 86400:
            raise ValueError('STR 不是每日记录')
        for i, record in enumerate(edf['records']):
            anchor = edf['start'] + timedelta(days=i)
            rows[(anchor.date() + timedelta(days=1)).isoformat()] = (anchor, record)
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        error = 'STR 缺失或无法校验：' + type(exc).__name__
    nights = []
    for offset in range(days - 1, -1, -1):
        key = (today - timedelta(days=offset)).isoformat()
        sleep = daily.get(key, {}).get('sleep_analysis') or {}
        item = dict(date=key, watch_start=None, watch_end=None, sleep_min=None,
                    deep_min=None, deep_pct=None, rem_min=None, awake_min=None,
                    sessions=[], watch_window_min=None, overlapping_pap_min=None,
                    window_coverage_pct=None, uncovered_window_min=None,
                    tail_uncovered_min=None, ahi=None, gaps=[], pap_status='unknown', issues=[])
        for source, dest in [('totalSleep', 'sleep_min'), ('deep', 'deep_min'),
                             ('rem', 'rem_min'), ('awake', 'awake_min')]:
            if _number(sleep.get(source)):
                item[dest] = sleep[source] * 60
        if item['sleep_min'] and item['deep_min'] is not None:
            item['deep_pct'] = item['deep_min'] / item['sleep_min'] * 100
        start = end = None
        try:
            start, end = _time(sleep.get('sleepStart')), _time(sleep.get('sleepEnd'))
            if start and end and end > start:
                item.update(watch_start=start.isoformat(), watch_end=end.isoformat(),
                            watch_window_min=_minutes(start, end))
            else:
                start = end = None
                item['issues'].append('手表睡眠窗口缺失或无效')
        except (ValueError, TypeError):
            start = end = None
            item['issues'].append('手表时间无法解析')
        row = rows.get(key)
        intervals = []
        if row:
            anchor, record = row
            duration = _first(record, 'Duration')
            events = _first(record, 'MaskEvents')
            if _number(duration) and duration > 0 and _number(events) and events > 0:
                item['pap_status'] = 'available'
                ahi = _first(record, 'AHI')
                item['ahi'] = ahi if _number(ahi) else None
                ons, offs = record.get('MaskOn', []), record.get('MaskOff', [])
                for index, minute in enumerate(ons):
                    if not _number(minute) or minute > 1440:
                        continue
                    on = anchor + timedelta(minutes=minute)
                    off_min = offs[index] if index < len(offs) else -1
                    off = anchor + timedelta(minutes=off_min) if _number(off_min) and off_min <= 1440 else None
                    item['sessions'].append({'start': on.isoformat(), 'end': off.isoformat() if off else None})
                    if off and off > on:
                        intervals.append((on, off))
                    else:
                        item['pap_status'] = 'uncertain'
                if events != 2 * len(item['sessions']) or events >= 2 * len(ons) or not intervals:
                    item['pap_status'] = 'uncertain'
                if item['pap_status'] == 'uncertain':
                    item['issues'].append('治疗会话未封口、数量不符或槽位已满，覆盖率未知')
            else:
                item['issues'].append('STR 无已确认的有效治疗记录，不能当作零治疗')
        else:
            item['issues'].append(error or 'STR 无对应治疗日')
        if start and end and item['pap_status'] == 'available':
            clipped = _union([(max(a, start), min(b, end)) for a, b in intervals if min(b, end) > max(a, start)])
            overlap = sum(_minutes(a, b) for a, b in clipped)
            cursor = start
            for a, b in clipped:
                if a > cursor:
                    item['gaps'].append(dict(start=cursor.isoformat(), end=a.isoformat(), minutes=_minutes(cursor, a)))
                cursor = b
            if cursor < end:
                item['gaps'].append(dict(start=cursor.isoformat(), end=end.isoformat(), minutes=_minutes(cursor, end)))
            last_off = max(b for a, b in intervals)
            item.update(overlapping_pap_min=overlap,
                        window_coverage_pct=overlap / item['watch_window_min'] * 100,
                        uncovered_window_min=max(0, item['watch_window_min'] - overlap),
                        tail_uncovered_min=max(0, _minutes(max(start, last_off), end)))
        nights.append(item)
    averages = {}
    for length in (7, 14):
        metrics = {}
        for field in ('sleep_min', 'deep_min', 'deep_pct', 'window_coverage_pct', 'tail_uncovered_min', 'ahi'):
            values = [n[field] for n in nights[-length:] if n[field] is not None]
            metrics[field] = dict(mean=sum(values) / len(values) if values else None, n=len(values))
        averages[str(length) + 'd'] = metrics
    latest = nights[-1] if nights else None
    observations = []
    if latest and latest['tail_uncovered_min'] is not None:
        observations.append(f"本次末次停机到手表睡眠窗口结束相隔 {latest['tail_uncovered_min']:.1f} 分钟；这不是精确的无治疗睡眠时长。")
    long_tails = [n for n in nights if n['tail_uncovered_min'] is not None and n['tail_uncovered_min'] >= 60]
    if long_tails:
        observations.append(f"可比较日期中有 {len(long_tails)} 夜，末次停机比手表睡眠窗口结束早至少 1 小时。")
    deep = averages['14d']['deep_min']
    if deep['n']:
        observations.append(f"最近窗口内 {deep['n']} 夜的手表深睡估计平均 {deep['mean']:.0f} 分钟；不据此诊断深睡不足。")
    return dict(nights=nights, latest=latest, averages=averages, observations=observations,
                limitations=['本模块只计算日汇总与会话窗口；逐段分期对照另见 stage_comparison，不能用汇总占比拼造时间轴。',
                             '覆盖率是手表睡眠起止窗口与 PAP 会话的交集，包含可能清醒的时间，不是实际睡眠覆盖率。',
                             'AHI 仅反映设备治疗期间，摘机后的事件未知。',
                             f'手表转换为 {TIMEZONE} 本地时间；EDF 无时区，假定设备时钟同为本地且未独立校准。',
                             '停机时间来自 STR，精度约 1 分钟；不能仅凭时间对照确定摘机、漏气、梦境或焦虑的原因。'])
