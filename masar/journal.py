"""Trade journal: entry context, max favourable/adverse excursion, rule-based lessons, comparison with the
backtest, and performance vs BTC buy-and-hold.

The bot never changes its own rules from these lessons. They are measurements: a change to the strategy
needs a new backtest and the owner's approval."""
import json
import numpy as np

# From the research backtest (tools/reference_backtest.py), final configuration, 2020-07 → 2026-11, 273 trades.
BACKTEST = dict(
    trades=273, win_rate=0.326, avg_r=1.24,
    type_share={"false_breakout": 0.29, "short_trend": 0.25, "loss_after_profit": 0.22, "early_exit": 0.16, "big": 0.08},
    type_avg_r={"false_breakout": -1.01, "short_trend": 1.61, "loss_after_profit": -0.85, "early_exit": -0.92, "big": 18.25},
    kept_winners=(0.19, 0.36, 0.54),     # share of the best open profit kept by winners: q25, median, q75
    mfe_median=1.15, days_median=12.0, top7pct_share=1.14,
    slippage_pct=0.05,
)

TYPE_AR = {"false_breakout": "اختراق كاذب", "short_trend": "اتجاه قصير ناجح", "loss_after_profit": "خسارة بعد ربح",
           "early_exit": "تراجع مبكر", "big": "اتجاه كبير", "abnormal": "تنفيذ غير طبيعي"}
TYPE_NOTE = {
    "false_breakout": "خسارة سريعة بالوقف دون أن تتحرك الصفقة لصالحك — ثمن البحث عن الاتجاهات الكبيرة.",
    "short_trend": "تحرك الاتجاه ثم انتهى، والخروج حفظ جزءاً من الربح.",
    "loss_after_profit": "وصلت لربح جيد ثم انعكست قبل أن يتحرك مستوى الخروج بما يكفي.",
    "early_exit": "خسارة بكسر القناة قبل أن يتطور الاتجاه.",
    "big": "هذه الصفقات النادرة تصنع ربح النظام كله.",
    "abnormal": "التنفيذ خرج عن المتوقع (انزلاق أو فجوة أو إغلاق خاص) — يستحق المراجعة.",
}


def dumps(x):
    return json.dumps(x, ensure_ascii=False)


def loads(x):
    try:
        return json.loads(x) if x else {}
    except Exception:
        return {}


# ---------- entry context ----------
def regime_age(series):
    """Days the last value of a regime series has lasted."""
    v = series.to_numpy()
    if len(v) == 0:
        return 0
    k = len(v) - 1
    while k > 0 and v[k - 1] == v[-1]:
        k -= 1
    return len(v) - k


def entry_context(f, side, btc_reg, open_positions, equity, value):
    """Snapshot of the conditions at entry (f = asset daily features up to the signal day)."""
    last = f.iloc[-1]
    close = float(last["c"])
    level = float(last["hhE"] if side == 1 else last["llE"])
    atr_pct = float(last["atr"]) / close * 100
    hist = (f["atr"] / f["c"] * 100).iloc[-365:].dropna()
    rank = float((hist <= atr_pct).mean() * 100) if len(hist) else 50.0
    exit_lvl = float(f["l"].iloc[-10:].min() if side == 1 else f["h"].iloc[-10:].max())
    return dict(
        level=level, strength_pct=side * (close / level - 1) * 100, atr=float(last["atr"]), atr_pct=atr_pct,
        atr_rank=rank, btc_reg=int(btc_reg.iloc[-1]) if len(btc_reg) else 0, btc_age=regime_age(btc_reg),
        same_dir=sum(1 for p in open_positions if p["side"] == side), equity=equity, value=value,
        value_pct=value / equity * 100 if equity else 0.0, exit_level=exit_lvl,
    )


# ---------- excursions ----------
def update_excursion(pos, bars):
    """bars: confirmed 1H candles after pos['ext_ts']. Updates best/worst price in place. Returns True if changed."""
    if bars is None or len(bars) == 0:
        return False
    side = pos["side"]
    best, worst = pos.get("best_px"), pos.get("worst_px")
    best = best if best is not None else pos["entry_px"]
    worst = worst if worst is not None else pos["entry_px"]
    for b in bars.itertuples():
        fav, adv = (b.h, b.l) if side == 1 else (b.l, b.h)
        if side * (fav - best) > 0:
            best, pos["best_ts"] = fav, int(b.ts)
        if side * (adv - worst) < 0:
            worst, pos["worst_ts"] = adv, int(b.ts)
    pos["best_px"], pos["worst_px"] = best, worst
    pos["ext_ts"] = int(bars.ts.iloc[-1])
    return True


def r_of(pos, px):
    dist = abs(pos["entry_px"] - pos["stop_px"])
    return pos["side"] * (px - pos["entry_px"]) / dist if dist else 0.0


# ---------- lesson ----------
def classify(r, mfe_r, reason, exit_slip, entry_slip):
    if reason in ("no_stop",) or abs(exit_slip or 0) > 0.5 or abs(entry_slip or 0) > 0.5:
        return "abnormal"
    if r >= 5:
        return "big"
    if r < 0 and mfe_r >= 1:
        return "loss_after_profit"
    if r <= -0.8 and mfe_r < 0.5:
        return "false_breakout"
    if r > 0:
        return "short_trend"
    return "early_exit"


def lesson(t):
    """t: closed trade dict with r, mfe_r, reason, exit_slip, entry_slip. Returns dict(type, lines, review)."""
    typ = classify(t["r"], t.get("mfe_r") or 0.0, t["reason"], t.get("exit_slip"), t.get("entry_slip"))
    lines = [TYPE_NOTE[typ]]
    share = BACKTEST["type_share"].get(typ)
    if share:
        lines.append(f"يحدث في {share * 100:.0f}% من صفقات الباك تست — طبيعي.")
    mfe = t.get("mfe_r") or 0.0
    if mfe >= 0.5:
        kept = max(t["r"], 0) / mfe
        lo, med, hi = BACKTEST["kept_winners"]
        verdict = "ضمن المعتاد" if lo <= kept <= hi else ("أفضل من المعتاد" if kept > hi else "أقل من المعتاد")
        lines.append(f"حفظت {kept * 100:.0f}% من أفضل ربح ({mfe:+.2f}R) — {verdict} (الباك تست: {lo * 100:.0f}–{hi * 100:.0f}%).")
    slips = [x for x in (t.get("entry_slip"), t.get("exit_slip")) if x is not None]
    if slips:
        avg = sum(slips) / len(slips)
        lines.append(f"الانزلاق: {avg:.2f}% — {'ضمن' if avg <= 0.15 else 'أعلى من'} حدود المرحلة (0.15%).")
    review = typ == "abnormal"
    lines.append("⚠️ يستدعي المراجعة." if review else "لا يستدعي تغييراً في القواعد.")
    return dict(type=typ, lines=lines, review=review)


# ---------- journal statistics ----------
def journal_stats(trades):
    n = len(trades)
    if n == 0:
        return None
    types = {}
    for t in trades:
        typ = loads(t.get("lesson")).get("type") or classify(t["r"], t.get("mfe_r") or 0, t["reason"],
                                                               t.get("exit_slip"), t.get("entry_slip"))
        types[typ] = types.get(typ, 0) + 1
    winners = [t for t in trades if t["r"] > 0 and (t.get("mfe_r") or 0) > 0]
    kept = [t["r"] / t["mfe_r"] for t in winners] if winners else []
    slips = [x for t in trades for x in (t.get("entry_slip"), t.get("exit_slip")) if x is not None]
    return dict(n=n, win=sum(1 for t in trades if t["r"] > 0) / n, avg_r=sum(t["r"] for t in trades) / n,
                pnl=sum(t["pnl"] for t in trades), types=types,
                kept_median=float(np.median(kept)) if kept else None,
                slip_avg=sum(slips) / len(slips) if slips else None,
                days_median=float(np.median([(t["exit_ts"] - t["entry_ts"]) / 86_400_000 for t in trades])))


# ---------- benchmark vs BTC ----------
def _max_dd(values):
    peak, dd = None, 0.0
    for v in values:
        peak = v if peak is None else max(peak, v)
        if peak and peak > 0:
            dd = max(dd, 1 - v / peak)
    return dd * 100


def benchmark(curve, btc_close, start_day, windows=(None, 7, 30)):
    """curve: [{day, equity}] (equity at the start of `day`), btc_close: Series day -> close.
    The equity of day d is compared with BTC's close of day d-1 (both describe 00:00 UTC of day d)."""
    rows = [(r["day"], r["equity"]) for r in curve if r["day"] >= start_day and r["equity"] > 0]
    out = {}
    if len(rows) < 2:
        return out
    last_day = rows[-1][0]
    for w in windows:
        first = start_day if w is None else last_day - w
        seg = [(d, e) for d, e in rows if d >= first]
        if w is not None and rows[0][0] > first:
            out[w] = None                         # not enough history yet
            continue
        if len(seg) < 2:
            out[w] = None
            continue
        b = [float(btc_close.get(d - 1, np.nan)) for d, _ in seg]
        if np.isnan(b[0]) or np.isnan(b[-1]):
            out[w] = None
            continue
        bot = (seg[-1][1] / seg[0][1] - 1) * 100
        btc = (b[-1] / b[0] - 1) * 100
        out[w] = dict(bot=bot, btc=btc, bot_dd=_max_dd([e for _, e in seg]),
                      btc_dd=_max_dd([x for x in b if not np.isnan(x)]), days=seg[-1][0] - seg[0][0])
    return out
