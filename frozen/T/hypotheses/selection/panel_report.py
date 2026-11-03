# Report for panel_collect.py: head-of-block rank correlation per validator and epoch, and the shift between two periods.
# Passive data: it can show that a block profile moved, not why. Primary evidence in the submission stays our own probes.
# Usage: panel_report.py panel.jsonl out.json
import collections, json, math, random, statistics as st, sys

BEFORE = range(1065, 1069)  # 22-26.10, the dates of the published measurements
AFTER = range(1072, 1074)   # 30-31.10
GROUPS = {
    'jito:HEL1USMZ': 'upgraded', 'harmonic-agave:Fd7btgyS': 'upgraded', 'bam-agave:DRpbCBMx': 'upgraded',
    'harmonic-fd:JUPiTERr': 'upgraded', 'firedancer:9eGrDohd': 'upgraded', 'firebam:9jxgosAf': 'upgraded',
    'jito:E1r4Psq8': 'upgraded', 'jito:C8Bey3LK': 'upgraded', 'moved:EvnRmnMr': 'moved',
    'agave:7QQGNm3p': 'control', 'bam-agave:5Cchr1XG': 'control', 'rakurai:ana2y2Yv': 'control',
    'jito:5pPRHnie': 'control', 'jito:GnC339vk': 'control', 'harmonic-agave:CAo1dCGY': 'control',
}


def spearman(xs):
    """Rank correlation of position and priority. Plus means higher priority sits earlier."""
    n = len(xs)
    if n < 5:
        return None
    order = sorted(range(n), key=lambda i: xs[i]); r = [0.0] * n; i = 0
    while i < n:
        j = i
        while j + 1 < n and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    mx = (n - 1) / 2; mr = sum(r) / n
    sxy = sum((i - mx) * (r[i] - mr) for i in range(n)); sxx = sum((i - mx) ** 2 for i in range(n)); syy = sum((v - mr) ** 2 for v in r)
    return -sxy / math.sqrt(sxx * syy) if syy > 0 else None


def boot(a, b, rng, rounds=4000):
    """Interval for mean(b) - mean(a), resampling blocks."""
    if len(a) < 4 or len(b) < 4:
        return None
    d = sorted(st.mean(rng.choices(b, k=len(b))) - st.mean(rng.choices(a, k=len(a))) for _ in range(rounds))
    return d[int(rounds * .025)], d[int(rounds * .975)]


def main():
    rows = []
    for line in open(sys.argv[1]):
        d = json.loads(line)
        if d.get('skipped') or not d.get('lg') or d['n'] < 60:
            continue
        lg = [x for x in d['lg'] if x is not None]
        n = len(lg); k = max(n // 5, 12)
        rows.append(dict(label=d['label'], epoch=d['epoch'], off=d['off'], head=spearman(lg[:k]), whole=spearman(lg), n=n,
                         leader=d.get('leader'), pubkey=d['pubkey']))
    wrong = sum(1 for r in rows if r['leader'] and r['leader'] != r['pubkey'])
    print(f'blocks used {len(rows)}, leader mismatch {wrong}')
    rng = random.Random(7)
    by = collections.defaultdict(list)
    for r in rows:
        if r['head'] is not None:
            by[(r['label'], r['epoch'], r['off'])].append(r['head'])
    epochs = sorted({r['epoch'] for r in rows})
    labels = sorted(GROUPS, key=lambda x: (GROUPS[x], x))
    series = {}
    for off in (0, 2):
        print(f'\nhead rho by epoch, slot {off} of the window (mean over blocks, blank under 3 blocks)')
        print(f"{'validator':26}{'group':10}" + ''.join(f'{e:>7}' for e in epochs))
        for lab in labels:
            cells = []
            for e in epochs:
                v = by.get((lab, e, off), [])
                cells.append(st.mean(v) if len(v) >= 3 else None)
            series[f'{lab}|{off}'] = cells
            print(f'{lab:26}{GROUPS[lab]:10}' + ''.join(f'{c:7.2f}' if c is not None else '       ' for c in cells))
    print('\nshift after minus before, slot 0 and slot 2 together')
    print(f"{'validator':26}{'group':10}{'before':>8}{'after':>8}{'shift':>8}   95% interval   n")
    shifts = {}
    for lab in labels:
        a = [r['head'] for r in rows if r['label'] == lab and r['epoch'] in BEFORE and r['head'] is not None]
        b = [r['head'] for r in rows if r['label'] == lab and r['epoch'] in AFTER and r['head'] is not None]
        ci = boot(a, b, rng)
        if not ci:
            continue
        shifts[lab] = dict(before=st.mean(a), after=st.mean(b), shift=st.mean(b) - st.mean(a), ci=ci, n=(len(a), len(b)))
        mark = '*' if ci[0] > 0 or ci[1] < 0 else ' '
        print(f'{lab:26}{GROUPS[lab]:10}{st.mean(a):8.2f}{st.mean(b):8.2f}{st.mean(b) - st.mean(a):8.2f}{mark} [{ci[0]:5.2f},{ci[1]:5.2f}]  {len(a)}/{len(b)}')
    for g in ('upgraded', 'moved', 'control'):
        v = [s['shift'] for lab, s in shifts.items() if GROUPS[lab] == g]
        if v:
            print(f'{g:10} mean shift {st.mean(v):.3f} over {len(v)} validators')
    json.dump(dict(epochs=epochs, series=series, shifts=shifts, groups=GROUPS), open(sys.argv[2], 'w'))


if __name__ == '__main__':
    main()
