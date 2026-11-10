from masar.guards import check_entry, find_foreign_activity, is_bot_id, allowed_instrument
from masar.brokers.base import make_clid


def test_whitelist():
    assert allowed_instrument("ETH", 1) and allowed_instrument("ETH", -1)
    assert not allowed_instrument("SOL", -1) and not allowed_instrument("PEPE", 1)


def test_check_entry_limits():
    ok = dict(asset="ETH", side=1, qty=1, ref_px=100, last_close=100, equity=10_000, entries_today=0, orders_today=0)
    assert check_entry(**ok) is None
    assert check_entry(**{**ok, "ref_px": 120}) == "price_jump"
    assert check_entry(**{**ok, "qty": 200}) == "max_notional"
    assert check_entry(**{**ok, "entries_today": 10}) == "max_entries_day"
    assert check_entry(**{**ok, "orders_today": 30}) == "max_orders_day"
    assert check_entry(**{**ok, "asset": "PEPE"}) == "not_whitelisted"


def test_bot_ids_recognised():
    cid = make_clid("e", "20261110", "ETH", "live")
    assert is_bot_id(cid) and is_bot_id(cid + "s") and not is_bot_id("") and not is_bot_id("abc")


def test_foreign_activity():
    pos = [dict(asset="ETH", side=1, algo_id="111")]
    snap = dict(
        orders=[{"clOrdId": "mle20261110ETH", "ordId": "1"}, {"clOrdId": "", "ordId": "2", "instId": "DOGE-USDT"}],
        algos=[{"algoId": "111", "algoClOrdId": "mle20261110ETHs"}, {"algoId": "999", "algoClOrdId": "x"}],
        fills=[{"ts": "2000", "clOrdId": "mle20261110ETH"}, {"ts": "2000", "clOrdId": "", "algoClOrdId": "mle1s"},
               {"ts": "2000", "clOrdId": "", "algoClOrdId": "", "billId": "b1"}, {"ts": "500", "clOrdId": ""}],
        swap_positions=[{"instId": "BTC-USDT-SWAP", "pos": "-1", "posId": "p1"}],
        balances=[{"ccy": "USDT", "eqUsd": "80"}, {"ccy": "ETH", "eqUsd": "5"}, {"ccy": "PEPE", "eqUsd": "40"},
                  {"ccy": "SOL", "eqUsd": "0.5"}])
    kinds = sorted(f["kind"] + ":" + str(f["id"]) for f in find_foreign_activity(snap, pos, since_ts=1000))
    assert kinds == ["algo:999", "balance:bal-PEPE", "fill:b1", "order:2", "position:p1"]


def test_engine_intrusion_pauses_live():
    from masar.store import Store
    from masar.engine import Engine
    from masar.config import Settings

    class FakeLive:
        account = "live"
        def check_stop(self, pos): return None, 0
        def verify_stop(self, pos): return True
        def activity_snapshot(self, since):
            return dict(orders=[{"clOrdId": "", "ordId": "7", "instId": "DOGE-USDT", "side": "buy", "sz": "100"}])

    st = Store("sqlite:///:memory:")
    eng = Engine(Settings(), st, None, {"live": FakeLive()})
    ev = eng.hourly()
    assert st.get("live_paused") is True and any(e[0] == "alert" for e in ev)
    assert eng.hourly() == []            # same finding is reported only once


def test_engine_blocks_price_jump(monkeypatch):
    from tests.fakes import trend_frame
    from tests.test_core import _engine
    from masar.config import ASSETS
    fr = trend_frame(n=101)
    monkeypatch.setattr("masar.engine.utc_day", lambda ts=None: int(fr.index[-1]) + 1)
    eng, st = _engine({"BTC": fr}, {a: float(fr["c"].iloc[-1]) * 1.3 for a in ASSETS})
    ev = eng.daily()
    assert not st.positions("paper")
    assert any(e[0] == "alert" and "price_jump" in e[2] for e in ev)
