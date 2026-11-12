"""The trading pack: a strategy never grades itself. Any backtesting framework writes the standard files
(files.py: trades.csv, equity.csv, prices.csv), testbench computes the numbers from them (metrics.py), and
stats.py says whether the best of many tries is better than luck (Deflated Sharpe, PBO). docs/trading.md has the
formats, the definitions and snippets for vectorbt, backtrader, NautilusTrader, LEAN, freqtrade and plain pandas;
docs/trading-stats.md the statistics."""
