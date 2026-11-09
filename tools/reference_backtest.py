"""Reference backtest engine (copied from the research backtest, trend.py + rma). Used only by tools/parity.py."""
from dataclasses import dataclass
import numpy as np
import pandas as pd

def rma(x, n):
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = np.nanmean(x[:n])
    a = 1.0 / n
    for i in range(n, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out



@dataclass
class TP:
    mode: str = "trend"       # trend | fvg
    entryN: int = 20
    exitN: int = 10
    atrN: int = 20
    stopATR: float = 2.0
    fvgDailyStop: bool = False
    minGap: float = 0.05
    retestWait: int = 48
    allowLong: bool = True
    allowShort: bool = True
    risk: float = 1.0
    commission: float = 0.05
    slipTicks: int = 2
    trailATR: float = 0.0     # >0: chandelier stop = best close since entry - k x daily ATR (ratchets)
    addR: float = 0.0         # >0: one add-on unit when daily close >= entry + addR x initial risk


def daily_features(df, p: TP):
    day = (df["ts"] // 86_400_000).astype(int)
    d = df.groupby(day).agg(o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"))
    pc = d["c"].shift(1)
    tr = np.maximum(d["h"] - d["l"], np.maximum((d["h"] - pc).abs(), (d["l"] - pc).abs()))
    tr.iloc[0] = d["h"].iloc[0] - d["l"].iloc[0]
    d["atr"] = rma(tr.to_numpy(float), p.atrN)
    d["hhE"] = d["h"].rolling(p.entryN).max().shift(1)
    d["llE"] = d["l"].rolling(p.entryN).min().shift(1)
    d["hhX"] = d["h"].rolling(p.exitN).max().shift(1)
    d["llX"] = d["l"].rolling(p.exitN).min().shift(1)
    # regime evaluated at each day's close
    reg = np.zeros(len(d), int)
    r = 0
    for k, (c, he, le, hx, lx) in enumerate(zip(d["c"], d["hhE"], d["llE"], d["hhX"], d["llX"])):
        if np.isnan(he):
            reg[k] = 0; continue
        if c > he: r = 1
        elif c < le: r = -1
        elif r == 1 and c < lx: r = 0
        elif r == -1 and c > hx: r = 0
        reg[k] = r
    d["reg"] = reg
    return day, d


def run_trend(df, pair, p: TP, tick, start_ts, gate=None):
    """gate: optional dict day -> required direction (BTC regime); entry only if gate[day] == dir."""
    O, H, L, C = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    TS = df["ts"].to_numpy()
    N = len(df)
    day, d = daily_features(df, p)
    dayv = day.to_numpy()
    last_of_day = np.r_[dayv[1:] != dayv[:-1], True]
    # 1H ATR for FVG mode
    pc = np.r_[np.nan, C[:-1]]
    tr = np.nanmax(np.vstack([H - L, np.abs(H - pc), np.abs(L - pc)]), axis=0); tr[0] = H[0] - L[0]
    atr1 = rma(tr, 14)

    slip = p.slipTicks * tick
    comm = p.commission / 100
    rt = lambda x: round(x / tick) * tick

    equity = 10000.0
    pos = 0; qty = 0.0; entry = 0.0; stop = np.nan; riskCash = 0.0; pnl = 0.0; ets = 0; epx = 0.0; eru = 0.0
    pending = None          # ("open", dir, stop_dist or stop_price) | ("close",) | ("add", dir)
    best = 0.0; add = None  # add-on leg: dict(qty, entry, stop, risk, ets, epx, eru, pnl)
    regime = 0              # regime known as of last completed day
    gap = None              # active FVG: dict(top, bot, first_ext, bar)
    trades = []

    def close_at(price, i, reason="stop"):
        nonlocal pos, equity, pnl
        g = pos * qty * (price - entry) - comm * qty * price
        equity += g; pnl += g
        trades.append(dict(pair=pair, dir=pos, entry=pd.Timestamp(ets, unit="ms", tz="UTC"),
                           exit=pd.Timestamp(TS[i], unit="ms", tz="UTC"), r=pnl / riskCash, pnl=pnl,
                           entry_ts=int(ets), exit_ts=int(TS[i]), entry_px=epx, risk_u=eru, reason=reason))
        pos = 0

    def close_add(price, i):
        nonlocal add, equity
        g = pos * add["qty"] * (price - add["entry"]) - comm * add["qty"] * price
        equity += g; tot = add["pnl"] + g
        trades.append(dict(pair=pair, dir=pos, entry=pd.Timestamp(add["ets"], unit="ms", tz="UTC"),
                           exit=pd.Timestamp(TS[i], unit="ms", tz="UTC"), r=tot / add["risk"], pnl=tot,
                           entry_ts=int(add["ets"]), exit_ts=int(TS[i]), entry_px=add["entry"], risk_u=add["eru"], addon=True))
        add = None

    for i in range(N):
        o, h, l, c = O[i], H[i], L[i], C[i]

        # ---- fills at open ----
        if pending is not None:
            if pending[0] == "close" and pos != 0:
                if add is not None: close_add(o - pos * slip, i)
                close_at(o - pos * slip, i, "channel")
            elif pending[0] == "add" and pos != 0 and add is None:
                f2 = o + pos * slip; st2 = entry; rk2 = pos * (f2 - st2)
                if rk2 > tick:
                    q2 = equity * p.risk / 100 / rk2
                    q2 = min(q2, equity * 5 / f2)
                    add = dict(qty=q2, entry=f2, stop=st2, risk=q2 * rk2, ets=TS[i], eru=rk2, pnl=-comm * q2 * f2)
                    equity -= comm * q2 * f2
            elif pending[0] == "open" and pos == 0:
                _, dr, stp, rpct = pending
                fill = o + dr * slip
                rk = abs(fill - stp)
                if rk > tick and (dr * (fill - stp)) > 0:
                    qty = equity * rpct / 100 / rk
                    qty = min(qty, equity * 5 / fill)
                    pos = dr; entry = fill; stop = stp; riskCash = qty * rk
                    pnl = -comm * qty * fill; equity -= comm * qty * fill; ets = TS[i]; epx = fill; eru = rk; best = fill
            pending = None

        # ---- intrabar stop ----
        if add is not None and pos != 0:
            if (pos == 1 and o <= add["stop"]) or (pos == -1 and o >= add["stop"]):
                close_add(o - pos * slip, i)
            elif (pos == 1 and l <= add["stop"]) or (pos == -1 and h >= add["stop"]):
                close_add(add["stop"] - pos * slip, i)
        if pos != 0:
            if (pos == 1 and o <= stop) or (pos == -1 and o >= stop):
                if add is not None: close_add(o - pos * slip, i)
                close_at(o - pos * slip, i)
            elif (pos == 1 and l <= stop) or (pos == -1 and h >= stop):
                if add is not None: close_add(stop - pos * slip, i)
                close_at(stop - pos * slip, i)

        tradable = TS[i] >= start_ts

        # ---- 1H FVG logic (mode fvg) ----
        if p.mode == "fvg" and not np.isnan(atr1[i]) and i >= 2:
            a = atr1[i]
            if regime == 1:
                if L[i] > H[i - 2] and L[i] - H[i - 2] >= max(p.minGap * a, tick):
                    gap = dict(top=L[i], bot=H[i - 2], ext=L[i - 2], bar=i, dir=1)
            elif regime == -1:
                if H[i] < L[i - 2] and L[i - 2] - H[i] >= max(p.minGap * a, tick):
                    gap = dict(top=L[i - 2], bot=H[i], ext=H[i - 2], bar=i, dir=-1)
            if gap is not None:
                if gap["dir"] != regime or i - gap["bar"] > p.retestWait or \
                   (gap["dir"] == 1 and c < gap["bot"]) or (gap["dir"] == -1 and c > gap["top"]):
                    gap = None
            if gap is not None and pos == 0 and pending is None and i > gap["bar"] and tradable:
                dr = gap["dir"]
                touches = l <= gap["top"] and h >= gap["bot"]
                ok = touches and ((dr == 1 and c > o and c > gap["top"]) or (dr == -1 and c < o and c < gap["bot"]))
                if ok and ((dr == 1 and p.allowLong) or (dr == -1 and p.allowShort)):
                    if p.fvgDailyStop:
                        datr_now = d["atr"].iloc[d.index.get_loc(dayv[i]) - 1]
                        stp = rt(c - dr * p.stopATR * datr_now); okr = True
                    else:
                        stp = rt(gap["ext"] - dr * 0.15 * a); okr = abs(c - stp) <= 3 * a
                    rk = abs(c - stp)
                    if tick < rk and okr:
                        pending = ("open", dr, stp, p.risk)
                    gap = None

        # ---- daily close logic ----
        if last_of_day[i]:
            k = d.index.get_loc(dayv[i])
            new_reg = int(d["reg"].iloc[k])
            datr = d["atr"].iloc[k]
            if pos != 0 and p.trailATR > 0 and not np.isnan(datr):
                best = max(best, c) if pos == 1 else min(best, c)
                ns = rt(best - pos * p.trailATR * datr)
                stop = max(stop, ns) if pos == 1 else min(stop, ns)
            if pos != 0 and new_reg != pos and pending is None:
                pending = ("close",)
            elif pos != 0 and p.addR > 0 and add is None and pending is None and pos * (c - entry) >= p.addR * eru:
                pending = ("add", pos)
            if p.mode == "trend" and pos == 0 and pending is None and tradable and not np.isnan(datr):
                if new_reg != 0 and new_reg != regime:     # fresh breakout
                    dr = new_reg
                    if ((dr == 1 and p.allowLong) or (dr == -1 and p.allowShort)) and \
                            (gate is None or gate.get(int(dayv[i]), 0) == dr):
                        pending = ("open", dr, rt(c - dr * p.stopATR * datr), p.risk)
            if new_reg != regime:
                gap = None
            regime = new_reg

    return trades
