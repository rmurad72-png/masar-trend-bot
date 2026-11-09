import pandas as pd
import pytest
from masar.signals import features, decide, regime_series
from masar.risk import size_position, cap_ok, drawdown
from masar.brokers.base import make_clid
from masar.brokers.paper import PaperBroker
from masar.store import Store
from masar.engine import Engine
from masar.config import Settings, SHORTABLE, COSTS, ASSETS
from tests.fakes import trend_frame, FakeMarket


def test_regime_breakout_and_exit():
    f = features(trend_frame(), 55, 10, 20)
    assert f["reg"].iloc[99] == 0 and f["reg"].iloc[100] == 1


def test_decide_entry_long_with_btc_up():
    f = features(trend_frame(n=101), 55, 10, 20)
    dec, why = decide("ETH", f, 0, 1, True, SHORTABLE, 2.0, 0.01)
    assert dec.action == "enter" and dec.side == 1 and dec.stop < dec.close


def test_btc_filter_blocks_alt_long():
    f = features(trend_frame(n=101), 55, 10, 20)
    dec, why = decide("SOL", f, 0, 0, True, SHORTABLE, 2.0, 0.01)
    assert dec is None and why == "btc_filter"
    dec, _ = decide("BTC", f, 0, 0, True, SHORTABLE, 2.0, 0.01)   # BTC itself is never filtered
    assert dec is not None


def test_short_only_btc_eth():
    fr = trend_frame(n=101)
    fr["c"] = 200 - fr["c"]; fr["h"], fr["l"] = fr["c"] + 1, fr["c"] - 1
    f = features(fr, 55, 10, 20)
    assert decide("SOL", f, 0, -1, True, SHORTABLE, 2.0, 0.01)[1] == "short_not_allowed"
    assert decide("ETH", f, 0, -1, True, SHORTABLE, 2.0, 0.01)[0].side == -1
    assert decide("ETH", f, 0, -1, False, SHORTABLE, 2.0, 0.01)[0] is None


def test_no_reentry_without_fresh_breakout():
    f = features(trend_frame(n=105), 55, 10, 20)
    dec, _ = decide("BTC", f, 0, 1, True, SHORTABLE, 2.0, 0.01)
    assert dec is None                       # regime already 1 yesterday


def test_exit_when_regime_changes():
    fr = trend_frame(n=115)
    fr.iloc[-1, fr.columns.get_loc("c")] = 50; fr.iloc[-1, fr.columns.get_loc("l")] = 49
    f = features(fr, 55, 10, 20)
    dec, _ = decide("BTC", f, 1, 1, True, SHORTABLE, 2.0, 0.01)
    assert dec.action == "exit"


def test_sizing_and_cap():
    qty, rc = size_position(10_000, 100, 90, 1, 0.5)
    assert qty == pytest.approx(5) and rc == pytest.approx(50)
    assert size_position(10_000, 100, 101, 1, 0.5) == (0.0, 0.0)
    assert cap_ok([{"risk_pct": 0.5}] * 9) and not cap_ok([{"risk_pct": 0.5}] * 10)
    assert drawdown(80, 100) == pytest.approx(20)


def test_clid_deterministic_and_valid():
    a = make_clid("e", "20261110", "SHIB", "paper")
    assert a == make_clid("e", "20261110", "SHIB", "paper") and a.isalnum() and len(a) <= 32


def test_paper_long_round_trip():
    st = Store("sqlite:///:memory:")
    br = PaperBroker(st, None, 10_000)
    f = br.open("ETH", 1, 2.0, 100.0, 90.0, "x")
    assert br.cash == pytest.approx(10_000 - 2 * f["px"] - f["fee"])
    pos = dict(asset="ETH", side=1, qty=2.0, entry_px=f["px"])
    br.close(pos, 110.0, "y")
    assert br.cash > 10_000 + 2 * 9                  # ~ +20 minus fees and slippage


def test_paper_short_pnl():
    st = Store("sqlite:///:memory:")
    br = PaperBroker(st, None, 1_000)
    f = br.open("BTC", -1, 0.01, 50_000, 52_000, "x")
    pos = dict(asset="BTC", side=-1, qty=0.01, entry_px=f["px"])
    br.close(pos, 45_000, "y")
    assert br.cash == pytest.approx(1_000 + 0.01 * (f["px"] - 45_000 * (1 + COSTS.paper_slip)) - f["fee"]
                                   - 0.01 * 45_000 * (1 + COSTS.paper_slip) * COSTS.swap_fee, rel=1e-6)


def _engine(frames, prices, hourly=None):
    s = Settings(); s.paper_enabled = True; s.live_enabled = False
    st = Store("sqlite:///:memory:")
    m = FakeMarket(frames, prices, hourly)
    return Engine(s, st, m, {"paper": PaperBroker(st, m, 10_000)}), st


def test_engine_daily_enters_and_is_idempotent(monkeypatch):
    fr = trend_frame(n=101)
    monkeypatch.setattr("masar.engine.utc_day", lambda ts=None: int(fr.index[-1]) + 1)
    eng, st = _engine({"BTC": fr}, {a: float(fr["c"].iloc[-1]) for a in ASSETS})
    ev = eng.daily()
    assert any(e[0] == "entry" for e in ev)
    n = len(st.positions("paper"))
    assert eng.daily() == [] and len(st.positions("paper")) == n      # second run same day does nothing
    used = sum(p["risk_pct"] for p in st.positions("paper"))
    assert used <= 5.0 + 1e-9


def test_engine_hourly_stop(monkeypatch):
    fr = trend_frame(n=101)
    monkeypatch.setattr("masar.engine.utc_day", lambda ts=None: int(fr.index[-1]) + 1)
    eng, st = _engine({"BTC": fr}, {a: float(fr["c"].iloc[-1]) for a in ASSETS})
    eng.daily()
    p = st.position("paper", "BTC")
    bars = pd.DataFrame([{"ts": p["checked_ts"] + 3_600_001, "o": p["entry_px"], "h": p["entry_px"],
                          "l": p["stop_px"] - 1, "c": p["stop_px"]}])
    eng.market.hourly = {"BTC-USDT": bars}
    ev = eng.hourly()
    t = [e for e in ev if e[0] == "exit"][0][2]
    assert t["reason"] == "stop" and -1.3 < t["r"] < -0.9 and st.position("paper", "BTC") is None


def test_dd_pause(monkeypatch):
    fr = trend_frame(n=101)
    monkeypatch.setattr("masar.engine.utc_day", lambda ts=None: int(fr.index[-1]) + 1)
    eng, st = _engine({"BTC": fr}, {a: float(fr["c"].iloc[-1]) for a in ASSETS})
    st.set("paper_peak", 20_000)                      # equity 10k = 50% below peak
    ev = eng.daily()
    assert st.get("paper_paused") is True and not st.positions("paper")
    assert any(e[0] == "skip" and e[2]["reason"] == "paused" for e in ev)
