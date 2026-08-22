import os
import html
import logging
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone, date
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

# FIX: username of the separate onboarding/registration bot. Anyone who
# messages THIS bot without an existing row in `clients` gets redirected
# here via a button instead of being allowed to poke around commands.
REGISTRATION_BOT_USERNAME = "Simonorigin_bot"

for _name, _val in [("BOT_TOKEN", BOT_TOKEN), ("SUPABASE_URL", SUPABASE_URL), ("SUPABASE_KEY", SUPABASE_KEY)]:
    if not _val:
        raise RuntimeError(f"Missing required environment variable: {_name}")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Ethiopia is UTC+3 year-round (no DST), used for daily schedules
EAT_TIMEZONE = ZoneInfo("Africa/Addis_Ababa")

# How far back to look when computing a client's *current consecutive
# streak*. This is intentionally much longer than the 7-day adherence
# window used elsewhere, so a genuine long streak isn't artificially
# capped at 7.
STREAK_LOOKBACK_DAYS = 60

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
    "Elite Transformation (90 Days)": {"allow_media": True, "allow_qa": True, "priority": False},
    "Lifestyle Coaching (6 Months)": {"allow_media": True, "allow_qa": True, "priority": True},
    "VIP Coaching (6 Months)": {"allow_media": True, "allow_qa": True, "priority": True},
}

# FIX: package duration attached directly to each tier name via exact-match
# dict lookup. Previously this was an ordered chain of substring checks
# (`"Transformation" in pkg`) which meant "Elite Transformation (90 Days)"
# matched the "Transformation" branch (60 days) before ever reaching the
# "Elite" branch (90 days) — Elite clients got renewal/testimonial nudges
# a full month early. A dict keyed on the exact tier string can't be
# shadowed like that, and safely falls back via .get(pkg, 60) for any
# unrecognized/legacy package value.
TIER_DURATION_DAYS = {
    "Meal Plan Only (2 Months)": 60,
    "Kickstart (21 Days)": 21,
    "Transformation (60 Days)": 60,
    "Elite Transformation (90 Days)": 90,
    "Lifestyle Coaching (6 Months)": 180,
    "VIP Coaching (6 Months)": 180,
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

# FIX: prices used to be hardcoded directly into the upgrade-menu button
# labels, so any price change meant editing code and redeploying. Now
# prices live in a Supabase "pricing" table (tier_name, price_etb,
# price_usd) and admins can update them live with /setprice. This dict
# is kept only as a safety fallback — if the table is missing, empty, or
# a lookup fails for any reason, the bot falls back to these values
# instead of crashing the upgrade menu.
FALLBACK_PRICES = {
    "Kickstart (21 Days)": {"etb": 4500, "usd": 50},
    "Transformation (60 Days)": {"etb": 8900, "usd": 119},
    "Elite Transformation (90 Days)": {"etb": 12500, "usd": 159},
    "Lifestyle Coaching (6 Months)": {"etb": 24000, "usd": 299},
    "VIP Coaching (6 Months)": {"etb": 39000, "usd": 549},
}


def get_tier_prices() -> dict:
    """Reads current tier prices from the Supabase "pricing" table.

    Returns {tier_name: {"etb": int, "usd": int}}. Falls back to
    FALLBACK_PRICES (whole dict, or just a missing tier) if the table
    read fails or a tier isn't in it yet, so the upgrade menu never
    breaks even if pricing hasn't been set up or is mid-edit.
    """
    prices = dict(FALLBACK_PRICES)
    try:
        res = supabase.table("pricing").select("tier_name, price_etb, price_usd").execute()
        for row in (res.data or []):
            name = row.get("tier_name")
            if name:
                prices[name] = {"etb": row.get("price_etb"), "usd": row.get("price_usd")}
    except Exception as e:
        logging.error(f"Error reading pricing table, using fallback prices: {e}")
    return prices

# FIX: Amharic display names for package tiers, so an Amharic-speaking
# client doesn't see a raw English tier string dropped into the middle of
# an otherwise fully localized profile message.
TIER_NAMES_AM = {
    "Meal Plan Only (2 Months)": "የምግብ እቅድ ብቻ (2 ወር)",
    "Kickstart (21 Days)": "ኪክስታርት (21 ቀናት)",
    "Transformation (60 Days)": "ትራንስፎርሜሽን (60 ቀናት)",
    "Elite Transformation (90 Days)": "ኤሊት ትራንስፎርሜሽን (90 ቀናት)",
    "Lifestyle Coaching (6 Months)": "ላይፍስታይል ኮቺንግ (6 ወር)",
    "VIP Coaching (6 Months)": "ቪአይፒ ኮቺንግ (6 ወር)",
}


def display_package(pkg: str, lang: str) -> str:
    if not pkg:
        return pkg
    return TIER_NAMES_AM.get(pkg, pkg) if lang == "am" else pkg


# FIX: in-process locks to serialize the check-in flow per client.
CHECKIN_LOCKS = defaultdict(asyncio.Lock)

# ------------------------------------------------------------------
# MORNING MOTIVATION MESSAGE POOL (bilingual AM/EN, goal-aware, rotating)
# ------------------------------------------------------------------
def _streak_line(streak: int) -> str:
    if streak <= 0:
        return "\n\n🎬 <i>Start your streak today! / ዛሬ ተከታታይ ቀኖችዎን ይጀምሩ!</i>"
    elif streak == 1:
        return f"\n\n🔥 <b>Day {streak}</b> — the streak has begun! / <b>{streak}ኛ ቀን</b> — አስደናቂውን ለውጥ ጀምረዋል!"
    else:
        return f"\n\n🔥 <b>{streak}-day streak</b> and counting! / <b>{streak} ተከታታይ ቀናት</b> እያስመዘገቡ ነው!"


MOTIVATION_VARIANTS = {
    "fat_loss": [
        lambda streak, name: (
            f"☀️ <b>Main Character Energy, {name}! / {name}፣ አዲስ ቀን፣ አዲስ ሌቭል!</b>\n\n"
            "ዛሬ ቀኑን በድል እንወጣዋለን! ሌቭል-አፕ ለማድረግ ዛሬም በጉልበትና በቁርጠኝነት እንነሳ! 🎮🔥\n\n"
            "<i>New day, new lobby! Time to level up and crush today's quest.</i>"
            + _streak_line(streak)
        ),
        lambda streak, name: (
            f"🏃 <b>Warm-Up Complete, Let's Go, {name}! / {name}፣ እንሂድ!</b>\n\n"
            "ሰውነትዎ ዝግጁ ነው፣ አእምሮዎም ዝግጁ ነው። ዛሬ የካሎሪ ግቦትን በርትተው ይምቱ! 💪🔥\n\n"
            "<i>Your body's ready, your mind's ready. Go hit today's calorie target with intent.</i>"
            + _streak_line(streak)
        ),
        lambda streak, name: (
            f"🎯 <b>ጽናት ከተነሳሽነት ይበልጣል! / Discipline Beats Motivation, {name}!</b>\n\n"
            "የስሜት ሁኔታ ይለዋወጣል፣ ልማድ ግን አይለወጥም። ዛሬ በእቅድዎ ይቀጥሉ! ✨\n\n"
            "<i>Motivation comes and goes — discipline shows up anyway. Stick to the plan today.</i>"
            + _streak_line(streak)
        ),
        lambda streak, name: (
            f"🌅 <b>አንድ እርምጃ ወደ ግብዎ, {name}! / One Step Closer!</b>\n\n"
            "የተለወጠ ሰውነት በአንድ ቀን አይገነባም፣ ግን ዛሬ ያደረጉት ምርጫ ወደዚያ ያደርስዎታል። 🚀\n\n"
            "<i>The transformation isn't built in one day — but today's choices get you there.</i>"
            + _streak_line(streak)
        ),
        lambda streak, name: (
            f"⚡ <b>አዲስ ቀን፣ ንጹህ ገጽ, {name}! / Fresh Page, New Day!</b>\n\n"
            "ትናንት ትናንት ነው። ዛሬ ደግሞ ራስዎን ለማሻሻል አዲስ ዕድል ነው! ☀️\n\n"
            "<i>Yesterday's done. Today's a clean page to build on.</i>"
            + _streak_line(streak)
        ),
    ],
    "muscle": [
        lambda streak, name: (
            f"🎮 <b>Level Up Time, {name}! / ጊዜው ደርሷል!</b>\n\n"
            "ዛሬ ጡንቻ ለመገንባት እና ፕሮቲንዎን ለመምታት ጊዜው ነው! XP እያገኙ ነው! 💪🔥\n\n"
            "<i>Time to grind — hit your protein, build that muscle, earn today's XP.</i>"
            + _streak_line(streak)
        ),
        lambda streak, name: (
            f"🏋️ <b>የውሰጦ ጥንካሬ ይጣራል, {name}! / The Iron is Calling!</b>\n\n"
            "እያንዳንዱ ስብስብ (rep) ወደ ግብዎ ያቀርብዎታል። ዛሬ በጥንካሬ ይስሩ! 🔥\n\n"
            "<i>Every rep gets you closer. Go put in work today.</i>"
            + _streak_line(streak)
        ),
        lambda streak, name: (
            f"🌱 <b>እድገት ጊዜ ይፈልጋል, {name}! / Growth Takes Time!</b>\n\n"
            "ጡንቻ የሚገነባው በጅምናዚየም ውስጥ ብቻ ሳይሆን በኩሽናዎም ውስጥ ጭምር ነው። ካሎሪ እና ፕሮቲንዎን ይምቱ! 💪\n\n"
            "<i>Muscle is built in the kitchen as much as the gym — hit your calories and protein today.</i>"
            + _streak_line(streak)
        ),
        lambda streak, name: (
            f"⚙️ <b>ቀን በቀን ፣ ጥንካሬን ይገንቡ, {name}! / Build Strength, Day by Day!</b>\n\n"
            "ትንንሽ ወጥነት ያላቸው ጥረቶች ትልቅ ውጤት ያመጣሉ። ዛሬ ወጥ ይሁኑ! ✨\n\n"
            "<i>Small consistent effort compounds into real strength. Stay consistent today.</i>"
            + _streak_line(streak)
        ),
        lambda streak, name: (
            f"⚡ <b>አዲስ ቀን፣ አዲስ ስብስብ, {name}! / New Day, New Gains!</b>\n\n"
            "ትናንት ያለፈ ነው። ዛሬ ጡንቻ ለመገንባት አዲስ ዕድል ነው! 🚀\n\n"
            "<i>Yesterday's in the books. Today's a fresh shot at building.</i>"
            + _streak_line(streak)
        ),
    ],
}


def comeback_message(name: str) -> str:
    """Shown instead of the regular daily motivation variant when a real
    streak (>=3 days) just broke yesterday and today's computed streak is 0.
    Goal-neutral: the emotional need is the same regardless of fat_loss vs
    muscle."""
    return (
        f"💙 <b>ደረጃ ይለያያል, {name}! / It Happens, {name}!</b>\n\n"
        "አንድ ቀን መዝለል ጉዞዎን አያቆምም — ትናንት ያመለጡት ብቻ ነው፣ ትናንትዎ ዛሬን አይገልጽም! ዛሬ ደግመው ይጀምሩ! 🔁\n\n"
        "<i>One missed day doesn't erase your progress. Yesterday doesn't define today — let's restart strong. 💪</i>"
    )


def milestone_teaser(milestone: int) -> str:
    """Appended to the bottom of whichever daily motivation variant fires
    when today's check-in, if completed, would hit a 7/14/30/60/90-day
    milestone. Not a standalone message."""
    return (
        f"\n\n🏆 <b>አንድ ተጨማሪ ቀን ብቻ! / One More Day!</b>\n"
        f"ዛሬ ከተመዘገቡ የ{milestone}-ቀን ተከታታይ ክትትል ያስመዘግባሉ! 🎉\n"
        f"<i>Check in today and you'll hit a {milestone}-day streak! 🔥</i>"
    )


MILESTONE_DAYS = [7, 14, 30, 60, 90]


# ------------------------------------------------------------------
# CHECK-IN FLOW CARDS (edit-in-place, 3-step, bilingual date + progress)
# ------------------------------------------------------------------
def _note_step_nudge(streak: int) -> str:
    if streak <= 0:
        return "🎬 Log today to start your streak! / ዛሬ በመመዝገብ ጅምርዎን ይጀምሩ!"
    nxt = streak + 1
    return (
        f"🔥 You're on a {streak}-day streak — one more makes it {nxt}! / "
        f"የ{streak} ቀናት ተከታታይ ክትትል አለዎት — አንድ ተጨማሪ ወደ {nxt} ያደርስዎታል!"
    )


def _nut_label(goal: str, hit: bool) -> str:
    if goal == "goal_muscle":
        return "🎯 Protein/Calories: Hit" if hit else "⚠️ Protein/Calories: Missed"
    return "🎯 Calories: On track" if hit else "⚠️ Calories: Off track"


def _second_label(goal: str, hit: bool) -> str:
    if goal == "goal_muscle":
        return "💤 Sleep: Hit" if hit else "❌ Sleep: Missed"
    return "💧 Water: Hit" if hit else "❌ Water: Missed"


def _checkin_step1_card(goal: str, today: date):
    date_label = bilingual_date_label(today)
    cancel_row = [InlineKeyboardButton("❌ ይቅር / Cancel", callback_data="cancel_checkin")]
    if goal == "goal_muscle":
        kb = [
            [InlineKeyboardButton("🎯 ፕሮቲን እና ካሎሪ ሞልቻለሁ", callback_data="log_nut_hit")],
            [InlineKeyboardButton("⚠️ ፕሮቲን/ካሎሪ አጎድያለሁ", callback_data="log_nut_miss")],
            cancel_row,
        ]
        text = (
            f"<b>[Step 1/3]</b> {date_label}\n"
            "🔔 <b>የጡንቻ ግንባታ ክትትል / MUSCLE BUILDING CHECK-IN</b>\n"
            "የፕሮቲን እና ካሎሪ መጠንዎን ሞልተዋል? / Committing to your protein & calorie target today?"
        )
    else:
        kb = [
            [InlineKeyboardButton("🎯 የካሎሪ ገደብ ጠብቄአለሁ", callback_data="log_nut_hit")],
            [InlineKeyboardButton("⚠️ የካሎሪ ገደብ አልጠበቅሁም", callback_data="log_nut_miss")],
            cancel_row,
        ]
        text = (
            f"<b>[Step 1/3]</b> {date_label}\n"
            "🔔 <b>የስብ መቀነስ ክትትል / FAT LOSS CHECK-IN</b>\n"
            "የካሎሪ ገደብዎን ጠብቀዋል? / Did you stay within your calorie deficit?"
        )
    return text, InlineKeyboardMarkup(kb)


def _checkin_step2_card(goal: str, today: date):
    date_label = bilingual_date_label(today)
    cancel_row = [InlineKeyboardButton("❌ ይቅር / Cancel", callback_data="cancel_checkin")]
    if goal == "goal_muscle":
        kb = [
            [InlineKeyboardButton("💤 7+ ሰዓት ተኝቻለሁ", callback_data="log_second_hit")],
            [InlineKeyboardButton("❌ በቂ እረፍት አላገኘሁም", callback_data="log_second_miss")],
            cancel_row,
        ]
        text = (
            f"<b>[Step 2/3]</b> {date_label}\n"
            "💤 <b>የእረፍት ክትትል / RECOVERY CHECK</b>\n"
            "በቂ (7+ ሰዓት) እረፍት አድርገዋል? / Did you hit your sleep target?"
        )
    else:
        kb = [
            [InlineKeyboardButton("💧 በእቅዱ መሰረት ጠጥቻለሁ", callback_data="log_second_hit")],
            [InlineKeyboardButton("❌ አልሞላሁም / Missed", callback_data="log_second_miss")],
            cancel_row,
        ]
        text = (
            f"<b>[Step 2/3]</b> {date_label}\n"
            "💧 <b>የውኃ ክትትል / HYDRATION CHECK</b>\n"
            "በእቅዱ መሰረት ውኃ ጠጥተዋል? / Drinking water on plan today?"
        )
    return text, InlineKeyboardMarkup(kb)


def _checkin_step3_card(streak: int, today: date):
    date_label = bilingual_date_label(today)
    nudge = _note_step_nudge(streak)
    text = (
        f"<b>[Step 3/3]</b> {date_label}\n"
        f"{nudge}\n\n"
        "✍️ <b>አጭር ማስታወሻ ወይም ጥያቄ ካለዎት ይላኩ — ጽሁፍ፣ ፎቶ ወይም ድምጽ ይላኩ:</b>\n"
        "<i>Anything to add? Text, photo, or voice — totally optional.</i>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Skip → / ይለፉ", callback_data="log_note_skip")],
        [InlineKeyboardButton("❌ ይቅር / Cancel", callback_data="cancel_checkin")],
    ])
    return text, kb


async def _finalize_checkin(context: ContextTypes.DEFAULT_TYPE, u_id: int, full_name: str, goal: str,
                             nut: str, second: str, note: str, chat_id: int, message_id: int):
    """Saves the check-in, computes the new streak, and edits the SAME
    tracked bot message into the final recap card. Shared by the Skip
    button path (a callback) and the free-text/photo/voice note path (the
    plain message handler) so both end the flow with one evolving card
    instead of a fresh 'Check-In Completed!' bubble."""
    today_eat_label = bilingual_date_label(datetime.now(EAT_TIMEZONE).date())
    already_text = (
        f"✅ <b>ዛሬ ቀድመው ተመዝግበዋል! / You've already checked in today!</b> — {today_eat_label}\n"
        "Come back tomorrow to keep your streak going. 🔥"
    )

    async with CHECKIN_LOCKS[u_id]:
        try:
            today_start = get_today_start_utc()
            if supabase.table("daily_logs").select("id").eq("client_id", u_id).gte("created_at", today_start).execute().data:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=already_text, parse_mode="HTML")
                return

            try:
                supabase.table("daily_logs").insert({
                    "client_id": u_id,
                    "nutrition_status": nut,
                    "hydration_status": second,
                    "notes": note,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }).execute()
            except Exception as insert_err:
                err_str = str(insert_err).lower()
                if "duplicate" in err_str or "unique" in err_str or "daily_logs_one_per_day" in err_str:
                    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=already_text, parse_mode="HTML")
                    return
                logging.error(f"Failed to insert daily log for {u_id}: {insert_err}")
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id,
                    text="⚠️ Something went wrong saving your check-in — please try again in a moment.",
                    parse_mode="HTML"
                )
                return

            logs = supabase.table("daily_logs").select("created_at").eq("client_id", u_id).gte(
                "created_at", (datetime.now(timezone.utc) - timedelta(days=STREAK_LOOKBACK_DAYS)).isoformat()
            ).execute().data
            today_eat = datetime.now(EAT_TIMEZONE).date()
            streak = compute_current_streak({to_eat_date(l["created_at"]) for l in (logs or [])}, today_eat)
            celeb = f"\n\n🔥 <b>ድንቅ ክንውን! / MILESTONE UNLOCKED!</b> የ<b>{streak} ቀን</b> ተከታታይ ክትትል አስመዝግበዋል!" if streak in MILESTONE_DAYS else ""

            recap = (
                f"🎉 <b>Check-In Completed! / ክትትልዎ ተመዝግቧል!</b> — {bilingual_date_label(today_eat)}\n\n"
                f"• {_nut_label(goal, nut == 'Hit')}\n"
                f"• {_second_label(goal, second == 'Hit')}\n"
                f"• Note: <i>{esc(note)}</i>"
            )
            if streak > 0:
                recap += f"\n\n🔥 <b>{streak}-day streak</b> — see you tomorrow!"
            recap += celeb

            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=recap, parse_mode="HTML")

            for a_id in ADMIN_USER_IDS:
                await send_message_safely(
                    context, chat_id=a_id,
                    text=f"📊 <b>DAILY CHECK-IN NOTE: {esc(full_name)}</b>\n• Nutrition: {nut}\n• Recovery/Hydration: {second}\n• Note: <i>{esc(note)}</i>",
                    parse_mode="HTML"
                )
        except Exception as e:
            logging.error(f"Unexpected error finalizing check-in for {u_id}: {e}")
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id,
                    text="⚠️ Something went wrong saving your check-in — please try again in a moment.",
                    parse_mode="HTML"
                )
            except Exception:
                pass

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


# ------------------------------------------------------------------
# REGISTRATION GATEKEEPER
# ------------------------------------------------------------------
# NEW: anyone who talks to this bot without an existing row in `clients`
# (i.e. they never went through the separate onboarding/registration bot)
# gets redirected there instead of being able to poke at commands,
# check-ins, or the menu.
def build_registration_prompt():
    """Bilingual 'please register first' message + a URL button that
    deep-links straight into the onboarding bot. A URL button (not a
    callback) so it works even for someone with zero prior interaction."""
    text = (
        "🔒 <b>እስካሁን አልተመዘገቡም! / You're not registered yet!</b>\n\n"
        "ይህ ቦት ምዝገባቸውን ላጠናቀቁ ደንበኞች ብቻ የተዘጋጀ ነው። እባክዎ መጀመሪያ ከታች ባለው ቁልፍ ይመዝገቡ፣ "
        "ከዚያ ወደዚህ ተመልሰው ይምጡ!\n"
        "<i>This bot is only for clients who've completed sign-up. Please register "
        "using the button below first, then come back here.</i>"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 ይመዝገቡ / Register Here", url=f"https://t.me/{REGISTRATION_BOT_USERNAME}")]
    ])
    return text, markup


async def is_registered(user_id: int) -> bool:
    """True if a `clients` row exists for this Telegram user id at all —
    independent of `plan_ready` (which only gates whether their plan is
    built yet, for people who ARE registered).

    On a DB error we fail OPEN (treat as registered) rather than locking
    out real clients over a transient Supabase hiccup — same defensive
    posture used elsewhere in this file (e.g. start_command's gatekeeper
    try/except). Worst case on a DB error is a not-yet-registered visitor
    briefly sees normal bot behavior instead of the registration prompt.
    """
    try:
        res = supabase.table("clients").select("id").eq("id", user_id).limit(1).execute()
        return bool(res.data)
    except Exception as e:
        logging.error(f"Registration check failed for {user_id}: {e}")
        return True


async def guard_registered_message(update: Update) -> bool:
    """Gate for command/message entry points. Returns True and does
    nothing if the caller is registered. Returns False (after sending the
    registration prompt as a reply) if they aren't — caller should return
    immediately."""
    user = update.effective_user
    if not user:
        return False
    if await is_registered(user.id):
        return True
    text, markup = build_registration_prompt()
    await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    return False


async def guard_registered_callback(update: Update) -> bool:
    """Same as guard_registered_message, but for callback-query (button)
    entry points — edits the existing message instead of sending a new
    one, falling back to a reply if the edit fails for any reason."""
    query = update.callback_query
    user = query.from_user
    if await is_registered(user.id):
        return True
    text, markup = build_registration_prompt()
    try:
        await query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await query.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    return False


def parse_supabase_timestamp(ts: str) -> datetime:
    normalized = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_eat_date(ts: str) -> date:
    """Bucket a Supabase timestamp by its EAT calendar date, not raw UTC
    date.

    FIX: EAT is UTC+3, so a check-in made between 00:00-03:00 EAT lands on
    the *previous* UTC calendar date when you just call .date() on the UTC
    timestamp. That mismatched the "already checked in today?" logic
    (which correctly uses EAT day boundaries via get_today_start_utc)
    against the streak-bucketing logic (which was using raw UTC dates),
    causing late-night/early-morning check-ins to be miscounted into the
    wrong day for streak purposes.
    """
    return parse_supabase_timestamp(ts).astimezone(EAT_TIMEZONE).date()


def compute_current_streak(log_dates: set, today: date) -> int:
    """True consecutive-day streak ending today or yesterday, in EAT.

    FIX: previously "streak" was just len(distinct dates in a trailing
    7-day window) — not an actual consecutive streak. That silently
    tolerated gaps (miss a day, resume later, and the count doesn't reset)
    and was hard-capped at 7 even for genuinely longer streaks. This walks
    backward day-by-day from today (or yesterday, if today's check-in
    hasn't happened yet) and stops at the first gap.
    """
    if today in log_dates:
        cursor = today
    elif (today - timedelta(days=1)) in log_dates:
        cursor = today - timedelta(days=1)
    else:
        return 0

    streak = 0
    while cursor in log_dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


# ------------------------------------------------------------------
# BILINGUAL DATE LABELS & CHECK-IN HISTORY GRID
# ------------------------------------------------------------------
AMHARIC_WEEKDAYS = {
    0: "ሰኞ",     # Monday
    1: "ማክሰኞ",   # Tuesday
    2: "ረቡዕ",    # Wednesday
    3: "ሐሙስ",    # Thursday
    4: "ዓርብ",    # Friday
    5: "ቅዳሜ",    # Saturday
    6: "እሁድ",    # Sunday
}

# Grid columns run Sunday → Saturday, expressed as Python .weekday() values
# (Mon=0 ... Sun=6).
#
# FIX: the header used to be a hand-typed string ("     S  M  T  W  T  F  S")
# that assumed each weekday letter is single-width. But the data rows below
# it are built from emoji (🟢🔴🟡⚪⬛), which render roughly double-width in
# Telegram's monospace font — so the plain-letter header drifted out of
# alignment with the boxes underneath, especially by the later columns.
# Using full-width Unicode letters (same visual width as an emoji glyph)
# and assembling the header with the exact same "prefix + single-space-
# joined cells" pattern used for each week row keeps the two in sync
# instead of relying on two independently hand-tuned spacing strings.
_GRID_DAY_LETTERS = ["Ｓ", "Ｍ", "Ｔ", "Ｗ", "Ｔ", "Ｆ", "Ｓ"]
GRID_HEADER = "     " + " ".join(_GRID_DAY_LETTERS)


def bilingual_date_label(d: date) -> str:
    """e.g. '📅 ዓርብ Fri, Aug 21' — Amharic weekday + English weekday/date."""
    am_day = AMHARIC_WEEKDAYS[d.weekday()]
    en_part = d.strftime("%a, %b ") + str(d.day)
    return f"📅 {am_day} {en_part}"


def first_name(full_name: str) -> str:
    if not full_name:
        return "there"
    return full_name.strip().split()[0]


def build_checkin_grid(log_dates: set, today: date, weeks: int = 2, client_since: date | None = None):
    """Builds the Sunday→Saturday weekly check-in grid used in profile/admin views.

    Returns (grid_text, current_streak, adherence_pct). Cells are:
      🟢 logged
      🔴 missed — a day that has already fully passed with no log
      🟡 today, pending — today hasn't ended yet and no log exists yet
         (FIX: today used to render 🔴 the instant midnight hit, even
         mid-morning before the client had a chance to check in, which
         reads as "you already failed today." 🟡 distinguishes "still
         time to log this" from an actual miss, and — like ⚪ — is NOT
         counted in the adherence % below, since the day isn't over.)
      ⚪ a future day that hasn't happened yet
      ⬛ before the client's `client_since` date — never counted as a miss

    Adherence is green / elapsed-days within the displayed window,
    excluding ⬛ (pre-enrollment), ⚪ (future), and 🟡 (today, pending).
    """
    # Most recent Sunday on/before today starts "this week".
    # Python weekday(): Monday=0 ... Sunday=6.
    days_since_sunday = (today.weekday() + 1) % 7
    start_of_this_week = today - timedelta(days=days_since_sunday)
    start_of_grid = start_of_this_week - timedelta(days=7 * (weeks - 1))

    rows = []
    elapsed = 0
    green = 0
    for w in range(weeks):
        week_start = start_of_grid + timedelta(days=7 * w)
        cells = []
        for i in range(7):
            d = week_start + timedelta(days=i)
            if client_since and d < client_since:
                cells.append("⬛")
            elif d > today:
                cells.append("⚪")
            elif d == today:
                if d in log_dates:
                    cells.append("🟢")
                    elapsed += 1
                    green += 1
                else:
                    cells.append("🟡")  # pending — day isn't over, not a miss yet
            elif d in log_dates:
                cells.append("🟢")
                elapsed += 1
                green += 1
            else:
                cells.append("🔴")
                elapsed += 1
        rows.append(f"Wk{w + 1}: " + " ".join(cells))

    streak = compute_current_streak(log_dates, today)
    adherence = round((green / elapsed) * 100) if elapsed else 0

    grid_lines = [GRID_HEADER] + rows
    grid_lines.append(f"     └─🔥 {streak}-day streak" if streak > 0 else "     └─ no active streak")
    grid_text = "\n".join(grid_lines)
    return grid_text, streak, adherence


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
    their current, true consecutive check-in streak."""
    try:
        res = supabase.table("clients").select("id, package, goal, full_name").eq("is_active", True).execute()
        if not res.data:
            return

        clients = [c for c in res.data if "Meal Plan Only" not in (c.get("package") or "")]
        if not clients:
            return

        lookback = (datetime.now(timezone.utc) - timedelta(days=STREAK_LOOKBACK_DAYS)).isoformat()
        logs_res = supabase.table("daily_logs").select("client_id, created_at").in_("client_id", [c["id"] for c in clients]).gte("created_at", lookback).execute()

        logs_by_client = defaultdict(set)
        for log in (logs_res.data or []):
            logs_by_client[log["client_id"]].add(to_eat_date(log["created_at"]))

        # Same variant for everyone on a given goal each day, rotating daily.
        day_index = datetime.now(EAT_TIMEZONE).timetuple().tm_yday
        today_eat = datetime.now(EAT_TIMEZONE).date()
        yesterday_eat = today_eat - timedelta(days=1)

        for client in clients:
            goal_key = "muscle" if client.get("goal") == "goal_muscle" else "fat_loss"
            name = first_name(client.get("full_name"))
            log_dates = logs_by_client.get(client["id"], set())

            streak = compute_current_streak(log_dates, today_eat)
            prior_streak = compute_current_streak(log_dates, yesterday_eat)

            # FIX: a real streak (>=3) that just broke yesterday gets a
            # dedicated comeback message instead of the regular rotating
            # variant — "yesterday doesn't define today" lands better than
            # a generic "New day, new lobby!" right after a miss.
            if streak == 0 and prior_streak >= 3:
                text = comeback_message(name)
            else:
                variants = MOTIVATION_VARIANTS[goal_key]
                template = variants[day_index % len(variants)]
                text = template(streak, name)
                if (streak + 1) in MILESTONE_DAYS:
                    text += milestone_teaser(streak + 1)

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
        res = supabase.table("clients").select("id, full_name, package, goal, created_at").eq("is_active", True).execute()
        if not res.data: return
        clients = [c for c in res.data if "Meal Plan Only" not in (c.get("package") or "")]
        if not clients: return

        # FIX: widened from a 7-day lookback to STREAK_LOOKBACK_DAYS so the
        # per-client compact grid (2 weeks) and its streak footer have
        # enough history, instead of only ever being able to show a
        # same-week streak.
        lookback = (datetime.now(timezone.utc) - timedelta(days=STREAK_LOOKBACK_DAYS)).isoformat()
        logs_res = supabase.table("daily_logs").select("client_id, created_at").in_("client_id", [c["id"] for c in clients]).gte("created_at", lookback).execute()

        logs_by_client = defaultdict(set)
        for log in (logs_res.data or []):
            # FIX: EAT-localized date bucketing (see to_eat_date docstring).
            logs_by_client[log["client_id"]].add(to_eat_date(log["created_at"]))

        today_eat = datetime.now(EAT_TIMEZONE).date()
        report_lines = ["📥 <b>WEEKLY REVIEW QUEUE FOR SCIENTIFIC SIMON</b>\n"]
        for client in clients:
            log_dates = logs_by_client.get(client["id"], set())
            client_since = to_eat_date(client["created_at"])
            grid_text, streak, adherence = build_checkin_grid(log_dates, today_eat, weeks=3, client_since=client_since)
            report_lines.append(
                f"<b>{esc(client.get('full_name', 'Client'))}</b> ({esc(client.get('package', 'N/A'))})\n"
                f"{esc(grid_text)}\n"
                f"📈 {adherence}% adherence\n"
            )

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
            # FIX: .get("package", default) only falls back when the KEY is
            # missing, not when the value is NULL. A client row with
            # package IS NULL made pkg = None here, and the membership
            # check below ("Meal Plan Only" in pkg) raised a TypeError that
            # aborted this whole try block — silently skipping every
            # remaining client in the loop for renewal/testimonial checks.
            pkg = client.get("package") or "Meal Plan Only (2 Months)"
            cycle_start_raw = client.get("package_started_at") or client["created_at"]
            days_active = (now - parse_supabase_timestamp(cycle_start_raw)).days

            # FIX: exact-match dict lookup instead of an ordered chain of
            # substring checks. The old chain matched "Elite Transformation
            # (90 Days)" against the "Transformation" branch (60 days)
            # before ever reaching "Elite" (90 days), so Elite clients got
            # their renewal/testimonial nudges a full month early. See
            # TIER_DURATION_DAYS definition above.
            completion_days = TIER_DURATION_DAYS.get(pkg, 60)

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
        msg = f"🎉 <b>Package Upgraded!</b>\nYour account has been updated to <b>{tier_name}</b>." if lang == "en" else f"🎉 <b>ፓኬጅዎ ተሻሽሏል! / Package Upgraded!</b>\nመለያዎ ወደ <b>{display_package(tier_name, lang)}</b> ከፍ ብሏል። እንኳን ደስ አለዎት!"
        await send_message_safely(context, chat_id=int(target_id), text=msg, parse_mode="HTML")
        await update.message.reply_text(f"✅ Client updated to **{tier_name}**!")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Failed: {e}")


async def admin_set_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /setprice <tier name> <etb> <usd> — updates a tier's price
    live in the Supabase "pricing" table, no code deploy needed. Reuses
    an upsert so it works whether the tier row already exists or not."""
    if update.effective_user.id not in ADMIN_USER_IDS: return
    if len(context.args) < 3:
        await update.message.reply_text(
            f"⚠️ Usage: `/setprice <tier name> <price_etb> <price_usd>`\n"
            f"Tiers: {', '.join(FALLBACK_PRICES.keys())}\n\n"
            f"Example: `/setprice Kickstart (21 Days) 4800 55`",
            parse_mode="HTML"
        )
        return

    price_etb_raw, price_usd_raw = context.args[-2], context.args[-1]
    tier_name = " ".join(context.args[:-2])

    if tier_name not in FALLBACK_PRICES:
        await update.message.reply_text(f"❌ Invalid Tier Name!\nTiers: {', '.join(FALLBACK_PRICES.keys())}")
        return

    try:
        price_etb = int(price_etb_raw)
        price_usd = int(price_usd_raw)
    except ValueError:
        await update.message.reply_text("❌ Prices must be whole numbers, e.g. `/setprice Kickstart (21 Days) 4800 55`")
        return

    try:
        supabase.table("pricing").upsert({
            "tier_name": tier_name,
            "price_etb": price_etb,
            "price_usd": price_usd,
        }).execute()
        await update.message.reply_text(
            f"✅ <b>{esc(tier_name)}</b> updated to {price_etb:,} ETB / ${price_usd}.",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Failed to update price: {e}")


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

        logs_res = supabase.table("daily_logs").select("created_at").eq("client_id", c["id"]).gte(
            "created_at", (datetime.now(timezone.utc) - timedelta(days=STREAK_LOOKBACK_DAYS)).isoformat()
        ).execute()
        log_dates = {to_eat_date(l["created_at"]) for l in (logs_res.data or [])}
        today_eat = datetime.now(EAT_TIMEZONE).date()
        client_since = to_eat_date(c["created_at"])
        grid_text, streak, adherence = build_checkin_grid(log_dates, today_eat, client_since=client_since)

        info = (
            f"👤 <b>INFO: {esc(c.get('full_name'))}</b>\n"
            f"• <b>ID:</b> {c['id']}\n• <b>Package:</b> {esc(c.get('package'))}\n"
            f"• <b>Goal:</b> {esc(c.get('goal'))}\n• <b>Active:</b> {days} days\n"
            f"• <b>Status:</b> {'🟢' if c.get('is_active') else '🔴'}\n"
            f"• <b>Plan Ready:</b> {'✅ Yes' if c.get('plan_ready') else '⏳ Pending'}\n\n"
            f"📅 <b>CHECK-IN HISTORY</b>\n\n"
            f"{esc(grid_text)}\n"
            f"📈 {adherence}% adherence this cycle"
        )
        await update.message.reply_text(info, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")


async def admin_send_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return

    reply = update.message.reply_to_message

    if len(context.args) < 2 or not reply:
        await update.message.reply_text(
            "⚠️ Usage: Reply to a file/photo with `/sendplan [client_id] [meal|workout]`",
            parse_mode="HTML"
        )
        return

    file_id = None
    if reply.document:
        file_id = reply.document.file_id
    elif reply.photo:
        file_id = reply.photo[-1].file_id
    elif reply.video:
        file_id = reply.video.file_id
    else:
        await update.message.reply_text("❌ Replied message contains no valid file or photo.")
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
        res = supabase.table("clients").select("id, full_name, package, is_active, plan_ready, created_at").order("created_at", desc=True).execute()
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
            c_id = client.get("id", "?")
            pkg = esc(client.get("package", "Standard"))
            status = "🟢" if client.get("is_active") else "🔴"
            text += f"• {status} {name} (<code>{c_id}</code>) | <i>{pkg}</i>\n"

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


async def admin_view_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /media [photo|voice|video] [limit]
    Lists the most recent saved client photos, voice notes, or videos,
    resolved to the client's name, and renders each one inline (mirrors
    /questions, which only covers media_type == 'text').

    FIX: previously only "photo" and "voice" were valid values for the
    first argument. /media video silently fell through to the numeric-
    limit branch, failed int("video") and was swallowed by a bare
    except, and quietly ran /media photo instead — a check-in video was
    saved to client_media but had no way to be retrieved. "video" is now
    accepted the same way photo/voice are.
    """
    if update.effective_user.id not in ADMIN_USER_IDS:
        return

    media_type = "photo"
    limit = 10
    args = context.args or []
    if len(args) >= 1:
        if args[0].lower() in ("photo", "voice", "video"):
            media_type = args[0].lower()
            if len(args) >= 2:
                try:
                    limit = max(1, min(20, int(args[1])))
                except ValueError:
                    pass
        else:
            try:
                limit = max(1, min(20, int(args[0])))
            except ValueError:
                pass

    try:
        res = (
            supabase.table("client_media")
            .select("client_id, telegram_file_id, message_text, created_at")
            .eq("media_type", media_type)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = res.data or []
        if not rows:
            await update.message.reply_text(f"📭 No saved {media_type} messages found.")
            return

        client_ids = list({r["client_id"] for r in rows})
        names_res = supabase.table("clients").select("id, full_name").in_("id", client_ids).execute()
        name_map = {c["id"]: c.get("full_name", "Unknown") for c in (names_res.data or [])}

        # FIX: "video" now supported alongside "photo"/"voice" (see docstring above).
        icon = "📸" if media_type == "photo" else ("🎙️" if media_type == "voice" else "🎥")
        await update.message.reply_text(f"{icon} <b>RECENT CLIENT {media_type.upper()}S (last {len(rows)})</b>", parse_mode="HTML")

        for r in rows:
            name = esc(name_map.get(r["client_id"], "Unknown"))
            when = ""
            try:
                when = parse_supabase_timestamp(r["created_at"]).astimezone(EAT_TIMEZONE).strftime("%b %d, %I:%M %p")
            except Exception:
                pass
            caption = f"{icon} <b>{name}</b> (<code>{r['client_id']}</code>){' — ' + when if when else ''}"
            if r.get("message_text"):
                caption += f"\n<i>{esc(r['message_text'])}</i>"

            try:
                if media_type == "photo":
                    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=r["telegram_file_id"], caption=caption, parse_mode="HTML")
                elif media_type == "voice":
                    await context.bot.send_voice(chat_id=update.effective_chat.id, voice=r["telegram_file_id"], caption=caption, parse_mode="HTML")
                else:
                    await context.bot.send_video(chat_id=update.effective_chat.id, video=r["telegram_file_id"], caption=caption, parse_mode="HTML")
            except Exception as send_err:
                logging.error(f"Failed to render {media_type} row for admin: {send_err}")
                await update.message.reply_text(caption, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Error fetching client media: {e}")
        await update.message.reply_text(f"⚠️ Failed to fetch media: {e}")


async def admin_send_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return

    reply = update.message.reply_to_message
    if not context.args or not reply:
        await update.message.reply_text(
            "⚠️ Usage: Reply to any media or file with `/sendfile [client_id]`",
            parse_mode="HTML"
        )
        return

    try:
        c_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Error: Client ID must be a valid number.")
        return

    caption = update.message.caption or "📁 <b>ከሳይመን የተላከ ተጨማሪ ሰነድ / Additional Document from Coach</b>"

    try:
        if reply.document:
            await context.bot.send_document(chat_id=c_id, document=reply.document.file_id, caption=caption, parse_mode="HTML")
        elif reply.photo:
            await context.bot.send_photo(chat_id=c_id, photo=reply.photo[-1].file_id, caption=caption, parse_mode="HTML")
        elif reply.video:
            await context.bot.send_video(chat_id=c_id, video=reply.video.file_id, caption=caption, parse_mode="HTML")
        else:
            await update.message.reply_text("❌ Replied message contains no valid file, photo, or video.")
            return

        await update.message.reply_text(f"✅ Attachment successfully delivered to client `{c_id}`!", parse_mode="HTML")
    except Forbidden:
        await update.message.reply_text(f"❌ Failed: Client `{c_id}` has blocked the bot.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send: {e}")


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
        client_exists = bool(res.data)
        if client_exists:
            client_record = res.data[0]
            plan_is_ready = client_record.get("plan_ready", False)
            lang = client_record.get("language", "am")
        else:
            plan_is_ready = False
            lang = "am"
    except Exception as e:
        logging.error(f"Gatekeeper check error for {user_id}: {e}")
        # Can't confirm registration status on a DB error — fail toward
        # "registered but not ready yet" rather than turning away someone
        # who's actually a real client just because Supabase hiccuped.
        client_exists = True
        plan_is_ready = False
        lang = "am"

    # NEW: no client row at all -> not registered -> send to the
    # onboarding bot instead of the "your plan is being built" message.
    if not client_exists:
        text, markup = build_registration_prompt()
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
        return

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
        "እንኳን ወደ ሳይመን ኦሪጅን የክትትል እና ኮቺንግ ፖርታል በደህና መጡ! 🎯\n"
        "Welcome to Simon Origin Tracking & Coaching Portal! 🎯\n\n"
        "ቋንቋ ለመቀየር ወይም ለመጀመር ከታች ያሉትን ይጫኑ / Select an option below:",
        reply_markup=markup,
    )


async def checkin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /checkin text command by opening the daily check-in flow."""
    user = update.effective_user
    if not user:
        return
    if not await guard_registered_message(update):
        return

    today_eat = datetime.now(EAT_TIMEZONE).date()
    try:
        today_start = get_today_start_utc()
        if supabase.table("daily_logs").select("id").eq("client_id", user.id).gte("created_at", today_start).execute().data:
            await update.message.reply_text(
                f"✅ <b>ዛሬ ቀድመው ተመዝግበዋል! / You've already checked in today!</b> — {bilingual_date_label(today_eat)}\n"
                "አስደናቂ ወጥነት! ተከታታይነትዎ እንዳይቋረጥ ነገ ይመለሱ! 🔥",
                parse_mode="HTML"
            )
            return

        res = supabase.table("clients").select("goal").eq("id", user.id).execute()
        goal = res.data[0].get("goal") if res.data else "goal_fat_loss"
    except Exception as e:
        logging.error(f"Error in checkin_command for {user.id}: {e}")
        goal = "goal_fat_loss"

    context.user_data["checkin_goal"] = goal
    text, markup = _checkin_step1_card(goal, today_eat)

    try:
        sent = await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
        context.user_data["checkin_chat_id"] = sent.chat_id
        context.user_data["checkin_msg_id"] = sent.message_id
    except Exception as err:
        logging.error(f"Failed to send checkin prompt to {user.id}: {err}")


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /profile text command to display client details."""
    user = update.effective_user
    if not user:
        return
    if not await guard_registered_message(update):
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

        logs_res = supabase.table("daily_logs").select("created_at").eq("client_id", c["id"]).gte(
            "created_at", (datetime.now(timezone.utc) - timedelta(days=STREAK_LOOKBACK_DAYS)).isoformat()
        ).execute()
        log_dates = {to_eat_date(l["created_at"]) for l in (logs_res.data or [])}
        today_eat = datetime.now(EAT_TIMEZONE).date()
        client_since = to_eat_date(c["created_at"])
        grid_text, streak, adherence = build_checkin_grid(log_dates, today_eat, client_since=client_since)

        pkg_display = display_package(c.get("package"), lang)

        if lang == "en":
            text = (
                f"👤 <b>YOUR COACHING PROFILE</b>\n\n"
                f"• <b>Name:</b> {esc(c.get('full_name'))}\n"
                f"• <b>Package:</b> {esc(pkg_display)}\n"
                f"• <b>Goal:</b> {esc(c.get('goal'))}\n"
                f"• <b>Days Active:</b> {days}\n\n"
                f"📅 <b>CHECK-IN HISTORY</b>\n\n"
                f"{esc(grid_text)}\n"
                f"📈 {adherence}% adherence this cycle"
            )
        else:
            text = (
                f"👤 <b>የኮቺንግ መለያዎ / YOUR PROFILE</b>\n\n"
                f"• <b>ስም:</b> {esc(c.get('full_name'))}\n"
                f"• <b>የፓኬጅ ዓይነት:</b> {esc(pkg_display)}\n"
                f"• <b>ዋና ግብ:</b> {esc(c.get('goal'))}\n"
                f"• <b>የቆይታ ጊዜ:</b> {days} ቀናት\n\n"
                f"📅 <b>የክትትል ታሪክ / CHECK-IN HISTORY</b>\n\n"
                f"{esc(grid_text)}\n"
                f"📈 {adherence}% adherence this cycle"
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
    await query.message.edit_text(f"✅ <b>{text}</b>\n\nእንኳን ወደ ሳይመን ኦሪጅን ፖርታል በደህና መጡ! ከታች ይምረጡ:", reply_markup=markup, parse_mode="HTML")


async def handle_client_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # FIX: this button previously duplicated /profile with its own,
    # simpler implementation — a naive "logs in the last 7 days" count
    # instead of the real consecutive-day streak, and no check-in grid
    # or adherence %. That meant the button and /profile could show
    # different numbers for the same client. Now both paths share the
    # exact same build_checkin_grid() output.
    query = update.callback_query
    await query.answer()
    if not await guard_registered_callback(update):
        return
    lang = await get_client_language(query.from_user.id)
    try:
        res = supabase.table("clients").select("*").eq("id", query.from_user.id).execute()
        if not res.data:
            await query.message.reply_text("📋 Profile not found. Please register first!" if lang == "en" else "📋 መረጃዎ አልተገኘም። እባክዎ መጀመሪያ ይመዝገቡ!")
            return

        c = res.data[0]
        days = (datetime.now(timezone.utc) - parse_supabase_timestamp(c["created_at"])).days

        logs_res = supabase.table("daily_logs").select("created_at").eq("client_id", c["id"]).gte(
            "created_at", (datetime.now(timezone.utc) - timedelta(days=STREAK_LOOKBACK_DAYS)).isoformat()
        ).execute()
        log_dates = {to_eat_date(l["created_at"]) for l in (logs_res.data or [])}
        today_eat = datetime.now(EAT_TIMEZONE).date()
        client_since = to_eat_date(c["created_at"])
        grid_text, streak, adherence = build_checkin_grid(log_dates, today_eat, client_since=client_since)

        pkg_display = display_package(c.get("package"), lang)

        if lang == "en":
            text = (
                f"👤 <b>YOUR COACHING PROFILE</b>\n\n"
                f"• <b>Name:</b> {esc(c.get('full_name'))}\n"
                f"• <b>Package:</b> {esc(pkg_display)}\n"
                f"• <b>Goal:</b> {esc(c.get('goal'))}\n"
                f"• <b>Days Active:</b> {days}\n\n"
                f"📅 <b>CHECK-IN HISTORY</b>\n\n"
                f"{esc(grid_text)}\n"
                f"📈 {adherence}% adherence this cycle"
            )
        else:
            text = (
                f"👤 <b>የኮቺንግ መለያዎ / YOUR PROFILE</b>\n\n"
                f"• <b>ስም:</b> {esc(c.get('full_name'))}\n"
                f"• <b>የፓኬጅ ዓይነት:</b> {esc(pkg_display)}\n"
                f"• <b>ዋና ግብ:</b> {esc(c.get('goal'))}\n"
                f"• <b>የቆይታ ጊዜ:</b> {days} ቀናት\n\n"
                f"📅 <b>የክትትል ታሪክ / CHECK-IN HISTORY</b>\n\n"
                f"{esc(grid_text)}\n"
                f"📈 {adherence}% adherence this cycle"
            )
        await query.message.reply_text(text, parse_mode="HTML")
    except Exception:
        await query.message.reply_text("⚠️ Error loading profile.")


async def handle_target_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await guard_registered_callback(update):
        return
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
    if not await guard_registered_callback(update):
        return
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

    # FIX: prices used to be hardcoded per-currency into these labels
    # directly. Now the tier name/emoji/callback stay static here (those
    # don't change), but the price itself is pulled live from
    # get_tier_prices() (Supabase "pricing" table, admin-editable via
    # /setprice) so a price change no longer needs a code deploy.
    prices = get_tier_prices()
    tier_meta = [
        ("Kickstart (21 Days)", "⚡", "Kickstart (21 Days)", "upgrade_kickstart"),
        ("Transformation (60 Days)", "🔥", "Transformation (60 Days)", "upgrade_transformation"),
        ("Elite Transformation (90 Days)", "🥇", "Elite (90 Days)", "upgrade_elite"),
        ("Lifestyle Coaching (6 Months)", "🌟", "Lifestyle (6 Months)", "upgrade_lifestyle"),
        ("VIP Coaching (6 Months)", "👑", "VIP Coaching (6 Months)", "upgrade_vip"),
    ]

    def _tier_label(tier_name: str, emoji: str, short_name: str) -> str:
        p = prices.get(tier_name, FALLBACK_PRICES.get(tier_name, {}))
        amount = f"{p.get('etb', '?'):,} ETB" if loc_type == "et" else f"${p.get('usd', '?')}"
        return f"{emoji} {short_name} — {amount}"

    rows = [(tier_name, _tier_label(tier_name, emoji, short_name), cb) for (tier_name, emoji, short_name, cb) in tier_meta]
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
    if not await guard_registered_callback(update):
        return
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
    """Fires from the '🚀 Check In Now' reminder button. Edits that same
    reminder message in place into Step 1/3, instead of sending a new
    message on top of it."""
    query = update.callback_query
    await query.answer()
    if not await guard_registered_callback(update):
        return
    u_id = query.from_user.id
    today_eat = datetime.now(EAT_TIMEZONE).date()

    try:
        today_start = get_today_start_utc()
        if supabase.table("daily_logs").select("id").eq("client_id", u_id).gte("created_at", today_start).execute().data:
            await query.message.edit_text(
                f"✅ <b>ዛሬ ቀድመው ተመዝግበዋል! / You've already checked in today!</b> — {bilingual_date_label(today_eat)}\n"
                "አስደናቂ ወጥነት! ተከታታይነትዎ እንዳይቋረጥ ነገ ይመለሱ! 🔥",
                parse_mode="HTML"
            )
            return

        res = supabase.table("clients").select("goal").eq("id", u_id).execute()
        goal = res.data[0].get("goal") if res.data else "goal_fat_loss"
    except Exception as e:
        logging.error(f"Error in trigger_daily_checkin for {u_id}: {e}")
        goal = "goal_fat_loss"

    context.user_data["checkin_goal"] = goal
    text, markup = _checkin_step1_card(goal, today_eat)

    try:
        await query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        context.user_data["checkin_chat_id"] = query.message.chat_id
        context.user_data["checkin_msg_id"] = query.message.message_id
    except Exception as err:
        logging.error(f"Failed to send checkin prompt to {u_id}: {err}")


async def handle_checkin_responses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Every step edits the SAME card in place (one evolving message,
    Step 1/3 → 2/3 → 3/3) rather than sending a fresh bubble each time."""
    query = update.callback_query
    await query.answer()
    if not await guard_registered_callback(update):
        return
    u_id = query.from_user.id
    today_eat = datetime.now(EAT_TIMEZONE).date()

    # Always keep the tracked message pointer current, in case this flow
    # was resumed from a stale card or a different entry point.
    context.user_data["checkin_chat_id"] = query.message.chat_id
    context.user_data["checkin_msg_id"] = query.message.message_id

    if query.data in ("log_nut_hit", "log_nut_miss"):
        context.user_data["checkin_nut"] = "Hit" if query.data == "log_nut_hit" else "Missed"

        goal = context.user_data.get("checkin_goal")
        if not goal:
            try:
                res = supabase.table("clients").select("goal").eq("id", u_id).execute()
                goal = res.data[0].get("goal", "goal_fat_loss") if res.data else "goal_fat_loss"
            except Exception as e:
                logging.error(f"Error fetching goal for {u_id} in checkin flow: {e}")
                goal = "goal_fat_loss"
            context.user_data["checkin_goal"] = goal

        text, markup = _checkin_step2_card(goal, today_eat)
        await query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")

    elif query.data in ("log_second_hit", "log_second_miss"):
        second = "Hit" if query.data == "log_second_hit" else "Missed"
        context.user_data["checkin_second"] = second
        context.user_data["awaiting_checkin_note"] = True
        context.user_data["awaiting_checkin_note_started_at"] = datetime.now(timezone.utc).isoformat()

        # Streak nudge on the note step is computed from logs *before*
        # today's check-in, since it hasn't been saved yet.
        try:
            logs = supabase.table("daily_logs").select("created_at").eq("client_id", u_id).gte(
                "created_at", (datetime.now(timezone.utc) - timedelta(days=STREAK_LOOKBACK_DAYS)).isoformat()
            ).execute().data
            streak_so_far = compute_current_streak({to_eat_date(l["created_at"]) for l in (logs or [])}, today_eat - timedelta(days=1))
        except Exception as e:
            logging.error(f"Error computing pre-checkin streak for {u_id}: {e}")
            streak_so_far = 0

        text, markup = _checkin_step3_card(streak_so_far, today_eat)
        await query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")

    elif query.data == "log_note_skip":
        nut = context.user_data.pop("checkin_nut", "Logged")
        second = context.user_data.pop("checkin_second", "Logged")
        goal = context.user_data.pop("checkin_goal", "goal_fat_loss")
        context.user_data["awaiting_checkin_note"] = False
        context.user_data.pop("awaiting_checkin_note_started_at", None)

        full_name = query.from_user.full_name
        await _finalize_checkin(
            context, u_id, full_name, goal, nut, second, note="No note",
            chat_id=query.message.chat_id, message_id=query.message.message_id
        )


async def handle_cancel_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel is available at every step (not just the note step), so a
    client who tapped into check-in by accident can back out immediately
    instead of being forced through nutrition + hydration first."""
    query = update.callback_query
    await query.answer()

    context.user_data["awaiting_checkin_note"] = False
    context.user_data.pop("awaiting_checkin_note_started_at", None)
    context.user_data.pop("checkin_nut", None)
    context.user_data.pop("checkin_second", None)
    context.user_data.pop("checkin_goal", None)
    context.user_data.pop("checkin_chat_id", None)
    context.user_data.pop("checkin_msg_id", None)

    try:
        await query.message.edit_text(
            "❌ <b>ክትትሉ ተሰርዟል / Check-in cancelled</b>\n"
            "ምንም አልተመዘገበም። ለመጀመር 'የዕለት ክትትል' ቁልፍን በማንኛውም ጊዜ ይጫኑ።\n"
            "<i>Nothing was saved. Tap Daily Check-In anytime to start over.</i>",
            parse_mode="HTML"
        )
    except Exception as err:
        logging.error(f"Failed to edit cancel-checkin message: {err}")


# ------------------------------------------------------------------
# MEDIA, Q&A, AND APPROVALS
# ------------------------------------------------------------------
async def handle_client_attachments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or not update.message: return

    # NEW: gate the catch-all handler too — this is the biggest surface
    # area (any text/photo/voice/video from anyone), so an unregistered
    # visitor rambling into the chat gets redirected instead of silently
    # having their message saved to client_media.
    if not await guard_registered_message(update):
        return

    if context.user_data.get("awaiting_checkin_note"):
        started_raw = context.user_data.get("awaiting_checkin_note_started_at")
        # FIX: previously discarded the client's already-given nutrition/
        # hydration answers after any 30-minute gap, forcing a full
        # restart. Now the only thing that invalidates in-progress answers
        # is the flow having started on a *different EAT calendar day* —
        # any gap within the same day just resumes normally.
        crossed_day = True
        if started_raw:
            try:
                started = datetime.fromisoformat(started_raw)
                crossed_day = to_eat_date(started.isoformat()) != datetime.now(EAT_TIMEZONE).date()
            except Exception:
                crossed_day = True

        if crossed_day:
            context.user_data["awaiting_checkin_note"] = False
            context.user_data.pop("awaiting_checkin_note_started_at", None)
            context.user_data.pop("checkin_nut", None)
            context.user_data.pop("checkin_second", None)
            context.user_data.pop("checkin_goal", None)
            await update.message.reply_text(
                "⏱️ <b>ክትትልዎ ጊዜው አልፎበታል / Your check-in session timed out</b>\n"
                "እባክዎ በድጋሚ ይሞክሩ፦ 'የዕለት ክትትል' ቁልፍን ይጫኑ።\n"
                "<i>A new day has started — please tap Daily Check-In again to log today's answers.</i>",
                parse_mode="HTML"
            )
            return

        context.user_data["awaiting_checkin_note"] = False
        context.user_data.pop("awaiting_checkin_note_started_at", None)
        nut = context.user_data.pop("checkin_nut", "Logged")
        second = context.user_data.pop("checkin_second", "Logged")
        goal = context.user_data.pop("checkin_goal", "goal_fat_loss")
        chat_id = context.user_data.pop("checkin_chat_id", None)
        message_id = context.user_data.pop("checkin_msg_id", None)

        # Any attached photo/voice/video is saved to client_media (same as
        # the general attachment path below) and referenced in the note;
        # its caption/text is used as the actual note content so nothing
        # a client sends here — text, photo, or voice — gets silently
        # dropped.
        note_media_type = None
        note_media_file_id = None
        if update.message.photo:
            note_media_type = "photo"
            note_media_file_id = update.message.photo[-1].file_id
        elif update.message.voice:
            note_media_type = "voice"
            note_media_file_id = update.message.voice.file_id
        elif update.message.video:
            note_media_type = "video"
            note_media_file_id = update.message.video.file_id

        if note_media_type:
            caption_text = (update.message.caption or "").strip()
            try:
                supabase.table("client_media").insert({
                    "client_id": u.id,
                    "media_type": note_media_type,
                    "telegram_file_id": note_media_file_id,
                    "message_text": caption_text,
                }).execute()
            except Exception as media_err:
                logging.error(f"Failed to save checkin-note {note_media_type} for {u.id}: {media_err}")
            note = f"[{note_media_type} attached]" + (f" — {caption_text}" if caption_text else "")
        else:
            note = update.message.text.strip() if update.message.text else "No note"

        if chat_id and message_id:
            # Normal path: edit the same tracked card into the final recap.
            await _finalize_checkin(context, u.id, u.full_name, goal, nut, second, note, chat_id, message_id)
        else:
            # Fallback (tracked message id lost, e.g. bot restart mid-flow):
            # save via a fresh message instead of failing silently.
            sent = await update.message.reply_text("⏳ Saving your check-in...", parse_mode="HTML")
            await _finalize_checkin(context, u.id, u.full_name, goal, nut, second, note, sent.chat_id, sent.message_id)
        return

    try:
        res = supabase.table("clients").select("package, full_name, plan_ready").eq("id", u.id).execute()
        if res.data:
            c = res.data[0]
            tier = c.get("package", "Meal Plan Only (2 Months)")
            plan_ready = c.get("plan_ready", True)
        else:
            tier = "Meal Plan Only (2 Months)"
            plan_ready = False
    except Exception:
        tier = "Meal Plan Only (2 Months)"
        plan_ready = False

    perms = TIER_PERMISSIONS.get(tier, TIER_PERMISSIONS["Meal Plan Only (2 Months)"])

    # FIX: while a client's plan is still being built (plan_ready is False),
    # they're usually still on the default restricted tier and can't yet
    # upgrade — but the /start intake question explicitly asks them to reply
    # with a text or voice note. Don't let the tier gate block that reply.
    if update.message.photo or update.message.video or update.message.voice:
        if plan_ready and not perms["allow_media"] and not update.message.photo:
            await update.message.reply_text("ማሳሰቢያ፦ የፎርም ግምገማዎች እና የድምጽ መልእክቶች ለ Transformation ፓኬጆች ብቻ የተሰጡ ናቸው። ለማሻሻል ከታች ይጫኑ:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏋️ Upgrade Plan", callback_data="upgrade_tier")]]))
            return

        m_type = "photo" if update.message.photo else ("video" if update.message.video else "voice")
        f_id = update.message.photo[-1].file_id if update.message.photo else (update.message.video.file_id if update.message.video else update.message.voice.file_id)

        try: supabase.table("client_media").insert({"client_id": u.id, "media_type": m_type, "telegram_file_id": f_id, "message_text": update.message.caption or ""}).execute()
        except Exception: pass

        # FIX: a client uploading a payment receipt is at the highest-anxiety
        # point of the flow ("did my money go through?"). Give them a
        # payment-specific confirmation instead of the generic attachment
        # acknowledgement everyone else gets.
        if update.message.photo and context.user_data.get("awaiting_payment_screenshot"):
            await update.message.reply_text(
                "💳 <b>ክፍያዎ ደርሶናል! / Payment Receipt Received!</b>\n"
                "ሳይመን በቅርቡ ያረጋግጣል፣ ከተረጋገጠ በኋላ ወዲያውኑ ፓኬጅዎ ይሻሻላል።\n"
                "<i>Simon will confirm shortly — your package upgrades the moment it's approved.</i>",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text("Got it! 📎 Your attachment has been saved for ሳይመን's review.", parse_mode="HTML")

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
        if plan_ready and not perms["allow_qa"]:
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
        client_lang = await get_client_language(int(t_id))
        msg = f"🎉 <b>Payment Approved!</b>\nUpgraded to <b>{tier}</b>." if client_lang == "en" else f"🎉 <b>ክፍያዎ ጸድቋል! / Payment Approved!</b>\nመለያዎ ወደ <b>{display_package(tier, client_lang)}</b> ከፍ ብሏል። እንኳን ደስ አለዎት!"
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
    app.add_handler(CommandHandler("setprice", admin_set_price))
    app.add_handler(CommandHandler("clientinfo", admin_client_info))
    app.add_handler(CommandHandler("sendplan", admin_send_plan))
    app.add_handler(CommandHandler("reply", admin_send_voice_feedback))
    app.add_handler(CommandHandler("status", admin_status_command))
    app.add_handler(CommandHandler("sendfile", admin_send_file))
    app.add_handler(CommandHandler("questions", admin_view_questions))
    app.add_handler(CommandHandler("media", admin_view_media))
    app.add_handler(CommandHandler("testmotivation", admin_test_motivation))
    app.add_handler(CommandHandler("testnudge", admin_test_nudge))
    app.add_handler(CommandHandler("testjob", admin_test_scheduled_job))

    app.add_handler(CallbackQueryHandler(handle_target_plan, pattern="^get_target_plan$"))
    app.add_handler(CallbackQueryHandler(handle_client_profile, pattern="^get_client_profile$"))
    app.add_handler(CallbackQueryHandler(trigger_daily_checkin, pattern="^start_checkin$"))
    app.add_handler(CallbackQueryHandler(handle_checkin_responses, pattern="^log_"))
    app.add_handler(CallbackQueryHandler(handle_cancel_checkin, pattern="^cancel_checkin$"))
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
