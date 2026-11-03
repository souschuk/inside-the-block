"""The full send path against a local QUIC endpoint, with slots driven by the test."""
import asyncio
import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from quic_server import settle, start_server  # noqa: E402

from tiepairs import guard, journal, schedule, session, targets, tx  # noqa: E402
from tiepairs.ledger import Ledger  # noqa: E402
from tiepairs.rpc import MAINNET_GENESIS  # noqa: E402
from tiepairs.slots import SlotWatcher  # noqa: E402
from tiepairs.targets import Target  # noqa: E402
from tiepairs.wire import LinkDown, LinkPool, Wire  # noqa: E402

T1 = Target('A' * 44, 'treated', 'JitoLabs', '4.4.0')
BLOCKHASH = tx.b58encode(bytes(range(32)))
CONFIG = session.Config(prepare_slots=12, anchor_slots=4, cancel_slots=1, slot_timeout=5.0, gossip_seconds=3600,
                        ack_wait_seconds=0.05, fast_rtt_seconds=0.05)
RUN_ID = '20261101T000000Z-live-abcx'
AUTH = guard.LiveAuthorization(guard._SEAL, 'test', MAINNET_GENESIS, 1)


class FakeRpc:
    def blockhash(self):
        return dict(blockhash=BLOCKHASH, lastValidBlockHeight=1)

    def call(self, method, params=None):
        return []


async def tick(watcher, start, end, pause=0.012):
    for slot in range(start, end):
        watcher.observe(slot)
        await asyncio.sleep(pause)


def nodes(version='4.4.0'):
    return [dict(pubkey=T1.identity, clientId='JitoLabs', version=version, tpuQuic='127.0.0.1:1')]


def make(tmp_path, live, address, cap=1_000_000, ledger_path=None, gossip=None):
    payers = dict(a=tx.Keypair.generate(), b=tx.Keypair.generate())
    watcher = SlotWatcher()
    watcher.observe(100)
    run_dir = str(tmp_path / 'run')
    os.makedirs(run_dir, exist_ok=True)
    pool = LinkPool(Wire(auth=AUTH if live else None, directory=str(tmp_path)), links_path=str(tmp_path / 'run' / 'links.jsonl'))
    ledger = Ledger(ledger_path or (str(tmp_path / 'ledger.jsonl') if live else None), cap)
    s = session.Session(CONFIG, FakeRpc(), watcher, pool, ledger, payers, run_dir, RUN_ID, AUTH if live else None,
                        gossip or session.Gossip(nodes()), address_of={T1.identity: address})
    return s, watcher, ledger, pool


def checked(address, version='4.4.0'):
    return targets.Checked(T1, True, '', 'JitoLabs', version, address)


def pairs_of(tmp_path):
    return journal.merged_pairs(str(tmp_path / 'run' / 'journal.jsonl'))


def probes_of(tmp_path):
    with open(tmp_path / 'run' / 'probes.jsonl') as f:
        return [json.loads(line) for line in f]


async def drive(tmp_path, live, cap=1_000_000, slots=None, gossip=None, pair=None, before=None):
    server, recorder, address = await start_server(str(tmp_path))
    s, watcher, ledger, pool = make(tmp_path, live, address, cap=cap, gossip=gossip)
    pair = pair or schedule.Pair(5, T1.identity, 'treated', 200, 'b')
    if before:
        before(s, watcher, ledger, pool)
    task = asyncio.create_task(s.run([pair], {T1.identity: checked(address)}))
    await (slots(watcher) if slots else tick(watcher, 101, 205))
    await task
    await settle()
    await pool.close()
    ledger.close()
    server.close()
    return recorder, ledger, s


def test_live_pair_sends_both_members_in_coin_order_with_the_gap(tmp_path):
    recorder, ledger, s = asyncio.run(drive(tmp_path, True))
    pair = pairs_of(tmp_path)[5]
    assert pair['status'] == 'sent'
    assert abs(pair['gap_ms'] - 10) < 3
    assert pair['link_age_s'] is not None and 'new_links' in pair  # the settled-connection view reads these from the journal
    assert 196 <= pair['polled_slot_at_fire'] < 199  # the anchor, plus however long the handshake took
    assert ledger.spent == 2 * session.TEMPLATE.total_fee()
    assert ledger.open == {}
    payloads = [p for _, p, uni in recorder.streams]
    senders = [tx.b58encode(tx.decode(p)['keys'][0]) for p in payloads]
    assert senders == [s.payers['b'].address, s.payers['a'].address]  # the coin said b goes first
    assert [tx.decode(p)['instructions'][-1][2] for p in payloads] == [b'bcx0005b', b'bcx0005a']
    for p in payloads:
        assert tx.verify_signature(p)


def test_each_window_opens_its_connection_early_and_releases_it(tmp_path):
    recorder, _, s = asyncio.run(drive(tmp_path, True))
    pair = pairs_of(tmp_path)[5]
    assert pair['new_links'] == 1 and pair['link_age_s'] > 0.2  # opened at F-40, not at the anchor
    assert pair['rtt_before_ms'] is not None
    assert s.pool.links == {}
    with open(tmp_path / 'run' / 'links.jsonl') as f:
        assert [json.loads(line)['event'] for line in f] == ['opened', 'released']


def test_a_link_the_server_dropped_after_warming_is_replaced_at_the_anchor(tmp_path):
    def before(s, watcher, ledger, pool):
        real = s._warm

        async def warm_then_lose_it(record):
            await real(record)
            protocol = pool.links[record['addr']].protocol
            protocol.closed_reason = '1:dropped'
            protocol._closed.set()  # as aioquic leaves it once the server's close has drained
        s._warm = warm_then_lose_it
    recorder, ledger, s = asyncio.run(drive(tmp_path, True, before=before))
    pair = pairs_of(tmp_path)[5]
    assert pair['status'] == 'sent' and pair['new_links'] == 2 and pair['link_age_s'] < 0.2
    assert len(recorder.streams) == 2 and ledger.spent == 2 * session.TEMPLATE.total_fee()
    assert s.pool.evictions[pair['addr']] == 1


def test_a_failed_warm_up_is_not_final(tmp_path):
    def before(s, watcher, ledger, pool):
        real = pool.wire.open
        calls = []

        async def fails_once(address, timeout=0):
            calls.append(address)
            if len(calls) == 1:
                raise asyncio.TimeoutError
            return await real(address, timeout)
        pool.wire.open = fails_once
    recorder, ledger, _ = asyncio.run(drive(tmp_path, True, before=before))
    pair = pairs_of(tmp_path)[5]
    assert pair['status'] == 'sent' and pair['new_links'] == 1 and len(recorder.streams) == 2


def test_signed_bytes_are_on_disk_and_match_what_was_sent(tmp_path):
    recorder, _, _ = asyncio.run(drive(tmp_path, True))
    pair = pairs_of(tmp_path)[5]
    journaled = {base64.b64decode(pair['raw_a']), base64.b64decode(pair['raw_b'])}
    assert {p for _, p, _ in recorder.streams} == journaled


def test_one_record_per_probe_with_everything_the_log_promises(tmp_path):
    asyncio.run(drive(tmp_path, True))
    lines = probes_of(tmp_path)
    assert len(lines) == 2
    first, second = lines
    assert (first['order'], first['role'], second['order'], second['role']) == (1, 'b', 2, 'a')
    for line in lines:
        assert line['status'] == 'sent' and line['signature'] and line['stream_id'] is not None
        assert line['t_send_wall_ns'] > 0 and line['t_send_mono_ns'] > 0
        assert line['conn_id'] and line['addr'].startswith('127.0.0.1')
        assert line['gossip_plan']['version'] == '4.4.0' and line['gossip_window']['version'] == '4.4.0'
        assert line['x_late_anchor'] is False and line['x_cap'] is False and line['x_version_changed'] is False
    assert first['conn_id'] == second['conn_id'] and first['ack_ms'] is not None and second['ack_ms'] is not None


def test_dry_run_walks_the_same_timeline_and_sends_nothing(tmp_path):
    recorder, _, _ = asyncio.run(drive(tmp_path, False))
    pair = pairs_of(tmp_path)[5]
    assert pair['status'] == 'rehearsed' and pair['gap_ms'] > 0
    assert recorder.connections == 1 and recorder.streams == []


def test_late_anchor_cancels_says_why_and_returns_the_reservation(tmp_path):
    async def slots(watcher):
        await tick(watcher, 101, 192)
        watcher.observe(199)  # the clock jumped past the anchor
        await tick(watcher, 200, 205)
    recorder, ledger, _ = asyncio.run(drive(tmp_path, True, slots=slots))
    pair = pairs_of(tmp_path)[5]
    assert pair['status'] == 'cancelled' and pair['cancel'] == 'late_anchor'
    assert recorder.streams == [] and ledger.spent == 0
    assert all(line['x_late_anchor'] is True and line['t_send_wall_ns'] is None for line in probes_of(tmp_path))


def test_cap_stops_before_anything_leaves(tmp_path):
    recorder, ledger, _ = asyncio.run(drive(tmp_path, True, cap=session.TEMPLATE.total_fee()))
    pair = pairs_of(tmp_path)[5]
    assert pair['status'] == 'cancelled' and pair['cancel'] == 'cap'
    assert recorder.streams == [] and ledger.spent == 0


def test_version_change_before_the_window_cancels(tmp_path):
    recorder, _, _ = asyncio.run(drive(tmp_path, True, gossip=session.Gossip(nodes('4.4.1'))))
    pair = pairs_of(tmp_path)[5]
    assert pair['cancel'] == 'version_changed' and '4.4.1' in pair['reason']
    assert recorder.streams == []
    assert probes_of(tmp_path)[0]['gossip_window']['version'] == '4.4.1'


def test_stale_clock_cancels(tmp_path):
    def before(s, watcher, ledger, pool):
        s.cfg = session.Config(prepare_slots=12, anchor_slots=4, cancel_slots=1, slot_timeout=5.0, gossip_seconds=3600,
                               max_clock_age_ms=0.0)
    recorder, ledger, _ = asyncio.run(drive(tmp_path, True, before=before))
    assert pairs_of(tmp_path)[5]['cancel'] == 'stale_clock'
    assert recorder.streams == [] and ledger.spent == 0


def test_unreachable_target_is_recorded_and_refunded(tmp_path):
    def before(s, watcher, ledger, pool):
        async def refuse(address, timeout=0):
            raise asyncio.TimeoutError
        pool.wire.open = refuse
    recorder, ledger, _ = asyncio.run(drive(tmp_path, True, before=before))
    assert pairs_of(tmp_path)[5]['cancel'] == 'no_link'
    assert ledger.spent == 0


def test_link_dying_at_the_first_write_refunds_the_pair(tmp_path):
    def before(s, watcher, ledger, pool):
        def boom(link, raw, record):
            raise LinkDown('evicted')
        s._write = boom
    recorder, ledger, _ = asyncio.run(drive(tmp_path, True, before=before))
    pair = pairs_of(tmp_path)[5]
    assert pair['cancel'] == 'send_failed' and ledger.spent == 0 and recorder.streams == []


def test_link_dying_between_the_writes_keeps_the_first_charged(tmp_path):
    def before(s, watcher, ledger, pool):
        real = s._write
        calls = []

        def second_fails(link, raw, record):
            calls.append(1)
            if len(calls) == 2:
                raise LinkDown('evicted')
            return real(link, raw, record)
        s._write = second_fails
    recorder, ledger, _ = asyncio.run(drive(tmp_path, True, before=before))
    pair = pairs_of(tmp_path)[5]
    assert pair['status'] == 'partial'
    assert ledger.spent == session.TEMPLATE.total_fee()
    assert len(recorder.streams) == 1


def test_unexpected_failure_after_firing_stays_charged(tmp_path):
    def before(s, watcher, ledger, pool):
        def explode(link, raw, record):
            raise RuntimeError('socket went away mid-write')
        s._write = explode
    recorder, ledger, _ = asyncio.run(drive(tmp_path, True, before=before))
    pair = pairs_of(tmp_path)[5]
    assert pair['status'] == 'send_error'
    assert ledger.spent == 2 * session.TEMPLATE.total_fee()  # nothing proves these bytes stayed home
    assert journal.progress(str(tmp_path / 'run' / 'journal.jsonl'))[0] == {T1.identity: 1}


def test_progress_counts_what_may_have_cost_money(tmp_path):
    (tmp_path / 'run').mkdir()
    path = tmp_path / 'run' / 'journal.jsonl'
    rows = [dict(seq=0, identity='x', status='sent'), dict(seq=1, identity='x', status='cancelled'),
            dict(seq=2, identity='y', status='crash_unknown'), dict(seq=3, identity='y', status='crash_unsent')]
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows) + '{"torn')
    done, seq = journal.progress(str(path))
    assert done == dict(x=1, y=1) and seq == 4


class PreflightRpc:
    def __init__(self, err=None, units=4_873, loaded=75_099, fee=None):
        self.reply = dict(err=err, unitsConsumed=units, loadedAccountsDataSize=loaded)
        self.fee = session.TEMPLATE.total_fee() if fee is None else fee

    def blockhash(self):
        return dict(blockhash=BLOCKHASH)

    def simulate(self, raw):
        decoded = tx.decode(raw)
        assert decoded['signatures'] == [b'\0' * 64]  # nothing signed, so nothing could be broadcast
        return self.reply

    def call(self, method, params):
        assert method == 'getFeeForMessage'
        return dict(value=self.fee)


PAYER = tx.Keypair.generate().address


def test_preflight_passes_on_a_clean_template():
    rows = session.preflight(PreflightRpc(), session.TEMPLATE, [PAYER, PAYER])
    assert len(rows) == 2 and rows[0]['fee'] == 13_520


@pytest.mark.parametrize('rpc,why', [(PreflightRpc(err='ProgramFailedToComplete'), 'simulation failed'),
                                     (PreflightRpc(units=6_001), 'over the limit'),
                                     (PreflightRpc(loaded=200_000), 'loaded bytes'),
                                     (PreflightRpc(fee=13_521), 'getFeeForMessage'),
                                     (PreflightRpc(fee=None), None)])
def test_preflight_stops_on_any_difference(rpc, why):
    if why is None:
        rpc.fee = None  # a null from the RPC is a difference too
    with pytest.raises(session.PreflightFailed, match=why or 'getFeeForMessage'):
        session.preflight(rpc, session.TEMPLATE, [PAYER])
