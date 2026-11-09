import numpy as np
import pandas as pd
from masar.config import ASSETS


def trend_frame(n=120, breakout_at=100, start_day=19000, base=100.0):
    """Flat range then a breakout on day `breakout_at` (last row)."""
    rng = np.random.default_rng(1)
    c = base + rng.normal(0, 0.5, n)
    c[breakout_at:] = base + 10 + np.arange(n - breakout_at)
    h, l = c + 1, c - 1
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"o": o, "h": h, "l": l, "c": c}, index=pd.Index(range(start_day, start_day + n), name="day"))


class FakeExchange:
    def __init__(self, prices):
        self.p = prices

    def public_get_market_tickers(self, params):
        return {"data": [{"instId": f"{a}-USDT", "last": str(px)} for a, px in self.p.items()]}


class FakeMarket:
    def __init__(self, frames, prices, hourly=None):
        self.frames, self.ex, self.hourly = frames, FakeExchange(prices), hourly or {}

    def daily(self, asset, days=400, inst_type="SPOT"):
        return self.frames.get(asset, self.frames["BTC"]).copy()

    def tick(self, inst_id):
        return 0.01

    def inst(self, inst_id):
        return {"tickSz": "0.01", "lotSz": "0.0001", "minSz": "0.0001", "ctVal": "0.01"}

    def hourly_since(self, inst_id, since_ms):
        df = self.hourly.get(inst_id, pd.DataFrame(columns=["ts", "o", "h", "l", "c"]))
        return df[df.ts > since_ms]
