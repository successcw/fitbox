#!/usr/bin/env python3
"""Read-only deployment preflight for fitbox.

Prints a checklist; with --json prints one machine-readable object. It never changes the
machine, writes files, installs packages, or prints secret values.

Typical use:
    python3 scripts/preflight.py --role auto
    python3 scripts/preflight.py --role mac --require-config --json
"""
import argparse
import json
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
DEFAULT_CONFIG = HOME / '.config' / 'fitbox' / 'config.env'
CHROME_CANDIDATES = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
]
PROJECT_FILES = [
    'fitbox/config.py', 'fitbox/pull.py', 'fitbox/watch.py', 'fitbox/cpap_pull.py',
    'fitbox/health_daily.py', 'fitbox/health_card.py', 'fitbox/notify.py',
    'fitbox/health_receiver.py', 'requirements.txt', 'deploy/systemd/fit-cpap.service',
]
MAC_KEYS = ('FITBOX_HOSTS', 'FITBOX_SSH_USER', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'FITBOX_TIMEZONE')
PI_KEYS = ('FITBOX_TIMEZONE',)


def run(cmd, timeout=8, env=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env), ''
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)


def parse_config(path):
    """Parse NAME='value' lines without executing them. Returns {} on missing/unreadable."""
    values = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def check(checks, name, ok, detail, required=True, hint=''):
    status = 'pass' if ok else ('fail' if required else 'warn')
    checks.append({'name': name, 'status': status, 'required': required,
                   'detail': detail, 'hint': hint})


def check_project(checks):
    missing = [p for p in PROJECT_FILES if not (ROOT / p).exists()]
    check(checks, 'project files', not missing,
          'all expected files present' if not missing else 'missing: ' + ', '.join(missing),
          True, 'run from a complete clone of the repository')


def check_python(checks):
    version = platform.python_version()
    ok = sys.version_info >= (3, 12)
    check(checks, 'python', ok, version, True, 'install Python 3.12 or newer')


def check_config(checks, args, role):
    path = Path(args.config).expanduser() if args.config else (
        Path(os.environ.get('FITBOX_CONFIG', '')).expanduser() if os.environ.get('FITBOX_CONFIG') else
        (ROOT / 'config.local.env' if (ROOT / 'config.local.env').exists() else DEFAULT_CONFIG))
    if not path.exists():
        check(checks, 'config file', False, f'not found: {path}',
              args.require_config, 'copy config.example.env to the path and chmod 600')
        return path, {}
    mode = stat.S_IMODE(path.stat().st_mode)
    check(checks, 'config permissions', not (mode & 0o077), oct(mode), False,
          'chmod 600 so other users cannot read tokens')
    values = parse_config(path)
    check(checks, 'config parse', bool(values), f'{len(values)} keys', True,
          'use NAME=\'value\' lines; do not use export or command substitution')
    keys = MAC_KEYS if role == 'mac' else PI_KEYS
    missing = [k for k in keys if not values.get(k)]
    check(checks, 'config required keys', not missing,
          'all present' if not missing else 'missing/empty: ' + ', '.join(missing),
          args.require_config, 'fill the documented FITBOX_* and TELEGRAM_* values')
    if values.get('TELEGRAM_BOT_TOKEN'):
        check(checks, 'telegram token shape', ':' in values['TELEGRAM_BOT_TOKEN'],
              'present (value hidden)', True, 'format is <bot-id>:<secret>')
    if values.get('TELEGRAM_CHAT_ID'):
        check(checks, 'telegram chat id shape', values['TELEGRAM_CHAT_ID'].lstrip('-').isdigit(),
              'present (value hidden)', True, 'use the numeric chat id')
    if values.get('FITBOX_TIMEZONE'):
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(values['FITBOX_TIMEZONE'])
            ok = True
        except Exception:
            ok = False
        check(checks, 'timezone', ok, values['FITBOX_TIMEZONE'], True,
              'use an IANA name such as Asia/Shanghai')
    if role == 'pi':
        if values.get('FITBOX_HEALTH_BIND') in ('0.0.0.0', '::', ''):
            check(checks, 'receiver bind address', False,
                  values.get('FITBOX_HEALTH_BIND') or 'default loopback', False,
                  'bind to 127.0.0.1 or the Tailscale address; never expose 8787 publicly')
        token_file = Path(os.path.expanduser(values.get(
            'FITBOX_HEALTH_TOKEN_FILE', '~/fitbox/health.token')))
        if token_file.exists():
            token_mode = stat.S_IMODE(token_file.stat().st_mode)
            check(checks, 'receiver token file', token_file.stat().st_size >= 16 and not (token_mode & 0o077),
                  f'{token_file.stat().st_size} bytes, mode {oct(token_mode)}', False,
                  'create a random token and chmod 600')
        else:
            check(checks, 'receiver token file', False, f'not found: {token_file}', False,
                  'required only if the health receiver is used')
        if not values.get('FITBOX_HOME_CONNECTION'):
            check(checks, 'home network profile', False, 'not set', False,
                  'set FITBOX_HOME_CONNECTION before using --net nm')
    return path, values


def check_chrome(checks, role, values):
    candidates = [values.get('CHROME_BIN'), os.environ.get('CHROME_BIN')] + CHROME_CANDIDATES
    candidates += [shutil.which(x) for x in ('google-chrome', 'chromium', 'chromium-browser')]
    path = next((p for p in candidates if p and Path(p).exists()), None)
    if not path:
        check(checks, 'chrome', False, 'not found', role == 'mac',
              'install Chrome/Chromium or set CHROME_BIN')
        return
    result = run([path, '--version'])
    code = result[0].returncode if isinstance(result, tuple) and result[0] else 1
    detail = result[0].stdout.strip() if isinstance(result, tuple) and result[0] else str(result[1])
    check(checks, 'chrome', code == 0, detail[:100], role == 'mac',
          'set CHROME_BIN to the executable')


def check_network(checks, role, values, args):
    for tool, required in (('ssh', role == 'mac'), ('scp', role == 'mac'),
                           ('systemctl', role == 'pi'), ('nmcli', role == 'pi')):
        path = shutil.which(tool)
        check(checks, tool, bool(path), path or 'not found', required,
              'install the system package or add it to PATH')
    for tool, required in (('tailscale', False), ('bluetoothctl', False), ('bluetoothd', False)):
        path = shutil.which(tool)
        check(checks, tool, bool(path), path or 'not found', required,
              'install only if you use this transport')
    if role == 'pi':
        result = run(['sudo', '-n', 'true'])
        ok = bool(result[0] and result[0].returncode == 0)
        check(checks, 'passwordless sudo', ok, 'ok' if ok else 'not available', False,
              'required only for --net nm / systemd-run recovery; otherwise use Ethernet + --net none')
    if role == 'mac' and not args.skip_ssh:
        hosts = [h.strip() for h in values.get('FITBOX_HOSTS', '').split(',') if h.strip()]
        user = values.get('FITBOX_SSH_USER', '')
        if not hosts or not user:
            check(checks, 'ssh connectivity', False, 'FITBOX_HOSTS/FITBOX_SSH_USER not set', True,
                  'complete the config first')
        else:
            for host in hosts:
                result = run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6',
                              f'{user}@{host}', 'true'], timeout=12)
                ok = bool(result[0] and result[0].returncode == 0)
                check(checks, f'ssh {host}', ok,
                      'ok' if ok else (result[1] or 'connection failed')[:120], True,
                      'verify the host key, key login, and Tailscale/LAN reachability')


def check_mac_runtime(checks):
    print_uid = os.getuid()
    result = run(['launchctl', 'print', f'gui/{print_uid}/dev.fitbox.watch'])
    ok = bool(result[0] and result[0].returncode == 0)
    check(checks, 'launchd watcher', ok, 'loaded' if ok else 'not loaded', False,
          'expected before first install; install deploy/launchd after config')


def check_pi_runtime(checks):
    for unit in ('fit-cpap.timer', 'fit-health.service', 'fit-scale.service'):
        result = run(['systemctl', 'is-enabled', unit])
        state = result[0].stdout.strip() if isinstance(result, tuple) and result[0] else 'unknown'
        check(checks, f'unit {unit}', state in ('enabled', 'static'),
              state, False, 'install and enable after the first successful manual run')


def check_disk(checks, values):
    target = Path(os.path.expanduser(values.get('FITBOX_DATA_ROOT', str(HOME))))
    probe = target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
        check(checks, 'free disk', free >= 2 * 1024 ** 3, f'{free / 1024 ** 3:.1f} GiB at {probe}', True,
              'keep enough room for CPAP history and raw health JSON')
    except OSError as exc:
        check(checks, 'free disk', False, str(exc), False, 'check FITBOX_DATA_ROOT')


def check_receiver_port(checks, values):
    bind = values.get('FITBOX_HEALTH_BIND', '')
    if bind in ('', '0.0.0.0', '::'):
        return
    port = int(os.environ.get('FITBOX_HEALTH_PORT', values.get('FITBOX_HEALTH_PORT', '8787')))
    try:
        with socket.create_connection((bind, port), timeout=2):
            ok, detail = True, f'{bind}:{port} reachable'
    except OSError as exc:
        ok, detail = False, f'{bind}:{port} not reachable ({exc.__class__.__name__})'
    check(checks, 'health receiver port', ok, detail, False,
          'start fit-health.service; bind to loopback or Tailscale')


def main():
    parser = argparse.ArgumentParser(description='Read-only fitbox deployment preflight')
    parser.add_argument('--role', choices=('auto', 'mac', 'pi'), default='auto')
    parser.add_argument('--config', help='path to config.env (default: FITBOX_CONFIG, repo config.local.env, or ~/.config/fitbox/config.env)')
    parser.add_argument('--require-config', action='store_true', help='treat missing config/keys as failures')
    parser.add_argument('--skip-ssh', action='store_true', help='do not test SSH to the Pi')
    parser.add_argument('--json', action='store_true', help='print one JSON object')
    args = parser.parse_args()
    role = args.role
    if role == 'auto':
        role = 'mac' if platform.system() == 'Darwin' else 'pi'
    checks = []
    check_project(checks)
    check_python(checks)
    path, values = check_config(checks, args, role)
    check_chrome(checks, role, values)
    check_network(checks, role, values, args)
    check_disk(checks, values)
    if role == 'mac':
        check_mac_runtime(checks)
    else:
        check_pi_runtime(checks)
        check_receiver_port(checks, values)
    failures = [c for c in checks if c['status'] == 'fail']
    result = {'ok': not failures, 'role': role, 'platform': platform.platform(),
              'config_path': str(path), 'checks': checks,
              'next': 'fix FAIL items, then rerun with --require-config' if failures else 'preflight passed'}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for c in checks:
            mark = {'pass': 'PASS', 'warn': 'WARN', 'fail': 'FAIL'}[c['status']]
            print(f'[{mark}] {c["name"]}: {c["detail"]}')
            if c['status'] != 'pass' and c['hint']:
                print(f'       -> {c["hint"]}')
        print(f'\n{"OK" if not failures else "BLOCKED"}: {len(failures)} required failure(s); role={role}')
    return 0 if not failures else 1


if __name__ == '__main__':
    sys.exit(main())
