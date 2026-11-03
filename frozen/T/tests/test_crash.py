"""Kills the sender with SIGKILL at each step of a pair, restarts it, and checks the money and the record."""
import base64
import json
import os
import random
import signal
import subprocess
import sys
import time

import pytest

from tiepairs import journal, session, tx

HERE = os.path.dirname(__file__)
FEE = session.TEMPLATE.total_fee()


@pytest.fixture
def sink(tmp_path):
    port_file, received = str(tmp_path / 'port'), str(tmp_path / 'received')
    process = subprocess.Popen([sys.executable, os.path.join(HERE, 'quic_sink.py'), str(tmp_path), port_file, received])
    for _ in range(200):
        if os.path.exists(port_file):
            break
        time.sleep(0.02)
    yield open(port_file).read(), received
    process.kill()
    process.wait()


def harness(tmp_path, address, point, seq, start_slot):
    return subprocess.Popen([sys.executable, os.path.join(HERE, 'crash_harness.py'), str(tmp_path), address, point, str(seq), str(start_slot)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ, PYTHONPATH=os.path.dirname(HERE)), text=True)


def finish(process):
    out, err = process.communicate(timeout=30)
    return process.returncode, out, err


def received(path):
    time.sleep(0.15)
    if not os.path.exists(path):
        return []
    return [base64.b64decode(line) for line in open(path).read().split()]


def ledger_state(tmp_path):
    charged, released = {}, set()
    for line in open(tmp_path / 'ledger.jsonl'):
        record = json.loads(line)
        if record.get('kind') == 'probe':
            charged[record['ref']] = record['lamports']
        elif record.get('kind') == 'release':
            released.add(record['ref'])
    return {ref: fee for ref, fee in charged.items() if ref not in released}


def check_invariants(tmp_path, received_path):
    pairs = journal.merged_pairs(str(tmp_path / 'run' / 'journal.jsonl'))
    payloads = received(received_path)
    charged = ledger_state(tmp_path)
    assert len(payloads) == len(set(payloads)), 'a transaction went out twice'
    journaled = {base64.b64decode(p[k]) for p in pairs.values() for k in ('raw_a', 'raw_b') if k in p}
    for payload in payloads:
        assert payload in journaled, 'something left that the disk does not hold'
        signature = tx.b58encode(tx.decode(payload)['signatures'][0])
        assert signature in charged, 'something left without a reservation'
    spent = [p for p in pairs.values() if p['status'] in journal.SPENT_STATUSES]
    assert sum(charged.values()) == 2 * FEE * len(spent)
    assert all(p['status'] != 'prepared' for p in pairs.values())
    with open(tmp_path / 'run' / 'probes.jsonl') as f:
        logged = [json.loads(line) for line in f]
    assert len(logged) == 2 * len(pairs) and sorted({r['seq'] for r in logged}) == sorted(pairs)
    return pairs, payloads, spent


CASES = [('after_prepared', 'crash_unsent', 0), ('after_reserved', 'crash_unknown', 0), ('after_first_send', 'crash_unknown', 1),
         ('after_second_send', 'crash_unknown', 2), ('after_sent_record', 'sent', 2)]


@pytest.mark.parametrize('point,status,left', CASES)
def test_kill_at_each_step_then_resume(tmp_path, sink, point, status, left):
    address, received_path = sink
    code, _, err = finish(harness(tmp_path, address, point, 1, 1000))
    assert code == -signal.SIGKILL, err
    before = received(received_path)
    assert len(before) == 2 + left  # pair 0 finished, pair 1 died where it was told to

    code, out, err = finish(harness(tmp_path, address, 'none', 0, 2000))
    assert code == 0, err
    report = json.loads(out)
    pairs, payloads, spent = check_invariants(tmp_path, received_path)
    assert pairs[1]['status'] == status
    if status != 'sent':
        assert report['settled'] == [[1, status]]
    assert payloads[:len(before)] == before  # the restart resent nothing
    assert len(spent) == 3  # a pair that may have cost money counts, so the plan is not exceeded
    assert 1 not in report['planned']


def test_kill_at_random_moments(tmp_path, sink):
    address, received_path = sink
    rng = random.Random(5)
    start = 1000
    for _ in range(4):
        process = harness(tmp_path, address, 'none', 0, start)
        time.sleep(rng.uniform(0.9, 2.2))
        process.send_signal(signal.SIGKILL)
        process.communicate(timeout=10)
        start += 1000
    code, _, err = finish(harness(tmp_path, address, 'none', 0, start))
    assert code == 0, err
    pairs, payloads, spent = check_invariants(tmp_path, received_path)
    assert len(spent) >= 3


def test_second_process_cannot_spend_while_the_first_holds_the_ledger(tmp_path, sink):
    address, _ = sink
    first = harness(tmp_path, address, 'none', 0, 1000)
    time.sleep(0.6)
    second = harness(tmp_path, address, 'none', 0, 1000)
    code, _, err = finish(second)
    assert code != 0 and 'LedgerBusy' in err
    first.kill()
    first.communicate()
