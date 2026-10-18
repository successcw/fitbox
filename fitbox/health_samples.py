"""从留存的 Auto Export 明细重建日统计；重传不累加，缺口不推算。"""
import json
import math
from pathlib import Path
from sleep_analysis import _time

ADDITIVE = {'step_count', 'active_energy', 'basal_energy_burned', 'apple_exercise_time',
            'apple_stand_time', 'apple_stand_hour', 'flights_climbed',
            'walking_running_distance', 'time_in_daylight'}
UNITS = {'heart_rate_variability': {'ms': 1}, 'resting_heart_rate': {'count/min': 1, 'bpm': 1},
         'heart_rate': {'count/min': 1, 'bpm': 1}, 'respiratory_rate': {'count/min': 1},
         'blood_oxygen_saturation': {'%': 1}, 'physical_effort': {'kcal/hr·kg': 1},
         'environmental_audio_exposure': {'dBASPL': 1}}
for _name in ADDITIVE:
    UNITS[_name] = ({'kJ': 1, 'kcal': 4.184} if 'energy' in _name else
                    {'km': 1, 'm': .001} if _name == 'walking_running_distance' else
                    {'min': 1, 's': 1/60, 'hr': 60} if _name in {'apple_exercise_time', 'apple_stand_time', 'time_in_daylight'} else
                    {'count': 1})


def is_raw_metric(metric):
    days = set()
    for row in metric.get('data') or []:
        if row.get('start') or row.get('startDate'):
            return True
        stamp = row.get('date', '')
        day = stamp[:10]
        if day in days:
            return True
        days.add(day)
        t = _time(stamp)
        if t and (t.hour or t.minute or t.second or t.microsecond):
            return True
    return False


def normalize_value(name, units, value):
    if name in UNITS:
        factor = UNITS[name].get(units)
        if factor is None:
            return None
        if isinstance(value, dict):
            return {k: v * factor if isinstance(v, (float, int)) else v for k, v in value.items()}
        return value * factor if isinstance(value, (float, int)) else None
    return value


def backfill_sample_metrics(raw_root, daily, extra_paths=()):
    groups = {}
    paths = sorted(set(Path(raw_root).glob('*.json')) | {Path(p) for p in extra_paths})
    for path in paths:
        try:
            payload = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        metrics = payload.get('data', {}).get('metrics', [])
        raw_feed = any(is_raw_metric(m) for m in metrics)
        for metric in metrics:
            name = metric.get('name')
            if name not in UNITS or not (is_raw_metric(metric) or raw_feed):
                continue
            for row in metric.get('data') or []:
                try:
                    a = _time(row.get('start') or row.get('date'))
                    b = _time(row.get('end') or row.get('start') or row.get('date'))
                    value = normalize_value(name, metric.get('units'), row.get('Avg') if name == 'heart_rate' else row.get('qty'))
                    if value is None or not math.isfinite(value) or value < 0 or not a or not b or b < a:
                        continue
                except (ValueError, TypeError):
                    continue
                # 跨日累计区间不能仅凭总量分配；保留原始记录，等待可按日归属的导出。
                if name in ADDITIVE and a.date() != b.date():
                    continue
                source = row.get('source') or 'unknown'
                groups.setdefault((a.date().isoformat(), name), {}).setdefault(source, {})[(a, b)] = (value, row)
    for (day, name), sources in groups.items():
        target = daily.setdefault(day, {})
        metadata = target.get('_sample_metrics', {})
        if target.get(name) is not None and name not in metadata:
            continue  # 保留历史官方日汇总，不把部分明细冒充全天。
        if len(sources) != 1:
            target.pop(name, None)
            target.setdefault('_sample_metrics', {})[name] = {'origin': 'samples', 'status': 'multiple_sources'}
            continue
        points = next(iter(sources.values()))
        values = [v for v, row in points.values()]
        aggregation = 'sample_mean'
        if name in ADDITIVE:
            value, aggregation = math.fsum(values), 'observed_sum'
        elif name == 'resting_heart_rate':
            value, aggregation = points[max(points, key=lambda x:(x[1],x[0]))][0], 'latest_measurement'
        elif name == 'heart_rate':
            value = {'Avg': math.fsum(values)/len(values),
                     'Min': min(row.get('Min', v) for v, row in points.values()),
                     'Max': max(row.get('Max', v) for v, row in points.values())}
        else:
            value = math.fsum(values)/len(values)
        target[name] = value
        target.setdefault('_sample_metrics', {})[name] = {
            'origin': 'samples', 'count': len(points), 'aggregation': aggregation,
            'first': min(a for a,b in points).isoformat(), 'last': max(b for a,b in points).isoformat(),
            'scope': '仅已同步记录，不能证明全天完整'}
    return daily
