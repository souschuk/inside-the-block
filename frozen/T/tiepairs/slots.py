"""Two slot sources, and a run uses both.

WebSocket events carry an exact `timestamp`, and on a private endpoint they arrive within about 30 ms of it. On the public
RPC they do not: measured on 31.10.2026 they came up to 9.8 s late, in bursts, on slotsUpdatesSubscribe and slotSubscribe
alike. So a late event must never be the only thing that moves the clock. Polled getSlot is the floor: its answer shows a
slot whose first shred is at most about 0.57 s old on either endpoint. Whichever source sees a slot first moves the clock,
and a late event simply changes nothing. The feed is also the witness: after the run its timestamps show how far ahead
of the first shred each pair was sent.
"""
import asyncio
import http.client
import json
import ssl
import threading
import time
import urllib.parse

import certifi
import websockets

KEEP_SLOTS = 4_000


class SlotTimeout(Exception):
    pass


class SlotWatcher:
    def __init__(self):
        self.latest = 0
        self.seen = {}
        self.last_event = 0
        self.waiters = []

    def observe(self, slot):
        now = time.perf_counter_ns()
        self.last_event = now
        if slot not in self.seen:
            self.seen[slot] = now
            if len(self.seen) > KEEP_SLOTS:
                for old in sorted(self.seen)[:KEEP_SLOTS // 4]:
                    del self.seen[old]
        if slot > self.latest:
            self.latest = slot
            ready = [w for w in self.waiters if w[0] <= slot]
            self.waiters = [w for w in self.waiters if w[0] > slot]
            for _, future in ready:
                if not future.done():
                    future.set_result(slot)

    def age_ms(self):
        return (time.perf_counter_ns() - self.last_event) / 1e6

    async def wait(self, slot, timeout):
        if self.latest >= slot:
            return self.latest
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((slot, future))
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise SlotTimeout(f'slot {slot} not seen in {timeout:.0f} s (latest {self.latest})') from None


class SlotPoller(threading.Thread):
    """getSlot over one kept-alive connection, in its own thread so that a slow answer never stalls a send."""

    def __init__(self, url, watcher, loop, period=0.33):
        super().__init__(daemon=True)
        self.url = urllib.parse.urlsplit(url)
        self.watcher = watcher
        self.loop = loop
        self.period = period
        self.halt = threading.Event()
        self.errors = 0
        self.polls = 0
        self.latencies = []

    def connection(self):
        if self.url.scheme == 'https':
            context = ssl.create_default_context(cafile=certifi.where())
            return http.client.HTTPSConnection(self.url.hostname, self.url.port or 443, timeout=2, context=context)
        return http.client.HTTPConnection(self.url.hostname, self.url.port or 80, timeout=2)

    def run(self):
        body = json.dumps(dict(jsonrpc='2.0', id=1, method='getSlot', params=[dict(commitment='processed')]))
        path = self.url.path or '/'
        if self.url.query:
            path += '?' + self.url.query
        conn = self.connection()
        while not self.halt.is_set():
            started = time.perf_counter()
            try:
                conn.request('POST', path, body, {'content-type': 'application/json'})
                response = conn.getresponse()
                payload = response.read()
                if response.status == 429:
                    self.errors += 1
                    time.sleep(float(response.getheader('retry-after') or 2))
                    continue
                slot = json.loads(payload)['result']
                self.polls += 1
                self.latencies.append(time.perf_counter() - started)
                self.loop.call_soon_threadsafe(self.watcher.observe, slot)
            except Exception:
                self.errors += 1
                conn.close()
                conn = self.connection()
            time.sleep(max(0.0, self.period - (time.perf_counter() - started)))

    def stop(self):
        self.halt.set()


class StampFeed:
    """Appends (slot, RPC timestamp, our arrival time) to a file and tells the watcher."""

    def __init__(self, url, path, watcher=None):
        self.url = url
        self.path = path
        self.watcher = watcher
        self.task = None
        self.count = 0
        self.reconnects = 0

    def start(self):
        self.task = asyncio.create_task(self._run())

    async def stop(self, linger=0.0):
        if linger:
            await asyncio.sleep(linger)
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _run(self):
        context = ssl.create_default_context(cafile=certifi.where()) if self.url.startswith('wss') else None
        with open(self.path, 'a') as f:
            while True:
                try:
                    async with websockets.connect(self.url, open_timeout=10, ping_interval=20, ssl=context, max_queue=None) as ws:
                        await ws.send(json.dumps(dict(jsonrpc='2.0', id=1, method='slotsUpdatesSubscribe', params=[])))
                        while True:
                            message = json.loads(await ws.recv())
                            result = message.get('params', {}).get('result')
                            if result and result.get('type') == 'firstShredReceived':
                                f.write(json.dumps(dict(slot=result['slot'], stamp_ms=result['timestamp'],
                                                        arrival_ms=round(time.time() * 1000, 1))) + '\n')
                                f.flush()
                                self.count += 1
                                if self.watcher:
                                    self.watcher.observe(result['slot'])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.reconnects += 1
                    await asyncio.sleep(1)


def read_stamps(path):
    stamps = {}
    offset = None
    try:
        with open(path) as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                stamps.setdefault(row['slot'], row['stamp_ms'])
                delay = row['arrival_ms'] - row['stamp_ms']
                offset = delay if offset is None else min(offset, delay)
    except FileNotFoundError:
        pass
    return stamps, offset
