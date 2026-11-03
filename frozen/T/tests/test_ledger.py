import json

import pytest

from tiepairs.ledger import TORN_PENALTY, CapExceeded, Ledger, LedgerBusy


def test_cap_is_hard(tmp_path):
    path = str(tmp_path / 'l.jsonl')
    ledger = Ledger(path, 100)
    ledger.reserve(60, 'probe', 'sig1')
    with pytest.raises(CapExceeded):
        ledger.reserve(41, 'probe', 'sig2')
    ledger.reserve(40, 'probe', 'sig3')
    assert ledger.spent == 100 and ledger.left() == 0
    ledger.close()
    refs = [r.get('ref') for r in map(json.loads, open(path)) if r['kind'] == 'probe']
    assert refs == ['sig1', 'sig3']


def test_restart_keeps_what_was_spent(tmp_path):
    path = str(tmp_path / 'l.jsonl')
    ledger = Ledger(path, 100)
    ledger.reserve(70, 'probe', 'sig1')
    ledger.close()
    again = Ledger(path, 100)
    assert again.spent == 70
    with pytest.raises(CapExceeded):
        again.reserve(31, 'probe', 'sig2')
    again.close()


def test_a_reservation_comes_back_only_in_the_process_that_made_it(tmp_path):
    path = str(tmp_path / 'l.jsonl')
    ledger = Ledger(path, 100)
    ledger.reserve(70, 'probe', 'kept')
    ledger.reserve(20, 'probe', 'cancelled')
    ledger.release('cancelled', 'late_anchor')
    assert ledger.spent == 70
    ledger.close()
    again = Ledger(path, 100)
    assert again.spent == 70  # the release is durable
    with pytest.raises(KeyError):
        again.release('kept', 'a restart proves nothing about what left')
    assert again.spent == 70
    again.close()


def test_settled_reservation_cannot_be_released(tmp_path):
    ledger = Ledger(str(tmp_path / 'l.jsonl'), 100)
    ledger.reserve(70, 'probe', 'sig')
    ledger.settle('sig')
    with pytest.raises(KeyError):
        ledger.release('sig', 'it already left')
    ledger.close()


def test_project_cap_counts_every_block(tmp_path):
    path = str(tmp_path / 'l.jsonl')
    t = Ledger(path, 100, block='T', project_cap=150)
    t.reserve(100, 'probe', 't1')
    t.close()
    v = Ledger(path, 100, block='V', project_cap=150)
    assert v.spent == 100 and v.block_spent == 0 and v.left() == 50
    with pytest.raises(CapExceeded):
        v.reserve(51, 'probe', 'v1')
    v.reserve(50, 'probe', 'v2')
    v.close()
    assert Ledger(path, 100, block='T', project_cap=150).spent == 150


def test_cap_cannot_change_silently(tmp_path):
    path = str(tmp_path / 'l.jsonl')
    Ledger(path, 100, project_cap=500).close()
    with pytest.raises(CapExceeded):
        Ledger(path, 200, project_cap=500)
    with pytest.raises(CapExceeded):
        Ledger(path, 100, project_cap=900)
    Ledger(path, 200, project_cap=500, raise_cap=True).close()
    assert Ledger(path, 200, project_cap=500).cap == 200


def test_torn_tail_counts_against_the_cap(tmp_path):
    path = str(tmp_path / 'l.jsonl')
    ledger = Ledger(path, 1_000_000)
    ledger.reserve(70, 'probe', 'sig')
    ledger.close()
    with open(path, 'a') as f:
        f.write('{"kind": "probe", "lamp')
    again = Ledger(path, 1_000_000)
    assert again.spent == 70 + TORN_PENALTY
    again.close()
    third = Ledger(path, 1_000_000)
    assert third.spent == 70 + TORN_PENALTY  # repaired once, not charged twice
    third.close()


def test_one_process_at_a_time(tmp_path):
    path = str(tmp_path / 'l.jsonl')
    ledger = Ledger(path, 100)
    with pytest.raises(LedgerBusy):
        Ledger(path, 100)
    ledger.close()


def test_memory_ledger_writes_nothing(tmp_path):
    ledger = Ledger(None, 100)
    ledger.reserve(60, 'probe', 'sig1')
    with pytest.raises(CapExceeded):
        ledger.reserve(41, 'probe', 'sig2')
    assert list(tmp_path.iterdir()) == []
