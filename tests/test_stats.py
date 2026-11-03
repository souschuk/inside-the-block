import math
import random

import pytest

from tiepairs.stats import binom_cdf, binom_pmf, binom_sf, clopper_pearson, fisher_exact, holm


def test_binomial_sums_to_one():
    assert sum(binom_pmf(k, 30, 0.58) for k in range(31)) == pytest.approx(1)
    assert binom_cdf(12, 30, 0.5) + binom_sf(13, 30, 0.5) == pytest.approx(1)
    assert binom_pmf(0, 5, 0.0) == 1 and binom_pmf(5, 5, 1.0) == 1


def test_clopper_pearson_meets_its_definition():
    for k, n in ((4, 54), (45, 78), (23, 35), (10, 29), (29, 30)):
        low, high = clopper_pearson(k, n)
        assert binom_sf(k, n, low) == pytest.approx(0.025, abs=1e-6)
        assert binom_cdf(k, n, high) == pytest.approx(0.025, abs=1e-6)


def test_clopper_pearson_edges():
    assert clopper_pearson(0, 10)[0] == 0
    assert clopper_pearson(10, 10)[1] == 1
    assert clopper_pearson(0, 0) == (0, 1)
    assert clopper_pearson(30, 30)[0] == pytest.approx(0.8843, abs=1e-3)


def test_published_interval_for_firedancer_ties():
    low, high = clopper_pearson(4, 54)
    assert round(low, 2) == 0.02 and round(high, 2) == 0.18


def test_against_scipy():
    scipy = pytest.importorskip('scipy.stats')
    rng = random.Random(3)
    for _ in range(40):
        n = rng.randint(1, 90)
        k = rng.randint(0, n)
        low, high = clopper_pearson(k, n)
        if k:
            assert low == pytest.approx(scipy.beta.ppf(0.025, k, n - k + 1), abs=1e-6)
        if k < n:
            assert high == pytest.approx(scipy.beta.ppf(0.975, k + 1, n - k), abs=1e-6)
        table = [[rng.randint(0, 30), rng.randint(0, 30)], [rng.randint(0, 30), rng.randint(0, 30)]]
        two, greater = fisher_exact(table[0][0], table[0][1], table[1][0], table[1][1])
        assert two == pytest.approx(scipy.fisher_exact(table)[1], abs=1e-6)
        assert greater == pytest.approx(scipy.fisher_exact(table, alternative='greater')[1], abs=1e-6)


def test_fisher_known_tables():
    assert fisher_exact(8, 2, 1, 5)[0] == pytest.approx(0.03497, abs=1e-4)
    assert fisher_exact(1, 9, 11, 3)[0] == pytest.approx(0.002759, abs=1e-5)
    assert fisher_exact(0, 0, 0, 0) == (1.0, 1.0)


def test_fisher_sides():
    assert fisher_exact(10, 0, 0, 10)[1] == pytest.approx(1 / math.comb(20, 10))
    assert fisher_exact(0, 10, 10, 0)[1] == pytest.approx(1)


def test_holm():
    assert holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
