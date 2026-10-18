#!/usr/bin/env python3
"""Render the README architecture and hardware diagrams as SVG + PNG.

The output is committed so GitHub does not need Mermaid or a renderer. Regenerate with:

    python3 scripts/render_diagrams.py

Requires Google Chrome / Chromium; set CHROME_BIN if it is not in the default macOS path.
"""
import html
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs' / 'diagrams'
CHROME = os.environ.get('CHROME_BIN', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
FONT = "-apple-system,BlinkMacSystemFont,'PingFang SC','Noto Sans CJK SC','Segoe UI',sans-serif"

THEMES = {
    'light': dict(bg='#f7f5f0', dot='#e9e3d8', card='#ffffff', border='#e4ddd1',
                  text='#2d2a26', muted='#6f6a62', zone='#fbf7e8', zone_border='#e6dcc0',
                  accent='#2f6f8f', purple='#7c5cbf', orange='#c9772f', green='#2e7d52',
                  shadow='#00000014'),
    'dark': dict(bg='#0b1522', dot='#15243a', card='#13233a', border='#22364f',
                 text='#e6edf5', muted='#8aa0b8', zone='#0f1d30', zone_border='#22364f',
                 accent='#4cc9f0', purple='#b48cf2', orange='#f59e42', green='#4ade80',
                 shadow='#00000066'),
}


def esc(value):
    return html.escape(str(value), quote=True)


def txt(x, y, value, size=13, fill='#000', weight='400', anchor='start', opacity=1):
    return (f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" '
            f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}" '
            f'opacity="{opacity}">{esc(value)}</text>')


def label(x, y, value, theme, color=None, size=11, anchor='middle'):
    """Small pill behind a connector label so lines do not cut through the text."""
    color = color or theme['muted']
    units = sum(1 if ord(ch) > 127 else 0.56 for ch in value)
    width = units * size + 12
    left = x - width / 2 if anchor == 'middle' else (x - width if anchor == 'end' else x)
    return (f'<rect x="{left:.1f}" y="{y - size - 3}" width="{width:.1f}" height="{size + 8}" '
            f'rx="6" fill="{theme["card"]}" stroke="{theme["border"]}" opacity="0.96"/>'
            + txt(x, y, value, size, color, '500', anchor))


def icon(kind, x, y, color):
    """Minimal 24x24 line icons; no external assets."""
    shapes = {
        'router': '<rect x="2" y="9" width="20" height="9" rx="2"/><path d="M6 9V4M18 9V4"/>'
                  '<circle cx="7" cy="13.5" r="1" fill="currentColor" stroke="none"/>'
                  '<path d="M11 13.5h6"/>',
        'watch': '<rect x="1" y="6" width="15" height="12" rx="3"/><path d="M4 6V3h9v3M4 18v3h9v-3"/>'
                 '<rect x="18" y="8" width="5" height="8" rx="1.5"/><path d="M19.5 8V5h2v3"/>',
        'mac': '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M1 20h22M9 20l1-4M15 20l-1-4"/>',
        'cpap': '<rect x="2" y="7" width="16" height="11" rx="3"/><path d="M18 12c3 0 3 4 0 4"/>'
                '<path d="M6 11h4M6 14h6"/><circle cx="5" cy="19" r="1" fill="currentColor" stroke="none"/>',
        'sd': '<path d="M4 2h11l5 5v15H4z"/><path d="M15 2v5h5"/><rect x="7" y="12" width="8" height="6" rx="1"/>'
              '<path d="M9 12v6M13 12v6"/>',
        'scale': '<rect x="2" y="5" width="20" height="15" rx="3"/><rect x="7" y="9" width="10" height="4" rx="1"/>'
                 '<path d="M7 16h10"/>',
        'pi': '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 5V2M12 5V2M17 5V2M7 19v3M12 19v3M17 19v3"/>'
              '<circle cx="8" cy="12" r="1.4" fill="currentColor" stroke="none"/><path d="M12 10h5M12 14h5"/>',
    }
    body = shapes.get(kind, shapes['pi'])
    return (f'<g transform="translate({x},{y})" color="{color}" stroke="{color}" '
            f'stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round">'
            f'{body}</g>')


def card(x, y, w, h, title, sub, kind, theme, color=None):
    color = color or theme['accent']
    subtitle = sub if sub else ''
    return (f'<g><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{theme["card"]}" '
            f'stroke="{theme["border"]}" filter="url(#shadow)"/>'
            + icon(kind, x + 16, y + h / 2 - 12, color)
            + txt(x + 52, y + 34, title, 16, theme['text'], '600')
            + txt(x + 52, y + 56, subtitle, 12, theme['muted'])
            + '</g>')


def pill(x, y, w, h, value, theme, color=None):
    color = color or theme['accent']
    return (f'<g><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{h / 2}" fill="{theme["card"]}" '
            f'stroke="{theme["border"]}" filter="url(#shadow)"/>'
            f'<circle cx="{x + 20}" cy="{y + h / 2}" r="4" fill="{color}"/>'
            + txt(x + 34, y + h / 2 + 5, value, 14, theme['text'], '600')
            + '</g>')


def panel(x, y, w, h, title, theme):
    return (f'<g><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="22" fill="{theme["zone"]}" '
            f'stroke="{theme["zone_border"]}" stroke-width="1.4"/>'
            + txt(x + 24, y + 32, title, 13, theme['muted'], '600')
            + '</g>')


def arrow(d, theme, color_key='accent', dash=None, marker=True):
    color = theme[color_key]
    dashed = f' stroke-dasharray="{dash}"' if dash else ''
    end = f' marker-end="url(#arrow-{color_key})"' if marker else ''
    return (f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round"{dashed}{end}/>')


def header(title, subtitle, theme, badge=None):
    out = [txt(60, 64, title, 30, theme['text'], '700'),
           txt(60, 90, subtitle, 13, theme['muted'])]
    if badge:
        width = sum(1 if ord(ch) > 127 else 0.58 for ch in badge) * 12 + 42
        x = 1200 - 60 - width
        out.append(f'<rect x="{x:.1f}" y="42" width="{width:.1f}" height="30" rx="15" '
                   f'fill="{theme["card"]}" stroke="{theme["border"]}"/>')
        out.append(f'<circle cx="{x + 16:.1f}" cy="57" r="4" fill="{theme["green"]}"/>')
        out.append(txt(x + 28, 62, badge, 12, theme['muted'], '500'))
    return ''.join(out)


def legend(theme, y=790):
    items = [('solid', '有线 / 数据'), ('dash', '无线'), ('dotted', '可选')]
    x = 90
    out = []
    for style, text_value in items:
        dash = {'solid': '', 'dash': ' stroke-dasharray="7 5"', 'dotted': ' stroke-dasharray="2 5"'}[style]
        out.append(f'<line x1="{x}" y1="{y}" x2="{x + 30}" y2="{y}" stroke="{theme["muted"]}" '
                   f'stroke-width="2"{dash}/>')
        out.append(txt(x + 40, y + 4, text_value, 11, theme['muted']))
        x += 40 + sum(1 if ord(ch) > 127 else 0.56 for ch in text_value) * 11 + 34
    out.append(txt(1200 - 60, y + 4, '采集在本地完成；只有日报内容发送到 Telegram。', 11,
                   theme['muted'], '400', 'end'))
    return ''.join(out)


def svg_doc(width, height, theme, body):
    markers = ''.join(
        f'<marker id="arrow-{key}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="{theme[key]}"/></marker>'
        for key in ('accent', 'purple', 'orange', 'green'))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img">'
            f'<defs><pattern id="dots" width="32" height="32" patternUnits="userSpaceOnUse">'
            f'<circle cx="1" cy="1" r="1" fill="{theme["dot"]}"/></pattern>'
            f'<filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">'
            f'<feDropShadow dx="0" dy="3" stdDeviation="5" flood-color="{theme["shadow"]}"/></filter>'
            f'{markers}</defs>'
            f'<rect width="{width}" height="{height}" fill="{theme["bg"]}"/>'
            f'<rect width="{width}" height="{height}" fill="url(#dots)"/>'
            f'{body}</svg>')


def hardware(theme):
    parts = [header('硬件连接', 'Hardware topology · 卡留在呼吸机内，树莓派只用无线读取',
                    theme, '本地优先 · 无云端依赖')]
    parts.append(card(70, 120, 230, 90, 'iPhone + Apple Watch', 'Health Auto Export', 'watch', theme, theme['purple']))
    parts.append(card(485, 120, 230, 90, '家庭路由器', 'Wi-Fi · 可选以太网', 'router', theme))
    parts.append(card(900, 120, 230, 90, 'Mac', '同步 · 分析 · 日报', 'mac', theme))
    parts.append(pill(420, 255, 360, 46, 'Tailscale 私有网络 / 家庭局域网', theme, theme['accent']))

    parts.append(arrow('M300 165 H362 Q372 165 372 175 V268 Q372 278 382 278 H420',
                       theme, 'purple', '6 6'))
    parts.append(label(382, 240, 'Tailscale', theme, theme['purple']))
    parts.append(arrow('M900 165 H838 Q828 165 828 175 V268 Q828 278 818 278 H780',
                       theme, 'accent', '6 6'))
    parts.append(label(825, 240, 'SSH / SCP', theme, theme['accent']))
    parts.append(arrow('M600 210 V255', theme, 'accent'))
    parts.append(label(612, 237, '家庭 Wi-Fi', theme, theme['accent'], anchor='start'))
    parts.append(arrow('M600 301 V610', theme, 'accent'))
    parts.append(label(612, 470, '局域网 / Tailscale', theme, theme['accent'], anchor='start'))

    parts.append(panel(60, 335, 1080, 420, '床边', theme))
    parts.append(txt(1090, 370, 'SD 卡始终留在呼吸机内', 12, theme['muted'], '400', 'end'))
    parts.append(card(800, 395, 280, 92, 'ResMed 呼吸机', '治疗数据写入 SD 卡', 'cpap', theme, theme['green']))
    parts.append(card(800, 535, 280, 92, 'ezShare Wi-Fi SD 卡', '卡自身热点', 'sd', theme, theme['orange']))
    parts.append(card(450, 610, 300, 100, '树莓派', 'Wi-Fi + BLE · 采集与索引', 'pi', theme, theme['accent']))
    parts.append(card(140, 535, 220, 92, '体脂秤', 'BLE 广播', 'scale', theme, theme['green']))

    parts.append(arrow('M940 487 V535', theme, 'green'))
    parts.append(label(952, 515, '供电 / 写入', theme, theme['green'], anchor='start'))
    parts.append(arrow('M800 581 H775 Q765 581 765 591 V650 Q765 660 755 660 H750',
                       theme, 'orange', '6 6'))
    parts.append(label(782, 612, '卡热点', theme, theme['orange'], anchor='end'))
    parts.append(arrow('M360 581 H410 V650 Q410 660 420 660 H450', theme, 'green', '6 6'))
    parts.append(label(408, 612, 'BLE', theme, theme['green'], anchor='end'))
    parts.append(txt(600, 735, '不通过 USB 读卡 · 不修改治疗设置', 11, theme['muted'], '400', 'middle'))
    parts.append(legend(theme, 790))
    return svg_doc(1200, 830, theme, ''.join(parts))


def architecture(theme):
    parts = [header('系统架构', 'Data flow · 三个数据源，一条本地分析链路', theme, '本地存储 · 单向上报')]

    parts.append(panel(40, 120, 290, 500, '数据源', theme))
    parts.append(panel(420, 120, 330, 500, '树莓派 · 采集与索引', theme))
    parts.append(panel(830, 120, 330, 500, 'Mac · 分析与输出', theme))

    parts.append(card(65, 170, 240, 90, 'ResMed + ezShare', 'Wi-Fi / HTTP · EDF', 'cpap', theme, theme['green']))
    parts.append(card(65, 295, 240, 90, 'iPhone + Apple Watch', 'Tailscale / REST', 'watch', theme, theme['purple']))
    parts.append(card(65, 420, 240, 90, '体脂秤', 'BLE 广播', 'scale', theme, theme['green']))

    parts.append(card(445, 170, 280, 90, '呼吸机采集', 'Wi-Fi 热点 · 校验后落盘', 'cpap', theme, theme['green']))
    parts.append(card(445, 295, 280, 90, '健康数据接收', 'Token 校验 · 原样保存', 'watch', theme, theme['purple']))
    parts.append(card(445, 420, 280, 90, '体脂秤监听', 'BLE · 体重 / 阻抗', 'scale', theme, theme['green']))
    parts.append(f'<rect x="445" y="540" width="280" height="48" rx="12" fill="{theme["card"]}" '
                 f'stroke="{theme["border"]}"/>'
                 + txt(585, 570, '本地文件 + JSONL 事件索引', 12, theme['text'], '500', 'middle'))

    parts.append(card(855, 170, 280, 80, '同步与校验', 'SSH / SCP · 哈希去重', 'mac', theme, theme['accent']))
    parts.append(card(855, 275, 280, 80, '睡眠对齐与统计', '手表分期 × 呼吸机实测', 'mac', theme, theme['accent']))
    parts.append(card(855, 380, 280, 80, '日报渲染', 'HTML → PNG / PDF', 'mac', theme, theme['accent']))
    parts.append(card(855, 485, 280, 70, 'Telegram', 'Bot API · 只发日报', 'mac', theme, theme['purple']))

    for y in (215, 340, 465):
        parts.append(arrow(f'M330 {y} H420', theme, 'accent'))
    parts.append(arrow('M750 340 H830', theme, 'accent'))
    parts.append(label(790, 330, 'SSH / SCP', theme, theme['accent']))
    for y in (250, 355, 460):
        parts.append(arrow(f'M995 {y} V{y + 25}', theme, 'accent'))

    parts.append(txt(1200 - 60, 660, '原始数据保留在树莓派与 Mac；手表分期是估计值。', 11,
                     theme['muted'], '400', 'end'))
    parts.append(txt(60, 660, '数据源 → 树莓派 → Mac → Telegram', 11, theme['muted']))
    return svg_doc(1200, 700, theme, ''.join(parts))


def render(name, svg, width, height):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f'{name}.svg').write_text(svg)
    page = OUT / f'{name}.html'
    page.write_text(f'<!doctype html><html><body style="margin:0">{svg}</body></html>')
    profile = tempfile.mkdtemp(prefix='fitbox-diagram-')
    try:
        subprocess.run([CHROME, '--headless=new', '--hide-scrollbars', '--no-first-run',
                        '--no-default-browser-check', '--disable-extensions',
                        f'--user-data-dir={profile}', '--force-device-scale-factor=2',
                        f'--screenshot={OUT / (name + ".png")}',
                        f'--window-size={width},{height}', f'file://{page}'],
                       check=True, capture_output=True, timeout=90)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        page.unlink(missing_ok=True)


def main():
    for theme_name, theme in THEMES.items():
        render(f'hardware-{theme_name}', hardware(theme), 1200, 830)
        render(f'architecture-{theme_name}', architecture(theme), 1200, 700)
    print('wrote', OUT)


if __name__ == '__main__':
    main()
