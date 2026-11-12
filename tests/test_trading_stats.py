"""agent_testbench.trading.stats: PSR, MinTRL, the expected maximum Sharpe, DSR, MinBTL, PBO by CSCV and BH.

Checked four ways: against the deflated-sharpe package (values hard-coded below; compared live too when it happens to
be importable - it is not a dependency), against worked numbers from the papers and by hand, against a slow and
obvious CSCV, and by properties with hypothesis (derandomized, so CI sees the same examples every time)."""
import importlib.util
import math
import os
import random
import re
import statistics
import subprocess
import sys
import time
from itertools import combinations
from pathlib import Path
from statistics import NormalDist

import pytest

pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, assume, given, settings, strategies as st  # noqa: E402

from agent_testbench.trading import stats as S  # noqa: E402
from agent_testbench.trading.stats import StatsError  # noqa: E402

PROPS = settings(max_examples=200, derandomize=True, database=None, deadline=None,
                 suppress_health_check=[HealthCheck.too_slow])


# ---- against deflated-sharpe -----------------------------------------------------------------------------------------
# Produced with deflated-sharpe 0.1.0 (Apache-2.0, https://pypi.org/project/deflated-sharpe/), installed apart from
# this project:  pip install --no-deps --target <scratch dir> deflated-sharpe==0.1.0, then (as D = deflated_sharpe)
#   D.deflated_sharpe_ratio(observed_sr, num_trials, num_obs, skewness, kurtosis) -> (z, p), rounded to 6 decimals
#   D.min_backtest_length(target_sr, num_trials, alpha, skewness, kurtosis) -> int
#   D.benjamini_hochberg(p_values, alpha) -> [(index, p, significant)]
# Mapping its conventions onto ours (read from its docs and source):
# - observed_sr is the Sharpe ratio at the data's own frequency. Its docstring says "annualized", but its formula
#   divides by num_obs - 1 and squares the Sharpe the way the paper does for a per-period one: we pass per-period
#   Sharpes to both.
# - kurtosis is the plain kurtosis there too (3 for normal returns), skewness the same.
# - It returns the z statistic and p = 1 - Z[z]; the paper's PSR and DSR, and ours, are the probability Z[z] = 1 - p.
# - With num_trials = 1 it is PSR against 0. With num_trials = M > 1 it takes the trials' Sharpe variance to be
#   1/(T - 1) (the null variance of a per-period Sharpe estimate) and the extreme-value expansion
#   E[max of M normals] ~ sqrt(2 ln M) - (ln ln M + ln 4 pi) / (2 sqrt(2 ln M)), not the paper's Euler-Mascheroni
#   form. So its whole computation is PSR against its benchmark SR0 = E_gumbel(M) / sqrt(T - 1), which it gives away
#   as -z for observed_sr = 0 (then z = -SR0 * sqrt(T - 1)): we compare our PSR at that benchmark, and our E[max]
#   with its E_gumbel separately (ours is higher, and closer to the exact value: see the worked numbers).
# - min_backtest_length with num_trials = 1 is not the MinBTL of Bailey et al. (2014) but the smallest T whose p is
#   below alpha: the first whole number above our MinTRL at confidence 1 - alpha.
# Every match below is within 1e-6, the rounding of its 6 decimals.

PSR_REF = [  # (sharpe, T, skewness, kurtosis) -> its (z, p) with num_trials=1
    ((0.1, 252, 0.0, 3.0), (1.580352, 0.057013)),
    ((0.05, 1000, -0.5, 5.0), (1.559056, 0.059492)),
    ((0.2, 60, 0.3, 4.0), (1.559804, 0.059403)),
    ((-0.05, 500, -1.0, 8.0), (-1.143301, 0.873543)),
    ((2.5 / math.sqrt(250), 1250, -3.0, 10.0), (4.516703, 0.000003)),
    ((0.03, 2520, 0.1, 3.5), (1.507528, 0.065838)),
]
DSR_REF = [  # (sharpe, M, T, skewness, kurtosis) -> its (z, p), and z0: its z for observed_sr 0, skewness 0, kurtosis 3
    ((0.1, 100, 1000, 0.0, 3.0), (0.792463, 0.214045), -2.366255),
    ((0.15, 1000, 2000, -1.0, 6.0), (3.307544, 0.000471), -3.11647),
    ((0.3, 10, 100, 0.5, 3.5), (1.704923, 0.044104), -1.361924),
    ((0.08, 212, 1260, -0.4, 4.5), (0.204702, 0.418902), -2.630076),
]
MINTRL_REF = [  # (sharpe, alpha, skewness, kurtosis) -> its min_backtest_length with num_trials=1
    ((0.1, 0.05, 0.0, 3.0), 273),
    ((0.05, 0.05, -1.0, 6.0), 1141),
    ((0.2, 0.01, 0.5, 4.0), 127),
    ((0.15, 0.10, -0.3, 5.0), 79),
]
BH_REF = [  # (p-values, alpha) -> its significant flags
    (([0.01, 0.04, 0.03, 0.005, 0.5], 0.05), [True, True, True, True, False]),
    (([0.2, 0.001, 0.04, 0.045, 0.3, 0.012], 0.05), [False, True, False, False, False, True]),
    (([0.06, 0.07, 0.08], 0.05), [False, False, False]),
    (([0.0, 1.0, 0.02, 0.019], 0.1), [True, False, True, True]),
]


@pytest.mark.parametrize("case,ref", PSR_REF)
def test_psr_matches_deflated_sharpe(case, ref):
    sr, t, g3, g4 = case
    assert S.probabilistic_sharpe_ratio(sr, t, skewness=g3, kurtosis=g4) == pytest.approx(1 - ref[1], abs=1e-6)
    assert S.deflated_sharpe_ratio(sr, t, n_trials=1, trials_sharpe_variance=0.3, skewness=g3,
                                   kurtosis=g4) == pytest.approx(1 - ref[1], abs=1e-6)


@pytest.mark.parametrize("case,ref,z0", DSR_REF)
def test_dsr_matches_deflated_sharpe_at_its_benchmark(case, ref, z0):
    sr, m, t, g3, g4 = case
    sr0 = -z0 / math.sqrt(t - 1)
    assert S.probabilistic_sharpe_ratio(sr, t, benchmark=sr0, skewness=g3, kurtosis=g4) == pytest.approx(1 - ref[1],
                                                                                                         abs=1e-6)
    # the paper's expected maximum is above the extreme-value expansion (by 4.4% at M = 1000, 16% at M = 10), so our
    # DSR at the same trial variance is lower: the more cautious of the two
    ours = S.expected_max_sharpe(m, 1 / (t - 1))
    assert ours > sr0
    assert ours / sr0 - 1 < {10: 0.16, 100: 0.07, 212: 0.06, 1000: 0.045}[m]
    dsr = S.deflated_sharpe_ratio(sr, t, n_trials=m, trials_sharpe_variance=1 / (t - 1), skewness=g3, kurtosis=g4)
    assert dsr < 1 - ref[1]


@pytest.mark.parametrize("case,ref", MINTRL_REF)
def test_mintrl_matches_deflated_sharpe_min_backtest_length(case, ref):
    sr, alpha, g3, g4 = case
    assert math.floor(S.min_track_record_length(sr, skewness=g3, kurtosis=g4, confidence=1 - alpha)) + 1 == ref


@pytest.mark.parametrize("case,ref", BH_REF)
def test_benjamini_hochberg_matches_deflated_sharpe(case, ref):
    ps, alpha = case
    got = S.benjamini_hochberg(ps, alpha)
    assert list(got.rejected) == ref
    assert [q <= alpha + 1e-12 for q in got.adjusted] == ref


@pytest.mark.skipif(importlib.util.find_spec("deflated_sharpe") is None, reason="deflated-sharpe is not installed "
                    "(not a dependency; the hard-coded values above came from it)")
def test_against_deflated_sharpe_live():
    import deflated_sharpe as D
    for (sr, t, g3, g4), ref in PSR_REF:
        z, p = D.deflated_sharpe_ratio(sr, 1, t, g3, g4)
        assert (z, p) == pytest.approx(ref, abs=1e-6)
        assert S.probabilistic_sharpe_ratio(sr, t, skewness=g3, kurtosis=g4) == pytest.approx(1 - p, abs=1e-6)
    for (sr, m, t, g3, g4), ref, z0 in DSR_REF:
        z, p = D.deflated_sharpe_ratio(sr, m, t, g3, g4)
        assert (z, p) == pytest.approx(ref, abs=1e-6)
        assert D.deflated_sharpe_ratio(0.0, m, t)[0] == pytest.approx(z0, abs=1e-6)
        sr0 = -D.deflated_sharpe_ratio(0.0, m, t)[0] / math.sqrt(t - 1)
        assert S.probabilistic_sharpe_ratio(sr, t, benchmark=sr0, skewness=g3, kurtosis=g4) == pytest.approx(1 - p,
                                                                                                             abs=1e-6)
    for (sr, alpha, g3, g4), ref in MINTRL_REF:
        assert D.min_backtest_length(sr, 1, alpha, g3, g4) == ref
    for (ps, alpha), ref in BH_REF:
        assert [sig for _, _, sig in D.benjamini_hochberg(ps, alpha)] == ref


# ---- worked numbers --------------------------------------------------------------------------------------------------

def test_dsr_paper_example():
    # Bailey & Lopez de Prado (2014), the numerical example: N = 100 trials, V[SR_n] = 1/2 (annualized), T = 1250
    # daily returns (5 years of 250 days), annualized Sharpe 2.5, skewness -3, kurtosis 10. Per period:
    #   SR  = 2.5 / sqrt(250) = 0.158114,  V = 0.5 / 250 = 0.002
    #   E[max] factor = (1 - 0.577216) * Z^-1[0.99] + 0.577216 * Z^-1[1 - 1/(100 e)]
    #                 = 0.422784 * 2.326348 + 0.577216 * 2.680210 = 2.530603
    #   SR0 = sqrt(0.002) * 2.530603 = 0.113172
    #   DSR = Z[(0.158114 - 0.113172) * sqrt(1249) / sqrt(1 + 3 * 0.158114 + 9/4 * 0.158114^2)]
    #       = Z[0.044942 * 35.341194 / 1.237171] = Z[1.283816] = 0.9004       (the paper: 0.9004, below 0.95)
    # and with N = 46 trials it is 0.9505 (the paper: 0.9505, significant).
    sr, v = 2.5 / math.sqrt(250), 0.5 / 250
    assert S.expected_max_sharpe(100, v) == pytest.approx(0.113172, abs=1e-6)
    dsr = S.deflated_sharpe_ratio(sr, 1250, n_trials=100, trials_sharpe_variance=v, skewness=-3, kurtosis=10)
    assert dsr == pytest.approx(0.9004, abs=5e-5)
    dsr = S.deflated_sharpe_ratio(sr, 1250, n_trials=46, trials_sharpe_variance=v, skewness=-3, kurtosis=10)
    assert dsr == pytest.approx(0.9505, abs=5e-5)


def test_expected_max_of_ten_standard_normals():
    # (1 - gamma) * Z^-1[1 - 1/10] + gamma * Z^-1[1 - 1/(10 e)], gamma = 0.5772157:
    #   Z^-1[0.9] = 1.2815516, Z^-1[0.9632121] = 1.7892418
    #   0.4227843 * 1.2815516 + 0.5772157 * 1.7892418 = 0.5418199 + 1.0327784 = 1.5745983
    assert S.expected_max_sharpe(10) == pytest.approx(1.5745983, abs=1e-7)
    assert S.expected_max_sharpe(10, 4.0) == pytest.approx(2 * 1.5745983, abs=1e-6)     # scales with sqrt(V)
    assert S.expected_max_sharpe(1, 4.0) == 0.0                                          # one trial: nothing picked
    assert S.expected_max_sharpe(2) == pytest.approx(0.5772157 * NormalDist().inv_cdf(1 - 1 / (2 * math.e)), abs=1e-7)


@pytest.mark.parametrize("n,exact,within", [(2, 0.5641895835, 0.08), (5, 1.1629644736, 0.03), (10, 1.5387527308, 0.025),
                                            (100, 2.5075936364, 0.01), (1000, 3.2414357691, 0.005),
                                            (10000, 3.8516158171, 0.003)])
def test_expected_max_is_near_the_exact_value(n, exact, within):
    # exact E[max of n standard normals] = integral of x * n * phi(x) * Phi(x)^(n - 1) dx, by scipy.integrate.quad
    # (n = 2 is 1/sqrt(pi)); the paper's approximation is 8% low at n = 2 and within 2.5% from n = 5 on
    assert abs(S.expected_max_sharpe(n) / exact - 1) < within


def test_paper_expected_max_is_closer_to_exact_than_the_extreme_value_expansion():
    # deflated-sharpe's E_gumbel(n) (its -z for observed_sr 0, see above) against the exact values of the test above
    for n, exact, gumbel in ((10, 1.5387527308, 1.361924), (100, 2.5075936364, 2.366255),
                             (1000, 3.2414357691, 3.11647)):
        assert abs(S.expected_max_sharpe(n) - exact) < abs(gumbel - exact) / 2


def test_min_backtest_length_paper_example():
    # Bailey, Borwein, Lopez de Prado & Zhu (2014): with 5 years of data, no more than 45 independent trials before an
    # annualized Sharpe of 1 is expected from luck alone. Z^-1[1 - 1/45] = 2.0098748, Z^-1[1 - 1/(45 e)] = 2.4010031:
    #   MinBTL = (0.4227843 * 2.0098748 + 0.5772157 * 2.4010031)^2 / 1^2 = 2.2356401^2 = 4.998 years < 2 ln 45 = 7.61
    assert S.min_backtest_length(45, annual_sharpe=1) == pytest.approx(4.998087, abs=1e-6)
    assert S.min_backtest_length(45, annual_sharpe=1) < 2 * math.log(45)
    assert S.min_backtest_length(45, annual_sharpe=2) == pytest.approx(4.998087 / 4, abs=1e-6)
    assert S.min_backtest_length(46, annual_sharpe=1) > 5 > S.min_backtest_length(45, annual_sharpe=1)
    assert S.min_backtest_length(1, annual_sharpe=1) == 0.0


def test_psr_of_a_short_series_by_hand():
    # r = 0.02 -0.01 0.03 -0.02 0.01 0.04 -0.03 0.02 (T = 8), mean 0.0075. In units of u = 0.0025 the deviations
    # from the mean are 5 -7 9 -11 1 13 -15 5, so
    #   sum d^2 = 696, sum d^3 = -1872, sum d^4 = 104040
    #   m2 = 87, m3 = -234, m4 = 13005 (each sum / 8); sample sd = sqrt(696 / 7) = 9.9713876 u
    #   SR = 3 / 9.9713876 = 0.3008608, skewness = -234 / 87^1.5 = -0.2883613, kurtosis = 13005 / 87^2 = 1.7181926
    #   1 - g3*SR + (g4 - 1)/4 * SR^2 = 1 + 0.0867555 + 0.1795482 * 0.0905173 = 1.1030088, sqrt = 1.0502423
    #   PSR(0) = Z[0.3008608 * sqrt(7) / 1.0502423] = Z[0.7960029 / 1.0502423] = Z[0.7579232] = 0.7757515
    r = [0.02, -0.01, 0.03, -0.02, 0.01, 0.04, -0.03, 0.02]
    got = S.return_stats(r)
    assert got.n_obs == 8
    assert got.sharpe == pytest.approx(0.3008608, abs=1e-7)
    assert got.sharpe == pytest.approx(statistics.mean(r) / statistics.stdev(r), rel=1e-12)
    assert got.skewness == pytest.approx(-0.2883613, abs=1e-7)
    assert got.kurtosis == pytest.approx(1.7181926, abs=1e-7)
    assert S.probabilistic_sharpe_ratio(**got._asdict()) == pytest.approx(0.7757515, abs=1e-7)


def test_mintrl_by_hand():
    # SR = 0.1 per period, normal returns, 95%: 1 + (1 + 2/4 * 0.01) * (1.6448536 / 0.1)^2 = 1 + 1.005 * 270.5543
    assert S.min_track_record_length(0.1) == pytest.approx(272.907117, abs=1e-6)
    # PSR reaches the confidence at that length, by definition
    t = S.min_track_record_length(0.1, benchmark=0.02, skewness=-1, kurtosis=6, confidence=0.9)
    assert S.probabilistic_sharpe_ratio(0.1, math.ceil(t), benchmark=0.02, skewness=-1, kurtosis=6) >= 0.9
    assert S.probabilistic_sharpe_ratio(0.1, math.ceil(t) - 1, benchmark=0.02, skewness=-1, kurtosis=6) < 0.9
    assert S.min_track_record_length(0.1, benchmark=0.1) == math.inf
    assert S.min_track_record_length(-0.1) == math.inf


def test_pbo_by_hand():
    # Two variants over 4 periods, S = 2 blocks of 2 rows:      A               B
    #   block 1 (rows 0-1)                                     0.01, 0.03      0.00, 0.01
    #   block 2 (rows 2-3)                                    -0.01, 0.01      0.02, 0.04
    # Sharpe (mean / sample sd):  block 1: A 0.02/0.0141421 = 1.4142, B 0.005/0.0070711 = 0.7071
    #                             block 2: A 0/0.0141421 = 0,         B 0.03/0.0141421 = 2.1213
    # Split 1, in-sample block 1: A is best (1.4142); out of sample A 0 < B 2.1213: rank 1 of 2, w = 1/3, logit ln(1/2)
    # Split 2, in-sample block 2: B is best (2.1213); out of sample B 0.7071 < A 1.4142: rank 1, logit ln(1/2)
    # PBO = 2/2 = 1. No out-of-sample loss (0 and 0.7071 are not below 0). The line through (1.4142, 0) and
    # (2.1213, 0.7071) has slope 1 and intercept -1.4142.
    rows = [[0.01, 0.00], [0.03, 0.01], [-0.01, 0.02], [0.01, 0.04]]
    got = S.probability_of_backtest_overfitting(rows, 2)
    assert got.pbo == 1.0
    assert got.logits == pytest.approx((math.log(0.5), math.log(0.5)))
    assert got.prob_oos_loss == 0.0
    assert sorted(got.is_sharpe) == pytest.approx([math.sqrt(2), 1.5 * math.sqrt(2)])
    assert sorted(got.oos_sharpe) == pytest.approx([0.0, math.sqrt(0.5)])
    assert got.degradation_slope == pytest.approx(1.0)
    assert got.degradation_intercept == pytest.approx(-math.sqrt(2))
    assert (got.n_strategies, got.n_splits, got.n_combinations, got.rows_used, got.rows_dropped) == (2, 2, 2, 4, 0)


# ---- PBO against a slow, obvious CSCV --------------------------------------------------------------------------------

def slow_cscv(rows: list[list[float]], s: int) -> tuple[float, list[float], float, float | None]:
    """CSCV as the paper lists its steps: every combination of S/2 blocks, Sharpe ratios from the rows themselves."""
    rows = rows[len(rows) % s:]
    size, n = len(rows) // s, len(rows[0])
    blocks = [rows[k * size:(k + 1) * size] for k in range(s)]

    def sharpe(part, j):
        col = [row[j] for row in part]
        return statistics.mean(col) / statistics.stdev(col)

    logits, pairs = [], []
    for chosen in combinations(range(s), s // 2):
        ins = [row for k in chosen for row in blocks[k]]
        oos = [row for k in range(s) if k not in chosen for row in blocks[k]]
        r_in = [sharpe(ins, j) for j in range(n)]
        r_out = [sharpe(oos, j) for j in range(n)]
        best = r_in.index(max(r_in))
        below = sum(1 for x in r_out if x < r_out[best])
        ties = sum(1 for x in r_out if x == r_out[best])
        w = (below + (ties + 1) / 2) / (n + 1)
        logits.append(math.log(w / (1 - w)))
        pairs.append((r_in[best], r_out[best]))
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    slope = sum((x - mx) * (y - my) for x, y in pairs) / sum((x - mx) ** 2 for x in xs)
    return sum(1 for v in logits if v <= 0) / len(logits), sorted(logits), sum(1 for y in ys if y < 0) / len(ys), slope


def noise(seed: int, t: int, n: int, drift=(), sd: float = 0.01) -> list[list[float]]:
    rnd = random.Random(seed)
    drift = list(drift) + [0.0] * (n - len(drift))
    return [[rnd.gauss(drift[j], sd) for j in range(n)] for _ in range(t)]


@pytest.mark.parametrize("seed,t,n,s", [(1, 40, 3, 2), (2, 61, 5, 4), (3, 90, 7, 6), (4, 163, 6, 8), (5, 48, 2, 4)])
def test_pbo_matches_the_slow_cscv(seed, t, n, s):
    rows = noise(seed, t, n, drift=[0.002, -0.001])
    got = S.probability_of_backtest_overfitting(rows, s)
    pbo, logits, loss, slope = slow_cscv(rows, s)
    assert got.pbo == pbo
    assert sorted(got.logits) == pytest.approx(logits, abs=1e-9)
    assert got.prob_oos_loss == loss
    assert got.degradation_slope == pytest.approx(slope, abs=1e-9)
    assert got.n_combinations == math.comb(s, s // 2) and got.rows_dropped == t % s and got.rows_used == t - t % s


def test_pbo_realistic_size():
    # 4 years of daily returns of 100 variants, S = 16: 12870 splits in about a second (the bound only catches a slow
    # path, such as computing every split's Sharpe ratios twice or from the rows)
    rows = noise(7, 1000, 100)
    start = time.perf_counter()
    got = S.probability_of_backtest_overfitting(rows)
    assert time.perf_counter() - start < 20
    assert got.n_combinations == 12870 == len(got.logits) and got.rows_dropped == 1000 % 16
    assert 0 <= got.pbo <= 1 and 0 <= got.prob_oos_loss <= 1 and got.degradation_slope is not None


def test_pbo_iid_noise_is_near_one_half():
    # with columns of IID noise the in-sample best's out-of-sample rank is uniform, so PBO is 1/2 on average for an
    # even number of columns (one matrix alone can be far off: its splits share rows). 100 seeded matrices.
    pbos = [S.probability_of_backtest_overfitting(noise(seed, 160, 10), 8).pbo for seed in range(100)]
    assert statistics.mean(pbos) == pytest.approx(0.5, abs=0.08)


def test_pbo_numpy_and_dataframe_inputs():
    np = pytest.importorskip("numpy")
    rows = noise(11, 64, 4)
    want = S.probability_of_backtest_overfitting(rows, 4)
    assert S.probability_of_backtest_overfitting(np.array(rows), 4) == want
    pd = pytest.importorskip("pandas")
    assert S.probability_of_backtest_overfitting(pd.DataFrame(rows, columns=list("abcd")), 4) == want
    assert S.return_stats(pd.Series([r[0] for r in rows])) == S.return_stats([r[0] for r in rows])


def test_pbo_flat_halves():
    # variant 0 loses steadily; variant 1 does nothing in the first half (Sharpe 0 there, not a division by zero),
    # then earns steadily: it is picked in both splits and beats variant 0 out of sample both times
    e = [r[0] / 10 for r in noise(12, 16, 1)]
    rows = [[-0.01 + e[t], 0.0 if t < 8 else 0.01 + e[t]] for t in range(16)]
    got = S.probability_of_backtest_overfitting(rows, 2)
    assert got.pbo == 0.0 and got.prob_oos_loss == 0.0
    assert 0.0 in got.is_sharpe and 0.0 in got.oos_sharpe
    # flat at a value other than 0: an infinite Sharpe ratio, best in-sample, and left out of the degradation fit
    rows = [[-0.01 + e[t], 0.001 if t < 8 else 0.01 + e[t]] for t in range(16)]
    got = S.probability_of_backtest_overfitting(rows, 2)
    assert math.inf in got.is_sharpe and got.pbo == 0.0
    assert got.degradation_slope is None                    # no finite pair left: no line


# ---- properties ------------------------------------------------------------------------------------------------------

skews = st.floats(-4, 4)
excess = st.floats(0.01, 50)          # kurtosis = 1 + skewness^2 + excess (no return series has less than 1 + skew^2)


@PROPS
@given(sr=st.floats(-2, 2), t=st.integers(2, 100_000), bench=st.floats(-1, 1), g3=skews, extra=excess,
       n=st.integers(1, 10 ** 6), v=st.floats(0, 0.1))
def test_psr_and_dsr_are_probabilities(sr, t, bench, g3, extra, n, v):
    g4 = 1 + g3 * g3 + extra
    assert 0 <= S.probabilistic_sharpe_ratio(sr, t, benchmark=bench, skewness=g3, kurtosis=g4) <= 1
    assert 0 <= S.deflated_sharpe_ratio(sr, t, n_trials=n, trials_sharpe_variance=v, skewness=g3, kurtosis=g4) <= 1


@PROPS
@given(sr=st.floats(-2, 2), t=st.integers(2, 100_000), g3=skews, extra=excess, n1=st.integers(1, 10 ** 6),
       n2=st.integers(1, 10 ** 6), v=st.floats(0, 0.1))
def test_dsr_does_not_rise_with_more_trials(sr, t, g3, extra, n1, n2, v):
    g4 = 1 + g3 * g3 + extra
    lo, hi = sorted((n1, n2))
    fewer = S.deflated_sharpe_ratio(sr, t, n_trials=lo, trials_sharpe_variance=v, skewness=g3, kurtosis=g4)
    more = S.deflated_sharpe_ratio(sr, t, n_trials=hi, trials_sharpe_variance=v, skewness=g3, kurtosis=g4)
    assert more <= fewer + 1e-12
    assert S.expected_max_sharpe(hi, v) >= S.expected_max_sharpe(lo, v)


@PROPS
@given(sr=st.floats(-2, 2), t=st.integers(2, 100_000), g3=skews, extra=excess, v=st.floats(0, 10))
def test_dsr_of_one_trial_is_psr_against_zero(sr, t, g3, extra, v):
    g4 = 1 + g3 * g3 + extra
    assert S.deflated_sharpe_ratio(sr, t, n_trials=1, trials_sharpe_variance=v, skewness=g3, kurtosis=g4) == \
        S.probabilistic_sharpe_ratio(sr, t, benchmark=0.0, skewness=g3, kurtosis=g4)


@PROPS
@given(bench=st.floats(0, 0.5), d1=st.floats(1e-4, 1.5), d2=st.floats(1e-4, 1.5), g3=st.floats(-3, 0), extra=excess,
       conf=st.floats(0.5, 0.999))
def test_mintrl_grows_as_the_sharpe_nears_the_benchmark(bench, d1, d2, g3, extra, conf):
    # for returns with skewness <= 0 (as most strategies' are) and a benchmark >= 0, d MinTRL / d SR < 0 for every
    # SR above the benchmark; with positive skewness that holds near the benchmark only
    g4 = 1 + g3 * g3 + extra
    near, far = sorted((d1, d2))
    assume(far - near > 1e-6)
    t_near = S.min_track_record_length(bench + near, benchmark=bench, skewness=g3, kurtosis=g4, confidence=conf)
    t_far = S.min_track_record_length(bench + far, benchmark=bench, skewness=g3, kurtosis=g4, confidence=conf)
    assert t_near >= t_far >= 1


@PROPS
@given(sr=st.floats(0.01, 1), bench=st.floats(-0.5, 0.5), g3=skews, extra=excess, conf=st.floats(0.5, 0.999))
def test_mintrl_is_where_psr_reaches_the_confidence(sr, bench, g3, extra, conf):
    g4 = 1 + g3 * g3 + extra
    assume(sr - bench > 1e-3)
    t = S.min_track_record_length(sr, benchmark=bench, skewness=g3, kurtosis=g4, confidence=conf)
    assume(t < 1e9)
    psr = S.probabilistic_sharpe_ratio(sr, max(2, math.ceil(t)), benchmark=bench, skewness=g3, kurtosis=g4)
    assert psr >= conf - 1e-9


@PROPS
@given(data=st.data(), n=st.integers(2, 5), s=st.sampled_from([2, 4, 6]), size=st.integers(2, 5),
       extra=st.integers(0, 5))
def test_pbo_is_a_probability(data, n, s, size, extra):
    t = s * size + extra % s
    rows = data.draw(st.lists(st.lists(st.floats(-0.05, 0.05), min_size=n, max_size=n), min_size=t, max_size=t))
    rows = [[x + 1e-3 * ((i * 7 + j * 3) % 5) for j, x in enumerate(row)] for i, row in enumerate(rows)]   # not flat
    assume(all(min(c) != max(c) for c in zip(*rows)))
    got = S.probability_of_backtest_overfitting(rows, s)
    assert 0 <= got.pbo <= 1 and 0 <= got.prob_oos_loss <= 1
    assert len(got.logits) == math.comb(s, s // 2) and all(math.isfinite(v) for v in got.logits)


@PROPS
@given(seed=st.integers(0, 10 ** 6), n=st.integers(2, 8), s=st.sampled_from([2, 4, 8]))
def test_pbo_of_a_variant_that_dominates_every_split_is_zero(seed, n, s):
    # variant 0 earns 1% a period with 0.1% noise; the others are noise around 0: it is best in and out of sample
    rows = noise(seed, 8 * s, n, drift=[0.01])
    for row in rows:
        row[0] = 0.01 + (row[0] - 0.01) / 10
    got = S.probability_of_backtest_overfitting(rows, s)
    assert got.pbo == 0.0 and got.prob_oos_loss == 0.0


@PROPS
@given(ps=st.lists(st.floats(0, 1), max_size=30), alpha=st.floats(0.001, 0.5))
def test_benjamini_hochberg_properties(ps, alpha):
    got = S.benjamini_hochberg(ps, alpha)
    assert len(got.rejected) == len(got.adjusted) == len(ps)
    assert all(p * (1 - 1e-12) <= q <= 1 for p, q in zip(ps, got.adjusted))
    # every p-value at or below a rejected one is rejected too, and none above alpha ever is
    worst = max((p for p, r in zip(ps, got.rejected) if r), default=-1)
    assert all(r for p, r in zip(ps, got.rejected) if p <= worst)
    assert not any(r for p, r in zip(ps, got.rejected) if p > alpha)


@PROPS
@given(st.lists(st.one_of(st.floats(), st.integers(), st.booleans(), st.none()), max_size=12))
def test_any_series_gives_stats_or_a_plain_refusal(values):
    try:
        got = S.return_stats(values)
    except StatsError as e:
        assert any(w in str(e) for w in ("give", "leave", "drop", "divide", "check")), e
        return
    assert all(math.isfinite(v) for v in got) and got.kurtosis >= 1 + got.skewness ** 2 - 1e-6
    assert 0 <= S.probabilistic_sharpe_ratio(**got._asdict()) <= 1


@PROPS
@given(st.lists(st.lists(st.one_of(st.floats(), st.integers(-10 ** 30, 10 ** 30)), min_size=2, max_size=3),
                min_size=4, max_size=12))
def test_any_table_gives_a_pbo_or_a_plain_refusal(rows):
    try:
        got = S.probability_of_backtest_overfitting(rows, 2)
    except StatsError as e:
        assert any(w in str(e) for w in ("give", "leave", "drop", "divide", "check")), e
        return
    assert 0 <= got.pbo <= 1 and all(math.isfinite(v) for v in got.logits)


# ---- a loop's tries --------------------------------------------------------------------------------------------------

def test_deflate_tries_from_returns():
    tries = [[r[0] for r in noise(seed, 252, 1, drift=[0.0005])] for seed in range(20)]
    got = S.deflate_tries(tries)
    stats = [S.return_stats(r) for r in tries]
    sharpes = [x.sharpe for x in stats]
    best = sharpes.index(max(sharpes))
    assert (got.n_trials, got.best_index, got.n_obs) == (20, best, 252)
    assert got.trials_sharpe_variance == pytest.approx(statistics.variance(sharpes))
    assert got.expected_max_sharpe == pytest.approx(S.expected_max_sharpe(20, statistics.variance(sharpes)))
    assert got.dsr == pytest.approx(S.deflated_sharpe_ratio(
        sharpes[best], 252, n_trials=20, trials_sharpe_variance=statistics.variance(sharpes),
        skewness=stats[best].skewness, kurtosis=stats[best].kurtosis))
    assert got.dsr < S.probabilistic_sharpe_ratio(**stats[best]._asdict())       # deflating lowers it
    assert re.fullmatch(r"best of 20 tries: deflated Sharpe [<> ]*[01]\.\d+ \(probability the true Sharpe is above "
                        r"0\); (not )?significant at 0\.95", got.verdict), got.verdict


def test_deflate_tries_from_sharpes():
    # 212 tries whose per-period Sharpe ratios spread like noise with sd 0.02: the best, 0.057, is about what the best
    # of 212 reaches by luck
    sharpes = [0.02 * NormalDist().inv_cdf((i + 0.5) / 212) for i in range(212)]
    got = S.deflate_tries(sharpes, n_obs=1260)
    assert got.n_trials == 212 and got.best_index == 211 and got.n_obs == 1260
    assert (got.skewness, got.kurtosis) == (0.0, 3.0)
    assert 0.3 < got.dsr < 0.7 and not got.significant
    assert got.verdict == (f"best of 212 tries: deflated Sharpe {got.dsr:.2f} (probability the true Sharpe is above "
                           "0); not significant at 0.95")
    one = S.deflate_tries([0.2], n_obs=1000)
    assert one.trials_sharpe_variance is None and one.expected_max_sharpe == 0.0
    assert one.dsr == S.probabilistic_sharpe_ratio(0.2, 1000)
    assert one.verdict == ("1 try (nothing to deflate): probabilistic Sharpe > 0.9999 (probability the true Sharpe is "
                           "above 0); significant at 0.95")
    strong = S.deflate_tries([0.3, 0.01, 0.02], n_obs=1000, skewness=-1, kurtosis=6, confidence=0.99)
    assert strong.significant and strong.verdict.endswith("; significant at 0.99")
    same = S.deflate_tries([0.05, 0.05, 0.05], n_obs=500)          # no spread across tries: nothing to deflate
    assert same.trials_sharpe_variance == 0.0 and same.dsr == S.probabilistic_sharpe_ratio(0.05, 500)


# ---- bad inputs: each refused with what to give instead --------------------------------------------------------------

BAD = [
    (lambda: S.return_stats([0.01, float("nan"), 0.02]), r"returns\[1\] is nan: drop or fill"),
    (lambda: S.return_stats([0.01, 0.02, float("inf")]), r"returns\[2\] is inf"),
    (lambda: S.return_stats([0.01]), r"has 1 return: a Sharpe ratio needs at least 2"),
    (lambda: S.return_stats([]), r"has 0 returns"),
    (lambda: S.return_stats([0.003] * 50), r"never changes \(every return is 0.003\).*leave out a strategy"),
    (lambda: S.return_stats("0.01 0.02"), r"must be a list of per-period returns"),
    (lambda: S.return_stats([0.01, "x"]), r"returns\[1\] is 'x': give one return per period"),
    (lambda: S.probabilistic_sharpe_ratio(0.1, 1), r"n_obs is 1: a Sharpe ratio needs at least 2 returns"),
    (lambda: S.probabilistic_sharpe_ratio(0.1, 10.5), r"n_obs must be a whole number"),
    (lambda: S.probabilistic_sharpe_ratio(0.1, True), r"n_obs must be a whole number"),
    (lambda: S.probabilistic_sharpe_ratio(float("nan"), 100), r"sharpe is nan: give a finite number"),
    (lambda: S.probabilistic_sharpe_ratio(0.1, 100, benchmark=float("-inf")), r"benchmark is -inf"),
    (lambda: S.probabilistic_sharpe_ratio(0.1, 100, kurtosis=0.0), r"excess kurtosis.*add 3"),
    (lambda: S.probabilistic_sharpe_ratio(0.1, 100, skewness=2, kurtosis=4), r"below 1 \+ skewness\^2"),
    (lambda: S.probabilistic_sharpe_ratio(1e200, 100), r"too large to be a per-period Sharpe ratio"),
    (lambda: S.probabilistic_sharpe_ratio(1.0, 100, skewness=2, kurtosis=5), r"standard error comes out as 0"),
    (lambda: S.deflated_sharpe_ratio(0.1, 100, n_trials=0, trials_sharpe_variance=0.01), r"n_trials is 0: give the"),
    (lambda: S.deflated_sharpe_ratio(0.1, 100, n_trials=2.5, trials_sharpe_variance=0.01), r"whole number"),
    (lambda: S.deflated_sharpe_ratio(0.1, 100, n_trials=10, trials_sharpe_variance=-0.01), r"a variance is 0 or more"),
    (lambda: S.deflated_sharpe_ratio(0.1, 100, n_trials=10, trials_sharpe_variance=float("nan")), r"is nan"),
    (lambda: S.expected_max_sharpe(10 ** 16), r"more than 10\^15"),
    (lambda: S.expected_max_sharpe(-3), r"n_trials is -3"),
    (lambda: S.min_track_record_length(0.1, confidence=1.0), r"confidence is 1.0: give the probability"),
    (lambda: S.min_track_record_length(0.1, confidence=0.3), r"from 0.5 up to"),
    (lambda: S.min_backtest_length(10, annual_sharpe=0), r"annual_sharpe is 0.0: .* above 0"),
    (lambda: S.min_backtest_length(0, annual_sharpe=1), r"n_trials is 0"),
    (lambda: S.probability_of_backtest_overfitting(noise(1, 64, 3), 5), r"n_splits is 5: .*even number"),
    (lambda: S.probability_of_backtest_overfitting(noise(1, 64, 3), 18), r"n_splits is 18: at most 16"),
    (lambda: S.probability_of_backtest_overfitting(noise(1, 64, 3), 0), r"n_splits is 0"),
    (lambda: S.probability_of_backtest_overfitting(noise(1, 20, 3), 16),
     r"20 rows \(periods\), too few for 16 blocks.*at least 32 periods, or fewer blocks \(n_splits=10"),
    (lambda: S.probability_of_backtest_overfitting(noise(1, 3, 3), 2), r"at least 4 periods$"),
    (lambda: S.probability_of_backtest_overfitting(noise(1, 64, 1), 4), r"1 column: .*at least 2 columns.*zip"),
    (lambda: S.probability_of_backtest_overfitting([], 4), r"returns is empty"),
    (lambda: S.probability_of_backtest_overfitting([[0.01, 0.02], [0.03]], 2), r"row 1 has 1 values but row 0 has 2"),
    (lambda: S.probability_of_backtest_overfitting([[0.01, float("nan")]] * 8, 2), r"row 0, column 1 is nan"),
    (lambda: S.probability_of_backtest_overfitting([0.01] * 8, 2), r"row 0 is 0.01, not a row of returns"),
    (lambda: S.probability_of_backtest_overfitting([[r[0], r[1], 0.0] for r in noise(1, 16, 2)], 2),
     r"column 2 never changes.*leave that variant out"),
    (lambda: S.benjamini_hochberg([0.01, 1.2]), r"p_values\[1\] is 1.2: give p-values from 0 to 1"),
    (lambda: S.benjamini_hochberg([0.01], alpha=0), r"alpha is 0.0: .*above 0 and below 1"),
    (lambda: S.benjamini_hochberg(0.01), r"p_values must be a list"),
    (lambda: S.deflate_tries([]), r"tries is empty"),
    (lambda: S.deflate_tries([0.1, 0.2]), r"give n_obs"),
    (lambda: S.deflate_tries([0.1, [0.01, 0.02]]), r"mixes Sharpe ratios and return series \(tries\[0\] and tries\[1"),
    (lambda: S.deflate_tries([[0.01, 0.02], [0.01, 0.03]], n_obs=2), r"leave out n_obs"),
    (lambda: S.deflate_tries([[0.01, 0.02], [0.01, float("nan"), 0.02]]), r"tries\[1\]\[1\] is nan"),
    (lambda: S.deflate_tries([[0.01, 0.02], [0.02, 0.02]]), r"tries\[1\] never changes"),
    (lambda: S.deflate_tries([0.1, 0.2], n_obs=100, confidence=95), r"confidence is 95.0"),
]


@pytest.mark.parametrize("call,message", BAD)
def test_bad_inputs_are_refused_with_what_to_give(call, message):
    with pytest.raises(StatsError, match=message) as e:
        call()
    assert isinstance(e.value, ValueError)


def test_empty_p_values_give_empty_results():
    assert S.benjamini_hochberg([]) == ((), ())


# ---- importing it is light -------------------------------------------------------------------------------------------

def test_import_loads_no_numpy_scipy_pandas():
    src = str(Path(S.__file__).resolve().parents[2])
    code = ("import sys, agent_testbench.trading.stats as s; print(s.__file__); "
            "print(sorted(m for m in ('numpy', 'scipy', 'pandas') if m in sys.modules))")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([src, *filter(None, [os.environ.get("PYTHONPATH")])])}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, env=env)
    assert out.returncode == 0, out.stderr
    where, loaded = out.stdout.splitlines()
    assert Path(where).resolve() == Path(S.__file__).resolve()
    assert loaded == "[]"
