"""Recomputes the group-selection figures quoted in H5.md from the archived block reads. Read-only, no network.

Head-of-block rank correlation per validator: first slot and third slot of a leader window, epochs 1067-1073, and for the
relayer arm the first slot in epochs 1072-1073.
"""
import collections
import gzip
import json
import os
import statistics

from panel_report import spearman

HERE = os.path.dirname(os.path.abspath(__file__))
TREATED = ('HEL1USMZ', 'E1r4Psq8', 'bitHUmbK')
CONTROL = ('3SkE34PV', '8gDfqHXZ', 'privaEdS')
RELAYER = ('EvnRmnMr', '5pPRHnie', 'GnC339vk')


def blocks(name):
    with gzip.open(os.path.join(HERE, name), 'rt') as f:
        for line in f:
            d = json.loads(line)
            if not d.get('skipped') and d.get('lg') and d['n'] >= 60:
                yield d


def head(d):
    lg = [x for x in d['lg'] if x is not None]
    return spearman(lg[:max(len(lg) // 5, 12)])


def main():
    wide = collections.defaultdict(list)
    for name in ('panel.jsonl.gz', 'ctrl.jsonl.gz'):
        for d in blocks(name):
            key = d['pubkey'][:8]
            if key in TREATED + CONTROL and 1067 <= d['epoch'] <= 1073 and head(d) is not None:
                wide[(key, d['off'])].append(head(d))
    print(f"{'validator':10}{'first slot':>12}{'n':>5}{'third slot':>12}{'n':>5}")
    for key in TREATED + CONTROL:
        a, b = wide[(key, 0)], wide[(key, 2)]
        print(f'{key:10}{statistics.mean(a):12.2f}{len(a):5}{statistics.mean(b):12.2f}{len(b):5}')
    recent = collections.defaultdict(list)
    for d in blocks('scan.jsonl.gz'):
        key = d['pubkey'][:8]
        if key in RELAYER and d['off'] == 0 and head(d) is not None:
            recent[key].append(head(d))
    print('relayer arm, first slot, epochs 1072-1073:', {k: round(statistics.mean(v), 2) for k, v in recent.items()})


if __name__ == '__main__':
    main()
