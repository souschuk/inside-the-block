"""QUIC to leaders' tpuQuic: ALPN solana-tpu, self-signed ed25519 client certificate, one unidirectional stream per
transaction, one connection per leader window.

Unstaked limits in Agave: 8 new connections per IP per minute, 20 streams per 100 ms per connection. We stay at 5 and
14. A link without a LiveAuthorization has no way to write a stream: dry runs handshake and ping, nothing else.

The same PINGs do two jobs. They keep the connection alive and they sample RTT, every 250 ms normally and every 10 ms
around a pair, which shows whether the network could have swapped two packets 10 ms apart. Servers delay ACKs by up to
25 ms, so each sample also carries the RTT aioquic corrected with the delay the ACK frame declares. A PING also rides in
the same packet as each transaction, so its acknowledgement is the moment that packet was acknowledged.
"""
import asyncio
import collections
import datetime
import itertools
import json
import os
import ssl
import tempfile
import time

from aioquic.asyncio import QuicConnectionProtocol, connect
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import ConnectionTerminated, PingAcknowledged
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

from .guard import require

ALPN = 'solana-tpu'
IDLE_TIMEOUT = 10.0
HANDSHAKE_TIMEOUT = 2.5
MAX_NEW_CONNECTIONS_PER_MINUTE = 5
MAX_STREAMS_PER_100MS = 14
SLOW_SAMPLE = 0.25
FAST_SAMPLE = 0.01


class LinkDown(Exception):
    pass


class ConnectionBudget(Exception):
    pass


class StreamBudget(Exception):
    pass


def write_certificate(directory):
    key = ed25519.Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Solana node')])
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(datetime.datetime(2020, 1, 1)).not_valid_after(datetime.datetime(2100, 1, 1))
                   .sign(key, None))
    cert_path, key_path = os.path.join(directory, 'c.pem'), os.path.join(directory, 'k.pem')
    with open(cert_path, 'wb') as f:
        f.write(certificate.public_bytes(serialization.Encoding.PEM))
    with open(key_path, 'wb') as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return cert_path, key_path


class TpuProtocol(QuicConnectionProtocol):
    closed_reason = None
    on_ack = None

    def quic_event_received(self, event):
        if isinstance(event, ConnectionTerminated):
            self.closed_reason = f'{event.error_code}:{event.reason_phrase}'
        elif isinstance(event, PingAcknowledged) and self.on_ack:
            self.on_ack(event.uid)
        super().quic_event_received(event)


class Link:
    def __init__(self, wire, manager, protocol, address, handshake_ms):
        self.wire = wire
        self.manager = manager
        self.protocol = protocol
        self.address = address
        self.handshake_ms = handshake_ms
        self.opened_at = time.time()
        self.uids = itertools.count(1 << 40)
        self.pings = {}    # uid -> send time, for RTT samples
        self.acks = {}     # uid -> ack time, for probes
        self.probes = set()
        self.recent = collections.deque()
        self.rtt_last = None   # corrected for the declared ACK delay
        self.sink = None
        self.fast_until = 0.0
        self.sampler = None
        protocol.on_ack = self._acked

    @property
    def alive(self):
        return self.protocol.closed_reason is None and not self.protocol._closed.is_set()

    @property
    def closed_reason(self):
        return self.protocol.closed_reason

    @property
    def conn_id(self):
        return self.protocol._quic.original_destination_connection_id.hex()

    @property
    def local_port(self):
        return self.protocol._transport.get_extra_info('sockname')[1]

    def _acked(self, uid):
        now = time.perf_counter_ns()
        if uid in self.probes:
            self.acks[uid] = now
        elif uid in self.pings:
            raw = (now - self.pings.pop(uid)) / 1e6
            self.rtt_last = self.protocol._quic._loss._rtt_latest * 1000
            if self.sink:
                self.sink(self, raw, self.rtt_last)

    def ping_nowait(self):
        uid = next(self.uids)
        self.pings[uid] = time.perf_counter_ns()
        if len(self.pings) > 400:  # the oldest never came back: lost, and that is not an RTT
            self.pings.pop(next(iter(self.pings)))
        self.protocol._quic.send_ping(uid)
        self.protocol.transmit()

    async def ping(self, timeout=1.5):
        started = time.perf_counter_ns()
        await asyncio.wait_for(self.protocol.ping(), timeout)
        return (time.perf_counter_ns() - started) / 1e6

    def sample_fast(self, seconds):
        self.fast_until = time.monotonic() + seconds

    async def _sample(self):
        while self.alive:
            self.ping_nowait()
            await asyncio.sleep(FAST_SAMPLE if time.monotonic() < self.fast_until else SLOW_SAMPLE)

    def start_sampling(self, sink):
        self.sink = sink
        self.sampler = asyncio.create_task(self._sample())

    def send(self, raw):
        """Hand the transaction to the socket. Returns what the journal needs. Refuses without a live authorization."""
        require(self.wire.auth)
        if not self.alive:
            raise LinkDown(self.closed_reason or 'closed')
        now = time.monotonic()
        while self.recent and now - self.recent[0] > 0.1:
            self.recent.popleft()
        if len(self.recent) >= MAX_STREAMS_PER_100MS:
            raise StreamBudget(f'{len(self.recent)} streams in the last 100 ms')
        self.recent.append(now)
        quic = self.protocol._quic
        stream = quic.get_next_available_stream_id(is_unidirectional=True)
        quic.send_stream_data(stream, raw, end_stream=True)
        uid = next(self.uids)
        self.probes.add(uid)
        quic.send_ping(uid)
        self.protocol.transmit()
        return dict(t_ns=time.perf_counter_ns(), wall_ns=time.time_ns(), stream_id=stream, ack_uid=uid)

    async def close(self):
        if self.sampler:
            self.sampler.cancel()
        try:
            await asyncio.wait_for(self.manager.__aexit__(None, None, None), 1.0)
        except Exception:
            pass


class Wire:
    def __init__(self, auth=None, directory=None):
        self.auth = auth
        self.directory = directory or tempfile.mkdtemp(prefix='tiepairs-')
        self.cert_path, self.key_path = write_certificate(self.directory)
        self.opened = collections.defaultdict(collections.deque)

    @property
    def live(self):
        return self.auth is not None

    def configuration(self):
        config = QuicConfiguration(is_client=True, alpn_protocols=[ALPN], verify_mode=ssl.CERT_NONE,
                                   idle_timeout=IDLE_TIMEOUT)
        config.load_cert_chain(self.cert_path, self.key_path)
        return config

    def spend_connection(self, host):
        times = self.opened[host]
        now = time.monotonic()
        while times and now - times[0] > 60:
            times.popleft()
        if len(times) >= MAX_NEW_CONNECTIONS_PER_MINUTE:
            raise ConnectionBudget(f'{len(times)} new connections to {host} in the last minute')
        times.append(now)

    async def open(self, address, timeout=HANDSHAKE_TIMEOUT):
        host, port = address.rsplit(':', 1)
        self.spend_connection(host)
        manager = connect(host, int(port), configuration=self.configuration(), create_protocol=TpuProtocol)
        started = time.perf_counter_ns()
        protocol = await asyncio.wait_for(manager.__aenter__(), timeout)
        return Link(self, manager, protocol, address, (time.perf_counter_ns() - started) / 1e6)


class LinkPool:
    """One connection per address at a time, released when its window is over and replaced within the budget if it dies."""

    def __init__(self, wire, rtt_path=None, links_path=None):
        self.wire = wire
        self.links = {}
        self.evictions = collections.Counter()
        self.rtt_file = open(rtt_path, 'a', buffering=1) if rtt_path else None
        self.links_file = open(links_path, 'a', buffering=1) if links_path else None

    def _sink(self, link, raw_ms, corrected_ms):
        if self.rtt_file:
            self.rtt_file.write(json.dumps(dict(t_ns=time.time_ns(), addr=link.address, conn=link.conn_id, rtt_ms=round(raw_ms, 3),
                                                rtt_adj_ms=round(corrected_ms, 3))) + '\n')

    def _note(self, event, link):
        if self.links_file:
            self.links_file.write(json.dumps(dict(event=event, t=time.time(), addr=link.address, conn=link.conn_id,
                                                  handshake_ms=round(link.handshake_ms, 2), lived_s=round(time.time() - link.opened_at, 2),
                                                  reason=link.closed_reason, local_port=link.local_port)) + '\n')

    async def get(self, address, timeout=HANDSHAKE_TIMEOUT):
        """Returns (link, is_new). Raises ConnectionBudget instead of breaking the limit."""
        link = self.links.get(address)
        if link and link.alive:
            return link, False
        if link:
            self.evictions[address] += 1
            self._note('closed', link)
            await link.close()
            del self.links[address]
        link = await self.wire.open(address, timeout)
        link.start_sampling(self._sink)
        self.links[address] = link
        self._note('opened', link)
        return link, True

    async def release(self, address):
        link = self.links.pop(address, None)
        if link:
            self._note('released', link)
            await link.close()

    async def close(self):
        for link in self.links.values():
            self._note('kept_to_end', link)
            await link.close()
        self.links = {}
        for f in (self.rtt_file, self.links_file):
            if f:
                f.close()
        self.rtt_file = self.links_file = None
