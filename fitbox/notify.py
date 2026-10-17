#!/usr/bin/env python3
"""The single place every push notification goes out from — Telegram.

Credentials live outside the repo, in ~/.config/fitbox/config.env (mode 600):

    TELEGRAM_BOT_TOKEN=<your-bot-token>
    TELEGRAM_CHAT_ID=<your-chat-id>

No proxy here. 网络出口由系统层负责（Clash TUN 模式），代码里不再有任何代理地址、
不做线路切换：一次请求，成或不成。opener 显式清空代理链，为的是让任何残留的
HTTP_PROXY / HTTPS_PROXY 环境变量都无法把请求劫到别处。

Nothing else in the pipeline knows how a message reaches the phone. Every public
function returns True/False and never raises — a failed push must never lose the record.
"""
import json, os, sys, uuid
from pathlib import Path
from urllib import request as _rq

from config import CONFIG_FILE
ENV = CONFIG_FILE
API = 'https://api.telegram.org'
TIMEOUT = 15

_cfg = None
# 无代理 opener：TUN 在系统层接管路由，进程内一律不走代理。
OPENER = _rq.build_opener(_rq.ProxyHandler({}))


def config():
    """{token, chat} — file first, environment as fallback. Cached per process."""
    global _cfg
    if _cfg is None:
        cfg = {}
        if ENV.exists():
            for line in ENV.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    cfg[k.strip()] = v.strip()
        _cfg = {'token': os.environ.get('TELEGRAM_BOT_TOKEN', ''),
                'chat': os.environ.get('TELEGRAM_CHAT_ID', '')}
    return _cfg


def _open(url, data=None, headers=None, timeout=TIMEOUT):
    return OPENER.open(_rq.Request(url, data=data, headers=headers or {}), timeout=timeout)


def api(method, params=None, files=None, timeout=TIMEOUT):
    """Call one Bot API method. files={field: (filename, bytes, content-type)} → multipart."""
    cfg = config()
    if not cfg['token']:
        raise RuntimeError(f'no bot token: {ENV} missing or empty')
    url = f"{API}/bot{cfg['token']}/{method}"
    if files:
        boundary = '----fitbox' + uuid.uuid4().hex
        body = bytearray()
        for k, v in (params or {}).items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, (fname, blob, ctype) in files.items():
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                     f'Content-Type: {ctype}\r\n\r\n').encode()
            body += blob + b'\r\n'
        body += f'--{boundary}--\r\n'.encode()
        with _open(url, bytes(body), {'Content-Type': f'multipart/form-data; boundary={boundary}'}, timeout) as r:
            return json.load(r)
    with _open(url, json.dumps(params or {}).encode(), {'Content-Type': 'application/json'}, timeout) as r:
        return json.load(r)


def download(file_path, dest):
    """Fetch a file the Bot API pointed at (getFile → file_path) to a local path."""
    cfg = config()
    url = f"{API}/file/bot{cfg['token']}/{file_path}"
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _open(url, None, {}, 120) as r:
        dest.write_bytes(r.read())
    return dest


def send(text):
    """Push a plain-text message. Never raises."""
    try:
        r = api('sendMessage', {'chat_id': config()['chat'], 'text': text, 'disable_web_page_preview': True})
        if not r.get('ok'):
            raise RuntimeError(r.get('description'))
        return True
    except Exception as e:
        print(f'notify send failed: {e}', file=sys.stderr)
        return False


def send_image(path, caption=''):
    """Push a local image (uploaded, no public URL needed). Never raises."""
    try:
        blob = Path(path).read_bytes()
        r = api('sendPhoto', {'chat_id': config()['chat'], 'caption': caption[:1024]},
                {'photo': (Path(path).name, blob, 'image/png')}, timeout=60)
        if not r.get('ok'):
            raise RuntimeError(r.get('description'))
        return True
    except Exception as e:
        print(f'notify send_image failed: {e}', file=sys.stderr)
        return False


if __name__ == '__main__':
    arg = ' '.join(sys.argv[1:])
    if arg in ('--check', 'check'):
        me = api('getMe')['result']
        print(f"bot @{me.get('username')} (id {me.get('id')}) chat={config()['chat']} route=direct(no-proxy)")
        print('test message sent:', send('🔧 notify.py 自检：通道正常。'))
    else:
        print('sent:', send(arg or '测试消息'))
