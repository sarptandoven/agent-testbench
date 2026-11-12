# Trading statistics: is that Sharpe ratio real?

An agent that tries 200 strategy variants will find one with a great backtest by luck alone. The statistics in
`agent_testbench.trading.stats` say how much of a result is luck: how likely the true Sharpe ratio is above zero
once the number of tries is counted, how long a backtest has to be, and how often the in-sample winner falls behind
out of sample. They are implemented from the published papers, with the standard library only.

```python
from agent_testbench.trading import stats

s = stats.return_stats(daily_returns)               # per-day Sharpe, T, skewness, kurtosis
stats.probabilistic_sharpe_ratio(**s._asdict())     # P(true Sharpe > 0), one strategy, no search
stats.deflate_tries(returns_of_every_try).verdict
# 'best of 212 tries: deflated Sharpe 0.41 (probability the true Sharpe is above 0); not significant at 0.95'
stats.probability_of_backtest_overfitting(table).pbo   # table: one row per day, one column per variant
```

## Units

- Every Sharpe ratio is **per period**, not annualized: mean over standard deviation of the returns at the
  frequency they were observed. Daily returns give a daily Sharpe. From an annualized Sharpe of data with `q`
  periods a year (252 trading days, 52 weeks, 12 months): divide by `sqrt(q)`. A variance of annualized Sharpe
  ratios is divided by `q`. Passing an annualized Sharpe with daily T makes almost anything look significant.
- The exception is `min_backtest_length`, which takes an annualized Sharpe and answers in years.
- Kurtosis is the plain kurtosis, 3 for normal returns. pandas and scipy report the excess kurtosis (0 for normal
  returns) by default: add 3. A kurtosis below `1 + skewness^2` is refused, so a forgotten 3 is usually caught.
- Returns are excess returns if the Sharpe ratio should be over the risk-free rate: subtract it per period first.

Bad inputs (NaN or inf, fewer than 2 returns, fewer than 1 trial, an odd or too large number of blocks, returns that
never change) raise `StatsError`, a `ValueError` whose message says what to give instead.

## Probabilistic Sharpe ratio (PSR)

**Answers:** given this track record (its length, skewness and fat tails), how likely is the true Sharpe ratio above
a benchmark `SR*`?

```
PSR(SR*) = Z[ (SR - SR*) * sqrt(T - 1) / sqrt(1 - g3*SR + (g4 - 1)/4 * SR^2) ]
```

`Z` is the standard normal CDF, `T` the number of returns, `g3` the skewness, `g4` the kurtosis.
`probabilistic_sharpe_ratio(sharpe, n_obs, benchmark=0, skewness=0, kurtosis=3)`.

From Bailey & Lopez de Prado (2012), [The Sharpe Ratio Efficient Frontier](https://ssrn.com/abstract=1821643),
Journal of Risk 15(2).

**Meaningless when** the Sharpe ratio is the best of several tries (use the DSR), the returns are far from
independent (strong autocorrelation, a regime change inside the sample), or T is a few dozen returns: the formula is
asymptotic.

## Minimum track record length (MinTRL)

**Answers:** how many returns does it take before this Sharpe ratio is above the benchmark with, say, 95%
confidence?

```
MinTRL = 1 + (1 - g3*SR + (g4 - 1)/4 * SR^2) * (Z^-1[c] / (SR - SR*))^2
```

`min_track_record_length(sharpe, benchmark=0, skewness=0, kurtosis=3, confidence=0.95)`, in returns at the Sharpe
ratio's frequency (a daily Sharpe of 0.1 with normal returns: 273 trading days). Infinite when the Sharpe ratio is
not above the benchmark. Same paper as the PSR.

**Meaningless when** the observed Sharpe ratio is itself the best of many tries, or the strategy will not trade the
same way in the future.

## Deflated Sharpe ratio (DSR)

**Answers:** after trying N variants and keeping the best, how likely is its true Sharpe ratio above 0? The best of
N variants with no skill at all reaches a Sharpe ratio of about `SR0` by luck; the DSR is the PSR against `SR0`.

```
SR0 = sqrt(V) * ( (1 - gamma) * Z^-1[1 - 1/N] + gamma * Z^-1[1 - 1/(N e)] )
DSR = PSR(SR0)
```

`V` is the variance of the N trials' Sharpe ratios and `gamma` = 0.5772... the Euler-Mascheroni constant.
`expected_max_sharpe(n_trials, trials_sharpe_variance)` is `SR0`;
`deflated_sharpe_ratio(sharpe, n_obs, n_trials=..., trials_sharpe_variance=..., skewness=0, kurtosis=3)` the DSR, and
`1 - DSR` its p-value. With N = 1 the DSR is the PSR against 0.

`deflate_tries(tries)` works out the inputs from a loop's tries: give every try's per-period returns (or every try's
per-period Sharpe ratio and `n_obs`), and it returns N, V, the best try, its DSR and a one-line verdict. It counts
every try as an independent trial; tries that are near copies of each other are fewer independent trials, so the DSR
it gives errs on the cautious side.

`testbench loop` does this for you. A loop over a strategy keeps every try's returns, reverted tries included, and
each `loop try`, `loop status`, `loop stop` report and loop notification adds the verdict for its best try (and
PBO once 4 or more tries cover the same periods); `--min-deflated-sharpe 0.95` makes the loop's `--target` count
only when the best try also clears that DSR. See [Loops: is the best try luck?](loops.md#is-the-best-try-luck).

From Bailey & Lopez de Prado (2014), [The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting
and Non-Normality](https://ssrn.com/abstract=2460551), Journal of Portfolio Management 40(5). The paper's example
(100 trials, 5 years of daily returns, an annualized Sharpe of 2.5) gives a DSR of 0.90: not significant at 0.95.

**Meaningless when** N leaves out tries (earlier sessions, variants run by hand and dropped), the trials ran on
different data or periods, or V comes from a handful of trials. The `SR0` formula is an approximation of the
expected maximum of N normals: 8% low for N = 2, within 2.5% from N = 5.

## Minimum backtest length (MinBTL)

**Answers:** how many years of data does it take before trying N variants, so that their best is not expected to
reach a given annualized Sharpe ratio by luck alone?

```
MinBTL = ( ((1 - gamma) * Z^-1[1 - 1/N] + gamma * Z^-1[1 - 1/(N e)]) / E[max_N] )^2  <  2 ln(N) / E[max_N]^2
```

`min_backtest_length(n_trials, annual_sharpe=1)`, in years. With 5 years of data, no more than 45 variants before
an annualized Sharpe of 1 is expected from luck.

From Bailey, Borwein, Lopez de Prado & Zhu (2014), [Pseudo-Mathematics and Financial Charlatanism: The Effects of
Backtest Overfitting on Out-of-Sample Performance](https://ssrn.com/abstract=2308659), Notices of the AMS 61(5).

**Meaningless as** a guarantee: a backtest at least this long is necessary, not enough. It assumes normal returns
and independent trials with a true Sharpe ratio of 0.

## Probability of backtest overfitting (PBO)

**Answers:** how often does the variant that did best in-sample do worse than the median variant out of sample?

Combinatorially symmetric cross-validation (CSCV): cut the T x N table of per-period returns (one column per
variant) into S blocks of periods; for each of the C(S, S/2) ways to pick half the blocks, the picked half is
in-sample and the rest out of sample. Find the in-sample best `n*`, its relative out-of-sample rank
`w = rank / (N + 1)` and the logit `lambda = ln(w / (1 - w))`:

```
PBO = share of splits with lambda <= 0
```

`probability_of_backtest_overfitting(returns, n_splits=16)` also gives the logits, the probability of out-of-sample
loss (the in-sample best's out-of-sample Sharpe below 0) and the performance degradation (the slope of out-of-sample
on in-sample Sharpe of the in-sample best across splits). S is even and at most 16 (12870 splits, about a second
for 100 variants over 1000 days); each block needs 2 rows or more.

From Bailey, Borwein, Lopez de Prado & Zhu (2017), [The Probability of Backtest
Overfitting](https://ssrn.com/abstract=2326253), Journal of Computational Finance 20(4).

**Meaningless when** the table leaves out variants that were tried, the blocks are a few rows each, or the variants
are near copies of each other (then the ranking is noise and PBO near 0.5 says nothing). One table's PBO is noisy:
with pure noise it averages 0.5 but one table can give 0.2 or 0.8.

## False discovery rate (Benjamini-Hochberg)

**Answers:** of several strategies, which are discoveries, keeping the expected share of false ones at most alpha?

Sort the m p-values; with k the largest i such that `p_(i) <= i/m * alpha`, the k smallest are discoveries.
`benjamini_hochberg(p_values, alpha=0.05)` gives which are rejected and the adjusted p-values. For PSR or DSR the
p-value is `1 - PSR` or `1 - DSR`.

From Benjamini & Hochberg (1995), [Controlling the False Discovery Rate: A Practical and Powerful Approach to
Multiple Testing](https://doi.org/10.1111/j.2517-6161.1995.tb02031.x), Journal of the Royal Statistical Society B
57(1).

**Meaningless when** the p-values are PSRs of strategies each picked as the best of many tries (deflate them first),
or the strategies are strongly negatively dependent.
