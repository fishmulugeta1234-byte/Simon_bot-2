import os
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

# FIX: single source of truth for "start of today" in EAT, converted to UTC for
# comparison against Supabase's UTC timestamps. Previously every call site computed
# this from datetime.now(timezone.utc).replace(hour=0,...), which is UTC midnight,
# not EAT midnight (EAT is UTC+3) -- causing checkins between 00:00-03:00 EAT to be
# attributed to the wrong day.
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
# BACKGROUND JOBS: MOTIVATION & REMINDERS
# ------------------------------------------------------------------
async def send_morning_motivation(context: ContextTypes.DEFAULT_TYPE):
    """Runs at 8:00 AM EAT"""
    try:
        res = supabase.table("clients").select("id, package").eq("is_active", True).execute()
        if not res.data:
            return
            
        clients = [c for c in res.data if "Meal Plan Only" not in c.get("package", "")]
        text = (
            "☀️ <b>Main Character Energy! / አዲስ ቀን፣ አዲስ ሌቭል!</b>\n\n"
            "ዛሬ ቀኑን በድል እንወጣዋለን! ሌቭል-አፕ ለማድረግ ዛሬም በጉልበትና በቁርጠኝነት እንነሳ! 🎮🔥\n\n"
            "<i>New day, new lobby! Time to level up and crush today's quest.</i>"
        )
        
        for client in clients:
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

        clients = [c for c in res.data if "Meal Plan Only" not in c.get("package", "")]
        today_start = get_today_start_utc()  # FIX: EAT-correct boundary
        
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

        clients = [c for c in res.data if "Meal Plan Only" not in c.get("package", "")]
        today_start = get_today_start_utc()  # FIX: EAT-correct boundary
        
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
        clients = [c for c in res.data if "Meal Plan Only" not in c.get("package", "")]
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
            report_lines.append(f"{icon} <b>{client.get('full_name', 'Client')}</b> ({client.get('package', 'N/A')})\n• Adherence: {streak} / 7 Days Logged\n")

        for admin_id in ADMIN_USER_IDS:
            await send_message_safely(context, chat_id=admin_id, text="\n".join(report_lines), parse_mode="HTML")
            await asyncio.sleep(0.05)
    except Exception as e:
        logging.error(f"Error generating Sunday report: {e}")

async def check_expirations_and_streaks(context: ContextTypes.DEFAULT_TYPE):
    try:
        res = supabase.table("clients").select("id, package, created_at, renewal_notified, testimonial_notified").eq("is_active", True).execute()
        if not res.data: return
        now = datetime.now(timezone.utc)
        
        for client in res.data:
            c_id = client["id"]
            pkg = client.get("package", "Meal Plan Only (2 Months)")
            days_active = (now - parse_supabase_timestamp(client["created_at"])).days

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

    # FIX: context.args is whitespace-tokenized, which collapses newlines the admin
    # typed. Pull the raw text after the command instead, so formatting/line breaks survive.
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
        supabase.table("clients").update({"package": tier_name}).eq("id", target_id).execute()
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
            f"👤 <b>INFO: {c.get('full_name')}</b>\n"
            f"• <b>ID:</b> {c['id']}\n• <b>Package:</b> {c.get('package')}\n"
            f"• <b>Goal:</b> {c.get('goal')}\n• <b>Active:</b> {days} days\n"
            f"• <b>7-Day Checkins:</b> {checkins}/7\n"
            f"• <b>Status:</b> {'🟢' if c.get('is_active') else '🔴'}\n"
            f"• <b>Plan Ready:</b> {'✅ Yes' if c.get('plan_ready') else '⏳ Pending'}\n"
        )
        await update.message.reply_text(info, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

# FIX: admin_send_plan now
#   1) accepts a document attached to the command message OR the message being replied to
#   2) validates p_type instead of silently defaulting unknown values to "workout"
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

        # FIX: give the client a tappable button instead of telling them to "open the
        # main menu" with no way to actually do that besides typing /start manually
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
    if not context.args or not update.message.voice: return
    try:
        await context.bot.send_voice(chat_id=context.args[0], voice=update.message.voice.file_id, caption="🎙️ <b>ሳይመን የተላከ የድምጽ መልእክት / Voice Feedback from Coach</b>", parse_mode="HTML")
        await update.message.reply_text("✅ Voice note delivered!")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Failed: {e}")

async def admin_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS: 
        return
    
    try:
        res = supabase.table("clients").select("full_name, package, is_active, plan_ready").execute()
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
        
        for client in clients[-5:]:
            name = client.get("full_name", "Unknown")
            pkg = client.get("package", "Standard")
            status = "🟢" if client.get("is_active") else "🔴"
            text += f"• {status} {name} | <i>{pkg}</i>\n"
            
        await update.message.reply_text(text, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Failed to fetch status: {e}")

async def admin_send_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS: 
        return
    
    # FIX: same reply-fallback as admin_send_plan, for consistency
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
            text = (f"👤 <b>YOUR COACHING PROFILE</b>\n\n• <b>Name:</b> {c.get('full_name')}\n• <b>Package:</b> {c.get('package')}\n"
                    f"• <b>Goal:</b> {c.get('goal')}\n• <b>Days Active:</b> {days}\n• <b>7-Day Check-ins:</b> {checkins}/7\n")
        else:
            text = (f"👤 <b>የኮቺንግ መለያዎ / YOUR PROFILE</b>\n\n• <b>ስም:</b> {c.get('full_name')}\n• <b>የፓኬጅ ዓይነት:</b> {c.get('package')}\n"
                    f"• <b>ዋና ግብ:</b> {c.get('goal')}\n• <b>የቆይታ ጊዜ:</b> {days} ቀናት\n• <b>የ7 ቀን ክትትል:</b> {checkins}/7 ቀናት\n")
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

    # FIX: entering the upgrade flow fresh means any old pending state is stale — clear it
    context.user_data.pop("pending_tier", None)
    context.user_data["awaiting_payment_screenshot"] = False

    loc_type = "et"
    try:
        res = supabase.table("clients").select("location_type").eq("id", u_id).execute()
        if res.data and len(res.data) > 0:
            loc_type = res.data[0].get("location_type", "et")
    except Exception as e:
        logging.error(f"Error checking location_type for upgrade {u_id}: {e}")

    if loc_type == "et":
        keyboard = [
            [InlineKeyboardButton("⚡ Kickstart (21 Days) — 4,500 ETB", callback_data="upgrade_kickstart")],
            [InlineKeyboardButton("🔥 Transformation (60 Days) — 8,900 ETB", callback_data="upgrade_transformation")],
            [InlineKeyboardButton("🥇 Elite (90 Days) — 12,500 ETB", callback_data="upgrade_elite")],
            [InlineKeyboardButton("🌟 Lifestyle (6 Months) — 24,000 ETB", callback_data="upgrade_lifestyle")],
            [InlineKeyboardButton("👑 VIP Coaching (6 Months) — 39,000 ETB", callback_data="upgrade_vip")],
            [InlineKeyboardButton("📲 ከሳይመን ጋር መነጋገር", url="https://t.me/s_simon_19")]
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("⚡ Kickstart (21 Days) — $50", callback_data="upgrade_kickstart")],
            [InlineKeyboardButton("🔥 Transformation (60 Days) — $119", callback_data="upgrade_transformation")],
            [InlineKeyboardButton("🥇 Elite (90 Days) — $159", callback_data="upgrade_elite")],
            [InlineKeyboardButton("🌟 Lifestyle (6 Months) — $299", callback_data="upgrade_lifestyle")],
            [InlineKeyboardButton("👑 VIP Coaching (6 Months) — $549", callback_data="upgrade_vip")],
            [InlineKeyboardButton("📲 Contact Simon", url="https://t.me/s_simon_19")]
        ]

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
    # FIX: explicit flag so the next photo this user sends is unambiguously a payment
    # receipt, instead of inferring that from "a photo arrived" (see handle_client_attachments)
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
        today_start = get_today_start_utc()  # FIX: EAT-correct boundary
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

        # FIX: guard against a missing/empty client row instead of indexing .data[0] blindly
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
            kb = [[InlineKeyboardButton("💧 3.5 ሊትር ውኃ ጠጥቻለሁ", callback_data="log_second_hit")], [InlineKeyboardButton("❌ ውኃ አልሞላሁም", callback_data="log_second_miss")]]
            text = "💧 <b>የውኃ ክትትል / HYDRATION CHECK</b>\n3.5 ሊትር ውኃ ጠጥተዋል? / Did you hit your 3.5L water target?"
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    elif query.data in ["log_second_hit", "log_second_miss"]:
        second = "Hit" if query.data == "log_second_hit" else "Missed"
        context.user_data["checkin_second"] = second
        context.user_data["awaiting_checkin_note"] = True

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

    # Check if client is currently in the checkin note-writing step
    if context.user_data.get("awaiting_checkin_note"):
        context.user_data["awaiting_checkin_note"] = False
        note = update.message.text.strip() if update.message.text else "No note"
        nut = context.user_data.pop("checkin_nut", "Logged")
        second = context.user_data.pop("checkin_second", "Logged")

        try:
            today_start = get_today_start_utc()  # FIX: EAT-correct boundary
            if supabase.table("daily_logs").select("id").eq("client_id", u.id).gte("created_at", today_start).execute().data:
                await update.message.reply_text("✅ <b>ዛሬ ቀድመው ተመዝግበዋል! / You've already checked in today!</b>\nCome back tomorrow to keep your streak going.", parse_mode="HTML")
                return

            supabase.table("daily_logs").insert({
                "client_id": u.id,
                "nutrition_status": nut,
                "hydration_status": second,
                "message_text": note
            }).execute()
            
            logs = supabase.table("daily_logs").select("created_at").eq("client_id", u.id).gte("created_at", (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()).execute().data
            streak = len({parse_supabase_timestamp(l["created_at"]).date() for l in logs})
            celeb = f"\n\n🔥 <b>ድንቅ ክንውን! / MILESTONE UNLOCKED!</b> የ <b>{streak} ቀን</b> የክትትል Streak አስመዝግበዋል!" if streak in [7,14,30] else ""
            
            await update.message.reply_text(
                f"🎉 <b>ክትትልዎ እና ማስታወሻዎ ተመዝግበዋል! / Check-In Completed!</b>\n"
                f"ማስታወሻ፦ <i>{note}</i>\n"
                f"ሳይመን progressዎን በቅርቡ ይገመግማል{celeb}",
                parse_mode="HTML"
            )

            # Route check-in note to admin chat
            for a_id in ADMIN_USER_IDS:
                await send_message_safely(
                    context, chat_id=a_id,
                    text=f"📊 <b>DAILY CHECK-IN NOTE: {u.full_name}</b>\n• Nutrition: {nut}\n• Recovery/Hydration: {second}\n• Note: <i>{note}</i>",
                    parse_mode="HTML"
                )
        except Exception:
            await update.message.reply_text("🎉 <b>ክትትልዎ ተመዝግቧል! / Check-In Completed!</b>\nሳይመን progressዎን በቅርቡ ይገመግማል.", parse_mode="HTML")
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

        # FIX: only treat this photo as a payment receipt if the user was actually sent
        # to the payment-instructions screen. Previously ANY photo (form checks, progress
        # pics) triggered the "PAYMENT RECEIPT! Approve tier" admin alert.
        if update.message.photo and context.user_data.get("awaiting_payment_screenshot"):
            context.user_data["awaiting_payment_screenshot"] = False
            req_tier = context.user_data.get("pending_tier", "Transformation (60 Days)")
            txt = f"🚨 <b>PAYMENT RECEIPT!</b>\nClient: {u.full_name} (<code>{u.id}</code>)\nTier: {req_tier}"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ Approve & Set {req_tier}", callback_data=f"approve_tier_{u.id}_{req_tier[:3]}")]])
            for a_id in ADMIN_USER_IDS:
                try: await context.bot.send_photo(chat_id=a_id, photo=f_id, caption=txt, reply_markup=kb, parse_mode="HTML")
                except Exception: pass
        return

    if update.message.text:
        if not perms["allow_qa"]:
            await update.message.reply_text("ጥያቄዎችን በቀጥታ የመጠየቅ መብት ለኮቺንግ ፓኬጅ ተጠቃሚዎች ብቻ የተሰጠ ነው። ለማሻሻል ከታች ይጫኑ:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏋️ Upgrade Plan", callback_data="upgrade_tier")]]))
            return
            
        try: supabase.table("client_media").insert({"client_id": u.id, "media_type": "text", "message_text": update.message.text.strip()}).execute()
        except Exception: pass

        if perms.get("priority"):
            for a_id in ADMIN_USER_IDS: await send_message_safely(context, chat_id=a_id, text=f"🚨 <b>VIP QUESTION!</b>\nClient: {u.full_name}\nTier: {tier}\nMessage: {update.message.text.strip()}", parse_mode="HTML")
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
    tier_map = {
        "Kic": "Kickstart (21 Days)", 
        "Tra": "Transformation (60 Days)", 
        "Eli": "Elite Transformation (90 Days)", 
        "Lif": "Lifestyle Coaching (6 Months)", 
        "VIP": "VIP Coaching (6 Months)"
    }
    tier = tier_map.get(parts[3] if len(parts) > 3 else "Tra", "Transformation (60 Days)")

    try:
        supabase.table("clients").update({"package": tier}).eq("id", t_id).execute()
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
    app.add_handler(CommandHandler("broadcast", admin_broadcast))
    app.add_handler(CommandHandler("setpackage", admin_set_package))
    app.add_handler(CommandHandler("clientinfo", admin_client_info))
    app.add_handler(CommandHandler("sendplan", admin_send_plan))
    app.add_handler(CommandHandler("reply", admin_send_voice_feedback))
    app.add_handler(CommandHandler("status", admin_status_command))
    app.add_handler(CommandHandler("sendfile", admin_send_file))

    app.add_handler(CallbackQueryHandler(handle_target_plan, pattern="^get_target_plan$"))
    app.add_handler(CallbackQueryHandler(handle_client_profile, pattern="^get_client_profile$"))
    app.add_handler(CallbackQueryHandler(trigger_daily_checkin, pattern="^start_checkin$"))
    app.add_handler(CallbackQueryHandler(handle_checkin_responses, pattern="^log_"))
    app.add_handler(CallbackQueryHandler(handle_language_switch, pattern="^set_lang_"))
    
    app.add_handler(CallbackQueryHandler(handle_upgrade_button, pattern="^upgrade_tier$"))
    app.add_handler(CallbackQueryHandler(handle_upgrade_payment_info, pattern="^upgrade_"))
    app.add_handler(CallbackQueryHandler(handle_admin_tier_approval, pattern="^approve_tier_"))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_client_attachments))

    print("⚡ Bot #2 with Gatekeeper is LIVE! Triple Motivation Schedule active.")
    app.run_polling()

if __name__ == "__main__":
    main()
