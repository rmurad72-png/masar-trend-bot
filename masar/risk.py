"""Position sizing, open-risk cap and the drawdown pause."""
from .config import RISK


def size_position(equity, ref_px, stop, side, risk_pct=RISK.risk_pct):
    """Quantity so that hitting the stop loses risk_pct% of equity. Returns (qty, risk_cash) or (0, 0)."""
    dist = side * (ref_px - stop)
    if dist <= 0 or equity <= 0:
        return 0.0, 0.0
    risk_cash = equity * risk_pct / 100
    qty = risk_cash / dist
    qty = min(qty, equity * 5 / ref_px)          # same notional ceiling as the backtest
    return qty, qty * dist


def cap_ok(open_positions, risk_pct=RISK.risk_pct, cap_pct=RISK.cap_pct):
    used = sum(p["risk_pct"] for p in open_positions)
    return used + risk_pct <= cap_pct + 1e-9


def drawdown(equity, peak):
    return 0.0 if peak <= 0 else max(0.0, 1 - equity / peak) * 100
