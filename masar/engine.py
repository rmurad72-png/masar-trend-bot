"""Daily cycle and hourly checks for every enabled account (paper and/or live)."""
import time
import logging
from datetime import datetime, timezone
from .config import ASSETS, SHORTABLE, STRATEGY, RISK, COSTS
from .signals import features, btc_regime, decide
from .risk import size_position, cap_ok, drawdown
from .brokers.base import BrokerError, make_clid

log = logging.getLogger(__name__)
DAY_MS = 86_400_000


def utc_day(ts_ms=None):
    return int((ts_ms if ts_ms is not None else time.time() * 1000) // DAY_MS)


def day_str(day):
    return datetime.fromtimestamp(day * 86400, tz=timezone.utc).strftime("%Y%m%d")


class Engine:
    def __init__(self, settings, store, market, brokers):
        self.s, self.store, self.market, self.brokers = settings, store, market, brokers
        self.feat, self.btc_reg = {}, None

    # ---------- data ----------
    def load_features(self):
        self.feat = {}
        for a in ASSETS:
            d = self.market.daily(a, STRATEGY.history_days)
            if len(d) < STRATEGY.entry_n + 2:
                log.warning("not enough history for %s (%d days)", a, len(d))
                continue
            self.feat[a] = features(d, STRATEGY.entry_n, STRATEGY.exit_n, STRATEGY.atr_n)
        self.btc_reg = btc_regime(self.market.daily("BTC", STRATEGY.history_days), STRATEGY.btc_entry_n, STRATEGY.btc_exit_n)

    def prices(self):
        data = self.market.ex.public_get_market_tickers({"instType": "SPOT"})["data"]
        m = {x["instId"]: float(x["last"]) for x in data}
        return {a: m[f"{a}-USDT"] for a in ASSETS if f"{a}-USDT" in m}

    # ---------- trade bookkeeping ----------
    def _record_close(self, acct, pos, fill, reason):
        pnl = pos["side"] * pos["qty"] * (fill["px"] - pos["entry_px"]) - pos["fees"] - fill["fee"]
        r = pnl / pos["risk_cash"] if pos["risk_cash"] else 0.0
        t = dict(account=acct, asset=pos["asset"], side=pos["side"], entry_ts=pos["entry_ts"], exit_ts=fill["ts"],
                 entry_px=pos["entry_px"], exit_px=fill["px"], qty=pos["qty"], pnl=pnl, r=r, reason=reason)
        self.store.add_trade(t)
        self.store.delete_position(acct, pos["asset"])
        self.store.event("exit", acct, pos["asset"], dict(reason=reason, r=round(r, 2), pnl=round(pnl, 2)))
        return t

    # ---------- hourly ----------
    def hourly(self):
        """Stops hit, stop health (live), heartbeat. Returns list of message events."""
        out = []
        for acct, br in self.brokers.items():
            for pos in self.store.positions(acct):
                try:
                    fill, checked = br.check_stop(pos)
                    if fill:
                        out.append(("exit", acct, self._record_close(acct, pos, fill, "stop")))
                        continue
                    pos["checked_ts"] = checked
                    self.store.save_position(pos)
                    if acct == "live" and not br.verify_stop(pos):
                        out.append(("alert", acct, f"وقف {pos['asset']} غير موجود على المنصة — إغلاق فوري"))
                        fill = br.close(pos, 0, make_clid("k", day_str(utc_day()), pos["asset"], acct))
                        out.append(("exit", acct, self._record_close(acct, pos, fill, "no_stop")))
                except Exception as e:
                    log.exception("hourly %s %s", acct, pos["asset"])
                    out.append(("alert", acct, f"خطأ في فحص {pos['asset']}: {e}"))
        self.store.set("heartbeat", int(time.time() * 1000))
        return out

    # ---------- daily ----------
    def data_ready(self):
        """True when yesterday's daily candle is confirmed for BTC."""
        d = self.market.daily("BTC", 3)
        return len(d) > 0 and int(d.index[-1]) == utc_day() - 1

    def daily(self, force=False):
        today = utc_day()
        if not force and self.store.get("daily_done") == today:
            return []
        out = self.hourly()
        self.load_features()
        prices = self.prices()
        sig_day = today - 1
        btc_today = int(self.btc_reg.loc[sig_day]) if sig_day in self.btc_reg.index else 0
        for acct, br in self.brokers.items():
            out += self._daily_account(acct, br, prices, sig_day, btc_today)
        self.store.set("daily_done", today)
        self.store.event("daily_done", detail=dict(day=today))
        return out

    def _daily_account(self, acct, br, prices, sig_day, btc_today):
        out = []
        ds = day_str(sig_day + 1)
        # 1) reconciliation (live)
        if acct == "live":
            for pos in self.store.positions(acct):
                try:
                    if not br.holdings_ok(pos):
                        self.store.set(f"{acct}_paused", True)
                        out.append(("alert", acct, f"عدم تطابق: {pos['asset']} غير موجود في الحساب. أوقفت الصفقات الجديدة."))
                except Exception as e:
                    out.append(("alert", acct, f"تعذّرت المطابقة: {e}"))
        # 2) exits
        exited = set()
        for pos in self.store.positions(acct):
            f = self.feat.get(pos["asset"])
            if f is None or int(f.index[-1]) != sig_day:
                continue
            dec, _ = decide(pos["asset"], f, pos["side"], btc_today, True, SHORTABLE, STRATEGY.stop_atr, 1e-12)
            if dec and dec.action == "exit":
                try:
                    fill = br.close(pos, prices.get(pos["asset"], float(f["c"].iloc[-1])), make_clid("x", ds, pos["asset"], acct))
                    out.append(("exit", acct, self._record_close(acct, pos, fill, "channel")))
                    exited.add(pos["asset"])
                except Exception as e:
                    out.append(("alert", acct, f"فشل إغلاق {pos['asset']}: {e}"))
        # 3) equity, peak, drawdown pause
        eq = br.equity(prices)
        peak = max(self.store.get(f"{acct}_peak", eq), eq)
        self.store.set(f"{acct}_peak", peak)
        self.store.save_equity(acct, sig_day + 1, eq)
        dd = drawdown(eq, peak)
        if dd >= RISK.dd_pause_pct and not self.store.get(f"{acct}_paused", False):
            self.store.set(f"{acct}_paused", True)
            out.append(("alert", acct, f"تراجع {dd:.1f}% من القمة: أوقفت الصفقات الجديدة حتى /resume"))
        paused = self.store.get(f"{acct}_paused", False)
        # 4) entries, in fixed priority order
        open_now = {p["asset"]: p for p in self.store.positions(acct)}
        for a in ASSETS:
            f = self.feat.get(a)
            if f is None or a in open_now or a in exited or int(f.index[-1]) != sig_day:
                continue
            tick = self.market.tick(f"{a}-USDT")
            dec, why = decide(a, f, 0, btc_today, self.s.shorts_allowed(acct), SHORTABLE, STRATEGY.stop_atr, tick)
            if dec is None:
                if why in ("btc_filter",):
                    self.store.event("skip", acct, a, dict(reason=why))
                    out.append(("skip", acct, dict(asset=a, reason=why)))
                continue
            reason = None
            if paused:
                reason = "paused"
            elif not cap_ok(list(open_now.values())):
                reason = "cap"
            if reason:
                self.store.event("skip", acct, a, dict(reason=reason))
                out.append(("skip", acct, dict(asset=a, reason=reason)))
                continue
            ref = prices.get(a)
            if not ref:
                out.append(("alert", acct, f"لا يوجد سعر حالي لـ {a}، تُركت الإشارة"))
                continue
            qty, risk_cash = size_position(eq, ref, dec.stop, dec.side)
            if qty <= 0:
                out.append(("skip", acct, dict(asset=a, reason="price_beyond_stop")))
                continue
            fee = COSTS.spot_fee if dec.side == 1 else COSTS.swap_fee
            free = br.free_cash(prices)
            need = qty * ref * (1 + fee) if dec.side == 1 else qty * ref / RISK.short_leverage
            if need > free:
                scale = max(0.0, free * 0.98 / need)
                qty *= scale
            if acct == "live" and qty < br.min_qty(a, dec.side):
                self.store.event("skip", acct, a, dict(reason="below_min", qty=qty))
                out.append(("skip", acct, dict(asset=a, reason="below_min")))
                continue
            clid = make_clid("e", ds, a, acct)
            try:
                fill = br.open(a, dec.side, qty, ref, dec.stop, clid)
            except BrokerError as e:
                out.append(("alert", acct, f"فشل فتح {a}: {e}"))
                continue
            pos = dict(account=acct, asset=a, side=dec.side, market="spot" if dec.side == 1 else "swap",
                       qty=fill["qty"], entry_px=fill["px"], stop_px=dec.stop,
                       risk_cash=fill["qty"] * abs(fill["px"] - dec.stop), risk_pct=RISK.risk_pct,
                       entry_ts=fill["ts"], clid=clid, algo_id=fill.get("algo_id"),
                       checked_ts=(fill["ts"] // 3_600_000) * 3_600_000 - 1, fees=fill["fee"])
            self.store.save_position(pos)
            open_now[a] = pos
            self.store.event("entry", acct, a, dict(side=dec.side, px=fill["px"], stop=dec.stop))
            out.append(("entry", acct, pos))
        return out

    # ---------- manual controls ----------
    def close_all(self, acct=None):
        out = []
        prices = self.prices()
        for a, br in self.brokers.items():
            if acct and a != acct:
                continue
            self.store.set(f"{a}_paused", True)
            for pos in self.store.positions(a):
                try:
                    fill = br.close(pos, prices.get(pos["asset"], pos["entry_px"]),
                                    make_clid("c", day_str(utc_day()), pos["asset"], a))
                    out.append(("exit", a, self._record_close(a, pos, fill, "manual")))
                except Exception as e:
                    out.append(("alert", a, f"فشل إغلاق {pos['asset']}: {e}"))
        return out
