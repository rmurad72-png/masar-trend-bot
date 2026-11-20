"""Daily cycle and hourly checks for every enabled account (paper and/or live)."""
import time
import logging
from datetime import datetime, timezone
from .config import ASSETS, SHORTABLE, STRATEGY, RISK, COSTS
from .signals import features, btc_regime, decide
from .risk import size_position, cap_ok, drawdown
from .brokers.base import BrokerError, make_clid
from .guards import check_entry, fill_slip_pct, find_foreign_activity, LIMITS, REASON_AR
from .journal import entry_context, update_excursion, r_of, lesson, dumps, loads, journal_stats

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
        self._px = {}
        self._number_legacy()

    def _number_legacy(self):
        """Positions opened before the journal existed get trade numbers in entry order."""
        for acct in self.brokers:
            for p in sorted(self.store.positions(acct), key=lambda x: x["entry_ts"]):
                if p.get("trade_no") is None:
                    p["trade_no"] = self.store.next_trade_no(acct)
                    self.store.save_position(p)

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
        try:
            self.backfill_context()
        except Exception:
            log.exception("backfill_context")

    def backfill_context(self):
        """Positions opened before the journal existed: rebuild the entry context from the signal day,
        i.e. the last confirmed day before entry on which the regime turned to the position's side."""
        for acct in self.brokers:
            for p in self.store.positions(acct):
                if p.get("ctx") or p["asset"] not in self.feat:
                    continue
                f = self.feat[p["asset"]]
                entry_day = int(p["entry_ts"] // DAY_MS)
                sig = None
                for d in range(entry_day - 1, entry_day - 8, -1):
                    if d in f.index and (d - 1) in f.index and int(f.loc[d, "reg"]) == p["side"] \
                            and int(f.loc[d - 1, "reg"]) != p["side"]:
                        sig = d
                        break
                if sig is None:
                    continue
                fs = f.loc[:sig]
                others = [o for o in self.store.positions(acct) if o["entry_ts"] < p["entry_ts"]
                          or (o["entry_ts"] == p["entry_ts"] and (o.get("trade_no") or 0) < (p.get("trade_no") or 0))]
                curve = [r for r in self.store.equity_curve(acct) if r["day"] <= entry_day and r["equity"] >= 1]
                eq = curve[-1]["equity"] if curve else (self.store.get("paper_start", 10000.0) if acct == "paper" else 0.0)
                ctx = entry_context(fs, p["side"], self.btc_reg.loc[:sig], others, eq, p["qty"] * p["entry_px"])
                ctx["backfilled"] = True
                p["ctx"] = dumps(ctx)
                close = float(fs["c"].iloc[-1])
                p["signal_close"] = close
                p["entry_slip"] = p["side"] * (p["entry_px"] - close) / close * 100
                self.store.save_position(p)

    def prices(self):
        data = self.market.ex.public_get_market_tickers({"instType": "SPOT"})["data"]
        m = {x["instId"]: float(x["last"]) for x in data}
        self._px = {a: m[f"{a}-USDT"] for a in ASSETS if f"{a}-USDT" in m}
        return self._px

    def ensure_features(self):
        if not self.feat or self.btc_reg is None:
            self.load_features()

    # ---------- trade bookkeeping ----------
    def _record_close(self, acct, pos, fill, reason, ref=None):
        side = pos["side"]
        pnl = side * pos["qty"] * (fill["px"] - pos["entry_px"]) - pos["fees"] - fill["fee"]
        r = pnl / pos["risk_cash"] if pos["risk_cash"] else 0.0
        # the exit price itself is part of the path
        best = pos.get("best_px") if pos.get("best_px") is not None else pos["entry_px"]
        worst = pos.get("worst_px") if pos.get("worst_px") is not None else pos["entry_px"]
        if side * (fill["px"] - best) > 0:
            best, pos["best_ts"] = fill["px"], fill["ts"]
        if side * (fill["px"] - worst) < 0:
            worst, pos["worst_ts"] = fill["px"], fill["ts"]
        exit_slip = side * (ref - fill["px"]) / ref * 100 if ref else None
        t = dict(account=acct, asset=pos["asset"], side=side, entry_ts=pos["entry_ts"], exit_ts=fill["ts"],
                 entry_px=pos["entry_px"], exit_px=fill["px"], qty=pos["qty"], pnl=pnl, r=r, reason=reason,
                 trade_no=pos.get("trade_no"), ctx=pos.get("ctx"), stop_px=pos["stop_px"],
                 mfe_r=r_of(pos, best), mae_r=r_of(pos, worst), mfe_ts=pos.get("best_ts"), mae_ts=pos.get("worst_ts"),
                 fees=pos["fees"] + fill["fee"], entry_slip=pos.get("entry_slip"), exit_slip=exit_slip,
                 signal_close=pos.get("signal_close"))
        les = lesson(t)
        t["lesson"] = dumps(les)
        self.store.add_trade(t)
        self.store.delete_position(acct, pos["asset"])
        self.store.event("exit", acct, pos["asset"], dict(no=t["trade_no"], reason=reason, r=round(r, 2),
                                                           pnl=round(pnl, 2), type=les["type"]))
        msg = dict(t, lesson_obj=les, ctx_obj=loads(t["ctx"]), stats=journal_stats(self.store.trades(acct)))
        try:
            msg["equity_after"] = self.brokers[acct].equity(self._px)
        except Exception:
            msg["equity_after"] = None
        return msg

    # ---------- counters & intrusion check ----------
    def _count(self, acct, kind):
        return int(self.store.get(f"{acct}_{kind}_{utc_day()}", 0))

    def _bump(self, acct, kind):
        self.store.set(f"{acct}_{kind}_{utc_day()}", self._count(acct, kind) + 1)

    def intrusion_check(self, acct, br):
        """Live only: anything on the sub-account the bot did not create pauses trading and alerts."""
        if not hasattr(br, "activity_snapshot"):
            return []
        now = int(time.time() * 1000)
        since = int(self.store.get(f"{acct}_activity_since", now - 3 * 86_400_000))
        try:
            snap = br.activity_snapshot(since)
        except Exception as e:
            return [("alert", acct, f"تعذّر فحص نشاط الحساب: {e}")]
        findings = find_foreign_activity(snap, self.store.positions(acct), since)
        seen = set(self.store.get(f"{acct}_foreign_seen", []))
        new = [f for f in findings if f"{f['kind']}:{f['id']}" not in seen]
        self.store.set(f"{acct}_activity_since", now - 600_000)     # 10 min overlap
        if not new:
            return []
        seen |= {f"{f['kind']}:{f['id']}" for f in new}
        self.store.set(f"{acct}_foreign_seen", sorted(seen)[-500:])
        self.store.set(f"{acct}_paused", True)
        self.store.event("intrusion", acct, detail=new)
        lines = "\n".join(f"• {f['kind']}: {f['detail']}" for f in new[:10])
        return [("alert", acct, "نشاط في الحساب لم ينفذه البوت! أوقفت الصفقات الجديدة.\n" + lines +
                 "\nإن لم تكن أنت: احذف مفتاح API فوراً من OKX وأنشئ مفتاحاً جديداً.")]

    # ---------- hourly ----------
    def hourly(self):
        """Stops hit, stop health (live), heartbeat. Returns list of message events."""
        out = []
        for acct, br in self.brokers.items():
            for pos in self.store.positions(acct):
                try:
                    since = pos.get("ext_ts") or (pos["entry_ts"] // 3_600_000) * 3_600_000 - 1
                    if update_excursion(pos, self.market.hourly_since(f"{pos['asset']}-USDT", since)):
                        self.store.save_position(pos)
                except Exception as e:
                    log.warning("excursion %s %s: %s", acct, pos["asset"], e)
                try:
                    fill, checked = br.check_stop(pos)
                    if fill:
                        out.append(("exit", acct, self._record_close(acct, pos, fill, "stop", ref=pos["stop_px"])))
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
            if acct == "live":
                out += self.intrusion_check(acct, br)
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
                if self._count(acct, "orders") >= LIMITS.max_orders_per_day:
                    out.append(("alert", acct, f"تجاوز حد الأوامر اليومي، لم يُغلق {pos['asset']}"))
                    continue
                self._bump(acct, "orders")
                try:
                    fill = br.close(pos, prices.get(pos["asset"], float(f["c"].iloc[-1])), make_clid("x", ds, pos["asset"], acct))
                    out.append(("exit", acct, self._record_close(acct, pos, fill, "channel", ref=float(f["c"].iloc[-1]))))
                    exited.add(pos["asset"])
                except Exception as e:
                    out.append(("alert", acct, f"فشل إغلاق {pos['asset']}: {e}"))
        # 3) equity, peak, drawdown pause
        eq = br.equity(prices)
        peak = self.store.get(f"{acct}_peak", eq)
        # deposits/withdrawals are not profit or loss: move the peak by the same amount
        if hasattr(br, "transfers_since"):
            now = int(time.time() * 1000)
            since = int(self.store.get(f"{acct}_flows_since", now - 86_400_000))
            try:
                flow = br.transfers_since(since, prices)
                self.store.set(f"{acct}_flows_since", now)
                if abs(flow) >= 0.01:
                    peak = max(peak + flow, 0.0)
                    self.store.event("transfer", acct, detail=dict(usd=round(flow, 2)))
                    self.store.set(f"{acct}_bench_start", sig_day + 1)
                    out.append(("info", acct, f"تحويل رصيد {flow:+.2f}$ — لا يُحسب ربحاً أو خسارة، وعُدّل حساب التراجع"))
            except Exception as e:
                log.warning("transfers_since: %s", e)
        if eq < 1.0:
            # an empty account is not a drawdown: skip entries, keep any pause state as it is.
            # Peak goes to 0 so a later deposit restarts the drawdown count from the new balance.
            self.store.set(f"{acct}_peak", 0.0)
            self.store.save_equity(acct, sig_day + 1, eq)
            if not self.store.get(f"{acct}_empty_warned", False):
                self.store.set(f"{acct}_empty_warned", True)
                out.append(("info", acct, "الحساب بلا رصيد في حساب التداول. لا صفقات جديدة حتى يعود الرصيد."))
            return out
        self.store.set(f"{acct}_empty_warned", False)
        peak = max(peak, eq)
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
            block = check_entry(a, dec.side, qty, ref, dec.close, eq,
                                self._count(acct, "entries"), self._count(acct, "orders"))
            if block:
                self.store.event("blocked", acct, a, dict(reason=block, qty=qty, ref=ref, close=dec.close))
                out.append(("alert", acct, f"منعت حماية البوت أمر {a}: {REASON_AR.get(block, block)} ({block})"))
                continue
            clid = make_clid("e", ds, a, acct)
            self._bump(acct, "orders")
            try:
                fill = br.open(a, dec.side, qty, ref, dec.stop, clid)
            except BrokerError as e:
                out.append(("alert", acct, f"فشل فتح {a}: {e}"))
                continue
            self._bump(acct, "entries")
            if fill_slip_pct(fill["px"], ref) > LIMITS.max_fill_slip_pct:
                out.append(("alert", acct, f"تنفيذ {a} بسعر {fill['px']} بعيد عن السعر المرجعي {ref}"))
            ctx = entry_context(f, dec.side, self.btc_reg.loc[:sig_day], list(open_now.values()), eq,
                                fill["qty"] * fill["px"])
            hour0 = (fill["ts"] // 3_600_000) * 3_600_000 - 1
            pos = dict(account=acct, asset=a, side=dec.side, market="spot" if dec.side == 1 else "swap",
                       qty=fill["qty"], entry_px=fill["px"], stop_px=dec.stop,
                       risk_cash=fill["qty"] * abs(fill["px"] - dec.stop), risk_pct=RISK.risk_pct,
                       entry_ts=fill["ts"], clid=clid, algo_id=fill.get("algo_id"),
                       checked_ts=hour0, fees=fill["fee"], trade_no=self.store.next_trade_no(acct),
                       ctx=dumps(ctx), signal_close=dec.close,
                       entry_slip=dec.side * (fill["px"] - dec.close) / dec.close * 100,
                       best_px=fill["px"], worst_px=fill["px"], best_ts=fill["ts"], worst_ts=fill["ts"], ext_ts=hour0)
            self.store.save_position(pos)
            open_now[a] = pos
            self.store.event("entry", acct, a, dict(no=pos["trade_no"], side=dec.side, px=fill["px"], stop=dec.stop))
            out.append(("entry", acct, dict(pos, ctx_obj=ctx, open_risk=sum(p["risk_pct"] for p in open_now.values()))))
        return out

    # ---------- watch & evening heads-up ----------
    def levels(self, a):
        """Tomorrow's decision levels from the last confirmed day."""
        f = self.feat[a]
        return dict(hh55=float(f["h"].iloc[-55:].max()), ll55=float(f["l"].iloc[-55:].min()),
                    hh10=float(f["h"].iloc[-10:].max()), ll10=float(f["l"].iloc[-10:].min()),
                    hh20=float(f["h"].iloc[-20:].max()), ll20=float(f["l"].iloc[-20:].min()),
                    reg=int(f["reg"].iloc[-1]))

    def open_positions_all(self):
        return [(acct, p) for acct in self.brokers for p in self.store.positions(acct)]

    def watch_data(self, prices):
        self.ensure_features()
        btc_r = int(self.btc_reg.iloc[-1])
        b = self.levels("BTC")
        held = {(p["asset"], p["side"]) for _, p in self.open_positions_all()}
        longs, shorts = [], []
        for a in self.feat:
            px = prices.get(a)
            if not px:
                continue
            lv = self.levels(a)
            if (a, 1) not in held:
                longs.append(dict(asset=a, dist=(lv["hh55"] / px - 1) * 100, in_trend=lv["reg"] == 1,
                                  blocked=a != "BTC" and btc_r != 1))
            if a in SHORTABLE:
                shorts.append(dict(asset=a, dist=(1 - lv["ll55"] / px) * 100, in_trend=lv["reg"] == -1,
                                   held=(a, -1) in held, blocked=a != "BTC" and btc_r != -1))
        longs.sort(key=lambda x: x["dist"])
        opens = []
        for acct, p in self.open_positions_all():
            px = prices.get(p["asset"])
            if not px or p["asset"] not in self.feat:
                continue
            lv = self.levels(p["asset"])
            ex = lv["ll10"] if p["side"] == 1 else lv["hh10"]
            opens.append(dict(acct=acct, pos=p, px=px, exit=ex, exit_dist=p["side"] * (px / ex - 1) * 100,
                              stop_dist=p["side"] * (px / p["stop_px"] - 1) * 100, r=r_of(p, px)))
        return dict(btc_reg=btc_r, btc=b, btc_px=prices.get("BTC"), longs=longs, shorts=shorts, opens=opens)

    def evening_items(self, prices, near_pct=3.0):
        """Assets that could trigger at tonight's close (information only; nothing is executed)."""
        w = self.watch_data(prices)
        items = []
        for x in w["longs"]:
            if not x["in_trend"] and x["dist"] <= near_pct:
                items.append(dict(kind="long", **x))
        for x in w["shorts"]:
            if not x["held"] and not x["in_trend"] and x["dist"] <= near_pct:
                items.append(dict(kind="short", **x))
        for o in w["opens"]:
            if o["exit_dist"] <= near_pct:
                items.append(dict(kind="exit", asset=o["pos"]["asset"], acct=o["acct"], side=o["pos"]["side"],
                                  dist=o["exit_dist"], level=o["exit"], r=o["r"]))
        return items

    def live_readiness(self, prices, near_pct=10.0, min_equity=100.0):
        """Is a real buying opportunity approaching? Buys need BTC's regime to turn up (filter).
        Returns a dict when the state escalates (far -> near -> open), else None. Live account only."""
        if "live" not in self.brokers:
            return None
        self.ensure_features()
        b = self.levels("BTC")
        px = prices.get("BTC")
        if not px:
            return None
        reg = int(self.btc_reg.iloc[-1])
        dist = (b["hh55"] / px - 1) * 100
        state = "open" if reg == 1 else ("near" if dist <= near_pct else "far")
        prev = self.store.get("live_ready_state", "far")
        self.store.set("live_ready_state", state)
        rank = {"far": 0, "near": 1, "open": 2}
        if rank[state] <= rank[prev]:
            return None
        try:
            eq = self.brokers["live"].equity(prices)
        except Exception:
            eq = 0.0
        near_longs = []
        for a in self.feat:
            p = prices.get(a)
            if p and a != "BTC":
                lv = self.levels(a)
                if lv["reg"] != 1:
                    near_longs.append((a, (lv["hh55"] / p - 1) * 100))
        near_longs.sort(key=lambda x: x[1])
        return dict(state=state, dist=dist, level=b["hh55"], equity=eq, min_equity=min_equity,
                    near_longs=near_longs[:3])

    def btc_closes(self):
        self.ensure_features()
        return self.feat["BTC"]["c"]

    def bench_start(self, acct):
        curve = self.store.equity_curve(acct)
        start = self.store.get(f"{acct}_bench_start")
        first = next((r["day"] for r in curve if r["equity"] >= 1), None)
        empty_days = [r["day"] for r in curve if r["equity"] < 1]
        cands = [x for x in (start, first, (max(empty_days) + 1) if empty_days else None) if x is not None]
        return max(cands) if cands else None

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
                    ref = prices.get(pos["asset"], pos["entry_px"])
                    fill = br.close(pos, ref, make_clid("c", day_str(utc_day()), pos["asset"], a))
                    out.append(("exit", a, self._record_close(a, pos, fill, "manual", ref=ref)))
                except Exception as e:
                    out.append(("alert", a, f"فشل إغلاق {pos['asset']}: {e}"))
        return out
