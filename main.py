import os
import logging
from datetime import datetime
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

# Logging configuration
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

# Hardcoded Admin Telegram IDs for VIP Alerts
ADMIN_USER_IDS = [1622298145, 389487101]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------------------------------------------------------------
# TIER PERMISSIONS DEFINITION
# ------------------------------------------------------------------
TIER_PERMISSIONS = {
    "Meal Plan Only": {"allow_media": False, "allow_qa": False, "priority": False},
    "Coaching Tier": {"allow_media": True, "allow_qa": True, "priority": False},
    "Elite Transformation (90 Days)": {"allow_media": True, "allow_qa": True, "priority": True},
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
# BOT COMMANDS & CALLBACK HANDLERS
# ------------------------------------------------------------------
async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🥗 My Meal Plan", callback_data="get_meal_plan")],
        [InlineKeyboardButton("🏋️ My Workout Plan", callback_data="get_workout_plan")],
        [InlineKeyboardButton("📊 Log Progress", callback_data="trigger_checkin")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Welcome to Simon Origin Portal! 🎯\nSelect an option below to view your plans or log progress:",
        reply_markup=reply_markup,
    )

async def handle_plan_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    col_name = "meal_plan_url" if query.data == "get_meal_plan" else "workout_plan_url"
    plan_label = "Meal Plan" if col_name == "meal_plan_url" else "Workout Plan"

    try:
        res = supabase.table("clients").select(col_name).eq("id", user_id).execute()
        if res.data and res.data[0].get(col_name):
            url = res.data[0][col_name]
            await query.message.reply_text(f"🥗 Here is your personalized {plan_label}:\n\n🔗 {url}")
        else:
            await query.message.reply_text(f"📋 Your {plan_label.lower()} hasn't been uploaded yet. Coach Simon will notify you once it's ready!")
    except Exception as e:
        logging.error(f"Database fetch error: {e}")
        await query.message.reply_text("⚠️ Could not fetch your plan right now. Please try again later.")

# ------------------------------------------------------------------
# TIER-BASED CLIENT INPUT HANDLER
# ------------------------------------------------------------------
async def handle_client_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Fetch user tier dynamically from Supabase
    try:
        res = supabase.table("clients").select("package").eq("id", user.id).execute()
        tier = res.data[0].get("package", "Meal Plan Only") if res.data else "Meal Plan Only"
    except Exception:
        tier = "Meal Plan Only"

    perms = TIER_PERMISSIONS.get(tier, TIER_PERMISSIONS["Meal Plan Only"])

    # Handle Photo, Video, or Voice Attachments
    if update.message.photo or update.message.video or update.message.voice:
        if not perms["allow_media"]:
            await update.message.reply_text(
                "Note: Photo/video form reviews and voice note audits are reserved for Coaching Tiers. Tap below to upgrade:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏋️ Upgrade Plan", callback_data="upgrade_tier")]]),
            )
            return

        if update.message.voice and update.message.voice.duration > 120:
            await update.message.reply_text("Voice notes are capped at 2 minutes max so Coach Simon can review efficiently! Please send a shorter audio note.")
            return

        # Log entry to Supabase
        try:
            supabase.table("progress_logs").insert({
                "client_id": user.id,
                "created_at": date_str,
                "has_media": True,
                "message_text": update.message.caption or "Media attachment"
            }).execute()
        except Exception as e:
            logging.error(f"Failed to log media to Supabase: {e}")

        await update.message.reply_text("Got it! 🎥 Your attachment has been date-locked and saved. Coach Simon will review it in your <b>next check-in</b>.", parse_mode="HTML")
        return

    # Handle Text Notes / Questions
    if update.message.text:
        if not perms["allow_qa"]:
            await update.message.reply_text(
                "Direct Q&A access is reserved for Coaching Tier clients. Tap below to upgrade:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏋️ Upgrade Plan", callback_data="upgrade_tier")]]),
            )
            return

        # Save note to Supabase
        try:
            supabase.table("progress_logs").insert({
                "client_id": user.id,
                "created_at": date_str,
                "has_media": False,
                "message_text": update.message.text
            }).execute()
        except Exception as e:
            logging.error(f"Failed to log note to Supabase: {e}")

        if perms.get("priority"):
            vip_alert = f"🚨 <b>INSTANT VIP QUESTION!</b>\nFrom: {user.full_name}\nMessage: {update.message.text}"
            for admin_id in ADMIN_USER_IDS:
                try:
                    await context.bot.send_message(chat_id=admin_id, text=vip_alert, parse_mode="HTML")
                except Exception as e:
                    logging.error(f"Failed to send VIP alert to {admin_id}: {e}")
            await update.message.reply_text("Your VIP message has been routed directly to Coach Simon!")
        else:
            await update.message.reply_text("Question saved! 📝 Coach Simon will address this in your <b>next check-in</b>.", parse_mode="HTML")

# ------------------------------------------------------------------
# MAIN ENTRY POINT
# ------------------------------------------------------------------
async def post_init(application):
    await start_web_server()

def main():
    persistence = PicklePersistence(filepath="bot_persistence")
    app = ApplicationBuilder().token(BOT_TOKEN).persistence(persistence).post_init(post_init).build()

    app.add_handler(CommandHandler("start", plan_command))
    app.add_handler(CommandHandler("plan", plan_command))
    app.add_handler(CallbackQueryHandler(handle_plan_request, pattern="^get_meal_plan$|^get_workout_plan$"))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_client_input))

    print("⚡ Bot #2 (Simon Tracking & Coaching Portal) is live on Render...")
    app.run_polling()

if __name__ == "__main__":
    main()
