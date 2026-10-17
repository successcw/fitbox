#!/usr/bin/env python3
"""Receive Health Auto Export (iOS) REST pushes and store them on fitbox.

Reachable only over Tailscale (ufw allows the port on tailscale0 only) and every request must carry
the shared token in the X-Fit-Token header. Each push is saved verbatim; an index line is appended to
~/fitdata/health.jsonl so the Mac watcher can pick it up the same way it does weigh-ins.
"""
import hmac, json, logging, os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
PORT = int(os.environ.get('FITBOX_HEALTH_PORT', '8787'))
ROOT = os.path.expanduser('~/fitdata')
INDEX = os.path.join(ROOT, 'health.jsonl')
TOKEN = open(os.path.expanduser(os.environ.get('FITBOX_HEALTH_TOKEN_FILE', '~/fitbox/health.token'))).read().strip()
if not TOKEN:
    raise RuntimeError('Health receiver token must not be empty')
MAX_BYTES = 150 * 1024 * 1024   # first full sync can be large

log = logging.getLogger('health')


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, msg):
        body = msg.encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not hmac.compare_digest(self.headers.get('X-Fit-Token', ''), TOKEN):
            log.warning('rejected request from %s (bad token)', self.client_address[0])
            return self._reply(403, 'forbidden')
        n = int(self.headers.get('Content-Length') or 0)
        if not 0 < n <= MAX_BYTES:
            return self._reply(413, 'bad size')
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw)
        except ValueError:
            return self._reply(400, 'not json')
        now = datetime.now()
        rel = f'health/{now:%Y%m%d-%H%M%S-%f}.json'
        os.makedirs(os.path.join(ROOT, 'health'), exist_ok=True)
        with open(os.path.join(ROOT, rel), 'wb') as f:
            f.write(raw)
        d = data.get('data', {}) if isinstance(data, dict) else {}
        metrics = {m.get('name'): len(m.get('data') or []) for m in d.get('metrics') or []}
        with open(INDEX, 'a') as f:
            f.write(json.dumps({'ts': now.isoformat(timespec='seconds'), 'file': rel,
                                'bytes': n, 'metrics': metrics,
                                'workouts': len(d.get('workouts') or [])}) + '\n')
        log.info('stored %s (%d bytes, %d metrics)', rel, n, len(metrics))
        self._reply(200, 'ok')

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    ThreadingHTTPServer((os.environ.get('FITBOX_HEALTH_BIND', '127.0.0.1'), PORT), Handler).serve_forever()
