"""Safety limits and intrusion detection. None of this changes the strategy rules: in normal
operation no limit is ever reached; they exist to stop a bug, bad data or a stolen API key."""
from dataclasses import dataclass
from .config import ASSETS, SHORTABLE


@dataclass(frozen=True)
class Limits:
    max_notional_pct: float = 100.0     # one position may never exceed 100% of equity (normal: 2-20%)
    max_entries_per_day: int = 10       # the 5% cap already implies <= 10 at 0.5% risk
    max_orders_per_day: int = 30        # entries + exits + closes, per account
    max_price_jump_pct: float = 15.0    # live price vs yesterday's close; beyond this = bad data
    max_fill_slip_pct: float = 3.0      # fill vs reference price; beyond this -> alert
    foreign_value_usd: float = 2.0      # holdings not opened by the bot above this value -> intrusion


LIMITS = Limits()

REASON_AR = {"not_whitelisted": "أصل أو اتجاه غير مسموح", "max_entries_day": "تجاوز حد الصفقات اليومي",
             "max_orders_day": "تجاوز حد الأوامر اليومي", "price_jump": "السعر الحالي بعيد جداً عن إغلاق الأمس (بيانات مشبوهة)",
             "max_notional": "قيمة الصفقة أكبر من الحد الأقصى"}


def allowed_instrument(asset, side):
    return asset in ASSETS and (side == 1 or asset in SHORTABLE)


def check_entry(asset, side, qty, ref_px, last_close, equity, entries_today, orders_today, limits=LIMITS):
    """Returns None if the order may be sent, otherwise a reason code."""
    if not allowed_instrument(asset, side):
        return "not_whitelisted"
    if entries_today >= limits.max_entries_per_day:
        return "max_entries_day"
    if orders_today >= limits.max_orders_per_day:
        return "max_orders_day"
    if last_close > 0 and abs(ref_px / last_close - 1) * 100 > limits.max_price_jump_pct:
        return "price_jump"
    if equity <= 0 or qty * ref_px > equity * limits.max_notional_pct / 100:
        return "max_notional"
    return None


def fill_slip_pct(fill_px, ref_px):
    return abs(fill_px / ref_px - 1) * 100 if ref_px else 0.0


def is_bot_id(cid, account="live"):
    """Every order the bot sends carries a clOrdId starting with 'm' + first letter of the account."""
    return bool(cid) and cid.startswith(f"m{account[0]}")


def find_foreign_activity(snapshot, positions, since_ts, limits=LIMITS):
    """snapshot (from OKXLiveBroker.activity_snapshot): pending orders, pending algos, fills, swap
    positions, spot balances. positions: the bot's open positions (DB). Returns list of findings
    (dict kind, id, detail) for anything the bot did not create."""
    out = []
    known_algos = {p.get("algo_id") for p in positions if p.get("algo_id")}
    pos_assets = {(p["asset"], p["side"]) for p in positions}
    for o in snapshot.get("orders", []):
        if not is_bot_id(o.get("clOrdId")):
            out.append(dict(kind="order", id=o.get("ordId"), detail=f"{o.get('instId')} {o.get('side')} {o.get('sz')}"))
    for a in snapshot.get("algos", []):
        if a.get("algoId") not in known_algos and not is_bot_id(a.get("algoClOrdId")):
            out.append(dict(kind="algo", id=a.get("algoId"), detail=f"{a.get('instId')} {a.get('side')} {a.get('sz')}"))
    for f in snapshot.get("fills", []):
        if int(f.get("ts", 0)) <= since_ts:
            continue
        if not (is_bot_id(f.get("clOrdId")) or is_bot_id(f.get("algoClOrdId"))):
            out.append(dict(kind="fill", id=f.get("billId") or f.get("tradeId"),
                            detail=f"{f.get('instId')} {f.get('side')} {f.get('fillSz')} @ {f.get('fillPx')}"))
    for p in snapshot.get("swap_positions", []):
        asset = p.get("instId", "").split("-")[0]
        if abs(float(p.get("pos") or 0)) > 0 and (asset, -1) not in pos_assets:
            out.append(dict(kind="position", id=p.get("posId") or p.get("instId"), detail=f"{p.get('instId')} {p.get('pos')}"))
    for b in snapshot.get("balances", []):
        ccy = b.get("ccy")
        if ccy in ("USDT", "USDC", None):
            continue
        if float(b.get("eqUsd") or 0) > limits.foreign_value_usd and (ccy, 1) not in pos_assets:
            out.append(dict(kind="balance", id=f"bal-{ccy}", detail=f"{ccy} ≈ {float(b['eqUsd']):.2f}$"))
    return out
