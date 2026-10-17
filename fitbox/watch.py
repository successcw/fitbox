#!/usr/bin/env python3
"""Mac side, event-driven: keep an SSH stream open to fitbox and ingest each new weigh-in the moment it lands.

Runs under launchd (org.fitbox.watch). Outbound SSH only; tries the home LAN first,
then Tailscale. On any drop it reconnects; replayed lines are skipped by pull.ingest's dedup.
"""
import hashlib, json, re, subprocess, threading, time
from datetime import datetime
from pull import FIT, HOSTS, ingest
from config import ssh_target
import health_daily
import tg_bot

HEALTH = FIT / '数据' / '健康' / 'raw'
CPAP = FIT / '呼吸机'
IP_LIKE = re.compile(r'^\d+\.\d+\.\d+\.\d+$')


def log(msg):
    print(f'{datetime.now():%m-%d %H:%M:%S} {msg}', flush=True)


def resolvable(host, timeout=1.5):
    """Cheap pre-flight before we spawn ssh.

    `fitbox.local` only exists on the home LAN, and mDNS fails *slowly*: ~5 s of dead waiting per
    attempt when the Mac is anywhere else. That wait is what turned a flaky link into a reconnect
    storm. Ask first with a hard timeout and skip the host if the name isn't there; a bare IP costs ~0.
    """
    if IP_LIKE.match(host):
        return True
    try:
        r = subprocess.run(['dscacheutil', '-q', 'host', '-a', 'name', host],
                           capture_output=True, text=True, timeout=timeout)
        return 'ip_address' in r.stdout
    except subprocess.TimeoutExpired:
        return False


def follow(host, name):
    """Yield each JSON line of ~/fitdata/<name> on fitbox, replaying from the start on every (re)connect."""
    cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
           '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3', ssh_target(host),
           f'mkdir -p ~/fitdata && touch ~/fitdata/{name} && tail -n +1 -F ~/fitdata/{name}']
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) as p:
        log(f'{name}: connected via {host}')
        for line in p.stdout:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def on_weight(host, rec):
    if ingest(rec):
        log(f"ingested {rec['ts']} {rec['weight']} kg")


def complete(path):
    """A push is only usable once it parses as JSON — a killed scp leaves a truncated file behind."""
    try:
        json.load(open(path))
        return True
    except (ValueError, OSError):
        return False


def on_health(host, rec):
    """Copy each Health Auto Export push to fit/数据/健康/raw once — whole or not at all.

    scp dies mid-transfer whenever the home mesh kicks the Pi (~every 70 s), which used to leave a
    half-written file on disk. Because we skip anything already present, that truncated copy blocked
    the push forever. Now we land the file in a .part name, check size *and* JSON, and only then
    rename it into place — so a broken attempt is retried on the next replay instead of poisoning the day.
    """
    dst = HEALTH / rec['file'].split('/')[-1]
    if dst.exists():
        if complete(dst):
            return
        log(f'{dst.name}: local copy is truncated, re-fetching')
        dst.unlink()
    HEALTH.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + '.part')
    r = subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                        f"{ssh_target(host)}:fitdata/{rec['file']}", str(tmp)], capture_output=True, text=True)
    got = tmp.stat().st_size if tmp.exists() else 0
    if r.returncode != 0 or got != rec['bytes'] or not complete(tmp):
        tmp.unlink(missing_ok=True)
        why = r.stderr.strip() or f'incomplete: {got} of {rec["bytes"]} bytes'
        log(f"health copy failed ({rec['ts']}): {why}")
        return
    tmp.replace(dst)
    log(f"health push {rec['ts']}: {len(rec.get('metrics') or {})} metrics, {rec['bytes']} bytes")
    health_daily.merge(dst)
    if health_daily.morning_report():
        log('morning report sent')
    # 睡眠到了但卡数据还没到 → 晨报会先压着（health_daily.morning_report 里的 wait_cpap），
    # 等 on_cpap 那边把 STR.edf 搬回来，它自己会再喊一次。


def on_cpap(host, rec):
    """把树莓派从 WiFi SD 卡拉到的睡眠机文件搬进 呼吸机/。

    树莓派那边已经做过 EDF 完整性校验（头长 + 记录数×记录长度 == 文件大小），所以这里
    只认字节数 —— 但同样要「整份落地，否则不落」：scp 被 mesh 断在半路会留半截文件，
    而 cpap_night() 读 STR.edf 时不会检查完整性，半截文件会静默产出错的 AHI。
    """
    rel = rec['file'].split('cpap/', 1)[-1]
    dst = CPAP / rel
    if dst.exists() and dst.stat().st_size == rec['bytes']:
        if not rec.get('sha256') or hashlib.sha256(dst.read_bytes()).hexdigest() == rec['sha256']:
            return
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + '.part')
    r = subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                        '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
                        f"{ssh_target(host)}:fitdata/{rec['file']}", str(tmp)], capture_output=True, text=True)
    got = tmp.stat().st_size if tmp.exists() else 0
    hash_bad = bool(rec.get('sha256') and tmp.exists()
                    and hashlib.sha256(tmp.read_bytes()).hexdigest() != rec['sha256'])
    if r.returncode != 0 or got != rec['bytes'] or hash_bad:
        why = r.stderr.strip() or ('内容校验失败' if hash_bad else f'{got} of {rec["bytes"]} bytes')
        tmp.unlink(missing_ok=True)
        log(f'cpap {rel}: 拷贝失败（{why}）')
        return
    tmp.replace(dst)
    log(f'cpap {rel}  {got:,} B')
    if health_daily.morning_report():
        log('morning report sent（呼吸机数据已到齐）')


def report_watcher():
    """兜底：卡没插 / 树莓派没网时，呼吸机数据可能一天都不到。

    watch 是纯事件驱动的 —— 没有新数据就没有回调，所以「等到 deadline 还没动静就别等了」
    这件事得有个定时器来喊。每 5 分钟看一眼，走的是同一个 morning_report，
    已发过的日子由 reported.txt 拦住，不会重复推。
    """
    while True:
        try:
            if datetime.now().strftime('%H:%M') >= health_daily.REPORT_DEADLINE:
                if health_daily.morning_report():
                    log(f'日报已发（{health_daily.REPORT_DEADLINE} 兜底，呼吸机数据未到也照发）')
        except Exception as e:
            log(f'日报兜底检查出错：{e}')
        time.sleep(300)


def run(name, handler):
    """Keep one stream open.

    Routes whose name doesn't resolve are skipped outright (see resolvable()). If connections keep
    dying on us the moment they open — a kicked Wi-Fi link, the Pi rebooting — we back off instead of
    retrying every 30 s all day. Any session that lasts half a minute resets the clock, so the normal
    home case (the mesh router drops us every ~70 s) stays on the fast 30 s retry.
    """
    wait, quick, skipped_before = 30, 0, None
    while True:
        skipped = [h for h in HOSTS if not resolvable(h)]
        if skipped != skipped_before:
            log(f'{name}: 跳过 {"、".join(skipped)}（名字解析不了）' if skipped
                else f'{name}: {len(HOSTS)} 个地址都可用')
            skipped_before = skipped
        for host in HOSTS:
            if host in skipped:
                continue
            t0 = time.time()
            try:
                for rec in follow(host, name):
                    handler(host, rec)
            except Exception as e:
                log(f'{name} {host}: {e}')
            if time.time() - t0 >= 30:
                wait, quick = 30, 0                 # a real session — forget the failures
            else:
                quick += 1
        if quick:
            wait = min(30 * 2 ** (quick - 1), 300)
            log(f'{name}: 连续 {quick} 次连接即断，退避 {wait} 秒')
        time.sleep(wait)


def main():
    threading.Thread(target=run, args=('health.jsonl', on_health), daemon=True).start()
    threading.Thread(target=run, args=('cpap.jsonl', on_cpap), daemon=True).start()
    threading.Thread(target=report_watcher, daemon=True).start()
    threading.Thread(target=tg_bot.run, daemon=True).start()
    run('scale.jsonl', on_weight)


if __name__ == '__main__':
    main()
