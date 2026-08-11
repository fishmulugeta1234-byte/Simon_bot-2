import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
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
    """Handles inline button clicks and fetches files from Supabase."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data

    # Query Supabase for the client's row matching their Telegram User ID
    response = supabase.table("clients").select("*").eq("id", user_id).execute()
    records = response.data

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
    """Sends a document whether it's a Telegram file_id or a web link."""
    try:
        # Tries sending directly as a Telegram document (file_id)
        await query.message.reply_document(document=file_identifier, caption=caption_text)
    except Exception:
        # If it's a web URL, sends it as a clickable link message instead
        await query.message.reply_text(f"{caption_text}\n{file_identifier}")

async def get_file_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Replies with your Telegram User ID and the File ID of any sent PDF/document."""
    user_id = update.message.from_user.id
    file_id = update.message.document.file_id
    
    response_text = (
        f"✅ **Document Received!**\n\n"
        f"👤 **Your Telegram ID:**\n`{user_id}`\n\n"
        f"📄 **Your File ID:**\n`{file_id}`\n\n"
        f"👉 Copy these numbers/strings into your Supabase SQL query!"
    )
    await update.message.reply_text(response_text, parse_mode="Markdown")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_button_click))
    
    # Catch any document/PDF uploaded to the bot chat
    app.add_handler(MessageHandler(filters.Document.ALL, get_file_id_handler))
    
    print("Bot running...")
    app.run_polling()
