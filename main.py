import os
import html
import logging
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    PicklePersistence,
    filters,
)
from supabase import create_client, Client
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# ------------------------------------------------------------------
# ENVIRONMENT & HARDCODED CONFIGURATION
# ------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

ADMIN_USER_IDS = [1622298145, 389487101]

for _name, _val in [("BOT_TOKEN", BOT_TOKEN), ("SUPABASE_URL", SUPABASE_URL), ("SUPABASE_KEY", SUPABASE_KEY)]:
    if not _val:
        raise RuntimeError(f"Missing required environment variable: {_name}")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Ethiopia is UTC+3 year-round (no DST), used for daily schedules
EAT_TIMEZONE = ZoneInfo("Africa/Addis_Ababa")

# ------------------------------------------------------------------
# REQUIRED SUPABASE MIGRATION (run once in the SQL editor)
# ------------------------------------------------------------------
# The app-level "already checked in today?" check is now a fast-path only.
# For a real guarantee against duplicate check-ins (survives races, bugs,
# missing created_at, etc.), run this once against the daily_logs table:
#
#   alter table daily_logs
#     add column log_date date generated always as
#       ((created_at at time zone 'Africa/Addis_Ababa')::date) stored;
#
#   alter table daily_logs
#     add constraint daily_logs_one_per_day unique (client_id, log_date);
#
# After this, a duplicate insert raises an exception that the code below
# catches and reports back to the client as "already checked in" instead of
# silently succeeding or silently failing.

# ------------------------------------------------------------------
# TIER PERMISSIONS DEFINITION (SYNCHRONIZED WITH BOT 1)
# ------------------------------------------------------------------
TIER_PERMISSIONS = {
    "Meal Plan Only (2 Months)": {"allow_media": False, "allow_qa": False, "priority": False},
    "Kickstart (21 Days)": {"allow_media": False, "allow_qa": True, "priority": False},
    "Transformation (60 Days)": {"allow_media": True, "allow_qa": True, "priority": False},
    "Elite Transformation (90 Days)": {"allow_media": True, "allow_qa": True, "priority": True},
    "Lifestyle Coaching (6 Months)": {"allow_media": True, "allow_qa": True, "priority": True},
    "VIP Coaching (6 Months)": {"allow_media": True, "allow_qa": True, "priority": True},
}

# FIX: stable, collision-proof short codes for tier callback_data.
TIER_CODES = {
    "Kickstart (21 Days)": "KS",
    "Transformation (60 Days)": "TRF",
    "Elite Transformation (90 Days)": "ELT",
    "Lifestyle Coaching (6 Months)": "LSC",
    "VIP Coaching (6 Months)": "VIP",
}
TIER_CODES_REVERSE = {v: k for k, v in TIER_CODES.items()}

# FIX: in-process locks to serialize the check-in flow per client.
CHECKIN_LOCKS = defaultdict(asyncio.Lock)

# ------------------------------------------------------------------
# MORNING MOTIVATION MESSAGE POOL (bilingual AM/EN, goal-aware, rotating)
# ------------------------------------------------------------------
def _streak_line(streak: int) -> str:
    if streak <= 0:
        return "\n\n🎬 <i>Start your streak today! / ዛሬ Streak ጀምር!</i>"
    elif streak == 1:
        return f"\n\n🔥 <b>Day {streak}</b> — the streak has begun! / <b>{streak}ኛ ቀን</b> — Streak ጀምሯል!"
    else:
        return f"\n\n🔥 <b>{streak}-day streak</b> and counting! / <b>{streak} ቀናት</b> ተከታታይ ክትትል!"

MOTIVATION_VARIANTS = {
    "fat_loss": [
        lambda streak: (
            "☀️ <b>Main Character Energy! / አዲስ ቀን፣ አዲስ ሌቭል!</b>\n\n"
            "ዛሬ ቀኑን በድል እንወጣዋለን! ሌቭል-አፕ ለማድረግ ዛሬም በጉልበትና በቁርጠኝነት እንነሳ! 🎮🔥\n\n"
            "<i>New day, new lobby! Time to level up and crush today's quest.</i>"
            + _streak_line(streak)
        ),
        lambda streak: (
            "🏃 <b>Warm-Up Complete, Let's Go! / እንሂድ!</b>\n\n"
            "ሰውነትዎ ዝግጁ ነው፣ አእምሮዎም ዝግጁ ነው። ዛሬ የካሎሪ ግቦትን በርትተው ይምቱ! 💪🔥\n\n"
            "<i>Your body's ready, your mind's ready. Go hit today's calorie target with intent.</i>"
            + _streak_line(streak)
        ),
        lambda streak: (
            "🎯 <b>ተግሣጽ ከተነሳሽነት ይበልጣል! / Discipline Beats Motivation!</b>\n\n"
            "የስሜት ሁኔታ ይለዋወጣል፣ ልማድ ግን አይለወጥም። ዛሬ በእቅድዎ ይቀጥሉ! ✨\n\n"
            "<i>Motivation comes and goes — discipline shows up anyway. Stick to the plan today.</i>"
            + _streak_line(streak)
        ),
        lambda streak: (
            "🌅 <b>አንድ እርምጃ ወደ ግብዎ! / One Step Closer!</b>\n\n"
            "የተለወጠ ሰውነት በአንድ ቀን አይገነባም፣ ግን ዛሬ ያደረጉት ምርጫ ወደዚያ ያደርስዎታል። 🚀\n\n"
            "<i>The transformation isn't built in one day — but today's choices get you there.</i>"
            + _streak_line(streak)
        ),
        lambda streak: (
            "⚡ <b>አዲስ ቀን፣ ንጹህ ገጽ! / Fresh Page, New Day!</b>\n\n"
            "ትናንት ትናንት ነው። ዛሬ ደግሞ ራስዎን ለማሻሻል አዲስ ዕድል ነው! ☀️\n\n"
            "<i>Yesterday's done. Today's a clean page to build on.</i>"
            + _streak_line(streak)
        ),
    ],
    "muscle": [
        lambda streak: (
            "🎮 <b>Level Up Time! / ጊዜው ደርሷል!</b>\n\n"
            "ዛሬ ጡንቻ ለመገንባት እና ፕሮቲንዎን ለመምታት ጊዜው ነው! XP እያገኙ ነው! 💪🔥\n\n"
            "<i>Time to grind — hit your protein, build that muscle, earn today's XP.</i>"
            + _streak_line(streak)
        ),
        lambda streak: (
            "🏋️ <b>ብረት ይጠራል! / The Iron is Calling!</b>\n\n"
            "እያንዳንዱ ስብስብ (rep) ወደ ግብዎ ያቀርብዎታል። ዛሬ በጥንካሬ ይስሩ! 🔥\n\n"
            "<i>Every rep gets you closer. Go put in work today.</i>"
            + _streak_line(streak)
        ),
        lambda streak: (
            "🌱 <b>እድገት ጊዜ ይፈልጋል! / Growth Takes Time!</b>\n\n"
            "ጡንቻ የሚገነባው በጅምናዚየም ውስጥ ብቻ ሳይሆን በኩሽናዎም ውስጥ ጭምር ነው። ካሎሪ እና ፕሮቲንዎን ይምቱ! 💪\n\n"
            "<i>Muscle is built in the kitchen as much as the gym — hit your calories and protein today.</i>"
            + _streak_line(streak)
        ),
        lambda streak: (
            "⚙️ <b>ጥንካሬ ይገንቡ፣ ቀን በቀን! / Build Strength, Day by Day!</b>\n\n"
            "ትንንሽ ወጥነት ያላቸው ጥረቶች ትልቅ ውጤት ያመጣሉ። ዛሬ ወጥ ይሁኑ! ✨\n\n"
            "<i>Small consistent effort compounds into real strength. Stay consistent today.</i>"
            + _streak_line(streak)
        ),
        lambda streak: (
            "⚡ <b>አዲስ ቀን፣ አዲስ ስብስብ! / New Day, New Gains!</b>\n\n"
            "ትናንት ያለፈ ነው። ዛሬ ጡንቻ ለመገንባት አዲስ ዕድል ነው! 🚀\n\n"
            "<i>Yesterday's in the books. Today's a fresh shot at building.</i>"
            + _streak_line(streak)
        ),
    ],
}

# ------------------------------------------------------------------
# DUMMY WEB SERVER (Keeps Render Health Checks Active)
# ------------------------------------------------------------------
async def health_check(request):
    return web.Response(text="Server listening on port 8080 & Bot polling started.")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------
def esc(value) -> str:
    if value is None:
        return ""
    return html.escape(str(value))

async def get_client_language(user_id: int) -> str:
    try:
        res = supabase.table("clients").select("language").eq("id", user_id).execute()
        if res.data and len(res.data) > 0:
            return res.data[0].get("language", "am")
    except Exception:
        pass
    return "am"

def parse_supabase_timestamp(ts: str) -> datetime:
    normalized = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def get_today_start_utc() -> str:
    now_eat = datetime.now(EAT_TIMEZONE)
    start_eat = now_eat.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_eat.astimezone(timezone.utc).isoformat()

async def send_message_safely(context: ContextTypes.DEFAULT_TYPE, chat_id: int, **kwargs) -> bool:
    try:
        await context.bot.send_message(chat_id=chat_id, **kwargs)
        return True
    except Forbidden:
        logging.warning(f"🚫 Client {chat_id} blocked the bot. Marking as inactive.")
        try:
            supabase.table("clients").update({"is_active": False}).eq("id", chat_id).execute()
        except Exception as db_err:
            logging.error(f"Failed to set is_active=False for {chat_id}: {db_err}")
        return False
    except Exception as err:
        logging.error(f"Failed to send message to {chat_id}: {err}")
        return False

# ------------------------------------------------------------------
# GLOBAL ERROR HANDLER
# ------------------------------------------------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Unhandled exception while processing an update:", exc_info=context.error)
    try:
        err_summary = f"{type(context.error).__name__}: {str(context.error)}"[:400]
        for admin_id in ADMIN_USER_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"⚠️ <b>Bot Error</b>\n<code>{esc(err_summary)}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
    except Exception:
        pass

# ------------------------------------------------------------------
# BACKGROUND JOBS: MOTIVATION & REMINDERS
# ------------------------------------------------------------------
async def send_morning_motivation(context: ContextTypes.DEFAULT_TYPE):
    """Runs at 8:00 AM EAT. Rotates through a bilingual message pool, picked
    per-client by their goal (fat loss vs muscle) and personalized with
    their current check-in streak."""
    try:
        res = supabase.table("clients").select("id, package, goal").eq("is_active", True).execute()
        if not res.data:
            return

        clients = [c for c in res.data if "Meal Plan Only" not in (c.get("package") or "")]
        if not clients:
            return

        seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        logs_res = supabase.table("daily_logs").select("client_id, created_at").in_("client_id", [c["id"] for c in clients]).gte("created_at", seven_days_ago).execute()

        logs_by_client = defaultdict(set)
        for log in (logs_res.data or []):
            logs_by_client[log["client_id"]].add(parse_supabase_timestamp(log["created_at"]).date())

        # Same variant for everyone on a given goal each day, rotating daily.
        day_index = datetime.now(EAT_TIMEZONE).timetuple().tm_yday

        for client in clients:
            goal_key = "muscle" if client.get("goal") == "goal_muscle" else "fat_loss"
            variants = MOTIVATION_VARIANTS[goal_key]
            template = variants[day_index % len(variants)]
            streak = len(logs_by_client.get(client["id"], set()))
            text = template(streak)

            await send_message_safely(context, chat_id=client["id"], text=text, parse_mode="HTML")
            await asyncio.sleep(0.05)
    except Exception as e:
        logging.error(f"Error sending morning motivation: {e}")

async def send_daily_checkin_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Runs at 8:00 PM EAT"""
    try:
        res = supabase.table("clients").select("id, package").eq("is_active", True).execute()
        if not res.data:
            return

        clients = [c for c in res.data if "Meal Plan Only" not in (c.get("package") or "")]
        # FIX: bail out before hitting the DB with an empty .in_() list, which
        # PostgREST rejects (in.() is invalid syntax) and would otherwise
        # abort the whole reminder run for this slot.
        if not clients:
            return

        today_start = get_today_start_utc()

        logs_res = supabase.table("daily_logs").select("client_id").in_("client_id", [c["id"] for c in clients]).gte("created_at", today_start).execute()
        already_checked_in = {log["client_id"] for log in (logs_res.data or [])}

        text = (
            "🔥 <b>የዕለት ክትትል ሰዓት ደርሷል! / Evening Check-In Time!</b>\n\n"
            "ዛሬም ውሎዎ እንዴት እንደነበረ አጫውቱን፤ ቀኑን በድል እንቋጨው! ✨ ከታች በመጫን የዕለት ክትትልዎን ያድርጉ:\n\n"
            "<i>You're crushing it! Let's log today's progress and finish strong:</i>"
        )

        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Check In Now / አሁኑኑ ይመዝገቡ", callback_data="start_checkin")]])

        for client in clients:
            if client["id"] not in already_checked_in:
                await send_message_safely(context, chat_id=client["id"], text=text, reply_markup=markup, parse_mode="HTML")
                await asyncio.sleep(0.05)
    except Exception as e:
        logging.error(f"Error sending evening reminders: {e}")

async def send_late_night_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Runs at 10:30 PM EAT"""
    try:
        res = supabase.table("clients").select("id, package").eq("is_active", True).execute()
        if not res.data:
            return

        clients = [c for c in res.data if "Meal Plan Only" not in (c.get("package") or "")]
        # FIX: same empty-.in_()-list guard as the evening reminder job.
        if not clients:
            return

        today_start = get_today_start_utc()

        logs_res = supabase.table("daily_logs").select("client_id").in_("client_id", [c["id"] for c in clients]).gte("created_at", today_start).execute()
        already_checked_in = {log["client_id"] for log in (logs_res.data or [])}

        text = (
            "🌙 <b>ቀኑ ሳይጠናቀቅ... / Before the day ends...</b>\n\n"
            "ዕለቱን በድል ለመዝጋት ትንሽ ደቂቃ ካሎት ዛሬ ያደረጉትን ይመዝገቡ! ነገ በአዲስ ጉልበት እንቀጥላለን ✨\n\n"
            "<i>Just a quick reminder to log your day before you head to sleep:</i>"
        )

        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Quick Log / አሁኑኑ ይመዝገቡ", callback_data="start_checkin")]])

        for client in clients:
            if client["id"] not in already_checked_in:
                await send_message_safely(context, chat_id=client["id"], text=text, reply_markup=markup, parse_mode="HTML")
                await asyncio.sleep(0.05)
    except Exception as e:
        logging.error(f"Error sending late night reminders: {e}")

async def send_sunday_admin_report(context: ContextTypes.DEFAULT_TYPE):
    try:
        res = supabase.table("clients").select("id, full_name, package, goal").eq("is_active", True).execute()
        if not res.data: return
        clients = [c for c in res.data if "Meal Plan Only" not in (c.get("package") or "")]
        if not clients: return

        seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        logs_res = supabase.table("daily_logs").select("client_id, created_at").in_("client_id", [c["id"] for c in clients]).gte("created_at", seven_days_ago).execute()

        logs_by_client = defaultdict(set)
        for log in (logs_res.data or []):
            logs_by_client[log["client_id"]].add(parse_supabase_timestamp(log["created_at"]).date())

        report_lines = ["📥 <b>WEEKLY REVIEW QUEUE FOR SCIENTIFIC SIMON</b>\n"]
        for client in clients:
            streak = len(logs_by_client.get(client["id"], set()))
            icon = "💪" if client.get("goal") == "goal_muscle" else "🔥"
            report_lines.append(f"{icon} <b>{esc(client.get('full_name', 'Client'))}</b> ({esc(client.get('package', 'N/A'))})\n• Adherence: {streak} / 7 Days Logged\n")

        for admin_id in ADMIN_USER_IDS:
            await send_message_safely(context, chat_id=admin_id, text="\n".join(report_lines), parse_mode="HTML")
            await asyncio.sleep(0.05)
    except Exception as e:
        logging.error(f"Error generating Sunday report: {e}")

async def check_expirations_and_streaks(context: ContextTypes.DEFAULT_TYPE):
    try:
        res = supabase.table("clients").select("id, package, created_at, package_started_at, renewal_notified, testimonial_notified").eq("is_active", True).execute()
        if not res.data: return
        now = datetime.now(timezone.utc)

        for client in res.data:
            c_id = client["id"]
            pkg = client.get("package", "Meal Plan Only (2 Months)")
            cycle_start_raw = client.get("package_started_at") or client["created_at"]
            days_active = (now - parse_supabase_timestamp(cycle_start_raw)).days

            completion_days = (
                60 if "Meal Plan Only" in pkg else
                (21 if "Kickstart" in pkg else
                (60 if "Transformation" in pkg else
                (90 if "Elite" in pkg else 180)))
            )

            if days_active >= completion_days and not client.get("testimonial_notified"):
                testimonial_text = (
                    "🏆 <b>እንኳን ደስ አለዎት! ሌቭሉን በድል ጨርሰዋል! 🔥</b>\n\n"
                    "ቆይታዎ እንዴት እንደነበረ እና ምን ያህል እንደተለወጡ ማየት ለኛ ትልቅ ደስታ ነው! እጅግ አስደናቂ ሥራ ሠርተዋል 💪\n\n"
                    "በፕሮግራሙ ላይ የነበረዎትን አጠቃላይ ልምድ እና ያገኙትን ለውጥ አጭር የቪዲዮ ወይም የጽሑፍ ምስክርነት (Testimonial) ቢያጋሩን በጣም ደስ ይለናል።"
                )
                success = await send_message_safely(context, chat_id=c_id, parse_mode="HTML", text=testimonial_text)
                if success:
                    supabase.table("clients").update({"testimonial_notified": True}).eq("id", c_id).execute()
                await asyncio.sleep(0.05)

            if not client.get("renewal_notified") and days_active >= (completion_days - 3):
                success = await send_message_safely(
                    context, chat_id=c_id, parse_mode="HTML",
                    text="⚠️ <b>የኮቺንግ ፓኬጅዎ በቅርቡ ይጠናቀቃል! / Package Expiring Soon!</b>\nእቅዶችዎን እና ክትትልዎን መቀጠል እንዲችሉ ከታች በመጫን ያድሱ:",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 ፓኬጅ ማደሻ / Renew Package", callback_data="upgrade_tier")]])
                )
                if success:
                    supabase.table("clients").update({"renewal_notified": True}).eq("id", c_id).execute()
                await asyncio.sleep(0.05)

    except Exception as e:
        logging.error(f"Error checking expirations: {e}")

# ------------------------------------------------------------------
# ADMIN COMMANDS
# ------------------------------------------------------------------
async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS: return
    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/broadcast [message]`", parse_mode="HTML")
        return

    raw_text = update.message.text.split(maxsplit=1)
    body = raw_text[1] if len(raw_text) > 1 else ""
    text = "📢 <b>ANNOUNCEMENT FROM ሳይመን / ማስታወቂያ</b>\n\n" + body

    res = supabase.table("clients").select("id").eq("is_active", True).execute()
    count = 0
    msg = await update.message.reply_text(f"🚀 Broadcasting to {len(res.data or [])} clients...")

    for c in (res.data or []):
        if await send_message_safely(context, chat_id=c["id"], text=text, parse_mode="HTML"): count += 1
        await asyncio.sleep(0.05)

    await msg.edit_text(f"✅ Delivered to {count} active clients.")

async def admin_set_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS: return
    if len(context.args) < 2:
        await update.message.reply_text(f"⚠️ Usage: `/setpackage <id> <tier>`\nTiers: {', '.join(TIER_PERMISSIONS.keys())}")
        return

    target_id, tier_name = context.args[0], " ".join(context.args[1:])
    if tier_name not in TIER_PERMISSIONS:
        await update.message.reply_text("❌ Invalid Tier Name!")
        return

    try:
        supabase.table("clients").update({
            "package": tier_name,
            "package_started_at": datetime.now(timezone.utc).isoformat(),
            "renewal_notified": False,
            "testimonial_notified": False,
        }).eq("id", target_id).execute()
        lang = await get_client_language(int(target_id))
        msg = f"🎉 <b>Package Upgraded!</b>\nYour account has been updated to <b>{tier_name}</b>." if lang == "en" else f"🎉 <b>ፓኬጅዎ ተሻሽሏል! / Package Upgraded!</b>\nመለያዎ ወደ <b>{tier_name}</b> ከፍ ብሏል። እንኳን ደስ አለዎት!"
        await send_message_safely(context, chat_id=int(target_id), text=msg, parse_mode="HTML")
        await update.message.reply_text(f"✅ Client updated to **{tier_name}**!")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Failed: {e}")

async def admin_client_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS: return
    if not context.args: return
    try:
        res = supabase.table("clients").select("*").eq("id", context.args[0]).execute()
        if not res.data:
            await update.message.reply_text("❌ Client not found.")
            return

        c = res.data[0]
        days = (datetime.now(timezone.utc) - parse_supabase_timestamp(c["created_at"])).days
        checkins = len(supabase.table("daily_logs").select("id").eq("client_id", c["id"]).gte("created_at", (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()).execute().data or [])

        info = (
            f"👤 <b>INFO: {esc(c.get('full_name'))}</b>\n"
            f"• <b>ID:</b> {c['id']}\n• <b>Package:</b> {esc(c.get('package'))}\n"
            f"• <b>Goal:</b> {esc(c.get('goal'))}\n• <b>Active:</b> {days} days\n"
            f"• <b>7-Day Checkins:</b> {checkins}/7\n"
            f"• <b>Status:</b> {'🟢' if c.get('is_active') else '🔴'}\n"
            f"• <b>Plan Ready:</b> {'✅ Yes' if c.get('plan_ready') else '⏳ Pending'}\n"
        )
        await update.message.reply_text(info, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

async def admin_send_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return

    doc = update.message.document or (
        update.message.reply_to_message.document if update.message.reply_to_message else None
    )

    if len(context.args) < 2 or not doc:
        await update.message.reply_text(
            "⚠️ Usage: Send the file first (no caption), then reply to it with "
            "`/sendplan [client_id] [meal|workout]`",
            parse_mode="HTML"
        )
        return

    try:
        c_id = int(context.args[0])
        p_type = context.args[1].lower()
    except ValueError:
        await update.message.reply_text("❌ Error: Client ID must be a valid number.")
        return

    if p_type not in ("meal", "workout"):
        await update.message.reply_text("❌ Error: plan type must be `meal` or `workout`.", parse_mode="HTML")
        return

    col = "meal_plan_url" if p_type == "meal" else "workout_plan_url"
    file_id = doc.file_id

    try:
        supabase.table("clients").update({
            col: file_id,
            "plan_ready": True
        }).eq("id", c_id).execute()

        menu_markup = await get_main_menu_markup(c_id)
        await send_message_safely(
            context, chat_id=c_id, parse_mode="HTML",
            text=f"🎉 <b>አዲስ የ{p_type.capitalize()} እቅድ ተጭኗል! / New Plan Updated!</b>\nሳይመን አዲስ እቅድዎን ልኮልዎታል! ከታች ይምረጡ:",
            reply_markup=menu_markup
        )
        await update.message.reply_text(f"✅ Successfully linked {p_type} plan and unlocked client `{c_id}`!", parse_mode="HTML")
    except Exception as e:
        logging.error(f"Failed to update plan in Supabase for client {c_id}: {e}")
        await update.message.reply_text(f"⚠️ Database Failed: {e}")

async def admin_send_voice_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS: return

    voice = update.message.voice or (
        update.message.reply_to_message.voice if update.message.reply_to_message else None
    )

    if not context.args or not voice:
        await update.message.reply_text(
            "⚠️ Usage: Send the voice note first, then reply to it with `/reply [client_id]`",
            parse_mode="HTML"
        )
        return

    try:
        c_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Error: Client ID must be a valid number.")
        return

    try:
        await context.bot.send_voice(chat_id=c_id, voice=voice.file_id, caption="🎙️ <b>ሳይመን የተላከ የድምጽ መልእክት / Voice Feedback from Coach</b>", parse_mode="HTML")
        await update.message.reply_text("✅ Voice note delivered!")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Failed: {e}")

async def admin_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return

    try:
        res = supabase.table("clients").select("full_name, package, is_active, plan_ready, created_at").order("created_at", desc=True).execute()
        clients = res.data or []

        total = len(clients)
        active = sum(1 for c in clients if c.get("is_active"))
        ready_plans = sum(1 for c in clients if c.get("plan_ready"))

        text = (
            f"📊 <b>SYSTEM STATUS OVERVIEW</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• 👥 Total Clients: <b>{total}</b>\n"
            f"• 🟢 Active Users: <b>{active}</b>\n"
            f"• ✅ Plans Ready: <b>{ready_plans}</b>\n\n"
            f"<b>Last 5 Registrations:</b>\n"
        )

        for client in clients[:5]:
            name = esc(client.get("full_name", "Unknown"))
            pkg = esc(client.get("package", "Standard"))
            status = "🟢" if client.get("is_active") else "🔴"
            text += f"• {status} {name} | <i>{pkg}</i>\n"

        await update.message.reply_text(text, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Failed to fetch status: {e}")

async def admin_view_questions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return

    limit = 10
    if context.args:
        try:
            limit = max(1, min(30, int(context.args[0])))
        except ValueError:
            pass

    try:
        res = (
            supabase.table("client_media")
            .select("client_id, message_text, created_at")
            .eq("media_type", "text")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = res.data or []
        if not rows:
            await update.message.reply_text("📭 No saved client questions found.")
            return

        client_ids = list({r["client_id"] for r in rows})
        names_res = supabase.table("clients").select("id, full_name").in_("id", client_ids).execute()
        name_map = {c["id"]: c.get("full_name", "Unknown") for c in (names_res.data or [])}

        lines = [f"📝 <b>RECENT CLIENT QUESTIONS (last {len(rows)})</b>\n"]
        for r in rows:
            name = esc(name_map.get(r["client_id"], "Unknown"))
            when = ""
            try:
                when = parse_supabase_timestamp(r["created_at"]).astimezone(EAT_TIMEZONE).strftime("%b %d, %I:%M %p")
            except Exception:
                pass
            lines.append(f"• <b>{name}</b> (<code>{r['client_id']}</code>){' — ' + when if when else ''}\n  <i>{esc(r.get('message_text', ''))}</i>\n")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        logging.error(f"Error fetching client questions: {e}")
        await update.message.reply_text(f"⚠️ Failed to fetch questions: {e}")

async def admin_send_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return

    doc = update.message.document or (
        update.message.reply_to_message.document if update.message.reply_to_message else None
    )

    if not context.args or not doc:
        await update.message.reply_text(
            "⚠️ Usage: Send the file first (no caption), then reply to it with `/sendfile [client_id]`",
            parse_mode="HTML"
        )
        return

    c_id = context.args[0]
    file_id = doc.file_id
    caption = update.message.caption or "📁 <b>ከሳይመን የተላከ ተጨማሪ ሰነድ / Additional Document from Coach</b>"

    success = await send_message_safely(
        context, chat_id=int(c_id),
        document=file_id,
        caption=caption,
        parse_mode="HTML"
    )

    if success:
        await update.message.reply_text(f"✅ Document successfully delivered to client `{c_id}`!", parse_mode="HTML")
    else:
        await update.message.reply_text(f"❌ Failed to send document. Check if the client ID is correct or if they blocked the bot.")

async def admin_test_motivation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually fires the 8:00 AM morning motivation job right now, so you
    don't have to wait for the scheduler. Sends to all active, non-Meal-Plan
    clients exactly as the real job would."""
    if update.effective_user.id not in ADMIN_USER_IDS:
        return
    await update.message.reply_text("🧪 Running morning motivation job now...")
    try:
        await send_morning_motivation(context)
        await update.message.reply_text("✅ Done. Check the logs above for who received it.")
    except Exception as e:
        logging.error(f"Manual motivation test failed: {e}")
        await update.message.reply_text(f"⚠️ Test failed: {e}")

async def admin_test_nudge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually fires the 8:00 PM evening check-in nudge right now. Only
    reaches clients who haven't logged today, exactly like the real job —
    so if you already checked in as a test client, you won't get it."""
    if update.effective_user.id not in ADMIN_USER_IDS:
        return
    await update.message.reply_text("🧪 Running evening check-in nudge job now (skips anyone already checked in today)...")
    try:
        await send_daily_checkin_reminders(context)
        await update.message.reply_text("✅ Done.")
    except Exception as e:
        logging.error(f"Manual nudge test failed: {e}")
        await update.message.reply_text(f"⚠️ Test failed: {e}")

async def admin_test_scheduled_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually fires one of the scheduled broadcast jobs right now, so you
    don't have to wait for its cron time to see what it sends.
    Usage: /testjob <morning|evening|latenight|sunday|expirations>"""
    if update.effective_user.id not in ADMIN_USER_IDS:
        return

    jobs = {
        "morning": send_morning_motivation,
        "evening": send_daily_checkin_reminders,
        "latenight": send_late_night_reminders,
        "sunday": send_sunday_admin_report,
        "expirations": check_expirations_and_streaks,
    }

    if not context.args or context.args[0].lower() not in jobs:
        await update.message.reply_text(
            f"⚠️ Usage: `/testjob <name>`\nAvailable: {', '.join(jobs.keys())}\n\n"
            "This runs the real job right now, against real client data — it will "
            "actually message clients (e.g. anyone not yet checked in today gets a "
            "reminder). Use with that in mind.",
            parse_mode="HTML"
        )
        return

    job_name = context.args[0].lower()
    msg = await update.message.reply_text(f"🧪 Running `{job_name}` job now...", parse_mode="HTML")
    try:
        await jobs[job_name](context)
        await msg.edit_text(f"✅ `{job_name}` job finished. Check the relevant chats.", parse_mode="HTML")
    except Exception as e:
        logging.error(f"Manual job trigger '{job_name}' failed: {e}")
        await msg.edit_text(f"⚠️ `{job_name}` job failed: {e}", parse_mode="HTML")

# ------------------------------------------------------------------
# CLIENT COMMANDS & VIEWS (WITH GATEKEEPER CHECK)
# ------------------------------------------------------------------
async def get_main_menu_markup(user_id: int):
    lang = await get_client_language(user_id)
    if lang == "en":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📖 My Target Plan", callback_data="get_target_plan")],
            [InlineKeyboardButton("👤 My Profile & Status", callback_data="get_client_profile")],
            [InlineKeyboardButton("📊 Daily Check-In", callback_data="start_checkin")],
            [InlineKeyboardButton("🔄 Upgrade Package", callback_data="upgrade_tier")],
            [InlineKeyboardButton("🌐 Switch to Amharic (አማርኛ)", callback_data="set_lang_am")]
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📖 የእኔ እቅድ / My Target Plan", callback_data="get_target_plan")],
            [InlineKeyboardButton("👤 መለያዬ / My Profile", callback_data="get_client_profile")],
            [InlineKeyboardButton("📊 የዕለት ክትትል / Daily Check-In", callback_data="start_checkin")],
            [InlineKeyboardButton("🔄 ፓኬጅ ማሻሻያ / Upgrade Package", callback_data="upgrade_tier")],
            [InlineKeyboardButton("🌐 ወደ እንግሊዝኛ ቀይር (Switch to English)", callback_data="set_lang_en")]
        ])

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    try:
        res = supabase.table("clients").select("plan_ready, language").eq("id", user_id).execute()
        if res.data and len(res.data) > 0:
            client_record = res.data[0]
            plan_is_ready = client_record.get("plan_ready", False)
            lang = client_record.get("language", "am")
        else:
            plan_is_ready = False
            lang = "am"
    except Exception as e:
        logging.error(f"Gatekeeper check error for {user_id}: {e}")
        plan_is_ready = False
        lang = "am"

    if not plan_is_ready:
        if lang == "am":
            wait_text = (
                "⏳ <b>ዕቅድዎ በመዘጋጀት ላይ ይገኛል!</b>\n\n"
                "📋 ሰላም! መረጃዎ እና ክፍያዎ ተረጋግጦ ወደ ሲስተሙ ገብቷል። ሳይመን አሁን ለእርስዎ የሚስማማውን ልዩ እቅድ በጥንቃቄ በማዘጋጀት ላይ ይገኛል!\n\n"
                "🍽️ <b>አንድ ትንሽ ጥያቄ (ስምዎን በመጥቀስ):</b> ዕቅድዎ ከእርስዎ የዕለት ተዕለት ሕይወት እና ከምርጫዎችዎ ጋር እንዲጣጣም፣ <b>ብዙውን ጊዜ ምን ዓይነት ምግቦችን ይመገባሉ?</b>\n\n"
                "👉 <b>እባክዎ ስምዎን በመጥቀስ አሁን በጽሑፍ ወይም በድምጽ መልእክት (Voice Note) ልኩልኝ!</b>\n\n"
                "⏰ ዕቅድዎ ሲጠናቀቅ እና ወደ ቦቱ ሲጫን በቀጥታ ማሳወቂያ ይደርስዎታል!"
            )
        else:
            wait_text = (
                "⏳ <b>Your Plan is Under Construction!</b>\n\n"
                "📋 Hello! Your intake and payment are confirmed. Simon is currently building your customized workout and nutrition plan with care.\n\n"
                "🛒 <b>Quick question (Please state your name):</b> To tailor your nutrition plan to your routine and food preferences, <b>what do your typical meals look like?</b>\n\n"
                "👉 <b>Please mention your name and drop me a quick text or voice note right here!</b>\n\n"
                "⏰ You will receive an instant notification here the moment your plan is finalized and uploaded!"
            )

        support_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("📲 ከሳይመን ጋር መነጋገር / Contact Simon", url="https://t.me/s_simon_19")]
        ])
        await update.message.reply_text(wait_text, reply_markup=support_markup, parse_mode="HTML")
        return

    markup = await get_main_menu_markup(user_id)
    await update.message.reply_text(
        "እንኳን ወደ ሲሞን ኦሪጅን የክትትል እና ኮቺንግ ፖርታል በደህና መጡ! 🎯\n"
        "Welcome to Simon Origin Tracking & Coaching Portal! 🎯\n\n"
        "ቋንቋ ለመቀየር ወይም ለመጀመር ከታች ያሉትን ይጫኑ / Select an option below:",
        reply_markup=markup,
    )

async def checkin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /checkin text command by opening the daily check-in flow."""
    user = update.effective_user
    if not user:
        return

    try:
        today_start = get_today_start_utc()
        if supabase.table("daily_logs").select("id").eq("client_id", user.id).gte("created_at", today_start).execute().data:
            await update.message.reply_text(
                "✅ <b>ዛሬ ቀድመው ተመዝግበዋል! / You've already checked in today!</b>\nአስደናቂ ወጥነት! Streak እንዳይቋረጥ ነገ ይመለሱ! 🔥",
                parse_mode="HTML"
            )
            return

        res = supabase.table("clients").select("goal").eq("id", user.id).execute()
        goal = res.data[0].get("goal") if res.data else "goal_fat_loss"
    except Exception as e:
        logging.error(f"Error in checkin_command for {user.id}: {e}")
        goal = "goal_fat_loss"

    if goal == "goal_muscle":
        kb = [
            [InlineKeyboardButton("🎯 ፕሮቲን እና ካሎሪ ሞልቻለሁ", callback_data="log_nut_hit")],
            [InlineKeyboardButton("⚠️ ፕሮቲን/ካሎሪ አጎድያለሁ", callback_data="log_nut_miss")]
        ]
        text = "🔔 <b>የጡንቻ ግንባታ ክትትል / MUSCLE BUILDING CHECK-IN</b>\nየፕሮቲን እና ካሎሪ መጠንዎን ሞልተዋል? / Did you hit your protein & calorie target?"
    else:
        kb = [
            [InlineKeyboardButton("🎯 የካሎሪ ገደብ ጠብቄአለሁ", callback_data="log_nut_hit")],
            [InlineKeyboardButton("⚠️ የካሎሪ ገደብ አልጠበቅሁም", callback_data="log_nut_miss")]
        ]
        text = "🔔 <b>የስብ መቀነስ ክትትል / FAT LOSS CHECK-IN</b>\nየካሎሪ ገደብዎን ጠብቀዋል? / Did you stay within your calorie deficit?"

    try:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except Exception as err:
        logging.error(f"Failed to send checkin prompt to {user.id}: {err}")

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /profile text command to display client details."""
    user = update.effective_user
    if not user:
        return

    lang = await get_client_language(user.id)
    try:
        res = supabase.table("clients").select("*").eq("id", user.id).execute()
        if not res.data:
            await update.message.reply_text(
                "📋 Profile not found. Please register first!" if lang == "en" else "📋 መረጃዎ አልተገኘም። እባክዎ መጀመሪያ ይመዝገቡ!"
            )
            return

        c = res.data[0]
        days = (datetime.now(timezone.utc) - parse_supabase_timestamp(c["created_at"])).days
        checkins = len(
            supabase.table("daily_logs")
            .select("id")
            .eq("client_id", c["id"])
            .gte("created_at", (datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
            .execute().data or []
        )

        if lang == "en":
            text = (
                f"👤 <b>YOUR COACHING PROFILE</b>\n\n"
                f"• <b>Name:</b> {esc(c.get('full_name'))}\n"
                f"• <b>Package:</b> {esc(c.get('package'))}\n"
                f"• <b>Goal:</b> {esc(c.get('goal'))}\n"
                f"• <b>Days Active:</b> {days}\n"
                f"• <b>7-Day Check-ins:</b> {checkins}/7\n"
            )
        else:
            text = (
                f"👤 <b>የኮቺንግ መለያዎ / YOUR PROFILE</b>\n\n"
                f"• <b>ስም:</b> {esc(c.get('full_name'))}\n"
                f"• <b>የፓኬጅ ዓይነት:</b> {esc(c.get('package'))}\n"
                f"• <b>ዋና ግብ:</b> {esc(c.get('goal'))}\n"
                f"• <b>የቆይታ ጊዜ:</b> {days} ቀናት\n"
                f"• <b>የ7 ቀን ክትትል:</b> {checkins}/7 ቀናት\n"
            )
        await update.message.reply_text(text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Error loading profile for {user.id}: {e}")
        await update.message.reply_text("⚠️ Error loading profile.")

async def handle_language_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    new_lang = "en" if query.data == "set_lang_en" else "am"
    try:
        supabase.table("clients").update({"language": new_lang}).eq("id", query.from_user.id).execute()
    except Exception: pass

    markup = await get_main_menu_markup(query.from_user.id)
    text = "Language switched to English! 🇬🇧" if new_lang == "en" else "ቋንቋ ወደ አማርኛ ተቀይሯል! 🇪🇹"
    await query.message.edit_text(f"✅ <b>{text}</b>\n\nእንኳን ወደ ሲሞን ኦሪጅን ፖርታል በደህና መጡ! ከታች ይምረጡ:", reply_markup=markup, parse_mode="HTML")

async def handle_client_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = await get_client_language(query.from_user.id)
    try:
        res = supabase.table("clients").select("*").eq("id", query.from_user.id).execute()
        if not res.data:
            await query.message.reply_text("📋 Profile not found. Please register first!" if lang == "en" else "📋 መረጃዎ አልተገኘም። እባክዎ መጀመሪያ ይመዝገቡ!")
            return

        c = res.data[0]
        days = (datetime.now(timezone.utc) - parse_supabase_timestamp(c["created_at"])).days
        checkins = len(supabase.table("daily_logs").select("id").eq("client_id", c["id"]).gte("created_at", (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()).execute().data or [])

        if lang == "en":
            text = (f"👤 <b>YOUR COACHING PROFILE</b>\n\n• <b>Name:</b> {esc(c.get('full_name'))}\n• <b>Package:</b> {esc(c.get('package'))}\n"
                    f"• <b>Goal:</b> {esc(c.get('goal'))}\n• <b>Days Active:</b> {days}\n• <b>7-Day Check-ins:</b> {checkins}/7\n")
        else:
            text = (f"👤 <b>የኮቺንግ መለያዎ / YOUR PROFILE</b>\n\n• <b>ስም:</b> {esc(c.get('full_name'))}\n• <b>የፓኬጅ ዓይነት:</b> {esc(c.get('package'))}\n"
                    f"• <b>ዋና ግብ:</b> {esc(c.get('goal'))}\n• <b>የቆይታ ጊዜ:</b> {days} ቀናት\n• <b>የ7 ቀን ክትትል:</b> {checkins}/7 ቀናት\n")
        await query.message.reply_text(text, parse_mode="HTML")
    except Exception:
        await query.message.reply_text("⚠️ Error loading profile.")

async def handle_target_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = await get_client_language(query.from_user.id)
    try:
        res = supabase.table("clients").select("*").eq("id", query.from_user.id).execute()
        if not res.data: return
        c = res.data[0]

        is_meal_only = "Meal Plan Only" in c.get("package", "")
        await query.message.reply_text("📋 <b>NUTRITION BLUEPRINT / የምግብ እቅድ</b>" if is_meal_only else "📋 <b>FULL COACHING BLUEPRINT / የኮቺንግ እቅድ</b>", parse_mode="HTML")

        if c.get("meal_plan_url"):
            await context.bot.send_document(chat_id=query.from_user.id, document=c["meal_plan_url"], caption="🔗 Meal Plan")
        else:
            await query.message.reply_text("🔗 Meal Plan: Not Uploaded Yet" if lang == "en" else "🔗 የምግብ እቅድ፦ እስካሁን አልተጫነም")

        if not is_meal_only:
            if c.get("workout_plan_url"):
                await context.bot.send_document(chat_id=query.from_user.id, document=c["workout_plan_url"], caption="🏋️ Workout Plan")
            else:
                await query.message.reply_text("🏋️ Workout Plan: Not Uploaded Yet" if lang == "en" else "🏋️ የአካል ብቃት እቅድ፦ እስካሁን አልተጫነም")
    except Exception:
        await query.message.reply_text("⚠️ Error fetching plans.")

# ------------------------------------------------------------------
# UPGRADE & PAYMENT FLOW (WITH ERROR SAFETY & DYNAMIC CURRENCY)
# ------------------------------------------------------------------
async def handle_upgrade_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u_id = query.from_user.id

    context.user_data.pop("pending_tier", None)
    context.user_data["awaiting_payment_screenshot"] = False

    loc_type = "et"
    current_package = None
    try:
        res = supabase.table("clients").select("location_type, package").eq("id", u_id).execute()
        if res.data and len(res.data) > 0:
            loc_type = res.data[0].get("location_type", "et")
            current_package = res.data[0].get("package")
    except Exception as e:
        logging.error(f"Error checking location_type for upgrade {u_id}: {e}")

    tier_rows_et = [
        ("Kickstart (21 Days)", "⚡ Kickstart (21 Days) — 4,500 ETB", "upgrade_kickstart"),
        ("Transformation (60 Days)", "🔥 Transformation (60 Days) — 8,900 ETB", "upgrade_transformation"),
        ("Elite Transformation (90 Days)", "🥇 Elite (90 Days) — 12,500 ETB", "upgrade_elite"),
        ("Lifestyle Coaching (6 Months)", "🌟 Lifestyle (6 Months) — 24,000 ETB", "upgrade_lifestyle"),
        ("VIP Coaching (6 Months)", "👑 VIP Coaching (6 Months) — 39,000 ETB", "upgrade_vip"),
    ]
    tier_rows_intl = [
        ("Kickstart (21 Days)", "⚡ Kickstart (21 Days) — $50", "upgrade_kickstart"),
        ("Transformation (60 Days)", "🔥 Transformation (60 Days) — $119", "upgrade_transformation"),
        ("Elite Transformation (90 Days)", "🥇 Elite (90 Days) — $159", "upgrade_elite"),
        ("Lifestyle Coaching (6 Months)", "🌟 Lifestyle (6 Months) — $299", "upgrade_lifestyle"),
        ("VIP Coaching (6 Months)", "👑 VIP Coaching (6 Months) — $549", "upgrade_vip"),
    ]

    rows = tier_rows_et if loc_type == "et" else tier_rows_intl
    keyboard = []
    for (tier_name, label, cb) in rows:
        if tier_name == current_package:
            keyboard.append([InlineKeyboardButton(f"{label} (🔁 Renew Same Plan)", callback_data=cb)])
        else:
            keyboard.append([InlineKeyboardButton(label, callback_data=cb)])
    contact_label = "📲 ከሳይመን ጋር መነጋገር" if loc_type == "et" else "📲 Contact Simon"
    keyboard.append([InlineKeyboardButton(contact_label, url="https://t.me/s_simon_19")])

    try:
        await query.message.reply_text(
            "🏋️ <b>ፓኬጅ ማሻሻያ / UPGRADE TO FULL COACHING</b>\n\n"
            "ሙሉ የ 1-ለ-1 ክትትል፣ የፎርም ግምገማ፣ የድምጽ ኦዲት እና የተስተካከሉ እቅዶችን ያግኙ!\n"
            "Unlock full 1-on-1 coaching, form reviews, voice audits, and custom plans!\n\n"
            "ከታች የሚፈልጉትን ፓኬጅ ይምረጡ / Select your tier below:",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )
    except Exception as err:
        logging.error(f"Failed to render upgrade menu for {u_id}: {err}")

async def handle_upgrade_payment_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u_id = query.from_user.id

    loc_type = "et"
    try:
        res = supabase.table("clients").select("location_type").eq("id", u_id).execute()
        if res.data and len(res.data) > 0:
            loc_type = res.data[0].get("location_type", "et")
    except Exception:
        pass

    tier_map = {
        "upgrade_kickstart": "Kickstart (21 Days)",
        "upgrade_transformation": "Transformation (60 Days)",
        "upgrade_elite": "Elite Transformation (90 Days)",
        "upgrade_lifestyle": "Lifestyle Coaching (6 Months)",
        "upgrade_vip": "VIP Coaching (6 Months)"
    }
    sel = tier_map.get(query.data, "Transformation (60 Days)")
    context.user_data["pending_tier"] = sel
    context.user_data["awaiting_payment_screenshot"] = True

    if loc_type == "et":
        text = (
            f"💳 <b>ክፍያ መፈጸሚያ / UPGRADE TO {sel.upper()}</b>\n\n"
            f"ክፍያውን ከዚህ በታች ባሉት አካውንቶች ያስተላልፉ:\n"
            f"• <b>ሲቢኢ (CBE):</b> 1000357796532 (Simon Mulugeta)\n"
            f"• <b>ቴሌብር (Telebirr):</b> 0939998090 (Simon Mulugeta)\n\n"
            f"📸 <b>ቀጣይ እርምጃ / Next Step:</b> የክፍያ ማረጋገጫ (Receipt) ስክሪንሾትዎን ወደዚህ ቻት ይላኩ!"
        )
    else:
        text = (
            f"💳 <b>Payment Instructions / UPGRADE TO {sel.upper()}</b>\n\n"
            f"Since you are joining from abroad, please contact Simon directly or use your international payment option (Grey account) to complete your checkout.\n\n"
            f"📲 <b>Contact Simon to pay:</b> https://t.me/s_simon_19\n\n"
            f"📸 Once completed, send your payment confirmation receipt screenshot to this chat!"
        )

    await query.message.reply_text(text, parse_mode="HTML")

# ------------------------------------------------------------------
# DAILY CHECK-IN FLOW (WITH TEXT INPUT FOR CLIENTS)
# ------------------------------------------------------------------
async def trigger_daily_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u_id = query.from_user.id

    try:
        today_start = get_today_start_utc()
        if supabase.table("daily_logs").select("id").eq("client_id", u_id).gte("created_at", today_start).execute().data:
            await query.message.reply_text("✅ <b>ዛሬ ቀድመው ተመዝግበዋል! / You've already checked in today!</b>\nአስደናቂ ወጥነት! Streak እንዳይቋረጥ ነገ ይመለሱ! 🔥", parse_mode="HTML")
            return

        res = supabase.table("clients").select("goal").eq("id", u_id).execute()
        goal = res.data[0].get("goal") if res.data else "goal_fat_loss"
    except Exception as e:
        logging.error(f"Error in trigger_daily_checkin for {u_id}: {e}")
        goal = "goal_fat_loss"

    if goal == "goal_muscle":
        kb = [[InlineKeyboardButton("🎯 ፕሮቲን እና ካሎሪ ሞልቻለሁ", callback_data="log_nut_hit")], [InlineKeyboardButton("⚠️ ፕሮቲን/ካሎሪ አጎድያለሁ", callback_data="log_nut_miss")]]
        text = "🔔 <b>የጡንቻ ግንባታ ክትትል / MUSCLE BUILDING CHECK-IN</b>\nየፕሮቲን እና ካሎሪ መጠንዎን ሞልተዋል? / Did you hit your protein & calorie target?"
    else:
        kb = [[InlineKeyboardButton("🎯 የካሎሪ ገደብ ጠብቄአለሁ", callback_data="log_nut_hit")], [InlineKeyboardButton("⚠️ የካሎሪ ገደብ አልጠበቅሁም", callback_data="log_nut_miss")]]
        text = "🔔 <b>የስብ መቀነስ ክትትል / FAT LOSS CHECK-IN</b>\nየካሎሪ ገደብዎን ጠብቀዋል? / Did you stay within your calorie deficit?"

    try:
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except Exception as err:
        logging.error(f"Failed to send checkin prompt to {u_id}: {err}")

async def handle_checkin_responses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    u_id = query.from_user.id

    if query.data in ["log_nut_hit", "log_nut_miss"]:
        context.user_data["checkin_nut"] = "Hit" if query.data == "log_nut_hit" else "Missed"

        try:
            res = supabase.table("clients").select("goal").eq("id", u_id).execute()
            goal = res.data[0].get("goal", "goal_fat_loss") if res.data else "goal_fat_loss"
        except Exception as e:
            logging.error(f"Error fetching goal for {u_id} in checkin flow: {e}")
            goal = "goal_fat_loss"

        if goal == "goal_muscle":
            kb = [[InlineKeyboardButton("💤 7+ ሰዓት ተኝቻለሁ", callback_data="log_second_hit")], [InlineKeyboardButton("❌ በቂ እረፍት አላገኘሁም", callback_data="log_second_miss")]]
            text = "💤 <b>የእረፍት ክትትል / RECOVERY CHECK</b>\nበቂ (7+ ሰዓት) እረፍት አድርገዋል? / Did you hit your sleep target?"
        else:
            kb = [[InlineKeyboardButton("💧 በእቅዱ መሰረት ጠጥቻለሁ", callback_data="log_second_hit")], [InlineKeyboardButton("❌ አልሞላሁም / Missed", callback_data="log_second_miss")]]
            text = "💧 <b>የውኃ ክትትል / HYDRATION CHECK</b>\nበእቅዱ መሰረት ውኃ ጠጥተዋል? / Did you drink water according to your plan?"
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    elif query.data in ["log_second_hit", "log_second_miss"]:
        second = "Hit" if query.data == "log_second_hit" else "Missed"
        context.user_data["checkin_second"] = second
        context.user_data["awaiting_checkin_note"] = True
        context.user_data["awaiting_checkin_note_started_at"] = datetime.now(timezone.utc).isoformat()

        await query.message.reply_text(
            "✍️ <b>አጭር ማስታወሻ ወይም ጥያቄ ካለዎት ይጻፉልን (ከሌለ 'የለም' ይበሉ)፦</b>\n"
            "<i>Drop any notes, feedback, or questions for Simon about your day:</i>",
            parse_mode="HTML"
        )

# ------------------------------------------------------------------
# MEDIA, Q&A, AND APPROVALS
# ------------------------------------------------------------------
async def handle_client_attachments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or not update.message: return

    if context.user_data.get("awaiting_checkin_note"):
        started_raw = context.user_data.get("awaiting_checkin_note_started_at")
        is_stale = True
        if started_raw:
            try:
                started = datetime.fromisoformat(started_raw)
                is_stale = (datetime.now(timezone.utc) - started) > timedelta(minutes=30)
            except Exception:
                is_stale = True

        if is_stale:
            context.user_data["awaiting_checkin_note"] = False
            context.user_data.pop("awaiting_checkin_note_started_at", None)
            context.user_data.pop("checkin_nut", None)
            context.user_data.pop("checkin_second", None)
        else:
            context.user_data["awaiting_checkin_note"] = False
            context.user_data.pop("awaiting_checkin_note_started_at", None)
            note = update.message.text.strip() if update.message.text else "No note"
            nut = context.user_data.pop("checkin_nut", "Logged")
            second = context.user_data.pop("checkin_second", "Logged")

            async with CHECKIN_LOCKS[u.id]:
                try:
                    today_start = get_today_start_utc()
                    if supabase.table("daily_logs").select("id").eq("client_id", u.id).gte("created_at", today_start).execute().data:
                        await update.message.reply_text("✅ <b>ዛሬ ቀድመው ተመዝግበዋል! / You've already checked in today!</b>\nCome back tomorrow to keep your streak going.", parse_mode="HTML")
                        return

                    try:
                        supabase.table("daily_logs").insert({
                            "client_id": u.id,
                            "nutrition_status": nut,
                            "hydration_status": second,
                            "notes": note,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        }).execute()
                    except Exception as insert_err:
                        err_str = str(insert_err).lower()
                        if "duplicate" in err_str or "unique" in err_str or "daily_logs_one_per_day" in err_str:
                            await update.message.reply_text(
                                "✅ <b>ዛሬ ቀድመው ተመዝግበዋል! / You've already checked in today!</b>\nCome back tomorrow to keep your streak going.",
                                parse_mode="HTML"
                            )
                            return
                        logging.error(f"Failed to insert daily log for {u.id}: {insert_err}")
                        await update.message.reply_text(
                            "⚠️ Something went wrong saving your check-in — please try again in a moment.",
                            parse_mode="HTML"
                        )
                        return

                    logs = supabase.table("daily_logs").select("created_at").eq("client_id", u.id).gte("created_at", (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()).execute().data
                    streak = len({parse_supabase_timestamp(l["created_at"]).date() for l in logs})
                    celeb = f"\n\n🔥 <b>ድንቅ ክንውን! / MILESTONE UNLOCKED!</b> የ <b>{streak} ቀን</b> የክትትል Streak አስመዝግበዋል!" if streak in [7,14,30] else ""

                    await update.message.reply_text(
                        f"🎉 <b>ክትትልዎ እና ማስታወሻዎ ተመዝግበዋል! / Check-In Completed!</b>\n"
                        f"ማስታወሻ፦ <i>{esc(note)}</i>\n"
                        f"ሳይመን progressዎን በቅርቡ ይገመግማል{celeb}",
                        parse_mode="HTML"
                    )

                    for a_id in ADMIN_USER_IDS:
                        await send_message_safely(
                            context, chat_id=a_id,
                            text=f"📊 <b>DAILY CHECK-IN NOTE: {esc(u.full_name)}</b>\n• Nutrition: {nut}\n• Recovery/Hydration: {second}\n• Note: <i>{esc(note)}</i>",
                            parse_mode="HTML"
                        )
                except Exception as e:
                    logging.error(f"Unexpected error in check-in flow for {u.id}: {e}")
                    await update.message.reply_text("⚠️ Something went wrong saving your check-in — please try again in a moment.", parse_mode="HTML")
            return

    try:
        c = supabase.table("clients").select("package, full_name").eq("id", u.id).execute().data[0]
        tier = c.get("package", "Meal Plan Only (2 Months)")
    except Exception: tier = "Meal Plan Only (2 Months)"

    perms = TIER_PERMISSIONS.get(tier, TIER_PERMISSIONS["Meal Plan Only (2 Months)"])

    if update.message.photo or update.message.video or update.message.voice:
        if not perms["allow_media"] and not update.message.photo:
            await update.message.reply_text("ማሳሰቢያ፦ የፎርም ግምገማዎች እና የድምጽ መልእክቶች ለ Transformation ፓኬጆች ብቻ የተሰጡ ናቸው። ለማሻሻል ከታች ይጫኑ:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏋️ Upgrade Plan", callback_data="upgrade_tier")]]))
            return

        m_type = "photo" if update.message.photo else ("video" if update.message.video else "voice")
        f_id = update.message.photo[-1].file_id if update.message.photo else (update.message.video.file_id if update.message.video else update.message.voice.file_id)

        try: supabase.table("client_media").insert({"client_id": u.id, "media_type": m_type, "telegram_file_id": f_id, "message_text": update.message.caption or ""}).execute()
        except Exception: pass

        await update.message.reply_text("Got it! 🎥 Your attachment has been saved for ሳይመን's review.", parse_mode="HTML")

        if update.message.photo and context.user_data.get("awaiting_payment_screenshot"):
            context.user_data["awaiting_payment_screenshot"] = False
            req_tier = context.user_data.get("pending_tier", "Transformation (60 Days)")
            tier_code = TIER_CODES.get(req_tier, TIER_CODES["Transformation (60 Days)"])
            txt = f"🚨 <b>PAYMENT RECEIPT!</b>\nClient: {esc(u.full_name)} (<code>{u.id}</code>)\nTier: {req_tier}"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ Approve & Set {req_tier}", callback_data=f"approve_tier_{u.id}_{tier_code}")]])
            for a_id in ADMIN_USER_IDS:
                try: await context.bot.send_photo(chat_id=a_id, photo=f_id, caption=txt, reply_markup=kb, parse_mode="HTML")
                except Exception: pass
        return

    if update.message.text:
        if not perms["allow_qa"]:
            await update.message.reply_text("ጥያቄዎችን በቀጥታ የመጠየቅ መብት ለኮቺንግ ፓኬጅ ተጠቃሚዎች ብቻ የተሰጡ ናቸው። ለማሻሻል ከታች ይጫኑ:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏋️ Upgrade Plan", callback_data="upgrade_tier")]]))
            return

        try: supabase.table("client_media").insert({"client_id": u.id, "media_type": "text", "message_text": update.message.text.strip()}).execute()
        except Exception: pass

        if perms.get("priority"):
            for a_id in ADMIN_USER_IDS: await send_message_safely(context, chat_id=a_id, text=f"🚨 <b>VIP QUESTION!</b>\nClient: {esc(u.full_name)}\nTier: {tier}\nMessage: {esc(update.message.text.strip())}", parse_mode="HTML")
            await update.message.reply_text("Your VIP message has been routed directly to ሳይመን!")
        else:
            await update.message.reply_text("Question saved! 📝 ሳይመን will address this in your next check-in.", parse_mode="HTML")

async def handle_admin_tier_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS: return
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    if len(parts) < 3: return
    t_id = parts[2]
    code = parts[3] if len(parts) > 3 else "TRF"
    tier = TIER_CODES_REVERSE.get(code, "Transformation (60 Days)")

    try:
        supabase.table("clients").update({
            "package": tier,
            "package_started_at": datetime.now(timezone.utc).isoformat(),
            "renewal_notified": False,
            "testimonial_notified": False,
        }).eq("id", t_id).execute()
        msg = f"🎉 <b>Payment Approved!</b>\nUpgraded to <b>{tier}</b>." if await get_client_language(int(t_id)) == "en" else f"🎉 <b>ክፍያዎ ጸድቋል! / Payment Approved!</b>\nመለያዎ ወደ <b>{tier}</b> ከፍ ብሏል። እንኳን ደስ አለዎት!"
        await send_message_safely(context, chat_id=int(t_id), text=msg, parse_mode="HTML")
        await query.message.edit_caption(caption=query.message.caption + f"\n\n✅ <b>APPROVED BY ሳይመን</b> ({tier})", parse_mode="HTML")
    except Exception: pass

# ------------------------------------------------------------------
# INITIALIZATION & SCHEDULERS
# ------------------------------------------------------------------
async def post_init(application):
    await start_web_server()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_morning_motivation, "cron", hour=8, minute=0, timezone=EAT_TIMEZONE, args=[application])
    scheduler.add_job(send_daily_checkin_reminders, "cron", hour=20, minute=0, timezone=EAT_TIMEZONE, args=[application])
    scheduler.add_job(send_late_night_reminders, "cron", hour=22, minute=30, timezone=EAT_TIMEZONE, args=[application])
    scheduler.add_job(send_sunday_admin_report, "cron", day_of_week="sun", hour=8, minute=0, timezone=EAT_TIMEZONE, args=[application])
    scheduler.add_job(check_expirations_and_streaks, "cron", hour=9, minute=0, timezone=EAT_TIMEZONE, args=[application])
    scheduler.start()

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).persistence(PicklePersistence(filepath="bot_persistence")).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("checkin", checkin_command))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("broadcast", admin_broadcast))
    app.add_handler(CommandHandler("setpackage", admin_set_package))
    app.add_handler(CommandHandler("clientinfo", admin_client_info))
    app.add_handler(CommandHandler("sendplan", admin_send_plan))
    app.add_handler(CommandHandler("reply", admin_send_voice_feedback))
    app.add_handler(CommandHandler("status", admin_status_command))
    app.add_handler(CommandHandler("sendfile", admin_send_file))
    app.add_handler(CommandHandler("questions", admin_view_questions))
    app.add_handler(CommandHandler("testmotivation", admin_test_motivation))
    app.add_handler(CommandHandler("testnudge", admin_test_nudge))

    app.add_handler(CallbackQueryHandler(handle_target_plan, pattern="^get_target_plan$"))
    app.add_handler(CallbackQueryHandler(handle_client_profile, pattern="^get_client_profile$"))
    app.add_handler(CallbackQueryHandler(trigger_daily_checkin, pattern="^start_checkin$"))
    app.add_handler(CallbackQueryHandler(handle_checkin_responses, pattern="^log_"))
    app.add_handler(CallbackQueryHandler(handle_language_switch, pattern="^set_lang_"))

    app.add_handler(CallbackQueryHandler(handle_upgrade_button, pattern="^upgrade_tier$"))
    app.add_handler(CallbackQueryHandler(handle_upgrade_payment_info, pattern="^upgrade_"))
    app.add_handler(CallbackQueryHandler(handle_admin_tier_approval, pattern="^approve_tier_"))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_client_attachments))

    app.add_error_handler(error_handler)

    print("⚡ Bot #2 with Gatekeeper is LIVE! Triple Motivation Schedule active.")
    app.run_polling()

if __name__ == "__main__":
    main()
