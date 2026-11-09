"""Telegram interface and scheduler (python-telegram-bot v21, JobQueue)."""
import asyncio
import logging
import time
from datetime import time as dtime, timezone, datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from . import messages as M

log = logging.getLogger(__name__)
HELP = ("🤖 بوت مسار — متابعة الاتجاه\n\n"
        "/status الرصيد والمراكز والتراجع\n/positions تفاصيل المراكز\n/watch الأقرب للاختراق\n"
        "/report ملخص الأداء\n/pause إيقاف الصفقات الجديدة\n/resume استئنافها\n"
        "/close_all إغلاق كل شيء (يطلب تأكيداً)\n/run_now تشغيل الدورة اليومية الآن (إن لم تُنفّذ اليوم)")


class MasarBot:
    def __init__(self, settings, engine):
        self.s, self.engine = settings, engine
        self.lock = asyncio.Lock()
        self.app = Application.builder().token(settings.telegram_token).build()
        for name, fn in [("start", self.help), ("help", self.help), ("status", self.status),
                         ("positions", self.positions), ("watch", self.watch), ("report", self.report),
                         ("pause", self.pause), ("resume", self.resume), ("close_all", self.close_all),
                         ("run_now", self.run_now)]:
            self.app.add_handler(CommandHandler(name, self._guard(fn)))
        self.app.add_handler(CallbackQueryHandler(self._guard(self.on_button)))
        jq = self.app.job_queue
        jq.run_daily(self.job_daily, time=dtime(0, 0, 30, tzinfo=timezone.utc), name="daily")
        jq.run_repeating(self.job_hourly, interval=3600, first=self._next_hour_offset(), name="hourly")
        jq.run_once(self.job_startup, when=5)

    @staticmethod
    def _next_hour_offset():
        now = time.time()
        return (3600 - now % 3600) + 300          # HH:05 UTC, after the 1H candle closes

    def _guard(self, fn):
        async def wrapped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            chat = update.effective_chat.id if update.effective_chat else None
            if chat != self.s.telegram_chat_id:
                return
            await fn(update, ctx)
        return wrapped

    async def send(self, text):
        for i in range(0, len(text), 3800):
            try:
                await self.app.bot.send_message(self.s.telegram_chat_id, text[i:i + 3800])
            except Exception:
                log.exception("telegram send failed")

    async def _send_events(self, events):
        for ev in events:
            await self.send(M.event_text(ev))

    async def _run(self, fn, *a):
        async with self.lock:
            return await asyncio.to_thread(fn, *a)

    # ---------- jobs ----------
    async def job_startup(self, ctx):
        log.info("startup job running")
        accts = "، ".join(M.ACCT[a] for a in self.engine.brokers) or "لا يوجد"
        await self.send(f"🚀 بوت مسار يعمل\nالحسابات: {accts}")
        try:
            await self._send_events(await self._run(self.engine.hourly))
            if self.engine.store.get("daily_done") != int(time.time() // 86400):
                await self.job_daily(ctx)
            elif not self.engine.feat:
                await self._run(self.engine.load_features)
        except Exception as e:
            log.exception("startup")
            await self.send(f"🚨 خطأ عند التشغيل: {type(e).__name__}: {e}")

    async def job_daily(self, ctx):
        for attempt in range(15):                         # wait up to ~30 min for the daily candle
            try:
                ready = await self._run(self.engine.data_ready)
            except Exception as e:
                ready = False
                log.warning("data_ready: %s", e)
                if attempt == 0:
                    await self.send(f"⚠️ تعذّر جلب الشموع اليومية من OKX: {type(e).__name__}: {e}\nسأعيد المحاولة كل دقيقتين.")
            if ready:
                break
            await asyncio.sleep(120)
        else:
            await self.send("🚨 شمعة الأمس اليومية لم تتأكد بعد 30 دقيقة. لم تُنفّذ الدورة اليومية.")
            return
        try:
            events = await self._run(self.engine.daily)
        except Exception as e:
            log.exception("daily")
            await self.send(f"🚨 فشلت الدورة اليومية: {e}")
            return
        await self._send_events(events)
        prices = await self._run(self.engine.prices)
        n_in = sum(1 for e in events if e[0] == "entry")
        n_out = sum(1 for e in events if e[0] == "exit")
        await self.send(f"🗓️ التقرير اليومي\nدخول: {n_in} | خروج: {n_out}\n\n" + M.status_text(self.engine, prices))
        if datetime.now(timezone.utc).weekday() == 6:     # Sunday
            since = int((time.time() - 7 * 86400) * 1000)
            await self.send(M.report_text(self.engine, since, "📅 التقرير الأسبوعي (آخر 7 أيام)"))

    async def job_hourly(self, ctx):
        try:
            await self._send_events(await self._run(self.engine.hourly))
        except Exception as e:
            log.exception("hourly")
            await self.send(f"🚨 فشل الفحص الساعي: {e}")

    # ---------- commands ----------
    async def help(self, update, ctx):
        await update.message.reply_text(HELP)

    async def status(self, update, ctx):
        prices = await self._run(self.engine.prices)
        await update.message.reply_text(M.status_text(self.engine, prices))

    async def positions(self, update, ctx):
        prices = await self._run(self.engine.prices)
        await update.message.reply_text(M.positions_text(self.engine, prices))

    async def watch(self, update, ctx):
        if not self.engine.feat:
            await self._run(self.engine.load_features)
        prices = await self._run(self.engine.prices)
        await update.message.reply_text(M.watch_text(self.engine, prices))

    async def report(self, update, ctx):
        await update.message.reply_text(M.report_text(self.engine))

    async def pause(self, update, ctx):
        for a in self.engine.brokers:
            self.engine.store.set(f"{a}_paused", True)
        self.engine.store.event("pause")
        await update.message.reply_text("⏸️ أوقفت الصفقات الجديدة. المراكز المفتوحة تبقى بوقفها.")

    async def resume(self, update, ctx):
        for a in self.engine.brokers:
            self.engine.store.set(f"{a}_paused", False)
            eq = self.engine.store.equity_curve(a)
            if eq:                                         # restart the drawdown count from today
                self.engine.store.set(f"{a}_peak", eq[-1]["equity"])
        self.engine.store.event("resume")
        await update.message.reply_text("▶️ استؤنفت الصفقات الجديدة.")

    async def close_all(self, update, ctx):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("نعم، أغلق الكل", callback_data="close_all_yes"),
                                    InlineKeyboardButton("إلغاء", callback_data="close_all_no")]])
        await update.message.reply_text("⚠️ إغلاق كل المراكز وإيقاف البوت؟", reply_markup=kb)

    async def on_button(self, update, ctx):
        q = update.callback_query
        await q.answer()
        if q.data == "close_all_yes":
            await q.edit_message_text("⏳ جارٍ الإغلاق...")
            await self._send_events(await self._run(self.engine.close_all))
            await self.send("🛑 أُغلقت المراكز وتوقفت الصفقات الجديدة. للاستئناف: /resume")
        else:
            await q.edit_message_text("أُلغي.")

    async def run_now(self, update, ctx):
        await update.message.reply_text("⏳ تشغيل الدورة اليومية...")
        await self.job_daily(ctx)

    def run(self):
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)
