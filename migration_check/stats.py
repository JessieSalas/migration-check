"""Integer statistics and rational tails, following the original study's graded primary test (scripts not included).

NumPy is optional. With NumPy the graded null uses the reference RNG stream;
the stdlib fallback uses seed-0 exact hypergeometric sampling, not Gaussian noise.
"""

from collections import Counter
from fractions import Fraction
from functools import lru_cache
import math
import random

try:
    import numpy as np
except ImportError:
    np = None

PERMUTATIONS = 10_000
ALPHA = Fraction(1, 20)


def bh(pvalues):
    order = sorted(range(len(pvalues)), key=pvalues.__getitem__)
    result, running = [Fraction(1)] * len(order), Fraction(1)
    for rank in range(len(order), 0, -1):
        j = order[rank - 1]
        running = min(running, Fraction(pvalues[j]) * len(order) / rank)
        result[j] = running
    return result


@lru_cache(maxsize=4096)
def hypergeom(total, r):
    return tuple((x, math.comb(total, x) * math.comb(2 * r - total, r - x))
                 for x in range(max(0, total - r), min(r, total) + 1))


def sample_count(total, r, rng):
    position = rng.randrange(math.comb(2 * r, r))
    for count, weight in hypergeom(total, r):
        if position < weight:
            return count
        position -= weight
    raise AssertionError("hypergeometric weights do not sum to the sample space")


def exact_item_p(ki, kc, r):
    observed, total = abs(ki - kc), ki + kc
    return Fraction(sum(weight for x, weight in hypergeom(total, r) if abs(2 * x - total) >= observed),
                    math.comb(2 * r, r))


@lru_cache(maxsize=4096)
def permutation_p(ki, kc, r, permutations=PERMUTATIONS, seed=0):
    """Binary, pre-specified outcome test; do not use for a selected mode."""
    observed, total = abs(ki - kc), ki + kc
    if observed == 0:
        return Fraction(1)
    rng = random.Random(seed)
    exceed = sum(abs(2 * sample_count(total, r, rng) - total) >= observed for _ in range(permutations))
    return Fraction(exceed + 1, permutations + 1)


@lru_cache(maxsize=512)
def tv_null(totals, r):
    """Count all labelled reallocations by their integer TV statistic.

    Grouping identical answers gives multivariate hypergeometric weights.
    A state budget bounds exact enumeration; large problems use shuffling.
    """
    states = {(0, 0): 1}
    processed = 0
    for total in totals:
        next_states = Counter()
        remaining = 2 * r - processed - total
        for (selected, distance), weight in states.items():
            for x in range(max(0, r - selected - remaining), min(total, r - selected) + 1):
                next_states[selected + x, distance + abs(2 * x - total)] += weight * math.comb(total, x)
            if len(next_states) > 100_000:
                return None
        states = next_states
        processed += total
    return tuple(sorted((distance, weight) for (selected, distance), weight in states.items() if selected == r))


@lru_cache(maxsize=4096)
def tv_tail(totals, observed, r, permutations, seed):
    null = tv_null(totals, r)
    if observed == 0:
        return Fraction(1), Fraction(1) if null is not None else None
    rng = random.Random(seed)
    if null is not None:
        denominator = math.comb(2 * r, r)
        tail = sum(weight for distance, weight in null if distance >= observed)
        exact = Fraction(tail, denominator)
        # Sampling the tail indicator is equivalent to sampling uniform pooled
        # reallocations, without materialising each of the 10,000 shuffles.
        exceed = sum(rng.randrange(denominator) < tail for _ in range(permutations))
    else:
        pooled = [j for j, total in enumerate(totals) for _ in range(total)]
        exact, exceed = None, 0
        for _ in range(permutations):
            counts = Counter(rng.sample(pooled, r))
            distance = sum(abs(2 * counts[j] - total) for j, total in enumerate(totals))
            exceed += distance >= observed
    return Fraction(exceed + 1, permutations + 1), exact


def distribution_test(old, new, permutations=PERMUTATIONS, seed=0):
    """Permutation test of total variation, invariant to mode and draw order."""
    r = len(old)
    if not r or len(new) != r:
        raise ValueError("Answer distributions must have equal, positive draw counts")
    a, b = Counter(old), Counter(new)
    pooled = a + b
    observed = sum(abs(a[value] - b[value]) for value in pooled)
    p, exact = tv_tail(tuple(sorted(pooled.values())), observed, r, permutations, seed)
    return observed, p, exact


def minp_test(observed, null, exhaustive=False):
    """Inclusive two-sided empirical ranks and exact cross-multiplied min-p."""
    count, k = len(null), len(observed)
    if not count or not k:
        return [], []
    columns = [Counter(abs(row[j]) for row in null) for j in range(k)]
    tails = []
    for column in columns:
        cumulative, ranks = 0, {}
        for value in sorted(column, reverse=True):
            cumulative += column[value]
            ranks[value] = cumulative
        tails.append(ranks)
    offset = 0 if exhaustive else 1
    raw = [Fraction(sum(n for value, n in columns[j].items() if value >= abs(observed[j])) + offset,
                    count + offset) for j in range(k)]
    minima = Counter(min(tails[j][abs(row[j])] for j in range(k)) for row in null)
    adjusted = [max(p, Fraction(sum(n for rank, n in minima.items()
                                    if rank * p.denominator <= p.numerator * count) + offset, count + offset))
                for p in raw]
    return raw, adjusted


def graded_test(a, b, memberships, permutations=PERMUTATIONS, seed=0):
    r, n = len(a), len(memberships)
    if r != len(b) or not r or any(len(row) != n for row in a + b):
        raise ValueError("Arms must have matching [draw, item] shapes")
    labels = sorted(set(memberships))
    index = {label: j for j, label in enumerate(labels)}
    membership = [index[label] for label in memberships]
    totals = [sum(row[i] for row in a + b) for i in range(n)]
    observed = [0] * len(labels)
    for i, j in enumerate(membership):
        observed[j] += sum(row[i] for row in a) - sum(row[i] for row in b)
    if np is not None:
        rng = np.random.default_rng(seed)
        total = np.asarray(totals, dtype=np.int64)
        null = []
        for start in range(0, permutations, 512):
            splits = rng.hypergeometric(total, 2 * r - total, r, size=(min(512, permutations - start), n))
            differences = 2 * splits - total
            chunk = np.zeros((len(splits), len(labels)), dtype=np.int64)
            for i, j in enumerate(membership):
                chunk[:, j] += differences[:, i]
            null.extend(chunk.tolist())
        engine = "numpy reference RNG"
    else:
        rng, null = random.Random(seed), []
        for _ in range(permutations):
            row = [0] * len(labels)
            for total, j in zip(totals, membership):
                row[j] += 2 * sample_count(total, r, rng) - total
            null.append(row)
        engine = "stdlib exact hypergeometric RNG"
    raw, adjusted = minp_test(observed, null)
    counts = Counter(memberships)
    tests = [{"slice": label, "n_items": counts[label], "statistic": observed[j],
              "statistic_denominator": r * counts[label], "expected_drop": observed[j] / (r * counts[label]),
              "p": float(raw[j]), "adjusted_p": float(adjusted[j]),
              "direction": "worse" if observed[j] > 0 else "better" if observed[j] < 0 else "unchanged",
              "flagged": adjusted[j] <= ALPHA and observed[j] != 0} for j, label in enumerate(labels)]
    return {"slice_tests": tests, "permutations": permutations, "seed": seed, "engine": engine,
            "method": "primary two-sided single-step min-p, inclusive absolute integer tails",
            "worse": [row for row in tests if row["flagged"] and row["direction"] == "worse"],
            "better": [row for row in tests if row["flagged"] and row["direction"] == "better"]}
