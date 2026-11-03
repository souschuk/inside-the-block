import json
import os
import stat
import struct

import pytest

from tiepairs import session, tx

BLOCKHASH = tx.b58encode(bytes(range(32)))
LOCK = tx.lock_address('test-run')


def pair(memo_a=b'abc0001a', memo_b=b'abc0001b', template=session.TEMPLATE):
    a, b = tx.Keypair.generate(), tx.Keypair.generate()
    return a, b, tx.build_probe(a, LOCK, BLOCKHASH, template, memo_a), tx.build_probe(b, LOCK, BLOCKHASH, template, memo_b)


def test_base58():
    assert tx.b58decode(tx.SYSTEM_PROGRAM) == b'\0' * 32
    for address in (tx.COMPUTE_BUDGET, tx.MEMO_PROGRAM):
        assert tx.b58encode(tx.key_bytes(address)) == address
    assert tx.b58encode(b'\0\0\x01') == '112'
    with pytest.raises(ValueError):
        tx.key_bytes('11')


@pytest.mark.parametrize('n,encoded', [(0, b'\x00'), (127, b'\x7f'), (128, b'\x80\x01'), (16383, b'\xff\x7f'),
                                       (16384, b'\x80\x80\x01')])
def test_compact_u16(n, encoded):
    assert tx.compact_u16(n) == encoded
    assert tx.read_compact_u16(encoded, 0) == (n, len(encoded))


# Published golden values for the eternally-black probe library (31 data bytes, 2 write locks, 1 signature), used here
# only as an external check of the arithmetic: cu limit, cu price, loaded limit -> priority fee, total, cost, priority.
GOLDEN = [(1_000, 1_000_000, 32 * 1024, 1_000, 6_000, 2_335, 1_498_287),
          (3_000, 500_000, 32 * 1024, 1_500, 6_500, 4_335, 922_509),
          (1_000, 1_000_000, 64 * 1024 * 1024, 1_000, 6_000, 18_711, 187_045),
          (1_000, 3_000_000, 32 * 1024, 3_000, 8_000, 2_335, 2_354_452),
          (1_500, 4_000_000, 32 * 1024, 6_000, 11_000, 2_835, 2_997_179)]


@pytest.mark.parametrize('limit,price,loaded,priority_fee,total,cost,priority', GOLDEN)
def test_fee_cost_priority(limit, price, loaded, priority_fee, total, cost, priority):
    t = tx.Template(limit, price, loaded)
    assert t.priority_fee() == priority_fee
    assert t.total_fee() == total
    assert t.cost(31) == cost
    assert t.priority(31) == priority


def test_priority_fee_rounds_up():
    assert tx.Template(1, 1).priority_fee() == 1
    assert tx.Template(1_000_000, 1).priority_fee() == 1
    assert tx.Template(1_000_001, 1).priority_fee() == 2


def test_reward_is_priority_fee_plus_half_the_signature_fee():
    assert tx.Template(1_000, 1_000_000).reward() == 1_000 + 2_500


def test_probe_template_keeps_the_published_priority_level():
    t = session.TEMPLATE
    priority = t.priority(tx.probe_data_bytes(session.MEMO_LEN))
    assert abs(priority - 1_498_287) / 1_498_287 < 0.01
    assert t.loaded_limit >= 75_099  # the Memo program account, measured by simulation


def test_pair_ties_under_every_key_on_the_wire():
    _, _, (raw_a, _), (raw_b, _) = pair()
    a, b = tx.wire_priority(raw_a), tx.wire_priority(raw_b)
    assert a == b
    assert a['write_locks'] == 2
    assert a['data_bytes'] == tx.probe_data_bytes(session.MEMO_LEN)
    t = session.TEMPLATE
    assert a['fee'] == t.total_fee()
    assert a['cost'] == t.cost(a['data_bytes'])
    assert a['priority'] == t.priority(a['data_bytes'])
    assert len(raw_a) == len(raw_b) <= tx.PACKET_DATA_SIZE


def test_probe_shape():
    payer_a, payer_b, (raw_a, sig_a), (raw_b, sig_b) = pair()
    a, b = tx.decode(raw_a), tx.decode(raw_b)
    assert a['header'] == (1, 0, 3)
    assert a['keys'][0] == payer_a.public and b['keys'][0] == payer_b.public and payer_a.public != payer_b.public
    assert a['keys'][1] == b['keys'][1] == tx.key_bytes(LOCK)  # the shared write lock is what forces an order
    assert a['blockhash'] == tx.b58decode(BLOCKHASH)
    assert tx.verify_signature(raw_a) and tx.verify_signature(raw_b)
    assert sig_a == tx.b58encode(a['signatures'][0]) and sig_a != sig_b
    kinds = [a['keys'][program] for program, _, _ in a['instructions']]
    budget, system, memo = tx.key_bytes(tx.COMPUTE_BUDGET), tx.key_bytes(tx.SYSTEM_PROGRAM), tx.key_bytes(tx.MEMO_PROGRAM)
    assert kinds == [budget, budget, budget, system, memo]
    (_, _, limit), (_, _, price), (_, _, loaded), (_, accounts, transfer), (_, memo_accounts, label) = a['instructions']
    assert limit == b'\x02' + struct.pack('<I', session.TEMPLATE.cu_limit)
    assert price == b'\x03' + struct.pack('<Q', session.TEMPLATE.cu_price)
    assert loaded == b'\x04' + struct.pack('<I', session.TEMPLATE.loaded_limit)
    assert accounts == [0, 1]
    assert transfer == struct.pack('<IQ', 2, 0)
    assert memo_accounts == [] and label == b'abc0001a'


def test_tampered_probe_fails_signature():
    _, _, (raw, _), _ = pair()
    broken = bytearray(raw)
    broken[-1] ^= 1
    with pytest.raises(Exception):
        tx.verify_signature(bytes(broken))


def test_rfc8032_vector():
    seed = bytes.fromhex('9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60')
    pair_ = tx.Keypair(seed)
    assert pair_.public.hex() == 'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a'
    assert pair_.sign(b'').hex().startswith('e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bac')


def test_keypair_file(tmp_path):
    path = tmp_path / 'k.json'
    pair_ = tx.Keypair.generate()
    pair_.dump(str(path))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert tx.Keypair.load(str(path)).address == pair_.address
    with pytest.raises(FileExistsError):
        pair_.dump(str(path))
    raw = json.loads(path.read_text())
    raw[40] ^= 1
    broken = tmp_path / 'broken.json'
    broken.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        tx.Keypair.load(str(broken))


def test_transfer_and_memo_messages_are_signed():
    payer = tx.Keypair.generate()
    raw, _ = tx.build_transfer(payer, LOCK, 123, BLOCKHASH)
    d = tx.decode(raw)
    assert d['header'] == (1, 0, 1) and d['instructions'][0][2] == struct.pack('<IQ', 2, 123) and tx.verify_signature(raw)
    raw, _ = tx.build_memo(payer, b'tiepairs H5 sha256:00', BLOCKHASH)
    assert tx.decode(raw)['instructions'][0][2] == b'tiepairs H5 sha256:00' and tx.verify_signature(raw)


def test_lock_address_depends_on_run():
    assert tx.lock_address('a') != tx.lock_address('b')
    assert len(tx.key_bytes(tx.lock_address('a'))) == 32


def test_memo_label_length_is_enforced():
    assert session.memo_label('abc', 12, 'a') == b'abc0012a'
    with pytest.raises(ValueError):
        session.memo_label('abcd', 12, 'a')
