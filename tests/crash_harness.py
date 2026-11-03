"""Runs a live session against the sink and, when told to, kills its own process with SIGKILL at an exact step.
No hook in the production code: the harness wraps Session methods from outside."""
import asyncio
import json
import os
import random
import signal
import sys

from tiepairs import guard, journal, schedule, session, targets, tx
from tiepairs.ledger import Ledger
from tiepairs.rpc import MAINNET_GENESIS
from tiepairs.slots import SlotWatcher
from tiepairs.targets import Target
from tiepairs.wire import LinkPool, Wire

AUTH = guard.LiveAuthorization(guard._SEAL, 'test', MAINNET_GENESIS, 1)
TARGET = Target('A' * 44, 'treated', 'JitoLabs', '4.4.0')
BLOCKHASH = tx.b58encode(bytes(range(32)))
WANTED = 3
SPACING = 40


class FakeRpc:
    def blockhash(self):
        return dict(blockhash=BLOCKHASH, lastValidBlockHeight=1)

    def call(self, method, params=None):
        return []


def die():
    os.kill(os.getpid(), signal.SIGKILL)


def arm(s, point, seq):
    if point == 'after_prepared':
        real_prepare = s.prepare

        def prepare(pair, record, latest, templates):
            out = real_prepare(pair, record, latest, templates)
            if pair.seq == seq:
                s.journal.write(out)
                die()
            return out
        s.prepare = prepare
    elif point == 'after_reserved':
        real_fire = s.fire

        async def fire(link, pair, record, reserved):
            if pair.seq == seq:
                die()
            return await real_fire(link, pair, record, reserved)
        s.fire = fire
    elif point in ('after_first_send', 'after_second_send'):
        real_write = s._write
        calls = []
        limit = 1 if point == 'after_first_send' else 2

        def write(link, raw, record):
            out = real_write(link, raw, record)
            if record['seq'] == seq:
                calls.append(1)
                if len(calls) == limit:
                    die()
            return out
        s._write = write
    elif point == 'after_sent_record':
        real_journal = s.journal.write

        def write_journal(record):
            real_journal(record)
            if record.get('status') == 'sent' and record.get('seq') == seq:
                die()
        s.journal.write = write_journal


async def tick(watcher, start, pause):
    slot = start
    while True:
        watcher.observe(slot)
        slot += 1
        await asyncio.sleep(pause)


async def main(work_dir, address, point, seq, start_slot):
    run_dir = os.path.join(work_dir, 'run')
    os.makedirs(run_dir, exist_ok=True)
    ledger = Ledger(os.path.join(work_dir, 'ledger.jsonl'), 10_000_000)
    settled = session.recover(run_dir, ledger)
    done, next_seq = journal.progress(os.path.join(run_dir, 'journal.jsonl'))
    windows = {TARGET.identity: [start_slot + 40 + SPACING * i for i in range(WANTED + 4)]}
    plan = schedule.plan_pairs(windows, [TARGET], WANTED, start_slot, 'seed', done=done, start_seq=next_seq, min_spacing=1)
    payers = {role: tx.Keypair(bytes([ord(role)]) * 32) for role in 'ab'}
    watcher = SlotWatcher()
    watcher.observe(start_slot)
    nodes = [dict(pubkey=TARGET.identity, clientId='JitoLabs', version='4.4.0', tpuQuic=address)]
    pool = LinkPool(Wire(auth=AUTH))
    cfg = session.Config(prepare_slots=14, anchor_slots=4, cancel_slots=1, slot_timeout=5.0, gossip_seconds=3600,
                         ack_wait_seconds=0.05, fast_rtt_seconds=0.05)
    s = session.Session(cfg, FakeRpc(), watcher, pool, ledger, payers, run_dir, '20261101T000000Z-live-abcx', AUTH, session.Gossip(nodes))
    if point != 'none':
        arm(s, point, seq)
    ticker = asyncio.create_task(tick(watcher, start_slot + 1, 0.012))
    checked = targets.Checked(TARGET, True, '', 'JitoLabs', '4.4.0', address)
    await s.run(plan, {TARGET.identity: checked})
    ticker.cancel()
    await pool.close()
    ledger.close()
    print(json.dumps(dict(settled=[(p['seq'], p['status']) for p in settled], planned=[p.seq for p in plan])))


random.seed(0)
asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])))
