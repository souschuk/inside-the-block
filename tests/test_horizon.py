import importlib.util
import json
import os
import random

path = os.path.join(os.path.dirname(__file__), '..', 'tools', 'horizon.py')
spec = importlib.util.spec_from_file_location('horizon', path)
horizon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(horizon)


def drawn(batch, rng, per_gap=36):
    return [(gap, rng.random() < horizon.overtakes(gap, batch)) for gap in (2, 10, 40) for _ in range(per_gap)]


def test_the_fit_finds_a_period_it_was_given():
    rng = random.Random(3)
    for batch in (2.5, 15.5, 50.0):
        fits = sorted(horizon.fit(drawn(batch, rng, 12))[0] for _ in range(41))
        assert abs(fits[20] - batch) <= 0.25 * batch


def test_a_validator_that_never_reorders_has_a_short_period():
    assert horizon.fit([(g, False) for g in (2, 10, 40) for _ in range(12)])[0] <= 2.5


def test_the_table_reads_outcomes(tmp_path):
    rows = [dict(identity='HEL1USMZKAL2', client='JitoLabs', version='4.4.0', contrast=kind, kind='same_slot', x_first=kind == 'W2', gap_ms=gap)
            for kind, gap in (('W2', 1.8), ('W10', 9.8), ('W40', 39.8)) for _ in range(4)]
    rows += [dict(identity='HEL1USMZKAL2', contrast='C4', kind='same_slot', x_first=True, gap_ms=9.8),
             dict(identity='HEL1USMZKAL2', contrast='W2', kind='none_landed', gap_ms=1.8)]
    (tmp_path / 'outcomes.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    text = horizon.render(horizon.read([str(tmp_path)]))
    best = horizon.fit([(r['gap_ms'], r['x_first']) for r in rows if r['contrast'] != 'C4' and r['kind'] == 'same_slot'])[0]
    assert '`HEL1USMZ`' in text and '| 4/4 | 0/4 | 0/4 |' in text and f'| {best:g} | ' in text and 2 < best < 40
