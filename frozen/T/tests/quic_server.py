"""A local QUIC endpoint that records what arrives on unidirectional streams, standing in for a leader's TPU port."""
import asyncio
import datetime
import os
import time

from aioquic.asyncio import QuicConnectionProtocol, serve
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import StreamDataReceived
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def server_certificate(directory):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'test')])
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(datetime.datetime(2020, 1, 1)).not_valid_after(datetime.datetime(2100, 1, 1))
                   .sign(key, __import__('cryptography.hazmat.primitives.hashes', fromlist=['SHA256']).SHA256()))
    cert_path, key_path = os.path.join(directory, 's.pem'), os.path.join(directory, 'sk.pem')
    with open(cert_path, 'wb') as f:
        f.write(certificate.public_bytes(serialization.Encoding.PEM))
    with open(key_path, 'wb') as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return cert_path, key_path


class Recorder:
    def __init__(self):
        self.buffers = {}
        self.streams = []  # (arrival ns, payload, is unidirectional)
        self.connections = 0


async def start_server(directory, port=0, on_stream=None):
    recorder = Recorder()

    class Protocol(QuicConnectionProtocol):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            recorder.connections += 1
            self.server_side = self

        def quic_event_received(self, event):
            if isinstance(event, StreamDataReceived):
                key = (id(self), event.stream_id)
                recorder.buffers[key] = recorder.buffers.get(key, b'') + event.data
                if event.end_stream:
                    payload = recorder.buffers.pop(key)
                    recorder.streams.append((time.perf_counter_ns(), payload, bool(event.stream_id & 2)))
                    if on_stream:
                        on_stream(payload)
            super().quic_event_received(event)

    config = QuicConfiguration(is_client=False, alpn_protocols=['solana-tpu'], idle_timeout=5)
    config.load_cert_chain(*server_certificate(directory))
    server = await serve('127.0.0.1', port, configuration=config, create_protocol=Protocol)
    actual = server._transport.get_extra_info('sockname')[1]
    return server, recorder, f'127.0.0.1:{actual}'


async def settle(seconds=0.1):
    await asyncio.sleep(seconds)
