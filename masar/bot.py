"""Telegram interface and scheduler (python-telegram-bot v21, JobQueue)."""
import asyncio
import logging
import time
from datetime import time as dtime, timezone, datetime, timedelta
import csv
import io
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from . import messages as M

log = logging.getLogger(__name__)
HELP = ("🤖 بوت مسار — متابعة الاتجاه\n\n"
        "/status الرصيد والمراكز والتراجع\n/positions تفاصيل المراكز\n/watch المراقبة: BTC والأقرب للدخول والمراكز\n"
        "/report الأداء ومقارنته بالبيتكوين\n/trade <رقم> بطاقة صفقة كاملة (أضف live للحساب الحقيقي)\n"
        "/journal سجل الدروس مقابل الباك تست\n/export ملف CSV بكل الصفقات\n"
        "/pause إيقاف الصفقات الجديدة\n/resume استئنافها\n"
        "/close_all إغلاق كل شيء (يطلب تأكيداً)\n/run_now تشغيل الدورة اليومية الآن (إن لم تُنفّذ اليوم)")


class MasarBot:
    def __init__(self, settings, engine):
        self.s, self.engine = settings, engine
        self.lock = asyncio.Lock()
        self.app = Application.builder().token(settings.telegram_token).build()
        for name, fn in [("start", self.help), ("help", self.help), ("status", self.status),
                         ("positions", self.positions), ("watch", self.watch), ("report", self.report),
                         ("pause", self.pause), ("resume", self.resume), ("close_all", self.close_all),
                         ("run_now", self.run_now), ("trade", self.trade), ("journal", self.journal),
                         ("export", self.export)]:
            self.app.add_handler(CommandHandler(name, self._guard(fn)))
        self.app.add_handler(CallbackQueryHandler(self._guard(self.on_button)))
        self.app.add_handler(MessageHandler(filters.COMMAND, self._guard(self.unknown)))   # after all commands
        jq = self.app.job_queue
        jq.run_daily(self.job_daily, time=dtime(0, 0, 30, tzinfo=timezone.utc), name="daily")
        jq.run_repeating(self.job_hourly, interval=3600, first=self._next_hour_offset(), name="hourly")
        jq.run_daily(self.job_evening, time=dtime(20, 0, 0, tzinfo=timezone.utc), name="evening")   # 23:00 Riyadh
        jq.run_once(self.job_startup, when=5)

    @staticmethod
    def _db_hint():
        """What DATABASE_URL looks like, without the password."""
        import os
        raw = os.getenv("DATABASE_URL")
        if raw is None:
            return "المتغير DATABASE_URL غير موجود في خدمة البوت"
        if not raw.strip():
            return "المتغير موجود لكنه فارغ (المرجع لم يُحل: تحقق من اسم خدمة Postgres)"
        v = raw.strip()
        if "://" in v:
            scheme, rest = v.split("://", 1)
            host = rest.split("@")[-1].split("/")[0]
            return f"{scheme}://***@{host}"
        return f"قيمة غير صحيحة تبدأ بـ: {v[:12]}"

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
        db = "PostgreSQL ✅" if self.s.persistent_db else "ملف مؤقت ⚠️"
        await self.send(f"🚀 بوت مسار يعمل\nالحسابات: {accts}\nالتخزين: {db}")
        if not self.s.trading_allowed:
            await self.send("🚨 لا توجد قاعدة بيانات دائمة (DATABASE_URL فارغ أو غير مربوط).\n"
                            "أوقفت التداول حتى لا تُفتح الصفقات من جديد بعد كل نشر.\n"
                            "الحل في Railway: أضف خدمة PostgreSQL، ثم في متغيرات البوت ضع\n"
                            "DATABASE_URL = ${{Postgres.DATABASE_URL}}\n"
                            f"ما يراه البوت الآن: {self._db_hint()}")
            return
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
        if not self.s.trading_allowed:
            await self.send("🚨 التداول متوقف: لا توجد قاعدة بيانات دائمة. راجع DATABASE_URL في Railway.\n"
                            f"ما يراه البوت الآن: {self._db_hint()}")
            return
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
        now = datetime.now(timezone.utc)
        if now.weekday() == 6:     # Sunday
            since = int((time.time() - 7 * 86400) * 1000)
            await self.send(M.report_text(self.engine, since, "📅 التقرير الأسبوعي (آخر 7 أيام)"))
        if now.day == 1:           # monthly "what did we learn"
            first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            prev = (first - timedelta(days=1)).replace(day=1)
            label = prev.strftime("%Y-%m")
            await self.send(M.monthly_text(self.engine, int(prev.timestamp() * 1000), label))

    async def job_evening(self, ctx):
        if not self.s.trading_allowed:
            return
        try:
            prices = await self._run(self.engine.prices)
            items = await self._run(self.engine.evening_items, prices)
            text = M.evening_text(items)
            if text:
                await self.send(text)
        except Exception as e:
            log.exception("evening")

    async def job_hourly(self, ctx):
        if not self.s.trading_allowed:
            return
        try:
            await self._send_events(await self._run(self.engine.hourly))
        except Exception as e:
            log.exception("hourly")
            await self.send(f"🚨 فشل الفحص الساعي: {e}")

    # ---------- commands ----------
    async def unknown(self, update, ctx):
        await update.message.reply_text("أمر غير معروف — اكتب /help لعرض الأوامر.")

    async def help(self, update, ctx):
        await update.message.reply_text(HELP)

    async def status(self, update, ctx):
        prices = await self._run(self.engine.prices)
        await update.message.reply_text(M.status_text(self.engine, prices))

    async def positions(self, update, ctx):
        prices = await self._run(self.engine.prices)
        await update.message.reply_text(M.positions_text(self.engine, prices))

    async def watch(self, update, ctx):
        await self._run(self.engine.ensure_features)
        prices = await self._run(self.engine.prices)
        await self.send(M.watch_text(self.engine, prices))

    async def report(self, update, ctx):
        await self._run(self.engine.ensure_features)
        await update.message.reply_text(M.report_text(self.engine))

    async def trade(self, update, ctx):
        args = ctx.args or []
        nums = [a.lstrip("#") for a in args if a.lstrip("#").isdigit()]
        if not nums:
            await update.message.reply_text("اكتب رقم الصفقة، مثل: /trade 1  أو  /trade 1 live")
            return
        acct = "live" if any(a.lower() in ("live", "حقيقي") for a in args) else None
        if acct and acct not in self.engine.brokers:
            acct = None
        prices = await self._run(self.engine.prices)
        if acct is None and "paper" in self.engine.brokers:
            acct_try = "paper" if (self.engine.store.position_by_no("paper", int(nums[0]))
                                   or self.engine.store.trade_by_no("paper", int(nums[0]))) else None
            acct = acct_try
        await self.send(M.trade_card(self.engine, int(nums[0]), acct, prices))

    async def journal(self, update, ctx):
        await self.send(M.journal_text(self.engine))

    async def export(self, update, ctx):
        buf = io.StringIO()
        cols = ["account", "trade_no", "status", "asset", "side", "entry_ts", "exit_ts", "entry_px", "exit_px",
                "stop_px", "qty", "pnl", "r", "mfe_r", "mae_r", "reason", "lesson_type", "entry_slip", "exit_slip",
                "fees", "ctx"]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for acct in self.engine.brokers:
            for t in self.engine.store.trades(acct):
                w.writerow(dict(t, status="closed", lesson_type=M.loads(t.get("lesson")).get("type")))
            for p in self.engine.store.positions(acct):
                w.writerow(dict(p, status="open"))
        data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
        await self.app.bot.send_document(self.s.telegram_chat_id, document=data,
                                         filename=f"masar_trades_{datetime.now(timezone.utc):%Y%m%d}.csv",
                                         caption="📤 سجل صفقات مسار (المغلقة والمفتوحة)")

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
