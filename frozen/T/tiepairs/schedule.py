"""Leader windows and the pair plan. Read-only."""
import hashlib
from dataclasses import asdict, dataclass

MIN_SPACING_SLOTS = 1200  # about 4 minutes, so that a large validator's pairs span the same hours as a small one's
MIN_GAP_SLOTS = 40  # about 8 s between any two pairs of a run: they share payers, so one must be over before the next goes


@dataclass(frozen=True)
class Pair:
    seq: int
    identity: str
    group: str
    first_slot: int
    first_sent: str  # 'a' or 'b': which payer goes first

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


def plan_pairs(windows, targets, pairs_per_target, after_slot, seed, done=None, start_seq=0, min_spacing=MIN_SPACING_SLOTS,
               min_gap=MIN_GAP_SLOTS, earlier=None):
    """One pair per window, nearest first: at most one window per target in min_spacing and one pair of the run in min_gap.
    `earlier` gives each target's last window already in this run, so a resumed run keeps the spacing.
    The coin is a hash of the seed, so a seed fixed before the run makes the send order auditable: anyone can recompute it
    from the seed and the slot."""
    done = done or {}
    want = {t.identity: max(pairs_per_target - done.get(t.identity, 0), 0) for t in targets}
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
    return [Pair(start_seq + i, t.identity, t.group, s, coin(seed, s, t.identity)) for i, (s, t) in enumerate(plan)]
