"""Block P: which key orders each validator's probes, how strictly, and what breaks a tie. Decision rule: hypotheses/H6.md.

A pair counts when both probes landed without error in the target's window, and "lands before" is (slot, index), as in the
published studies. The same-slot view is a sensitivity. Every verdict speaks for the ingress path we measured.
"""
import collections
import json
import os

from . import contrasts, results
from .stats import binom_two_sided, clopper_pearson, holm

ALPHA = 0.05
MIN_INFORMATIVE = 10
STRICT_SHARE = 0.80
BALANCE_ALPHA = 0.01
MAX_GAP_ERROR_MS = 1.0
NO_RULE_BAND = (0.20, 0.80)
FAMILY = 10
SCORED = ('same_slot', 'different_slots')
KEYS = {('x', 'y'): 'reward_per_cost', ('x', 'x'): 'price_fee_or_no_loaded_cost', ('y', 'y'): 'cost_alone'}
PLANNED = sum(contrasts.QUOTA.values()) * len(contrasts.SESSIONS)
SHARES = {('x', 'x'): 'above_0.76', ('x', 'y'): 'half', ('y', 'y'): 'below_0.38'}  # the share of the signature fee that counts as reward


def scored(o, contrast):
    return o.get('contrast') == contrast and o['kind'] in SCORED and o.get('x_first') is not None


def cell(rows, won):
    k = sum(1 for o in rows if won(o))
    n = len(rows)
    low, high = clopper_pearson(k, n)
    return dict(k=k, n=n, share=k / n if n else None, low=low, high=high, p=binom_two_sided(k, n))


def side(c, adjusted):
    if not c['n'] or adjusted >= ALPHA:
        return '-'
    return 'x' if c['share'] > 0.5 else 'y'


def target_row(rows):
    first = rows[0]
    by = {c: [o for o in rows if scored(o, c)] for c in contrasts.KINDS}
    priced = by['C4'] + by['C6']
    gaps = sorted(o['gap_ms'] for o in rows if o.get('gap_ms') is not None and o['regime'] == contrasts.BUFFERED)
    views = dict(same_slot=lambda o: o['kind'] == 'same_slot', lead_150ms=results.lead_ok, gap_10ms=results.gap_ok, settled_link=results.link_ok)
    return dict(group=first['group'], client=first.get('client'), version=first.get('version'), regime={o['contrast']: o['regime'] for o in rows},
                funnel=dict(collections.Counter(o['kind'] for o in rows)),
                not_sent=dict(collections.Counter(o.get('cancel') or 'unknown' for o in rows if o['kind'] == 'not_sent')),
                moved=sum(1 for o in rows if o.get('x_version_moved') or o.get('cancel') == 'version_changed'),
                fee_differs=sum(1 for o in rows if o.get('x_fee_differs')),
                cells={c: cell(by[c], lambda o: o['x_first']) for c in contrasts.KINDS if c != 'E'},
                ties=cell(by['E'], lambda o: o['first_sent_first']),
                send_order=cell(priced, lambda o: o['first_sent_first']),
                balance=cell(priced, lambda o: o['x_sent_first']),
                gap_median=gaps[len(gaps) // 2] if gaps else None,
                curve_gaps={c: median_gap(by[c]) for c in contrasts.CURVE},
                views={name: {c: cell([o for o in by[c] if keep(o)], lambda o: o['x_first']) for c in ('C4', 'C6')} for name, keep in views.items()},
                lost=dict(C4=sum(1 for o in rows if o.get('contrast') == 'C4' and o['kind'] == 'one_landed'),
                          C6=sum(1 for o in rows if o.get('contrast') == 'C6' and o['kind'] == 'one_landed')),
                sessions={run: {c: cell([o for o in by[c] if o['run'] == run], lambda o: o['x_first'] if c != 'E' else o['first_sent_first'])
                                for c in contrasts.KINDS} for run in sorted({o['run'] for o in rows})})


def median_gap(rows):
    gaps = sorted(o['gap_ms'] for o in rows if o.get('gap_ms') is not None)
    return gaps[len(gaps) // 2] if gaps else None


def worst_case(c, lost, predicted_x):
    """Every pair that lost one member counted against the prediction."""
    k = c['k'] if predicted_x else c['n'] - c['k']
    n = c['n'] + lost
    return dict(k=k, n=n, share=k / n if n else None)


def adjust(pvalues, family):
    """Holm over the planned family: a target with no pairs at all still counts, as a test that found nothing."""
    return holm(list(pvalues) + [1.0] * (family - len(pvalues)))[:len(pvalues)]


def inside_band(c):
    return NO_RULE_BAND[0] < c['low'] and c['high'] < NO_RULE_BAND[1]


def name_rule(c4, c6, adjusted_c4, adjusted_c6, send_order, adjusted_order):
    if c4['n'] < MIN_INFORMATIVE or c6['n'] < MIN_INFORMATIVE:
        return 'not_evaluable'
    sides = side(c4, adjusted_c4), side(c6, adjusted_c6)
    if sides != ('-', '-'):
        return KEYS.get(sides, 'partial' if '-' in sides else 'mixed')
    order = side(send_order, adjusted_order)
    if order != '-':
        return 'arrival' if order == 'x' else 'reverse_arrival'
    return 'no_rule_seen' if inside_band(c4) and inside_band(c6) else 'undetermined'


def verdicts(report):
    names = sorted(report['targets'])
    tests = [(name, c) for name in names for c in ('C4', 'C6')]
    adjusted = dict(zip(tests, adjust([report['targets'][name]['cells'][c]['p'] for name, c in tests], 2 * FAMILY)))
    ties = dict(zip(names, adjust([report['targets'][name]['ties']['p'] for name in names], FAMILY)))
    orders = dict(zip(names, adjust([report['targets'][name]['send_order']['p'] for name in names], FAMILY)))
    for name in names:
        row = report['targets'][name]
        c4, c6, t = row['cells']['C4'], row['cells']['C6'], row['ties']
        row['holm'] = dict(C4=adjusted[name, 'C4'], C6=adjusted[name, 'C6'], ties=ties[name], send_order=orders[name])
        row['worst_case'] = dict(C4=worst_case(c4, row['lost']['C4'], True), C6=worst_case(c6, row['lost']['C6'], False))
        row['unbalanced'] = row['balance']['p'] < BALANCE_ALPHA
        if row['gap_median'] is not None and abs(row['gap_median'] - 10.0) > MAX_GAP_ERROR_MS:
            row.update(rule='withdrawn', strict=False, tie_break='withdrawn')
            continue
        row['rule'] = name_rule(c4, c6, adjusted[name, 'C4'], adjusted[name, 'C6'], row['send_order'], orders[name])
        row['strict'] = row['rule'] == 'reward_per_cost' and c4['share'] >= STRICT_SHARE and 1 - c6['share'] >= STRICT_SHARE
        if t['n'] < MIN_INFORMATIVE:
            row['tie_break'] = 'not_evaluable'
        else:
            row['tie_break'] = {'x': 'arrival', 'y': 'reverse_arrival', '-': 'no_relation_seen'}[side(t, ties[name])]


def named_validators(report):
    return {name for name, row in report['targets'].items() if row['rule'] == 'reward_per_cost'}


def signature_share(outcomes, report):
    """S, pooled over the validators C4 and C6 named reward per cost: under another key the same pattern means something else."""
    named = named_validators(report)
    tests = [(group, c) for group in ('read', 'unread') for c in ('S1', 'S2')]
    cells = {(group, c): cell([o for o in outcomes if o['identity'] in named and contrasts.source(o.get('client')) == group and scored(o, c)], lambda o: o['x_first'])
             for group, c in tests}
    adjusted = dict(zip(tests, adjust([cells[t]['p'] for t in tests], len(tests))))
    out = {}
    for group in ('read', 'unread'):
        s1, s2 = cells[group, 'S1'], cells[group, 'S2']
        if s1['n'] < MIN_INFORMATIVE or s2['n'] < MIN_INFORMATIVE:
            share_ = 'not_evaluable'
        else:
            share_ = SHARES.get((side(s1, adjusted[group, 'S1']), side(s2, adjusted[group, 'S2'])), 'undetermined')
        out[group] = dict(S1=s1, S2=s2, holm=dict(S1=adjusted[group, 'S1'], S2=adjusted[group, 'S2']), share=share_,
                          validators=sorted({o['identity'] for o in outcomes if o['identity'] in named and contrasts.source(o.get('client')) == group}))
    return out


def analyze(outcomes):
    targets = {name: target_row([o for o in outcomes if o['identity'] == name]) for name in sorted({o['identity'] for o in outcomes})}
    report = dict(targets=targets, pooled={})
    verdicts(report)
    report['signature_share'] = signature_share(outcomes, report)
    report['k'] = cell([o for o in outcomes if o['identity'] in named_validators(report) and scored(o, 'K')], lambda o: o['x_first'])
    for c in contrasts.KINDS:
        if c != 'E':
            report['pooled'][c] = cell([o for o in outcomes if scored(o, c)], lambda o: o['x_first'])
    for family in ('agave', 'firedancer'):
        rows = [o for o in outcomes if code_base(o.get('client')) == family]
        report['pooled'][f'VH {family}'] = cell([o for o in rows if scored(o, 'VH')], lambda o: o['x_first'])
        report['pooled'][f'VL {family}'] = cell([o for o in rows if scored(o, 'VL')], lambda o: o['x_first'])
    report['clients'] = len({(row['client'], row['version']) for row in targets.values()})
    return report


def code_base(client):
    return 'firedancer' if 'Fire' in (client or '') else 'agave'


def share(c):
    return '–' if c['share'] is None else f'{c["k"]}/{c["n"]} ({c["low"]:.2f}–{c["high"]:.2f})'


def render(report):
    lines = ['# Block P result', '', f'Validators {len(report["targets"])}, client and version pairs {report["clients"]}.', '',
             '| Validator | Client | Ingress | Rule | Strict | C4: Y first | C6: D first | Ties: first-sent first | Tie-break | K: K-x first |',
             '|---|---|---|---|---|---:|---:|---:|---|---:|']
    for name, r in report['targets'].items():
        cells = r['cells']
        lines.append(f'| `{name[:8]}` | {r["client"]} {r["version"]} | {r.get("ingress") or "–"} | {r["rule"]} | {"yes" if r["strict"] else "no"} | {share(cells["C4"])} | {share(cells["C6"])} '
                     f'| {share(r["ties"])} | {r["tie_break"]} | {share(cells["K"])} |')
    lines += ['', '## Overtaking: Y sent after A, inside the window', '', '| Validator | 2 ms | 10 ms | 40 ms | Achieved gaps, ms |', '|---|---:|---:|---:|---|']
    for name, r in report['targets'].items():
        gaps = ' / '.join(str(r['curve_gaps'][c]) for c in contrasts.CURVE)
        lines.append(f'| `{name[:8]}` | ' + ' | '.join(share(r['cells'][c]) for c in contrasts.CURVE) + f' | {gaps} |')
    lines += ['', '## Signature fee', '', '| Validator | Source | S1: two signatures first | S2: two signatures first |', '|---|---|---:|---:|']
    for name, r in report['targets'].items():
        lines.append(f'| `{name[:8]}` | {contrasts.source(r["client"])} | {share(r["cells"]["S1"])} | {share(r["cells"]["S2"])} |')
    lines += ['', '| Group, validators named reward per cost | Validators | S1 | S2 | Share of the signature fee counted |', '|---|---:|---:|---:|---|']
    for group, g in report['signature_share'].items():
        lines.append(f'| {group} | {len(g["validators"])} | {share(g["S1"])} | {share(g["S2"])} | {g["share"]} |')
    lines += ['', '## Landing', '', '| Validator | Pairs of the plan | Pair classes | Moved | Fee differs | Worst case C4 | Worst case C6 | First-sent won C4 and C6 | x sent first | Median gap, buffered pairs, ms |', '|---|---:|---|---:|---:|---:|---:|---:|---:|---:|']
    for name, r in report['targets'].items():
        kinds = ', '.join(f'{k} {v}' for k, v in sorted(r['funnel'].items()))
        if r['not_sent']:
            kinds += ' (not sent: ' + ', '.join(f'{k} {v}' for k, v in sorted(r['not_sent'].items())) + ')'
        w = r['worst_case']
        flag = ' (unbalanced)' if r['unbalanced'] else ''
        lines.append(f'| `{name[:8]}` | {sum(r["funnel"].values())}/{PLANNED} | {kinds} | {r["moved"]} | {r["fee_differs"]} | {w["C4"]["k"]}/{w["C4"]["n"]} | {w["C6"]["k"]}/{w["C6"]["n"]} '
                     f'| {share(r["send_order"])} | {share(r["balance"])}{flag} | {r["gap_median"]} |')
    left_out = [(run, t) for run, rows in (report.get('left_out') or {}).items() for t in rows]
    if left_out:
        lines += ['', '## Targets left out of a run', '', '| Run | Validator | Shows | Reason |', '|---|---|---|---|']
        lines += [f'| {run} | `{t["identity"][:8]}` | {t["client"]} {t["version"]} | {t["reason"]} |' for run, t in left_out]
    lines += ['', '## By session', '', '| Validator | Run | C4 | C6 | Ties |', '|---|---|---:|---:|---:|']
    for name, r in report['targets'].items():
        for run, cells in r['sessions'].items():
            lines.append(f'| `{name[:8]}` | {run} | {share(cells["C4"])} | {share(cells["C6"])} | {share(cells["E"])} |')
    lines += ['', '## Sensitivity: C4 and C6', '', '| Validator | One slot only | Lead at least 150 ms | Gap 10 ± 1 ms | Connection at least 5 s old |', '|---|---:|---:|---:|---:|']
    for name, r in report['targets'].items():
        cells = [f'{share(r["views"][v]["C4"])}; {share(r["views"][v]["C6"])}' for v in ('same_slot', 'lead_150ms', 'gap_10ms', 'settled_link')]
        lines.append(f'| `{name[:8]}` | ' + ' | '.join(cells) + ' |')
    lines += ['', '## All validators', '', f'- K over the validators named reward per cost: {share(report["k"])}, p = {report["k"]["p"]:.2g}']
    for c, cell_ in report['pooled'].items():
        lines.append(f'- {c}: {share(cell_)}')
    if report.get('network'):
        lines += ['', '## Could the network have swapped a pair', '', '| Address | n | RTT min / p50 / p99, ms | Close ping pairs | Slower by at least 10 ms | Upper 95% bound | Slower by at least 2 ms | Upper 95% bound |', '|---|---:|---|---:|---:|---:|---:|---:|']
        for run, rows in report['network'].items():
            for addr, r in rows.items():
                bound, short = ('–' if b is None else f'{b:.4f}' for b in (r['swap_bound'], r['short_swap_bound']))
                lines.append(f'| {run[:15]} {addr} | {r["n"]} | {r["min"]:.1f} / {r["p50"]:.1f} / {r["p99"]:.1f} | {r["close_pairs"]} | {r["slower_by_gap"]} | {bound} | {r["slower_by_short_gap"]} | {short} |')
    return '\n'.join(lines) + '\n'


def write_report(run_dirs, out_dir):
    outcomes = []
    network = {}
    left_out = {}
    ingress = {}
    for run_dir in run_dirs:
        run = os.path.basename(run_dir.rstrip('/'))
        with open(os.path.join(run_dir, 'outcomes.jsonl')) as f:
            outcomes += [json.loads(line) for line in f]
        network[run] = results.rtt_report(run_dir)
        meta_path = os.path.join(run_dir, 'meta.json')
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                listed = json.load(f)['targets']
            left_out[run] = [t for t in listed if not t['ok']]
            ingress.update({t['identity']: t.get('ingress') for t in listed})
    report = analyze(outcomes)
    report.update(network=network, left_out=left_out)
    for name, row in report['targets'].items():
        row['ingress'] = ingress.get(name)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'summary.json'), 'w') as f:
        json.dump(report, f, indent=1)
    text = render(report)
    with open(os.path.join(out_dir, 'summary.md'), 'w') as f:
        f.write(text)
    for run_dir in run_dirs:
        results.manifest(run_dir)
    return report, text
