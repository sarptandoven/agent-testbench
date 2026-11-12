"""The trading pack: a strategy never grades itself. Any backtesting framework writes the standard files
(files.py: trades.csv, equity.csv, prices.csv), testbench computes the numbers from them (metrics.py), stats.py
says whether the best of many tries is better than luck (Deflated Sharpe, PBO), and tearsheet.py draws the numbers,
the growth against buy and hold, the drawdowns and the monthly returns as one page. docs/trading.md has the
formats, the definitions and snippets for vectorbt, backtrader, NautilusTrader, LEAN, freqtrade and plain pandas;
docs/trading-stats.md the statistics."""
