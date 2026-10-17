"""Render evidence-aware sleep comparisons without external assets."""

from datetime import date, datetime, timedelta
from html import escape
import math


INK = "#2d2a26"
MUTED = "#5d5a54"
GRID = "#e8e4dd"
WATCH = "#c96442"
PAP = "#2e7d52"


def _num(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _text(x, y, value, color=MUTED, size=12, anchor="start"):
    return (f'<text x="{x:.1f}" y="{y:.1f}" fill="{color}" '
            f'font-size="{size}" text-anchor="{anchor}">{escape(str(value))}</text>')


def _svg(body, height, label):
    return (f'<svg viewBox="0 0 600 {height}" role="img" '
            f'aria-label="{escape(label, quote=True)}" '
            'style="display:block;width:100%;height:auto;font-family:inherit">'
            f'<title>{escape(label)}</title>{body}</svg>')


def _duration(value):
    value = _num(value)
    if value is None:
        return "暂无数据"
    return f"{int(round(value)) // 60} 小时 {int(round(value)) % 60} 分"


def _timeline(night):
    start, end = _time(night.get("watch_start")), _time(night.get("watch_end"))
    if start is None or end is None or end <= start:
        return '<p class="sl-note">缺少手表睡眠起止时间，暂不能绘制同轴对照。</p>'
    sessions = []
    for session in night.get("sessions") or []:
        a, b = _time(session.get("start")), _time(session.get("end"))
        if a is not None and b is not None and b > a:
            sessions.append((a, b))
    lo = min([start] + [a for a, _ in sessions])
    hi = max([end] + [b for _, b in sessions])
    left, width = 88, 490
    x = lambda value: left + width * (value - lo) / (hi - lo)
    body = _text(0, 43, "手表窗口") + _text(0, 91, "PAP 会话")
    for pos in range(5):
        moment = lo + (hi - lo) * pos / 4
        xx = x(moment)
        body += f'<line x1="{xx}" x2="{xx}" y1="20" y2="108" stroke="{GRID}"/>'
        # Both data sources expose local ISO times. Format in the watch timezone.
        zone = datetime.fromisoformat(str(night["watch_start"]).replace("Z", "+00:00")).tzinfo
        label = datetime.fromtimestamp(moment, tz=zone).strftime("%H:%M")
        body += _text(xx, 130, label, anchor="middle")
    body += (f'<rect x="{x(start)}" y="27" width="{x(end)-x(start)}" '
             f'height="24" rx="5" fill="{WATCH}" opacity=".85"/>')
    for a, b in sessions:
        body += (f'<rect x="{x(a)}" y="75" width="{max(1, x(b)-x(a))}" '
                 f'height="24" rx="4" fill="{PAP}"/>')
    if not sessions:
        body += _text(left + 8, 91, "暂无可核验的会话时间")
    coverage = _num(night.get("window_coverage_pct"))
    subtitle = (f"窗口覆盖 {coverage:.0f}%" if coverage is not None else "窗口覆盖率待核验")
    tail = _num(night.get("tail_uncovered_min"))
    if tail is not None and tail > 0:
        subtitle += f" · 窗口末段未覆盖 {tail:.0f} 分钟"
    return (_svg(body, 145, "手表睡眠窗口与全部 PAP 会话的时间对照")
            + f'<p class="sl-note">{subtitle}。手表窗口包含清醒间隔，不代表全程睡眠；'
            '会话间隔不能单凭数据认定为摘面罩。</p>')


def _date_label(night):
    return str(night.get("date", ""))[5:].replace("-", "/")


def _bars(nights):
    values = []
    for night in nights:
        values.extend([_num(night.get("sleep_min")), _num(night.get("overlapping_pap_min"))])
    ceiling = max(120, math.ceil(max([v for v in values if v is not None] or [0]) / 120) * 120)
    left, top, width, height = 38, 20, 544, 120
    body = ""
    for tick in range(4):
        minutes = ceiling * tick / 3
        y = top + height - height * minutes / ceiling
        body += f'<line x1="{left}" x2="582" y1="{y}" y2="{y}" stroke="{GRID}"/>'
        body += _text(left - 7, y + 4, f"{minutes / 60:.1f}h", anchor="end", size=11)
    step = width / max(1, len(nights))
    bar_width = min(13, step * .29)
    for i, night in enumerate(nights):
        center = left + step * (i + .5)
        for key, color, offset in (("sleep_min", WATCH, -bar_width - 1),
                                    ("overlapping_pap_min", PAP, 1)):
            value = _num(night.get(key))
            if value is None:
                body += _text(center + offset + bar_width / 2, top + height - 3,
                              "·", color=color, anchor="middle")
                continue
            bar_height = max(0, value) / ceiling * height
            body += (f'<rect x="{center+offset}" y="{top+height-bar_height}" '
                     f'width="{bar_width}" height="{bar_height}" rx="2" fill="{color}">'
                     f'<title>{escape(str(night.get("date", "")))} '
                     f'{"手表睡眠" if key == "sleep_min" else "PAP 窗口内重叠"} '
                     f'{value:.0f} 分钟</title></rect>')
        if len(nights) <= 7 or i % 2 == 0 or i == len(nights) - 1:
            body += _text(center, 160, _date_label(night), anchor="middle", size=11)
    return _svg(body, 175, "近 14 日手表睡眠时长与 PAP 在手表窗口内的重叠时长")


def _trend(nights, key, title, unit, color, ceiling=None):
    valid = [_num(night.get(key)) for night in nights]
    points = [value for value in valid if value is not None]
    maximum = ceiling or max(30, math.ceil(max(points or [0]) / 30) * 30)
    left, width, top, height = 38, 540, 18, 62
    body = ""
    for value in (0, maximum):
        y = top + height - value / maximum * height
        body += f'<line x1="{left}" x2="578" y1="{y}" y2="{y}" stroke="{GRID}"/>'
        body += _text(left - 7, y + 4, f"{value:g}", anchor="end", size=11)
    previous = None
    for i, value in enumerate(valid):
        x = left + width * i / max(1, len(nights) - 1)
        if value is None:
            previous = None
        else:
            y = top + height - value / maximum * height
            if previous:
                body += f'<line x1="{previous[0]}" y1="{previous[1]}" x2="{x}" y2="{y}" stroke="{color}" stroke-width="2"/>'
            body += f'<circle cx="{x}" cy="{y}" r="3.5" fill="{color}"><title>{escape(str(nights[i].get("date", "")))} {value:.1f}{unit}</title></circle>'
            previous = (x, y)
        if i == 0 or i == len(nights) - 1:
            body += _text(x, 101, _date_label(nights[i]), anchor="middle", size=11)
    mean = f"{sum(points)/len(points):.1f}{unit}" if points else "暂无数据"
    return (f'<div class="sl-trend"><p class="sl-subtitle">{escape(title)}'
            f'<span>均值 {mean} · n={len(points)}</span></p>'
            + _svg(body, 112, title + "，缺失数据不连线") + '</div>')


def _message(item):
    if isinstance(item, dict):
        return str(item.get("text") or item.get("message") or item.get("title") or "")
    return str(item)


def _clock_label(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%m/%d %H:%M")
    except (TypeError, ValueError):
        return "时间待确认"


def _stop_metric(metric):
    metric = metric or {}
    if metric.get("status") == "unknown":
        return "待数据"
    median, upper = _num(metric.get("p50")), _num(metric.get("p95"))
    if median is None or upper is None:
        return "待数据"
    value = f"{median:.1f} / {upper:.1f}"
    coverage = _num(metric.get("coverage_fraction"))
    if coverage is not None and coverage < .999:
        value += f"<br><span class=\"sl-note\">波形覆盖 {coverage * 100:.0f}%</span>"
    return value


def _stop_windows(windows):
    result = '<div class="sl-panel"><h4>停机前 10 分钟</h4>'
    if not windows:
        result += '<p class="sl-note">尚无可分析的停机窗口，等待会话与波形数据。</p>'
    else:
        result += ('<p class="sl-note">漏气与机器压力均列出中位数 / 第 95 百分位；短会话只统计实际可用片段。</p>'
                   '<div style="overflow-x:auto"><table style="width:100%;font-size:12px;text-align:left;border-collapse:collapse">'
                   '<thead><tr><th style="padding:7px 5px">分析窗口</th>'
                   '<th style="padding:7px 5px">漏气<br>L/min</th>'
                   '<th style="padding:7px 5px">机器压力<br>cmH₂O</th>'
                   '<th style="padding:7px 5px">机器标记事件</th></tr></thead><tbody>')
        for window in windows:
            times = (escape(_clock_label(window.get("window_start"))) + '<br>至 '
                     + escape(_clock_label(window.get("window_end"))))
            status = {"complete": "数据完整", "partial": "数据不全", "unknown": "待数据"}.get(window.get("status"), "待核验")
            times += f'<br><span class="sl-note">{status}</span>'
            events = window.get("events") or {}
            counts = events.get("counts")
            event_text = "待数据"
            if events.get("status") == "observed" and isinstance(counts, dict):
                # Do not infer absent event categories as zero.
                labels = []
                for key in ("OA", "CA", "UA", "H", "RERA"):
                    count = _num(counts.get(key))
                    labels.append(f'{key} {int(count)}' if count is not None else f'{key} —')
                event_text = '<br>'.join((' · '.join(labels[:3]), ' · '.join(labels[3:])))
            cells = [times, _stop_metric(window.get("leak")),
                     _stop_metric(window.get("pressure")), event_text]
            result += '<tr>' + ''.join(f'<td style="padding:9px 5px;border-top:1px solid #e8e4dd;vertical-align:top">{cell}</td>' for cell in cells) + '</tr>'
            issues = [_message(issue) for issue in window.get("issues") or []]
            if any(issues):
                result += '<tr><td colspan="4" class="sl-note" style="padding:0 5px 9px">' + '；'.join(escape(issue) for issue in issues if issue) + '</td></tr>'
        result += '</tbody></table></div>'
        result += '<p class="sl-note">OA 阻塞性暂停 · CA 中枢性暂停 · UA 未分类暂停 · H 低通气 · RERA 呼吸努力相关觉醒标记。缺失项显示 — 或待数据，不计作 0；机器标记不等同于脑电确认的觉醒。</p>'
    result += '<p class="sl-note">停机时间来自 STR，精确到分钟。这些窗口不能确认觉醒或摘面罩的原因；机器压力字段不代表面罩端实测压力。</p></div>'
    return result


def render_section(analysis):
    """Return a self-contained HTML section; unknown measures stay unknown."""
    analysis = analysis or {}
    nights = sorted(analysis.get("nights") or [], key=lambda n: str(n.get("date", "")))[-14:]
    latest = analysis.get("latest") or (nights[-1] if nights else {})
    if not isinstance(latest, dict):
        latest = next((n for n in nights if n.get("date") == latest), {})
    # Preserve missing calendar days as gaps rather than joining their neighbors.
    if nights:
        try:
            last_day = date.fromisoformat(str(nights[-1]["date"]))
            first_day = max(date.fromisoformat(str(nights[0]["date"])),
                            last_day - timedelta(days=13))
            by_day = {str(n["date"]): n for n in nights}
            nights = [by_day.get((first_day + timedelta(days=i)).isoformat(),
                                {"date": (first_day + timedelta(days=i)).isoformat()})
                      for i in range((last_day - first_day).days + 1)]
        except (ValueError, KeyError):
            pass
    styles = '''<style>
    .sleep-review{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',sans-serif;color:#2d2a26;line-height:1.7;margin:24px 0;padding:20px;background:#f7f5f0;border:1px solid #e8e4dd;border-radius:14px}
    .sleep-review h3{font-size:.75rem;letter-spacing:.08em;border-bottom:1px solid #e8e4dd;padding-bottom:10px;margin:0 0 16px}
    .sleep-review h4{font-size:15px;margin:20px 0 8px}.sleep-review p{margin:8px 0}
    .sleep-review .sl-note{font-size:12px;color:#5d5a54}.sleep-review .sl-panel{background:#fff;border:1px solid #f0ece5;border-radius:10px;padding:14px;margin:12px 0}
    .sleep-review .sl-stats{display:flex;flex-wrap:wrap;gap:12px 28px;font-size:13px}.sleep-review .sl-stats strong{display:block;font-size:19px;font-weight:600}
    .sleep-review .sl-subtitle{font-size:13px;font-weight:600}.sleep-review .sl-subtitle span{float:right;font-weight:400;font-size:12px;color:#5d5a54}
    .sleep-review .sl-trend{margin-top:15px}.sleep-review ul{padding-left:20px;font-size:13px}.sleep-review li{margin:7px 0}
    @media(max-width:480px){.sleep-review{padding:12px}.sleep-review .sl-panel{padding:9px}.sleep-review .sl-subtitle span{float:none;display:block}}
    </style>'''
    result = styles + '<section class="sleep-review"><h3>睡眠 × 呼吸机 · 联合观察</h3>'
    if not nights and not latest:
        return result + '<p class="sl-note">暂无可对照的睡眠与呼吸机会话数据。</p></section>'
    result += f'<p class="sl-note">按睡醒日期对齐 · 最近一晚 {escape(str(latest.get("date", "待确认")))}</p>'
    deep = _num(latest.get("deep_min"))
    ahi = _num(latest.get("ahi"))
    result += ('<div class="sl-stats">'
               f'<div>手表睡眠<strong>{_duration(latest.get("sleep_min"))}</strong></div>'
               f'<div>手表深睡估计<strong>{f"{deep:.0f} 分钟" if deep is not None else "暂无数据"}</strong></div>'
               f'<div>PAP 使用期间 AHI<strong>{f"{ahi:.1f}" if ahi is not None else "暂无数据"}</strong></div></div>')
    result += '<div class="sl-panel"><h4>昨晚时间对照</h4>' + _timeline(latest) + '</div>'
    result += _stop_windows(analysis.get("stop_windows") or [])
    if nights:
        result += ('<div class="sl-panel"><h4>近 14 日趋势</h4>'
                   '<p class="sl-note"><span style="color:#c96442">● 手表睡眠时长</span>　'
                   '<span style="color:#2e7d52">● PAP 窗口内重叠时长</span></p>' + _bars(nights)
                   + '<p class="sl-note">两组时长口径不同：手表睡眠排除其识别的清醒时间；PAP 重叠包含窗口内的清醒时间。圆点占位表示缺失，不能当作 0。</p>'
                   + _trend(nights, "window_coverage_pct", "PAP 对手表窗口覆盖率", "%", PAP, 100)
                   + _trend(nights, "deep_min", "手表深睡估计", " 分钟", WATCH)
                   + '<p class="sl-note">n 为有效夜数。缺失记录保留断点；消费级手表分期是估计，深睡比例不等同于睡眠质量诊断。</p></div>')
    averages = analysis.get("averages") or {}
    rows = []
    for period, label in (("7d", "近 7 日"), ("14d", "近 14 日")):
        metrics = averages.get(period) or {}
        cells = [label]
        for key, unit in (("sleep_min", "分"), ("deep_min", "分"),
                          ("window_coverage_pct", "%")):
            metric = metrics.get(key) or {}
            mean = _num(metric.get("mean"))
            count = _num(metric.get("n"))
            cells.append(f'{mean:.0f}{unit}（n={int(count or 0)}）' if mean is not None else "暂无数据")
        if metrics:
            rows.append('<tr>' + ''.join(f'<td style="padding:7px 5px;border-top:1px solid #e8e4dd">{cell}</td>' for cell in cells) + '</tr>')
    if rows:
        result += ('<div style="overflow-x:auto"><table style="width:100%;font-size:12px;text-align:left;border-collapse:collapse">'
                   '<thead><tr><th>均值</th><th>手表睡眠</th><th>手表深睡</th><th>窗口覆盖</th></tr></thead>'
                   '<tbody>' + ''.join(rows) + '</tbody></table></div>')
    observations = [_message(item) for item in analysis.get("observations") or []]
    if any(observations):
        result += '<h4>今晚值得关注</h4><ul>' + ''.join(f'<li>{escape(item)}</li>' for item in observations if item) + '</ul>'
    limitations = [_message(item) for item in analysis.get("limitations") or []]
    limitations += ['AHI 只反映呼吸机使用期间；未覆盖时段不能据此判断呼吸事件。没有睡眠分期时间戳，因此不绘制分期时间轴。']
    result += '<p class="sl-note">' + '<br>'.join(escape(item) for item in limitations if item) + '</p></section>'
    return result
