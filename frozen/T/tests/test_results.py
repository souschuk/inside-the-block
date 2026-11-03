import json
import random

import pytest

from tiepairs import results, schedule


def sent(seq=0, first='a', identity='L1', group='treated', first_slot=100, **extra):
    record = dict(seq=seq, identity=identity, group=group, gossip_plan=dict(client='JitoLabs', version='4.4.0'), first_slot=first_slot,
                  first_sent=first, status='sent', sig_a=f'sa{seq}', sig_b=f'sb{seq}', gap_ms=10.1, rtt_before_ms=40.0)
    record.update(extra)
    return record


class FakeRpc:
    def __init__(self, landed, leader='L1', recipient=None):
        self.landed = landed  # signature -> (slot, position, err)
        self.leader = leader
        self.recipient = recipient

    def call(self, method, params):
        if method == 'getSignatureStatuses':
            value = [dict(slot=self.landed[s][0], err=self.landed[s][2]) if s in self.landed else None for s in params[0]]
            return dict(value=value)
        if method == 'getBlock':
            slot = params[0]
            assert params[1]['maxSupportedTransactionVersion'] == 1
            size = 50
            signatures = [f'other{i}' for i in range(size)]
            for s, (landed_slot, position, _) in self.landed.items():
                if landed_slot == slot:
                    signatures[position] = s
            return dict(signatures=signatures, rewards=[dict(pubkey=self.recipient or self.leader, rewardType='Fee')], blockhash='h', blockTime=1)
        if method == 'getSlotLeaders':
            return [self.leader]
        if method == 'getTransaction':
            return dict(meta=dict(fee=13_520, computeUnitsConsumed=4_873, err=None))
        raise AssertionError(method)


def collect(tmp_path, pairs, rpc):
    with open(tmp_path / 'journal.jsonl', 'w') as f:
        for p in pairs:
            f.write(json.dumps(p) + '\n')
    return results.collect(rpc, str(tmp_path))


def test_first_sent_first_uses_send_order_not_payer_label(tmp_path):
    rpc = FakeRpc(dict(sa0=(100, 7, None), sb0=(100, 3, None)))
    outcome = collect(tmp_path, [sent(first='b')], rpc)[0]
    assert outcome['kind'] == 'same_slot' and outcome['first_sent_first'] is True
    assert outcome['first']['position'] == 3 and outcome['fee_equal'] is True


def test_later_sent_first_is_scored_false(tmp_path):
    rpc = FakeRpc(dict(sa0=(100, 7, None), sb0=(100, 3, None)))
    assert collect(tmp_path, [sent(first='a')], rpc)[0]['first_sent_first'] is False


def test_every_exclusion_has_its_own_class(tmp_path):
    rpc = FakeRpc(dict(sa1=(100, 1, None), sb1=(101, 1, None),   # different slots
                       sa2=(100, 2, None),                       # one landed
                       sa4=(100, 3, dict(InstructionError=[3, 'x'])), sb4=(100, 4, None),  # failed
                       sa5=(300, 1, None), sb5=(300, 2, None)))  # outside the window
    pairs = [sent(1), sent(2), sent(3), sent(4), sent(5), sent(6, status='cancelled', cancel='late_anchor')]
    outcomes = collect(tmp_path, pairs, rpc)
    assert [o['kind'] for o in outcomes] == ['different_slots', 'one_landed', 'none_landed', 'failed', 'outside_window', 'not_sent']
    assert outcomes[0]['x_different_slots'] is True and outcomes[0]['x_one_landed'] is False
    assert outcomes[1]['x_one_landed'] is True
    assert outcomes[5]['x_not_sent'] is True and outcomes[5]['x_cancel'] == 'late_anchor'
    probes = [json.loads(line) for line in open(tmp_path / 'probes.final.jsonl')]
    assert len(probes) == 2 * len(pairs)
    assert probes[3]['landing'] is None and probes[3]['x_one_landed'] is True


def test_a_crashed_pair_is_still_looked_up_on_chain(tmp_path):
    rpc = FakeRpc(dict(sa0=(100, 1, None), sb0=(100, 2, None)))
    outcome = collect(tmp_path, [sent(status='crash_unknown')], rpc)[0]
    assert outcome['kind'] == 'crash_unknown' and outcome['first']['position'] == 1 and outcome['x_crash_unknown'] is True
    assert not results.primary(outcome)


def test_lead_comes_from_the_rpc_stamp_of_the_first_slot(tmp_path):
    with open(tmp_path / 'stamps.jsonl', 'w') as f:
        f.write(json.dumps(dict(slot=100, stamp_ms=10_000, arrival_ms=10_030)) + '\n')
    pair = sent(first='a', sent_a=dict(wall_ns=9_000_000_000), sent_b=dict(wall_ns=9_010_000_000))
    rpc = FakeRpc(dict(sa0=(100, 1, None), sb0=(100, 2, None)))
    outcome = collect(tmp_path, [pair], rpc)[0]
    assert outcome['lead_ms'] == 10_000 - 9_010 - 20  # stamp minus the later send minus half the RTT
    assert outcome['clock_offset_bound_ms'] == 30 and outcome['x_lead_under_150ms'] is False


def test_rtt_report_uses_the_corrected_rtt_and_bounds_a_swap(tmp_path):
    rows = []
    t = 1_000_000_000_000
    for i in range(50):  # raw RTT swings with ACK delay, the corrected one does not
        raw = 75.0 if i % 2 else 50.0
        rows.append(dict(t_ns=t + int(raw * 1e6), addr='a:1', conn='c', rtt_ms=raw, rtt_adj_ms=50.0))
        t += 10_000_000
    with open(tmp_path / 'rtt.jsonl', 'w') as f:
        for row in rows:
            f.write(json.dumps(row) + '\n')
    report = results.rtt_report(str(tmp_path))['a:1']
    assert report['close_pairs'] == 49 and report['slower_by_gap'] == 0 and report['swap_bound'] < 0.08


def test_verify_finds_a_moved_probe(tmp_path):
    landed = dict(sa0=(100, 1, None), sb0=(100, 2, None))
    pair = sent()
    pair['first_sent'] = schedule.coin('seed', 100, 'L1')
    collect(tmp_path, [pair], FakeRpc(landed))
    assert results.verify(FakeRpc(landed), str(tmp_path), 'seed')[1] == []
    moved = dict(sa0=(100, 5, None), sb0=(100, 2, None))
    problems = results.verify(FakeRpc(moved), str(tmp_path), 'seed')[1]
    assert any('not at index' in p for p in problems)


def test_verify_finds_an_order_the_seed_does_not_give(tmp_path):
    landed = dict(sa0=(100, 1, None), sb0=(100, 2, None))
    pair = sent()
    pair['first_sent'] = 'a' if schedule.coin('seed', 100, 'L1') == 'b' else 'b'
    collect(tmp_path, [pair], FakeRpc(landed))
    assert any('seed' in p for p in results.verify(FakeRpc(landed), str(tmp_path), 'seed')[1])


def test_another_leader_is_outside_the_window(tmp_path):
    rpc = FakeRpc(dict(sa0=(100, 1, None), sb0=(100, 2, None)), leader='SOMEONE')
    assert collect(tmp_path, [sent()], rpc)[0]['kind'] == 'outside_window'


def test_leader_comes_from_the_schedule_not_the_fee_reward(tmp_path):
    rpc = FakeRpc(dict(sa0=(100, 1, None), sb0=(100, 2, None)), leader='L1', recipient='2FL6757b')  # as on GnC339vk
    outcome = collect(tmp_path, [sent()], rpc)[0]
    assert outcome['kind'] == 'same_slot'
    assert outcome['first']['leader'] == 'L1' and outcome['first']['fee_recipient'] == '2FL6757b'


def test_window_wide_view_counts_different_slots(tmp_path):
    rpc = FakeRpc(dict(sa0=(100, 9, None), sb0=(101, 1, None)))
    outcome = collect(tmp_path, [sent()], rpc)[0]
    assert outcome['kind'] == 'different_slots' and outcome['first_sent_first'] is True


def outcome(group, first_sent_first, kind='same_slot', identity=None, version='4.4.0', gap=10.1, lead=400.0):
    return dict(group=group, kind=kind, first_sent_first=first_sent_first, identity=identity or group, version=version,
                gap_ms=gap, lead_ms=lead, cancel=None if kind != 'not_sent' else 'late_anchor')


def sample(k_t, n_t, k_c, n_c):
    return ([outcome('treated', i < k_t) for i in range(n_t)] + [outcome('control', i < k_c, version='4.3.2') for i in range(n_c)])


def test_one_weak_treated_validator_fails_the_rule():
    rows = [outcome('treated', True, identity='t1') for _ in range(30)] + [outcome('treated', True, identity='t2') for _ in range(30)]
    rows += [outcome('treated', i < 15, identity='t3') for i in range(30)]
    rows += [outcome('control', i < 45, version='4.3.2') for i in range(90)]
    report = results.analyze(rows)
    assert report['views']['primary']['cells']['treated']['share'] > 0.8
    assert report['views']['primary']['contrast']['fisher_greater'] < 0.01
    assert report['verdict'] == 'not_supported'


def test_a_small_difference_fails_the_rule():
    assert results.analyze(sample(90, 90, 76, 90))['verdict'] == 'not_supported'


def test_a_treated_validator_with_few_pairs_is_not_evaluable():
    rows = sample(28, 30, 15, 30) + [outcome('treated', True, identity='few') for _ in range(9)]
    assert results.analyze(rows)['verdict'].startswith('not_evaluable')


def test_the_relayer_arm_cannot_withdraw_the_verdict():
    rows = sample(28, 30, 15, 30) + [outcome('relayer', True, version='4.3.2', gap=20.0) for _ in range(30)]
    assert results.analyze(rows)['verdict'] == 'supported'


def test_clear_fifo_is_supported():
    report = results.analyze(sample(28, 30, 15, 30))
    assert report['verdict'] == 'supported'
    assert report['views']['primary']['contrast']['table'] == [[28, 2], [15, 15]]


def test_shift_without_fifo_is_not_supported():
    # p < 0.01 but only 70% first-sent first: a shift, not the rule the code predicts
    report = results.analyze(sample(35, 50, 15, 50))
    assert report['views']['primary']['contrast']['fisher_greater'] < 0.01
    assert report['verdict'] == 'not_supported'


def test_no_difference_is_not_supported():
    assert results.analyze(sample(18, 30, 17, 30))['verdict'] == 'not_supported'


def test_too_few_pairs_is_not_evaluable():
    assert results.analyze(sample(15, 15, 5, 15))['verdict'].startswith('not_evaluable')


def test_wrong_gap_withdraws_the_hypothesis():
    rows = sample(28, 30, 15, 30)
    for r in rows:
        r['gap_ms'] = 13.0
    assert results.analyze(rows)['verdict'].startswith('withdrawn')


def test_exclusions_stay_visible():
    rows = sample(28, 30, 15, 30) + [outcome('treated', None, kind='one_landed'), outcome('control', None, kind='not_sent')]
    report = results.analyze(rows)
    assert report['exclusions']['treated']['kinds']['one_landed'] == 1
    assert report['exclusions']['control']['not_sent'] == {'late_anchor': 1}
    text = results.render(report)
    assert 'one_landed' in text and 'Виключення' in text


def test_selection_bound_counts_lost_pairs_against_the_claim():
    rows = sample(28, 30, 15, 30) + [outcome('treated', None, kind='one_landed')] * 5 + [outcome('control', None, kind='one_landed')] * 5
    bound = results.analyze(rows)['selection_bound']
    assert bound['treated'] == dict(k=28, n=35)
    assert bound['control'] == dict(k=20, n=35)


def test_sensitivity_views_filter():
    rows = sample(28, 30, 15, 30)
    rows[0]['lead_ms'] = 100.0
    rows[1]['gap_ms'] = 14.0
    for row in rows:
        row['link_age_s'] = 30.0
    rows[2]['link_age_s'] = 1.0
    report = results.analyze(rows)
    assert report['views']['lead_150ms']['cells']['treated']['n'] == 29
    assert report['views']['gap_10ms']['cells']['treated']['n'] == 29
    assert report['views']['settled_link']['cells']['treated']['n'] == 29
    assert report['young_link']['treated'] == dict(n=30, young=1)
    assert report['views']['primary']['cells']['treated']['n'] == 30


def test_power_matches_brute_force_on_a_small_case():
    from tiepairs import power
    from tiepairs.stats import binom_pmf
    n = 24
    total = sum(binom_pmf(a, n, 0.95) * binom_pmf(b, n, 0.5) for a in range(n + 1) for b in range(n + 1) if power.passes(a, n, b, n))
    assert power.exact_power(n, 0.95, n, 0.5) == pytest.approx(total, abs=1e-6)


def test_false_alarm_stays_under_alpha():
    from tiepairs import power
    for p in (0.5, 0.58, 0.8, 0.9):
        assert power.exact_power(30, p, 30, p) < 0.01


def test_simulated_outcomes_recover_the_rates():
    rng = random.Random(1)
    rows = [outcome('treated', rng.random() < 0.95) for _ in range(90)] + [outcome('control', rng.random() < 0.58, version='4.3.2') for _ in range(90)]
    report = results.analyze(rows)
    assert 0.85 < report['views']['primary']['cells']['treated']['share'] <= 1
    assert report['verdict'] == 'supported'
