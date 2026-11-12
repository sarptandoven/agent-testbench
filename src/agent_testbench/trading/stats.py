"""Is a backtest's Sharpe ratio real, or the luck of trying many variants? The statistics that answer it.

An agent that tries hundreds of strategy variants will find one that looks great by chance. These functions correct
for that. They are implemented from the formulas in the published papers; no code was taken from any implementation.

  probabilistic_sharpe_ratio, min_track_record_length
      Bailey & Lopez de Prado (2012), "The Sharpe Ratio Efficient Frontier", Journal of Risk 15(2), 3-44.
  expected_max_sharpe, deflated_sharpe_ratio, deflate_tries
      Bailey & Lopez de Prado (2014), "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest
      Overfitting and Non-Normality", Journal of Portfolio Management 40(5), 94-107.
  min_backtest_length
      Bailey, Borwein, Lopez de Prado & Zhu (2014), "Pseudo-Mathematics and Financial Charlatanism: The Effects of
      Backtest Overfitting on Out-of-Sample Performance", Notices of the AMS 61(5), 458-471.
  probability_of_backtest_overfitting
      Bailey, Borwein, Lopez de Prado & Zhu (2017), "The Probability of Backtest Overfitting", Journal of
      Computational Finance 20(4), 39-69.
  benjamini_hochberg
      Benjamini & Hochberg (1995), "Controlling the False Discovery Rate: A Practical and Powerful Approach to
      Multiple Testing", Journal of the Royal Statistical Society B 57(1), 289-300.

Equation numbers differ between the SSRN preprints and the journal versions, so each docstring names the paper and
the result in it. docs/trading-stats.md says what each number answers and when it is meaningless.

Units, the one thing to get right:
- Every Sharpe ratio here is PER PERIOD, not annualized: the mean over the standard deviation of the (excess) returns
  at the frequency they were observed, so daily returns give a daily Sharpe. PSR, MinTRL and DSR are defined on that
  Sharpe and on T, the number of returns it was estimated from. From an annualized Sharpe SR_a of data with q periods
  a year (252 trading days, 52 weeks, 12 months): SR = SR_a / sqrt(q); a variance of annualized Sharpes V_a is V_a / q.
- min_backtest_length is the exception: it takes an annualized Sharpe and answers in years.
- kurtosis is the plain kurtosis, 3 for normal returns, not the excess kurtosis (0 for normal returns) that pandas
  and scipy report by default: add 3 to an excess kurtosis.

Bad inputs (NaN or inf, too few returns, fewer than 1 trial, an odd or too large number of CSCV blocks, returns that
never change) raise StatsError, a ValueError whose message says what to give instead. Standard library only:
importing this module loads neither numpy nor scipy nor pandas.
"""
from __future__ import annotations

import bisect
import math
import numbers
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import combinations
from typing import NamedTuple

EULER_GAMMA = 0.5772156649015329    # the Euler-Mascheroni constant
MAX_SPLITS = 16                      # CSCV blocks: C(16, 8) = 12870 splits; 18 would be 48620, slow in pure Python
MAX_TRIALS = 10 ** 15                # also the most returns n_obs may count
TOO_LARGE = 1e100                    # no per-period return (or P&L) is this large: bigger values overflow the moments
_Z = statistics.NormalDist()         # the standard normal: _Z.cdf is Z[.], _Z.inv_cdf is Z^-1[.]


class StatsError(ValueError):
    """A statistic was asked for with inputs it cannot use. The message says what was wrong and what to give instead."""


# ---- checking inputs -------------------------------------------------------------------------------------------------

def _float(x) -> float | None:
    """x as a float; None when it is not a real number (True and False are not); an int too large for a float is inf."""
    if isinstance(x, bool) or not isinstance(x, numbers.Real):
        return None
    try:
        return float(x)
    except OverflowError:
        return math.copysign(math.inf, x)


def _real(x, name: str) -> float:
    v = _float(x)
    if v is None:
        raise StatsError(f"{name} must be a number, not {x!r}")
    if not math.isfinite(v):
        raise StatsError(f"{name} is {v}: give a finite number")
    return v


def _cell(x, name: str) -> float:
    """One return: a finite number of a sane size, or StatsError."""
    v = _float(x)
    if v is None:
        raise StatsError(f"{name} is {x!r}: give one return per period, as a number (0.01 for 1%)")
    if not math.isfinite(v):
        raise StatsError(f"{name} is {v}: drop or fill missing periods before computing statistics (one NaN or inf "
                         "makes every statistic NaN)")
    if abs(v) > TOO_LARGE:
        raise StatsError(f"{name} is {v:g}, far too large for a return: give returns as fractions (0.01 for 1%)")
    return v


def _whole(x, name: str, least: int, why: str) -> int:
    v = _float(x)
    if v is None or not (isinstance(x, numbers.Integral) or (math.isfinite(v) and v.is_integer())):
        raise StatsError(f"{name} must be a whole number, not {x!r}")
    n = int(x)
    if n < least:
        raise StatsError(f"{name} is {n}: {why}")
    return n


def _n_obs(x) -> int:
    n = _whole(x, "n_obs", 2, "a Sharpe ratio needs at least 2 returns; give T, the number of returns the Sharpe "
                              "ratio was computed from")
    if n > MAX_TRIALS:
        raise StatsError(f"n_obs is {n}, more than 10^15: give T, the number of returns the Sharpe ratio was computed "
                         "from")
    return n


def _n_trials(x) -> int:
    n = _whole(x, "n_trials", 1, "give the number of strategy variants tried, counting the one being judged "
                                 "(at least 1)")
    if n > MAX_TRIALS:
        raise StatsError(f"n_trials is {n}, more than 10^15: no search tries that many independent variants; give "
                         "the number of variants actually tried")
    return n


def _variance(x) -> float:
    v = _real(x, "trials_sharpe_variance")
    if v < 0:
        raise StatsError(f"trials_sharpe_variance is {v}: a variance is 0 or more; give the variance of the trials' "
                         "per-period Sharpe ratios")
    return v


def _confidence(x) -> float:
    c = _real(x, "confidence")
    if not 0.5 <= c < 1:
        raise StatsError(f"confidence is {c}: give the probability you want, from 0.5 up to (not including) 1, "
                         "such as 0.95")
    return c


def _shape(skewness, kurtosis) -> tuple[float, float]:
    g3, g4 = _real(skewness, "skewness"), _real(kurtosis, "kurtosis")
    if g4 < (1 + g3 * g3) * (1 - 1e-9):
        raise StatsError(f"kurtosis is {g4}, below 1 + skewness^2 = {1 + g3 * g3:g}, which no return series can "
                         "have. Give the plain kurtosis (3 for normal returns); if you have the excess kurtosis (0 "
                         "for normal returns, what pandas and scipy report by default), add 3 to it")
    return g3, g4


def _se2(sr: float, g3: float, g4: float) -> float:
    """1 - g3*SR + (g4 - 1)/4 * SR^2: T - 1 times the variance of the Sharpe ratio estimate (Mertens 2002, as used in
    Bailey & Lopez de Prado 2012). Never below (1 - g3*SR/2)^2 when g4 >= 1 + g3^2."""
    v = 1 - g3 * sr + (g4 - 1) / 4 * sr * sr
    if not math.isfinite(v):
        raise StatsError(f"sharpe {sr} is too large to be a per-period Sharpe ratio: divide an annualized Sharpe by "
                         "the square root of the number of periods a year")
    if v <= 0:
        raise StatsError(f"the Sharpe ratio's standard error comes out as 0 for sharpe {sr}, skewness {g3} and "
                         f"kurtosis {g4} (1 - skewness*sharpe + (kurtosis - 1)/4*sharpe^2 = {v:.3g}): check that "
                         "sharpe is per period, not annualized, and that kurtosis is the plain kurtosis (3 for normal "
                         "returns)")
    return v


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _returns(values, name: str) -> list[float]:
    """A return series as floats, or StatsError: not a series, a NaN or inf, fewer than 2 returns, never changes."""
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise StatsError(f"{name} must be a list of per-period returns (numbers, 0.01 for 1%), not "
                         f"{type(values).__name__}")
    out = [_cell(v, f"{name}[{i}]") for i, v in enumerate(values)]
    if len(out) < 2:
        raise StatsError(f"{name} has {_plural(len(out), 'return', 'returns')}: a Sharpe ratio needs at least 2; "
                         "give a longer series")
    if min(out) == max(out):
        raise StatsError(f"{name} never changes (every return is {out[0]!r}), so it has no standard deviation and no "
                         "Sharpe ratio: leave out a strategy that never trades, or give its real returns")
    return out


# ---- one return series -----------------------------------------------------------------------------------------------

class ReturnStats(NamedTuple):
    """What PSR, MinTRL and DSR need to know about one return series. Field names match their parameters, so
    probabilistic_sharpe_ratio(**return_stats(r)._asdict()) works."""
    sharpe: float      # per period: mean / sample standard deviation (T - 1 in the denominator)
    n_obs: int         # T, the number of returns
    skewness: float    # m3 / m2^1.5, from the central moments m_k = sum((r - mean)^k) / T
    kurtosis: float    # m4 / m2^2, plain (3 for normal returns), same moments


def _stats(r: list[float], name: str) -> ReturnStats:
    t, m2 = len(r), math.nan
    try:
        mean = math.fsum(r) / t
        d = [x - mean for x in r]
        d2 = [x * x for x in d]
        m2 = math.fsum(d2) / t
        m3 = math.fsum(x * y for x, y in zip(d, d2)) / t
        m4 = math.fsum(x * x for x in d2) / t
        if m2 > 0 and math.isfinite(m4):
            out = ReturnStats(mean / math.sqrt(m2 * t / (t - 1)), t, m3 / m2 ** 1.5, m4 / (m2 * m2))
            if all(math.isfinite(v) for v in out):
                return out
    except (OverflowError, ValueError):            # fsum's intermediate overflow
        pass
    raise StatsError(f"{name} are too small or too large to measure (their variance is {m2:.3g}): give returns as "
                     "fractions, 0.01 for 1%")


def return_stats(returns) -> ReturnStats:
    """The per-period Sharpe ratio, T, skewness and kurtosis of one series of per-period returns.

    Give excess returns (over the risk-free rate) if the Sharpe ratio should be against it: subtract the per-period
    risk-free rate first. With r_1..r_T and mean m:
        SR = m / s, s = sqrt(sum((r_t - m)^2) / (T - 1))          (the usual sample standard deviation)
        m_k = sum((r_t - m)^k) / T,  skewness = m3 / m2^(3/2),  kurtosis = m4 / m2^2   (3 for normal returns)
    The moments are the plain (biased) estimators, as scipy.stats.skew and scipy.stats.kurtosis(fisher=False)
    compute them by default; they always satisfy kurtosis >= 1 + skewness^2, which keeps PSR's standard error real.
    """
    return _stats(_returns(returns, "returns"), "returns")


# ---- PSR and MinTRL: Bailey & Lopez de Prado (2012) ------------------------------------------------------------------

def _psr(sr: float, benchmark: float, t: int, g3: float, g4: float) -> float:
    return _Z.cdf((sr - benchmark) * math.sqrt(t - 1) / math.sqrt(_se2(sr, g3, g4)))


def probabilistic_sharpe_ratio(sharpe, n_obs, *, benchmark=0.0, skewness=0.0, kurtosis=3.0) -> float:
    """The probability that the true Sharpe ratio is above `benchmark`, given an observed one (PSR).

    Bailey & Lopez de Prado (2012), "The Sharpe Ratio Efficient Frontier": the definition of the Probabilistic
    Sharpe Ratio, from the asymptotic distribution of the Sharpe ratio estimate under non-normal IID returns
    (Mertens 2002):
        PSR(SR*) = Z[ (SR - SR*) * sqrt(T - 1) / sqrt(1 - g3*SR + (g4 - 1)/4 * SR^2) ]
    Z is the standard normal CDF, SR the observed Sharpe ratio, SR* the benchmark, T the number of returns, g3 the
    skewness and g4 the kurtosis of the returns.

    sharpe and benchmark: PER PERIOD (not annualized), at the frequency of the returns; daily returns, daily Sharpe.
    n_obs: T, the number of returns the Sharpe ratio was estimated from (at least 2).
    skewness, kurtosis: of those returns; kurtosis is plain (3 for normal returns), not excess. The defaults (0, 3)
    assume normal returns. return_stats(returns) gives all four from a series.
    Returns a probability in [0, 1]. 1 - PSR is the one-sided p-value of "the true Sharpe is at most SR*".
    """
    t = _n_obs(n_obs)
    g3, g4 = _shape(skewness, kurtosis)
    return _psr(_real(sharpe, "sharpe"), _real(benchmark, "benchmark"), t, g3, g4)


def min_track_record_length(sharpe, *, benchmark=0.0, skewness=0.0, kurtosis=3.0, confidence=0.95) -> float:
    """How many returns it takes before an observed Sharpe ratio is above `benchmark` with the given confidence.

    Bailey & Lopez de Prado (2012), "The Sharpe Ratio Efficient Frontier": the Minimum Track Record Length, the T
    at which PSR(SR*) reaches the confidence c:
        MinTRL = 1 + (1 - g3*SR + (g4 - 1)/4 * SR^2) * (Z^-1[c] / (SR - SR*))^2
    sharpe and benchmark: PER PERIOD (not annualized). skewness, kurtosis: of the returns, kurtosis plain (3 for
    normal returns). confidence: from 0.5 up to (not including) 1.
    Returns a number of returns at the frequency of the Sharpe ratio (daily Sharpe: trading days), not rounded;
    a track record needs more returns than this. math.inf when sharpe <= benchmark: no track record is long enough.
    """
    sr, benchmark = _real(sharpe, "sharpe"), _real(benchmark, "benchmark")
    g3, g4 = _shape(skewness, kurtosis)
    c = _confidence(confidence)
    v = _se2(sr, g3, g4)
    if sr <= benchmark:
        return math.inf
    return 1 + v * (_Z.inv_cdf(c) / (sr - benchmark)) ** 2


# ---- the expected maximum and DSR: Bailey & Lopez de Prado (2014) ----------------------------------------------------

def _emax(n: int) -> float:
    """E[max of n IID standard normals] ~ (1 - gamma) Z^-1[1 - 1/n] + gamma Z^-1[1 - 1/(n e)]; 0 for n = 1. Uses
    Z^-1[1 - p] = -Z^-1[p], which stays accurate however large n is (1 - 1/n would round to 1)."""
    if n == 1:
        return 0.0
    return -(1 - EULER_GAMMA) * _Z.inv_cdf(1 / n) - EULER_GAMMA * _Z.inv_cdf(1 / (n * math.e))


def expected_max_sharpe(n_trials, trials_sharpe_variance=1.0) -> float:
    """The Sharpe ratio the best of N skill-less trials is expected to reach by luck alone (SR0 in the DSR).

    Bailey & Lopez de Prado (2014), "The Deflated Sharpe Ratio", from the expected maximum of N IID normals in
    Bailey, Borwein, Lopez de Prado & Zhu (2014): when N independent trials have Sharpe ratios with mean 0 (no skill,
    the null) and variance V,
        E[max_n SR_n] ~ sqrt(V) * ( (1 - gamma) * Z^-1[1 - 1/N] + gamma * Z^-1[1 - 1/(N e)] )
    gamma is the Euler-Mascheroni constant (0.5772...), e is Euler's number and Z^-1 the standard normal quantile.
    With V = 1 it is the expected maximum of N standard normals (1.5745 for N = 10; the exact value is 1.5388).
    N = 1 gives 0: one trial, nothing selected.

    n_trials: N, the number of independent trials, a whole number from 1.
    trials_sharpe_variance: V, the variance of the trials' Sharpe ratios. The answer is in the unit of sqrt(V): the
    variance of per-period Sharpes gives a per-period Sharpe, which is what deflated_sharpe_ratio needs.
    """
    return math.sqrt(_variance(trials_sharpe_variance)) * _emax(_n_trials(n_trials))


def deflated_sharpe_ratio(sharpe, n_obs, *, n_trials, trials_sharpe_variance, skewness=0.0, kurtosis=3.0) -> float:
    """The probability that the true Sharpe ratio is above 0, after correcting for picking the best of N trials (DSR).

    Bailey & Lopez de Prado (2014), "The Deflated Sharpe Ratio": the DSR is the PSR against SR0, the Sharpe ratio
    the best of N skill-less trials reaches by luck:
        SR0 = sqrt(V) * ( (1 - gamma) * Z^-1[1 - 1/N] + gamma * Z^-1[1 - 1/(N e)] )
        DSR = PSR(SR0) = Z[ (SR - SR0) * sqrt(T - 1) / sqrt(1 - g3*SR + (g4 - 1)/4 * SR^2) ]
    With N = 1 it is PSR(0).

    sharpe: SR, the observed Sharpe ratio of the selected (best) trial, PER PERIOD (not annualized).
    n_obs: T, the number of returns that Sharpe ratio was estimated from.
    n_trials: N, the number of independent trials it was selected from (1 or more). Tries that are near copies of each
    other are fewer independent trials: counting each one makes SR0 higher and the DSR lower, the cautious side.
    trials_sharpe_variance: V, the variance across the trials of their PER-PERIOD Sharpe ratios (an annualized
    variance is divided by the periods a year).
    skewness, kurtosis: of the selected trial's returns; kurtosis plain (3 for normal returns), not excess.
    Returns a probability in [0, 1]; 1 - DSR is the p-value. The paper's example tests it at a 95% confidence level.
    Its example: N = 100, V = 0.5 / 250 (1/2 annualized), T = 1250 daily returns, an annualized Sharpe of 2.5
    (0.1581 a day), skewness -3 and kurtosis 10 give SR0 = 0.1132 and a DSR of 0.9004, short of 0.95; N = 46 gives
    0.9505.
    """
    t = _n_obs(n_obs)
    sr0 = math.sqrt(_variance(trials_sharpe_variance)) * _emax(_n_trials(n_trials))
    g3, g4 = _shape(skewness, kurtosis)
    return _psr(_real(sharpe, "sharpe"), sr0, t, g3, g4)


# ---- MinBTL: Bailey, Borwein, Lopez de Prado & Zhu (2014) ------------------------------------------------------------

def min_backtest_length(n_trials, *, annual_sharpe) -> float:
    """The shortest backtest, in years, for which N skill-less trials are not expected to reach `annual_sharpe`.

    Bailey, Borwein, Lopez de Prado & Zhu (2014), "Pseudo-Mathematics and Financial Charlatanism": the Minimum
    Backtest Length. Over y years the annualized Sharpe estimate of a strategy with no skill has variance about 1/y,
    so the best of N such strategies is expected to reach E[max_N] ~ y^(-1/2) * ((1 - gamma) Z^-1[1 - 1/N] +
    gamma Z^-1[1 - 1/(N e)]). Solved for y:
        MinBTL ~ ( ((1 - gamma) * Z^-1[1 - 1/N] + gamma * Z^-1[1 - 1/(N e)]) / E[max_N] )^2  <  2 ln(N) / E[max_N]^2
    The paper's example: with 5 years of data, no more than 45 independent trials before an annualized Sharpe of 1
    is expected from luck alone (min_backtest_length(45, annual_sharpe=1) is 4.998).

    n_trials: N, the number of independent trials, from 1 (N = 1 gives 0: nothing is selected).
    annual_sharpe: E[max_N], the ANNUALIZED in-sample Sharpe ratio a skill-less best trial should not reach by luck.
    (A Sharpe per another unit of time gives the answer in that unit.)
    Returns years. A shorter backtest proves nothing: its best trial reaches annual_sharpe by luck. A longer one is
    necessary, not sufficient.
    """
    n = _n_trials(n_trials)
    target = _real(annual_sharpe, "annual_sharpe")
    if target <= 0:
        raise StatsError(f"annual_sharpe is {target}: give the annualized Sharpe ratio to tell apart from luck, above "
                         "0 (such as 1)")
    return (_emax(n) / target) ** 2


# ---- PBO by CSCV: Bailey, Borwein, Lopez de Prado & Zhu (2017) -------------------------------------------------------

@dataclass(frozen=True)
class PBOResult:
    """probability_of_backtest_overfitting's answer. Sharpe ratios are per period; 'split' is one CSCV combination."""
    pbo: float                                 # share of splits whose in-sample best ranked at or below the OOS median
    prob_oos_loss: float                       # share of splits whose in-sample best had a negative OOS Sharpe
    degradation_slope: float | None            # OLS slope of OOS on IS Sharpe of the in-sample best; None: no fit
    degradation_intercept: float | None
    logits: tuple[float, ...]                  # per split: ln(w / (1 - w)), w the in-sample best's relative OOS rank
    is_sharpe: tuple[float, ...]               # per split: the in-sample best's in-sample Sharpe
    oos_sharpe: tuple[float, ...]              # per split: its out-of-sample Sharpe
    n_strategies: int                          # N, the columns
    n_splits: int                              # S, the blocks the rows were cut into
    n_combinations: int                        # C(S, S/2), the splits
    rows_used: int                             # T used: a multiple of S
    rows_dropped: int                          # the oldest T mod S rows, left out so the blocks are equal


def _matrix(returns) -> list[list[float]]:
    if hasattr(returns, "to_numpy"):                      # a pandas DataFrame iterates over its column names
        returns = returns.to_numpy()
    if hasattr(returns, "tolist"):                        # a numpy array
        returns = returns.tolist()
    if isinstance(returns, (str, bytes)) or not isinstance(returns, Iterable):
        raise StatsError("returns must be a T x N table of per-period returns: one row per period, one column per "
                         "strategy variant (a list of rows, a numpy array or a DataFrame), not "
                         f"{type(returns).__name__}")
    rows = []
    for t, row in enumerate(returns):
        if isinstance(row, (str, bytes)) or not isinstance(row, Iterable):
            raise StatsError(f"returns row {t} is {row!r}, not a row of returns: give one row per period, each with "
                             "one return per strategy variant")
        vals = list(row)
        if rows and len(vals) != len(rows[0]):
            raise StatsError(f"returns row {t} has {len(vals)} values but row 0 has {len(rows[0])}: give every row "
                             "(period) one return for each strategy variant")
        rows.append([_cell(v, f"returns row {t}, column {j}") for j, v in enumerate(vals)])
    if not rows:
        raise StatsError("returns is empty: give a T x N table of per-period returns, one row per period and one "
                         "column per strategy variant")
    return rows


def probability_of_backtest_overfitting(returns, n_splits=MAX_SPLITS) -> PBOResult:
    """How likely it is that the variant that did best in-sample does worse than the median variant out of sample (PBO).

    Bailey, Borwein, Lopez de Prado & Zhu (2017), "The Probability of Backtest Overfitting": combinatorially
    symmetric cross-validation (CSCV), with the Sharpe ratio as the performance statistic:
      1. M is the T x N matrix of per-period returns: N strategy variants (columns) over T periods (rows).
      2. Cut M by rows into S blocks of equal size, in time order (S even).
      3. For each of the C(S, S/2) ways to choose S/2 blocks: the chosen blocks are the in-sample set J, the others
         the out-of-sample set Jc. R_n and Rc_n are column n's Sharpe ratios on J and on Jc.
      4. n* = argmax_n R_n, the variant picked in-sample. Its relative out-of-sample rank is
             w_c = rank of Rc_n* among Rc_1..Rc_N / (N + 1)        (rank 1 = worst; ties share the average rank)
         and its logit lambda_c = ln(w_c / (1 - w_c)): above 0 when it beat the out-of-sample median.
      5. PBO = #{c : lambda_c <= 0} / C(S, S/2), the paper's integral of f(lambda) from -inf to 0.
    Also from the paper: the probability of out-of-sample loss, #{c : Rc_n* < 0} / C(S, S/2), and the performance
    degradation, the least-squares line Rc_n* = intercept + slope * R_n* across the splits (a slope below 0: better
    in-sample went with worse out of sample). Splits where a Sharpe is infinite are left out of that fit.

    returns: T x N, one row per period and one column per variant (a list of rows, a numpy array or a pandas
    DataFrame). With one list of returns per variant, pass list(zip(*lists)). All variants over the same periods.
    n_splits: S, even, from 2 to 16 (C(16, 8) = 12870 splits; more is slow in pure Python and adds little). Each
    block needs at least 2 rows, so T >= 2 * S. When T is not a multiple of S the oldest T mod S rows are left out.
    A variant whose returns never change (one that never trades) has no Sharpe ratio and is refused: leave it out.
    A half whose mean is 0 (within 1e-9 of the variant's standard deviation) gets Sharpe 0; a half in which a variant
    does not change otherwise gets +-inf. Ties for the in-sample best go to the first column.
    """
    rows = _matrix(returns)
    s = _whole(n_splits, "n_splits", 2, "give an even number of blocks from 2 to 16, such as 16")
    if s % 2:
        raise StatsError(f"n_splits is {s}: CSCV cuts the periods into an even number of blocks, half to pick the best "
                         "variant and half to test it; give an even number from 2 to 16, such as 16")
    if s > MAX_SPLITS:
        raise StatsError(f"n_splits is {s}: at most {MAX_SPLITS} (C(16, 8) = 12870 splits already; more takes long in "
                         "pure Python and adds little); give 16 or fewer")
    n = len(rows[0])
    if n < 2:
        raise StatsError(f"returns has {_plural(n, 'column', 'columns')}: PBO compares strategy variants, so give at "
                         "least 2 columns, one per variant. Rows are periods and columns variants: with one list of "
                         "returns per variant, pass list(zip(*lists))")
    total = len(rows)
    if total < 2 * s:
        most = (total // 2) // 2 * 2
        hint = f", or fewer blocks (n_splits={most} works with {total} rows)" if most >= 2 else ""
        raise StatsError(f"returns has {_plural(total, 'row', 'rows')} (periods), too few for {s} blocks of at least 2 "
                         f"rows each: give at least {2 * s} periods{hint}")
    dropped = total % s
    rows = rows[dropped:]
    t, size, half = len(rows), len(rows) // s, len(rows) // s * (s // 2)
    cols = [list(c) for c in zip(*rows)]
    for j, c in enumerate(cols):
        if min(c) == max(c):
            raise StatsError(f"returns column {j} never changes (every return is {c[0]!r}), so it has no Sharpe ratio: "
                             "leave that variant out (one that never trades) and run again")
    centre = [math.fsum(c) / t for c in cols]           # sums are of returns minus the column's mean: less rounding
    spread = [math.fsum((x - m) * (x - m) for x in c) / (t - 1) for c, m in zip(cols, centre)]
    for j, v in enumerate(spread):
        if not v > 0:
            raise StatsError(f"returns column {j} varies too little to measure (variance {v:.3g}): give returns as "
                             "fractions, 0.01 for 1%")
    flat_var = [1e-12 * v for v in spread]              # a half whose variance is below this does not change
    flat_mean = [1e-9 * math.sqrt(v) for v in spread]
    dev = [[x - m for x in c] for c, m in zip(cols, centre)]
    block_s = [[math.fsum(d[k * size:(k + 1) * size]) for d in dev] for k in range(s)]
    block_q = [[math.fsum(x * x for x in d[k * size:(k + 1) * size]) for d in dev] for k in range(s)]
    all_s = [math.fsum(v) for v in zip(*block_s)]
    all_q = [math.fsum(v) for v in zip(*block_q)]

    def sharpes(sums: list[float], squares: list[float]) -> list[float]:
        out = []
        for sj, qj, cj, fv, fm in zip(sums, squares, centre, flat_var, flat_mean):
            m = sj / half
            var = (qj - sj * m) / (half - 1)
            mean = m + cj
            if abs(mean) <= fm:                         # 0, give or take the rounding of the shift
                out.append(0.0)
            elif var > fv:
                out.append(mean / math.sqrt(var))
            else:
                out.append(math.copysign(math.inf, mean))
        return out

    logits, is_sr, oos_sr = [], [], []
    for rest in combinations(range(1, s), s // 2 - 1):  # each split with block 0 in-sample, and its mirror image
        chosen = (0, *rest)
        sums = list(map(math.fsum, zip(*(block_s[k] for k in chosen))))
        squares = list(map(math.fsum, zip(*(block_q[k] for k in chosen))))
        a = sharpes(sums, squares)
        b = sharpes([x - y for x, y in zip(all_s, sums)], [x - y for x, y in zip(all_q, squares)])
        for ins, oos in ((a, b), (b, a)):
            best = max(range(n), key=ins.__getitem__)
            order = sorted(oos)
            v = oos[best]
            w = (bisect.bisect_left(order, v) + 1 + bisect.bisect_right(order, v)) / 2 / (n + 1)
            logits.append(math.log(w / (1 - w)))
            is_sr.append(ins[best])
            oos_sr.append(v)
    splits = len(logits)
    fit = [(x, y) for x, y in zip(is_sr, oos_sr) if math.isfinite(x) and math.isfinite(y)]
    slope = intercept = None
    if len(fit) >= 2:
        mx, my = math.fsum(x for x, _ in fit) / len(fit), math.fsum(y for _, y in fit) / len(fit)
        sxx = math.fsum((x - mx) ** 2 for x, _ in fit)
        if sxx > 0:
            slope = math.fsum((x - mx) * (y - my) for x, y in fit) / sxx
            intercept = my - slope * mx
    return PBOResult(pbo=sum(1 for v in logits if v <= 0) / splits,
                     prob_oos_loss=sum(1 for v in oos_sr if v < 0) / splits, degradation_slope=slope,
                     degradation_intercept=intercept, logits=tuple(logits), is_sharpe=tuple(is_sr),
                     oos_sharpe=tuple(oos_sr), n_strategies=n, n_splits=s, n_combinations=splits, rows_used=t,
                     rows_dropped=dropped)


# ---- false discovery rate: Benjamini & Hochberg (1995) ---------------------------------------------------------------

class FDRResult(NamedTuple):
    rejected: tuple[bool, ...]     # per p-value, in the order given: True is a discovery at this false discovery rate
    adjusted: tuple[float, ...]    # BH-adjusted p-values (q-values): rejected where adjusted <= alpha


def benjamini_hochberg(p_values, alpha=0.05) -> FDRResult:
    """Which of several strategies' p-values are discoveries, keeping the expected share of false ones at most alpha.

    Benjamini & Hochberg (1995): sort the m p-values, p_(1) <= ... <= p_(m); with k the largest i such that
    p_(i) <= i/m * alpha, reject the hypotheses of p_(1)..p_(k) (none when there is no such i). The adjusted p-values
    are q_(i) = min over j >= i of min(1, m/j * p_(j)). For PSR or DSR, a strategy's p-value is 1 - PSR (or 1 - DSR).
    Valid for independent or positively dependent tests, which strategies on the same market usually are.

    p_values: numbers in [0, 1], one per strategy; alpha: the false discovery rate, above 0 and below 1.
    Returns rejected and adjusted in the order the p-values were given; empty for no p-values.
    """
    if isinstance(p_values, (str, bytes)) or not isinstance(p_values, Iterable):
        raise StatsError(f"p_values must be a list of numbers in [0, 1], not {type(p_values).__name__}")
    ps = [_real(p, f"p_values[{i}]") for i, p in enumerate(p_values)]
    for i, p in enumerate(ps):
        if not 0 <= p <= 1:
            raise StatsError(f"p_values[{i}] is {p}: give p-values from 0 to 1 (for PSR or DSR: 1 - PSR)")
    a = _real(alpha, "alpha")
    if not 0 < a < 1:
        raise StatsError(f"alpha is {a}: give the false discovery rate to allow, above 0 and below 1, such as 0.05")
    m = len(ps)
    order = sorted(range(m), key=ps.__getitem__)
    k = max((rank for rank, i in enumerate(order, 1) if ps[i] <= rank / m * a), default=0)
    rejected, adjusted, running = [False] * m, [1.0] * m, 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, ps[i] * m / rank)
        adjusted[i], rejected[i] = running, rank <= k
    return FDRResult(tuple(rejected), tuple(adjusted))


# ---- a loop's tries --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class TriesVerdict:
    """deflate_tries's answer. Sharpe ratios are per period."""
    n_trials: int                          # N, the tries counted
    trials_sharpe_variance: float | None   # V, the sample variance of the tries' Sharpe ratios; None for 1 try
    best_index: int                        # which try had the highest Sharpe (the first one on ties)
    best_sharpe: float
    n_obs: int                             # T of the best try
    skewness: float                        # of the best try's returns
    kurtosis: float                        # plain (3 for normal returns)
    expected_max_sharpe: float             # SR0: what the best of N skill-less tries reaches by luck
    dsr: float                             # the best try's deflated Sharpe ratio; 1 - dsr is its p-value
    confidence: float
    significant: bool                      # dsr >= confidence
    verdict: str                           # one plain line for a report


def _prob(p: float) -> str:
    if p >= 0.99995:
        return "> 0.9999"
    if p < 0.00005:
        return "< 0.0001"
    return f"{p:.2f}" if 0.01 <= p <= 0.99 else f"{p:.4f}"


def deflate_tries(tries, *, n_obs=None, skewness=None, kurtosis=None, confidence=0.95) -> TriesVerdict:
    """The deflated Sharpe ratio of the best of a loop's tries, with the inputs it needs worked out from the tries.

    tries: one entry per try, the baseline included if it was a candidate too - either
      - every try's per-period returns (a list of numbers each; lengths may differ): each try's Sharpe ratio comes
        from its returns, and the best try's T, skewness and kurtosis from its own; leave n_obs, skewness and
        kurtosis out; or
      - every try's PER-PERIOD Sharpe ratio (numbers): then give n_obs, T, the number of returns behind each Sharpe
        (the same for every try), and the best try's skewness and kurtosis if known (default: normal returns, 0
        and 3; kurtosis plain, not excess).
    N is the number of tries, V the sample variance (n - 1) of their Sharpe ratios, and the DSR is
    deflated_sharpe_ratio(best Sharpe, T, n_trials=N, trials_sharpe_variance=V, ...): see there for the formula.
    Counting every try as an independent trial errs on the cautious side when tries are near copies of each other.
    confidence: the DSR needed to call the best try significant, from 0.5 up to (not including) 1.
    The verdict reads like: "best of 212 tries: deflated Sharpe 0.41 (probability the true Sharpe is above 0); not
    significant at 0.95".
    """
    if isinstance(tries, (str, bytes)) or not isinstance(tries, Iterable):
        raise StatsError(f"tries must be a list with one entry per try (its returns or its Sharpe ratio), not "
                         f"{type(tries).__name__}")
    items = list(tries)
    if not items:
        raise StatsError("tries is empty: give one entry per try, its per-period returns or its per-period Sharpe "
                         "ratio")
    c = _confidence(confidence)
    numeric = [isinstance(x, numbers.Real) and not isinstance(x, bool) for x in items]
    if all(numeric):
        if n_obs is None:
            raise StatsError("tries are Sharpe ratios, so give n_obs: T, the number of returns behind each one (the "
                             "same for every try); or give every try's returns instead")
        t = _n_obs(n_obs)
        sharpes = [_real(x, f"tries[{i}]") for i, x in enumerate(items)]
        g3, g4 = _shape(0.0 if skewness is None else skewness, 3.0 if kurtosis is None else kurtosis)
        best = max(range(len(sharpes)), key=sharpes.__getitem__)
    elif not any(numeric):
        if n_obs is not None or skewness is not None or kurtosis is not None:
            raise StatsError("tries are return series, so T, skewness and kurtosis come from the best try's own "
                             "returns: leave out n_obs, skewness and kurtosis")
        stats = [_stats(_returns(x, f"tries[{i}]"), f"tries[{i}]") for i, x in enumerate(items)]
        sharpes = [st.sharpe for st in stats]
        best = max(range(len(sharpes)), key=sharpes.__getitem__)
        t, g3, g4 = stats[best].n_obs, stats[best].skewness, stats[best].kurtosis
    else:
        first = numeric.index(not numeric[0])
        raise StatsError(f"tries mixes Sharpe ratios and return series (tries[0] and tries[{first}]): give every try "
                         "as its returns, or every try as its Sharpe ratio")
    n = len(sharpes)
    var = statistics.variance(sharpes) if n > 1 else None
    sr0 = math.sqrt(var) * _emax(n) if var else 0.0
    dsr = _psr(sharpes[best], sr0, t, g3, g4)
    ok = dsr >= c
    head = f"best of {n} tries: deflated Sharpe" if n > 1 else "1 try (nothing to deflate): probabilistic Sharpe"
    verdict = (f"{head} {_prob(dsr)} (probability the true Sharpe is above 0); "
               f"{'significant' if ok else 'not significant'} at {c:g}")
    return TriesVerdict(n_trials=n, trials_sharpe_variance=var, best_index=best, best_sharpe=sharpes[best], n_obs=t,
                        skewness=g3, kurtosis=g4, expected_max_sharpe=sr0, dsr=dsr, confidence=c, significant=ok,
                        verdict=verdict)
