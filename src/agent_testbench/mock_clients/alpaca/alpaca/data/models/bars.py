"""alpaca.data.models.bars of the mock alpaca package: Bar and BarSet as in alpaca-py 0.45.0."""
import _testbench_mock as core

__all__ = ["Bar", "BarSet"]


class Bar(core.Obj):
    _required = ("symbol", "timestamp", "open", "high", "low", "close", "volume")
    _optional = ("trade_count", "vwap")


class BarSet(core.Obj):
    """Bars by symbol: barset["AAPL"] is a list of Bar, oldest first; .df a pandas DataFrame indexed by symbol and
    timestamp (pandas must be installed where the step runs)."""
    _optional = ("data",)

    def __getitem__(self, symbol):
        if symbol not in self.data:
            raise KeyError(f"No key {symbol} was found.")
        return self.data[symbol]

    def __contains__(self, key):
        return key in self.data

    def dict(self, **kwargs) -> dict:
        return {symbol: [b.model_dump() for b in bars] for symbol, bars in self.data.items()}

    @property
    def df(self):
        try:
            import pandas as pd
        except ImportError:
            raise ImportError("BarSet.df needs pandas, which is not installed where the step runs "
                              "(pip install pandas)") from None
        frame = pd.DataFrame([b.model_dump() for bars in self.data.values() for b in bars])
        if {"symbol", "timestamp"} <= set(frame.columns):
            frame = frame.set_index(["symbol", "timestamp"])
        frame.dropna(axis=1, how="all", inplace=True)
        return frame
