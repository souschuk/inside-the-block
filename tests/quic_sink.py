"""A separate process standing in for a leader, so that it outlives the sender killed by the crash test. Every stream it
receives goes to a file before anything else happens."""
import asyncio
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from quic_server import start_server  # noqa: E402


async def main(directory, port_file, received_file):
    sink = open(received_file, 'a')

    def on_stream(payload):
        sink.write(base64.b64encode(payload).decode() + '\n')
        sink.flush()
        os.fsync(sink.fileno())

    server, _, address = await start_server(directory, on_stream=on_stream)
    temporary = port_file + '.tmp'
    with open(temporary, 'w') as f:
        f.write(address)
    os.rename(temporary, port_file)
    await asyncio.Event().wait()


asyncio.run(main(*sys.argv[1:4]))
