#!/usr/bin/env python3
"""The reordering curve of block P: how long a validator's sort can wait. Reads outcomes.jsonl, needs no network.

For a batch of period B with a random phase, two probes g ms apart share a batch with probability max(0, 1 - g/B). In one
batch the better probe wins, otherwise the earlier one does. B is fitted per validator by maximum likelihood on the achieved
gaps, with a noise floor so that one odd pair does not decide the fit, and the 95% profile range beside it. It is the
exploratory fit named in hypotheses/H6.md, so it lives outside the frozen package. Usage: horizon.py RUN_DIR [RUN_DIR ...]
"""
import collections
import json
import math
import os
import sys

CURVE = ('W2', 'W10', 'W40')
SCORED = ('same_slot', 'different_slots')
FLOOR = .01
GRID = [x / 2 for x in range(2, 801)]  # 1 to 400 ms
PUBLISHED = {'7QQGNm3p': 2.5, 'HEL1USMZ': 2.5, 'JUPiTERr': 15.5, 'Fd7btgyS': 28.0, 'DRpbCBMx': 50.0}


def overtakes(gap, batch):
    return max(0.0, 1 - gap / batch)


def loglik(rows, batch):
    total = 0.0
    for gap, won in rows:
        p = FLOOR + (1 - 2 * FLOOR) * overtakes(gap, batch)
        total += math.log(p if won else 1 - p)
    return total


def fit(rows):
    """Best period and its profile range. A range that reaches the end of the grid means only a lower bound."""
    scores = {b: loglik(rows, b) for b in GRID}
    best = max(GRID, key=scores.get)
    keep = [b for b in GRID if scores[b] >= scores[best] - 1.92]
    return best, min(keep), max(keep)


def read(run_dirs):
    by = collections.defaultdict(list)
    for run_dir in run_dirs:
        with open(os.path.join(run_dir, 'outcomes.jsonl')) as f:
            for line in f:
                o = json.loads(line)
                if o.get('contrast') in CURVE and o['kind'] in SCORED and o.get('x_first') is not None and o.get('gap_ms') is not None:
                    by[o['identity'][:8], o.get('client'), o.get('version')].append(o)
    return by


def render(by):
    lines = ['| Validator | Client | W2 | W10 | W40 | B, ms | 95% range | Published B |', '|---|---|---:|---:|---:|---:|---|---:|']
    for (name, client, version), rows in sorted(by.items(), key=str):
        cells = []
        for kind in CURVE:
            own = [o for o in rows if o['contrast'] == kind]
            cells.append(f'{sum(o["x_first"] for o in own)}/{len(own)}')
        best, low, high = fit([(o['gap_ms'], o['x_first']) for o in rows])
        upper = 'more' if high >= GRID[-1] else f'{high:g}'
        lines.append(f'| `{name}` | {client} {version} | ' + ' | '.join(cells) + f' | {best:g} | {low:g} to {upper} | {PUBLISHED.get(name, "–")} |')
    return '\n'.join(lines) + '\n'


def main():
    print(render(read(sys.argv[1:])))


if __name__ == '__main__':
    main()
