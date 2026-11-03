from tiepairs import contrasts, tx
from tiepairs.contrasts import A, BARE_DATA_BYTES as DATA, D, K_X, K_Y, KINDS, S_ONE, S_TWO, V_HIGH, V_LOW, Y

PAYER = tx.Keypair.generate()
BLOCKHASH = tx.b58encode(bytes(range(32)))


def winner(key, kind):
    x, y = key(KINDS[kind].x), key(KINDS[kind].y)
    return 'x' if x > y else 'y' if y > x else 'tie'


def test_probes_match_the_published_numbers():
    assert (A.cost(DATA), A.reward(), A.priority(DATA)) == (2335, 3500, 1_498_287)
    assert (Y.cost(DATA), Y.reward(), Y.priority(DATA)) == (2835, 8500, 2_997_179)


def test_two_contrasts_separate_every_candidate_key():
    keys = dict(ratio=lambda t: t.priority(DATA), price=lambda t: t.cu_price, fee=lambda t: t.total_fee(), cost=lambda t: -t.cost(DATA))
    patterns = {name: (winner(key, 'C4'), winner(key, 'C6')) for name, key in keys.items()}
    assert patterns == dict(ratio=('x', 'y'), price=('x', 'x'), fee=('x', 'x'), cost=('y', 'y'))


def test_c6_keeps_the_ratio_gap_whatever_share_of_the_base_fee_counts():
    for base in (0, 2_500, 5_000):
        d, a = (D.priority_fee() + base) / D.cost(DATA), (A.priority_fee() + base) / A.cost(DATA)
        assert a > d


def test_k_sets_the_ratio_against_every_small_lambda():
    assert K_X.priority(DATA) > K_Y.priority(DATA)
    for lam in (0, .5, 1, 2, 2.4):
        assert K_Y.reward() - lam * K_Y.cost(DATA) > K_X.reward() - lam * K_X.cost(DATA)


def test_k_favours_the_ratio_whatever_share_of_the_signature_fee_counts():
    for share in (0, 0.25, 0.5, 0.75, 1):
        assert rate(K_X, share) > rate(K_Y, share)
    assert (K_X.total_fee(), K_X.cost(DATA), K_X.reward(), K_X.priority(DATA)) == (11_000, 2_335, 8_500, 3_638_698)
    assert (K_Y.total_fee(), K_Y.cost(DATA), K_Y.reward(), K_Y.priority(DATA)) == (13_500, 3_335, 11_000, 3_297_362)


def test_k_is_no_contrast_under_the_other_keys():
    assert K_X.cu_price > K_Y.cu_price and K_X.cost(DATA) < K_Y.cost(DATA) and K_X.total_fee() < K_Y.total_fee()


def test_the_bare_probe_on_the_wire_is_the_published_shape():
    raw, _ = tx.build_bare_probe(PAYER, tx.lock_address('x'), BLOCKHASH, A)
    wire = tx.wire_priority(raw)
    assert (wire['cost'], wire['priority'], wire['fee'], wire['data_bytes'], wire['write_locks']) == (2335, 1_498_287, 6_000, 31, 2)
    assert tx.verify_signature(raw)


def test_only_the_curve_goes_inside_the_window():
    inside = {k for k in KINDS if contrasts.regime(k) == contrasts.WINDOW}
    assert inside == set(contrasts.CURVE) and {KINDS[k].gap_ms for k in KINDS if k not in inside} == {10.0}
    assert [KINDS[k].gap_ms for k in contrasts.CURVE] == [2.0, 10.0, 40.0]


def test_the_curve_is_c4_with_the_better_probe_second():
    for k in contrasts.CURVE:
        assert KINDS[k].x_second and (KINDS[k].x, KINDS[k].y) == (KINDS['C4'].x, KINDS['C4'].y)
    assert not any(KINDS[k].x_second for k in KINDS if k not in contrasts.CURVE)


def test_the_design_is_48_pairs_per_session_per_target():
    assert sum(contrasts.QUOTA.values()) == 48 and set(contrasts.QUOTA) == set(KINDS)
    fees = sum(KINDS[k].fee() * n for k, n in contrasts.QUOTA.items()) * 10 * 3
    assert fees == 22_320_180


def test_a_deck_holds_the_quota_and_depends_only_on_its_inputs():
    cards = contrasts.deck('seed', 1, 'who')
    assert sorted(cards) == sorted(k for k, n in contrasts.QUOTA.items() for _ in range(n))
    assert cards == contrasts.deck('seed', 1, 'who')
    assert cards != contrasts.deck('seed', 2, 'who') and cards != contrasts.deck('seed', 1, 'other')


def test_a_restart_deals_what_is_left():
    assert contrasts.remaining(['C4', 'E', 'C4', 'C6'], dict(C4=1)) == ['E', 'C4', 'C6']
    assert contrasts.remaining(['C4', 'E'], {}) == ['C4', 'E']


def test_the_signature_probes_have_the_numbers_in_h6():
    assert (S_TWO.total_fee(), S_TWO.cost(DATA), S_TWO.reward(), S_TWO.priority(DATA)) == (10_001, 3_055, 5_001, 1_636_452)
    assert (S_ONE.total_fee(), S_ONE.cost(DATA), S_ONE.reward(), S_ONE.priority(DATA)) == (7_000, 2_335, 4_500, 1_926_369)
    assert KINDS['S1'].fee() == 16_001 and KINDS['S2'].fee() == 17_001


def rate(template, share):
    return (template.priority_fee() + share * 5_000 * template.signatures) / (template.cost(DATA) + 1)


def pattern(share):
    return tuple('x' if rate(KINDS[k].x, share) > rate(KINDS[k].y, share) else 'y' for k in ('S1', 'S2'))


def test_s_brackets_the_share_of_the_signature_fee():
    assert [pattern(s) for s in (0, 0.37, 0.39, 0.5, 0.75, 0.77, 1)] == [('y', 'y'), ('y', 'y'), ('x', 'y'), ('x', 'y'), ('x', 'y'), ('x', 'x'), ('x', 'x')]


def test_agave_arithmetic_reads_as_half():
    assert S_TWO.priority(DATA) > A.priority(DATA) and S_TWO.priority(DATA) < S_ONE.priority(DATA)


def test_a_two_signature_probe_on_the_wire():
    cosigner = tx.Keypair.generate()
    raw, signature = tx.build_bare_probe(PAYER, tx.lock_address('x'), BLOCKHASH, S_TWO, cosigner)
    d = tx.decode(raw)
    assert d['header'] == (2, 1, 2) and d['keys'][:2] == [PAYER.public, cosigner.public] and d['keys'][2] == tx.key_bytes(tx.lock_address('x'))
    wire = tx.wire_priority(raw)
    assert (wire['fee'], wire['cost'], wire['priority'], wire['write_locks'], wire['data_bytes']) == (10_001, 3_055, 1_636_452, 2, 31)
    assert tx.verify_signature(raw) and signature == tx.b58encode(d['signatures'][0]) and len(d['signatures']) == 2
    program, accounts, _ = d['instructions'][-1]
    assert d['keys'][program] == tx.key_bytes(tx.SYSTEM_PROGRAM) and accounts == [0, 2]  # the cosigner takes no part in the transfer


def test_a_broken_second_signature_fails():
    import pytest
    raw, _ = tx.build_bare_probe(PAYER, tx.lock_address('x'), BLOCKHASH, S_TWO, tx.Keypair.generate())
    broken = bytearray(raw)
    broken[1 + 64 + 3] ^= 1
    with pytest.raises(Exception):
        tx.verify_signature(bytes(broken))


def test_a_message_for_simulation_carries_a_zero_signature_per_signer():
    message = tx.bare_message(PAYER.public, tx.lock_address('x'), BLOCKHASH, S_TWO, tx.Keypair.generate().public)
    assert tx.decode(tx.unsigned_wire(message))['signatures'] == [b'\0' * 64] * 2


def test_a_one_signature_probe_stays_the_published_shape():
    raw, _ = tx.build_bare_probe(PAYER, tx.lock_address('x'), BLOCKHASH, A, tx.Keypair.generate())
    assert tx.decode(raw)['header'] == (1, 0, 2) and len(tx.decode(raw)['signatures']) == 1


def test_the_read_groups_are_the_clients_whose_source_was_read():
    assert [contrasts.source(c) for c in ('Agave', 'JitoLabs', 'Firedancer')] == ['read'] * 3
    assert [contrasts.source(c) for c in ('AgaveBam', 'HarmonicAgave', 'HarmonicFiredancer', 'FireBAM', 'Rakurai')] == ['unread'] * 5


def test_v1_probes_sit_just_above_and_just_below_a():
    a = A.priority(DATA)
    high, low = (V_HIGH.priority(contrasts.V1_DATA_BYTES), V_LOW.priority(contrasts.V1_DATA_BYTES))
    assert low < a < high
    assert (high - a) / a < 0.002 and (a - low) / a < 0.003
    assert V_HIGH.total_fee() == V_LOW.total_fee() == A.total_fee()
    assert (V_HIGH.cost(contrasts.V1_DATA_BYTES), V_LOW.cost(contrasts.V1_DATA_BYTES)) == (2331, 2341)


def test_a_v1_probe_on_the_wire_gives_the_same_numbers():
    for template in (V_HIGH, V_LOW):
        raw, signature = tx.build_v1_probe(PAYER, tx.lock_address('x'), BLOCKHASH, template)
        wire = tx.wire_priority(raw)
        assert wire['fee'] == template.total_fee() and wire['cost'] == template.cost(contrasts.V1_DATA_BYTES)
        assert wire['priority'] == template.priority(contrasts.V1_DATA_BYTES) and wire['write_locks'] == 2
        assert tx.verify_signature(raw) and raw[0] == tx.V1_VERSION and len(raw) < 300
        assert tx.b58encode(tx.decode(raw)['signatures'][0]) == signature


def test_the_v1_layout_matches_a_transaction_parsed_by_the_block_reader():
    import base64
    import importlib.util
    import os
    path = os.path.join(os.path.dirname(__file__), '..', '..', 'design', 'v1_wire.py')
    if not os.path.exists(path):
        return
    spec = importlib.util.spec_from_file_location('v1_wire', path)
    v1_wire = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v1_wire)
    raw, _ = tx.build_v1_probe(PAYER, tx.lock_address('x'), BLOCKHASH, V_LOW)
    parsed = v1_wire.parse(raw)
    assert parsed['config'] == dict(priority_fee=1_000, cu_limit=1_010, loaded=32 * 1024) and parsed['mask'] == 0b1111
    assert parsed['addresses'][0] == PAYER.public and base64.b64encode(parsed['signatures'][0])

