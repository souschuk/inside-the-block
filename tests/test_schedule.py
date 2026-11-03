import collections

from tiepairs import schedule, targets
from tiepairs.targets import Target

T1 = Target('A' * 44, 'treated', 'JitoLabs', '4.4.0')
T2 = Target('B' * 44, 'control', 'JitoLabs', '4.3.2')


def test_a_long_run_counts_once():
    assert schedule.window_starts([8, 9, 10, 11, 12, 13, 14, 15, 40, 41, 42, 43]) == [8, 40]


class FakeRpc:
    def __init__(self, schedules):
        self.schedules = schedules

    def call(self, method, params):
        assert method == 'getLeaderSchedule'
        slot, options = params
        reply = self.schedules.get(slot // 1000)
        if reply is None:
            return None
        return {options['identity']: reply}


def test_windows_cross_the_epoch_boundary():
    rpc = FakeRpc({5: [100, 101, 102, 103, 900, 901, 902, 903], 6: [4, 5, 6, 7]})
    epoch = dict(absoluteSlot=5500, slotIndex=500, slotsInEpoch=1000)
    assert schedule.upcoming_windows(rpc, 'x', epoch) == [5100, 5900, 6004]


def test_next_epoch_may_be_unknown():
    rpc = FakeRpc({5: [100, 101, 102, 103]})
    epoch = dict(absoluteSlot=5500, slotIndex=500, slotsInEpoch=1000)
    assert schedule.upcoming_windows(rpc, 'x', epoch) == [5100]


def test_plan_takes_nearest_windows_in_slot_order():
    windows = {T1.identity: [100, 200, 300, 400], T2.identity: [150, 250]}
    plan = schedule.plan_pairs(windows, [T1, T2], 2, 120, 'seed', min_spacing=1)
    assert [(p.first_slot, p.group) for p in plan] == [(150, 'control'), (200, 'treated'), (250, 'control'), (300, 'treated')]
    assert [p.seq for p in plan] == [0, 1, 2, 3]


def test_plan_keeps_pairs_of_different_targets_apart():
    windows = {T1.identity: [100, 300], T2.identity: [104, 200]}
    plan = schedule.plan_pairs(windows, [T1, T2], 1, 0, 'seed', min_spacing=1)
    assert [(p.first_slot, p.group) for p in plan] == [(100, 'treated'), (200, 'control')]


def test_plan_continues_after_a_restart():
    windows = {T1.identity: [100, 200, 300]}
    plan = schedule.plan_pairs(windows, [T1], 2, 0, 'seed', done={T1.identity: 1}, start_seq=7, min_spacing=1)
    assert [(p.seq, p.first_slot) for p in plan] == [(7, 100)]


def test_plan_spacing_limits_new_connections():
    windows = {T1.identity: [100, 104, 120, 172, 180, 300]}
    plan = schedule.plan_pairs(windows, [T1], 9, 0, 'seed', min_spacing=72)
    assert [p.first_slot for p in plan] == [100, 172, 300]


def test_coin_is_reproducible_and_balanced():
    assert schedule.coin('s', 5, 'x') == schedule.coin('s', 5, 'x')
    counts = collections.Counter(schedule.coin('s', slot, 'x') for slot in range(4000))
    assert abs(counts['a'] - counts['b']) < 200


def node(pubkey, client='JitoLabs', version='4.4.0', address='1.1.1.1:8002'):
    return dict(pubkey=pubkey, clientId=client, version=version, tpuQuic=address)


def test_verify_refuses_the_relayer_port_outside_its_group():
    c = targets.verify([T1], [node(T1.identity, address='1.2.3.4:11228')])[0]
    assert not c.ok and 'Jito Relayer' in c.reason
    arm = targets.Target(T1.identity, 'relayer', T1.client, T1.version)
    assert targets.verify([arm], [node(T1.identity, address='1.2.3.4:11228')])[0].ok


def test_verify_accepts_a_matching_target():
    c = targets.verify([T1], [node(T1.identity)])[0]
    assert c.ok and c.tpu_quic == '1.1.1.1:8002'


def test_verify_rejects_a_changed_version():
    c = targets.verify([T1], [node(T1.identity, version='4.4.1')])[0]
    assert not c.ok and '4.4.1' in c.reason


def test_verify_rejects_a_changed_client():
    c = targets.verify([T1], [node(T1.identity, client='Agave')])[0]
    assert not c.ok


def test_verify_rejects_missing_and_shared():
    assert not targets.verify([T1], [])[0].ok
    assert not targets.verify([T1], [node(T1.identity, address=None)])[0].ok
    shared = [node(T1.identity), node('x', address='1.1.1.1:8002')]
    c = targets.verify([T1], shared)[0]
    assert not c.ok and 'shared' in c.reason


def test_a_resumed_run_keeps_the_spacing_from_its_earlier_pairs():
    windows = {T1.identity: [100, 200, 300, 400]}
    plan = schedule.plan_pairs(windows, [T1], 3, 0, 'seed', done={T1.identity: 1}, min_spacing=150, earlier={T1.identity: 120})
    assert [p.first_slot for p in plan] == [300]


S1 = Target('S' * 44, 'bam', 'AgaveBam', '4.4.0', 'shared')


def test_a_block_plan_deals_kinds_from_each_deck_in_order():
    windows = {T1.identity: [100, 300, 500], S1.identity: [200, 400]}
    decks = {T1.identity: ['C4', 'W10'], S1.identity: ['E', 'C6']}
    plan = schedule.plan_block(windows, [T1, S1], decks, 0, 'seed', start_seq=3, min_spacing=1, min_gap=1)
    assert [(p.seq, p.first_slot, p.kind, p.regime) for p in plan] == [(3, 100, 'C4', 'buffered'), (4, 200, 'E', 'buffered'),
                                                                       (5, 300, 'W10', 'window'), (6, 400, 'C6', 'buffered')]


def test_a_block_plan_is_auditable_from_the_seed():
    plan = schedule.plan_block({T1.identity: [100]}, [T1], {T1.identity: ['C4']}, 0, 'seed', min_spacing=1)
    assert plan[0].first_sent == schedule.coin('seed', 100, T1.identity)
    assert plan[0].x_role == schedule.payer_of_x('seed', 100, T1.identity)


def test_a_curve_pair_sends_x_second_whatever_the_coin_says():
    windows = {T1.identity: list(range(100, 300, 4))}
    plan = schedule.plan_block(windows, [T1], {T1.identity: ['W2', 'W10', 'W40'] * 16}, 0, 'seed', min_spacing=1, min_gap=1)
    assert {p.first_sent for p in plan} == {'a', 'b'} and all(p.x_role != p.first_sent for p in plan)
    assert all(p.first_sent == schedule.coin('seed', p.first_slot, T1.identity) for p in plan)


def test_the_limit_caps_pairs_per_target():
    windows = {T1.identity: [100, 200, 300], S1.identity: [150, 250]}
    plan = schedule.plan_block(windows, [T1, S1], {T1.identity: ['C4', 'C6', 'E'], S1.identity: ['C4', 'E']}, 0, 'seed', limit=1, min_spacing=1, min_gap=1)
    assert sorted(p.identity for p in plan) == sorted([T1.identity, S1.identity])


def test_the_two_coins_are_independent():
    both = collections.Counter((schedule.coin('s', i, 'x'), schedule.payer_of_x('s', i, 'x')) for i in range(4_000))
    assert len(both) == 4 and min(both.values()) > 800

