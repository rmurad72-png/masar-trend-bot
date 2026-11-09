"""Entry point: python main.py"""
import logging
import time
from masar.config import Settings
from masar.store import Store
from masar.market import Market
from masar.brokers import PaperBroker, OKXLiveBroker
from masar.engine import Engine
from masar.bot import MasarBot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


def wait_for_lock(s, store):
    """Only one running copy may trade and talk to Telegram. During a Railway redeploy the new copy
    waits here until the old one stops; a permanent second copy (duplicate service or replicas > 1)
    waits forever and warns once in Telegram."""
    t0, warned = time.time(), False
    while not store.try_single_instance_lock():
        if not warned and time.time() - t0 > 300:
            warned = True
            try:
                import urllib.request, urllib.parse, json
                data = urllib.parse.urlencode({"chat_id": s.telegram_chat_id, "text":
                       "⚠️ نسخة ثانية من بوت مسار تعمل على نفس قاعدة البيانات، وهذه النسخة متوقفة في الانتظار.\n"
                       "في Railway: تأكد من وجود خدمة بوت واحدة فقط، وأن Replicas = 1."}).encode()
                urllib.request.urlopen(f"https://api.telegram.org/bot{s.telegram_token}/sendMessage", data, timeout=15)
            except Exception:
                logging.exception("warn send failed")
        logging.warning("another instance holds the lock; waiting")
        time.sleep(15)
    logging.info("single-instance lock acquired")


def build():
    s = Settings()
    if not s.telegram_token or not s.telegram_chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    store = Store(s.database_url)
    wait_for_lock(s, store)
    market = Market()
    brokers = {}
    if "paper" in s.accounts():
        brokers["paper"] = PaperBroker(store, market, s.paper_start_equity)
    if "live" in s.accounts():
        brokers["live"] = OKXLiveBroker(store, market, s.okx_key, s.okx_secret, s.okx_passphrase)
    return MasarBot(s, Engine(s, store, market, brokers))


if __name__ == "__main__":
    build().run()
