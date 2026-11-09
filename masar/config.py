"""All tunable settings in one place. Strategy values are fixed by the design doc and the backtest:
change them only together with a new backtest."""
import os
from dataclasses import dataclass, field

# Priority order when several signals arrive in the same minute (BTC first).
ASSETS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "AVAX", "LINK", "BCH", "LTC", "ADA",
          "HBAR", "INJ", "SEI", "SUI", "CFX", "XLM", "SHIB", "XTZ", "APT", "ALGO", "TRX"]
SHORTABLE = {"BTC", "ETH"}          # shorts only on these, via USDT perpetual swaps


@dataclass(frozen=True)
class Strategy:
    entry_n: int = 55               # breakout channel (days)
    exit_n: int = 10                # exit channel (days)
    atr_n: int = 20                 # daily ATR (Wilder)
    stop_atr: float = 2.0           # initial stop = close -/+ stop_atr x ATR
    btc_entry_n: int = 55           # BTC regime filter: up when close > 55d high ...
    btc_exit_n: int = 20            # ... until close < 20d low
    history_days: int = 400         # daily candles loaded (regime state needs warm-up)


@dataclass(frozen=True)
class Risk:
    risk_pct: float = 0.5           # % of equity at risk per trade
    cap_pct: float = 5.0            # max total open risk %
    dd_pause_pct: float = 20.0      # pause new entries at this drawdown from peak
    short_leverage: int = 3         # isolated margin leverage on swap shorts (risk is set by the stop, not by leverage)


@dataclass(frozen=True)
class Costs:
    spot_fee: float = 0.0010        # OKX regular spot taker
    swap_fee: float = 0.0005        # OKX regular swap taker
    paper_slip: float = 0.0005      # paper fills: 0.05% adverse slippage


def _b(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: int = field(default_factory=lambda: int(os.getenv("TELEGRAM_CHAT_ID", "0") or 0))
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///masar.db"))
    okx_key: str = field(default_factory=lambda: os.getenv("OKX_API_KEY", ""))
    okx_secret: str = field(default_factory=lambda: os.getenv("OKX_API_SECRET", ""))
    okx_passphrase: str = field(default_factory=lambda: os.getenv("OKX_API_PASSPHRASE", ""))
    paper_enabled: bool = field(default_factory=lambda: _b("PAPER_ENABLED", True))
    paper_start_equity: float = field(default_factory=lambda: float(os.getenv("PAPER_START_EQUITY", "10000")))
    paper_shorts: bool = field(default_factory=lambda: _b("PAPER_SHORTS", True))
    live_enabled: bool = field(default_factory=lambda: _b("LIVE_ENABLED", False))
    live_shorts: bool = field(default_factory=lambda: _b("LIVE_SHORTS", False))

    def accounts(self):
        out = []
        if self.paper_enabled:
            out.append("paper")
        if self.live_enabled and self.okx_key:
            out.append("live")
        return out

    def shorts_allowed(self, account):
        return self.paper_shorts if account == "paper" else self.live_shorts


STRATEGY = Strategy()
RISK = Risk()
COSTS = Costs()
