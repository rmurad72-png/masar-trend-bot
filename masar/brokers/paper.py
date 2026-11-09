"""Virtual wallet: simulates fills at live OKX prices with slippage and fees, and checks stops
against confirmed 1H candles (same path rule as the backtest: gap through stop -> fill at open)."""
import time
from ..config import COSTS
from .base import inst_id


class PaperBroker:
    account = "paper"

    def __init__(self, store, market, start_equity=10000.0):
        self.store, self.market = store, market
        if self.store.get("paper_cash") is None:
            self.store.set("paper_cash", start_equity)
            self.store.set("paper_start", start_equity)

    # ---------- balances ----------
    @property
    def cash(self):
        return float(self.store.get("paper_cash"))

    def _add_cash(self, x):
        self.store.set("paper_cash", self.cash + x)

    def equity(self, prices):
        eq = self.cash
        for p in self.store.positions(self.account):
            px = prices.get(p["asset"], p["entry_px"])
            if p["side"] == 1:
                eq += p["qty"] * px
            else:
                eq += p["qty"] * (p["entry_px"] - px)
        return eq

    def free_cash(self, prices):
        """Cash not locked as short margin."""
        locked = sum(p["qty"] * p["entry_px"] / 3 for p in self.store.positions(self.account) if p["side"] == -1)
        return self.cash - locked

    @staticmethod
    def _fee(side):
        return COSTS.spot_fee if side == 1 else COSTS.swap_fee

    # ---------- orders ----------
    def open(self, asset, side, qty, ref_px, stop, clid):
        px = ref_px * (1 + side * COSTS.paper_slip)
        fee = qty * px * self._fee(side)
        if side == 1:
            self._add_cash(-(qty * px + fee))
        else:
            self._add_cash(-fee)
        return dict(px=px, qty=qty, fee=fee, ts=int(time.time() * 1000), algo_id="paper")

    def close(self, pos, ref_px, clid):
        side = pos["side"]
        px = ref_px * (1 - side * COSTS.paper_slip)
        return self._settle(pos, px)

    def _settle(self, pos, px):
        side, qty = pos["side"], pos["qty"]
        fee = qty * px * self._fee(side)
        if side == 1:
            self._add_cash(qty * px - fee)
        else:
            self._add_cash(qty * (pos["entry_px"] - px) - fee)
        return dict(px=px, qty=qty, fee=fee, ts=int(time.time() * 1000))

    def verify_stop(self, pos):
        return True

    def check_stop(self, pos):
        """Returns a fill if a confirmed 1H candle since the last check reached the stop."""
        since = max(pos["checked_ts"], pos["entry_ts"])
        bars = self.market.hourly_since(inst_id(pos["asset"], 1), since)   # spot candles for both sides
        side, stop = pos["side"], pos["stop_px"]
        for b in bars.itertuples():
            gap = (side == 1 and b.o <= stop) or (side == -1 and b.o >= stop)
            hit = (side == 1 and b.l <= stop) or (side == -1 and b.h >= stop)
            if gap or hit:
                raw = b.o if gap else stop
                fill = self._settle(pos, raw * (1 - side * COSTS.paper_slip))
                fill["ts"] = int(b.ts)
                return fill, int(b.ts)
        last = int(bars.ts.iloc[-1]) if len(bars) else since
        return None, last
