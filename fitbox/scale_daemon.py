#!/usr/bin/env python3
"""Record weigh-ins from the AFU-BH-TZ-B1 body-fat scale over BLE, triggered by the scale waking up.

Idle, the scale advertises ~0.5/s; when someone steps on it jumps to ~3-5/s for ~45 s. We only listen
(passive, no battery cost to the scale) and connect just for the weigh-in, then disconnect.
Protocol notes: docs/deployment.md. Only ever writes the read-type commands the 阿福 App itself sends
(request realtime / impedance result, read-only protocol); never settings, never the DFU service 00001530-....
Output: ~/fitdata/scale.jsonl, one line per weigh-in.
"""
import asyncio, json, logging, os, time
from collections import deque
from datetime import datetime
from bleak import BleakClient, BleakScanner
import config

NAME = 'AFU-BH-TZ-B1'
NOTIFY = 'af000003-9f2d-4c8a-8d6f-6a7b45f90000'
WRITE = 'af000002-9f2d-4c8a-8d6f-6a7b45f90000'
SCALE_SVC = '0000fc50-0000-1000-8000-00805f9b34fb'
IMP_WAIT = 12        # s to wait for the impedance packet after asking
OUT = os.path.expanduser('~/fitdata/scale.jsonl')
BURST = (6, 3.0)     # >= 6 adverts within 3 s means the scale is awake
SESSION_LIMIT = 60   # s max per connection
OFF_GRACE = 3        # s of zero weight after a load = stepped off
REARM = 60           # s to ignore triggers after a session (scale keeps fast-advertising a while)
USER_KG = (float(os.environ.get('FITBOX_WEIGHT_MIN_KG', '0')), float(os.environ.get('FITBOX_WEIGHT_MAX_KG', '300')))  # Configure a range when sharing the scale.

log = logging.getLogger('scale')


def cmd(body):
    """20-byte command: af45 + 17 body bytes + checksum (low 5 bits = sum of bytes 3..19)."""
    b = bytes.fromhex('af45') + bytes.fromhex(body).ljust(17, b'\0')
    return b + bytes([sum(b[2:19]) & 0x1f])


REQ_RESULT = cmd('1251' + '00' * 14 + '5f')       # what the App sends once the weight is stable


def decode_weight(b):
    """kg from a realtime packet; byte 4 is the 65.536 kg page counter offset by 0x08."""
    return round((b[4] - 0x08) * 65.536 + (b[2] | b[3] << 8) / 1000, 2)


async def wait_for_wake():
    times, hit = deque(), {}
    woke = asyncio.Event()

    def cb(dev, adv):
        if adv.local_name != NAME and dev.name != NAME:
            return
        now = time.time()
        times.append(now)
        while times and now - times[0] > BURST[1]:
            times.popleft()
        if len(times) >= BURST[0] and not woke.is_set():
            hit['dev'] = dev
            hit['adv'] = {k: v.hex() for k, v in adv.service_data.items()}
            woke.set()

    # let BlueZ drop everything but the scale's FC50 adverts, so dbus/python aren't woken by every neighbour's device
    async with BleakScanner(detection_callback=cb, service_uuids=[SCALE_SVC],
                            bluez={'filters': {'UUIDs': [SCALE_SVC], 'DuplicateData': True}}):
        await woke.wait()
    return hit['dev'], hit.get('adv')


async def session(dev, adv=None):
    pk, t0 = [], time.time()
    state = {'loaded': False, 'zero_since': None, 'final': None, 'imp': None, 'asked': None}
    done = asyncio.Event()

    def on(_, data):
        b = bytes(data)
        pk.append((round(time.time() - t0, 2), b.hex()))
        if len(b) < 20 or b[0] != 0xAF:
            return
        if b[18] == 0x52:                               # impedance of this weigh-in, e.g. af45 01 00 cc01 → 460 Ω
            state['imp'] = b[4] | b[5] << 8
            done.set()
        elif b[18] == 0x54:
            state['final'] = b.hex()
        elif b[18] == 0x51:
            if decode_weight(b) > 1:
                state['loaded'], state['zero_since'] = True, None
            elif state['loaded'] and state['zero_since'] is None:
                state['zero_since'] = time.time()

    async with BleakClient(dev, timeout=15) as c:
        await c.start_notify(NOTIFY, on)
        while not done.is_set() and time.time() - t0 < SESSION_LIMIT and c.is_connected:
            stable = any(h.startswith('af45') and int(h[10:12], 16) & 0x80 and h[36:38] == '51' for _, h in pk[-3:])
            if stable and state['asked'] is None:
                await c.write_gatt_char(WRITE, REQ_RESULT, response=True)
                state['asked'] = time.time()
            if state['asked'] and not state.get('resent') and time.time() - state['asked'] > 6:
                await c.write_gatt_char(WRITE, REQ_RESULT, response=True)   # the App also asks twice ~6 s apart
                state['resent'] = True
            if state['asked'] and time.time() - state['asked'] > IMP_WAIT:
                break
            if state['zero_since'] and time.time() - state['zero_since'] > OFF_GRACE + (IMP_WAIT if state['asked'] else 0):
                break
            try:
                await asyncio.wait_for(done.wait(), 0.5)
            except asyncio.TimeoutError:
                pass
    # leaving the context manager disconnects, so the scale is free again

    stable = [bytes.fromhex(h) for _, h in pk
              if len(h) == 40 and h.startswith('af45') and int(h[10:12], 16) & 0x80 and h[36:38] == '51']
    if not stable:
        log.info('session without stable weight (%d packets)', len(pk))
        return
    kg = decode_weight(stable[-1])
    if not USER_KG[0] <= kg <= USER_KG[1]:
        log.info('weigh-in outside user range, ignored')
        return
    seen, raw = set(), []
    for t, h in pk:
        if h not in seen:
            seen.add(h)
            raw.append([t, h])
    rt = stable[-1]
    rec = {'ts': datetime.now().isoformat(timespec='seconds'), 'weight': kg, 'impedance': state['imp'],
           'battery_guess': {'rt_byte11': rt[11], 'adv': adv},   # which one is battery is not confirmed yet
           'final': state['final'], 'raw': raw}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'a') as f:
        f.write(json.dumps(rec) + '\n')
    log.info('recorded %.2f kg, impedance %s', kg, state['imp'])


async def main():
    while True:
        dev, adv = await wait_for_wake()
        log.info('scale woke up, connecting')
        try:
            await session(dev, adv)
        except Exception as e:  # BLE drops are routine; keep the daemon alive
            log.warning('session error: %s', e)
        await asyncio.sleep(REARM)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    asyncio.run(main())
