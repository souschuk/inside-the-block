"""From signatures to verdict: where each probe landed, which pairs count, and what every excluded pair was excluded for.

A pair is informative when both probes landed without error in the same slot of the target's window. Everything else
stays in the outcome file under its own class and flags. "Different slots" pairs are also scored window-wide, which is
how the published 4.3.2 baseline was counted.
"""
import base64
import collections
import hashlib
import json
import os

from . import contrasts, journal, schedule, tx
from .rpc import RpcError
from .slots import read_stamps
from .stats import clopper_pearson, fisher_exact

MIN_LEAD_MS = 150.0
MAX_GAP_ERROR_MS = 1.0
MIN_LINK_AGE_S = 5.0
SHORT_GAP_MS = 2.0
LANDABLE = ('sent', 'partial', 'send_error', 'crash_unknown')

# Decision rule from hypotheses/H5.md
MIN_INFORMATIVE = 20
TREATED_SHARE = 0.80
FISHER_ALPHA = 0.01
MIN_VALIDATOR_PAIRS = 10
MIN_DIFFERENCE = 0.20


def load_pairs(run_dir):
    return [pair for _, pair in sorted(journal.merged_pairs(os.path.join(run_dir, 'journal.jsonl')).items())]


def slot_leader(rpc, slot, required):
    """Public nodes keep the schedules of about eight epochs, so a later check may have to do without it."""
    try:
        return rpc.call('getSlotLeaders', [slot, 1])[0]
    except RpcError:
        if required:
            raise
        return None


def block_view(rpc, slot, cache, need_leader=True):
    """The leader comes from the schedule. The Fee reward can go elsewhere: blocks of GnC339vk pay it to 2FL6757b."""
    if slot in cache:
        return cache[slot]
    block = rpc.call('getBlock', [slot, dict(encoding='json', transactionDetails='signatures', rewards=True,
                                             maxSupportedTransactionVersion=1, commitment='confirmed')])
    signatures = block.get('signatures') or []
    leader = slot_leader(rpc, slot, need_leader)
    recipient = next((r['pubkey'] for r in block.get('rewards') or [] if r.get('rewardType') == 'Fee'), None)
    view = dict(positions={s: i for i, s in enumerate(signatures)}, size=len(signatures), leader=leader, fee_recipient=recipient,
                blockhash=block.get('blockhash'), block_time=block.get('blockTime'))
    cache[slot] = view
    return view


def tx_view(rpc, signature):
    reply = rpc.call('getTransaction', [signature, dict(encoding='json', maxSupportedTransactionVersion=1,
                                                        commitment='confirmed')])
    if not reply:
        return {}
    meta = reply.get('meta') or {}
    return dict(fee=meta.get('fee'), units=meta.get('computeUnitsConsumed'), err=meta.get('err'))


def landings(rpc, signatures):
    out = {}
    for i in range(0, len(signatures), 200):
        batch = signatures[i:i + 200]
        reply = rpc.call('getSignatureStatuses', [batch, dict(searchTransactionHistory=True)])['value']
        out.update(zip(batch, reply))
    return out


def classify(pair, first, second):
    """`first` and `second` are landing dicts (or None), in send order."""
    status = pair.get('status')
    if status in ('partial', 'send_error', 'crash_unknown'):
        return status
    if status != 'sent':
        return 'not_sent'
    if not first and not second:
        return 'none_landed'
    if not first or not second:
        return 'one_landed'
    if first.get('err') or second.get('err'):
        return 'failed'
    window = range(pair['first_slot'], pair['first_slot'] + 4)
    for member in (first, second):
        if member.get('leader') != pair['identity'] or (member['slot'] not in window and not pair.get('no_window')):
            return 'outside_window'
    return 'same_slot' if first['slot'] == second['slot'] else 'different_slots'


def order_of(first, second):
    return (first['slot'], first['position']) < (second['slot'], second['position'])


def lead_ms(pair, stamps):
    """How long before the first shred of F, as an RPC node stamped it, the later probe should have reached the leader."""
    stamp = stamps.get(pair['first_slot'])
    second = 'a' if pair.get('first_sent') == 'b' else 'b'
    sent = pair.get(f'sent_{second}') or {}
    if stamp is None or not sent.get('wall_ns'):
        return None
    return round(stamp - sent['wall_ns'] / 1e6 - (pair.get('rtt_before_ms') or 0) / 2, 1)


def flags(outcome):
    """Every reason a pair can be left out, each as its own field."""
    kind = outcome['kind']
    fields = {f'x_{k}': kind == k for k in ('not_sent', 'none_landed', 'one_landed', 'failed', 'outside_window',
                                            'different_slots', 'partial', 'send_error', 'crash_unknown')}
    lead = outcome.get('lead_ms')
    gap = outcome.get('gap_ms')
    planned, seen = outcome.get('gossip_plan') or {}, outcome.get('gossip_window') or {}
    nominal = contrasts.KINDS[outcome['contrast']].gap_ms if outcome.get('contrast') else 10.0
    fields.update(x_cancel=outcome.get('cancel'),
                  x_lead_under_150ms=lead is not None and lead < MIN_LEAD_MS,
                  x_lead_unknown=kind in ('same_slot', 'different_slots') and lead is None,
                  x_gap_off=gap is not None and abs(gap - nominal) > MAX_GAP_ERROR_MS,
                  x_version_moved=bool(seen) and (seen.get('version') != planned.get('version') or seen.get('client') != planned.get('client')),
                  x_fee_differs=outcome.get('fee_equal') is False)
    return fields


def collect(rpc, run_dir):
    pairs = load_pairs(run_dir)
    stamps, offset = read_stamps(os.path.join(run_dir, 'stamps.jsonl'))
    signatures = [s for p in pairs if p.get('status') in LANDABLE for s in (p.get('sig_a'), p.get('sig_b')) if s]
    statuses = landings(rpc, signatures) if signatures else {}
    cache = {}
    outcomes = []
    final_probes = []
    for pair in pairs:
        members = {}
        for role in ('a', 'b'):
            status = statuses.get(pair.get(f'sig_{role}'))
            if not status:
                members[role] = None
                continue
            view = block_view(rpc, status['slot'], cache)
            detail = tx_view(rpc, pair[f'sig_{role}'])
            members[role] = dict(slot=status['slot'], err=status.get('err'), position=view['positions'].get(pair[f'sig_{role}']),
                                 block_size=view['size'], leader=view['leader'], fee_recipient=view['fee_recipient'],
                                 fee=detail.get('fee'), units=detail.get('units'))
        first_role = pair['first_sent']
        first, second = members[first_role], members['a' if first_role == 'b' else 'b']
        kind = classify(pair, first, second)
        outcome = {k: pair.get(k) for k in ('run', 'seq', 'identity', 'group', 'first_slot', 'first_sent', 'status', 'cancel',
                                            'reason', 'gap_ms', 'rtt_before_ms', 'rtt_after_ms', 'sig_a', 'sig_b', 'addr',
                                            'conn_id', 'link_age_s', 'new_links', 'gossip_plan', 'gossip_window')}
        outcome.update(kind=kind, first=first, second=second, lead_ms=lead_ms(pair, stamps), client=(pair.get('gossip_plan') or {}).get('client'),
                       version=(pair.get('gossip_plan') or {}).get('version'), clock_offset_bound_ms=offset,
                       contrast=pair.get('kind') or '', x_role=pair.get('x_role') or '', regime=pair.get('regime') or contrasts.BUFFERED)
        if first and second and first.get('position') is not None and second.get('position') is not None:
            outcome['first_sent_first'] = order_of(first, second)
            planned = contrasts.KINDS.get(pair.get('kind'))
            if planned:
                x, y = members[pair['x_role']], members['a' if pair['x_role'] == 'b' else 'b']
                outcome['x_first'] = order_of(x, y)
                outcome['x_sent_first'] = pair['x_role'] == first_role
                outcome['fee_equal'] = (x.get('fee'), y.get('fee')) == (planned.x.total_fee(), planned.y.total_fee())
            else:
                outcome['fee_equal'] = first.get('fee') == second.get('fee')
        outcome.update(flags(outcome))
        outcomes.append(outcome)
        for role in ('a', 'b'):
            final_probes.append(dict(signature=pair.get(f'sig_{role}'), seq=pair.get('seq'), role=role, landing=members[role],
                                     pair_kind=kind, **{k: v for k, v in outcome.items() if k.startswith('x_')}))
    with open(os.path.join(run_dir, 'outcomes.jsonl'), 'w') as f:
        for outcome in outcomes:
            f.write(json.dumps(outcome) + '\n')
    with open(os.path.join(run_dir, 'probes.final.jsonl'), 'w') as f:
        for probe in final_probes:
            f.write(json.dumps(probe) + '\n')
    return outcomes


def lead_ok(o):
    return o.get('lead_ms') is not None and o['lead_ms'] >= MIN_LEAD_MS


def gap_ok(o):
    return o.get('gap_ms') is not None and abs(o['gap_ms'] - 10.0) <= MAX_GAP_ERROR_MS


def link_ok(o):
    return o.get('link_age_s') is not None and o['link_age_s'] >= MIN_LINK_AGE_S


def tally(outcomes, keep):
    rows = [o for o in outcomes if keep(o)]
    k = sum(1 for o in rows if o.get('first_sent_first'))
    n = len(rows)
    low, high = clopper_pearson(k, n)
    return dict(k=k, n=n, share=k / n if n else None, low=low, high=high)


def primary(o):
    return o['kind'] == 'same_slot'


VIEWS = dict(
    primary=primary,
    window_wide=lambda o: o['kind'] in ('same_slot', 'different_slots'),
    lead_150ms=lambda o: primary(o) and lead_ok(o),
    gap_10ms=lambda o: primary(o) and gap_ok(o),
    settled_link=lambda o: primary(o) and link_ok(o),
)


def contrast(treated, control):
    a, b = treated['k'], treated['n'] - treated['k']
    c, d = control['k'], control['n'] - control['k']
    two_sided, greater = fisher_exact(a, b, c, d)
    return dict(table=[[a, b], [c, d]], fisher_two_sided=two_sided, fisher_greater=greater)


def worst_case(outcomes, group, against):
    """Count every pair with exactly one landed probe against the hypothesis."""
    rows = [o for o in outcomes if o['group'] == group and o['kind'] in ('same_slot', 'one_landed')]
    k = sum(1 for o in rows if o.get('first_sent_first') or (o['kind'] == 'one_landed' and not against))
    return dict(k=k, n=len(rows))


def analyze(outcomes):
    groups = sorted({o['group'] for o in outcomes})
    report = dict(views={}, by_target={}, exclusions={}, gap={}, lead={}, young_link={})
    for name, keep in VIEWS.items():
        cells = {g: tally(outcomes, lambda o, g=g: o['group'] == g and keep(o)) for g in groups}
        report['views'][name] = dict(cells=cells)
        if 'treated' in cells and 'control' in cells:
            report['views'][name]['contrast'] = contrast(cells['treated'], cells['control'])
    for identity in sorted({o['identity'] for o in outcomes}):
        rows = [o for o in outcomes if o['identity'] == identity]
        report['by_target'][identity] = dict(group=rows[0]['group'], version=rows[0].get('version'),
                                             **tally(rows, primary), kinds=dict(collections.Counter(o['kind'] for o in rows)))
    for g in groups:
        rows = [o for o in outcomes if o['group'] == g]
        cancels = collections.Counter(o.get('cancel') or 'unknown' for o in rows if o['kind'] == 'not_sent')
        report['exclusions'][g] = dict(total=len(rows), kinds=dict(collections.Counter(o['kind'] for o in rows)),
                                       not_sent=dict(cancels))
        gaps = sorted(o['gap_ms'] for o in rows if o.get('gap_ms') is not None)
        leads = sorted(o['lead_ms'] for o in rows if o.get('lead_ms') is not None)
        if gaps:
            report['gap'][g] = dict(median=gaps[len(gaps) // 2], low=gaps[0], high=gaps[-1])
        if leads:
            report['lead'][g] = dict(median=leads[len(leads) // 2], low=leads[0], high=leads[-1])
        ages = [o['link_age_s'] for o in rows if primary(o) and o.get('link_age_s') is not None]
        if ages:
            report['young_link'][g] = dict(n=len(ages), young=sum(1 for a in ages if a < MIN_LINK_AGE_S))
    if {'treated', 'control'} <= set(groups):
        treated, control = report['views']['primary']['cells']['treated'], report['views']['primary']['cells']['control']
        fisher = report['views']['primary']['contrast']['fisher_greater']
        low = worst_case(outcomes, 'treated', True)
        high = worst_case(outcomes, 'control', False)
        robust = fisher_exact(low['k'], low['n'] - low['k'], high['k'], high['n'] - high['k'])[1]
        report['selection_bound'] = dict(treated=low, control=high, fisher_greater=robust)
        report['verdict'] = verdict(treated, control, fisher, report['gap'], report['by_target'])
    return report


def verdict(treated, control, fisher, gaps, by_target):
    if treated['n'] < MIN_INFORMATIVE or control['n'] < MIN_INFORMATIVE:
        return 'not_evaluable: fewer than 20 informative pairs in a group'
    validators = [v for v in by_target.values() if v['group'] == 'treated']
    if any(v['n'] < MIN_VALIDATOR_PAIRS for v in validators):
        return 'not_evaluable: a treated validator has fewer than 10 informative pairs'
    for g in ('treated', 'control'):
        if g in gaps and abs(gaps[g]['median'] - 10.0) > MAX_GAP_ERROR_MS:
            return f'withdrawn: median achieved gap in {g} is {gaps[g]["median"]} ms'
    if (fisher < FISHER_ALPHA and treated['share'] >= TREATED_SHARE and treated['share'] - control['share'] >= MIN_DIFFERENCE
            and all(v['share'] >= TREATED_SHARE for v in validators)):
        return 'supported'
    return 'not_supported'


def rtt_report(run_dir, gap_ms=10.0):
    """RTT per address, and the empirical bound on a network swap: how often a ping sent up to 20 ms before the next one came
    back at least `gap_ms` slower. A swap of the pair needs at least that, in one direction alone."""
    rows = collections.defaultdict(list)
    try:
        with open(os.path.join(run_dir, 'rtt.jsonl')) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                rows[r['addr']].append((r['t_ns'] - r['rtt_ms'] * 1e6, r.get('rtt_adj_ms', r['rtt_ms'])))
    except FileNotFoundError:
        return {}
    report = {}
    for addr, samples in rows.items():
        samples.sort()
        rtts = sorted(s[1] for s in samples)
        close = [(a[1], b[1]) for a, b in zip(samples, samples[1:]) if b[0] - a[0] <= 20e6]
        worse = sum(1 for a, b in close if a - b >= gap_ms)
        near = sum(1 for a, b in close if a - b >= SHORT_GAP_MS)
        n = len(rtts)
        report[addr] = dict(n=n, min=rtts[0], p50=rtts[n // 2], p99=rtts[min(n - 1, int(0.99 * n))], max=rtts[-1],
                            close_pairs=len(close), slower_by_gap=worse, swap_bound=clopper_pearson(worse, len(close))[1] if close else None,
                            slower_by_short_gap=near, short_swap_bound=clopper_pearson(near, len(close))[1] if close else None)
    return report


def render(report):
    lines = ['# Block T result', '']
    if 'verdict' in report:
        lines += [f'**Verdict under H5: {report["verdict"]}**', '']
    lines += ['| View | Group | k/n | Share | 95% Clopper-Pearson |', '|---|---|---:|---:|---|']
    for name, view in report['views'].items():
        for g, c in view['cells'].items():
            share = '–' if c['share'] is None else f'{c["share"]:.2f}'
            lines.append(f'| {name} | {g} | {c["k"]}/{c["n"]} | {share} | {c["low"]:.2f}–{c["high"]:.2f} |')
    lines.append('')
    for name, view in report['views'].items():
        if 'contrast' in view:
            lines.append(f'- {name}: Fisher, treated above control p = {view["contrast"]["fisher_greater"]:.2g}, two-sided p = {view["contrast"]["fisher_two_sided"]:.2g}')
    if 'selection_bound' in report:
        s = report['selection_bound']
        lines.append(f'- Worst-case selection: treated {s["treated"]["k"]}/{s["treated"]["n"]}, control {s["control"]["k"]}/{s["control"]["n"]}, p = {s["fisher_greater"]:.2g}')
    lines += ['', '## By validator', '', '| Validator | Group | Version | k/n | 95% | Classes |', '|---|---|---|---:|---|---|']
    for identity, row in report['by_target'].items():
        kinds = ', '.join(f'{k} {v}' for k, v in sorted(row['kinds'].items()))
        lines.append(f'| `{identity[:8]}` | {row["group"]} | {row["version"]} | {row["k"]}/{row["n"]} | {row["low"]:.2f}–{row["high"]:.2f} | {kinds} |')
    lines += ['', '## Exclusions', '']
    for g, row in report['exclusions'].items():
        lines.append(f'- {g}: total {row["total"]}, classes {row["kinds"]}, not sent {row["not_sent"]}')
    for g, row in report['gap'].items():
        lines.append(f'- Gap {g}: median {row["median"]} ms, range {row["low"]}–{row["high"]}')
    for g, row in report['young_link'].items():
        lines.append(f'- Pairs on a connection younger than {MIN_LINK_AGE_S:.0f} s, {g}: {row["young"]} of {row["n"]}')
    for g, row in report['lead'].items():
        lines.append(f'- Lead over the first shred {g}: median {row["median"]} ms, range {row["low"]}–{row["high"]}')
    if report.get('network'):
        lines += ['', '## Could the network have swapped a pair', '', '| Address | RTT, ms: min / p50 / p99 / max | Close ping pairs | Slower by at least 10 ms | Upper 95% bound |', '|---|---|---:|---:|---:|']
        for addr, r in report['network'].items():
            bound = '–' if r['swap_bound'] is None else f'{r["swap_bound"]:.4f}'
            lines.append(f'| {addr} | {r["min"]:.1f} / {r["p50"]:.1f} / {r["p99"]:.1f} / {r["max"]:.1f} | {r["close_pairs"]} | {r["slower_by_gap"]} | {bound} |')
    return '\n'.join(lines) + '\n'


def manifest(run_dir):
    digests = {}
    for name in sorted(os.listdir(run_dir)):
        path = os.path.join(run_dir, name)
        if os.path.isfile(path) and name != 'manifest.json':
            with open(path, 'rb') as f:
                digests[name] = hashlib.sha256(f.read()).hexdigest()
    with open(os.path.join(run_dir, 'manifest.json'), 'w') as f:
        json.dump(digests, f, indent=1)
    return digests


def write_report(run_dirs, out_dir):
    outcomes = []
    network = {}
    for run_dir in run_dirs:
        with open(os.path.join(run_dir, 'outcomes.jsonl')) as f:
            outcomes += [json.loads(line) for line in f]
        network.update(rtt_report(run_dir))
    report = analyze(outcomes)
    report['network'] = network
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'summary.json'), 'w') as f:
        json.dump(report, f, indent=1)
    text = render(report)
    with open(os.path.join(out_dir, 'summary.md'), 'w') as f:
        f.write(text)
    for run_dir in run_dirs:
        manifest(run_dir)
    return report, text


def verify(rpc, run_dir, seed):
    """Recheck a run against the chain through any RPC, plus everything that can be recomputed from the journal alone."""
    pairs = {p['seq']: p for p in load_pairs(run_dir)}
    problems = []
    checked = dict(blocks=0, signatures=0, coins=0, ties=0, leaders_unknown=0)
    cache = {}
    with open(os.path.join(run_dir, 'outcomes.jsonl')) as f:
        outcomes = [json.loads(line) for line in f]
    for o in outcomes:
        pair = pairs[o['seq']]
        if pair.get('first_slot') and schedule.coin(seed, pair['first_slot'], pair['identity']) != pair['first_sent']:
            problems.append(f'#{o["seq"]}: send order does not follow the seed')
        planned = contrasts.KINDS.get(pair.get('kind'))
        if planned and schedule.x_role(seed, pair['first_slot'], pair['identity'], pair['kind']) != pair['x_role']:
            problems.append(f'#{o["seq"]}: which payer carries x does not follow the seed')
        checked['coins'] += 1
        if pair.get('raw_a') and pair.get('raw_b'):
            raw_a, raw_b = base64.b64decode(pair['raw_a']), base64.b64decode(pair['raw_b'])
            tx.verify_signature(raw_a)
            tx.verify_signature(raw_b)
            checked['signatures'] += 2
            wire = dict(a=tx.wire_priority(raw_a), b=tx.wire_priority(raw_b))
            if planned:
                other = 'a' if pair['x_role'] == 'b' else 'b'
                for role, template in ((pair['x_role'], planned.x), (other, planned.y)):
                    if (wire[role]['fee'], wire[role]['priority']) != (template.total_fee(), template.priority(contrasts.data_bytes(template))):
                        problems.append(f'#{o["seq"]}: the wire fee or priority is not that of {pair["kind"]}')
            if (not planned or planned.equal_key) and wire['a'] != wire['b']:
                problems.append(f'#{o["seq"]}: the pair does not tie on the wire')
            checked['ties'] += 1
        for member, role in ((o.get('first'), o['first_sent']), (o.get('second'), 'a' if o['first_sent'] == 'b' else 'b')):
            if not member:
                continue
            view = block_view(rpc, member['slot'], cache, need_leader=False)
            checked['blocks'] += 1
            if view['positions'].get(o[f'sig_{role}']) != member['position']:
                problems.append(f'#{o["seq"]}: {o[f"sig_{role}"]} is not at index {member["position"]} of slot {member["slot"]}')
            if view['leader'] is None:
                checked['leaders_unknown'] += 1
            elif view['leader'] != member.get('leader'):
                problems.append(f'#{o["seq"]}: leader of slot {member["slot"]} differs')
    spent = collections.Counter((p['identity'], p['kind']) for p in pairs.values() if p.get('kind') and p.get('status') in journal.SPENT_STATUSES)
    problems += [f'{identity[:8]}: {count} pairs of {kind}, the quota is {contrasts.QUOTA[kind]}' for (identity, kind), count in spent.items()
                 if count > contrasts.QUOTA[kind]]
    return checked, problems
