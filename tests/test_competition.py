import importlib.util
import os


path = os.path.join(os.path.dirname(__file__), '..', 'tools', 'competition.py')
spec = importlib.util.spec_from_file_location('competition', path)
competition = importlib.util.module_from_spec(spec)
spec.loader.exec_module(competition)


def test_a_probe_row_counts_who_stood_where():
    rows = [('a', 9.0), ('b', 1.0), ('us', 5.0), ('c', 7.0), ('d', 2.0)]
    row = competition.probe_row(rows, 'us')
    assert (row['position'], row['size'], row['rank'], row['ahead_lower'], row['behind_higher']) == (2, 5, 2, 1, 1)


def test_priority_from_the_wire_equals_what_the_sender_computed():
    from tiepairs import contrasts, session, tx
    payer, cosigner = tx.Keypair.generate(), tx.Keypair.generate()
    lock, blockhash = tx.lock_address('x'), tx.b58encode(bytes(range(32)))
    built = [tx.build_bare_probe(payer, lock, blockhash, t, cosigner) for t in (contrasts.A, contrasts.D, contrasts.S_TWO, contrasts.K_Y)]
    built += [tx.build_v1_probe(payer, lock, blockhash, contrasts.V_LOW), tx.build_probe(payer, lock, blockhash, session.TEMPLATE, b'abc0001a')]
    for raw, signature in built:
        wire = tx.wire_priority(raw)
        parsed = competition.parse_v1(raw) if raw[0] == tx.V1_VERSION else competition.parse_legacy(raw)
        assert tx.b58encode(parsed['signature']) == signature
        assert competition.priority(parsed, dict(fee=wire['fee'])) == wire['priority']


def test_a_transaction_without_budget_instructions_gets_the_defaults():
    from tiepairs import tx
    keys = [bytes([1]) * 32, bytes([2]) * 32, tx.key_bytes(tx.SYSTEM_PROGRAM), bytes([3]) * 32]
    message = tx.compile_message(1, 0, 2, keys, tx.b58encode(bytes(range(32))), [(2, [0, 1], b'\0' * 12), (3, [], b'')])
    parsed = competition.parse_legacy(tx.compact_u16(1) + bytes(64) + message)
    assert competition.requested(parsed) == (203_000, 0, competition.DEFAULT_LOADED)
    assert competition.priority(parsed, dict(fee=5_000)) == 2_500 * 1_000_000 // (203_000 + 720 + 600 + 3 + 16_384 + 1)
    parsed = competition.parse_legacy(tx.compact_u16(1) + bytes(64) + b'\x80' + message + b'\0')
    assert parsed['version'] == 0 and len(parsed['keys']) == 4
