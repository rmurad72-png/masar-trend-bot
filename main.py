"""Entry point: python main.py"""
import logging
from masar.config import Settings
from masar.store import Store
from masar.market import Market
from masar.brokers import PaperBroker, OKXLiveBroker
from masar.engine import Engine
from masar.bot import MasarBot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


def build():
    s = Settings()
    if not s.telegram_token or not s.telegram_chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    store = Store(s.database_url)
    market = Market()
    brokers = {}
    if "paper" in s.accounts():
        brokers["paper"] = PaperBroker(store, market, s.paper_start_equity)
    if "live" in s.accounts():
        brokers["live"] = OKXLiveBroker(store, market, s.okx_key, s.okx_secret, s.okx_passphrase)
    return MasarBot(s, Engine(s, store, market, brokers))


if __name__ == "__main__":
    build().run()
