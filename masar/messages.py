"""Arabic Telegram texts (plain text with light emoji; no Markdown to avoid escaping issues)."""
from datetime import datetime, timezone, timedelta
from .journal import BACKTEST, TYPE_AR, loads, r_of, journal_stats, benchmark, protection

RIYADH = timezone(timedelta(hours=3))
ACCT = {"paper": "🧪 المحفظة الافتراضية", "live": "💰 الحساب الحقيقي"}
SIDE = {1: "شراء", -1: "بيع"}
SKIP = {"btc_filter": "فلتر BTC (البيتكوين ليس في نفس الاتجاه)", "cap": "سقف المخاطرة المفتوحة ممتلئ",
        "paused": "البوت متوقف عن الصفقات الجديدة", "below_min": "الكمية أقل من الحد الأدنى للمنصة",
        "price_beyond_stop": "السعر تجاوز مستوى الوقف"}
REASON = {"channel": "كسر قناة 10 أيام", "stop": "ضرب الوقف", "manual": "إغلاق يدوي", "no_stop": "وقف مفقود"}
REG = {1: "صاعد", -1: "هابط", 0: "محايد"}


def ts_txt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=RIYADH).strftime("%Y-%m-%d %H:%M")


def date_txt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=RIYADH).strftime("%d/%m")


def fmt_px(x):
    if x is None:
        return "—"
    if abs(x) >= 100:
        return f"{x:,.2f}"
    if abs(x) >= 1:
        return f"{x:.4f}"
    return f"{x:.8f}".rstrip("0")


def _no(d):
    return f" — صفقة #{d['trade_no']}" if d.get("trade_no") else ""


def _exit_word(side):
    return "تحت قاع" if side == 1 else "فوق قمة"


# ---------- cards ----------
def entry_card(acct, p):
    c = p.get("ctx_obj") or loads(p.get("ctx"))
    side, a = p["side"], p["asset"]
    lvl_word = "اختراق قمة" if side == 1 else "كسر قاع"
    stop_pct = (p["stop_px"] / p["entry_px"] - 1) * 100
    lines = [f"{ACCT.get(acct, acct)}{_no(p)}",
             f"🟢 دخول {SIDE[side]} {a} — {lvl_word} 55 يوماً ({fmt_px(c.get('level'))})", "",
             "📍 التنفيذ",
             f"السعر: {fmt_px(p['entry_px'])} | إغلاق الإشارة: {fmt_px(p.get('signal_close'))}"
             + (f" | انزلاق: {p['entry_slip']:.2f}%" + (" (لصالحك)" if p["entry_slip"] < 0 else "")
                if p.get("entry_slip") is not None else ""),
             f"الوقت: {ts_txt(p['entry_ts'])}", "",
             "🛡️ المخاطرة",
             f"الوقف: {fmt_px(p['stop_px'])} ({stop_pct:+.1f}%) | الخروج عند: إغلاق {_exit_word(side)} 10 أيام ({fmt_px(c.get('exit_level'))})",
             f"الكمية: {p['qty']:.6g} {a} ≈ {c.get('value', p['qty'] * p['entry_px']):,.2f}$"
             + (f" ({c['value_pct']:.1f}% من الرصيد)" if c.get("value_pct") is not None else ""),
             f"المخاطرة: {p['risk_cash']:.2f}$ ({p['risk_pct']:.1f}%)"
             + (f" | المفتوحة الآن: {p['open_risk']:.1f}% من 5%" if p.get("open_risk") is not None else ""),
             f"السوق: {'سبوت' if p['market'] == 'spot' else 'عقود دائمة · معزول 3x'}"]
    if c:
        lines += ["", "🌍 الظروف" + (" (محسوبة بأثر رجعي)" if c.get("backfilled") else ""),
                  f"اتجاه BTC: {REG.get(c.get('btc_reg'), '—')} منذ {c.get('btc_age', 0)} يوم",
                  f"التذبذب (ATR): {c.get('atr_pct', 0):.1f}% يومياً — أعلى من {c.get('atr_rank', 0):.0f}% من أيام السنة",
                  f"قوة الاختراق: الإغلاق بعد المستوى بـ {c.get('strength_pct', 0):.1f}%",
                  f"مراكز مفتوحة بنفس الاتجاه قبلها: {c.get('same_dir', 0)}"]
    return "\n".join(lines)


def exit_card(acct, t):
    side = t["side"]
    icon = "✅" if t["r"] > 0 else "🔻"
    move = (t["exit_px"] / t["entry_px"] - 1) * 100
    days = (t["exit_ts"] - t["entry_ts"]) / 86_400_000
    lines = [f"{ACCT.get(acct, acct)}{_no(t)}",
             f"{icon} خروج {SIDE[side]} {t['asset']} — {REASON.get(t['reason'], t['reason'])}", "",
             "📍 التنفيذ",
             f"الدخول: {fmt_px(t['entry_px'])} ← الخروج: {fmt_px(t['exit_px'])} ({move:+.1f}%)",
             (f"انزلاق الخروج: {t['exit_slip']:.2f}% | " if t.get("exit_slip") is not None else "")
             + f"الرسوم: {t.get('fees') or 0:.2f}$",
             f"المدة: {days:.1f} يوم ({date_txt(t['entry_ts'])} ← {date_txt(t['exit_ts'])})", "",
             "📊 النتيجة",
             f"{t['r']:+.2f}R | {t['pnl']:+.2f}$ صافي"]
    if t.get("mfe_r") is not None:
        lines.append(f"أفضل نقطة: {t['mfe_r']:+.2f}R"
                     + (f" ({date_txt(t['mfe_ts'])})" if t.get("mfe_ts") else "")
                     + f" | أسوأ نقطة: {t.get('mae_r', 0):+.2f}R")
        if t["mfe_r"] > 0.5 and t["r"] > 0:
            lines.append(f"أعادت من ربحها: {(1 - t['r'] / t['mfe_r']) * 100:.0f}%")
    st = t.get("stats")
    lines += ["", "💼 الحساب بعدها"]
    if t.get("equity_after") is not None:
        lines.append(f"الرصيد: {t['equity_after']:,.2f}$")
    if st:
        lines.append(f"الصفقات: {st['n']} | الرابحة: {st['win'] * 100:.0f}% | متوسط R: {st['avg_r']:+.2f} "
                     f"(الباك تست: {BACKTEST['avg_r']:+.2f})")
    les = t.get("lesson_obj") or loads(t.get("lesson"))
    if les:
        lines += ["", f"📝 الدرس — النوع: {TYPE_AR.get(les['type'], les['type'])}"]
        lines += [f"• {x}" for x in les["lines"]]
    return "\n".join(lines)


def event_text(ev):
    kind, acct, data = ev
    head = ACCT.get(acct, acct or "")
    if kind == "entry":
        return entry_card(acct, data)
    if kind == "exit":
        return exit_card(acct, data)
    if kind == "skip":
        return f"{head}\n⏭️ إشارة {data['asset']} مرفوضة: {SKIP.get(data['reason'], data['reason'])}"
    if kind == "alert":
        return f"{head}\n🚨 {data}"
    if kind == "info":
        return f"{head}\nℹ️ {data}"
    return str(data)


# ---------- protection line ----------
def protection_lines(pos, exit_level, short=False):
    pr = protection(pos, exit_level)
    side, entry = pos["side"], pos["entry_px"]
    toward = "ينزل تحت" if side == -1 else "يصعد فوق"
    if pr["protected"]:
        head = f"🛡️ الحماية: {pr['locked_r']:+.2f}R محمية تقريباً"
        if short:
            return [head]
        return [head, f"مستوى الخروج {fmt_px(pr['level'])} أقرب من الوقف — إغلاق يومي بعده يُنهي الصفقة عند حوالي "
                      f"{pr['locked_r']:+.2f}R (يُفحص على الإغلاق اليومي فقط)"]
    if pr["by_exit"]:
        head = f"🛡️ الحماية: الخسارة محدودة بحوالي {pr['locked_r']:+.2f}R"
        detail = [f"مستوى الخروج {fmt_px(pr['level'])} أقرب من الوقف، ويبدأ حماية الربح عندما {toward} {fmt_px(entry)}"]
    else:
        head = "🛡️ الحماية: الربح غير محمي بعد"
        detail = [f"أسوأ نتيجة الآن: {pr['locked_r']:+.2f}R (الوقف {fmt_px(pos['stop_px'])})"]
        if pr["exit_level"] is not None:
            detail.append(f"يبدأ حماية الربح عندما {toward.replace('ينزل', 'ينزل مستوى الخروج').replace('يصعد', 'يصعد مستوى الخروج')} "
                          f"{fmt_px(entry)} (الآن {fmt_px(pr['exit_level'])}، بفارق {fmt_px(abs(pr['gap']))})")
    return [head] if short else [head] + detail


def _exit_level_now(engine, p):
    if p["asset"] not in engine.feat:
        return None
    lv = engine.levels(p["asset"])
    return lv["ll10"] if p["side"] == 1 else lv["hh10"]


# ---------- /trade ----------
def trade_card(engine, no, acct_pref=None, prices=None):
    accts = [acct_pref] if acct_pref else list(engine.brokers)
    for acct in accts:
        p = engine.store.position_by_no(acct, no)
        if p:
            px = (prices or {}).get(p["asset"])
            card = entry_card(acct, p)
            extra = ["", "⏳ الصفقة مفتوحة"]
            if px:
                extra.append(f"السعر الآن: {fmt_px(px)} | النتيجة الآن: {r_of(p, px):+.2f}R")
            ex = _exit_level_now(engine, p)
            if ex is not None:
                extra.append(f"مستوى الخروج الآن: إغلاق {'تحت' if p['side'] == 1 else 'فوق'} {fmt_px(ex)}")
            if p.get("best_px") is not None:
                extra.append(f"أفضل نقطة حتى الآن: {r_of(p, p['best_px']):+.2f}R | أسوأ نقطة: {r_of(p, p['worst_px']):+.2f}R")
            extra += [""] + protection_lines(p, ex)
            return card + "\n" + "\n".join(extra)
        t = engine.store.trade_by_no(acct, no)
        if t:
            ctx = loads(t.get("ctx"))
            entry = entry_card(acct, dict(t, market="spot" if t["side"] == 1 else "swap",
                                          risk_cash=abs(t["entry_px"] - (t.get("stop_px") or t["entry_px"])) * t["qty"],
                                          risk_pct=0.5, ctx_obj=ctx))
            return entry + "\n\n" + "━" * 12 + "\n" + exit_card(acct, t)
    return f"لا توجد صفقة بالرقم #{no}."


# ---------- /journal and monthly ----------
def _types_lines(st):
    lines = []
    for typ in ("big", "short_trend", "false_breakout", "loss_after_profit", "early_exit", "abnormal"):
        n = st["types"].get(typ, 0)
        if n == 0 and typ == "abnormal":
            continue
        share = n / st["n"] * 100
        bt = BACKTEST["type_share"].get(typ)
        lines.append(f"• {TYPE_AR[typ]}: {n} ({share:.0f}%)" + (f" — الباك تست {bt * 100:.0f}%" if bt else ""))
    return lines


def journal_text(engine, since_ts=0, title="📓 سجل الدروس"):
    lines = [title]
    for acct in engine.brokers:
        tr = engine.store.trades(acct, since_ts)
        st = journal_stats(tr)
        if not st:
            lines.append(f"\n{ACCT[acct]}: لا صفقات مغلقة بعد")
            continue
        lines += [f"\n{ACCT[acct]}",
                  f"الصفقات: {st['n']} | الرابحة: {st['win'] * 100:.0f}% (الباك تست {BACKTEST['win_rate'] * 100:.0f}%)",
                  f"متوسط R: {st['avg_r']:+.2f} (الباك تست {BACKTEST['avg_r']:+.2f}) | الربح: {st['pnl']:+.2f}$",
                  f"متوسط المدة: {st['days_median']:.1f} يوم (الباك تست {BACKTEST['days_median']:.0f})"]
        if st["kept_median"] is not None:
            lo, med, hi = BACKTEST["kept_winners"]
            lines.append(f"الرابحة حفظت {st['kept_median'] * 100:.0f}% من أفضل ربحها (الباك تست {lo * 100:.0f}–{hi * 100:.0f}%)")
        if st["slip_avg"] is not None:
            lines.append(f"متوسط الانزلاق: {st['slip_avg']:.2f}% (الافتراض {BACKTEST['slippage_pct']:.2f}%)")
        lines += ["الأنواع:"] + _types_lines(st)
        if st["n"] < 20:
            lines.append(f"ℹ️ العينة صغيرة ({st['n']} صفقات): لا نحكم قبل 20–30 صفقة.")
    return "\n".join(lines)


def monthly_text(engine, since_ts, month_label):
    lines = [f"🎓 ماذا تعلّمنا — {month_label}", journal_text(engine, since_ts, "").strip()]
    flags = []
    for acct in engine.brokers:
        st = journal_stats(engine.store.trades(acct, since_ts))
        if not st or st["n"] < 10:
            continue
        fb = st["types"].get("false_breakout", 0) / st["n"]
        if fb > BACKTEST["type_share"]["false_breakout"] + 0.15:
            flags.append(f"{ACCT[acct]}: الاختراقات الكاذبة {fb * 100:.0f}% أعلى بوضوح من الباك تست — تستحق باك تست على الفترة الأخيرة.")
        if st["slip_avg"] is not None and st["slip_avg"] > 0.15:
            flags.append(f"{ACCT[acct]}: الانزلاق {st['slip_avg']:.2f}% أعلى من حد المرحلة 0.15%.")
        if st["types"].get("abnormal"):
            flags.append(f"{ACCT[acct]}: {st['types']['abnormal']} صفقات بتنفيذ غير طبيعي — راجعها بـ /trade.")
    lines.append("\n🔎 ما يستحق المراجعة:")
    lines += [f"• {f}" for f in flags] or ["• لا شيء — النتائج ضمن المتوقع. لا تغيير في القواعد."]
    lines.append("\nأي تغيير في القواعد يمر بباك تست كامل وموافقتك.")
    return "\n".join(lines)


# ---------- status / positions ----------
def status_text(engine, prices):
    s = engine.store
    lines = ["📊 الحالة"]
    for acct, br in engine.brokers.items():
        eq = br.equity(prices)
        peak = s.get(f"{acct}_peak", eq) or eq
        dd = max(0.0, 1 - eq / peak) * 100 if peak else 0.0
        pos = s.positions(acct)
        risk = sum(p["risk_pct"] for p in pos)
        paused = "⏸️ متوقف" if s.get(f"{acct}_paused", False) else "▶️ يعمل"
        if eq < 1.0:
            lines.append(f"\n{ACCT[acct]} — {paused}\n⚪ بلا رصيد في حساب التداول")
            continue
        lines.append(f"\n{ACCT[acct]} — {paused}\nالرصيد: {eq:,.2f}$ | التراجع: {dd:.1f}%\n"
                     f"المراكز: {len(pos)} | المخاطرة المفتوحة: {risk:.1f}% من 5%")
        for p in pos:
            px = prices.get(p["asset"], p["entry_px"])
            r = p["side"] * p["qty"] * (px - p["entry_px"]) / p["risk_cash"] if p["risk_cash"] else 0
            no = f"#{p['trade_no']} " if p.get("trade_no") else ""
            lines.append(f"  • {no}{SIDE[p['side']]} {p['asset']}: {r:+.2f}R")
    hb = s.get("heartbeat")
    if hb:
        lines.append(f"\nآخر فحص: {ts_txt(hb)}")
    return "\n".join(lines)


def positions_text(engine, prices):
    lines = ["📋 المراكز المفتوحة"]
    for acct in engine.brokers:
        pos = engine.store.positions(acct)
        lines.append(f"\n{ACCT[acct]}" + ("" if pos else ": لا توجد"))
        for p in pos:
            exit_lvl = ""
            if p["asset"] in engine.feat:
                lv = engine.levels(p["asset"])
                ex = lv["ll10"] if p["side"] == 1 else lv["hh10"]
                exit_lvl = f" | الخروج عند إغلاق {'تحت' if p['side'] == 1 else 'فوق'} {fmt_px(ex)}"
            no = f"#{p['trade_no']} " if p.get("trade_no") else ""
            lines.append(f"• {no}{SIDE[p['side']]} {p['asset']} من {fmt_px(p['entry_px'])} | الوقف {fmt_px(p['stop_px'])}"
                         f"{exit_lvl} | منذ {ts_txt(p['entry_ts'])}")
            lines += ["   " + x for x in protection_lines(p, _exit_level_now(engine, p))]
    lines.append("\nللتفاصيل الكاملة: /trade <رقم>")
    return "\n".join(lines)


# ---------- /watch ----------
def watch_text(engine, prices):
    w = engine.watch_data(prices)
    b, px, r = w["btc"], w["btc_px"], w["btc_reg"]
    lines = ["👀 المراقبة", f"اتجاه BTC: {REG[r]}" + {1: " ✅ (الشراء مسموح)", -1: " (بيع BTC/ETH فقط)", 0: " (لا شراء للعملات البديلة)"}[r]]
    if px:
        if r == -1:
            lines.append(f"ينتهي الهبوط عند إغلاق فوق {fmt_px(b['hh20'])} ({(b['hh20'] / px - 1) * 100:+.1f}%)")
            lines.append(f"يتحول للصعود (يُفتح الشراء) عند إغلاق فوق {fmt_px(b['hh55'])} ({(b['hh55'] / px - 1) * 100:+.1f}%)")
        elif r == 1:
            lines.append(f"ينتهي الصعود عند إغلاق تحت {fmt_px(b['ll20'])} ({(b['ll20'] / px - 1) * 100:+.1f}%)")
            lines.append(f"يتحول للهبوط عند إغلاق تحت {fmt_px(b['ll55'])} ({(b['ll55'] / px - 1) * 100:+.1f}%)")
        else:
            lines.append(f"يتحول للصعود عند إغلاق فوق {fmt_px(b['hh55'])} ({(b['hh55'] / px - 1) * 100:+.1f}%)")
            lines.append(f"يتحول للهبوط عند إغلاق تحت {fmt_px(b['ll55'])} ({(b['ll55'] / px - 1) * 100:+.1f}%)")
    lines.append("\n📈 الأقرب لاختراق الشراء")
    for x in w["longs"][:8]:
        note = " — في اتجاه صاعد قائم (لا دخول حتى يتجدد)" if x["in_trend"] else (" — محجوب بفلتر BTC" if x["blocked"] else "")
        d = f"{x['dist']:+.1f}%" if x["dist"] > 0 else f"فوق القمة بالفعل ({-x['dist']:.1f}%)"
        lines.append(f"• {x['asset']}: {d}{note}")
    lines.append("\n📉 الأقرب لكسر البيع (BTC/ETH فقط)")
    for x in w["shorts"]:
        if x["held"]:
            lines.append(f"• {x['asset']}: مركز بيع مفتوح")
        else:
            note = " — في اتجاه هابط قائم (لا دخول حتى يتجدد)" if x["in_trend"] else (" — محجوب بفلتر BTC" if x["blocked"] else "")
            d = f"−{x['dist']:.1f}%" if x["dist"] > 0 else f"تحت القاع بالفعل ({-x['dist']:.1f}%)"
            lines.append(f"• {x['asset']}: {d}{note}")
    lines.append("\n🎯 المراكز المفتوحة")
    if not w["opens"]:
        lines.append("• لا توجد")
    for o in w["opens"]:
        p = o["pos"]
        tag = "🧪" if o["acct"] == "paper" else "💰"
        no = f"#{p['trade_no']} " if p.get("trade_no") else ""
        lines.append(f"• {tag} {no}{SIDE[p['side']]} {p['asset']} ({o['r']:+.2f}R): الخروج عند إغلاق "
                     f"{'تحت' if p['side'] == 1 else 'فوق'} {fmt_px(o['exit'])} ({(o['exit'] / o['px'] - 1) * 100:+.1f}%) | "
                     f"الوقف {fmt_px(p['stop_px'])} ({(p['stop_px'] / o['px'] - 1) * 100:+.1f}%)")
        lines += ["   " + x for x in protection_lines(p, o["exit"], short=True)]
    return "\n".join(lines)


def evening_text(items):
    if not items:
        return None
    lines = ["👀 قبل إغلاق اليوم (03:00 بتوقيت الرياض)", "قريبة من إشارة الليلة:"]
    for it in items:
        if it["kind"] == "long":
            note = "لن تُنفّذ (فلتر BTC)" if it["blocked"] else "قد يُنفّذ شراء"
            lines.append(f"• {it['asset']}: يحتاج {it['dist']:+.1f}% فوق قمة 55 يوماً — {note}")
        elif it["kind"] == "short":
            note = "لن تُنفّذ (فلتر BTC)" if it["blocked"] else "قد يُنفّذ بيع (المحفظة الافتراضية)"
            lines.append(f"• {it['asset']}: يحتاج −{it['dist']:.1f}% تحت قاع 55 يوماً — {note}")
        else:
            tag = "🧪" if it["acct"] == "paper" else "💰"
            lines.append(f"• {tag} {SIDE[it['side']]} {it['asset']} ({it['r']:+.2f}R): على بعد {it['dist']:.1f}% "
                         f"من مستوى الخروج {fmt_px(it['level'])} — قد تُغلق")
    lines.append("القرار النهائي عند إغلاق 03:00. هذه رسالة للمعلومة فقط.")
    return "\n".join(lines)


# ---------- reports ----------
def bench_lines(engine, acct):
    start = engine.bench_start(acct)
    if start is None:
        return []
    try:
        bm = benchmark(engine.store.equity_curve(acct), engine.btc_closes(), start)
    except Exception:
        return []
    if not bm or bm.get(None) is None:
        return ["📈 مقابل البيتكوين: يحتاج يومين على الأقل من البيانات"]
    lines = ["📈 الأداء مقابل البيتكوين (شراء واحتفاظ)"]
    for w, lab in ((None, "منذ البداية"), (7, "آخر 7 أيام"), (30, "آخر 30 يوماً")):
        x = bm.get(w)
        lines.append(f"• {lab}: " + ("—" if not x else f"مسار {x['bot']:+.1f}% | BTC {x['btc']:+.1f}%"))
    x = bm[None]
    lines.append(f"• أقصى تراجع: مسار −{x['bot_dd']:.1f}% | BTC −{x['btc_dd']:.1f}%")
    diff = x["bot"] - x["btc"]
    lines.append(f"• الفارق: {diff:+.1f} نقطة {'لصالح مسار' if diff >= 0 else 'لصالح البيتكوين'} ({x['days']} يوم)")
    return lines


def report_text(engine, since_ts=0, title="📈 التقرير"):
    lines = [title]
    for acct in engine.brokers:
        t = engine.store.trades(acct, since_ts)
        lines.append(f"\n{ACCT[acct]}")
        st = journal_stats(t)
        if not st:
            lines.append("لا صفقات مغلقة في هذه الفترة")
        else:
            lines.append(f"الصفقات: {st['n']} | الرابحة: {st['win'] * 100:.0f}%\n"
                         f"متوسط R: {st['avg_r']:+.2f} (الباك تست: {BACKTEST['avg_r']:+.2f})\nالربح: {st['pnl']:+.2f}$")
        lines += bench_lines(engine, acct)
    return "\n".join(lines)
