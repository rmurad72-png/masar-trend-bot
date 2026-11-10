"""Real orders on an OKX sub-account (trade-only API key, no withdrawal permission).

Longs: spot market buy, then a conditional (algo) stop that sells the whole holding.
Shorts (BTC/ETH only): USDT swap, isolated margin, fixed leverage, reduce-only algo stop.
Every order carries a deterministic clOrdId; before placing, the broker looks it up so a retry
never duplicates an order. If the stop cannot be placed, the position is closed at once."""
import math
import time
import logging
import ccxt
from ..config import RISK
from .base import BrokerError, inst_id
from ..market import make_okx
from ..guards import allowed_instrument

log = logging.getLogger(__name__)


def _floor(x, step):
    return math.floor(x / step + 1e-9) * step


def _fmt(x):
    return format(x, "f").rstrip("0").rstrip(".")


class OKXLiveBroker:
    account = "live"

    def __init__(self, store, market, key, secret, passphrase):
        self.store, self.market = store, market
        self.ex = make_okx(apiKey=key, secret=secret, password=passphrase)
        self._pos_mode = None

    # ---------- helpers ----------
    def _call(self, fn, params, tries=3):
        for i in range(tries):
            try:
                r = fn(params)
                if r.get("code") not in (None, "0"):
                    raise BrokerError(f"{r.get('code')}: {r.get('msg')} {r.get('data')}")
                return r["data"]
            except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
                log.warning("okx retry %s: %s", i, e)
                time.sleep(1 + i)
        raise BrokerError("network")

    def pos_mode(self):
        if self._pos_mode is None:
            self._pos_mode = self._call(self.ex.private_get_account_config, {})[0]["posMode"]
        return self._pos_mode

    def _order_by_clid(self, inst, clid):
        try:
            d = self._call(self.ex.private_get_trade_order, {"instId": inst, "clOrdId": clid}, tries=1)
            return d[0] if d else None
        except BrokerError:
            return None

    def _wait_filled(self, inst, clid, timeout=20):
        t0 = time.time()
        while time.time() - t0 < timeout:
            o = self._order_by_clid(inst, clid)
            if o and o["state"] == "filled":
                return o
            if o and o["state"] in ("canceled", "mmp_canceled"):
                raise BrokerError(f"order {clid} canceled")
            time.sleep(1)
        raise BrokerError(f"order {clid} not filled in {timeout}s")

    # ---------- balances ----------
    def equity(self, prices=None):
        return float(self._call(self.ex.private_get_account_balance, {})[0]["totalEq"])

    def _bal(self, ccy):
        d = self._call(self.ex.private_get_account_balance, {"ccy": ccy})[0]["details"]
        return float(d[0]["availBal"]) if d else 0.0

    def free_cash(self, prices=None):
        return self._bal("USDT")

    def min_qty(self, asset, side):
        i = self.market.inst(inst_id(asset, side))
        return float(i["minSz"]) * (float(i["ctVal"]) if side == -1 else 1.0)

    # ---------- orders ----------
    def open(self, asset, side, qty, ref_px, stop, clid):
        if not allowed_instrument(asset, side):
            raise BrokerError("not_whitelisted")
        inst = inst_id(asset, side)
        spec = self.market.inst(inst)
        tick = float(spec["tickSz"])
        trig = _fmt(round(stop / tick) * tick)
        if side == 1:
            sz = _floor(qty, float(spec["lotSz"]))
            if sz < float(spec["minSz"]):
                raise BrokerError("below_min")
            if not self._order_by_clid(inst, clid):
                self._call(self.ex.private_post_trade_order, {
                    "instId": inst, "tdMode": "cash", "side": "buy", "ordType": "market",
                    "sz": _fmt(sz), "tgtCcy": "base_ccy", "clOrdId": clid})
            o = self._wait_filled(inst, clid)
            px = float(o["avgPx"])
            fee_base = -float(o["fee"]) if o.get("feeCcy") == asset else 0.0
            held = _floor(min(float(o["accFillSz"]) - fee_base, self._bal(asset)), float(spec["lotSz"]))
            fee_usdt = fee_base * px + (-float(o["fee"]) if o.get("feeCcy") == "USDT" else 0.0)
            algo = {"instId": inst, "tdMode": "cash", "side": "sell", "ordType": "conditional",
                    "sz": _fmt(held), "slTriggerPx": trig, "slOrdPx": "-1", "algoClOrdId": clid + "s"}
            qty_out = held
        else:
            ct = float(spec["ctVal"])
            contracts = _floor(qty / ct, float(spec["lotSz"]))
            if contracts < float(spec["minSz"]):
                raise BrokerError("below_min")
            self._call(self.ex.private_post_account_set_leverage,
                       {"instId": inst, "lever": str(RISK.short_leverage), "mgnMode": "isolated"})
            order = {"instId": inst, "tdMode": "isolated", "side": "sell", "ordType": "market",
                     "sz": _fmt(contracts), "clOrdId": clid}
            if self.pos_mode() == "long_short_mode":
                order["posSide"] = "short"
            if not self._order_by_clid(inst, clid):
                self._call(self.ex.private_post_trade_order, order)
            o = self._wait_filled(inst, clid)
            px = float(o["avgPx"])
            fee_usdt = -float(o["fee"])
            algo = {"instId": inst, "tdMode": "isolated", "side": "buy", "ordType": "conditional",
                    "sz": _fmt(contracts), "slTriggerPx": trig, "slOrdPx": "-1", "reduceOnly": "true",
                    "algoClOrdId": clid + "s"}
            if "posSide" in order:
                algo["posSide"] = "short"
                algo.pop("reduceOnly")
            qty_out = contracts * ct
        try:
            algo_id = self._call(self.ex.private_post_trade_order_algo, algo)[0]["algoId"]
        except Exception as e:
            # never leave a position without a stop on the exchange
            log.error("stop placement failed for %s: %s -> closing", asset, e)
            self.close(dict(asset=asset, side=side, qty=qty_out, algo_id=None), px, clid + "f")
            raise BrokerError(f"stop_failed: {e}")
        return dict(px=px, qty=qty_out, fee=fee_usdt, ts=int(o["fillTime"] or time.time() * 1000), algo_id=algo_id)

    def _cancel_algo(self, pos):
        if pos.get("algo_id"):
            try:
                self._call(self.ex.private_post_trade_cancel_algos,
                           [{"algoId": pos["algo_id"], "instId": inst_id(pos["asset"], pos["side"])}])
            except BrokerError as e:
                log.warning("cancel algo: %s", e)

    def close(self, pos, ref_px, clid):
        asset, side = pos["asset"], pos["side"]
        inst = inst_id(asset, side)
        self._cancel_algo(pos)
        if side == 1:
            spec = self.market.inst(inst)
            sz = _floor(self._bal(asset), float(spec["lotSz"]))
            if sz < float(spec["minSz"]):
                raise BrokerError("nothing_to_sell")
            if not self._order_by_clid(inst, clid):
                self._call(self.ex.private_post_trade_order, {
                    "instId": inst, "tdMode": "cash", "side": "sell", "ordType": "market",
                    "sz": _fmt(sz), "clOrdId": clid})
            o = self._wait_filled(inst, clid)
            fee = -float(o["fee"]) if o.get("feeCcy") == "USDT" else -float(o["fee"]) * float(o["avgPx"])
        else:
            if not self._order_by_clid(inst, clid):
                p = {"instId": inst, "mgnMode": "isolated", "clOrdId": clid}
                if self.pos_mode() == "long_short_mode":
                    p["posSide"] = "short"
                self._call(self.ex.private_post_trade_close_position, p)
            o = self._wait_filled(inst, clid)
            fee = -float(o["fee"])
        return dict(px=float(o["avgPx"]), qty=pos["qty"], fee=fee, ts=int(o["fillTime"] or time.time() * 1000))

    def verify_stop(self, pos):
        if not pos.get("algo_id"):
            return False
        d = self._call(self.ex.private_get_trade_order_algo, {"algoId": pos["algo_id"]}, tries=2)
        return bool(d) and d[0]["state"] in ("live", "effective")

    def check_stop(self, pos):
        """Detects a stop that OKX already executed."""
        d = self._call(self.ex.private_get_trade_order_algo, {"algoId": pos["algo_id"]})
        now = int(time.time() * 1000)
        if not d or d[0]["state"] != "effective":
            return None, now
        ord_id = d[0].get("ordId") or (d[0].get("ordIdList") or [None])[0]
        o = self._call(self.ex.private_get_trade_order, {"instId": inst_id(pos["asset"], pos["side"]), "ordId": ord_id})[0]
        if o["state"] != "filled":
            return None, now
        fee = -float(o["fee"]) if o.get("feeCcy") == "USDT" else -float(o["fee"]) * float(o["avgPx"])
        return dict(px=float(o["avgPx"]), qty=pos["qty"], fee=fee, ts=int(o["fillTime"])), now

    def activity_snapshot(self, since_ts):
        """Everything on the sub-account that could reveal orders the bot did not place."""
        snap = dict(orders=[], algos=[], fills=[], swap_positions=[], balances=[])
        for t in ("SPOT", "SWAP"):
            snap["orders"] += self._call(self.ex.private_get_trade_orders_pending, {"instType": t})
            snap["algos"] += self._call(self.ex.private_get_trade_orders_algo_pending,
                                        {"instType": t, "ordType": "conditional"})
            fills = self._call(self.ex.private_get_trade_fills, {"instType": t, "begin": str(since_ts)})
            for f in fills:                         # stop-triggered orders carry the algo's client id
                if not f.get("clOrdId"):
                    try:
                        o = self._call(self.ex.private_get_trade_order, {"instId": f["instId"], "ordId": f["ordId"]}, tries=1)
                        f["algoClOrdId"] = o[0].get("algoClOrdId", "") if o else ""
                    except BrokerError:
                        f["algoClOrdId"] = ""
            snap["fills"] += fills
        snap["swap_positions"] = self._call(self.ex.private_get_account_positions, {"instType": "SWAP"})
        snap["balances"] = self._call(self.ex.private_get_account_balance, {})[0]["details"]
        return snap

    def holdings_ok(self, pos):
        """Reconciliation: does the exchange still hold this position?"""
        if pos["side"] == 1:
            return self._bal(pos["asset"]) >= pos["qty"] * 0.98
        d = self._call(self.ex.private_get_account_positions, {"instId": inst_id(pos["asset"], -1)})
        return any(abs(float(x["pos"])) > 0 for x in d)
