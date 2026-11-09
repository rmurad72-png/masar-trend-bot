"""OKX market data and instrument rules (public endpoints, no key needed)."""
import os
import time
import logging
import ccxt
import pandas as pd

log = logging.getLogger(__name__)
DAY_MS = 86_400_000


def make_okx(**cfg):
    """ccxt OKX client. Honors HTTPS_PROXY / REQUESTS_CA_BUNDLE when present (ccxt ignores them)."""
    ex = ccxt.okx({"enableRateLimit": True, "headers": {"User-Agent": "masar-trend-bot/0.1"}, **cfg})
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    if proxy:
        ex.https_proxy = proxy
    ca = os.getenv("REQUESTS_CA_BUNDLE")
    if ca:
        ex.session.verify = ca
    return ex


class Market:
    def __init__(self, exchange=None):
        self.ex = exchange or make_okx()
        self._inst = {}

    # ---------- instruments ----------
    def load_instruments(self):
        for kind in ("SPOT", "SWAP"):
            for x in self.ex.public_get_public_instruments({"instType": kind})["data"]:
                self._inst[x["instId"]] = x

    def inst(self, inst_id):
        if not self._inst:
            self.load_instruments()
        return self._inst[inst_id]

    def tick(self, inst_id):
        return float(self.inst(inst_id)["tickSz"])

    # ---------- candles ----------
    def _candles(self, inst_id, bar, limit_rows, after=None):
        rows = {}
        cursor = after
        while len(rows) < limit_rows:
            params = {"instId": inst_id, "bar": bar, "limit": "100"}
            if cursor:
                params["after"] = str(cursor)
            for attempt in range(5):
                try:
                    data = self.ex.public_get_market_history_candles(params)["data"]
                    break
                except Exception as e:          # network hiccup: retry with backoff
                    log.warning("candles %s retry %s: %s", inst_id, attempt, e)
                    time.sleep(1 + attempt)
            else:
                raise RuntimeError(f"candles failed for {inst_id}")
            if not data:
                break
            for c in data:
                rows[int(c[0])] = c
            cursor = int(data[-1][0])
        return rows

    def daily(self, asset, days=400, inst_type="SPOT") -> pd.DataFrame:
        """Confirmed UTC daily candles (bar=1Dutc) indexed by day number."""
        inst_id = f"{asset}-USDT" if inst_type == "SPOT" else f"{asset}-USDT-SWAP"
        rows = self._candles(inst_id, "1Dutc", days)
        recs = [(int(k) // DAY_MS, float(c[1]), float(c[2]), float(c[3]), float(c[4]))
                for k, c in sorted(rows.items()) if c[8] == "1"]
        d = pd.DataFrame(recs, columns=["day", "o", "h", "l", "c"]).set_index("day")
        return d

    def hourly_since(self, inst_id, since_ms) -> pd.DataFrame:
        """Confirmed 1H candles with ts > since_ms (for paper stop checks)."""
        rows = {}
        cursor = None
        while True:
            params = {"instId": inst_id, "bar": "1H", "limit": "100"}
            if cursor:
                params["after"] = str(cursor)
            data = self.ex.public_get_market_history_candles(params)["data"]
            if not data:
                break
            for c in data:
                if int(c[0]) > since_ms and c[8] == "1":
                    rows[int(c[0])] = c
            cursor = int(data[-1][0])
            if cursor <= since_ms:
                break
        recs = [(k, float(c[1]), float(c[2]), float(c[3]), float(c[4])) for k, c in sorted(rows.items())]
        return pd.DataFrame(recs, columns=["ts", "o", "h", "l", "c"])

    def last(self, inst_id) -> float:
        return float(self.ex.public_get_market_ticker({"instId": inst_id})["data"][0]["last"])
