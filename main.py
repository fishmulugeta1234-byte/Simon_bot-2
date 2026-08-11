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
            text=f"🎉 <b>New {plan_type.capitalize()} Plan Updated!</b>\nCoach Simon has uploaded your new plan. Tap 📖 My Target Plan to download it!",
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
            caption="🎙️ <b>Voice Feedback from Coach Simon</b>",
            parse_mode="HTML"
        )
        await update.message.reply_text("✅ Voice note delivered to client!")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Failed to deliver voice note: {e}")

# ------------------------------------------------------------------
# BOT COMMANDS & CALLBACKS
# ------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📖 My Target Plan", callback_data="get_target_plan")],
        [InlineKeyboardButton("📊 Daily Check-In", callback_data="start_checkin")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Welcome to Simon Origin Tracking & Coaching Portal! 🎯\nSelect an option below to manage your journey:",
        reply_markup=reply_markup,
    )

async def handle_target_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    try:
        res = supabase.table("clients").select("package, meal_plan_url, workout_plan_url, goal").eq("id", user_id).execute()
        if not res.data:
            await query.message.reply_text("📋 Profile not found. Please complete onboarding with Bot #1 first!")
            return

        client = res.data[0]
        tier = client.get("package", "Meal Plan Only")
        meal_url = client.get("meal_plan_url", "Not Uploaded Yet")
        workout_url = client.get("workout_plan_url", "Not Uploaded Yet")

        if tier == "Meal Plan Only":
            text = f"📋 <b>YOUR NUTRITION BLUEPRINT SUMMARY</b>\n\n🔗 <b>Meal Plan:</b> {meal_url}"
        else:
            text = f"📋 <b>YOUR FULL COACHING BLUEPRINT SUMMARY</b>\n\n🔗 <b>Meal Plan:</b> {meal_url}\n🏋️ <b>Workout Plan:</b> {workout_url}"

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
        "🏋️ <b>UPGRADE TO FULL COACHING</b>\n\n"
        "Unlock full 1-on-1 coaching, exercise form reviews, voice note audits, and workout plans!\n\n"
        "Select your tier to view payment options:",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

async def handle_upgrade_payment_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    selected_tier = "60 Days Transformation" if query.data == "upgrade_60day" else "90 Days Elite Transformation"

    text = (
        f"💳 <b>UPGRADE TO {selected_tier.upper()}</b>\n\n"
        "To complete your upgrade, transfer the fee via:\n"
        "• <b>CBE:</b> 1000357796532 (Simon Mulugeta)\n"
        "• <b>Telebirr:</b> 0939998090 (Simon Mulugeta)\n\n"
        "📸 <b>Next Step:</b> Send a screenshot of your transfer receipt directly to this chat!"
    )
    await query.message.reply_text(text, parse_mode="HTML")

# ------------------------------------------------------------------
# DYNAMIC FAT LOSS VS MUSCLE CHECK-IN FLOW
# ------------------------------------------------------------------
async def trigger_daily_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

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
        text = "🔔 <b>DAILY MUSCLE BUILDING CHECK-IN</b>\nDid you hit your daily protein & total calorie targets today?"
    else:
        keyboard = [
            [InlineKeyboardButton("🎯 Hit Deficit Target", callback_data="log_nut_hit")],
            [InlineKeyboardButton("⚠️ Exceeded Calorie Target", callback_data="log_nut_miss")],
        ]
        text = "🔔 <b>DAILY FAT LOSS CHECK-IN</b>\nDid you stay within your prescribed calorie deficit today?"

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
            text = "💧 <b>RECOVERY CHECK</b>\nDid you hit your sleep and recovery target today?"
        else:
            keyboard = [
                [InlineKeyboardButton("💧 Hit Water Target (3.5L)", callback_data="log_second_hit")],
                [InlineKeyboardButton("❌ Missed Target", callback_data="log_second_miss")],
            ]
            text = "💧 <b>HYDRATION CHECK</b>\nDid you hit your 3.5L water target today?"

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
        except Exception as e:
            logging.error(f"Failed to record check-in: {e}")

        await query.message.reply_text("🎉 <b>Check-In Completed!</b>\nYour data has been date-locked. Coach Simon will review your progress in your next check-in!", parse_mode="HTML")

# ------------------------------------------------------------------
# CRASH-PROOF MEDIA & TEXT HANDLER (Handles Nonsense Safely)
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

        # 1. Handle Media (Photos/Videos/Voice)
        if update.message.photo or update.message.video or update.message.voice:
            if not perms["allow_media"]:
                await update.message.reply_text(
                    "Note: Video/photo form reviews and voice note audits are reserved for Transformation Tiers. Tap below to upgrade:",
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

            await update.message.reply_text("Got it! 🎥 Your attachment has been date-locked and saved. Coach Simon will review it in your <b>next check-in</b>.", parse_mode="HTML")
            
            if update.message.photo and tier == "Meal Plan Only":
                 vip_alert = f"🚨 <b>NEW TIER UPGRADE RECEIPT!</b>\nClient: {user.full_name} ({user.id})\nStatus: Check attachment for payment validation."
                 for admin_id in ADMIN_USER_IDS:
                     try:
                         await context.bot.send_photo(chat_id=admin_id, photo=file_id, caption=vip_alert, parse_mode="HTML")
                     except Exception:
                         pass
            return

        # 2. Handle Text Notes / Nonsense / Questions
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
                goal_label = "💪 MUSCLE BUILDING" if goal == "goal_muscle" else "🔥 FAT LOSS"
                vip_alert = f"🚨 <b>INSTANT VIP QUESTION!</b>\nClient: {user.full_name}\nTier: {tier} ({goal_label})\nMessage: {text_content}"
                for admin_id in ADMIN_USER_IDS:
                    try:
                        await context.bot.send_message(chat_id=admin_id, text=vip_alert, parse_mode="HTML")
                    except Exception:
                        pass
                await update.message.reply_text("Your VIP message has been routed directly to Coach Simon for priority review!")
            else:
                await update.message.reply_text("Question saved! 📝 Coach Simon will address this in your <b>next check-in</b>.", parse_mode="HTML")

    except Exception as e:
        logging.error(f"Unexpected error in message handler: {e}")
        keyboard = [
            [InlineKeyboardButton("📖 My Target Plan", callback_data="get_target_plan")],
            [InlineKeyboardButton("📊 Daily Check-In", callback_data="start_checkin")],
        ]
        await update.message.reply_text(
            "Hmm, I didn't quite catch that! 🎯 Tap an option below to manage your journey:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# ------------------------------------------------------------------
# MAIN INITIALIZATION
# ------------------------------------------------------------------
async def post_init(application):
    await start_web_server()

def main():
    persistence = PicklePersistence(filepath="bot_persistence")
    app = ApplicationBuilder().token(BOT_TOKEN).persistence(persistence).post_init(post_init).build()

    # Base Commands
    app.add_handler(CommandHandler("start", start_command))
    
    # Admin Commands (File & Voice Note Delivery)
    app.add_handler(CommandHandler("sendplan", admin_send_plan))
    app.add_handler(CommandHandler("reply", admin_send_voice_feedback))
    
    # Button Callbacks
    app.add_handler(CallbackQueryHandler(handle_target_plan, pattern="^get_target_plan$"))
    app.add_handler(CallbackQueryHandler(trigger_daily_checkin, pattern="^start_checkin$"))
    app.add_handler(CallbackQueryHandler(handle_checkin_responses, pattern="^log_"))
    
    # Upgrade Callbacks
    app.add_handler(CallbackQueryHandler(handle_upgrade_button, pattern="^upgrade_tier$"))
    app.add_handler(CallbackQueryHandler(handle_upgrade_payment_info, pattern="^upgrade_60day$|^upgrade_90day$"))
    
    # Media & Text Handlers
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_client_attachments))

    print("⚡ Bot #2 (Simon Tracking & Coaching Portal) is live on Render...")
    app.run_polling()

if __name__ == "__main__":
    main()
