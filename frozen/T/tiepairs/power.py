"""Operating characteristics of the H5 decision rule, exact where possible.

The rule: supported when the one-sided Fisher p < 0.01, the treated share is at least 0.80, the difference from control is
at least 0.20, and every treated validator shows at least 0.80 on at least 10 pairs. The first table is exact for the pooled
part. The field tables simulate the whole rule, with pairs lost to landing and validators that differ from one another.
"""
import functools
import random

from .results import FISHER_ALPHA, MIN_DIFFERENCE, MIN_INFORMATIVE, MIN_VALIDATOR_PAIRS, TREATED_SHARE
from .stats import binom_pmf, fisher_exact


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


def table():
    lines = ['Точна потужність: частка запусків, де правило H5 каже «підтверджено».', '',
             'n на групу | контроль | лікування 0,75 | 0,80 | 0,90 | 0,95 | 1,00',
             '---:|---:|---:|---:|---:|---:|---:']
    for n in (30, 60, 90):
        for control in (0.42, 0.58, 0.71):
            cells = [f'{exact_power(n, t, n, control):.3f}' for t in (0.75, 0.80, 0.90, 0.95, 1.00)]
            lines.append(f'{n} | {control:.2f} | ' + ' | '.join(cells))
    lines += ['', 'Хибна тривога, коли змін немає (обидві групи мають однакову частку):', '',
              'n на групу | 0,58 | 0,71 | 0,85', '---:|---:|---:|---:']
    for n in (30, 60, 90):
        lines.append(f'{n} | ' + ' | '.join(f'{exact_power(n, p, n, p):.4f}' for p in (0.58, 0.71, 0.85)))
    for label, st, sc in (('розкид між валідаторами 0,05 у лікуванні, 0,15 у контролі', 0.05, 0.15), ('розкид 0,15 в обох', 0.15, 0.15)):
        lines += ['', f'Польові умови, усе правило: 3 валідатори по 30 пар, посадка кожної проби 0,9, {label}', '',
                  'контроль | лікування 0,90 | 0,95 | 0,98 | 1,00', '---:|---:|---:|---:|---:']
        for control in (0.42, 0.58, 0.71):
            cells = [f'{field_power(t, control, st, sc):.3f}' for t in (0.90, 0.95, 0.98, 1.00)]
            lines.append(f'{control:.2f} | ' + ' | '.join(cells))
    lines += ['', 'Хибна тривога в польових умовах: обидві групи мають однакову частку, розкид 0,15 в обох', '',
              'частка | 0,58 | 0,71 | 0,80 | 0,90', '---:|---:|---:|---:|---:',
              'тривога | ' + ' | '.join(f'{field_power(p, p, 0.15, 0.15):.3f}' for p in (0.58, 0.71, 0.80, 0.90))]
    return '\n'.join(lines) + '\n'
