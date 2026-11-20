import pandas as pd
import pytest
from masar.journal import classify, lesson, update_excursion, r_of, benchmark, journal_stats, regime_age
from masar import messages as M


def test_classify():
    assert classify(6, 7, "channel", 0.01, 0.01) == "big"
    assert classify(-0.9, 1.4, "stop", 0.01, 0.01) == "loss_after_profit"
    assert classify(-1.0, 0.2, "stop", 0.01, 0.01) == "false_breakout"
    assert classify(1.2, 2.0, "channel", 0.01, 0.01) == "short_trend"
    assert classify(-0.5, 0.7, "channel", 0.01, 0.01) == "early_exit"
    assert classify(1.2, 2.0, "channel", 1.2, 0.01) == "abnormal"
    assert classify(-1, 0, "no_stop", None, None) == "abnormal"


def test_lesson_lines():
    les = lesson(dict(r=1.5, mfe_r=2.1, reason="channel", exit_slip=0.04, entry_slip=0.03))
    assert les["type"] == "short_trend" and not les["review"]
    assert any("حفظت 71%" in x for x in les["lines"])


def test_excursion_short():
    pos = dict(side=-1, entry_px=100.0, stop_px=110.0, best_px=None, worst_px=None)
    bars = pd.DataFrame([dict(ts=1, o=100, h=104, l=95, c=96), dict(ts=2, o=96, h=97, l=90, c=91)])
    assert update_excursion(pos, bars)
    assert pos["best_px"] == 90 and pos["worst_px"] == 104 and pos["ext_ts"] == 2
    assert r_of(pos, pos["best_px"]) == pytest.approx(1.0) and r_of(pos, pos["worst_px"]) == pytest.approx(-0.4)


def test_regime_age():
    assert regime_age(pd.Series([0, 1, 1, -1, -1, -1])) == 3


def test_benchmark_windows():
    curve = [dict(day=d, equity=10_000 + 10 * i) for i, d in enumerate(range(100, 110))]
    btc = pd.Series({d: 100.0 - i for i, d in enumerate(range(99, 110))})
    bm = benchmark(curve, btc, 100)
    assert bm[None]["bot"] == pytest.approx(0.9) and bm[None]["btc"] < 0
    assert bm[30] is None and bm[7] is not None


def test_cards_render():
    p = dict(asset="BTC", side=-1, market="swap", qty=0.0111, entry_px=74821.57, stop_px=79362.4, risk_cash=50.42,
             risk_pct=0.5, entry_ts=1794200000000, trade_no=7, signal_close=74800.0, entry_slip=0.03, open_risk=1.0,
             ctx_obj=dict(level=73500, strength_pct=1.8, atr=2258, atr_pct=3.0, atr_rank=70, btc_reg=-1, btc_age=2,
                          same_dir=1, equity=10000, value=831, value_pct=8.3, exit_level=81970))
    txt = M.entry_card("paper", p)
    assert "صفقة #7" in txt and "كسر قاع 55" in txt and "73,500.00" in txt and "معزول 3x" in txt
    t = dict(p, exit_px=70100.0, exit_ts=1794640000000, pnl=75.6, r=1.5, reason="channel", fees=1.2, exit_slip=0.04,
             mfe_r=2.1, mfe_ts=1794550000000, mae_r=-0.35, equity_after=10075.6,
             stats=journal_stats([dict(r=1.5, pnl=75.6, reason="channel", mfe_r=2.1, entry_ts=0, exit_ts=86400000 * 5)]),
             lesson_obj=lesson(dict(r=1.5, mfe_r=2.1, reason="channel", exit_slip=0.04, entry_slip=0.03)))
    txt = M.exit_card("paper", t)
    assert "كسر قناة 10 أيام" in txt and "+1.50R" in txt and "الدرس" in txt and "أعادت من ربحها: 29%" in txt


def test_evening_text():
    assert M.evening_text([]) is None
    txt = M.evening_text([dict(kind="long", asset="INJ", dist=2.1, blocked=True),
                          dict(kind="exit", asset="BTC", acct="paper", side=-1, dist=0.8, level=81200, r=1.5)])
    assert "فلتر BTC" in txt and "قد تُغلق" in txt


def test_protection_states():
    from masar.journal import protection
    short = dict(side=-1, entry_px=75000.0, stop_px=79362.4)
    pr = protection(short, 81200.0)                       # exit level beyond stop -> stop governs
    assert not pr["by_exit"] and pr["locked_r"] == pytest.approx(-1.0) and not pr["protected"]
    pr = protection(short, 77000.0)                       # exit level nearer than stop, still above entry
    assert pr["by_exit"] and -1 < pr["locked_r"] < 0
    pr = protection(short, 70000.0)                       # below entry -> profit locked
    assert pr["protected"] and pr["locked_r"] == pytest.approx((75000 - 70000) / 4362.4)
    lines = M.protection_lines(short, 81200.0)
    assert "غير محمي" in lines[0] and "−" not in lines[0]
    assert "محمية تقريباً" in M.protection_lines(short, 70000.0)[0]
    long = dict(side=1, entry_px=100.0, stop_px=90.0)
    assert protection(long, 105.0)["protected"] and not protection(long, 85.0)["by_exit"]


def test_unknown_command_handler_registered(monkeypatch):
    import os
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    import main
    b = main.build()
    from telegram.ext import MessageHandler
    hs = b.app.handlers[0]
    assert isinstance(hs[-1], MessageHandler)


def test_live_readiness_escalates_once(monkeypatch):
    from tests.fakes import trend_frame, FakeMarket
    from masar.store import Store
    from masar.engine import Engine
    from masar.config import Settings, ASSETS
    fr = trend_frame(n=101)            # BTC breaks out on the last day -> regime up
    st = Store("sqlite:///:memory:")

    class Live:
        account = "live"
        def equity(self, p=None): return 17.0
    eng = Engine(Settings(), st, FakeMarket({"BTC": fr}, {}), {"live": Live()})
    eng.load_features()
    prices = {a: float(fr["c"].iloc[-1]) for a in ASSETS}
    r = eng.live_readiness(prices)
    assert r["state"] == "open" and r["equity"] == 17.0
    txt = M.readiness_text(r)
    assert "الشراء مفتوح" in txt and "أقل من 100$" in txt
    assert eng.live_readiness(prices) is None          # no repeat while the state is unchanged


def test_dd_format_and_empty_bench():
    assert M._dd(0.01) == "0.0%" and M._dd(14.9) == "−14.9%"
