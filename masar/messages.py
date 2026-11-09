"""Arabic Telegram texts (plain text with light emoji; no Markdown to avoid escaping issues)."""
from datetime import datetime, timezone, timedelta

RIYADH = timezone(timedelta(hours=3))
ACCT = {"paper": "🧪 المحفظة الافتراضية", "live": "💰 الحساب الحقيقي"}
SIDE = {1: "شراء", -1: "بيع"}
SKIP = {"btc_filter": "فلتر BTC (البيتكوين ليس في نفس الاتجاه)", "cap": "سقف المخاطرة المفتوحة ممتلئ",
        "paused": "البوت متوقف عن الصفقات الجديدة", "below_min": "الكمية أقل من الحد الأدنى للمنصة",
        "price_beyond_stop": "السعر تجاوز مستوى الوقف"}
REASON = {"channel": "كسر قناة 10 أيام", "stop": "ضرب الوقف", "manual": "إغلاق يدوي", "no_stop": "وقف مفقود"}


def ts_txt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=RIYADH).strftime("%Y-%m-%d %H:%M")


def fmt_px(x):
    if x >= 100:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}"
    return f"{x:.8f}".rstrip("0")


def event_text(ev):
    kind, acct, data = ev
    head = ACCT.get(acct, acct or "")
    if kind == "entry":
        p = data
        return (f"{head}\n🟢 دخول {SIDE[p['side']]} {p['asset']}\n"
                f"السعر: {fmt_px(p['entry_px'])}\nالوقف: {fmt_px(p['stop_px'])}\n"
                f"الكمية: {p['qty']:.6g}\nالمخاطرة: {p['risk_cash']:.2f}$")
    if kind == "exit":
        t = data
        icon = "✅" if t["r"] > 0 else "🔻"
        days = (t["exit_ts"] - t["entry_ts"]) / 86_400_000
        return (f"{head}\n{icon} خروج {SIDE[t['side']]} {t['asset']} — {REASON.get(t['reason'], t['reason'])}\n"
                f"النتيجة: {t['r']:+.2f}R ({t['pnl']:+.2f}$)\nالمدة: {days:.1f} يوم")
    if kind == "skip":
        return f"{head}\n⏭️ إشارة {data['asset']} مرفوضة: {SKIP.get(data['reason'], data['reason'])}"
    if kind == "alert":
        return f"{head}\n🚨 {data}"
    return str(data)


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
        lines.append(f"\n{ACCT[acct]} — {paused}\nالرصيد: {eq:,.2f}$ | التراجع: {dd:.1f}%\n"
                     f"المراكز: {len(pos)} | المخاطرة المفتوحة: {risk:.1f}% من 5%")
        for p in pos:
            px = prices.get(p["asset"], p["entry_px"])
            r = p["side"] * p["qty"] * (px - p["entry_px"]) / p["risk_cash"] if p["risk_cash"] else 0
            lines.append(f"  • {SIDE[p['side']]} {p['asset']}: {r:+.2f}R")
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
            f = engine.feat.get(p["asset"])
            exit_lvl = ""
            if f is not None:
                lvl = f["llX"].iloc[-1] if p["side"] == 1 else f["hhX"].iloc[-1]
                exit_lvl = f" | الخروج عند إغلاق {'تحت' if p['side'] == 1 else 'فوق'} {fmt_px(float(lvl))}"
            lines.append(f"• {SIDE[p['side']]} {p['asset']} من {fmt_px(p['entry_px'])} | الوقف {fmt_px(p['stop_px'])}"
                         f"{exit_lvl} | منذ {ts_txt(p['entry_ts'])}")
    return "\n".join(lines)


def watch_text(engine, prices):
    rows = []
    for a, f in engine.feat.items():
        lvl = float(f["h"].iloc[-55:].max())        # tomorrow's breakout level = highest high incl. today
        px = prices.get(a)
        if px:
            rows.append((a, (lvl / px - 1) * 100))
    rows.sort(key=lambda x: x[1])
    btc = int(engine.btc_reg.iloc[-1]) if engine.btc_reg is not None else 0
    head = {1: "صاعد ✅ (الشراء مسموح)", -1: "هابط (بيع BTC/ETH فقط)", 0: "محايد (لا شراء للعملات البديلة)"}[btc]
    lines = [f"👀 الأقرب للاختراق\nاتجاه BTC: {head}"]
    for a, dist in rows[:10]:
        lines.append(f"• {a}: يحتاج {dist:+.1f}% للوصول لقمة 55 يوماً")
    return "\n".join(lines)


def report_text(engine, since_ts=0, title="📈 التقرير"):
    lines = [title]
    for acct, br in engine.brokers.items():
        t = engine.store.trades(acct, since_ts)
        n = len(t)
        if n == 0:
            lines.append(f"\n{ACCT[acct]}: لا صفقات مغلقة بعد")
            continue
        wins = sum(1 for x in t if x["r"] > 0)
        avg = sum(x["r"] for x in t) / n
        pnl = sum(x["pnl"] for x in t)
        lines.append(f"\n{ACCT[acct]}\nالصفقات: {n} | الرابحة: {wins / n * 100:.0f}%\n"
                     f"متوسط R: {avg:+.2f} (الباك تست: +1.34)\nالربح: {pnl:+.2f}$")
    return "\n".join(lines)
