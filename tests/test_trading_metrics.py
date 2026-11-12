"""The trading pack's standard files and testbench's own numbers from them (trading/files.py, trading/metrics.py).

Every metric has a small case worked out by hand (the derivation is in the comment next to the expected number); every
way a file is refused has a test of its message (file, line, column, what to write); hypothesis checks properties
that hold for any equity curve. Derandomized, so CI sees the same examples every time."""
import datetime as dt
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_testbench.trading import files as F, metrics as M

pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, assume, given, settings, strategies as st  # noqa: E402

DAY = dt.timedelta(days=1)
MON = dt.datetime(2024, 1, 1)                          # a Monday
TRADES_HEADER = "time,symbol,side,quantity,price,fee"


def csv_file(path: Path, *lines: str) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def equity_rows(values, start=MON, step=DAY, **columns):
    """Rows for equity.csv, one per value, `step` apart; columns: name -> list of values per row."""
    return [{"time": start + i * step, "equity": v, **{k: c[i] for k, c in columns.items()}}
            for i, v in enumerate(values)]


def weekdays(n, start=MON):
    out, t = [], start
    while len(out) < n:
        if t.weekday() < 5:
            out.append(t)
        t += DAY
    return out


def refused(kind, src) -> str:
    with pytest.raises(F.TradingFileError) as e:
        getattr(F, f"read_{kind}")(src)
    return str(e.value)


# ---- every metric, by hand ---------------------------------------------------------------------------------------

def test_returns_risk_and_drawdown_by_hand():
    # Mon..Fri, no weekend: 252 periods a year. Returns +10%, -10%, +10%, +10%.
    m = M.compute(equity_rows([100, 110, 99, 108.9, 119.79]))
    assert m["periods_per_year"] == 252 and "252" in m["notes"][0] and "--calendar crypto" in m["notes"][0]
    assert m["bars"] == 5 and m["start_equity"] == 100 and m["end_equity"] == 119.79
    assert m["days"] == 4.0 and m["years"] == pytest.approx(4 / 365.25)            # Jan 1 -> Jan 5
    assert m["total_return"] == pytest.approx(0.1979)                              # 119.79 / 100 - 1
    assert m["cagr"] == pytest.approx(1.1979 ** (365.25 / 4) - 1)                  # (last / first) ** (1 / years) - 1
    # mean 0.05; deviations .05 -.15 .05 .05 -> squares sum 0.03, / (4 - 1) = 0.01 -> stdev 0.1
    assert m["volatility"] == pytest.approx(0.1 * math.sqrt(252))                  # 1.5875
    assert m["sharpe"] == pytest.approx(0.05 / 0.1 * math.sqrt(252))               # 7.937
    # downside: min(0, r) = 0, -0.1, 0, 0 -> sqrt(0.01 / 4) = 0.05
    assert m["sortino"] == pytest.approx(0.05 / 0.05 * math.sqrt(252))             # 15.87
    assert m["max_drawdown"] == pytest.approx(-0.1)                                # 99 / 110 - 1
    # the peak is row 1 (110, Jan 2); 108.9 is still below it, 119.79 (row 4, Jan 5) is back above
    assert (m["max_drawdown_bars"], m["max_drawdown_days"], m["max_drawdown_recovered"]) == (3, 3.0, True)
    assert m["longest_drawdown_days"] == 3.0
    assert m["calmar"] == pytest.approx(m["cagr"] / 0.1)                           # cagr / -max_drawdown
    assert m["risk_free"] == 0.0
    assert any("only 5 bars" in n for n in m["notes"]) and any("covers 4 days" in n for n in m["notes"])


def test_risk_free_rate_and_periods_per_year_given():
    # given 4 periods a year: rf per period = 1.04 ** (1 / 4) - 1 = 0.0098534
    m = M.compute(equity_rows([100, 110, 99, 108.9, 119.79]), risk_free=0.04, periods_per_year=4)
    rf = 1.04 ** 0.25 - 1
    assert m["periods_per_year"] == 4 and m["notes"][0] == "periods per year: 4, as given"
    assert m["sharpe"] == pytest.approx((0.05 - rf) / 0.1 * 2)                     # (mean - rf) / stdev * sqrt(4)
    # only r = -0.1 is below rf: downside deviation sqrt((-0.1 - rf) ** 2 / 4)
    down = math.sqrt((-0.1 - rf) ** 2 / 4)
    assert m["sortino"] == pytest.approx((0.05 - rf) / down * 2)
    assert m["volatility"] == pytest.approx(0.1 * 2) and m["risk_free"] == 0.04


def test_drawdown_not_recovered_and_longest_drawdown():
    m = M.compute(equity_rows([100, 120, 90, 110]))
    assert m["max_drawdown"] == pytest.approx(-0.25)                               # 90 / 120 - 1
    # from the peak (row 1) to the last row (3): never back at 120
    assert (m["max_drawdown_bars"], m["max_drawdown_days"], m["max_drawdown_recovered"]) == (2, 2.0, False)
    # two drawdowns: -20% from row 0 back at row 2 (2 days), then -3% from row 2 back at row 6 (4 days)
    m = M.compute(equity_rows([100, 80, 100, 99, 98, 97, 100]))
    assert m["max_drawdown"] == pytest.approx(-0.2)
    assert (m["max_drawdown_days"], m["max_drawdown_bars"], m["longest_drawdown_days"]) == (2.0, 2, 4.0)
    # two drawdowns of -25% that differ only by rounding (found by hypothesis): the first one, at any scale
    for scale in (1, 9):
        m = M.compute(equity_rows([v * scale for v in (1, 0.75, 1.125, 1.125001125, 0.84375084375)]))
        assert (m["max_drawdown_days"], m["max_drawdown_recovered"]) == (2.0, True)


def test_round_trips_fifo_fees_exposure_and_turnover_by_hand(tmp_path):
    eq = csv_file(tmp_path / "equity.csv", "time,equity",
                  *[f"{(MON + i * DAY).date()}T00:00:00,{v}" for i, v in enumerate([1000, 1010, 990, 1000, 1005, 995,
                                                                                     1000])])
    tr = csv_file(tmp_path / "trades.csv", TRADES_HEADER + ",order_id",
                  "2024-01-01T12:00:00,A,buy,10,100,1,o1",     # long 10 A at 100, fee 0.1 a unit
                  "2024-01-02T12:00:00,A,sell,4,110,0.4,o2",   # 4 x (110 - 100) - 4 x (0.1 + 0.1) = 39.2 on 400
                  "2024-01-03T12:00:00,A,SELL,6,90,0.6,o3",    # 6 x (90 - 100) - 6 x 0.2 = -61.2 on 600
                  "2024-01-04T12:00:00,B,sell,5,50,0.5,o4",    # short 5 B at 50
                  "2024-01-05T12:00:00,B,buy,8,45,0.8,o5",     # covers 5: 5 x (50 - 45) - 5 x 0.2 = 24 on 250; long 3
                  "2024-01-06T12:00:00,B,Sell,3,47,0.3,o6")    # 3 x (47 - 45) - 3 x 0.2 = 5.4 on 135
    m = M.compute(eq, tr)
    assert m["periods_per_year"] == 365                                            # Mon..Sun: 2 of 7 on a weekend
    assert (m["fills"], m["trades"], m["open_positions"]) == (6, 4, 0)
    assert m["win_rate"] == 0.75                                                   # 3 of 4 made money
    assert m["profit_factor"] == pytest.approx((39.2 + 24 + 5.4) / 61.2)           # 1.1209
    assert m["average_trade"] == pytest.approx((39.2 - 61.2 + 24 + 5.4) / 4)       # 1.85
    assert m["average_trade_return"] == pytest.approx((39.2 / 400 - 61.2 / 600 + 24 / 250 + 5.4 / 135) / 4)  # 0.033
    assert m["total_fees"] == pytest.approx(3.6) and m["costs_modeled"] is True    # 1 + .4 + .6 + .5 + .8 + .3
    assert m["traded_value"] == pytest.approx(2731)                                # 1000+440+540+250+360+141
    assert m["turnover"] == pytest.approx(2.731)                                   # 2731 / mean equity 1000
    assert m["turnover_per_year"] == pytest.approx(2.731 / (6 / 365.25))
    # open at the start of bars 1, 2 (A), 4 (B short), 5 (B long 3); flat at 0 and 3: 4 of the 6 bars
    assert m["exposure"] == pytest.approx(4 / 6)
    assert any("only 4 round trips" in n for n in m["notes"])
    assert not any("charged no trading costs" in n for n in m["notes"])


def test_round_trips_partial_lots_and_open_position_at_the_end():
    fills = F.read_trades([("2024-01-01T10:00:00", "X", "buy", 5, 10, 0),       # lot 1: 5 at 10
                           ("2024-01-01T11:00:00", "X", "buy", 5, 12, 0),       # lot 2: 5 at 12
                           ("2024-01-01T12:00:00", "X", "sell", 7, 13, 0),      # 5 x 3 + 2 x 1 = 17 on 50 + 24
                           ("2024-01-01T13:00:00", "Y", "buy", 1, 100, 0)])     # still open at the end
    trips, still_open = M.round_trips(fills)
    assert trips == [(pytest.approx(17.0), pytest.approx(74.0))] and still_open == ["X", "Y"]  # 3 of X left


def test_exposure_from_position_value_wins_over_trades():
    rows = equity_rows([100, 100, 100, 100, 100], position_value=[0, 50, -20, 0, 30])
    # open at the start of bars 1 and 2 of the 4 (the last row starts no bar)
    assert M.compute(rows)["exposure"] == 0.5
    assert M.compute(rows, [])["exposure"] == 0.5                                  # no fills, but position_value rules


def test_no_costs_and_slippage_count_as_costs():
    eq = equity_rows([100, 101, 102])
    free = [{"time": MON, "symbol": "X", "side": "buy", "quantity": 1, "price": 100, "fee": 0}]
    m = M.compute(eq, free)
    assert m["costs_modeled"] is False and m["total_fees"] == 0
    assert any("charged no trading costs" in n for n in m["notes"])
    assert any("1 symbol still had a position open at the end (X)" in n for n in m["notes"])
    assert M.compute(eq, [{**free[0], "slippage": 0.05}])["costs_modeled"] is True
    m = M.compute(eq, [])                                                          # a trades file with no fills
    assert (m["fills"], m["trades"], m["costs_modeled"], m["win_rate"]) == (0, 0, None, None)
    assert any("never traded" in n for n in m["notes"]) and m["exposure"] == 0.0


def test_profit_factor_without_losses_is_null_with_a_note():
    eq = equity_rows([100, 101, 102])
    fills = [(MON, "X", "buy", 1, 100, 0.1), (MON + DAY, "X", "sell", 1, 102, 0.1)]   # 2 - 0.2 = 1.8 won
    m = M.compute(eq, fills)
    assert m["win_rate"] == 1.0 and m["profit_factor"] is None and m["average_trade"] == pytest.approx(1.8)
    assert any("profit_factor is null: none of the 1 round trips lost money" in n for n in m["notes"])


def test_benchmark_column():
    # benchmark returns +10%, -10%, 0, +10%: mean 0.025; deviations .075 -.125 -.025 .075 -> squares 0.0275 / 3
    m = M.compute(equity_rows([100, 110, 99, 108.9, 119.79], benchmark=[100, 110, 99, 99, 108.9]))
    assert m["benchmark_return"] == pytest.approx(0.089)                           # 108.9 / 100 - 1
    assert m["benchmark_sharpe"] == pytest.approx(0.025 / math.sqrt(0.0275 / 3) * math.sqrt(252))   # 4.145
    assert m["excess_return"] == pytest.approx(0.1979 - 0.089)                     # 0.1089
    assert not any("no benchmark" in n for n in m["notes"])


def test_benchmark_from_prices_takes_the_last_close_at_or_before_each_row(tmp_path):
    prices = csv_file(tmp_path / "prices.csv", "time,symbol,close",
                      "2023-12-31T16:00:00,SPY,100", "2023-12-31T16:00:00,QQQ,7",
                      "2024-01-02T16:00:00,SPY,110", "2024-01-04T16:00:00,SPY,121", "2024-01-04T17:00:00,QQQ,9")
    eq = equity_rows([100, 101, 102, 103, 104])                                    # Jan 1..5 at midnight
    # SPY at each row: 100 (Dec 31), 100, 110 (Jan 2 16:00), 110, 121 (Jan 4 16:00) -> 121 / 100 - 1
    m = M.compute(eq, prices=prices, symbol="SPY")
    assert m["benchmark_return"] == pytest.approx(0.21) and m["excess_return"] == pytest.approx(0.04 - 0.21)
    with pytest.raises(F.TradingFileError, match=r"prices.csv has closes for 2 symbols \(QQQ, SPY\); say which is the "
                                                 r"benchmark: symbol='SPY' \(--symbol SPY\)"):
        M.compute(eq, prices=prices)
    with pytest.raises(F.TradingFileError, match="has no closes for 'IWM'; it has QQQ, SPY"):
        M.compute(eq, prices=prices, symbol="IWM")
    one = [(MON, "SPY", "buy", 1, 100, 0)]                                         # the one symbol traded is the one
    assert M.compute(eq, one, prices)["benchmark_return"] == pytest.approx(0.21)
    # equity.csv's own benchmark column is not used when prices.csv is given (a note says so); QQQ: 7 -> 9
    m = M.compute(equity_rows([100, 101, 102, 103, 104], benchmark=[1, 1, 1, 1, 1]), prices=prices, symbol="QQQ")
    assert m["benchmark_return"] == pytest.approx(9 / 7 - 1) and any("column is not used" in n for n in m["notes"])
    late = M.compute(equity_rows([100, 101], start=dt.datetime(2023, 12, 30)), prices=prices, symbol="SPY")
    assert late["benchmark_return"] is None and any("is after" in n for n in late["notes"])


def test_benchmark_missing_is_a_note():
    m = M.compute(equity_rows([100, 101, 102]))
    assert m["benchmark_return"] is m["benchmark_sharpe"] is m["excess_return"] is None
    assert any(n.startswith("no benchmark: add a benchmark column") for n in m["notes"])


def test_flat_equity_has_no_ratios_and_says_why():
    m = M.compute(equity_rows([100, 100, 100, 100]))
    assert m["sharpe"] is None and m["sortino"] is None and m["calmar"] is None and m["volatility"] == 0
    assert m["max_drawdown"] == 0 and m["total_return"] == 0 and m["cagr"] == 0
    assert any("sharpe is null" in n for n in m["notes"]) and any("calmar is null" in n for n in m["notes"])
    m = M.compute(equity_rows([100, 101]))                                         # one return: no stdev
    assert m["sharpe"] is None and m["volatility"] is None and any("at least 3 rows" in n for n in m["notes"])


def test_keys_are_stable_and_plain():
    m = M.compute(equity_rows([100, 110, 99, 108.9]), [(MON, "X", "buy", 1, 100, 1)])
    assert list(m) == [*M.KEYS, "notes"]
    assert all(v is None or type(v) in (int, float, bool) for k, v in m.items() if k != "notes")
    assert all(isinstance(n, str) for n in m["notes"])
    json.dumps(m, allow_nan=False)                                                 # no NaN or infinity anywhere


# ---- periods per year --------------------------------------------------------------------------------------------

def test_daily_stocks_versus_crypto_annualization():
    rets = [0.01, -0.02, 0.015, 0.005, -0.01, 0.02, 0.0, 0.01, -0.005, 0.012]
    values = [100.0]
    for r in rets:
        values.append(values[-1] * (1 + r))
    stocks = [{"time": t, "equity": v} for t, v in zip(weekdays(len(values)), values)]
    crypto = equity_rows(values)                                                   # every day, weekends too
    s, c = M.compute(stocks), M.compute(crypto)
    assert (s["periods_per_year"], c["periods_per_year"]) == (252, 365)
    assert "crypto" in c["notes"][0] and "--calendar stocks" in c["notes"][0]
    assert c["sharpe"] / s["sharpe"] == pytest.approx(math.sqrt(365 / 252))       # same returns, longer year
    assert c["volatility"] / s["volatility"] == pytest.approx(math.sqrt(365 / 252))
    assert M.compute(crypto, calendar="stocks")["periods_per_year"] == 252
    assert M.compute(stocks, calendar="crypto")["sharpe"] == pytest.approx(c["sharpe"])
    assert M.compute(stocks, periods_per_year=365)["sharpe"] == pytest.approx(c["sharpe"])


@pytest.mark.parametrize("step, per_year", [(dt.timedelta(weeks=1), 52), (dt.timedelta(days=14), 26),
                                            (dt.timedelta(days=30), 12), (dt.timedelta(days=91), 4),
                                            (dt.timedelta(days=182), 2), (dt.timedelta(days=365), 1)])
def test_spacing_sets_periods_per_year(step, per_year):
    assert M.periods_per_year([MON + i * step for i in range(6)])[0] == per_year


def test_intraday_bars():
    hourly = [MON + i * dt.timedelta(hours=1) for i in range(24 * 10)]             # around the clock, ten days
    assert M.periods_per_year(hourly)[0] == pytest.approx(365 * 24)
    session = []                                                                   # 9:30 to 16:00, 5-minute bars
    for day in weekdays(5):
        start = day.replace(hour=9, minute=30)
        session += [start + i * dt.timedelta(minutes=5) for i in range(78)]
    ppy, how = M.periods_per_year(session)
    assert ppy == 252 * 78 and "78 a day" in how
    ppy, how = M.periods_per_year(session[:100])                                   # under 3 dates: say so
    assert "pass periods_per_year" in how


def test_settings_are_refused_with_what_to_write():
    rows = equity_rows([100, 101, 102])
    for bad in (math.nan, math.inf, -1.5, True, "0.04"):
        with pytest.raises(ValueError, match="risk_free is .*as a fraction, e.g. 0.04"):
            M.compute(rows, risk_free=bad)
    for bad in (0, -252, math.nan, math.inf, True, "252"):
        with pytest.raises(ValueError, match="periods_per_year is .*above 0, e.g. 252"):
            M.compute(rows, periods_per_year=bad)
    with pytest.raises(ValueError, match="calendar is 'weekly'; use auto, stocks"):
        M.compute(rows, calendar="weekly")
    with pytest.raises(F.TradingFileError, match="has 1 row; the numbers need at least 2"):
        M.compute(rows[:1])


# ---- refused files -----------------------------------------------------------------------------------------------

def test_missing_columns_name_the_file_line_and_what_is_needed(tmp_path):
    msg = refused("trades", csv_file(tmp_path / "trades.csv", "time,symbol,side,quantity,price",
                                     "2024-01-01,X,buy,1,10"))
    assert "trades.csv, line 1 (the header): no `fee` column (write 0 on rows without a fee)" in msg
    assert "needs time, symbol, side, quantity, price, fee" in msg and "found: time, symbol" in msg
    msg = refused("equity", csv_file(tmp_path / "equity.csv", ",equity", "2024-01-01,100"))
    assert "no `time` column" in msg and "index_label='time'" in msg               # pandas' unnamed index
    msg = refused("equity", csv_file(tmp_path / "e3.csv", "Day,equity", "0,10000"))    # a beginner's own file
    assert "no `time` column. If `day` holds the times, name that column time, with ISO 8601 times" in msg, msg
    assert "found: day, equity" in msg
    msg = refused("equity", csv_file(tmp_path / "e2.csv", "Time , EQUITY,equity", "2024-01-01,1,2"))
    assert "column `equity` appears more than once" in msg


@pytest.mark.parametrize("cell, says", [
    ("01/02/2024", "'01/02/2024' is not an ISO 8601 time; write year-month-day first"),
    ("2024-13-01", "'2024-13-01' is not a real date and time"),
    ("", "is empty; write an ISO 8601 time"),
    ("1704153600", "looks like a Unix timestamp in seconds"),
    ("1704153600000", "looks like a Unix timestamp in milliseconds; write ISO 8601 instead"),
])
def test_unreadable_times(tmp_path, cell, says):
    msg = refused("equity", csv_file(tmp_path / "equity.csv", "time,equity", "2024-01-01,100", f"{cell},101"))
    assert msg.startswith(f"{tmp_path / 'equity.csv'}, line 3, column `time`: ") and says in msg


def test_times_that_do_not_increase(tmp_path):
    msg = refused("equity", csv_file(tmp_path / "equity.csv", "time,equity", "2024-01-01,100", "2024-01-02,101",
                                     "2024-01-02,102"))
    assert "line 4, column `time`: '2024-01-02' is not after line 3's '2024-01-02'" in msg
    assert "one row per bar with times increasing" in msg
    msg = refused("trades", csv_file(tmp_path / "trades.csv", TRADES_HEADER, "2024-01-02,X,buy,1,10,0",
                                     "2024-01-02,X,sell,1,11,0", "2024-01-01,X,buy,1,10,0"))
    assert "line 4, column `time`: '2024-01-01' is before line 3's '2024-01-02'; fills are in time order" in msg
    msg = refused("prices", csv_file(tmp_path / "prices.csv", "time,symbol,close", "2024-01-02,A,1", "2024-01-01,B,1",
                                     "2024-01-02,A,2"))
    assert "line 4, column `time`: '2024-01-02' is not after line 2's '2024-01-02' for A" in msg


@pytest.mark.parametrize("column, cell, says", [
    ("price", "nan", "is NaN (not a number)"),
    ("price", "NaN", "is NaN (not a number)"),
    ("fee", "inf", "'inf' is infinite"),
    ("price", "-inf", "'-inf' is infinite"),
    ("price", "abc", "'abc' is not a number"),
    ("price", "1,234.5", "'1,234.5' is not a number (no thousands separators"),
    ("price", "$12", "'$12' is not a number (no currency sign"),
    ("price", "0", "0 is not above 0; a fill price is above 0"),
    ("fee", "-0.5", "-0.5 is negative; a fee is a cost"),
    ("fee", "", "is empty; write a number (0 when there was no fee)"),
    ("quantity", "-3", "-3 is negative; write the size without a sign and put the direction in side"),
    ("side", "long", "'long' is not a side; write buy or sell (opening a short is a sell"),
    ("side", "", "is empty; write buy or sell"),
    ("symbol", " ", "is empty; write the instrument's symbol"),
])
def test_bad_values_in_trades(tmp_path, column, cell, says):
    row = {"time": "2024-01-01", "symbol": "X", "side": "buy", "quantity": "1", "price": "10", "fee": "0", column: cell}
    path = csv_file(tmp_path / "trades.csv", TRADES_HEADER, ",".join(f'"{v}"' for v in row.values()))
    msg = refused("trades", path)
    assert msg.startswith(f"{path}, line 2, column `{column}`: ") and says in msg


@pytest.mark.parametrize("column, cell, says", [
    ("equity", "-5", "-5 is negative; equity is the account's whole value"),
    ("equity", "0", "0 is not above 0; equity is"),
    ("benchmark", "0", "0 is not above 0; a benchmark is a price"),
    ("cash", "nan", "is NaN"),
    ("position_value", "", "is empty; write a number"),
])
def test_bad_values_in_equity(tmp_path, column, cell, says):
    good = {"time": "2024-01-01", "equity": "100", "cash": "-50", "position_value": "-10", "benchmark": "1"}
    path = csv_file(tmp_path / "equity.csv", ",".join(good), ",".join(good.values()),
                    ",".join({**good, "time": "2024-01-02", column: cell}.values()))
    msg = refused("equity", path)
    assert msg.startswith(f"{path}, line 3, column `{column}`: ") and says in msg


def test_mixed_time_zones(tmp_path):
    msg = refused("equity", csv_file(tmp_path / "equity.csv", "time,equity", "2024-01-01T00:00:00Z,100",
                                     "2024-01-02T00:00:00,101"))
    assert ("line 3, column `time`: '2024-01-02T00:00:00' has no time zone but line 2's '2024-01-01T00:00:00Z' has "
            "one; write every time in the file the same way") in msg
    # offsets may differ (summer time); a zone on every row is one convention
    bars = F.read_equity(csv_file(tmp_path / "ok.csv", "time,equity", "2024-03-30T00:00:00+01:00,1",
                                  "2024-03-31T00:00:00+02:00,2", "2024-04-01 00:00:00.5 Z,3"))
    assert [b.time.utcoffset().total_seconds() for b in bars] == [3600, 7200, 0]
    eq = csv_file(tmp_path / "eq.csv", "time,equity", "2024-01-01T00:00:00Z,100", "2024-01-02T00:00:00Z,101")
    tr = csv_file(tmp_path / "tr.csv", TRADES_HEADER, "2024-01-01T10:00:00,X,buy,1,10,0")
    with pytest.raises(F.TradingFileError) as e:
        M.compute(eq, tr)
    assert (f"{tr} writes times without a time zone (2024-01-01T10:00:00) but {eq} with "
            "(2024-01-01T00:00:00+00:00); write the times of all the files the same way") in str(e.value)


def test_rows_with_the_wrong_number_of_values_and_empty_files(tmp_path):
    msg = refused("equity", csv_file(tmp_path / "equity.csv", "time,equity", "2024-01-01,100,7", "2024-01-02"))
    assert "line 2: 3 values but the header has 2 columns" in msg and "line 3: 1 value but" in msg
    assert F.read_equity(csv_file(tmp_path / "comma.csv", "time,equity", "2024-01-01,100,", "", "2024-01-02,101,"))
    assert refused("trades", csv_file(tmp_path / "trades.csv", "")).endswith(
        "trades.csv is empty; its first line is the header: time,symbol,side,quantity,price,fee")
    assert refused("equity", csv_file(tmp_path / "e.csv", "time,equity")).endswith(
        "e.csv has no rows after the header; write one row per bar")
    assert F.read_trades(csv_file(tmp_path / "none.csv", TRADES_HEADER)) == []     # no fills: a strategy that waited
    assert "no such file" in refused("equity", tmp_path / "missing.csv")
    assert "is a folder" in refused("equity", tmp_path)
    (tmp_path / "latin.csv").write_bytes(b"time,equity\n2024-01-01,100\xe9\n")
    assert "is not UTF-8 text" in refused("equity", tmp_path / "latin.csv")


def test_many_problems_are_listed_at_once_up_to_a_limit(tmp_path):
    rows = [f"2024-01-{d:02d},X,buy,1,-1,0" for d in range(1, 16)]                 # 15 negative prices
    with pytest.raises(F.TradingFileError) as e:
        F.read_trades(csv_file(tmp_path / "trades.csv", TRADES_HEADER, *rows))
    msg = str(e.value)
    assert msg.startswith(f"{tmp_path / 'trades.csv'}: 15 problems\n  line 2, column `price`: -1 is negative")
    assert msg.count("\n  line ") == F.MAX_PROBLEMS and msg.endswith("... and 5 more")
    assert len(e.value.problems) == 15


def test_columns_any_case_extra_columns_ignored_and_optional_columns(tmp_path):
    fills = F.read_trades(csv_file(tmp_path / "trades.csv",
                                   "﻿Time, Symbol ,SIDE,quantity,price,fee,note,slippage,strategy,order_id",
                                   '2024-01-01T09:30:00.123456789,"BRK.B",Buy,1.5,400.25,0.4,"a, b",0.02,sma,7'))
    assert fills == [F.Fill(dt.datetime(2024, 1, 1, 9, 30, 0, 123456), "BRK.B", "buy", 1.5, 400.25, 0.4, "7", "sma",
                            0.02)]
    assert fills[0].signed_quantity == 1.5


# ---- other sources and the writers -------------------------------------------------------------------------------

def test_writers_round_trip_and_refuse_bad_rows(tmp_path):
    rows = [{"time": MON, "symbol": "X", "side": "buy", "quantity": 2, "price": 10.5, "fee": 0.1, "order_id": 1},
            {"time": MON + DAY, "symbol": "X", "side": "sell", "quantity": 2, "price": 11, "fee": 0.1, "extra": "?"}]
    path = F.write_trades(rows, tmp_path / "out" / "trades.csv")
    assert path.read_text().splitlines() == ["time,symbol,side,quantity,price,fee,order_id",
                                             "2024-01-01T00:00:00,X,buy,2.0,10.5,0.1,1",
                                             "2024-01-02T00:00:00,X,sell,2.0,11.0,0.1,"]
    assert F.read_trades(path) == F.read_trades(rows)
    assert F.read_trades(F.read_trades(path)) == F.read_trades(path)               # records are checked again
    eq = F.write_equity([("2024-01-01", 100), ("2024-01-02", 101.5)], tmp_path / "equity.csv")
    assert eq.read_text() == "time,equity\n2024-01-01T00:00:00,100.0\n2024-01-02T00:00:00,101.5\n"
    assert F.write_trades([], tmp_path / "none.csv").read_text() == TRADES_HEADER + "\n"
    pr = F.write_prices([(MON, "SPY", 470.1)], tmp_path / "prices.csv")
    assert F.read_prices(pr) == [F.Close(MON, "SPY", 470.1)]
    with pytest.raises(F.TradingFileError, match=r"the rows for .*bad.csv, row 2, column `price`: -1 is negative"):
        F.write_trades([rows[0], {**rows[1], "price": -1}], tmp_path / "bad.csv")
    assert not (tmp_path / "bad.csv").exists()
    with pytest.raises(TypeError, match="takes the rows to write and then the path"):
        F.write_equity(str(tmp_path / "equity.csv"), tmp_path / "x.csv")
    with pytest.raises(F.TradingFileError, match="a row as a tuple has at most 5, in this order: time, equity"):
        F.read_equity([(MON, 1, 2, 3, 4, 5)])


def test_dataframes_and_series(tmp_path):
    pd = pytest.importorskip("pandas")
    index = pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC")
    bars = F.read_equity(pd.Series([100.0, 101.0, 99.0], index=index))
    assert [b.equity for b in bars] == [100, 101, 99]
    assert bars[0].time == dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    frame = pd.DataFrame({"equity": [100.0, 101.0, 99.0], "benchmark": [1.0, 1.1, 1.2]}, index=index)
    assert M.compute(frame)["benchmark_return"] == pytest.approx(0.2)
    trades = pd.DataFrame({"time": index[:2], "symbol": "X", "side": ["Buy", "Sell"], "quantity": [1.0, 1.0],
                           "price": [10.0, 11.0], "fee": [0.0, 0.0]})
    assert M.compute(frame, trades)["trades"] == 1
    with pytest.raises(F.TradingFileError) as e:
        F.read_equity(pd.DataFrame({"equity": [100.0, math.nan]}, index=index[:2]))
    assert str(e.value).startswith("the DataFrame given for equity.csv, row 2 (iloc 1), column `equity`: is NaN")
    with pytest.raises(F.TradingFileError, match="a Series can only be equity"):
        F.read_trades(pd.Series([1.0], index=index[:1]))
    F.write_equity(frame, tmp_path / "equity.csv")
    assert (tmp_path / "equity.csv").read_text().splitlines()[1] == "2024-01-01T00:00:00+00:00,100.0,1.0"


# ---- command line and imports ------------------------------------------------------------------------------------

def test_command_line_prints_json_and_refuses_with_the_message(tmp_path):
    eq = F.write_equity(equity_rows([100, 110, 99, 108.9, 119.79]), tmp_path / "equity.csv")
    tr = F.write_trades([(MON, "SPY", "buy", 1, 100, 0.5), (MON + 3 * DAY, "SPY", "sell", 1, 109, 0.5)],
                        tmp_path / "trades.csv")
    pr = F.write_prices([(MON, "SPY", 100), (MON + 4 * DAY, "SPY", 105)], tmp_path / "prices.csv")
    out = tmp_path / "m" / "metrics.json"
    run = subprocess.run([sys.executable, "-m", "agent_testbench.trading.metrics", str(eq), str(tr), "--benchmark",
                          str(pr), "--risk-free", "0.02", "--out", str(out)], capture_output=True, text=True,
                         timeout=120)
    assert run.returncode == 0, run.stderr
    m = json.loads(run.stdout)
    assert m == json.loads(out.read_text()) and list(m) == [*M.KEYS, "notes"]
    assert m["trades"] == 1 and m["benchmark_return"] == pytest.approx(0.05) and m["risk_free"] == 0.02
    assert m["average_trade"] == pytest.approx(8.0)                                # 109 - 100 - 0.5 - 0.5
    bad = csv_file(tmp_path / "bad.csv", "time,equity", "2024-01-01,100", "2024-01-02,nan")
    run = subprocess.run([sys.executable, "-m", "agent_testbench.trading.metrics", str(bad)], capture_output=True,
                         text=True, timeout=120)
    assert run.returncode == 2 and run.stdout == ""
    assert run.stderr.startswith(f"error: {bad}, line 3, column `equity`: is NaN")
    run = subprocess.run([sys.executable, "-m", "agent_testbench.trading.metrics", str(eq), "--periods-per-year",
                          "nan"], capture_output=True, text=True, timeout=120)
    assert run.returncode == 2 and "periods_per_year is nan" in run.stderr


BACKTEST = """\
from agent_testbench.trading.files import write_equity, write_trades
write_equity([("2024-01-0%d" % d, v) for d, v in zip(range(1, 6), (100, 110, 99, 108.9, 119.79))], "out/equity.csv")
write_trades([("2024-01-01", "X", "buy", 1, 100, 0.5), ("2024-01-04", "X", "sell", 1, 108, 0.5)], "out/trades.csv")
"""


def test_a_plan_step_computes_the_numbers_and_json_checks_read_them(tmp_path):
    from conftest import cli, write_config
    root = tmp_path / "strategy"
    root.mkdir()
    (root / "backtest.py").write_text(BACKTEST)
    metrics = "python -m agent_testbench.trading.metrics out/equity.csv out/trades.csv --out out/metrics.json"
    step = {"name": "metrics", "run": metrics, "max_minutes": 2}
    write_config(root, {"project": "strategy", "plans": {
        "good": {"steps": [{"name": "backtest", "run": "python backtest.py", "max_minutes": 2},
                           {**step, "expect": {"json": {"out/metrics.json": {
                               "sharpe": ">= 1", "max_drawdown": ">= -0.2", "trades": "== 1",
                               "costs_modeled": True}}}}]},
        "strict": {"steps": [{"name": "backtest", "run": "python backtest.py", "max_minutes": 2},
                             {**step, "expect": {"json": {"out/metrics.json": {"profit_factor": ">= 1.5",
                                                                                 "trades": ">= 30"}}}}]}}})
    good = cli(root, "run", "good", "--wait", "120")
    assert good.returncode == 0, good.stdout + good.stderr
    strict = cli(root, "run", "strict", "--wait", "120")
    assert strict.returncode == 1
    report = strict.stdout + "".join(p.read_text() for p in (root / ".testbench" / "runs").glob("*/report.md"))
    assert "trades >= 30: 1 (wanted >= 30" in report                              # a number that misses its rule
    assert "profit_factor >= 1.5: None cannot be compared" in report             # null (no losing trade) never passes


def test_importing_never_loads_numpy_or_pandas(tmp_path):
    code = ("import sys, agent_testbench, agent_testbench.trading.metrics as m, agent_testbench.trading.files\n"
            "m.compute([('2024-01-01', 100), ('2024-01-02', 101), ('2024-01-03', 99)],"
            " [('2024-01-01', 'X', 'buy', 1, 10, 0)])\n"
            "loaded = sorted({'numpy', 'pandas'} & set(sys.modules))\n"
            "assert not loaded, loaded\n")
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=tmp_path,
                         env={**os.environ, "PYTHONWARNINGS": "ignore"})
    assert run.returncode == 0, run.stderr


# ---- properties --------------------------------------------------------------------------------------------------

def fast(n: int):
    return settings(max_examples=n * int(os.environ.get("TESTBENCH_FUZZ_SCALE", "1")), derandomize=True,
                    database=None, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# returns of 0 or at least 1e-6 either way: a rise of a few ulps can round away when the curve is scaled
step_returns = st.lists(st.one_of(st.just(0.0), st.floats(1e-6, 0.5), st.floats(-0.5, -1e-6)), min_size=2, max_size=60)


def curve(rets, start=100.0):
    values = [start]
    for r in rets:
        values.append(values[-1] * (1 + r))
    return values


@fast(150)
@given(st.lists(st.floats(1e-3, 1e9, allow_nan=False), min_size=2, max_size=60))
def test_max_drawdown_is_between_minus_one_and_zero(values):
    m = M.compute(equity_rows(values))
    assert -1 <= m["max_drawdown"] <= 0
    assert m["max_drawdown_days"] <= m["longest_drawdown_days"] <= m["days"]
    json.dumps(m, allow_nan=False)


@fast(150)
@given(step_returns, st.floats(1.0, 1e6), st.floats(1e-3, 1e3))
def test_scaling_equity_changes_no_ratio(rets, start, scale):
    values = curve(rets, start)
    a, b = M.compute(equity_rows(values)), M.compute(equity_rows([v * scale for v in values]))
    for key in ("total_return", "cagr", "volatility", "sharpe", "sortino", "max_drawdown", "calmar",
                "max_drawdown_days", "longest_drawdown_days", "periods_per_year"):
        if a[key] is None:
            assert b[key] is None, key
        else:
            assert b[key] == pytest.approx(a[key], rel=1e-6, abs=1e-6), key     # abs: noise around a mean of 0


@fast(150)
@given(st.lists(st.floats(1e-6, 0.5, allow_nan=False), min_size=1, max_size=60))
def test_a_curve_that_only_rises_has_no_drawdown(rets):
    m = M.compute(equity_rows(curve(rets)))
    assert m["max_drawdown"] == 0 and m["max_drawdown_days"] == 0 and m["longest_drawdown_days"] == 0
    assert m["max_drawdown_recovered"] is True and m["calmar"] is None


@fast(150)
@given(step_returns)
def test_sharpe_has_the_sign_of_the_mean_return(rets):
    values = curve(rets)
    m = M.compute(equity_rows(values))
    actual = [b / a - 1 for a, b in zip(values, values[1:])]
    mean = math.fsum(actual) / len(actual)
    assume(m["sharpe"] is not None and abs(mean) > 1e-12)
    assert (m["sharpe"] > 0) == (mean > 0)
    if m["sortino"] is not None:
        assert (m["sortino"] > 0) == (mean > 0)


cells = st.one_of(st.text(max_size=8), st.sampled_from(
    ["", "nan", "inf", "-1", "0", "1e308", "1e-320", "buy", "SELL", "X", "2024-01-01", "2024-02-30", "2024-01-01T25:00",
     "2024-01-01T00:00:00Z", "2024-01-01T00:00:00+24:00", "2024-01-01T00:00:00-05:30", "1704153600", "\"", ",", "\n"]))


@fast(300)
@given(st.sampled_from(["trades", "equity", "prices"]), st.booleans(),
       st.lists(st.lists(cells, min_size=1, max_size=7), max_size=6))
def test_any_text_is_read_or_refused_with_a_message(tmp_path_factory, kind, with_header, rows):
    path = tmp_path_factory.mktemp("fuzz") / F.FILE[kind]
    header = [",".join(F.REQUIRED[kind] + F.OPTIONAL[kind][:1])] if with_header else []
    path.write_text("\n".join(header + [",".join(r) for r in rows]), encoding="utf-8", errors="surrogatepass")
    try:
        records = getattr(F, f"read_{kind}")(path)
    except F.TradingFileError as e:
        assert str(e) and str(path) in str(e)
    else:
        assert all(r.time is not None for r in records)


huge = st.floats(1e-300, 1e300)


@fast(200)
@given(st.lists(huge, min_size=2, max_size=20),
       st.lists(st.tuples(st.sampled_from("AB"), st.booleans(), huge, huge, st.floats(0, 1e300)), max_size=10),
       st.one_of(st.just(0.0), st.floats(-0.99, 1e6)), st.one_of(st.none(), st.floats(1e-6, 1e9)))
def test_any_valid_files_give_numbers_or_nulls_never_a_traceback(values, orders, risk_free, per_year):
    fills = [(MON + i * dt.timedelta(hours=1), s, "buy" if buy else "sell", q, p, fee)
             for i, (s, buy, q, p, fee) in enumerate(orders)]
    m = M.compute(equity_rows(values), fills, risk_free=risk_free, periods_per_year=per_year)
    assert list(m) == [*M.KEYS, "notes"]
    json.dumps(m, allow_nan=False)
    assert m["max_drawdown"] is None or -1 <= m["max_drawdown"] <= 0


@fast(100)
@given(st.lists(st.tuples(st.sampled_from("AB"), st.integers(1, 20), st.floats(1, 100), st.floats(0, 1)),
                min_size=1, max_size=30))
def test_round_trips_add_up_to_the_cash_when_everything_is_closed(orders):
    """Whatever the order of buys and sells, once every position is closed the round trips' profits are the cash the
    fills made, less every fee: FIFO only decides how that total is split between round trips."""
    fills, position, t = [], {"A": 0, "B": 0}, MON
    for symbol, size, price, fee in orders:
        side = "buy" if size % 2 else "sell"
        fills.append((t, symbol, side, size, price, fee))
        position[symbol] += size if side == "buy" else -size
        t += dt.timedelta(minutes=1)
    for symbol, q in position.items():                                             # close what is open
        if q:
            fills.append((t, symbol, "sell" if q > 0 else "buy", abs(q), 50.0, 0.0))
    parsed = F.read_trades(fills)
    trips, still_open = M.round_trips(parsed)
    cash = math.fsum((-1 if f.side == "buy" else 1) * f.quantity * f.price - f.fee for f in parsed)
    assert still_open == [] and math.fsum(p for p, _ in trips) == pytest.approx(cash, abs=1e-6)
    assert all(c > 0 for _, c in trips)
