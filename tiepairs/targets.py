"""Targets come from a file; each plan re-checks them against a fresh getClusterNodes."""
import collections
import json
from dataclasses import dataclass

RELAYER_PORT = 11228  # default of jito-relayer --tpu-quic-port
RELAYER_GROUP = 'relayer'


@dataclass(frozen=True)
class Target:
    identity: str
    group: str
    client: str
    version: str
    ingress: str = 'direct'

    @property
    def short(self):
        return self.identity[:8]


@dataclass
class Checked:
    target: Target
    ok: bool
    reason: str
    client: str
    version: str
    tpu_quic: str | None
    sharing: int = 0


def load(path):
    with open(path) as f:
        return [Target(t['identity'], t['group'], t['client'], t['version'], t.get('ingress', 'direct')) for t in json.load(f)['targets']]


def verify(targets, nodes):
    """A target runs only if it still shows the expected client and version and has its own ingress address."""
    by_key = {n['pubkey']: n for n in nodes}
    sharing = collections.Counter(n.get('tpuQuic') for n in nodes if n.get('tpuQuic'))
    out = []
    for t in targets:
        n = by_key.get(t.identity)
        if not n:
            out.append(Checked(t, False, 'not in gossip', '', '', None))
            continue
        client, version, addr = n.get('clientId') or '', n.get('version') or '', n.get('tpuQuic')
        if client != t.client or version != t.version:
            reason = f'shows {client} {version}, expected {t.client} {t.version}'
        elif not addr:
            reason = 'no tpuQuic in gossip'
        elif sharing[addr] > 1 and t.ingress != 'shared':
            reason = f'tpuQuic shared by {sharing[addr]} nodes: not direct ingress'
        elif t.group != RELAYER_GROUP and addr.endswith(f':{RELAYER_PORT}'):
            reason = f'tpuQuic on port {RELAYER_PORT}, the Jito Relayer default: not direct ingress'
        else:
            reason = ''
        out.append(Checked(t, not reason, reason, client, version, addr, sharing[addr] if addr else 0))
    return out
