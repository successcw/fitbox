#!/usr/bin/env python3
"""从 ezShare WiFi SD 卡把 ResMed 睡眠机数据搬到本地。

ezShare 卡没有官方 API，社区反向工程出来的只有三个端点：

    GET /client?command=version     版本信息
    GET /dir?dir=A:<path>           HTML 目录列表（得自己解析）
    GET /download?file=<path>       原始文件（支持 Range）

根目录写作 ``A:``，子目录写作 ``A:DATALOG``、``A:DATALOG\\20250101``。

因为是解析 HTML，不同固件的排版可能不一样 —— 第一次接卡先跑 ``--probe``，
把卡的原始响应打出来核对（用法见 docs/deployment.md）。

只依赖标准库：树莓派上没有 requests / bs4。

    cpap_pull.py --probe                         # 首次：打印原始响应 + 解析结果
    cpap_pull.py --list                          # 只看卡上有什么，不下载
    cpap_pull.py --dest ~/fitdata/cpap --net nm  # 切热点 → 拉新数据 → 切回来
    cpap_pull.py --dest ~/fitdata/cpap --dry-run --since 20250102
    cpap_pull.py --dest ~/fitdata/cpap --if-missing   # 今天已完整取得最近夜间数据就直接退出（重试档用）

定时（fit-cpap.timer）：每天 07:30，21:30 补漏；周六周日再加 08:00、09:00 两档重试，重试档带
--if-missing —— 起得晚的话机器还没把当晚文件写完，早那趟白跑，后面两档才有意义。
"""
import argparse, collections, hashlib, html as _html, json, os, re, shlex, shutil, signal, subprocess, sys, time, urllib.parse, urllib.request, uuid
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    from . import config
except ImportError:
    import config

BASE = 'http://192.168.4.1'          # ezShare 卡的固定网关
SSID = 'ez Share'                    # 出厂 SSID（有的克隆卡是 ezshare）
PSK = '88888888'                     # 出厂密码，八个 8
ROOT = 'A:'                          # 卡上的根

Entry = collections.namedtuple('Entry', 'name is_dir size rel')

# 卡上混着的非数据文件：macOS 的 ._ 影子文件、卡自己的配置、Windows 的回收站目录。
IGNORE = re.compile(r'^(\.|System Volume Information$|ezshare\.(cfg|ini)$|FOUND\.000$)', re.I)

# 目录页里的条目行，形态来自多个开源反向工程实现：
#   2025-01-01 22:54:40 1KB 20250101_225440_CSL.edf
#   2025-01-01 22:54:40 1KB 20250101_225440_CSL.crc
LINE = re.compile(
    r'^(?:(?P<date>\d{4}\s*-\s*\d{1,2}\s*-\s*\d{1,2})\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s+)?'
    r'(?P<size>\d+)\s*(?P<unit>KB|MB|B)\b\s*(?P<name>\S.*?)\s*$', re.I)


def log(msg):
    print(f'{datetime.now():%m-%d %H:%M:%S} {msg}', flush=True)


# 卡就在局域网里（192.168.4.1），一律直连。不走代理、也没有代理可配（网络出口交给
# 系统层的 TUN）；这里显式清空代理链，只是为了让残留的 HTTP_PROXY 环境变量失效 ——
# 一旦被塞进代理，卡根本收不到包。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------- 卡上的 HTTP

def _fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': 'fitbox-cpap/1'})
    with OPENER.open(req, timeout=timeout) as r:
        return r.read()


def card_version(base=BASE):
    """拿一句版本号，用来确认「现在真的连到卡上」而不是某个会回 200 的路由器。"""
    for url in (f'{base}/client?command=version', f'{base}/client'):
        try:
            return _fetch(url, timeout=8).decode('latin1', 'replace').strip()[:200]
        except Exception:
            continue
    return None


def _pre_lines(html):
    """把目录页剥成纯文本行：去标签、还原实体。

    标签是**直接删掉**而不是换成换行 —— ezShare 那一行的形态是
    ``2025-01-01 22:54:40 1KB <a href=...>20250101_225440_PLD.edf</a>``，
    元数据和文件名之间只隔着一个 <a>，换成换行就会把大小和名字拆成两行、大小就丢了。
    """
    m = re.search(r'<pre[^>]*>(.*?)</pre>', html, re.I | re.S)
    body = m.group(1) if m else html
    body = re.sub(r'<script.*?</script>', '', body, flags=re.I | re.S)
    body = re.sub(r'<br\s*/?>', '\n', body, flags=re.I)
    body = re.sub(r'<[^>]+>', '', body)
    text = _html.unescape(body)
    return [l.strip() for l in text.splitlines() if l.strip()]


def _basename(path):
    return re.split(r'[\\/]+', path.strip().rstrip('\\/'))[-1]


def _rel_of(remote_dir, name):
    """卡上的路径 → 本地相对路径（统一用 /）。

    ``A:`` → ``STR.edf``；``A:DATALOG`` → ``DATALOG/20250101``。
    注意 download?file= 用的是**不带 A: 前缀**的相对路径（STR.EDF、DATALOG\\20250101\\x.edf），
    而 dir?dir= 才带前缀，两者不能混用。
    """
    d = remote_dir[2:] if remote_dir.upper().startswith('A:') else remote_dir
    d = d.strip('\\/').replace('\\', '/')
    return f'{d}/{name}' if d else name


def parse_listing(html, remote_dir=ROOT):
    """把一页目录解析成 Entry 列表。

    两条腿走路，谁给信息用谁：
      1. ``<a href>`` 里的 ``dir=`` / ``file=`` 参数 —— 判定文件还是目录最可靠；
      2. ``<pre>`` 文本行里的 KB 大小 —— 用来做增量比对（卡只精确到 KB，且是向上取整）。
    两条都拿不到就返回空列表，让调用方把原始 HTML dump 出来人工看。
    """
    dirs, sizes = {}, {}

    for href in re.findall(r'href\s*=\s*["\']?([^"\'>\s]+)', html, re.I):
        href = _html.unescape(href)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        for key, is_dir in (('dir', True), ('file', False)):
            for raw in q.get(key, []):
                name = _basename(raw)
                if name and name not in ('.', '..'):
                    dirs[name] = is_dir

    for line in _pre_lines(html):
        if re.match(r'^(total|directory index|total entries|total size)\b', line, re.I):
            continue
        m = LINE.match(line)
        if m:
            unit = m.group('unit').upper()
            size = int(m.group('size')) * {'B': 1, 'KB': 1024, 'MB': 1024 * 1024}[unit]
            sizes[_basename(m.group('name'))] = size
        elif re.match(r'^(.+?)[\\/]\1$', line) or line.endswith(('/', '\\')):
            name = _basename(line)                   # 只有名字的目录行（DATALOG/ 或 DATALOG/DATALOG）
            if name:
                dirs.setdefault(name, True)

    out = []
    for name, is_dir in sorted(dirs.items()):
        out.append(Entry(name, is_dir, None if is_dir else sizes.get(name), _rel_of(remote_dir, name)))
    for name, size in sorted(sizes.items()):          # 没有 href 的固件，只靠文本行兜底
        if name not in dirs:
            out.append(Entry(name, False, size, _rel_of(remote_dir, name)))
    return out


def listdir(remote_dir, base=BASE):
    """列一个目录。remote_dir 用 ``A:`` 风格（``A:``、``A:DATALOG``）。"""
    url = f'{base}/dir?dir=' + urllib.parse.quote(remote_dir, safe='')
    html = _fetch(url).decode('latin1', 'replace')
    if 'REDIRECTFORM' in html or 'loginPass' in html:
        raise RuntimeError('卡返回欢迎/登录页，当前固件不支持此目录接口；不能当作空目录或成功')
    entries = parse_listing(html, remote_dir)
    if not entries:
        log(f'!! 解析不出任何条目：{url}（{len(html)} 字节）—— 跑 --probe 看原始响应')
    return entries


def join_remote(remote_dir, name):
    """拼子目录的 dir= 参数：A: + DATALOG → ``A:DATALOG``，再往下才是 ``A:DATALOG\\20250101``。"""
    return ('A:' + name) if remote_dir.upper() in ('A:', 'A:\\', 'A:/') else remote_dir.rstrip('\\/') + '\\' + name


def download(remote_file, dst, base=BASE, chunk=1 << 16):
    """流式下载到 dst。返回落盘字节数。"""
    url = f'{base}/download?file=' + urllib.parse.quote(remote_file, safe='')
    req = urllib.request.Request(url, headers={'User-Agent': 'fitbox-cpap/1'})
    got = 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    with OPENER.open(req, timeout=30) as r, open(dst, 'wb') as f:
        while True:
            b = r.read(chunk)
            if not b:
                break
            f.write(b)
            got += len(b)
    return got


# ---------------------------------------------------------------- 校验

def size_ok(got, reported):
    """卡报的大小是 KB 且向上取整，所以实际字节数落在 ((kb-1)*1024, kb*1024] 里。"""
    if reported is None:
        return got > 0
    lo = (reported // 1024 - 1) * 1024 + 1 if reported else 1
    return lo <= got <= reported


def edf_complete(path):
    """EDF 是否完整：头部声明的 头长 + 记录数×记录长度 == 文件字节数。

    睡眠机在会话进行中就会开始写文件 —— 此时头部已就位、数据还没写完。这种半截文件
    必须拒收，否则会被当成一夜的有效数据解析（和之前 health 推送被 scp 截断是同一类坑）。
    """
    try:
        size = path.stat().st_size
        with open(path, 'rb') as f:
            h = f.read(256)
            if len(h) < 256 or h[:1] != b'0':
                return False
            g = lambda a, b: h[a:b].decode('latin1').strip()
            hbytes, nrec, ns = int(g(184, 192)), int(g(236, 244)), int(g(252, 256))
            if ns <= 0 or not 256 <= hbytes <= size:
                return False
            if nrec <= 0:                       # 记录数不定，只能靠大小（调用方已查过）
                return True
            sh = f.read(ns * 256)
            if len(sh) < ns * 256:
                return False
        o = ns * (16 + 80 + 8 + 8 * 4 + 80)      # label/transducer/physdim/(min,max)×2/prefilter
        nsamp = []
        for i in range(ns):
            v = sh[o + i * 8:o + (i + 1) * 8].decode('latin1').strip()
            nsamp.append(int(v or 0))
        rec = sum(nsamp) * 2
        return rec > 0 and size == hbytes + nrec * rec
    except Exception:
        return False


# ---------------------------------------------------------------- 网络切换

NM_PROFILE = 'ezshare'


def _run(cmd, **kw):
    """跑外部命令；命令不存在或没权限时返回 rc=1，不要把调用方炸掉。"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, **kw)
    except (FileNotFoundError, PermissionError, OSError) as e:
        return subprocess.CompletedProcess(cmd, 1, '', str(e))


def _nm(*args):
    # systemd 的服务用户没有交互式 polkit 授权，切网必须使用免密 sudo。
    return _run(['sudo', '-n', 'nmcli', *args])


def pick_net(mode, iface='eth0'):
    """auto：插了网线就让 wlan0 空出来连卡（不打扰任何东西）；否则只能临时把 wlan0 切过去。

    非 Linux（也就是在 Mac 上）永远不要自作主张切 WiFi —— 那会把 Telegram 轮询和
    到树莓派的 SSH 一起打断，而 Mac 上根本没有 nmcli 那一套。
    """
    if mode != 'auto':
        return mode
    if sys.platform != 'linux':
        return 'none'
    try:
        return 'none' if Path(f'/sys/class/net/{iface}/carrier').read_text().strip() == '1' else 'nm'
    except OSError:
        return 'nm'


def _arm_watchdog(home, seconds):
    """独立 systemd timer 不属于拉取服务的 cgroup，主进程退出后仍能恢复网络。"""
    unit = 'fit-cpap-recover-' + uuid.uuid4().hex
    # 每次有独立 unit；不共享完成标记，避免上一次任务误取消本次兜底。
    script = ("for attempt in 1 2 3; do "
              f'nmcli --wait 20 con up {shlex.quote(home)} && exit 0; '
              'sleep 2; done; exit 1')
    result = _run(['sudo', '-n', 'systemd-run', '--quiet', '--collect',
                   '--unit=' + unit, '--on-active=' + str(int(seconds)) + 's',
                   '--timer-property=AccuracySec=1s',
                   '/bin/sh', '-c', script], timeout=20)
    if result.returncode:
        raise RuntimeError('无法建立独立网络恢复定时器，取消切网：' + result.stderr.strip())
    return unit


def net_up(mode, iface, ssid, psk, grace=900):
    """把 wlan0 切到卡的热点。返回一个「恢复原状」的可调用对象（幂等）。

    公开版仅使用独立恢复计时器，不启停外部网络监控或自动重启任务。
    若机器另有网络监控，应先确认其不会将预期离网视为故障。
    """
    if mode == 'none':
        return lambda: None
    if mode == 'nm':
        if not shutil.which('nmcli'):
            raise SystemExit('这台机器上没有 nmcli —— --net nm 是给树莓派/Linux 用的；'
                             'Mac 上请先把卡接到同一张网，或者用 --net macos')
        home = _nm('-g', 'GENERAL.CONNECTION', 'dev', 'show', iface).stdout.strip()
        if not home or home == '--' or home == NM_PROFILE:
            home = os.environ.get('FITBOX_HOME_CONNECTION', '')
        if not home or home == NM_PROFILE:
            raise RuntimeError('无法确定恢复网络，请设置 FITBOX_HOME_CONNECTION 后再切网')
        watchdog = _arm_watchdog(home, grace)
        _nm('con', 'delete', NM_PROFILE)
        _nm('con', 'add', 'type', 'wifi', 'ifname', iface, 'con-name', NM_PROFILE, 'ssid', ssid)
        args = ['con', 'modify', NM_PROFILE, 'ipv4.never-default', 'yes',
                'wifi.hidden', 'yes', 'wifi.powersave', '2',
                'ipv6.method', 'disabled', 'connection.autoconnect', 'no']
        if psk:                                     # 留空就是开放热点（有些克隆卡没有密码）
            args += ['wifi-sec.key-mgmt', 'wpa-psk', 'wifi-sec.psk', psk]
        _nm(*args)
        for attempt in range(3):
            scan = _nm('--wait', '10', '-t', '-f', 'SSID,BSSID', 'dev', 'wifi',
                       'list', 'ifname', iface, '--rescan', 'yes')
            prefix = ssid.replace(':', r'\:') + ':'
            for line in scan.stdout.splitlines():
                if line.startswith(prefix):
                    bssid = line[len(prefix):].replace(r'\:', ':')
                    if re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}', bssid):
                        _nm('con', 'modify', NM_PROFILE, 'wifi.bssid', bssid)
                        break
            # --wait 只限制 nmcli 等待，不会取消 NM 后台激活；给认证完整窗口。
            r = _nm('--wait', '60', 'con', 'up', NM_PROFILE)
            state = _nm('-g', 'GENERAL.STATE,GENERAL.CONNECTION', 'dev', 'show', iface)
            fields = state.stdout.strip().splitlines()
            if (state.returncode == 0 and len(fields) == 2
                    and fields[0].split()[0] == '100' and fields[1] == NM_PROFILE):
                break
            log(f'!! 连接卡第{attempt+1}次失败：{r.stderr.strip()[:120]}')
            # 超时后先明确取消仍在进行的激活，再重试，避免重入打断握手。
            stopped = _nm('--wait', '20', 'con', 'down', NM_PROFILE)
            if stopped.returncode:
                # 已经失败或回到家庭网络时，down 会报 not active；这不应阻止重试。
                after = _nm('-g', 'GENERAL.STATE,GENERAL.CONNECTION', 'dev', 'show', iface)
                fields = after.stdout.strip().splitlines()
                inactive = (after.returncode == 0 and len(fields) == 2
                            and fields[0].split()[0] in {'10', '20', '30', '100', '120'}
                            and fields[1] != NM_PROFILE)
                if not inactive:
                    log('!! 未能确认取消卡连接，停止重试，交给收尾及独立兜底恢复')
                    break

        def restore():
            recovered = False
            try:
                _nm('--wait', '20', 'con', 'down', NM_PROFILE)
                r = _nm('--wait', '20', 'con', 'up', home)
                active = _nm('-g', 'GENERAL.CONNECTION', 'dev', 'show', iface)
                recovered = r.returncode == 0 and active.stdout.strip() == home
                if not recovered:
                    log('!! 家庭网络恢复失败，独立定时器将继续恢复')
            finally:
                # 仅恢复成功后取消独立计时器；不启停机器上其他网络服务。
                if recovered:
                    _run(['sudo', '-n', 'systemctl', 'stop',
                          watchdog + '.timer', watchdog + '.service'])
        return restore
    if mode == 'macos':
        if not shutil.which('networksetup'):
            raise SystemExit('--net macos 只能在 macOS 上用')
        # 新版 macOS 可能隐藏当前 SSID；不知道原网络时不能盲切后再猜首选网络。
        current = _run(['networksetup', '-getairportnetwork', iface])
        prefix = 'Current Wi-Fi Network: '
        if current.returncode or not current.stdout.strip().startswith(prefix):
            raise SystemExit('无法可靠读取 Mac 当前 Wi-Fi，已取消自动切网；请手动连接后用 --net none')
        home = current.stdout.strip()[len(prefix):]

        def restore():
            result = _run(['networksetup', '-setairportnetwork', iface, home])
            active = _run(['networksetup', '-getairportnetwork', iface])
            if result.returncode == 0 and active.stdout.strip() == prefix + home:
                log(f'已验证切回 {home}')
            else:
                log('!! 未能验证恢复原 Wi-Fi，不能视为恢复成功')

        result = _run(['networksetup', '-setairportnetwork', iface, ssid, psk])
        if result.returncode or re.search(r'failed|error|could not', result.stdout + result.stderr, re.I):
            restore()
            raise RuntimeError('Mac 连接卡失败')
        return restore
    raise SystemExit(f'未知 --net {mode}')


# ---------------------------------------------------------------- 同步

def collect_wanted(since, kinds, base):
    """列出要拉的文件。只深挖 DATALOG 里 >= since 的日期目录 —— 全量列目录要 7000+ 次请求。"""
    wanted, dirs = [], []

    root = listdir(ROOT, base)
    datalog = None
    for e in root:
        if e.is_dir:
            dirs.append(e)
            if e.name.upper() == 'DATALOG':
                datalog = e
        elif not IGNORE.match(e.name) and (kinds is None or Path(e.name).suffix.lstrip('.').upper() in kinds):
            wanted.append(e)

    for e in dirs:                                  # SETTINGS 一次列完；DATALOG 只看最近
        if e.name.upper() == 'DATALOG':
            continue
        for sub in listdir(join_remote(ROOT, e.name), base):
            if not sub.is_dir and not IGNORE.match(sub.name):
                wanted.append(sub)

    if datalog is None:                             # 有的固件根目录不列子目录，直接去敲
        remote = join_remote(ROOT, 'DATALOG')
    else:
        remote = join_remote(ROOT, datalog.name)
    nights = [e for e in listdir(remote, base) if e.is_dir]
    if nights:
        for n in sorted(nights, key=lambda x: x.name):
            if since and n.name.isdigit() and n.name < since:
                continue
            for f in listdir(join_remote(remote, n.name), base):
                if f.is_dir or IGNORE.match(f.name):
                    continue
                if kinds and Path(f.name).suffix.lstrip('.').upper() not in kinds:
                    continue
                wanted.append(f)
    return wanted


def stale_locals(dest):
    """本地已有、但内容已经不完整的 EDF（掉电写坏、磁盘出错）。

    增量只盯最近一夜，更早的坏文件不会被发现 —— 这个函数直接扫本地，
    不需要列卡的目录，点名重下就行。
    """
    dest = Path(dest).expanduser()
    return [Entry(p.name, False, None, str(p.relative_to(dest)))
            for p in sorted(dest.rglob('*.edf')) if not edf_complete(p)]


def already_have(dst, e):
    # 汇总、日志和设置会原地更新，字节数相同也不能认为内容相同。
    if not e.rel.upper().startswith('DATALOG/'):
        return False
    if not dst.exists():
        return False
    got = dst.stat().st_size
    if not size_ok(got, e.size):
        return False
    if Path(e.name).suffix.lower() == '.edf' and not edf_complete(dst):
        return False
    return True


def sync(dest, since, kinds, base, force=False, dry=False, deadline=None, journal=None, verify=False):
    """拉新文件。先写 .part、验完再原子改名 —— 中途断掉不会留下半截文件占住位置。"""
    dest = Path(dest).expanduser()
    staging = dest / '.staging'
    wanted = collect_wanted(since, kinds, base)
    if verify:
        extra = {e.rel: e for e in stale_locals(dest)}
        for e in wanted:
            extra.pop(e.rel, None)
        if extra:
            log(f'--verify：本地另有 {len(extra)} 个 EDF 已损坏，重下')
        wanted += list(extra.values())
    new, skipped = [], 0
    for e in wanted:
        if e.is_dir or (not force and already_have(dest / e.rel, e)):
            skipped += 1
        else:
            new.append(e)

    log(f'卡上 {len(wanted)} 个文件，本地已有 {skipped}，待拉 {len(new)}'
        + (f'（since {since}）' if since else ''))
    if dry:
        for e in new:
            log(f'  would fetch  {e.rel}  {e.size or "?"} B')
        return 0

    staging.mkdir(parents=True, exist_ok=True)
    for tmp in staging.glob('*.part'):
        tmp.unlink()
    success = Path(journal).with_suffix('.success.json') if journal else None
    if success:
        success.unlink(missing_ok=True)
    landed, failed = 0, 0
    for e in new:
        if deadline and time.time() > deadline:
            log('!! 到达时间上限，剩下的下次再拉')
            failed += 1
            break
        tmp = staging / (e.name + '.part')
        remote = e.rel.replace('/', '\\')             # download?file= 不带 A: 前缀
        try:
            got = download(remote, tmp, base)
        except Exception as ex:
            log(f'  {e.rel}: 下载失败 {type(ex).__name__}: {str(ex)[:80]}')
            failed += 1
            continue
        # 注意：判扩展名要用原始文件名 —— 暂存文件叫 xxx.edf.part，suffix 是 .part，
        # 拿它去判就永远判不到 .edf，半截的 EDF 会被当成完整文件放行。
        is_edf = Path(e.name).suffix.lower() == '.edf'
        if not size_ok(got, e.size) or (is_edf and not edf_complete(tmp)):
            why = 'size' if not size_ok(got, e.size) else 'a half-written EDF'
            log(f'  {e.rel}: {got} B 不完整（{why}），留待下次')
            tmp.unlink(missing_ok=True)
            failed += 1
            continue
        out = dest / e.rel
        out.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, out)
        landed += 1
        log(f'  ↓ {e.rel}  {got:,} B')
        if journal:
            with open(journal, 'a') as f:
                f.write(json.dumps({'ts': f'{datetime.now():%Y-%m-%d %H:%M:%S}',
                                    'file': f'cpap/{e.rel}', 'bytes': got,
                                    'sha256': hashlib.sha256(out.read_bytes()).hexdigest()}, ensure_ascii=False) + '\n')
    for tmp in staging.glob('*.part'):
        tmp.unlink()
    log(f'完成：新落地 {landed}，跳过 {skipped}，失败 {failed}')
    yesterday = (date.today() - timedelta(days=1)).strftime('%Y%m%d')
    recent = [e for e in wanted if e.rel.upper().startswith('DATALOG/')
              and len(e.rel.split('/')) == 3 and e.rel.split('/')[1] >= yesterday
              and e.name.lower().endswith('.edf') and edf_complete(dest / e.rel)]
    if success and not failed and recent:
        tmp = success.with_suffix('.part')
        tmp.write_text(json.dumps({'date': str(date.today()), 'files': len(recent)}))
        tmp.replace(success)
    return -1 if failed else landed


def newest_night(dest):
    """本地已有的最新日期目录，用来决定 --since。"""
    d = Path(dest).expanduser() / 'DATALOG'
    nights = [p.name for p in d.iterdir() if p.is_dir() and p.name.isdigit()] if d.is_dir() else []
    return max(nights) if nights else None


def pulled_today(journal):
    """只认完整同步的成功标记；单个文件落地不代表整夜已取齐。"""
    j = Path(journal).expanduser().with_suffix('.success.json')
    try:
        record = json.loads(j.read_text())
        return record.get('date') == str(date.today()) and record.get('files', 0) > 0
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def legacy_pulled_today(journal):
    """旧固件仅认可昨夜 STR 会话已核验的独立标记，不冒充目录镜像。"""
    path = Path(journal).with_suffix('.legacy-success.json')
    try:
        value = json.loads(path.read_text())
        yesterday = (date.today()-timedelta(days=1)).strftime('%Y%m%d')
        return (value.get('date') == str(date.today()) and value.get('night') == yesterday
                and value.get('scope') == 'str-session-coverage')
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def record_legacy_success(journal, result):
    yesterday = (date.today()-timedelta(days=1)).strftime('%Y%m%d')
    path = Path(journal).with_suffix('.legacy-success.json')
    path.unlink(missing_ok=True)
    if (result.get('window_complete') and not result.get('errors')
            and yesterday in result.get('treatment_dates', [])):
        tmp = path.with_suffix('.part')
        tmp.write_text(json.dumps({'date': str(date.today()), 'night': yesterday,
                                   'scope': 'str-session-coverage', 'mirror_complete': False}))
        tmp.replace(path)
        log('昨夜 STR 所列治疗会话核验通过，今天后续档跳过；非完整目录枚举')


def sync_legacy_summary(dest, journal, base=BASE, dry=False):
    """旧照片固件只能可靠获取已知汇总；不设置完整目录同步的成功标记。"""
    url = base + '/download?fname=STR.EDF&fdir=..'
    if dry:
        log(f'只同步汇总（不含波形目录）：{url}')
        return 0
    dest = Path(dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / 'STR.edf.part'
    try:
        tmp.write_bytes(_fetch(url, timeout=30))
        if not edf_complete(tmp):
            raise RuntimeError('旧固件返回的 STR.EDF 不完整，保留原文件')
        content = tmp.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        tmp.replace(dest / 'STR.edf')
        with open(journal, 'a') as f:
            f.write(json.dumps({'ts': f'{datetime.now():%Y-%m-%d %H:%M:%S}',
                                'file': 'cpap/STR.edf', 'bytes': len(content),
                                'sha256': digest}) + '\n')
        log(f'汇总已更新：STR.edf {len(content):,} 字节；尚未同步 DATALOG，不能标记整夜完成')
        return 0
    finally:
        tmp.unlink(missing_ok=True)


def probe(base):
    """第一次接卡时跑这个：把原始响应摊开，好核对解析器对不对得上这块卡的固件。"""
    log(f'探测 {base}')
    for label, url in (('version', f'{base}/client?command=version'),
                       ('root   ', f'{base}/'),
                       ('dir A: ', f'{base}/dir?dir=A:')):
        try:
            body = _fetch(url, timeout=10)
            head = body[:600].decode('latin1', 'replace').replace('\r', '')
            log(f'--- {label}  {len(body)} B ---\n{head}')
        except Exception as e:
            log(f'--- {label}  失败：{type(e).__name__}: {e}')
    entries = listdir(ROOT, base)
    log(f'--- 解析出 {len(entries)} 条 ---')
    for e in entries[:40]:
        log(f'   {"DIR " if e.is_dir else "FILE"} {e.name:40s} {e.size if e.size is not None else ""}')


def main():
    ap = argparse.ArgumentParser(description='从 ezShare WiFi SD 卡拉 ResMed 数据')
    ap.add_argument('--dest', default=str(Path.home() / 'fitdata' / 'cpap'), help='落盘目录')
    ap.add_argument('--base-url', default=BASE)
    ap.add_argument('--since', help='只拉这个日期之后的夜目录（YYYYMMDD）；默认接上次进度')
    ap.add_argument('--days', type=int, default=30, help='没有进度时往回拉多少天')
    ap.add_argument('--kinds', help='只拉这些扩展名，逗号分隔，如 edf,crc')
    ap.add_argument('--net', default='auto', choices=['auto', 'none', 'nm', 'macos'],
                    help='怎么让卡可达：auto=有网线就不动网络 / none=已同网 / nm=切热点 / macos')
    ap.add_argument('--iface', default='wlan0')
    ap.add_argument('--ssid', default=SSID)
    ap.add_argument('--psk', default=PSK)
    ap.add_argument('--journal', help='每落地一个文件追加一行 JSON（给 Mac 端消费）')
    ap.add_argument('--max-minutes', type=float, default=8, help='离网窗口上限，超了就收工')
    ap.add_argument('--grace', type=float, help='兜底守护等待秒数；默认 = max-minutes + 150。'
                                                '脚本被杀时，它到这个点会把家里的网无条件接回来')
    ap.add_argument('--verify', action='store_true', help='顺手复查本地已有的 EDF，坏的重新拉')
    ap.add_argument('--legacy-scan', action='store_true',
                    help='旧固件：按STR治疗时间探测日期目录内的长文件名，不依赖目录接口')
    ap.add_argument('--summary-only', action='store_true',
                    help='旧照片固件：仅下载 STR.EDF 汇总，不代表完整目录同步成功；卡上需有空 DCIM')
    ap.add_argument('--if-missing', action='store_true',
                    help='今天已完整取得最近夜间数据就立刻退出（给 08:00/09:00 这种重试档用）')
    ap.add_argument('--force', action='store_true', help='已存在也重下')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--probe', action='store_true')
    a = ap.parse_args()

    if a.probe:
        return probe(a.base_url)
    if a.list:
        for e in collect_wanted(None, None, a.base_url):
            log(f'   {"DIR " if e.is_dir else "FILE"} {e.rel:52s} {e.size if e.size is not None else ""}')
        return

    journal = Path(a.journal).expanduser() if a.journal else Path(a.dest).expanduser().parent / 'cpap.jsonl'
    journal.parent.mkdir(parents=True, exist_ok=True)

    legacy_mode = a.legacy_scan or (Path(a.dest).expanduser() / '.legacy-probes.json').exists()

    # 重试档：今天已经抓到过，就不再白跑一趟（也不去切网络）
    if a.if_missing and not a.force and (legacy_pulled_today(journal) if legacy_mode else pulled_today(journal)):
        log('今天已取得并核验昨夜数据，这一档跳过')
        return 0

    since = a.since or newest_night(a.dest)
    if since and not a.since:
        since = (datetime.strptime(since, '%Y%m%d') - timedelta(days=1)).strftime('%Y%m%d')
    if not since:
        since = (date.today() - timedelta(days=a.days)).strftime('%Y%m%d')

    kinds = {k.strip().lstrip('.').upper() for k in a.kinds.split(',')} if a.kinds else None

    restore, deadline = None, time.time() + a.max_minutes * 60
    mode = pick_net(a.net)
    if a.net == 'auto' and sys.platform == 'linux':
        log(f'--net auto → {mode}（{"网线通着，wlan0 不用切" if mode == "none" else "只有一张无线网卡，得临时切过去"}）')

    def cleanup():
        if restore:
            restore()

    # ssh 断线会送 SIGHUP，systemd 收工送 SIGTERM —— 都得先把网络还回去再走
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: (cleanup(), sys.exit(1)))
    try:
        if mode == 'none' and card_version(a.base_url) is None:
            log('连不上卡。要么给树莓派接网线并把 wlan0 留给卡，要么用 --net nm 临时切热点')
            return 1
        grace = a.grace or (a.max_minutes * 60 + 150)
        restore = net_up(mode, a.iface, a.ssid, a.psk, grace=grace)
        v, give_up = None, time.time() + min(90, a.max_minutes * 60)
        while time.time() < give_up:                # 切完等 DHCP 和卡的 web 起来
            v = card_version(a.base_url)
            if v is not None:
                break
            time.sleep(2)
        if v is None:
            log('热点连上了但卡没应答，收工')
            return 1
        log(f'卡：{v.splitlines()[0][:80]}')
        if not a.summary_only and (legacy_mode or 'LZ1001EDPG:4.4.0' in v):
            try:
                from . import cpap_legacy
            except ImportError:
                import cpap_legacy
            if a.dry_run:
                log('旧固件将按 STR 的日期及使用时间生成候选文件名')
                return 0
            def reconnect(remaining):
                if mode != 'nm' or remaining < 5:
                    return False
                log('卡的连接中断，重新连接后从已完成的位置继续')
                return _nm('--wait', str(min(20, int(remaining))), 'con', 'up', NM_PROFILE).returncode == 0
            if not a.since and newest_night(a.dest) is None:
                since = (date.today()-timedelta(days=min(a.days, 2))).strftime('%Y%m%d')
            result = cpap_legacy.sync(a.dest, journal, base=a.base_url, since=since,
                                      deadline=time.monotonic()+max(0, deadline-time.time()),
                                      include_off=False, log=log, reconnect=reconnect)
            record_legacy_success(journal, result)
            log('旧固件探测结果：' + json.dumps(result, ensure_ascii=False))
            return 0 if result.get('treatment_complete') and result['window_complete'] and not result['errors'] else 1
        if a.summary_only:
            return sync_legacy_summary(a.dest, journal, a.base_url, a.dry_run)
        return 0 if sync(a.dest, since, kinds, a.base_url, a.force, a.dry_run, deadline,
                         journal if not a.dry_run else None, a.verify) >= 0 else 1
    finally:
        cleanup()


if __name__ == '__main__':
    sys.exit(main())
