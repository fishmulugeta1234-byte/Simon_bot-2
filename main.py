from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import logging
import os
import threading
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# ==========================================
# ⚙️ RENDER ENVIRONMENT CONFIGURATION
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USER_IDS = [1622298145, 389487101]

CBE_ACCOUNT = os.getenv("CBE_ACCOUNT", "1000357796532")
TELEBIRR_NUMBER = os.getenv("TELEBIRR_NUMBER", "0939998090")
ACCOUNT_NAME = os.getenv("ACCOUNT_NAME", "Simon mulugeta")
SUPPORT_HANDLE = "@s_simon_19"

# Tier Access & Permissions Map
TIER_PERMISSIONS = {
    "Meal Plan Only": {
        "allow_media": False,
        "allow_qa": False,
        "has_workouts": False,
    },
    "Kickstart (21 Days)": {
        "allow_media": True,
        "allow_qa": True,
        "has_workouts": True,
    },
    "Transformation (60 Days)": {
        "allow_media": True,
        "allow_qa": True,
        "has_workouts": True,
    },
    "Elite Transformation (90 Days)": {
        "allow_media": True,
        "allow_qa": True,
        "has_workouts": True,
    },
    "Lifestyle Coaching (6 Months)": {
        "allow_media": True,
        "allow_qa": True,
        "has_workouts": True,
    },
    "VIP Coaching (6 Months)": {
        "allow_media": True,
        "allow_qa": True,
        "has_workouts": True,
        "priority": True,
    },
}


# ==========================================
# 🌐 WEB SERVER FOR RENDER KEEP-ALIVE
# ==========================================
class HealthCheckHandler(BaseHTTPRequestHandler):

  def do_GET(self):
    self.send_response(200)
    self.end_headers()
    self.wfile.write(b"Bot #2 is alive and running!")


def run_web_server():
  port = int(os.getenv("PORT", 10000))
  server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
  server.serve_forever()


# ==========================================
# 📖 ON-DEMAND MENU: "MY TARGET PLAN"
# ==========================================
async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
  user_id = update.effective_user.id

  # In production, fetch client profile from Supabase
  # Mocked profile for testing:
  tier = "Elite Transformation (90 Days)"
  lang = context.user_data.get("lang", "am")
  goal = "fat_loss"

  perms = TIER_PERMISSIONS.get(tier, TIER_PERMISSIONS["Meal Plan Only"])

  if perms["has_workouts"]:
    text = (
        f"📋 <b>የእርስዎ ሙሉ የሥልጠና እና የምግብ እቅድ</b>\n\n"
        f"• <b>ፓኬጅ፦</b> {tier}\n"
        f"• <b>ዓላማ፦</b> {goal.replace('_', ' ').title()}\n"
        f"• <b>አሰልጣኝ፦</b> {SUPPORT_HANDLE}\n\n"
        f"📁 <i>የተዘጋጁ ፋይሎችን በማንኛውም ጊዜ ከታች ማውረድ ይችላሉ፦</i>"
        if lang == "am"
        else f"📋 <b>YOUR FULL COACHING BLUEPRINT</b>\n\n"
        f"• <b>Tier:</b> {tier}\n"
        f"• <b>Goal:</b> {goal.replace('_', ' ').title()}\n"
        f"• <b>Coach:</b> {SUPPORT_HANDLE}\n\n"
        f"📁 <i>Access your files anytime below:</i>"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "📄 Download Meal Plan PDF", callback_data="dl_meal"
            )
        ],
        [
            InlineKeyboardButton(
                "🏋️ Download Workout PDF", callback_data="dl_workout"
            )
        ],
    ]
  else:
    text = (
        f"📋 <b>የእርስዎ የምግብ እቅድ Blueprint</b>\n\n"
        f"• <b>ፓኬጅ፦</b> {tier}\n"
        f"• <b>አሰልጣኝ፦</b> {SUPPORT_HANDLE}\n\n"
        f"📁 <i>የተዘጋጀውን የምግብ እቅድ ከታች ያውርዱ፦</i>"
        if lang == "am"
        else f"📋 <b>YOUR NUTRITION BLUEPRINT</b>\n\n"
        f"• <b>Tier:</b> {tier}\n"
        f"• <b>Coach:</b> {SUPPORT_HANDLE}\n\n"
        f"📁 <i>Download your official meal plan below:</i>"
    )
    keyboard = [[
        InlineKeyboardButton(
            "📄 Download Meal Plan PDF", callback_data="dl_meal"
        )
    ]]

  await update.message.reply_text(
      text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
  )


# ==========================================
# 🔔 DAILY CHECK-IN TRIGGER
# ==========================================
async def trigger_checkin_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
  query = update.callback_query
  await query.answer()

  lang = context.user_data.get("lang", "am")
  goal = context.user_data.get("goal", "fat_loss")

  if goal == "fat_loss":
    text = (
        "🔔 <b>የዕለት ተዕለት የፋት ሎስ መመዝገቢያ (8:00 PM)</b>\n\n"
        "1️⃣ <b>የካሎሪ እቅድ፦</b> ዛሬ የተመደበልዎትን የካሎሪ/ዲፊሲት እቅድ 100% ጠብቀዋል?"
        if lang == "am"
        else (
            "🔔 <b>DAILY FAT LOSS CHECK-IN (8:00 PM)</b>\n\n1️⃣ <b>Calorie"
            " Deficit:</b> Did you stay within your prescribed deficit target"
            " today?"
        )
    )
  else:
    text = (
        "🔔 <b>የዕለት ተዕለት የምግብ መመዝገቢያ</b>\n\nዛሬ የፕሮቲን እና ካሎሪ ግብዎን አሟልተዋል?"
        if lang == "am"
        else (
            "🔔 <b>DAILY NUTRITION CHECK-IN</b>\n\nDid you hit your protein"
            " and calorie targets today?"
        )
    )

  keyboard = [
      [
          InlineKeyboardButton("🎯 Hit Target", callback_data="log_hit"),
          InlineKeyboardButton("⚠️ Off-Plan", callback_data="log_miss"),
      ],
      [
          InlineKeyboardButton(
              "🕒 Log Later Tonight", callback_data="log_snooze"
          )
      ],
  ]
  await query.edit_message_text(
      text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
  )


async def handle_log_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
  query = update.callback_query
  await query.answer()
  action = query.data
  lang = context.user_data.get("lang", "am")

  if action == "log_snooze":
    await query.edit_message_text(
        "🕒 Snoozed! We will remind you again at 10:00 PM before bed."
    )
    return

  status = "Hit" if action == "log_hit" else "Off-Plan"

  text = (
      f"✅ <b>የዛሬው መረጃ ተመዝግቧል! ({status})</b>\n\n"
      f"💧 <b>የውሃ መጠን፦</b> ዛሬ የታዘዘውን 3.5 ሊትር ውሃ ጠጥተዋል?"
      if lang == "am"
      else (
          f"✅ <b>Daily Log Saved! ({status})</b>\n\n💧 <b>Hydration Check:</b>"
          f" Did you hit your 3.5L water target today?"
      )
  )

  keyboard = [
      [
          InlineKeyboardButton(
              "💧 3.5L Hit" if lang == "am" else "💧 Hit 3.5L Target",
              callback_data="water_hit",
          )
      ],
      [
          InlineKeyboardButton(
              "❌ Missed" if lang == "am" else "❌ Missed Water Target",
              callback_data="water_miss",
          )
      ],
  ]
  await query.edit_message_text(
      text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
  )


async def handle_water_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
  query = update.callback_query
  await query.answer()
  lang = context.user_data.get("lang", "am")

  text = (
      "🎉 <b>የዛሬው ምዝገባ ተጠናቋል!</b>\n\n"
      "<i>አሰልጣኝ ሳይመን አጠቃላይ የሳምንቱን እንቅስቃሴዎን <b>በሚቀጥለው ቼክ-ኢንዎ</b> ላይ ገምግሞ"
      " ምላሽ ይሰጥዎታል። መልካም ምሽት!</i>"
      if lang == "am"
      else (
          "🎉 <b>Check-In Completed!</b>\n\n<i>Coach Simon will review your"
          " overall progress in your <b>next check-in</b>. Rest up!</i>"
      )
  )
  await query.edit_message_text(text, parse_mode="HTML")


# ==========================================
# 🛡️ CRASH-PROOF INPUT & ATTACHMENT HANDLER
# ==========================================
async def handle_client_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
  user = update.effective_user
  tier = "Elite Transformation (90 Days)"  # Fetched from DB in production
  perms = TIER_PERMISSIONS.get(tier, TIER_PERMISSIONS["Meal Plan Only"])
  date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

  # Handle Photo, Video, or Voice Attachments
  if update.message.photo or update.message.video or update.message.voice:
    if not perms["allow_media"]:
      await update.message.reply_text(
          "Note: Photo/video form reviews and voice note audits are reserved"
          " for Transformation Tiers. Tap below to upgrade:",
          reply_markup=InlineKeyboardMarkup([[
              InlineKeyboardButton(
                  "🏋️ Upgrade Plan", callback_data="upgrade_tier"
              )
          ]]),
      )
      return

    if update.message.voice and update.message.voice.duration > 120:
      await update.message.reply_text(
          "Voice notes are capped at 2 minutes max so Coach Simon can review"
          " efficiently! Please send a shorter audio note."
      )
      return

    await update.message.reply_text(
        "Got it! 🎥 Your attachment has been date-locked and saved. Coach Simon"
        " will review it in your <b>next check-in</b>.",
        parse_mode="HTML",
    )
    return

  # Handle Text Notes / Questions
  if update.message.text:
    if not perms["allow_qa"]:
      await update.message.reply_text(
          "Direct Q&A access is reserved for Coaching Tier clients. Tap below"
          " to upgrade:",
          reply_markup=InlineKeyboardMarkup([[
              InlineKeyboardButton(
                  "🏋️ Upgrade Plan", callback_data="upgrade_tier"
              )
          ]]),
      )
      return

    if perms.get("priority"):
      vip_alert = (
          f"🚨 <b>INSTANT VIP QUESTION!</b>\nFrom: {user.full_name}\nMessage:"
          f" {update.message.text}"
      )
      for admin_id in ADMIN_USER_IDS:
        try:
          await context.bot.send_message(
              chat_id=admin_id, text=vip_alert, parse_mode="HTML"
          )
        except Exception:
          pass
      await update.message.reply_text(
          "Your VIP message has been routed directly to Coach Simon!"
      )
    else:
      await update.message.reply_text(
          "Question saved! 📝 Coach Simon will address this in your <b>next"
          " check-in</b>.",
          parse_mode="HTML",
      )


# ==========================================
# 🏁 MAIN ENTRY POINT
# ==========================================
def main():
  threading.Thread(target=run_web_server, daemon=True).start()

  persistence = PicklePersistence(filepath="bot_persistence")
  app = (
      ApplicationBuilder()
      .token(BOT_TOKEN)
      .persistence(persistence)
      .build()
  )

  app.add_handler(CommandHandler("start", plan_command))
  app.add_handler(CommandHandler("plan", plan_command))

  app.add_handler(
      CallbackQueryHandler(
          trigger_checkin_callback, pattern="^trigger_checkin$"
      )
  )
  app.add_handler(CallbackQueryHandler(handle_log_button, pattern="^log_"))
  app.add_handler(CallbackQueryHandler(handle_water_button, pattern="^water_"))

  app.add_handler(
      MessageHandler(filters.ALL & ~filters.COMMAND, handle_client_input)
  )

  print("⚡ Bot #2 (Simon Tracking & Coaching Portal) is live on Render...")
  app.run_polling()


if __name__ == "__main__":
  main()
