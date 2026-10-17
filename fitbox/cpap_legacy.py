"""旧 ezShare 固件的已知文件下载和有界文件名探测（仅标准库）。

没有目录列表，候选窗口完成不等于完整镜像；本模块不写全量同步成功标记。
"""
import errno
import hashlib
import json
import math
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

KINDS = ('PLD', 'BRP', 'SAD', 'CSL', 'EVE')
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
WELCOME_REDIRECT = (b'<html><head><title>Success</title></head><body><form name="REDIRECTFORM" '
                    b'method="get" action=http://ezshare.card/publicdir/welcome.htm></form>'
                    b"<script language='javascript'>REDIRECTFORM.submit();</script></body></html>")


def read_edf(path):
    """严格校验长度后读取 EDF；保留各记录信号数组，避免丢失日期边界。"""
    raw = Path(path).read_bytes()
    if len(raw) < 256 or raw[:8].strip() != b'0':
        raise ValueError('不是 EDF 文件')
    h = lambda a, b: raw[a:b].decode('ascii').strip()
    header, count, ns = int(h(184, 192)), int(h(236, 244)), int(h(252, 256))
    duration = float(h(244, 252))
    if ns <= 0 or header != 256 * (ns + 1) or count < 0 or duration < 0:
        raise ValueError('EDF 头部或记录数无效/尚未封口')
    sh = raw[256:header]
    def field(offset, width, cast=str):
        return [cast(sh[offset + i*width:offset + (i+1)*width].decode('ascii').strip()) for i in range(ns)]
    labels = field(0, 16)
    pmin, pmax = field(ns*104, 8, float), field(ns*112, 8, float)
    dmin, dmax = field(ns*120, 8, float), field(ns*128, 8, float)
    samples = field(ns*216, 8, int)
    recsize = sum(samples)*2
    if any(n < 0 for n in samples) or recsize <= 0 or len(raw) != header + count*recsize:
        raise ValueError('EDF 数据长度与头部不符')
    start = datetime.strptime(h(168, 176) + ' ' + h(176, 184), '%d.%m.%y %H.%M.%S')
    # EDF 的世纪分界为 1985，而不是 datetime 默认的 1969。
    if 1969 <= start.year < 1985:
        start = start.replace(year=start.year + 100)
    records, annotations = [], []
    for r in range(count):
        p, record = header + r*recsize, {}
        for i, label in enumerate(labels):
            n = samples[i]
            vals = struct.unpack_from('<%dh' % n, raw, p)
            p += n*2
            if label == 'EDF Annotations':
                for tal in raw[p-n*2:p].split(b'\0'):
                    if not tal:
                        continue
                    parts = tal.split(b'\x14')
                    if len(parts) < 2:
                        raise ValueError('EDF 注释 TAL 格式不完整')
                    timing = parts[0].split(b'\x15')
                    onset = float(timing[0])
                    length = float(timing[1]) if len(timing) > 1 else 0
                    if not math.isfinite(onset) or not math.isfinite(length) or length < 0:
                        raise ValueError('EDF 注释时间无效')
                    for text in parts[1:]:
                        if text:
                            annotations.append({'onset': onset, 'duration': length, 'label': text.decode('utf-8')})
                continue
            gain = (pmax[i]-pmin[i])/(dmax[i]-dmin[i]) if dmax[i] != dmin[i] else 1
            record[label] = [pmin[i] + (v-dmin[i])*gain for v in vals]
        records.append(record)
    return {'start': start, 'duration': duration, 'records': records, 'annotations': annotations}


def sessions_from_str(path, since=None):
    edf = read_edf(path)
    if edf['duration'] != 86400:
        raise ValueError('STR 不是每日一条记录，不能推断目录')
    result = []
    for index, record in enumerate(edf['records']):
        start = edf['start'] + timedelta(days=index)
        day = start.strftime('%Y%m%d')
        if since and day < since:
            continue
        duration = record.get('Duration', [-1])[0]
        events = record.get('MaskEvents', [-1])[0]
        if duration < 0 or events < 0:
            continue
        on = record.get('MaskOn', [])
        off = record.get('MaskOff', [])
        for i, minute in enumerate(on):
            if not math.isfinite(minute) or not 0 <= minute <= 1440:
                continue
            end = off[i] if i < len(off) else -1
            result.append({'day': day, 'on': start + timedelta(minutes=minute),
                           'off': start + timedelta(minutes=end) if 0 <= end <= 1440 else None,
                           'mask_events': events, 'slots': len(on),
                           'duration_minutes': duration, 'summary_start': start,
                           'indices': {key: record.get(key, [-1])[0] for key in ('AHI','AI','CAI','OAI','UAI','HI')},
                           'slots_saturated': events >= 2*len(on)})
    return result


def candidate_paths(sessions, before=10, after=120, include_off=True):
    """先探测起点，再向外展开；目录按 STR 的治疗日，文件名按实际钟表日期。"""
    seen = set()
    offsets = sorted(range(-before, after+1), key=lambda n: (abs(n), n < 0))
    anchors = [(s['day'], s['on']) for s in sessions]
    if include_off:
        anchors += [(s['day'], s['off']) for s in sessions if s['off'] is not None]
    for offset in offsets:
        for day, anchor in anchors:
            stamp = (anchor + timedelta(seconds=offset)).strftime('%Y%m%d_%H%M%S')
            for kind in KINDS:
                for ext in ('edf', 'crc'):
                    rel = 'DATALOG/%s/%s_%s.%s' % (day, stamp, kind, ext)
                    if rel not in seen:
                        seen.add(rel)
                        yield rel


def legacy_url(base, relative):
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('非法相对路径')
    directory = '..' + ('\\' + str(path.parent).replace('/', '\\') if str(path.parent) != '.' else '')
    return base.rstrip('/') + '/download?' + urllib.parse.urlencode({'fname': 'STR.EDF' if relative == 'STR.edf' else path.name, 'fdir': directory})


def _download_once(base, relative, target, deadline, opener=None):
    """返回字节数；404 或实测的缺失文件跳转页返回 None，其他异常交给调用方。"""
    opener = opener or OPENER
    urls = [legacy_url(base, relative)]
    # 卡在呼吸机中时旧下载入口可能 404，但完整直接路径仍能读取文件。
    direct_path = 'STR.EDF' if relative == 'STR.edf' else relative
    urls.append(base.rstrip('/') + '/' + urllib.parse.quote(direct_path, safe='/'))
    for url in urls:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('探测时间已用完')
        req = urllib.request.Request(url, headers={'User-Agent': 'fitbox-cpap/1'})
        try:
            response = opener.open(req, timeout=min(15, remaining))
            break
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            exc.close()
    else:
        return None
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with response, target.open('wb') as output:
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError('下载时间已用完')
            # read1 每次至多做一次底层读取，避免慢速响应不断续 socket 超时。
            data = response.read1(65536)
            if not data:
                break
            output.write(data)
            count += len(data)
    if time.monotonic() >= deadline:
        raise TimeoutError('下载时间已用完')
    if not count:
        raise ValueError('卡返回空文件')
    if count <= 1024 and target.read_bytes().strip() == WELCOME_REDIRECT:
        target.unlink()
        return None
    return count


def download(base, relative, target, deadline, opener=None):
    """瞬时失败最多重试三次；404 尝试直接路径，认证等错误不重试。"""
    for attempt in range(3):
        try:
            return _download_once(base, relative, target, deadline, opener)
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt == 2 or time.monotonic() >= deadline:
                raise
            exc.close()
        except (OSError, urllib.error.URLError):
            if attempt == 2 or time.monotonic() >= deadline:
                raise


def _save(path, value):
    tmp = path.with_suffix(path.suffix + '.part')
    tmp.write_text(json.dumps(value, ensure_ascii=False))
    os.replace(tmp, path)


def sync(dest, journal, *, base='http://192.168.4.1', since=None, deadline=None,
         lookback_days=2, before=10, after=120, include_off=True, log=print, opener=None,
         reconnect=None):
    """刷新 STR 并扫描候选；reconnect(剩余秒数) 必须在该期限内返回。

    每轮最多重连两次，回调返回 False 表示失败；永不声明完整目录镜像成功。
    """
    dest = Path(dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    deadline = deadline if deadline is not None else time.monotonic()+600
    journal = Path(journal) if journal else None
    if journal:
        journal.parent.mkdir(parents=True, exist_ok=True)
    result = {'downloaded': 0, 'missing': 0, 'cached': 0, 'errors': [],
              'window_complete': False, 'mirror_complete': False, 'sessions': 0, 'reconnections': 0}
    def land(relative):
        target = dest / relative
        tmp = target.with_name(target.name + '.part')
        try:
            while True:
                try:
                    count = download(base, relative, tmp, deadline, opener)
                    break
                except urllib.error.HTTPError:
                    raise
                except (OSError, urllib.error.URLError) as exc:
                    network_error = isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)) or getattr(exc, 'errno', None) in {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.ENETRESET, errno.ECONNRESET, errno.ECONNREFUSED, errno.ETIMEDOUT}
                    remaining = deadline-time.monotonic()
                    if not network_error or reconnect is None or result['reconnections'] >= 2 or remaining <= 0:
                        raise
                    result['reconnections'] += 1
                    log('!! 卡连接中断，尝试重新连接后继续同一文件')
                    if reconnect(remaining) is False:
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError('重连后扫描时间已用完')
            if count is None:
                return None
            if target.suffix.lower() == '.edf':
                read_edf(tmp)
            elif target.suffix.lower() == '.crc' and count != 8:
                raise ValueError('CRC 文件长度不是实测的 8 字节')
            digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
            os.replace(tmp, target)
            if journal:
                with journal.open('a') as f:
                    f.write(json.dumps({'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                        'file': 'cpap/'+relative, 'bytes': count, 'sha256': digest})+'\n')
            result['downloaded'] += 1
            log('  ↓ %s  %s B' % (relative, count))
            return digest
        finally:
            tmp.unlink(missing_ok=True)
    try:
        summary_hash = land('STR.edf')
        if summary_hash is None:
            raise RuntimeError('STR.edf 返回 404')
        since = since or (date.today()-timedelta(days=lookback_days)).strftime('%Y%m%d')
        sessions = sessions_from_str(dest/'STR.edf', since)
        result['sessions'] = len(sessions)
        if not sessions:
            raise RuntimeError('STR 中没有目标日期的有效治疗记录')
        cache_path = dest/'.legacy-probes.json'
        # 入口协议、STR、窗口变化或隔天后失效，避免复用旧入口的 404。
        generation = [summary_hash, str(date.today()), since, before, after, include_off,
                      'direct-path-fallback-v1']
        try:
            cache = json.loads(cache_path.read_text())
        except (OSError, ValueError):
            cache = {}
        if cache.get('generation') != generation or time.time() - cache.get('created', 0) > 3600:
            cache = {'generation': generation, 'created': time.time(), 'done': {}}
        done = cache['done']
        groups = {}
        for session in sessions:
            groups.setdefault(session['day'], []).append(session)
        closed_days = {day for day, group in groups.items()
                       if all(s['off'] is not None and s['off'] > s['on']
                              and s['off'] < datetime.now()-timedelta(minutes=5) for s in group)}
        try:
            for relative in candidate_paths(sessions, before, after, include_off):
                if time.monotonic() >= deadline:
                    raise TimeoutError('探测时间已用完，已保存进度供下一次继续')
                old = done.get(relative)
                target = dest/relative
                # 只有全部会话已结束五分钟的治疗日才复用短期 404 缓存。
                if target.suffix == '.crc' and done.get(str(Path(relative).with_suffix('.edf'))) == '404':
                    continue
                closed_day = Path(relative).parts[1] in closed_days
                if (old == '404' and closed_day) or (old and target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == old):
                    result['cached'] += 1
                    continue
                digest = land(relative)
                done[relative] = digest or '404'
                if digest is None:
                    result['missing'] += 1
                if (result['downloaded'] + result['missing']) % 20 == 0:
                    _save(cache_path, cache)
            result['window_complete'] = True
        finally:
            _save(cache_path, cache)
    except Exception as exc:
        result['errors'].append('%s: %s' % (type(exc).__name__, exc))
        log('!! ' + result['errors'][-1])
    result['treatment_complete'] = False
    result['treatment_dates'] = []
    if result['window_complete'] and not result['errors']:
        accepted = [dest/relative for relative,digest in done.items() if digest != '404' and relative.endswith('.edf')
                    and (dest/relative).exists() and hashlib.sha256((dest/relative).read_bytes()).hexdigest() == digest]
        verified_crc = {dest/relative for relative,digest in done.items() if digest != '404' and relative.endswith('.crc')
                        and (dest/relative).exists() and (dest/relative).stat().st_size == 8
                        and hashlib.sha256((dest/relative).read_bytes()).hexdigest() == digest}
        report = assess_treatment(sessions, accepted, verified_crc=verified_crc)
        result['treatment_complete'] = report['treatment_complete']
        result['treatment_dates'] = report['treatment_dates']
        result['treatment_report'] = report
    return result


def assess_treatment(sessions, files, tolerance=60, verified_crc=None):
    """核验 STR 所列会话的波形覆盖；不能证明卡上所有文件均已枚举。

    MaskOn/Off 只有分钟精度，仅两端容许 tolerance 秒，内部不容许缺口。
    CSL 检查首会话 Recording starts；EVE 事件总数必须匹配 STR 的唯一整数计数。
    这不能证明 Arousal 或所有独立空启动文件完整。
    """
    evidence, problems, crc_missing = [], [], {}
    if verified_crc is not None:
        verified_crc = {Path(p) for p in verified_crc}
    for path in files:
        path = Path(path)
        try:
            edf = read_edf(path)
            crc = path.with_suffix('.crc')
            if verified_crc is None:
                try:
                    crc_ok = len(crc.read_bytes()) == 8
                except OSError:
                    crc_ok = False
            else:
                crc_ok = crc in verified_crc
            if not crc_ok:
                crc_missing.setdefault(path.parent.name, []).append(path.name)
            kind = path.stem.rsplit('_', 1)[-1]
            evidence.append({'day': path.parent.name, 'kind': kind, 'file': str(path),
                             'start': edf['start'], 'annotations': edf.get('annotations', []),
                             'end': edf['start']+timedelta(seconds=edf['duration']*len(edf['records']))})
        except (OSError, ValueError, struct.error) as exc:
            problems.append('文件不可校验 %s: %s' % (path.name, exc))
    by_day = {}
    for session in sessions:
        by_day.setdefault(session['day'], []).append(session)
    day_reports = {}
    for day, group in by_day.items():
        reasons = ['缺少有效配套 CRC: '+name for name in crc_missing.get(day, [])]
        events = group[0].get('mask_events', -1)
        if events != 2*len(group):
            reasons.append('MaskEvents 与有效 MaskOn/Off 会话数不匹配')
        if any(s.get('slots_saturated', True) for s in group):
            reasons.append('MaskOn 槽位已满，可能还有未列出的会话')
        event_report = assess_daily_events(group, [e for e in evidence if e['day']==day])
        reasons.extend(event_report['reasons'])
        details = []
        for session in group:
            on, off = session['on'], session['off']
            item = {'on': on.isoformat(), 'off': off.isoformat() if off else None, 'covered': {}, 'events': {}}
            if off is None or off <= on:
                reasons.append('会话尚无有效结束时间')
                details.append(item)
                continue
            for kind in ('BRP', 'PLD', 'SAD'):
                intervals = sorted((e['start'],e['end']) for e in evidence if e['day']==day and e['kind']==kind and e['end']>e['start'])
                cursor = on+timedelta(seconds=tolerance)
                stop = off-timedelta(seconds=tolerance)
                matched = False
                for begin, end in intervals:
                    if end <= on-timedelta(seconds=tolerance) or begin >= off+timedelta(seconds=tolerance):
                        continue
                    if begin > cursor:
                        break
                    if end >= cursor:
                        matched = True
                        cursor = end
                covered = matched and cursor >= stop
                item['covered'][kind] = covered
                if not covered:
                    reasons.append('%s 的 %s 波形未覆盖会话' % (on.isoformat(),kind))
            item['events'] = {'daily_counts_verified': event_report['complete']}
            details.append(item)
        day_reports[day] = {'complete': not reasons, 'reasons': reasons, 'sessions': details, 'event_validation': event_report}
    return {'treatment_complete': bool(day_reports) and not problems and all(d['complete'] for d in day_reports.values()),
            'treatment_dates': [d for d,r in day_reports.items() if r['complete']],
            'days': day_reports, 'errors': problems, 'mirror_complete': False}


def assess_daily_events(sessions, evidence):
    """用本机实测的 0.1 向下截断指数核对整夜事件，计数有歧义就拒绝。"""
    reasons, expected = [], {}
    summary = sessions[0]
    duration = summary.get('duration_minutes', -1)
    indices = summary.get('indices', {})
    if not math.isfinite(duration) or duration <= 0:
        reasons.append('STR 缺少有效治疗分钟数')
    else:
        for key in ('AHI','AI','CAI','OAI','UAI','HI'):
            value = indices.get(key, -1)
            if not math.isfinite(value) or value < 0:
                reasons.append('STR 缺少有效 '+key)
                continue
            low = math.ceil(value*duration/60-1e-7)
            high = math.ceil((value+0.1)*duration/60-1e-7)-1
            if low < 0 or low != high:
                reasons.append('STR '+key+' 不能唯一确定事件整数计数')
            else:
                expected[key] = low
    first_on = min(s['on'] for s in sessions)
    for kind in ('CSL','EVE'):
        recording = any(e['kind']==kind and any(a['label']=='Recording starts'
                        and -60 <= (e['start']+timedelta(seconds=a['onset'])-first_on).total_seconds() <= 120
                        for a in e['annotations']) for e in evidence)
        if not recording:
            reasons.append('缺少首会话附近的 '+kind+' Recording starts')
    day_start = summary.get('summary_start')
    if day_start is None:
        reasons.append('STR 缺少治疗日边界')
    counts, seen = Counter(), set()
    breathing = {'Central Apnea':'CAI','Obstructive Apnea':'OAI','Apnea':'UAI','Hypopnea':'HI'}
    for file in evidence:
        if file['kind'] != 'EVE':
            continue
        for annotation in file['annotations']:
            label = annotation['label']
            if label in ('Recording starts','Arousal'):
                continue
            if label not in breathing:
                reasons.append('未知 EVE 事件类型: '+label)
                continue
            instant = file['start']+timedelta(seconds=annotation['onset'])
            identity = (instant, annotation['duration'], label)
            if identity in seen:
                reasons.append('EVE 有重复呼吸事件，不能用重复文件补齐计数')
                continue
            seen.add(identity)
            if day_start is not None and not day_start <= instant < day_start+timedelta(days=1):
                reasons.append('EVE 呼吸事件超出 STR 治疗日')
            counts[breathing[label]] += 1
    observed = {key: counts[key] for key in ('CAI','OAI','UAI','HI')}
    observed['AI'] = sum(observed[key] for key in ('CAI','OAI','UAI'))
    observed['AHI'] = observed['AI']+observed['HI']
    for key, count in expected.items():
        if observed[key] != count:
            reasons.append('EVE %s 事件数 %d 与 STR 应有 %d 不符' % (key,observed[key],count))
    return {'complete': not reasons, 'reasons': reasons, 'expected': expected, 'observed': observed}
