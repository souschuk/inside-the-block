import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from quic_server import settle, start_server  # noqa: E402

from tiepairs import guard, wire as wire_module  # noqa: E402
from tiepairs.guard import Refused  # noqa: E402
from tiepairs.rpc import MAINNET_GENESIS  # noqa: E402
from tiepairs.wire import ConnectionBudget, LinkPool, StreamBudget, Wire  # noqa: E402

AUTH = guard.LiveAuthorization(guard._SEAL, 'test', MAINNET_GENESIS, 1)


def run(coroutine):
    return asyncio.run(coroutine)


def test_handshake_and_ping(tmp_path):
    async def go():
        server, recorder, address = await start_server(str(tmp_path))
        link = await Wire(directory=str(tmp_path)).open(address)
        assert link.alive and link.handshake_ms > 0
        assert await link.ping() >= 0
        assert len(link.conn_id) >= 16 and link.local_port > 0
        await link.close()
        server.close()
        assert recorder.connections == 1
    run(go())


def test_link_without_a_permission_cannot_write_a_stream(tmp_path):
    async def go():
        server, recorder, address = await start_server(str(tmp_path))
        link = await Wire(directory=str(tmp_path)).open(address)
        with pytest.raises(Refused):
            link.send(b'x' * 300)
        await settle()
        await link.close()
        server.close()
        assert recorder.streams == []
    run(go())


def test_live_link_delivers_each_transaction_on_its_own_unidirectional_stream(tmp_path):
    async def go():
        server, recorder, address = await start_server(str(tmp_path))
        link = await Wire(auth=AUTH, directory=str(tmp_path)).open(address)
        one, two = b'a' * 300, b'b' * 300
        first = link.send(one)
        await asyncio.sleep(0.01)
        second = link.send(two)
        await settle(0.15)
        await link.close()
        server.close()
        assert [s[1] for s in recorder.streams] == [one, two]
        assert all(s[2] for s in recorder.streams)
        arrival_gap = (recorder.streams[1][0] - recorder.streams[0][0]) / 1e6
        assert abs(arrival_gap - (second['t_ns'] - first['t_ns']) / 1e6) < 5  # no pacer stands between the two writes
        assert first['stream_id'] != second['stream_id'] and first['wall_ns'] > 0
        assert link.acks.get(first['ack_uid']) and link.acks.get(second['ack_uid'])  # each packet's ACK moment is known
    run(go())


def test_ten_writes_keep_their_order(tmp_path):
    async def go():
        server, recorder, address = await start_server(str(tmp_path))
        link = await Wire(auth=AUTH, directory=str(tmp_path)).open(address)
        for i in range(10):
            link.send(bytes([i]) * 300)
            await asyncio.sleep(0.012)
        await settle()
        await link.close()
        server.close()
        assert [s[1][0] for s in recorder.streams] == list(range(10))
    run(go())


def test_stream_burst_limit(tmp_path):
    async def go():
        server, recorder, address = await start_server(str(tmp_path))
        link = await Wire(auth=AUTH, directory=str(tmp_path)).open(address)
        for i in range(wire_module.MAX_STREAMS_PER_100MS):
            link.send(bytes([i]) * 100)
        with pytest.raises(StreamBudget):
            link.send(b'x' * 100)
        await asyncio.sleep(0.11)
        link.send(b'y' * 100)
        await settle()
        await link.close()
        server.close()
        assert len(recorder.streams) == wire_module.MAX_STREAMS_PER_100MS + 1
    run(go())


def test_connection_budget_per_ip(tmp_path):
    async def go():
        server, recorder, address = await start_server(str(tmp_path))
        wire = Wire(directory=str(tmp_path))
        links = [await wire.open(address) for _ in range(wire_module.MAX_NEW_CONNECTIONS_PER_MINUTE)]
        with pytest.raises(ConnectionBudget):
            await wire.open(address)
        wire.opened['127.0.0.1'][0] -= 61  # the oldest one leaves the minute
        links.append(await wire.open(address))
        for link in links:
            await link.close()
        server.close()
        assert recorder.connections == wire_module.MAX_NEW_CONNECTIONS_PER_MINUTE + 1
    run(go())


def test_closed_link_reports_it(tmp_path):
    async def go():
        server, _, address = await start_server(str(tmp_path))
        link = await Wire(auth=AUTH, directory=str(tmp_path)).open(address)
        link.protocol.close()
        await settle()
        assert not link.alive
        await link.close()
        server.close()
    run(go())


def test_pool_keeps_one_connection_per_address_and_samples_rtt(tmp_path):
    async def go():
        server, recorder, address = await start_server(str(tmp_path))
        rtt, links = str(tmp_path / 'rtt.jsonl'), str(tmp_path / 'links.jsonl')
        pool = LinkPool(Wire(directory=str(tmp_path)), rtt, links)
        first, new_1 = await pool.get(address)
        again, new_2 = await pool.get(address)
        assert first is again and new_1 and not new_2
        first.sample_fast(0.1)
        await asyncio.sleep(0.15)
        assert first.rtt_last is not None
        first.protocol.close()
        await settle()
        replaced, new_3 = await pool.get(address)
        assert replaced is not first and new_3 and pool.evictions[address] == 1
        await pool.close()
        server.close()
        return recorder, rtt, links
    recorder, rtt, links = run(go())
    assert recorder.connections == 2
    lines = open(rtt).read().splitlines()
    assert len(lines) > 5 and 'rtt_adj_ms' in lines[0]
    assert '"closed"' in open(links).read()


def test_pool_refuses_to_reconnect_past_the_budget(tmp_path):
    async def go():
        server, _, address = await start_server(str(tmp_path))
        wire = Wire(directory=str(tmp_path))
        wire.opened['127.0.0.1'].extend([time.monotonic()] * wire_module.MAX_NEW_CONNECTIONS_PER_MINUTE)
        pool = LinkPool(wire)
        with pytest.raises(ConnectionBudget):
            await pool.get(address)
        await pool.close()
        server.close()
    run(go())
