"""Operating characteristics of the H5 decision rule, exact where possible.

The rule: supported when the one-sided Fisher p < 0.01, the treated share is at least 0.80, the difference from control is
at least 0.20, and every treated validator shows at least 0.80 on at least 10 pairs. The first table is exact for the pooled
part. The field tables simulate the whole rule, with pairs lost to landing and validators that differ from one another.
"""
import functools
import random

from .results import FISHER_ALPHA, MIN_DIFFERENCE, MIN_INFORMATIVE, MIN_VALIDATOR_PAIRS, TREATED_SHARE
from .rules import MIN_INFORMATIVE as MIN_RULE_PAIRS
from .stats import binom_pmf, binom_two_sided, fisher_exact, holm


@functools.lru_cache(maxsize=None)
def passes(k1, n1, k2, n2):
    if k1 < TREATED_SHARE * n1 or n1 < MIN_INFORMATIVE or n2 < MIN_INFORMATIVE:
        return False
    if k1 / n1 - k2 / n2 < MIN_DIFFERENCE:
        return False
    return fisher_exact(k1, n1 - k1, k2, n2 - k2)[1] < FISHER_ALPHA


def exact_power(n1, p1, n2, p2):
    total = 0.0
    for k1 in range(n1 + 1):
        a = binom_pmf(k1, n1, p1)
        if a < 1e-12:
            continue
        for k2 in range(n2 + 1):
            if passes(k1, n1, k2, n2):
                total += a * binom_pmf(k2, n2, p2)
    return total


def group_tallies(rng, rate, spread, pairs, validators, landing):
    """(k, n) per validator. Each has its own rate around the group's, a pair counts only when both probes land."""
    rows = []
    for _ in range(validators):
        own = min(1.0, max(0.0, rng.gauss(rate, spread)))
        k = n = 0
        for _ in range(pairs):
            if rng.random() < landing * landing:
                n += 1
                k += rng.random() < own
        rows.append((k, n))
    return rows


def field_power(p_treated, p_control, spread_treated=0.05, spread_control=0.15, pairs=30, validators=3, landing=0.9, runs=4000, seed=7):
    rng = random.Random(seed)
    wins = 0
    for _ in range(runs):
        treated = group_tallies(rng, p_treated, spread_treated, pairs, validators, landing)
        control = group_tallies(rng, p_control, spread_control, pairs, validators, landing)
        k1, n1 = sum(k for k, _ in treated), sum(n for _, n in treated)
        k2, n2 = sum(k for k, _ in control), sum(n for _, n in control)
        wins += passes(k1, n1, k2, n2) and all(n >= MIN_VALIDATOR_PAIRS and k >= TREATED_SHARE * n for k, n in treated)
    return wins / runs


def rule_power(strictness, landing, pairs=24, validators=10, runs=4_000, seed=11):
    """Block P, the whole rule by simulation: share of validators named reward_per_cost, and share of runs that name all of them."""
    rng = random.Random(seed)
    named = everyone = 0
    pvalue = {}

    def p_of(k, n):
        return pvalue.setdefault((k, n), binom_two_sided(k, n))

    for _ in range(runs):
        cells = []
        for _ in range(validators * 2):
            n = sum(rng.random() < landing for _ in range(pairs))
            cells.append((sum(rng.random() < strictness for _ in range(n)), n))
        adjusted = holm([p_of(k, n) for k, n in cells])
        hits = sum(all(n >= MIN_RULE_PAIRS and p < 0.05 and k / n > 0.5 for (k, n), p in zip(cells[i:i + 2], adjusted[i:i + 2]))
                   for i in range(0, 2 * validators, 2))
        named += hits
        everyone += hits == validators
    return named / (runs * validators), everyone / runs


def share_power(strictness, landing, pairs=(36, 54), runs=4_000, seed=13):
    """Block P, S: for each group, the share of runs that name half the signature fee, with Holm over the four pooled tests."""
    rng = random.Random(seed)
    hits = [0] * len(pairs)
    pvalue = {}

    def drawn(planned, rate):
        n = sum(rng.random() < landing for _ in range(planned))
        return sum(rng.random() < rate for _ in range(n)), n

    for _ in range(runs):
        cells = [drawn(planned, rate) for planned in pairs for rate in (strictness, 1 - strictness)]
        adjusted = holm([pvalue.setdefault(c, binom_two_sided(*c)) for c in cells])
        for g in range(len(pairs)):
            (k1, n1), (k2, n2) = cells[2 * g:2 * g + 2]
            hits[g] += (min(n1, n2) >= MIN_RULE_PAIRS and max(adjusted[2 * g:2 * g + 2]) < 0.05 and k1 / n1 > 0.5 and k2 / n2 < 0.5)
    return [h / runs for h in hits]


def table():
    lines = ['Exact power: share of runs where the H5 rule says supported.', '',
             'n per group | control | treated 0.75 | 0.80 | 0.90 | 0.95 | 1.00',
             '---:|---:|---:|---:|---:|---:|---:']
    for n in (30, 60, 90):
        for control in (0.42, 0.58, 0.71):
            cells = [f'{exact_power(n, t, n, control):.3f}' for t in (0.75, 0.80, 0.90, 0.95, 1.00)]
            lines.append(f'{n} | {control:.2f} | ' + ' | '.join(cells))
    lines += ['', 'False alarm with no change (both groups share one rate):', '',
              'n per group | 0.58 | 0.71 | 0.85', '---:|---:|---:|---:']
    for n in (30, 60, 90):
        lines.append(f'{n} | ' + ' | '.join(f'{exact_power(n, p, n, p):.4f}' for p in (0.58, 0.71, 0.85)))
    for label, st, sc in (('spread between validators 0.05 in treated, 0.15 in control', 0.05, 0.15), ('spread 0.15 in both', 0.15, 0.15)):
        lines += ['', f'Field conditions, the whole rule: 3 validators of 30 pairs, each probe lands at 0.9, {label}', '',
                  'control | treated 0.90 | 0.95 | 0.98 | 1.00', '---:|---:|---:|---:|---:']
        for control in (0.42, 0.58, 0.71):
            cells = [f'{field_power(t, control, st, sc):.3f}' for t in (0.90, 0.95, 0.98, 1.00)]
            lines.append(f'{control:.2f} | ' + ' | '.join(cells))
    lines += ['', 'False alarm in field conditions: both groups share one rate, spread 0.15 in both', '',
              'rate | 0.58 | 0.71 | 0.80 | 0.90', '---:|---:|---:|---:|---:',
              'alarm | ' + ' | '.join(f'{field_power(p, p, 0.15, 0.15):.3f}' for p in (0.58, 0.71, 0.80, 0.90))]
    lines += ['', 'Block P, the whole rule: 10 validators, 24 pairs per contrast, Holm over 20 tests. Share named reward_per_cost / share of runs that name all ten.',
              'both probes land | follows 0.90 | 0.95 | 1.00 | no effect 0.50', '---:|---:|---:|---:|---:']
    for landing in (0.4, 0.6, 0.8):
        cells = ['{:.3f} / {:.3f}'.format(*rule_power(s, landing, runs=2_000)) for s in (0.90, 0.95, 1.00, 0.50)]
        lines.append(f'{landing:.1f} | ' + ' | '.join(cells))
    lines += ['', 'Block P, S: Holm over 4 pooled tests, 36 planned pairs per contrast in the group whose source was read, 54 in the other. Share of runs that name half, read / unread.',
              'both probes land | follows 0.90 | 0.95 | 1.00', '---:|---:|---:|---:']
    for landing in (0.4, 0.6, 0.8):
        cells = ['{:.3f} / {:.3f}'.format(*share_power(s, landing, runs=2_000)) for s in (0.90, 0.95, 1.00)]
        lines.append(f'{landing:.1f} | ' + ' | '.join(cells))
    return '\n'.join(lines) + '\n'
