#!/usr/bin/env python3
"""Render the morning report as a phone-sized card (HTML → PNG/PDF) in the weekly-report dark palette.

render(daily, today) → (png_path, pdf_path). Watch sleep stages are consumer-grade estimates,
shown as such rather than graded against fixed clinical thresholds.
"""
import json, os, re, shutil, subprocess, tempfile
from pathlib import Path
from html import escape
from datetime import date, timedelta
from pull import FIT
from config import WEIGHT_GOAL

OUT = FIT / '数据' / '健康' / '晨报'
CHROME = os.environ.get('CHROME_BIN', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
WD = '一二三四五六日'
STAGES = [  # key, name, colour, (low, high) share of sleep, meaning
    ('deep', '深睡', '#8b6cf0', (13, 23), '身体修复：生长激素、肌肉和组织恢复主要靠它'),
    ('rem', '快速眼动', '#4cc9f0', (20, 25), '做梦期：整理记忆、调节情绪，大脑在"存档"'),
    ('core', '核心（浅睡）', '#3b82f6', (45, 60), '分期为算法估计，以个人多晚变化为主'),
]
GOOD, WARN, BAD = '#4ade80', '#f2c14e', '#f2667a'


def chip(text, colour):
    return f'<span class="chip" style="color:{colour};border-color:{colour}55;background:{colour}1a">{text}</span>'


def stage_status(pct, lo, hi, key=''):
    # 消费级手表阶段估计只做个人趋势，不用固定占比判正常/异常。
    return chip('手表估计', '#8aa0b8')


def trend_svg(daily, today):
    days = [today - timedelta(days=k) for k in range(6, -1, -1)]
    W, H, top, base = 470, 196, 30, 156
    bw, gap = 46, (470 - 7 * 46) / 6
    scale = (base - top) / 9.0                      # 9 h = full height
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%">',
           f'<line x1="0" x2="{W}" y1="{base - 7 * scale:.1f}" y2="{base - 7 * scale:.1f}" stroke="#4a6a92" stroke-dasharray="4 4"/>']
    for i, d in enumerate(days):
        x = i * (bw + gap)
        s = daily.get(str(d), {}).get('sleep_analysis')
        label = f'周{WD[d.weekday()]}'
        colour = '#e6edf5' if d == today else '#8aa0bd'
        out.append(f'<text x="{x + bw / 2:.1f}" y="{base + 20}" text-anchor="middle" font-size="12" fill="{colour}">{label}</text>')
        if not s:
            out.append(f'<text x="{x + bw / 2:.1f}" y="{base - 6}" text-anchor="middle" font-size="11" fill="#4a6a92">—</text>')
            continue
        y = base
        for key, _, c, _, _ in reversed(STAGES):     # core at the bottom, deep on top
            h = s.get(key, 0) * scale
            out.append(f'<rect x="{x:.1f}" y="{y - h:.1f}" width="{bw}" height="{max(h - 1.5, 0):.1f}" rx="3" fill="{c}" opacity="{1 if d == today else .55}"/>')
            y -= h
        aw = s.get('awake', 0) * scale
        out.append(f'<rect x="{x:.1f}" y="{y - aw:.1f}" width="{bw}" height="{max(aw - 1, 0):.1f}" rx="3" fill="#f59e42" opacity="{1 if d == today else .55}"/>')
        out.append(f'<text x="{x + bw / 2:.1f}" y="{y - aw - 6:.1f}" text-anchor="middle" font-size="12" font-weight="700" fill="{colour}">{s.get("totalSleep", 0):.1f}</text>')
    out.append('</svg>')
    return ''.join(out)


def metric(label, val, unit, avg, better_low, fmt='{:.0f}'):
    if val is None:
        return ''
    diff = ''
    if avg is not None:
        d = val - avg
        good = (d <= 0) if better_low else (d >= 0)
        c = GOOD if good or abs(d) < (1 if better_low else 3) else WARN
        flat = float(fmt.format(abs(d))) == 0          # shows as 0 after rounding → call it level
        diff = f'<div class="diff" style="color:{GOOD if flat else c}">' + ('与 7 天均值持平' if flat else f'{"▲" if d > 0 else "▼"} {fmt.format(abs(d))} vs 7 天均值') + '</div>'
    return f'<div class="m"><div class="ml">{label}</div><div class="mv">{fmt.format(val)}<small>{unit}</small></div>{diff}</div>'


HERO = """<div class="hero"><svg viewBox="0 0 760 250" preserveAspectRatio="xMidYMid slice">
<defs><linearGradient id="sky" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#16263b"/><stop offset=".55" stop-color="#2a3a4f"/><stop offset=".85" stop-color="#7a5a3a"/><stop offset="1" stop-color="#c47b3c"/></linearGradient>
<radialGradient id="sun" cx="0.74" cy="0.5" r="0.4"><stop offset="0" stop-color="#ffe7b0" stop-opacity=".95"/><stop offset=".15" stop-color="#f5b867" stop-opacity=".55"/><stop offset="1" stop-color="#f5b867" stop-opacity="0"/></radialGradient>
<linearGradient id="fade" x1="0" y1="0" x2="0" y2="1"><stop offset=".5" stop-color="#0b1522" stop-opacity="0"/><stop offset="1" stop-color="#0b1522"/></linearGradient></defs>
<rect width="760" height="250" fill="url(#sky)"/><rect width="760" height="250" fill="url(#sun)"/><circle cx="562" cy="122" r="9" fill="#fff4d6"/>
<path d="M0 185 L90 140 L170 170 L260 100 L340 155 L400 125 L490 168 L560 118 L640 155 L710 125 L760 140 L760 250 L0 250Z" fill="#23344a"/>
<path d="M215 140 L260 100 L276 124 Z" fill="#8196ad" opacity=".7"/><path d="M545 132 L560 118 L582 138 Z" fill="#8196ad" opacity=".6"/>
<path d="M0 210 L110 175 L200 200 L300 162 L400 196 L500 172 L610 200 L690 178 L760 196 L760 250 L0 250Z" fill="#17263a"/>
<path d="M0 230 L140 212 L290 228 L440 208 L590 228 L760 214 L760 250 L0 250Z" fill="#0f1d2e"/>
<rect width="760" height="250" fill="url(#fade)"/></svg>"""


def tile(colour, label, value, unit, foot_l='', foot_r='', foot_cls=''):
    return (f'<div class="tile"><div class="h"><i class="dot" style="background:{colour}"></i>{label}</div>'
            f'<div class="v">{value}<small>{unit}</small></div><div class="f"><span>{foot_l}</span><span class="{foot_cls}">{foot_r}</span></div></div>')


def vs(val, avg, better_low, fmt='{:.0f}'):
    """(text, css class) comparing to the 7-day average"""
    if val is None or avg is None:
        return '', ''
    d = val - avg
    if float(fmt.format(abs(d))) == 0:
        return '持平', 'g'
    good = (d < 0) if better_low else (d > 0)
    return f'{"+" if d > 0 else "−"}{fmt.format(abs(d))}', 'g' if good else 'y'


def weight_svg(points):
    """near-2-week morning weights with a least-squares line"""
    if len(points) < 2:
        return '<div class="note">体重数据还不够画趋势</div>'
    W, H, l, r, t, b = 690, 200, 34, 12, 14, 30
    ws = [v for _, v in points]
    lo, hi = min(ws) - 0.4, max(ws) + 0.4
    d0 = points[0][0]; span = max((points[-1][0] - d0).days, 1)
    X = lambda d: l + (d - d0).days / span * (W - l - r)
    Y = lambda v: t + (hi - v) / (hi - lo) * (H - t - b)
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%">']
    for k in range(3):
        v = lo + (hi - lo) * k / 2
        out.append(f'<line x1="{l}" x2="{W - r}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="#22364f"/><text x="{l - 6}" y="{Y(v) + 4:.1f}" text-anchor="end" font-size="11" fill="#6f87a6">{v:.1f}</text>')
    xs = [(d - d0).days for d, _ in points]; mx, my = sum(xs) / len(xs), sum(ws) / len(ws)
    den = sum((x - mx) ** 2 for x in xs) or 1
    k_ = sum((x - mx) * (y - my) for x, y in zip(xs, ws)) / den
    y0, y1 = my - k_ * mx, my + k_ * (span - mx)
    out.append(f'<line x1="{l}" y1="{Y(y0):.1f}" x2="{W - r}" y2="{Y(y1):.1f}" stroke="#f2c14e" stroke-width="2" stroke-dasharray="6 5"/>')
    out.append('<polyline fill="none" stroke="#4cc9f0" stroke-width="2.5" points="' + ' '.join(f'{X(d):.1f},{Y(v):.1f}' for d, v in points) + '"/>')
    for d, v in points:
        out.append(f'<circle cx="{X(d):.1f}" cy="{Y(v):.1f}" r="3.5" fill="#4cc9f0"/>')
    for d in (points[0][0], points[-1][0]):
        out.append(f'<text x="{X(d):.1f}" y="{H - 8}" text-anchor="{"start" if d == points[0][0] else "end"}" font-size="11" fill="#6f87a6">{d.month}/{d.day}</text>')
    lv = points[-1][1]
    out.append(f'<text x="{X(points[-1][0]) - 6:.1f}" y="{Y(lv) - 9:.1f}" text-anchor="end" font-size="13" font-weight="700" fill="#e6edf5">{lv:.2f}</text>')
    out.append('</svg>')
    return ''.join(out)


def html(daily, today, verdict, flags, weight, ex):
    from health_daily import avg7, hm_, leak_say
    d = daily.get(str(today), {})
    s = d['sleep_analysis']
    tot = s.get('totalSleep', 0) or 1e-9
    awake_min = s.get('awake', 0) * 60
    avg_tot = avg7(daily, today, 'sleep_analysis', 'totalSleep')
    yday = daily.get(str(today - timedelta(days=1)), {})
    rhr_src = '' if d.get('resting_heart_rate') else '昨天 · '
    rhr = d.get('resting_heart_rate') or yday.get('resting_heart_rate')
    hrv = d.get('heart_rate_variability')
    a_rhr, a_hrv = avg7(daily, today, 'resting_heart_rate'), avg7(daily, today, 'heart_rate_variability')
    t_sleep, c_sleep = vs(tot * 60, avg_tot * 60 if avg_tot else None, False)
    t_sleep = t_sleep + '分' if t_sleep and t_sleep != '持平' else t_sleep
    t_rhr, c_rhr = vs(rhr, a_rhr, True)
    t_hrv, c_hrv = vs(hrv, a_hrv, False)
    hrv_meta = d.get('_sample_metrics', {}).get('heart_rate_variability')
    hrv_ref = (f"{hrv_meta['count']} 次采样均值 · " if hrv_meta and hrv_meta.get('count') else '') + (f'7 天均 {a_hrv:.0f}' if a_hrv else '')
    body = weight[2] if weight and len(weight) > 2 else None
    tr = ex['trend']
    fat_wk = tr.get('fat_wk')
    tiles = ''.join([
        tile('#4cc9f0', '睡眠', hm_(tot).replace('小时', '<small>小时</small>').replace('分', '<small>分</small>'), '', f'7 天均 {hm_(avg_tot)}' if avg_tot else '', t_sleep, c_sleep),
        tile('#f2667a', '静息心率', f'{rhr:.0f}' if rhr else '—', '次/分', f'{rhr_src}7 天均 {a_rhr:.0f}' if a_rhr else rhr_src, t_rhr, c_rhr),
        tile('#4ade80', '心率变异性', f'{hrv:.0f}' if hrv else '—', 'ms', hrv_ref, t_hrv, c_hrv),
        tile('#f2c14e', '今早体重', f'{weight[1]:.2f}' if weight else '—', 'kg', f'{weight[0]} 自动称' if weight else '还没称', ''),
        tile('#f59e42', '昨日' + ex.get('energy_label', '总消耗'), f'{ex["total"]:.0f}' if ex['total'] is not None else '—', '千卡', ' · '.join(f'{label} {value:.0f}' if value is not None else f'{label} 未同步' for label, value in [('活动', ex['act']), ('基础', ex['bas'])]), ''),
        tile('#4cc9f0', '手表锻炼', f'{ex["ex_min"]:.0f}' if ex['ex_min'] is not None else '—', '分钟', f'{ex["steps"]:.0f} 步' if ex['steps'] is not None else '', ''),
        tile('#b48cf2', '体脂率', f'{body["fat_pct"]}' if body else '—', '%', f'脂肪 {body["fat_kg"]} kg' if body else '', ''),
        tile('#4ade80', '肌肉量', f'{body["muscle_kg"]}' if body else '—', 'kg', '开源算法估算' if body else '', ''),
    ])
    bar = ''.join(f'<div style="flex:{s.get(k, 0)};background:{c}"></div>' for k, _, c, _, _ in STAGES)
    bar += f'<div style="flex:{s.get("awake", 0)};background:#f59e42"></div>'
    rows = '<div class="note">睡眠时长由已同步的逐段记录汇总，后续同步可能补齐。</div>' if s.get('origin') == 'segments' else ''
    for k, name, c, (lo, hi), mean in STAGES:
        hrs = s.get(k, 0); pct = hrs / tot * 100
        rows += (f'<div class="sig"><i class="dot" style="background:{c}"></i><div class="sn"><b>{name}</b><em>{mean}</em></div>'
                 f'<div class="sv">{hm_(hrs)}<small> · {pct:.0f}%</small></div>{stage_status(pct, lo, hi, k)}</div>')
    from health_daily import cpap_brief, cpap_indicators
    brief = ex.get('cpap_brief') or cpap_brief(ex.get('cpap'))
    cards = []
    for metric in cpap_indicators(ex.get('cpap')):
        colour = {'good':GOOD,'warn':WARN,'unknown':'#8aa0b8'}[metric['level']]
        cards.append(f'<div class="pap-metric"><div class="pap-label">{escape(metric["title"])}</div>'
                     f'<div class="pap-value" style="color:{colour}">{escape(metric["value"])}<small>{escape(metric["unit"])}</small></div>'
                     f'<div>{chip(metric["status"],colour)}</div><div class="pap-ref">{escape(metric["reference"])}</div></div>')
    from sleep_stages import render_stages
    stage_html = render_stages(ex.get('stage_comparison'))
    usage = ' · 使用 ' + hm_(ex['cpap']['min']/60) if ex.get('cpap') else ''
    cp_html = ('<div style="margin-top:12px;padding-top:12px;border-top:1px solid #22364f">'
               f'<b style="font-size:15px;color:#4cc9f0">呼吸机</b><span style="font-size:12px;color:#8aa0b8">{usage}</span><div class="pap-grid">'+''.join(cards)+'</div>'
               f'<div style="font-size:13px;margin-top:8px">{escape(brief["action"])}</div>'
               '<div class="note">呼吸频率灯：个人趋势参考；漏气 P95：95% 治疗时间不超过此值。</div>' + stage_html + '</div>')
    rows += (f'<div class="sig"><i class="dot" style="background:#f59e42"></i><div class="sn"><b>清醒</b><em>手表估计的清醒总时长，不等同于临床觉醒指数</em></div>'
             f'<div class="sv">{hm_(s.get("awake", 0))}</div>{chip('手表估计', '#8aa0b8')}</div>')
    # 练
    wo = ''.join(f'<div class="sig"><i class="dot" style="background:#f59e42"></i><div class="sn"><b>{w["name"]}</b><em>{w["start"]}–{w["end"]} · 平均心率 {w["avg_hr"]}</em></div>'
                 f'<div class="sv">{w["min"]}<small> 分钟 · {w["kcal"]} 千卡</small></div></div>' for w in ex['workouts'])
    tr_chips = ''.join(f'<span class="tag">{x}</span>' for x in ex['train'])
    train_html = (wo or '<div class="note" style="margin:0 0 6px">手表昨天没有记录到专门的运动</div>') + \
                 (f'<div class="tags">{tr_chips}</div>' if tr_chips else '<div class="note">昨天没有训练记录</div>')
    if ex.get('activity_partial'):
        train_html += '<div class="note">活动指标仅统计已同步明细，未确认全天覆盖。</div>'
    # 吃
    if ex['meals']:
        food_html = ''.join(f'<div class="sig"><i class="dot" style="background:#4ade80"></i><div class="sn"><b>{t}</b><em>{f}</em></div></div>' for t, f in ex['meals'])
    else:
        food_html = '<div class="note" style="margin:0">昨天没有饮食记录。吃饭时随手拍照发我，这里就会列出每餐。</div>'
    rule = (chip('出现 ' + '、'.join(ex['bad']), WARN) if ex['bad'] else (chip('无糖无酒 ✓', GOOD) if ex['meals'] else chip('未记录', '#8aa0b8')))
    # 趋势
    rate = fat_wk if fat_wk is not None else tr.get('weight_wk')
    st_col = {'on': GOOD, 'fast': WARN, 'slow': WARN, 'off': BAD}.get(tr['status'], '#8aa0b8')
    trend_line = (f'<b style="color:{st_col}">{tr["say"]}</b> · 近 2 周{"脂肪" if fat_wk is not None else "体重"}每周 <b>{rate:+.2f} kg</b>'
                  + (f' · 推算每天热量缺口约 <b>{tr["deficit"]}</b> 千卡' if tr.get('deficit', 0) > 0 else '')
                  + (f' · 按此速度约 <b>{tr["eta"]:%m-%d}</b> 到 {WEIGHT_GOAL:g} kg' if tr.get('eta') else '')) if rate is not None else tr['say']
    light = BAD if tr['status'] == 'off' else (WARN if flags else GOOD)
    first = ex.get('summary', '')
    return f'''<!DOCTYPE html><html><head><meta charset="utf-8"><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{width:760px;max-width:880px;margin:0 auto;background:#0b1522;color:#e6edf5;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue','PingFang SC',sans-serif;line-height:1.7}}
.hero{{position:relative;height:250px;overflow:hidden}} .hero svg{{position:absolute;inset:0;width:100%;height:100%}}
.hero .l{{position:absolute;left:30px;top:26px;right:150px}} .hero h1{{font-size:38px;font-weight:700;letter-spacing:1px}}
.hero .en{{font-size:16px;color:#8aa0b8;margin-top:-4px}} .hero .dt{{font-size:15px;color:#8aa0b8;margin-top:10px}}
.hero .r{{position:absolute;right:30px;top:24px;font-size:12.5px;color:#8aa0b8;line-height:1.4}} .hero .r hr{{border:0;border-top:1.5px solid #8aa0b8;margin-top:6px;width:96px}}
.wrap{{padding:0 18px 22px}}
.sum{{background:#13233a;border:1.5px solid #f2c14e66;border-radius:16px;padding:14px 16px;margin-top:-34px;position:relative;font-size:16px;font-weight:600;line-height:1.7}}
.sum small{{display:block;font-size:12px;color:#f2c14e;letter-spacing:.1em;font-weight:600;margin-bottom:2px}}
.panel{{background:#13233a;border:1.5px solid #22364f;border-radius:18px;padding:16px 16px;margin-top:16px}}
.pt{{display:flex;align-items:baseline;gap:12px;border-bottom:1.5px solid #22364f;padding-bottom:8px;margin-bottom:12px}}
.pt h2{{font-size:22px;color:#f2c14e;font-weight:600}} .pt span{{color:#8aa0b8;font-size:12.5px;flex:1}} .pt span.chip{{flex:none}} .pt .chip{{align-self:center;flex:none}} .pt>span.chip{{flex:none}}
.pap-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:10px}}
.pap-metric{{background:#0f1d30;border:1px solid #22364f;border-radius:12px;padding:12px}}
.pap-label{{font-size:13px;color:#c9d6e4}} .pap-value{{font-size:30px;font-weight:700}} .pap-value small{{font-size:12px;color:#8aa0b8;margin-left:5px;font-weight:400}} .pap-ref{{font-size:12px;color:#8aa0b8;margin-top:8px}}
.grid4{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}
.tile{{background:#13233a;border:1.5px solid #22364f;border-radius:14px;padding:11px 12px}}
.tile .h{{display:flex;align-items:center;gap:7px;font-weight:600;font-size:14px}}
.dot{{width:12px;height:12px;border-radius:50%;display:inline-block;flex:0 0 auto}}
.tile .v{{font-size:27px;font-weight:700;margin:2px 0 0 2px;white-space:nowrap}} .tile .v small{{font-size:12px;color:#8aa0b8;font-weight:400;margin-left:2px}}
.tile .f{{display:flex;justify-content:space-between;font-size:11px;color:#8aa0b8;gap:4px;white-space:nowrap}}
.g{{color:#4ade80}} .y{{color:#f2c14e}} .c{{color:#4cc9f0}}
.bar{{display:flex;height:20px;border-radius:10px;overflow:hidden;gap:2px;margin:2px 0 6px}}
.sig{{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #22364f;font-size:14px}} .sig:last-child{{border-bottom:none}}
.sig .dot{{width:11px;height:11px}} .sn{{flex:1}} .sn b{{font-weight:600;font-size:15px}} .sn em{{font-style:normal;color:#8aa0b8;font-size:12px;display:block}}
.sv{{text-align:right;font-size:18px;font-weight:700;white-space:nowrap}} .sv small{{font-size:11.5px;color:#8aa0b8;font-weight:400}}
.chip{{font-size:11.5px;font-weight:700;padding:2px 8px;border-radius:8px;border:1px solid;flex:none}}
.tags{{margin-top:8px;font-size:12px;color:#8aa0b8}} .tags b{{font-weight:600;margin-right:6px}}
.tag{{display:inline-block;background:#0b1522;border:1px solid #22364f;border-radius:8px;padding:1px 9px;margin:3px 6px 3px 0;font-size:13px;color:#e6edf5}}
.note{{font-size:11.5px;color:#6f87a6;margin-top:8px}}
.trend{{font-size:14px;color:#8aa0b8;margin-top:4px}}
.light{{display:flex;gap:16px;align-items:center}} .bulb{{width:60px;height:60px;border-radius:50%;flex:none;background:{light};box-shadow:0 0 0 6px {light}22,0 0 30px {light}66}}
.light b{{font-size:19px;color:{light};display:block}} .light span{{font-size:13px;color:#8aa0b8}}
.foot{{display:flex;justify-content:space-between;color:#6f87a6;font-size:11px;margin-top:14px}}
</style></head><body>
{HERO}
<div class="l"><h1>康复晨报</h1><div class="en">Morning Recovery Report · 吃 · 睡 · 练</div>
<div class="dt">{today.year}.{today.month:02d}.{today.day:02d} &nbsp; 周{WD[today.weekday()]}</div></div>
<div class="r">Recover<br>Rebuild<br>Return<hr></div></div>
<div class="wrap">
<div class="sum"><small>一句话总评</small>{first}</div>
<div class="panel"><div class="pt"><h2>今日总览</h2><span>Today · 和过去 7 天平均比</span></div><div class="grid4">{tiles}</div></div>
<div class="panel"><div class="pt"><h2>趋势</h2><span>Fat-loss Track · 近 2 周早晨体重</span></div>{weight_svg(tr['points'])}
<div class="trend">{trend_line}</div><div class="note">只看早上的体重；每公斤脂肪约 7700 千卡，用体重走势反推热量缺口，比拍照估热量准。</div></div>
<div class="panel"><div class="pt"><h2>睡</h2><span>Sleep · {s['sleepStart'][11:16]} 入睡 → {s['sleepEnd'][11:16]} 醒来</span></div><div class="note">以下是整晚阶段时长占比，不是分期时间轴。</div><div class="bar">{bar}</div>{rows}{cp_html}</div>
<div class="panel"><div class="pt"><h2>练</h2><span>Move · 昨天 · {ex.get('energy_label', '总消耗')} {ex.get('energy_text', '未同步')}</span></div>{train_html}</div>
<div class="panel"><div class="pt"><h2>吃</h2><span>Eat · 昨天</span>{rule}</div>{food_html}</div>
<div class="panel"><div class="pt"><h2>今日灯号</h2><span>Today's Call</span></div>
<div class="light"><div class="bulb"></div><div><b>{verdict}</b></div></div></div>
<div class="foot"><span>▲ fit / 康复日志</span><span>Recover · Rebuild · Return</span></div>
</div></body></html>'''


def render(daily, today, verdict, flags, weight, ex):
    """按实际内容高度渲染；使用独立 Chrome 配置目录，避免与正在运行的浏览器争用配置。"""
    OUT.mkdir(parents=True, exist_ok=True)
    base = OUT / f'{today}'
    document = html(daily, today, verdict, flags, weight, ex)
    (OUT / f'{today}.sleep.json').write_text(json.dumps({**ex.get('sleep_comparison', {}), 'stage_comparison': ex.get('stage_comparison', {})}, ensure_ascii=False, indent=2, allow_nan=False))
    measure = "<script>document.body.setAttribute('data-report-height',document.documentElement.scrollHeight)</script>"
    page = f'{base}.html'
    Path(page).write_text(document.replace('</body>', measure + '</body>'))
    profile = tempfile.mkdtemp(prefix='fitbox-chrome-')
    common = [CHROME, '--headless=new', '--hide-scrollbars', '--no-pdf-header-footer',
              f'--user-data-dir={profile}', '--no-first-run', '--no-default-browser-check',
              '--disable-extensions', '--disable-background-networking']
    try:
        measured = subprocess.run(common + ['--dump-dom', '--window-size=760,900', f'file://{page}'],
                                  capture_output=True, text=True, timeout=60, check=True)
        match = re.search(r'data-report-height="(\d+)"', measured.stdout)
        if not match:
            raise RuntimeError('无法测量日报高度，保留文字发送回退')
        css_h = int(match.group(1)) + 10
        png, pdf = f'{base}.png', f'{base}.pdf'
        subprocess.run(common + [f'--screenshot={png}', f'--window-size=760,{css_h}',
                                '--force-device-scale-factor=2', f'file://{page}'],
                       capture_output=True, timeout=60, check=True)
        from PIL import Image
        with Image.open(png) as source:
            im = source.convert('RGB')
        # Telegram sendPhoto 对宽高总和有限制；保持比例，不截去内容。
        if sum(im.size) > 9900:
            scale = 9900 / sum(im.size)
            im = im.resize((int(im.width * scale), int(im.height * scale)), Image.Resampling.LANCZOS)
        im.save(png)
        tmp = f'{base}.pdf.html'
        Path(tmp).write_text(document.replace('</style>', f'@page{{size:760px {css_h}px;margin:0}}</style>'))
        try:
            subprocess.run(common + [f'--print-to-pdf={pdf}', f'file://{tmp}'],
                           capture_output=True, timeout=60, check=True)
        finally:
            os.remove(tmp)
        return png, pdf
    finally:
        shutil.rmtree(profile, ignore_errors=True)
