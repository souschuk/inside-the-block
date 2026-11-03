"""Exact intervals and tests with the standard library only, so `analyze` needs nothing but the outcome files."""
import math


def log_choose(n, k):
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def binom_pmf(k, n, p):
    if p <= 0:
        return 1.0 if k == 0 else 0.0
    if p >= 1:
        return 1.0 if k == n else 0.0
    return math.exp(log_choose(n, k) + k * math.log(p) + (n - k) * math.log1p(-p))


def binom_cdf(k, n, p):
    return min(1.0, sum(binom_pmf(i, n, p) for i in range(0, k + 1)))


def binom_sf(k, n, p):
    """P(X >= k)."""
    return min(1.0, sum(binom_pmf(i, n, p) for i in range(k, n + 1)))


def _bisect(f, lo=0.0, hi=1.0):
    for _ in range(80):
        mid = (lo + hi) / 2
        if f(mid):
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def clopper_pearson(k, n, alpha=0.05):
    if n == 0:
        return (0.0, 1.0)
    lower = 0.0 if k == 0 else _bisect(lambda p: binom_sf(k, n, p) < alpha / 2)
    upper = 1.0 if k == n else _bisect(lambda p: binom_cdf(k, n, p) > alpha / 2)
    return (lower, upper)


def hypergeom(a, b, c, d):
    """Probability of the 2x2 table [[a, b], [c, d]] with fixed margins."""
    return math.exp(log_choose(a + b, a) + log_choose(c + d, c) - log_choose(a + b + c + d, a + c))


def fisher_exact(a, b, c, d):
    """Table [[a, b], [c, d]]. Returns the two-sided p (tables no likelier than the observed one) and P(A >= a)."""
    row1, row2, col1 = a + b, c + d, a + c
    low, high = max(0, col1 - row2), min(row1, col1)
    observed = hypergeom(a, b, c, d)
    two_sided = greater = 0.0
    for x in range(low, high + 1):
        p = hypergeom(x, row1 - x, col1 - x, row2 - col1 + x)
        if p <= observed * (1 + 1e-9):
            two_sided += p
        if x >= a:
            greater += p
    return min(1.0, two_sided), min(1.0, greater)


def holm(pvalues):
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    adjusted = [0.0] * len(pvalues)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(pvalues) - rank) * pvalues[i]))
        adjusted[i] = running
    return adjusted
