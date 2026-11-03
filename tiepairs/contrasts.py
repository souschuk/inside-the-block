"""Probe kinds of block P and what each candidate key predicts for them. Hypotheses: hypotheses/H6.md.

Two priced contrasts identify the key among the candidates of the published studies. C4 sets reward per cost against cost
alone. C6 sets reward per cost against CU price and lamport fee: D pays more on both and still has less reward per cost,
because it asks for 64 MiB of loaded data. E is two identical probes, for the tie-break. K sets the ratio, whatever share of
the signature fee it counts, against an additive key, reward - lambda * cost, for every lambda below 2.5. VH and VL put a v1 probe just above and just below the legacy A:
both pay the same lamports, and the v1 cost differs by a few units, so the order shows whether v1 gets the same key exactly.
S1 and S2 measure how much of the signature fee counts as reward: a second signature adds 5,000 lamports and 720 cost units,
so the two-signature probe beats A once a share above 0.38 counts, and the dearer one-signature probe once a share above 0.76
does. W2, W10 and W40 send the better probe second, inside the window, and how often it still lands first traces how long a
client's sort can wait.
"""
import hashlib
from dataclasses import dataclass

from .tx import Template, probe_data_bytes

KIB32 = 32 * 1024
MIB64 = 64 * 1024 * 1024
BARE_DATA_BYTES = probe_data_bytes(0)
V1_DATA_BYTES = 12  # the transfer alone: a v1 transaction has no compute-budget instructions
BUFFERED = 'buffered'
WINDOW = 'window'

A = Template(1_000, 1_000_000, KIB32)
Y = Template(1_500, 4_000_000, KIB32)
D = Template(1_000, 3_000_000, MIB64)
K_X = Template(1_000, 6_000_000, KIB32)
K_Y = Template(2_000, 4_250_000, KIB32)
V_HIGH = Template(1_000, 1_000_000, KIB32, 1)
V_LOW = Template(1_010, 990_099, KIB32, 1)
S_TWO = Template(1_000, 1_000, KIB32, signatures=2)  # one lamport of priority fee: a zero could meet a filter on shared ingress
S_ONE = Template(1_000, 2_000_000, KIB32)


@dataclass(frozen=True)
class Kind:
    name: str
    x: Template
    y: Template
    regime: str = BUFFERED
    gap_ms: float = 10.0
    x_second: bool = False  # x always goes later, so the pair reads overtaking

    @property
    def equal_key(self):
        return self.x == self.y

    def fee(self):
        return self.x.total_fee() + self.y.total_fee()


CURVE = ('W2', 'W10', 'W40')
KINDS = {k.name: k for k in (Kind('C4', Y, A), Kind('C6', D, A), Kind('E', A, A), Kind('K', K_X, K_Y), Kind('VH', V_HIGH, A),
                             Kind('VL', V_LOW, A), Kind('S1', S_TWO, A), Kind('S2', S_TWO, S_ONE), Kind('W2', Y, A, WINDOW, 2.0, True),
                             Kind('W10', Y, A, WINDOW, 10.0, True), Kind('W40', Y, A, WINDOW, 40.0, True))}

# Pairs per target in one session. The same on every target, so a difference between clients is not a difference of design.
QUOTA = dict(C4=8, C6=8, E=8, W2=4, W10=4, W40=4, K=2, VH=2, VL=2, S1=3, S2=3)
SESSIONS = (1, 2, 3)
SOURCE_READ = ('Agave', 'JitoLabs', 'Firedancer')  # their reward arithmetic was read at the running tag


def regime(kind):
    """The curve goes inside the window. Everything else waits in the buffer, on shared ingress too: inside the window a 10 ms
    gap hands the rule's loser a head start in half the pairs, and published pre-window sends to BAM and Harmonic landed in the
    target window."""
    return KINDS[kind].regime


def source(client):
    return 'read' if client in SOURCE_READ else 'unread'


def deck(seed, session, identity):
    """The kinds of one session for one target, in an order anyone can recompute from the seed."""
    cards = [(name, i) for name, count in sorted(QUOTA.items()) for i in range(count)]
    return [name for name, _ in sorted(cards, key=lambda c: hashlib.sha256(f'{seed}:{session}:{identity}:{c[0]}:{c[1]}'.encode()).digest())]


def remaining(cards, done):
    """What is left of a deck after a restart: earlier cards go first, whichever of them were already spent."""
    left = dict(done)
    out = []
    for card in cards:
        if left.get(card, 0):
            left[card] -= 1
        else:
            out.append(card)
    return out


def data_bytes(template):
    return V1_DATA_BYTES if template.version == 1 else BARE_DATA_BYTES


def templates():
    seen = []
    for kind in KINDS.values():
        for t in (kind.x, kind.y):
            if t not in seen:
                seen.append(t)
    return seen
