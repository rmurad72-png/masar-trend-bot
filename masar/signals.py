"""Daily Donchian regime + entry/exit decisions.

Ported line-for-line from the backtest (trend.py `daily_features` / `run_trend`), so the bot and the
backtest produce the same signals on the same candles (checked by tools/parity.py).

Regime per day, evaluated at that day's UTC close:
  r = 1   when close > highest high of previous entry_n days
  r = -1  when close < lowest low  of previous entry_n days
  r = 0   when long regime and close < lowest low of previous exit_n days (mirror for short)
  otherwise r keeps yesterday's value.
"""
from dataclasses import dataclass
import numpy as np
import pandas as pd


def rma(x, n):
    """Wilder moving average (TradingView ta.rma): SMA seed, then alpha = 1/n."""
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = np.nanmean(x[:n])
    a = 1.0 / n
    for i in range(n, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def regime_series(d: pd.DataFrame, entry_n: int, exit_n: int) -> np.ndarray:
    hhE = d["h"].rolling(entry_n).max().shift(1).to_numpy()
    llE = d["l"].rolling(entry_n).min().shift(1).to_numpy()
    hhX = d["h"].rolling(exit_n).max().shift(1).to_numpy()
    llX = d["l"].rolling(exit_n).min().shift(1).to_numpy()
    c = d["c"].to_numpy()
    reg = np.zeros(len(d), int)
    r = 0
    for k in range(len(d)):
        if np.isnan(hhE[k]):
            reg[k] = 0
            continue
        if c[k] > hhE[k]:
            r = 1
        elif c[k] < llE[k]:
            r = -1
        elif r == 1 and c[k] < llX[k]:
            r = 0
        elif r == -1 and c[k] > hhX[k]:
            r = 0
        reg[k] = r
    return reg


def features(d: pd.DataFrame, entry_n=55, exit_n=10, atr_n=20) -> pd.DataFrame:
    """d: daily candles indexed by UTC day number (int, days since epoch) with columns o,h,l,c,
    only CONFIRMED (closed) days. Returns d plus atr, reg, exit levels."""
    d = d.copy()
    pc = d["c"].shift(1)
    tr = np.maximum(d["h"] - d["l"], np.maximum((d["h"] - pc).abs(), (d["l"] - pc).abs()))
    tr.iloc[0] = d["h"].iloc[0] - d["l"].iloc[0]
    d["atr"] = rma(tr.to_numpy(float), atr_n)
    d["reg"] = regime_series(d, entry_n, exit_n)
    d["hhE"] = d["h"].rolling(entry_n).max().shift(1)     # breakout level used today
    d["llX"] = d["l"].rolling(exit_n).min().shift(1)      # long exit level used today
    d["hhX"] = d["h"].rolling(exit_n).max().shift(1)      # short exit level used today
    return d


@dataclass
class Decision:
    asset: str
    action: str            # "enter" | "exit"
    side: int              # 1 long, -1 short
    close: float = 0.0
    stop: float = 0.0
    atr: float = 0.0
    reason: str = ""


def round_to_tick(x, tick):
    return round(x / tick) * tick


def decide(asset: str, f: pd.DataFrame, open_side: int, btc_reg_today: int, shorts_allowed: bool,
           shortable: set, stop_atr: float, tick: float):
    """Decision for one asset after the latest confirmed daily close (last row of f).

    open_side: side of the bot's open position in this asset (0 if flat).
    Returns (decision or None, info) where info explains a skipped breakout."""
    if len(f) < 2:
        return None, "history"
    today, prev = f.iloc[-1], f.iloc[-2]
    new_reg, old_reg = int(today["reg"]), int(prev["reg"])
    c = float(today["c"])
    # exit first: regime no longer matches the open position
    if open_side != 0:
        if new_reg != open_side:
            return Decision(asset, "exit", open_side, close=c, reason="channel"), ""
        return None, ""
    # fresh breakout only
    if new_reg == 0 or new_reg == old_reg or np.isnan(today["atr"]):
        return None, ""
    side = new_reg
    if side == -1 and not (shorts_allowed and asset in shortable):
        return None, "short_not_allowed"
    if asset != "BTC" and btc_reg_today != side:
        return None, "btc_filter"
    stop = round_to_tick(c - side * stop_atr * float(today["atr"]), tick)
    if side * (c - stop) <= tick:
        return None, "stop_too_close"
    return Decision(asset, "enter", side, close=c, stop=stop, atr=float(today["atr"]), reason="breakout"), ""


def btc_regime(btc_daily: pd.DataFrame, entry_n=55, exit_n=20) -> pd.Series:
    return pd.Series(regime_series(btc_daily, entry_n, exit_n), index=btc_daily.index)
