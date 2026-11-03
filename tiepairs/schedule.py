"""Leader windows and the pair plan. Read-only."""
import hashlib
from dataclasses import asdict, dataclass

from . import contrasts

MIN_SPACING_SLOTS = 1200  # about 4 minutes, so that a large validator's pairs span the same hours as a small one's
MIN_GAP_SLOTS = 40  # about 8 s between any two pairs of a run: they share payers, so one must be over before the next goes


@dataclass(frozen=True)
class Pair:
    seq: int
    identity: str
    group: str
    first_slot: int
    first_sent: str  # 'a' or 'b': which payer goes first
    kind: str = ''  # empty in block T, where both members are one template
    x_role: str = ''  # which payer carries the x template of the kind
    regime: str = 'buffered'

    def asdict(self):
        return asdict(self)


def window_starts(slots):
    """First slot of each leader run. A run longer than one window counts once: only its start follows another leader,
    so only there can a pair sit in the buffer until the validator starts producing."""
    have = set(slots)
    return sorted(s for s in have if s - 1 not in have)


def epoch_slots(rpc, identity, epoch_first, slot_in_epoch):
    reply = rpc.call('getLeaderSchedule', [slot_in_epoch, dict(identity=identity)])
    if not reply:
        return []
    return [epoch_first + s for s in reply.get(identity, [])]


def upcoming_windows(rpc, identity, epoch):
    """Window starts in this epoch and, when its schedule is already known, the next one."""
    first = epoch['absoluteSlot'] - epoch['slotIndex']
    slots = epoch_slots(rpc, identity, first, epoch['absoluteSlot'])
    following = first + epoch['slotsInEpoch']
    slots += epoch_slots(rpc, identity, following, following)
    return window_starts(slots)


def coin(seed, slot, identity):
    return 'a' if hashlib.sha256(f'{seed}:{identity}:{slot}'.encode()).digest()[0] & 1 else 'b'


def payer_of_x(seed, slot, identity):
    return 'a' if hashlib.sha256(f'{seed}:{identity}:{slot}'.encode()).digest()[1] & 1 else 'b'


def x_role(seed, slot, identity, kind):
    """Which payer carries x: a coin of its own, unless the kind fixes x as the later member."""
    if contrasts.KINDS[kind].x_second:
        return 'a' if coin(seed, slot, identity) == 'b' else 'b'
    return payer_of_x(seed, slot, identity)


def assign(windows, targets, want, after_slot, min_spacing, min_gap, earlier):
    """Windows in slot order, with at most one window per target in min_spacing and one pair of the run in min_gap."""
    want = dict(want)
    owners = {t.identity: t for t in targets}
    open_windows = sorted((s, t.identity) for t in targets for s in windows.get(t.identity, []) if s > after_slot)
    plan = []
    latest = dict(earlier or {})
    for s, identity in open_windows:
        if not want[identity] or s - latest.get(identity, -min_spacing) < min_spacing:
            continue
        if plan and s - plan[-1][0] < min_gap:
            continue
        plan.append((s, owners[identity]))
        latest[identity] = s
        want[identity] -= 1
    return plan


def plan_pairs(windows, targets, pairs_per_target, after_slot, seed, done=None, start_seq=0, min_spacing=MIN_SPACING_SLOTS,
               min_gap=MIN_GAP_SLOTS, earlier=None):
    """One pair per window, nearest first. `earlier` gives each target's last window already in this run, so a resumed run
    keeps the spacing. The coin is a hash of the seed, so a seed fixed before the run makes the send order auditable: anyone
    can recompute it from the seed and the slot."""
    done = done or {}
    want = {t.identity: max(pairs_per_target - done.get(t.identity, 0), 0) for t in targets}
    plan = assign(windows, targets, want, after_slot, min_spacing, min_gap, earlier)
    return [Pair(start_seq + i, t.identity, t.group, s, coin(seed, s, t.identity)) for i, (s, t) in enumerate(plan)]


def plan_block(windows, targets, decks, after_slot, seed, start_seq=0, limit=None, min_spacing=MIN_SPACING_SLOTS,
               min_gap=MIN_GAP_SLOTS, earlier=None):
    """`decks`: target -> the kinds still to send, in order. `limit` caps the pairs per target, for a first small run."""
    want = {t.identity: min(len(decks[t.identity]), limit if limit is not None else len(decks[t.identity])) for t in targets}
    plan = assign(windows, targets, want, after_slot, min_spacing, min_gap, earlier)
    used = dict.fromkeys(decks, 0)
    pairs = []
    for i, (s, t) in enumerate(plan):
        kind = decks[t.identity][used[t.identity]]
        used[t.identity] += 1
        pairs.append(Pair(start_seq + i, t.identity, t.group, s, coin(seed, s, t.identity), kind, x_role(seed, s, t.identity, kind),
                          contrasts.regime(kind)))
    return pairs
