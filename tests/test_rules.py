import json
import random

from tiepairs import contrasts, rules


def pair(identity, contrast, x_first, first_sent_first=True, kind='same_slot', run='r1', x_sent_first=True, client='Agave', version='4.3.2'):
    return dict(identity=identity, group='g', client=client, version=version, contrast=contrast, regime='buffered', kind=kind, run=run,
                x_first=x_first, first_sent_first=first_sent_first, x_sent_first=x_sent_first, gap_ms=10.0, lead_ms=900.0, link_age_s=7.0)


def block(identity, c4, c6, n=20, ties=None, **extra):
    """c4 and c6 are how many of n pairs put x first."""
    rows = [pair(identity, 'C4', i < c4, x_sent_first=i % 2 == 0, **extra) for i in range(n)]
    rows += [pair(identity, 'C6', i < c6, x_sent_first=i % 2 == 0, **extra) for i in range(n)]
    rows += [pair(identity, 'E', None, first_sent_first=i < (n if ties is None else ties), **extra) for i in range(n)]
    for row in rows:
        if row['contrast'] == 'E':
            row['x_first'] = False
    return rows


def test_the_ratio_pattern_is_named():
    report = rules.analyze(block('v', 20, 0))
    row = report['targets']['v']
    assert row['rule'] == 'reward_per_cost' and row['strict'] is True and row['tie_break'] == 'arrival'


def test_price_or_fee_pattern():
    assert rules.analyze(block('v', 20, 20))['targets']['v']['rule'] == 'price_fee_or_no_loaded_cost'


def test_cost_alone_pattern():
    assert rules.analyze(block('v', 0, 0))['targets']['v']['rule'] == 'cost_alone'


def balanced_send_order(rows):
    for i, row in enumerate(rows):
        row['first_sent_first'] = i % 2 == 0 if row['contrast'] in ('C4', 'C6') else row['first_sent_first']
    return rows


def test_no_rule_is_a_verdict():
    row = rules.analyze(balanced_send_order(block('v', 12, 12, n=24, ties=12)))['targets']['v']
    assert row['rule'] == 'no_rule_seen' and row['tie_break'] == 'no_relation_seen'


def test_too_few_pairs_for_a_null_is_undetermined():
    row = rules.analyze(balanced_send_order(block('v', 5, 5, n=10)))['targets']['v']
    assert row['rule'] == 'undetermined'


def test_weak_evidence_beside_nulls_is_not_called_no_rule():
    rows = balanced_send_order(block('strong', 9, 1, n=10))
    for i in range(9):
        rows += balanced_send_order(block(f'null{i}', 12, 12, n=24))
    assert rules.analyze(rows)['targets']['strong']['rule'] == 'undetermined'


def test_strict_first_in_first_out_is_named():
    rows = block('v', 12, 12, n=24)
    for row in rows:
        row['first_sent_first'] = True
    assert rules.analyze(rows)['targets']['v']['rule'] == 'arrival'


def test_later_sent_first_is_named():
    rows = block('v', 12, 12, n=24)
    for row in rows:
        row['first_sent_first'] = False
    assert rules.analyze(rows)['targets']['v']['rule'] == 'reverse_arrival'


def test_one_decided_contrast_is_partial_and_a_conflict_is_mixed():
    assert rules.analyze(balanced_send_order(block('v', 20, 10)))['targets']['v']['rule'] == 'partial'
    assert rules.analyze(balanced_send_order(block('v', 0, 20)))['targets']['v']['rule'] == 'mixed'


def test_few_pairs_are_not_evaluable():
    row = rules.analyze(block('v', 9, 0, n=9))['targets']['v']
    assert row['rule'] == 'not_evaluable' and row['tie_break'] == 'not_evaluable' and row['strict'] is False


def test_a_rule_that_holds_loosely_is_not_strict():
    row = rules.analyze(block('v', 18, 2))['targets']['v']
    assert row['rule'] == 'reward_per_cost' and row['strict'] is True
    row = rules.analyze(block('v', 36, 12, n=48))['targets']['v']
    assert row['rule'] == 'reward_per_cost' and row['strict'] is False


def test_later_first_is_reverse_arrival():
    assert rules.analyze(block('v', 20, 0, ties=1))['targets']['v']['tie_break'] == 'reverse_arrival'


def test_holm_covers_every_validator():
    rows = []
    for i in range(10):
        rows += balanced_send_order(block(f'v{i}', 15, 5))  # p = 0.04 each, which does not survive twenty tests
    report = rules.analyze(rows)
    assert {r['rule'] for r in report['targets'].values()} == {'undetermined'}


def test_lost_pairs_count_against_the_prediction():
    rows = block('v', 20, 0) + [pair('v', 'C4', None, kind='one_landed') for _ in range(5)]
    row = rules.analyze(rows)['targets']['v']
    assert row['worst_case']['C4'] == dict(k=20, n=25, share=0.8)


def test_only_landed_pairs_are_scored():
    rows = block('v', 20, 0) + [pair('v', 'C4', None, kind='none_landed'), pair('v', 'C4', None, kind='outside_window')]
    row = rules.analyze(rows)['targets']['v']
    assert row['cells']['C4']['n'] == 20 and row['funnel']['none_landed'] == 1


def test_different_slots_count_in_the_primary_and_not_in_the_same_slot_view():
    rows = block('v', 20, 0) + [pair('v', 'C4', True, kind='different_slots')]
    row = rules.analyze(rows)['targets']['v']
    assert row['cells']['C4']['n'] == 21 and row['views']['same_slot']['C4']['n'] == 20


def test_sessions_stay_apart():
    rows = block('v', 10, 0, n=10, run='r1') + block('v', 0, 0, n=10, run='r2')
    sessions = rules.analyze(rows)['targets']['v']['sessions']
    assert sessions['r1']['C4']['k'] == 10 and sessions['r2']['C4']['k'] == 0


def test_the_report_renders():
    text = rules.render(rules.analyze(block('v', 20, 0)))
    assert 'reward_per_cost' in text and 'Landing' in text


def curve(identity, kind, wins, n=12, gap=None):
    rows = [pair(identity, kind, i < wins, x_sent_first=False) for i in range(n)]
    for row in rows:
        row.update(regime='window', gap_ms=gap if gap is not None else contrasts.KINDS[kind].gap_ms)
    return rows


def test_the_curve_is_read_per_gap():
    rows = block('v', 20, 0) + curve('v', 'W2', 3) + curve('v', 'W10', 1) + curve('v', 'W40', 0)
    row = rules.analyze(rows)['targets']['v']
    assert [row['cells'][k]['k'] for k in contrasts.CURVE] == [3, 1, 0] and row['curve_gaps']['W40'] == 40.0


def test_curve_gaps_do_not_withdraw_a_validator():
    rows = block('v', 20, 0) + curve('v', 'W2', 3) * 5 + curve('v', 'W40', 0) * 5
    row = rules.analyze(rows)['targets']['v']
    assert row['rule'] == 'reward_per_cost' and row['gap_median'] == 10.0


def test_a_wrong_gap_on_buffered_pairs_withdraws_the_validator():
    rows = block('v', 20, 0) + curve('v', 'W10', 1)
    for r in rows:
        if r['regime'] == 'buffered':
            r['gap_ms'] = 12.5
    row = rules.analyze(rows)['targets']['v']
    assert (row['rule'], row['strict'], row['tie_break']) == ('withdrawn', False, 'withdrawn')


def signatures(identity, client, s1, s2, n=9, version='4.3.2'):
    rows = [pair(identity, 'S1', i < s1, client=client, version=version) for i in range(n)]
    return rows + [pair(identity, 'S2', i < s2, client=client, version=version) for i in range(n)]


def named(identity, client):
    return block(identity, 20, 0, client=client)


def test_half_of_the_signature_fee_is_named_per_group():
    rows = []
    for i, client in enumerate(('Agave', 'JitoLabs', 'Firedancer')):
        rows += named(f'r{i}', client) + signatures(f'r{i}', client, 9, 0)
    for i, client in enumerate(('AgaveBam', 'HarmonicAgave')):
        rows += named(f'u{i}', client) + signatures(f'u{i}', client, 9, 9)
    shares = rules.analyze(rows)['signature_share']
    assert shares['read']['share'] == 'half' and shares['unread']['share'] == 'above_0.76'
    assert (shares['read']['S1']['n'], len(shares['read']['validators'])) == (27, 3)


def test_the_low_pattern_and_a_null_are_kept_apart():
    rows = named('a', 'Agave') + signatures('a', 'Agave', 0, 0, n=20) + named('b', 'Rakurai') + signatures('b', 'Rakurai', 10, 0, n=20)
    shares = rules.analyze(rows)['signature_share']
    assert shares['read']['share'] == 'below_0.38' and shares['unread']['share'] == 'undetermined'


def test_a_validator_under_another_key_is_left_out_of_s():
    rows = named('a', 'Agave') + signatures('a', 'Agave', 9, 0, n=12)
    rows += block('b', 0, 0, client='Agave') + signatures('b', 'Agave', 0, 0, n=12)
    shares = rules.analyze(rows)['signature_share']
    assert shares['read']['validators'] == ['a'] and shares['read']['S2']['n'] == 12


def test_few_pooled_pairs_are_not_evaluable():
    shares = rules.analyze(named('a', 'Agave') + signatures('a', 'Agave', 9, 0))['signature_share']
    assert shares['read']['share'] == 'not_evaluable' and shares['unread']['share'] == 'not_evaluable'


def test_k_is_pooled_over_the_validators_named_reward_per_cost():
    rows = block('a', 20, 0) + [pair('a', 'K', True) for _ in range(6)]
    rows += block('b', 20, 20) + [pair('b', 'K', False) for _ in range(6)]
    report = rules.analyze(rows)
    assert (report['k']['k'], report['k']['n']) == (6, 6) and (report['pooled']['K']['k'], report['pooled']['K']['n']) == (6, 12)


def test_what_was_lost_is_visible_per_validator():
    rows = block('v', 20, 0) + [dict(pair('v', 'C4', None, kind='not_sent'), cancel='late_anchor'), dict(pair('v', 'E', None, kind='not_sent'), cancel='version_changed')]
    rows[0]['x_fee_differs'] = True
    row = rules.analyze(rows)['targets']['v']
    assert row['not_sent'] == dict(late_anchor=1, version_changed=1) and row['moved'] == 1 and row['fee_differs'] == 1
    text = rules.render(rules.analyze(rows))
    assert 'not sent: late_anchor 1, version_changed 1' in text and '62/144' in text


FLEET = (('7QQGNm3p', 'Agave', '4.3.2'), ('HEL1USMZ', 'JitoLabs', '4.4.0'), ('3SkE34PV', 'JitoLabs', '4.3.2'), ('9eGrDohd', 'Firedancer', '26.10.5'),
         ('ana2y2Yv', 'Rakurai', '4.3.2'), ('DRpbCBMx', 'AgaveBam', '4.4.0'), ('5Cchr1XG', 'AgaveBam', '4.3.2'), ('Fd7btgyS', 'HarmonicAgave', '4.4.0'),
         ('JUPiTERr', 'HarmonicFiredancer', '26.10.5'), ('9jxgosAf', 'FireBAM', '26.10.5'))


def whole_block(rng):
    """Every kind on every target over three sessions, as results.collect leaves them, with some of them lost."""
    rows = []
    for name, client, version in FLEET:
        for session in (1, 2, 3):
            for kind, count in contrasts.QUOTA.items():
                planned = contrasts.KINDS[kind]
                x_wins = dict(C6=False, S2=False, VL=False).get(kind, True)
                for i in range(count):
                    landed = rng.random() < 0.9
                    rows.append(dict(identity=name, group=name, client=client, version=version, contrast=kind, regime=planned.regime, run=f'r{session}',
                                     kind='same_slot' if landed else rng.choice(['one_landed', 'none_landed', 'not_sent']), x_first=x_wins if landed else None,
                                     first_sent_first=(rng.random() < 0.5) if landed else None, x_sent_first=(not planned.x_second and rng.random() < 0.5) if landed else None,
                                     gap_ms=planned.gap_ms - 0.18 if landed else None, lead_ms=900.0, link_age_s=7.0))
    return rows


def test_the_whole_block_runs_through_analysis_and_report(tmp_path):
    rows = whole_block(random.Random(5))
    report = rules.analyze(rows)
    assert {r['rule'] for r in report['targets'].values()} == {'reward_per_cost'} and report['clients'] == 10
    assert {g['share'] for g in report['signature_share'].values()} == {'half'}
    assert set(report['pooled']) >= set(contrasts.KINDS) - {'E'}
    run = tmp_path / 'r1'
    run.mkdir()
    (run / 'outcomes.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    (run / 'rtt.jsonl').write_text(''.join(json.dumps(dict(addr='h:1', t_ns=int(1e9 + i * 1e7), rtt_ms=30.0)) + '\n' for i in range(5)))
    (run / 'meta.json').write_text(json.dumps(dict(targets=[dict(identity='7QQGNm3p', client='Agave', version='4.3.2', ingress='direct', ok=True, reason=''),
                                                            dict(identity='9eGrDohd', client='Firedancer', version='26.10.6', ingress='direct', ok=False, reason='shows Firedancer 26.10.6')])))
    report, text = rules.write_report([str(run)], str(tmp_path / 'report'))
    assert report['targets']['7QQGNm3p']['ingress'] == 'direct' and 'shows Firedancer 26.10.6' in text and 'Targets left out' in text
    for heading in ('Overtaking', 'Signature fee', 'Landing', 'By session', 'Sensitivity', 'All validators', 'Could the network'):
        assert heading in text
    assert json.loads((tmp_path / 'report' / 'summary.json').read_text())['signature_share']['read']['share'] == 'half'


def test_a_missing_validator_does_not_shrink_the_family():
    rows = block('v', 17, 3)  # p = 0.0026, which needs the family of twenty to fail
    assert rules.analyze(balanced_send_order(rows))['targets']['v']['rule'] == 'undetermined'


def test_sensitivity_views_filter():
    rows = block('v', 20, 0)
    rows[0]['lead_ms'] = 50.0
    rows[1]['gap_ms'] = 14.0
    rows[2]['link_age_s'] = 1.0
    views = rules.analyze(rows)['targets']['v']['views']
    assert (views['lead_150ms']['C4']['n'], views['gap_10ms']['C4']['n'], views['settled_link']['C4']['n']) == (19, 19, 19)


def test_v1_pools_by_code_base():
    rows = [pair('v', 'VH', True), pair('w', 'VH', False, client='FireBAM', version='26.10.5')]
    pooled = rules.analyze(rows)['pooled']
    assert (pooled['VH agave']['k'], pooled['VH firedancer']['k']) == (1, 0)


def test_a_lopsided_send_order_is_flagged():
    rows = block('v', 20, 0)
    for r in rows:
        r['x_sent_first'] = True
    assert rules.analyze(rows)['targets']['v']['unbalanced'] is True

