import os
import logging
import asyncio
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)
from supabase import create_client, Client

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Environment Variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
PORT = int(os.getenv("PORT", 8080))

# Initialize Supabase Client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends start message with interactive menu buttons."""
    keyboard = [
        [InlineKeyboardButton("🥗 My Meal Plan", callback_data="get_meal_plan")],
        [InlineKeyboardButton("🏋️ My Workout Plan", callback_data="get_workout_plan")],
        [InlineKeyboardButton("📊 Log Progress", callback_data="log_progress")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "Welcome to Simon Origin Portal! 🎯\nSelect an option below to view your plans:",
        reply_markup=reply_markup
    )

async def handle_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline button clicks and fetches links/files from Supabase."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data

    try:
        response = supabase.table("clients").select("*").eq("id", user_id).execute()
        records = response.data
    except Exception as e:
        logging.error(f"Database error: {e}")
        await query.message.reply_text("⚠️ Database error occurred. Please try again later.")
        return

    if not records:
        await query.message.reply_text(
            "⚠️ You aren't registered in the client database yet. Please reach out to Simon!"
        )
        return

    client_data = records[0]

    if data == "get_meal_plan":
        meal_url = client_data.get("meal_plan_url")
        if meal_url:
            await send_plan_file(query, meal_url, "🥗 Here is your personalized Meal Plan:")
        else:
            await query.message.reply_text("📋 Your meal plan hasn't been uploaded yet.")

    elif data == "get_workout_plan":
        workout_url = client_data.get("workout_plan_url")
        if workout_url:
            await send_plan_file(query, workout_url, "🏋️ Here is your personalized Workout Plan:")
        else:
            await query.message.reply_text("📋 Your workout plan hasn't been uploaded yet.")

    elif data == "log_progress":
        await query.message.reply_text("📝 To log your progress, send your update directly in this chat!")

async def send_plan_file(query, file_identifier: str, caption_text: str):
    """Sends a document if it's a Telegram file_id or a link message if it's a web URL."""
    try:
        await query.message.reply_document(document=file_identifier, caption=caption_text)
    except Exception:
        await query.message.reply_text(f"{caption_text}\n\n🔗 {file_identifier}")

# Dummy HTTP health check handler for Render port binding
async def health_check(request):
    return web.Response(text="Bot is live!")

async def main():
    # Build Telegram Bot App
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_button_click))

    # Build HTTP Server for Render to satisfy health checks
    web_app = web.Application()
    web_app.router.add_get("/", health_check)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)

    # Start both the web server and polling loop
    await site.start()
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    logging.info(f"Server listening on port {PORT} & Bot polling started.")

    # Keep application running continuously
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
