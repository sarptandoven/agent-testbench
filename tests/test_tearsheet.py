"""The tear sheet (trading/tearsheet.py): one page made by testbench from a backtest's standard files. Its numbers are
trading.metrics' numbers, its charts are well-formed inline SVG, the monthly table adds up by hand, it fetches and
runs nothing, a refused file is a plain note, and the HTML report, `testbench ui`, `report --tearsheet` and the live
dashboard all show it."""
import datetime as dt
import html
import http.client
import json
import random
import re
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from agent_testbench import config as C, dashboard, report as R, ui
from agent_testbench.trading import files as F, metrics as M, tearsheet as T
from conftest import cli, write_config

HOSTILE = "<img src=x onerror=alert(1)>"


def backtest(out: Path, *, days: int = 300, seed: int = 3, prices: bool = False, trades: bool = True) -> Path:
    """A deterministic backtest: a random walk of closes on weekdays, a moving-average crossover, 0.1% fees."""
    rng = random.Random(seed)
    t, close, closes = dt.date(2023, 1, 2), 100.0, []
    while len(closes) < days:
        if t.weekday() < 5:
            close *= 1 + rng.gauss(0.0004, 0.012)
            closes.append((t, round(close, 4)))
        t += dt.timedelta(days=1)
    cash, units, fills, bars = 10_000.0, 0.0, [], []
    for i, (day, c) in enumerate(closes):
        fast = sum(x for _, x in closes[max(0, i - 4):i + 1]) / min(5, i + 1)
        slow = sum(x for _, x in closes[max(0, i - 19):i + 1]) / min(20, i + 1)
        want = i >= 20 and fast > slow
        if want and not units:
            q = round(cash * 0.95 / c, 2)
            fee = round(q * c * 0.001, 4)
            cash, units = cash - q * c - fee, q
            fills.append({"time": day, "symbol": "SPY", "side": "buy", "quantity": q, "price": c, "fee": fee})
        elif not want and units:
            fee = round(units * c * 0.001, 4)
            cash += units * c - fee
            fills.append({"time": day, "symbol": "SPY", "side": "sell", "quantity": units, "price": c, "fee": fee})
            units = 0.0
        bars.append({"time": day, "equity": cash + units * c, "position_value": units * c,
                     **({} if prices else {"benchmark": c})})
    out.mkdir(parents=True, exist_ok=True)
    F.write_equity(bars, out / "equity.csv")
    if trades:
        F.write_trades(fills, out / "trades.csv")
    if prices:
        F.write_prices([{"time": d, "symbol": "SPY", "close": c} for d, c in closes], out / "prices.csv")
    return out


def tiles(page: str) -> dict:
    """{key: (data-value, the number as shown)} for every headline tile on a page."""
    found = re.findall(r"data-key=(\w+) data-value='([^']*)'[^>]*><b>[^<]*</b><span>([^<]*)</span>", page)
    return {k: (v, html.unescape(shown)) for k, v, shown in found}


def svgs(page: str) -> list[ET.Element]:
    return [ET.fromstring(s) for s in re.findall(r"<svg\b.*?</svg>", page, re.S)]


def luminance(color: str) -> float:
    def channel(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(int(color[i:i + 2], 16)) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# ---- the numbers, the charts, the table ---------------------------------------------------------------------------

def test_the_numbers_on_the_page_are_trading_metrics_numbers(tmp_path):
    out = backtest(tmp_path / "bt")
    want = M.compute(out / "equity.csv", out / "trades.csv")
    s = T.sheet(out / "equity.csv", out / "trades.csv")
    assert s["ok"], s
    page = T.fragment(s)
    shown = tiles(page)
    keys = [k for group in T.HEADLINE.values() for k, *_ in group]
    assert list(shown) == keys, "a tile per headline number, in order"
    kinds = {k: kind for group in T.HEADLINE.values() for k, _, kind, _ in group}
    for key, (value, text) in shown.items():
        assert value == ("null" if want[key] is None else repr(want[key])), key
        assert text == T.fmt(want[key], kinds[key]).replace("-", "\u2212"), (key, text)
    for key in ("total_return", "sharpe", "max_drawdown", "trades", "win_rate", "profit_factor", "total_fees",
                "exposure", "turnover_per_year", "benchmark_return", "excess_return", "cagr", "sortino", "calmar"):
        assert want[key] is not None, f"the fixture defines {key}"
    for group in T.HEADLINE.values():                    # every number says what it is, on hover and in the list
        for key, label, _, what in group:
            assert f"title='{html.escape(what)}" in page and f"<dt>{label}</dt><dd>{html.escape(what)}</dd>" in page
    caveats = re.search(r"<aside class=ts-caveats>(.*?)</aside>", page, re.S).group(1)
    for note in want["notes"]:
        assert html.escape(note).encode("ascii", "xmlcharrefreplace").decode() in caveats, note
    assert T.NOT_ADVICE in caveats and "risk-free rate as 0% a year" in caveats
    profits = [p for p, _ in M.round_trips(F.read_trades(out / "trades.csv"))[0]]
    assert f"{len(profits):,}: " in page and T.fmt(max(profits), "signed_money") in page
    assert T.fmt(min(profits), "signed_money").replace("-", "&#8722;") in page, "the largest loss"


def test_the_charts_are_well_formed_svg_with_their_series(tmp_path):
    out = backtest(tmp_path / "bt", days=2500)          # more bars than the charts draw points
    s = T.sheet(out / "equity.csv", out / "trades.csv")
    growth, under = svgs(T.fragment(s))
    for svg, height in ((growth, T.GROWTH_HEIGHT), (under, T.DRAWDOWN_HEIGHT)):
        assert svg.get("viewBox") == f"0 0 {T.WIDTH} {height}" and svg.get("role") == "img" and svg.get("aria-label")
        rects = svg.findall("./g[@class='ts-hits']/rect")
        assert 10 <= len(rects) <= T.STRIPS and all(r.find("title") is not None for r in rects)
        for p in svg.iter("path"):
            points = re.findall(r"(-?[\d.]+),(-?[\d.]+)", p.get("d"))
            assert points and len(points) < 4 * (T.WIDTH - T.LEFT - T.RIGHT) // 2 + 3, "thinned to the pixels"
            for x, y in points:
                assert T.LEFT - 0.1 <= float(x) <= T.WIDTH - T.RIGHT + 0.1 and 0 <= float(y) <= height
    lines = {p.get("class") for p in growth.iter("path")}
    assert lines == {"ts-line ts-strategy", "ts-line ts-benchmark"}, "the strategy against buy and hold"
    labels = [t.text for t in growth.iter("text")]
    assert "0%" in labels and any(x.startswith("+") for x in labels) and any(re.fullmatch(r"\d{4}", x or "")
                                                                              for x in labels), labels
    worst = under.find(".//text[@class='ts-label']").text
    assert worst == T.fmt(M.compute(out / "equity.csv")["max_drawdown"], "pct").replace("-", "\u2212")
    assert "(log scale)" not in T.fragment(s)
    assert "(log scale)" in T.fragment({**s, "log": True}) and "(log scale)" not in T.fragment({**s, "log": False})


def test_a_tenfold_growth_is_drawn_on_a_log_scale(tmp_path):
    days = [dt.date(2020, 1, 1) + dt.timedelta(days=7 * i) for i in range(160)]
    F.write_equity([{"time": d, "equity": 100 * 1.02 ** i} for i, d in enumerate(days)], tmp_path / "equity.csv")
    page = T.fragment(T.sheet(tmp_path / "equity.csv"))
    assert "(log scale)" in page
    labels = [t.text for t in svgs(page)[0].iter("text")]
    assert {"0%", "+100%", "+400%"} <= set(labels), labels


def test_the_time_axis_from_minutes_to_decades():
    t0 = dt.datetime(2024, 3, 1, 9, 31, tzinfo=dt.timezone.utc)
    for span, labels in ((dt.timedelta(minutes=50), ["Mar 1 09:40", "09:50", "10:00", "10:10", "10:20"]),
                         (dt.timedelta(days=5), ["Mar 2", "Mar 3", "Mar 4", "Mar 5", "Mar 6"]),
                         (dt.timedelta(days=200), ["Apr 2024", "May", "Jun", "Jul", "Aug", "Sep"]),
                         (dt.timedelta(days=365 * 30), ["2025", "2030", "2035", "2040", "2045", "2050"])):
        ticks = T._time_ticks(t0, t0 + span)
        assert [label for _, label in ticks] == labels, span
        assert all(t0 <= t <= t0 + span and t.tzinfo == t0.tzinfo for t, _ in ticks)


def test_the_monthly_table_of_a_hand_made_series(tmp_path):
    rows = [("2023-12-29", 100.0), ("2024-01-02", 100.0), ("2024-01-31", 110.0), ("2024-02-15", 99.0),
            ("2024-02-29", 104.5), ("2024-04-30", 115.5), ("2025-01-02", 115.5), ("2025-01-31", 103.95)]
    F.write_equity([{"time": t, "equity": v} for t, v in rows], tmp_path / "equity.csv")
    m = T.monthly([dt.datetime.fromisoformat(t) for t, _ in rows], [v for _, v in rows])
    assert m["months"] == pytest.approx({(2023, 12): 0.0, (2024, 1): 0.1, (2024, 2): -0.05,
                                         (2024, 4): 115.5 / 104.5 - 1, (2025, 1): -0.1}), "March has no row"
    assert m["years"] == pytest.approx({2023: 0.0, 2024: 0.155, 2025: -0.1})
    page = T.fragment(T.sheet(tmp_path / "equity.csv"))
    table = re.search(r"<table class=ts-months>(.*?)</table>", page, re.S).group(1)
    row = re.search(r"<tr><th scope=row>2024</th>(.*?)</tr>", table).group(1)
    cells = re.findall(r"<td([^>]*)>([^<]*)</td>", row)
    assert [html.unescape(text) for _, text in cells] == ["+10.0%", "\u22125.0%", "", "+10.5%"] + [""] * 8 + \
        ["+15.5%"], "Jan to Dec, then the year (115.5 / 100 - 1)"
    assert [re.search(r"class='?([\w-]+)", attrs).group(1) for attrs, _ in cells[:4]] == \
        ["ts-u3", "ts-d3", "ts-none", "ts-u3"]
    assert "title='January 2024: +10.00%'" in row and "ts-year ts-u3" in row
    assert re.search(r"<tr><th scope=row>2023</th><td class=ts-none></td>(<td class=ts-none></td>){10}<td class=ts-z "
                     r"title='December 2023: \+0\.00%'>0\.0%</td>", table), "a month that rounds to 0 shows 0.0%"
    assert re.search(r"<tr><th scope=row>2025</th><td class=ts-d3 title='January 2025: &#8722;10\.00%'>"
                     r"&#8722;10\.0%</td>", table)
    assert [T.shade(r) for r in (0.0004, 0.01, 0.03, 0.06, -0.01, -0.03, -0.06)] == \
        ["ts-z", "ts-u1", "ts-u2", "ts-u3", "ts-d1", "ts-d2", "ts-d3"]


def test_the_monthly_colors_keep_text_on_them_readable_in_light_and_dark():
    page_css = T.css()
    for mode, c in T.COLORS.items():
        backgrounds = [c["flat"], *c["up"], *c["down"]]
        for bg in backgrounds:
            ratio = contrast(c["text"], bg)
            assert ratio >= 4.5, f"{mode}: text {c['text']} on {bg} is {ratio:.2f}:1, under WCAG AA's 4.5:1"
            assert bg in page_css and c["text"] in page_css, "the page uses these colours"
        for arm in ("up", "down"):                    # weakest to strongest moves away from the surface
            lum = [luminance(x) for x in c[arm]]
            assert lum == sorted(lum, reverse=(mode == "light")), (mode, arm, lum)
    assert "prefers-color-scheme:dark" in page_css and ":root[data-theme=dark] .ts{" in page_css


def test_holding_times_follow_the_same_first_in_first_out_matching(tmp_path):
    d = dt.datetime(2024, 1, 1)
    fills = F.read_trades([(d, "A", "buy", 10, 100, 1), (d + dt.timedelta(days=2), "A", "buy", 10, 104, 1),
                           (d + dt.timedelta(days=4), "A", "sell", 15, 110, 1.5),
                           (d + dt.timedelta(days=5), "A", "sell", 5, 90, 0.5),
                           (d + dt.timedelta(days=5), "B", "sell", 3, 50, 0),
                           (d + dt.timedelta(days=5, hours=6), "B", "buy", 3, 49, 0)])
    held = T.holding_times(fills)
    assert len(held) == len(M.round_trips(fills)[0]) == 3
    assert held == pytest.approx([(10 * 4 + 5 * 2) / 15 * T.DAY, 3 * T.DAY, 6 * 3600])
    assert [T.duration(x) for x in (held[0], 86400, 6 * 3600, 90, 30)] == \
        ["3.3 days", "1 day", "6 hours", "90 seconds", "30 seconds"]


# ---- what the page never holds, and what it says when it cannot be made ------------------------------------------

def test_the_page_fetches_and_runs_nothing(tmp_path):
    out = backtest(tmp_path / "bt", prices=True)
    sheets = T.for_folder(out)
    assert len(sheets) == 1 and sheets[0][1]["ok"]
    page = T.page(sheets, "Tear sheet", "From a test.")
    lowered = page.lower()
    for bad in ("http://", "https://", "//cdn", "<script", "<link", "<iframe", "<img", "src=", "url(", "@import",
                "javascript:", "onerror", "onload", "xmlns"):
        assert bad not in lowered, f"the page has {bad!r}"
    assert page.isascii(), "everything beyond ASCII as numeric references, so any encoding reads it"
    assert "prices.csv" in page and "Buy and hold" in page


def test_a_refused_equity_csv_is_a_plain_note(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "equity.csv").write_text("time,equity\n2024-01-02,100\n2024-01-03,-5\n2024-01-01,101\n")
    s = T.sheet(bad / "equity.csv", shown={"equity": "out/equity.csv"})
    assert not s["ok"] and s["note"].startswith("out/equity.csv: 2 problems"), s
    page = T.fragment(s, "Strategy: x")
    assert "<p class=ts-note>No tear sheet: out/equity.csv: 2 problems" in page and "-5 is negative" in page
    assert "Traceback" not in page and str(tmp_path) not in page and "<svg" not in page
    (bad / "equity.csv").write_text("time,equity\n2024-01-02,100\n")
    assert "has 1 row" in T.sheet(bad / "equity.csv")["note"]
    assert T.sheet(bad / "missing.csv")["ok"] is False
    (bad / "equity.csv").write_text("date,value\n2024-01-02,100\n2024-01-03,101\n")
    assert not T.standard(bad / "equity.csv") and T.find(bad) == [], "another format: no tear sheet at all"
    assert T.standard(backtest(tmp_path / "ok") / "equity.csv")


def test_a_benchmark_it_cannot_use_is_left_out_with_a_caveat(tmp_path):
    out = backtest(tmp_path / "bt", prices=True)
    F.write_prices([{"time": "2023-01-02", "symbol": s, "close": 10} for s in ("QQQ", "IWM")], out / "prices.csv")
    s = T.sheet(out / "equity.csv", out / "trades.csv", out / "prices.csv", shown={"prices": "prices.csv"})
    assert s["ok"] and s["benchmark"] is None and s["numbers"]["benchmark_return"] is None
    assert any(n.startswith("prices.csv is not used as the benchmark: prices.csv has closes for 2 symbols")
               for n in s["numbers"]["notes"])


def test_without_trades_the_trade_numbers_are_not_shown(tmp_path):
    out = backtest(tmp_path / "bt", trades=False)
    page = T.fragment(T.sheet(out / "equity.csv"))
    shown = tiles(page)
    assert shown["trades"] == ("null", "\u2013") and shown["win_rate"] == ("null", "\u2013"), "a dash: no number"
    assert shown["exposure"][0] != "null", "exposure comes from position_value"
    assert "No trades.csv next to equity.csv" in page and "no trades.csv" in page


# ---- in the reports ------------------------------------------------------------------------------------------------

def _run(root: Path, name: str, steps: list[dict]) -> Path:
    """A finished run's folder with a report.json for these steps (dir, artifacts, ...)."""
    d = root / ".testbench" / "runs" / name
    for s in steps:
        (d / s["dir"]).mkdir(parents=True, exist_ok=True)
        (d / s["dir"] / "log.txt").write_text("ok\n")
    (d / "state.json").write_text(json.dumps({"run_id": name, "plan": "bt", "backend": "local", "status": "passed",
                                              "created": 1.7e9, "started": 1.7e9}))
    (d / "report.json").write_text(json.dumps({
        "run_id": name, "plan": "bt", "status": "passed", "backend": "local", "started": 1.7e9, "minutes": 0.1,
        "cost_usd": 0.0, "skipped": [], "steps": [{"kind": "run", "status": "passed", "minutes": 0.1,
                                                    "expectations": [], **s} for s in steps]}))
    return d


def test_the_reports_show_a_tear_sheet_for_each_step_that_kept_an_equity_csv(tmp_path):
    run = _run(tmp_path, "20260101-000000-bt", [
        {"name": HOSTILE, "dir": "steps/01-a", "artifacts": ["out/equity.csv", "out/trades.csv"]},
        {"name": "refused", "dir": "steps/02-b", "artifacts": ["equity.csv"]},
        {"name": "other format", "dir": "steps/03-c", "artifacts": ["equity.csv"]},
        {"name": "escapes", "dir": "steps/04-d", "artifacts": ["../../../../../outside/equity.csv"]},
        {"name": "too big", "dir": "steps/05-e", "artifacts": ["equity.csv (not copied: 600 MB)"]}])
    backtest(run / "steps/01-a/artifacts/out")
    (run / "steps/02-b/artifacts").mkdir(parents=True)
    (run / "steps/02-b/artifacts/equity.csv").write_text("time,equity\n2024-01-02,100\n2024-01-02,101\n")
    (run / "steps/03-c/artifacts").mkdir(parents=True)
    (run / "steps/03-c/artifacts/equity.csv").write_text("date,nav\n2024-01-02,100\n")
    backtest(tmp_path / "outside")
    rep = R.load(run)
    sheets = T.for_report(rep, run)
    assert [label for label, _ in sheets] == [f"{HOSTILE} (out/equity.csv)", "refused"]
    assert sheets[0][1]["files"][0][0] == "steps/01-a/artifacts/out/equity.csv" and not sheets[1][1]["ok"]

    page = R.render_html(rep, run)
    assert page.count("<section class=ts>") == 2 and page.count("<style>") == 2, "the tear sheet's style once"
    assert html.escape(HOSTILE) in page and HOSTILE not in page
    assert "No tear sheet: steps/02-b/artifacts/equity.csv, line 3, column `time`" in page
    want = M.compute(run / "steps/01-a/artifacts/out/equity.csv", run / "steps/01-a/artifacts/out/trades.csv")
    assert tiles(page)["sharpe"][0] == repr(want["sharpe"])
    for svg in svgs(page):
        assert svg.tag == "svg"

    md = R.render(rep, run)
    assert f"## Strategy: {HOSTILE} (out/equity.csv)" in md and "## Strategy: refused" in md
    assert f"- total return {T.fmt(want['total_return'], 'pct')} (buy and hold " in md
    assert f"Sharpe {want['sharpe']:.2f}," in md and f"{want['trades']} trades" in md
    assert "No tear sheet: steps/02-b/artifacts/equity.csv, line 3" in md and T.NOT_ADVICE in md
    assert "Strategy" not in R.render(rep), "only with the run folder"


def test_a_report_without_an_equity_csv_never_loads_the_trading_pack(tmp_path):
    run = _run(tmp_path, "20260101-000000-bt", [{"name": "s", "dir": "steps/01-a", "artifacts": ["out/x.csv"]}])
    code = ("import sys, json; from pathlib import Path; from agent_testbench import report as R\n"
            f"run = Path({str(run)!r}); rep = R.load(run); R.render(rep, run); R.render_html(rep, run)\n"
            "print(json.dumps(sorted(m for m in sys.modules if m.startswith('agent_testbench.trading'))))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and json.loads(out.stdout) == [], out.stderr + out.stdout


STEP = """\
from pathlib import Path
Path("out").mkdir(exist_ok=True)
for name, text in {files!r}.items():
    Path("out", name).write_text(text)
print("backtest written")
"""


def test_report_html_and_tearsheet_through_the_command_line(tmp_path):
    root = tmp_path / "proj"
    data = backtest(tmp_path / "data", prices=True)
    files = {p.name: p.read_text() for p in data.iterdir()}
    root.mkdir()
    (root / "bt.py").write_text(STEP.format(files=files))
    write_config(root, {"project": "ts", "plans": {
        "bt": {"steps": [{"name": "backtest", "run": "python bt.py", "max_minutes": 2, "artifacts": ["out/*.csv"]}]},
        "plain": {"steps": [{"name": "nothing", "run": "python -c \"print(1)\"", "max_minutes": 1}]}}})
    r = cli(root, "run", "bt", "--wait", "120")
    assert r.returncode == 0, r.stdout + r.stderr
    want = M.compute(data / "equity.csv", data / "trades.csv", data / "prices.csv")
    assert "## Strategy: backtest (out/equity.csv)" in r.stdout and f"Sharpe {want['sharpe']:.2f}" in r.stdout
    run = next((root / ".testbench" / "runs").iterdir())

    r = cli(root, "report", "--format", "html")
    assert r.returncode == 0, r.stderr
    page = (run / "report.html").read_text()
    assert tiles(page)["sharpe"][0] == repr(want["sharpe"]) and tiles(page)["benchmark_return"][0] != "null"

    r = cli(root, "report", run.name, "--tearsheet", "-o", "sheet.html")
    assert r.returncode == 0 and "wrote sheet.html" in r.stdout, r.stdout + r.stderr
    sheet = (root / "sheet.html").read_text()
    assert sheet.startswith("<!doctype html>") and "<script" not in sheet and "https://" not in sheet
    assert f"Tear sheet: bt {run.name}" in sheet and "step backtest (out/equity.csv)" in sheet
    assert {k: v for k, (v, _) in tiles(sheet).items()} == {k: v for k, (v, _) in tiles(page).items()}
    assert "Growth of the account, against buy and hold" in sheet and "Monthly returns" in sheet
    assert "Steps" not in sheet, "the tear sheet alone"
    r = cli(root, "report", "--tearsheet")
    assert r.returncode == 0 and (run / "tearsheet.html").is_file()
    assert cli(root, "verify").returncode == 0, "the tear sheet is no evidence: the run still verifies"

    assert cli(root, "run", "plain", "--wait", "120").returncode == 0
    r = cli(root, "report", "--tearsheet")
    assert r.returncode == 1 and "no step kept an equity.csv" in r.stdout and "artifacts:" in r.stdout


def test_the_static_pages_and_the_dashboard_show_it(tmp_path):
    write_config(tmp_path, {"project": "dash", "plans": {"bt": {"steps": [{"name": "b", "run": "true"}]}}})
    run = _run(tmp_path, "20260101-000000-bt", [{"name": "b", "dir": "steps/01-b", "artifacts": ["equity.csv"]}])
    backtest(run / "steps/01-b/artifacts", trades=False)
    ui.build(tmp_path, C.load(tmp_path))
    assert "<section class=ts>" in (run / "report.html").read_text(), "testbench ui's page of the run"
    (run / "report.html").unlink()
    T_page, _ = T.run_page(R.load(run), run)
    (run / "tearsheet.html").write_text(T_page)

    server = dashboard.make_server(tmp_path, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]

        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            try:
                c.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
                r = c.getresponse()
                return r.status, r.getheader("Content-Type") or "", r.read().decode()
            finally:
                c.close()
        status, kind, body = get(f"/runs/{run.name}/")
        assert status == 200 and "has ended" in body and "<section class=ts>" in body and "data-key=sharpe" in body
        assert body.count("<script") == 1 and "<script src='/dashboard.js' defer></script>" in body, "only its own"
        status, kind, body = get(f"/runs/{run.name}/tearsheet.html")
        assert status == 200 and kind.startswith("text/html") and "data-key=sharpe" in body
        assert get(f"/runs/{run.name}/state.json")[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_the_command_line_of_the_module(tmp_path):
    out = backtest(tmp_path / "bt")
    r = subprocess.run([sys.executable, "-m", "agent_testbench.trading.tearsheet", str(out), "-o",
                        str(tmp_path / "t.html"), "--risk-free", "0.04", "--log"], capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stderr
    page = (tmp_path / "t.html").read_text()
    assert tiles(page)["sharpe"][0] == repr(M.compute(out / "equity.csv", out / "trades.csv", risk_free=0.04)["sharpe"])
    assert "risk-free rate as 4% a year" in page and "(log scale)" in page
    (tmp_path / "empty").mkdir()
    r = subprocess.run([sys.executable, "-m", "agent_testbench.trading.tearsheet", str(tmp_path / "empty")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 2 and "no equity.csv" in r.stderr
