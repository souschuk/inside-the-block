#!/usr/bin/env python3
"""Which clients lead the slots of an epoch, and what share of them our targets hold. Reads saved RPC replies, needs no network.

An epoch's leader schedule is fixed, while the client labels in gossip are a snapshot, so both replies are saved under
data/coverage/ and the table comes from those files. `--fetch SLOT` first saves a fresh pair through a public RPC: the schedule
of the epoch holding SLOT and the gossip of that moment. Usage: coverage.py [--fetch SLOT]
"""
import collections
import datetime
import gzip
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from tiepairs.rpc import Rpc  # noqa: E402

OUT = os.path.join(HERE, 'data', 'coverage')
SLOTS_PER_EPOCH = 432_000  # mainnet has no warm-up epochs, so an epoch is slot // 432,000


def path(name):
    return os.path.join(OUT, f'{name}.json.gz')


def fetch(slot):
    rpc = Rpc()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
    os.makedirs(OUT, exist_ok=True)
    for name, method, params in (('leader-schedule', 'getLeaderSchedule', [slot]), ('cluster-nodes', 'getClusterNodes', [])):
        with gzip.open(path(name), 'wt') as f:
            json.dump(dict(slot=slot, epoch=slot // SLOTS_PER_EPOCH, fetched=stamp, result=rpc.call(method, params)), f)


def table():
    with gzip.open(path('leader-schedule'), 'rt') as f:
        schedule = json.load(f)
    with gzip.open(path('cluster-nodes'), 'rt') as f:
        gossip = json.load(f)
    with open(os.path.join(HERE, 'targets-P.json')) as f:
        ours = {t['identity'] for t in json.load(f)['targets']}
    label = {n['pubkey']: str(n.get('clientId') or 'unlabelled') for n in gossip['result']}
    slots, ours_by = collections.Counter(), collections.Counter()
    for identity, own in schedule['result'].items():
        client = label.get(identity, 'not in gossip')
        slots[client] += len(own)
        if identity in ours:
            ours_by[client] += len(own)
    total = sum(slots.values())
    lines = [f'Epoch {schedule["epoch"]}, {total:,} leader slots. Client labels from gossip at {gossip["fetched"]}.', '',
             '| Client in gossip | Leader slots | Share | Held by our targets | Of that client |', '|---|---:|---:|---:|---:|']
    for client, count in slots.most_common():
        mine = ours_by.get(client, 0)
        lines.append(f'| {client} | {count:,} | {100 * count / total:.1f}% | {mine:,} | {100 * mine / count:.0f}% |' if mine else
                     f'| {client} | {count:,} | {100 * count / total:.1f}% | – | – |')
    held = sum(ours_by.values())
    covered = sum(slots[c] for c in ours_by)
    lines += ['', f'Our targets lead {held:,} slots, {100 * held / total:.1f}% of the epoch. The clients they run lead '
                  f'{100 * covered / total:.1f}% of it.']
    return '\n'.join(lines) + '\n'


def main():
    if '--fetch' in sys.argv:
        fetch(int(sys.argv[sys.argv.index('--fetch') + 1]))
    print(table())


if __name__ == '__main__':
    main()
