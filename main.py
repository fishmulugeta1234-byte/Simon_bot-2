import os
import logging
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------------------------------------------------------------
# TIER PERMISSIONS DEFINITION
# ------------------------------------------------------------------
TIER_PERMISSIONS = {
    "Meal Plan Only": {"allow_media": False, "allow_qa": False, "priority": False},
    "Kickstart (21 Days)": {"allow_media": False, "allow_qa": True, "priority": False},
    "Transformation (60 Days)": {"allow_media": True, "allow_qa": True, "priority": False},
    "Elite Transformation (90 Days)": {"allow_media": True, "allow_qa": True, "priority": False},
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
# HELPER: GET CLIENT LANGUAGE PREFERENCE
# ------------------------------------------------------------------
async def get_client_language(user_id: int) -> str:
    """Fetches client language preference from Supabase, defaults to Amharic/English mix."""
    try:
        res = supabase.table("clients").select("language").eq("id", user_id).execute()
        if res.data and len(res.data) > 0:
            return res.data[0].get("language", "am")
    except Exception:
        pass
    return "am"  # Default to Amharic/English dual display

# ------------------------------------------------------------------
# ELITE BACKGROUND JOBS (Sunday Reports, Streaks, Expirations)
# ------------------------------------------------------------------
async def send_sunday_admin_report(context: ContextTypes.DEFAULT_TYPE):
    try:
        res = supabase.table("clients").select("id, full_name, package, goal").execute()
        if not res.data:
            return

        report_lines = ["📥 <b>WEEKLY REVIEW QUEUE FOR COACH SIMON</b>\n"]
        
        for client in res.data:
            c_id = client["id"]
            name = client.get("full_name", "Client")
            tier = client.get("package", "Meal Plan Only")
            goal = client.get("goal", "goal_fat_loss")

            if tier == "Meal Plan Only":
                continue

            seven_days_ago = (datetime.now() - timedelta(days=7)).isoformat()
            logs_res = supabase.table("daily_logs").select("*").eq("client_id", c_id).gte("created_at", seven_days_ago).execute()
            logs = logs_res.data if logs_res.data else []
            streak = len(logs)

            goal_icon = "💪" if goal == "goal_muscle" else "🔥"
            report_lines.append(f"{goal_icon} <b>{name}</b> ({tier})\n• Adherence: {streak} / 7 Days Logged\n")

        full_report = "\n".join(report_lines)
        for admin_id in ADMIN_USER_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=full_report, parse_mode="HTML")
            except Exception:
                pass
    except Exception as e:
        logging.error(f"Error generating Sunday report: {e}")

async def check_expirations_and_streaks(context: ContextTypes.DEFAULT_TYPE):
    try:
        res = supabase.table("clients").select("id, full_name, package, created_at").execute()
        if not res.data:
            return

        now = datetime.now()
        for client in res.data:
            c_id = client["id"]
            tier = client.get("package", "Meal Plan Only")
            if tier == "Meal Plan Only":
                continue

            created_date = datetime.fromisoformat(client["created_at"].replace("Z", "+00:00").split("+")[0])
            days_active = (now - created_date).days

            if days_active == 57:
                await context.bot.send_message(
                    chat_id=c_id,
                    text="⚠️ <b>የኮቺንግ ፓኬጅዎ በ3 ቀናት ውስጥ ይጠናቀቃል! / Package Expires in 3 Days!</b>\n"
                         "እቅዶችዎን እና ክትትልዎን መቀጠል እንዲችሉ ከታች በመጫን ያድሱ:",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 ፓኬጅ ማደሻ / Renew Package", callback_data="upgrade_tier")]]),
                    parse_mode="HTML"
                )
    except Exception as e:
        logging.error(f"Error checking expirations: {e}")

# ------------------------------------------------------------------
# ADMIN FILE & VOICE NOTE DELIVERY HANDLERS
# ------------------------------------------------------------------
async def admin_send_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        return

    if len(context.args) < 2 or not update.message.document:
        await update.message.reply_text("Usage: Send a PDF/DOCX with caption `/sendplan <client_id> <meal|workout>`")
        return

    target_client_id = context.args[0]
    plan_type = context.args[1].lower()
    file_id = update.message.document.file_id
    col_name = "meal_plan_url" if plan_type == "meal" else "workout_plan_url"

    try:
        supabase.table("clients").update({col_name: file_id}).eq("id", target_client_id).execute()
        
        await context.bot.send_message(
            chat_id=target_client_id,
            text=f"🎉 <b>አዲስ የ{plan_type.capitalize()} እቅድ ተጭኗል! / New Plan Updated!</b>\nኮች ሲሞን አዲስ እቅድዎን ልኮልዎታል። ለማየት ዋናውን ምናሌ ይክፈቱ!",
            parse_mode="HTML"
        )
        await update.message.reply_text("✅ Plan successfully updated and sent to the client!")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Failed to update plan: {e}")

async def admin_send_voice_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        return

    if not context.args or not update.message.voice:
        await update.message.reply_text("Usage: Record a voice note with caption `/reply <client_id>`")
        return

    target_client_id = context.args[0]
    voice_file_id = update.message.voice.file_id

    try:
        await context.bot.send_voice(
            chat_id=target_client_id,
            voice=voice_file_id,
            caption="🎙️ <b>ከኮች ሲሞን የተላከ የድምጽ መልእክት / Voice Feedback from Coach</b>",
            parse_mode="HTML"
        )
        await update.message.reply_text("✅ Voice note delivered to client!")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Failed to deliver voice note: {e}")

# ------------------------------------------------------------------
# BOT COMMANDS, MAIN MENU & LANGUAGE TOGGLE
# ------------------------------------------------------------------
async def get_main_menu_markup(user_id: int):
    lang = await get_client_language(user_id)
    if lang == "en":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📖 My Target Plan", callback_data="get_target_plan")],
            [InlineKeyboardButton("📊 Daily Check-In", callback_data="start_checkin")],
            [InlineKeyboardButton("🌐 Switch to Amharic (አማርኛ)", callback_data="set_lang_am")]
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📖 የእኔ እቅድ / My Target Plan", callback_data="get_target_plan")],
            [InlineKeyboardButton("📊 የዕለት ክትትል / Daily Check-In", callback_data="start_checkin")],
            [InlineKeyboardButton("🌐 ወደ እንግሊዝኛ ቀይር (Switch to English)", callback_data="set_lang_en")]
        ])

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reply_markup = await get_main_menu_markup(user_id)
    await update.message.reply_text(
        "እንኳን ወደ ሲሞን ኦሪጅን የክትትል እና ኮቺንግ ፖርታል በደህና መጡ! 🎯\n"
        "Welcome to Simon Origin Tracking & Coaching Portal! 🎯\n\n"
        "ቋንቋ ለመቀየር ወይም ለመጀመር ከታች ያሉትን ይጫኑ / Select an option below:",
        reply_markup=reply_markup,
    )

async def handle_language_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    new_lang = "en" if query.data == "set_lang_en" else "am"

    try:
        supabase.table("clients").update({"language": new_lang}).eq("id", user_id).execute()
    except Exception:
        pass  # Fallback if column doesn't exist yet, stored in user session

    reply_markup = await get_main_menu_markup(user_id)
    confirmation_text = "Language switched to English! 🇬🇧" if new_lang == "en" else "ቋንቋ ወደ አማርኛ ተቀይሯል! 🇪🇹"
    
    await query.message.edit_text(
        f"✅ <b>{confirmation_text}</b>\n\nእንኳን ወደ ሲሞን ኦሪጅን ፖርታል በደህና መጡ! ከታች ይምረጡ:",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

async def handle_target_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = await get_client_language(user_id)

    try:
        res = supabase.table("clients").select("package, meal_plan_url, workout_plan_url, goal").eq("id", user_id).execute()
        if not res.data:
            msg = "📋 Profile not found. Please complete onboarding first!" if lang == "en" else "📋 መረጃዎ አልተገኘም። እባክዎ መጀመሪያ ይመዝገቡ!"
            await query.message.reply_text(msg)
            return

        client = res.data[0]
        tier = client.get("package", "Meal Plan Only")
        meal_url = client.get("meal_plan_url", "Not Uploaded Yet")
        workout_url = client.get("workout_plan_url", "Not Uploaded Yet")

        if tier == "Meal Plan Only":
            text = f"📋 <b>NUTRITION BLUEPRINT / የምግብ እቅድ</b>\n\n🔗 <b>Meal Plan:</b> {meal_url}"
        else:
            text = f"📋 <b>FULL COACHING BLUEPRINT / የኮቺንግ እቅድ</b>\n\n🔗 <b>Meal Plan:</b> {meal_url}\n🏋️ <b>Workout Plan:</b> {workout_url}"

        await query.message.reply_text(text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Error fetching target plan: {e}")
        await query.message.reply_text("⚠️ Database error fetching your plan.")

# ------------------------------------------------------------------
# UPGRADE FLOW HANDLERS
# ------------------------------------------------------------------
async def handle_upgrade_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🔥 Transformation (60 Days)", callback_data="upgrade_60day")],
        [InlineKeyboardButton("⚡ Elite Transformation (90 Days)", callback_data="upgrade_90day")],
        [InlineKeyboardButton("📲 Contact Coach Simon", url="https://t.me/s_simon_19")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.message.reply_text(
        "🏋️ <b>UPGRADE TO FULL COACHING / ፓኬጅ ማሻሻያ</b>\n\n"
        "Unlock full 1-on-1 coaching, form reviews, voice audits, and custom plans!\n\n"
        "Select your tier below / አማራጭ ይምረጡ:",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

async def handle_upgrade_payment_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    selected_tier = "60 Days Transformation" if query.data == "upgrade_60day" else "90 Days Elite Transformation"

    text = (
        f"💳 <b>UPGRADE TO {selected_tier.upper()}</b>\n\n"
        "Transfer fee via / ክፍያውን ያስተላልፉ:\n"
        "• <b>CBE:</b> 1000357796532 (Simon Mulugeta)\n"
        "• <b>Telebirr:</b> 0939998090 (Simon Mulugeta)\n\n"
        "📸 <b>Next Step:</b> Send your payment receipt screenshot to this chat!"
    )
    await query.message.reply_text(text, parse_mode="HTML")

# ------------------------------------------------------------------
# DYNAMIC CHECK-IN FLOW
# ------------------------------------------------------------------
async def trigger_daily_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = await get_client_language(user_id)

    try:
        res = supabase.table("clients").select("goal").eq("id", user_id).execute()
        goal = res.data[0].get("goal", "goal_fat_loss") if res.data else "goal_fat_loss"
    except Exception:
        goal = "goal_fat_loss"

    if goal == "goal_muscle":
        keyboard = [
            [InlineKeyboardButton("🎯 Hit Protein & Calorie Target", callback_data="log_nut_hit")],
            [InlineKeyboardButton("⚠️ Under-Ate Protein/Calories", callback_data="log_nut_miss")],
        ]
        text = "🔔 <b>MUSCLE BUILDING CHECK-IN / የጡንቻ ግንባታ ክትትል</b>\nDid you hit your protein & calorie target today? / ፕሮቲን እና ካሎሪ ሞልተዋል?"
    else:
        keyboard = [
            [InlineKeyboardButton("🎯 Hit Deficit Target", callback_data="log_nut_hit")],
            [InlineKeyboardButton("⚠️ Exceeded Calorie Target", callback_data="log_nut_miss")],
        ]
        text = "🔔 <b>FAT LOSS CHECK-IN / ስብ መቀነስ ክትትል</b>\nDid you stay within your calorie deficit? / የካሎሪ ገደብ ጠብቀዋል?"

    await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def handle_checkin_responses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data in ["log_nut_hit", "log_nut_miss"]:
        status = "Hit" if data == "log_nut_hit" else "Missed"
        context.user_data["checkin_nut"] = status

        res = supabase.table("clients").select("goal").eq("id", user_id).execute()
        goal = res.data[0].get("goal", "goal_fat_loss") if res.data else "goal_fat_loss"

        if goal == "goal_muscle":
            keyboard = [
                [InlineKeyboardButton("💤 Hit Sleep Target (7+ hrs)", callback_data="log_second_hit")],
                [InlineKeyboardButton("❌ Under-Rested", callback_data="log_second_miss")],
            ]
            text = "💧 <b>RECOVERY CHECK / የእረፍት ክትትል</b>\nDid you hit your sleep target (7+ hrs)? / በቂ እረፍት አድርገዋል?"
        else:
            keyboard = [
                [InlineKeyboardButton("💧 Hit Water Target (3.5L)", callback_data="log_second_hit")],
                [InlineKeyboardButton("❌ Missed Target", callback_data="log_second_miss")],
            ]
            text = "💧 <b>HYDRATION CHECK / የውኃ ክትትል</b>\nDid you hit your 3.5L water target? / 3.5 ሊትር ውኃ ጠጥተዋል?"

        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data in ["log_second_hit", "log_second_miss"]:
        second_status = "Hit" if data == "log_second_hit" else "Missed"
        nut_status = context.user_data.get("checkin_nut", "Logged")

        try:
            supabase.table("daily_logs").insert({
                "client_id": user_id,
                "nutrition_status": nut_status,
                "hydration_status": second_status
            }).execute()

            logs_res = supabase.table("daily_logs").select("*").eq("client_id", user_id).execute()
            streak_count = len(logs_res.data) if logs_res.data else 1

            celebration = ""
            if streak_count in [7, 14, 30]:
                celebration = f"\n\n🔥 <b>MILESTONE UNLOCKED! / ድንቅ ክንውን!</b> You hit a <b>{streak_count}-day check-in streak</b>!"

            await query.message.reply_text(f"🎉 <b>Check-In Completed! / ክትትልዎ ተመዝግቧል!</b>\nCoach Simon will review your progress soon.{celebration}", parse_mode="HTML")
        except Exception as e:
            logging.error(f"Failed to record check-in: {e}")
            await query.message.reply_text("🎉 <b>Check-In Completed!</b> Coach Simon will review your progress soon.", parse_mode="HTML")

# ------------------------------------------------------------------
# CRASH-PROOF MEDIA & TEXT HANDLER
# ------------------------------------------------------------------
async def handle_client_attachments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        if not user or not update.message:
            return

        try:
            res = supabase.table("clients").select("package, goal, full_name").eq("id", user.id).execute()
            client = res.data[0] if res.data and len(res.data) > 0 else {}
            tier = client.get("package", "Meal Plan Only")
            goal = client.get("goal", "goal_fat_loss")
        except Exception:
            tier = "Meal Plan Only"
            goal = "goal_fat_loss"

        perms = TIER_PERMISSIONS.get(tier, TIER_PERMISSIONS["Meal Plan Only"])

        if update.message.photo or update.message.video or update.message.voice:
            if not perms["allow_media"]:
                await update.message.reply_text(
                    "Note: Form reviews and voice notes are reserved for Transformation Tiers. Tap below to upgrade:",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏋️ Upgrade Plan", callback_data="upgrade_tier")]]),
                )
                return

            media_type = "photo" if update.message.photo else ("video" if update.message.video else "voice")
            file_id = update.message.photo[-1].file_id if update.message.photo else (update.message.video.file_id if update.message.video else update.message.voice.file_id)

            try:
                supabase.table("client_media").insert({
                    "client_id": user.id,
                    "media_type": media_type,
                    "telegram_file_id": file_id,
                    "message_text": update.message.caption or ""
                }).execute()
            except Exception as db_err:
                logging.error(f"Database error saving media: {db_err}")

            await update.message.reply_text("Got it! 🎥 Your attachment has been saved for Coach Simon's review.", parse_mode="HTML")
            
            if update.message.photo and tier == "Meal Plan Only":
                 vip_alert = f"🚨 <b>UPGRADE RECEIPT / የክፍያ ደረሰኝ!</b>\nClient: {user.full_name} ({user.id})"
                 for admin_id in ADMIN_USER_IDS:
                     try:
                         await context.bot.send_photo(chat_id=admin_id, photo=file_id, caption=vip_alert, parse_mode="HTML")
                     except Exception:
                         pass
            return

        if update.message.text:
            text_content = update.message.text.strip()

            if not perms["allow_qa"]:
                await update.message.reply_text(
                    "Direct Q&A access is reserved for Coaching Tier clients. Tap below to upgrade:",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏋️ Upgrade Plan", callback_data="upgrade_tier")]]),
                )
                return

            try:
                supabase.table("client_media").insert({
                    "client_id": user.id,
                    "media_type": "text",
                    "message_text": text_content
                }).execute()
            except Exception as db_err:
                logging.error(f"Database error saving text: {db_err}")

            if perms.get("priority"):
                vip_alert = f"🚨 <b>INSTANT VIP QUESTION!</b>\nClient: {user.full_name}\nTier: {tier}\nMessage: {text_content}"
                for admin_id in ADMIN_USER_IDS:
                    try:
                        await context.bot.send_message(chat_id=admin_id, text=vip_alert, parse_mode="HTML")
                    except Exception:
                        pass
                await update.message.reply_text("Your VIP message has been routed directly to Coach Simon!")
            else:
                await update.message.reply_text("Question saved! 📝 Coach Simon will address this in your next check-in.", parse_mode="HTML")

    except Exception as e:
        logging.error(f"Unexpected error in message handler: {e}")
        reply_markup = await get_main_menu_markup(update.effective_user.id if update.effective_user else 0)
        await update.message.reply_text(
            "Hmm, I didn't quite catch that! Select an option below:",
            reply_markup=reply_markup
        )

# ------------------------------------------------------------------
# MAIN INITIALIZATION & APSCHEDULER WIRING
# ------------------------------------------------------------------
async def post_init(application):
    await start_web_server()
    
    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_sunday_admin_report, "cron", day_of_week="sun", hour=8, minute=0, args=[application])
    scheduler.add_job(check_expirations_and_streaks, "cron", hour=9, minute=0, args=[application])
    scheduler.start()

def main():
    persistence = PicklePersistence(filepath="bot_persistence")
    app = ApplicationBuilder().token(BOT_TOKEN).persistence(persistence).post_init(post_init).build()

    # Base Commands
    app.add_handler(CommandHandler("start", start_command))
    
    # Admin Commands
    app.add_handler(CommandHandler("sendplan", admin_send_plan))
    app.add_handler(CommandHandler("reply", admin_send_voice_feedback))
    
    # Button Callbacks
    app.add_handler(CallbackQueryHandler(handle_target_plan, pattern="^get_target_plan$"))
    app.add_handler(CallbackQueryHandler(trigger_daily_checkin, pattern="^start_checkin$"))
    app.add_handler(CallbackQueryHandler(handle_checkin_responses, pattern="^log_"))
    
    # Language Switch Callbacks
    app.add_handler(CallbackQueryHandler(handle_language_switch, pattern="^set_lang_"))

    # Upgrade Callbacks
    app.add_handler(CallbackQueryHandler(handle_upgrade_button, pattern="^upgrade_tier$"))
    app.add_handler(CallbackQueryHandler(handle_upgrade_payment_info, pattern="^upgrade_60day$|^upgrade_90day$"))
    
    # Media & Text Handlers
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_client_attachments))

    print("⚡ Bot #2 is live with dynamic language switching (Amharic/English)...")
    app.run_polling()

if __name__ == "__main__":
    main()
