import os
import logging
import datetime
from typing import Dict, Any
from flask import Flask, request

# ======= FIX: Patch imghdr with Pillow =========
import sys, types
from PIL import Image

def what(file, h=None):
    try:
        img = Image.open(file)
        return img.format.lower()
    except:
        return None

imghdr_stub = types.ModuleType("imghdr")
imghdr_stub.what = what
sys.modules["imghdr"] = imghdr_stub
# ===============================================

from telegram import (
    Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Dispatcher, CommandHandler, CallbackQueryHandler,
    CallbackContext, JobQueue
)

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_USER_IDS = {7124683213}   # <-- replace with your admins
URL = os.getenv("RENDER_EXTERNAL_URL", "https://your-app.onrender.com")  # Render URL
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{URL}{WEBHOOK_PATH}"

bot = Bot(BOT_TOKEN)

# Flask app
app = Flask(__name__)

# ================== STORAGE =================
group_data: Dict[int, Dict[int, Dict[str, Any]]] = {}

# ================== LOGGING =================
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================== CONSTANTS =================
ACTIVITY_LIMITS = {
    "eat": {"limit_min": 30, "fine": 10},
    "toilet": {"limit_min": 15, "fine": 10},
    "smoke": {"limit_min": 10, "fine": 10},
    "meeting": {"limit_min": 60, "fine": 0},
}
LATE_WORK_FINE = 50

NAMES = {
    "work": "上班", "off": "下班", "eat": "吃饭",
    "toilet": "上厕所", "smoke": "抽烟", "meeting": "会议", "back": "回座"
}

# ================== HELPERS ==================
def ensure_user(chat_id: int, user_id: int, name: str):
    if chat_id not in group_data:
        group_data[chat_id] = {}
    users = group_data[chat_id]
    if user_id not in users:
        users[user_id] = {
            "name": name,
            "activities": [],
            "daily_fines": 0,
            "monthly_fines": 0,
            "work_start": None,
            "work_time": datetime.timedelta(),
            "pure_work_time": datetime.timedelta(),
            "total_activity_time": datetime.timedelta(),
        }
    return users[user_id]

def format_td(td: datetime.timedelta) -> str:
    total = int(td.total_seconds())
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    parts = []
    if h: parts.append(f"{h}小时")
    if m: parts.append(f"{m}分钟")
    if s or not parts: parts.append(f"{s}秒")
    return " ".join(parts)

def make_inline_menu() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(NAMES['work'], callback_data="work"),
         InlineKeyboardButton(NAMES['off'], callback_data="off")],
        [InlineKeyboardButton(NAMES['eat'], callback_data="eat"),
         InlineKeyboardButton(NAMES['toilet'], callback_data="toilet"),
         InlineKeyboardButton(NAMES['smoke'], callback_data="smoke")],
        [InlineKeyboardButton(NAMES['meeting'], callback_data="meeting")],
        [InlineKeyboardButton(NAMES['back'], callback_data="back")],
    ]
    return InlineKeyboardMarkup(kb)

# ================== COMMANDS ==================
def start(update: Update, context: CallbackContext):
    ensure_user(update.effective_chat.id, update.effective_user.id, update.effective_user.full_name)
    update.message.reply_text("📋 欢迎使用考勤机器人，请打卡：", reply_markup=make_inline_menu())

def report(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    if uid not in ADMIN_USER_IDS:
        update.message.reply_text("❌ 仅限管理员使用")
        return
    chat_id = update.effective_chat.id
    users = group_data.get(chat_id, {})
    lines = ["📅 每日考勤报告"]
    for u, d in users.items():
        lines.append(f"{d['name']} | 本日罚款 ${d['daily_fines']}, 本月罚款 ${d['monthly_fines']}")
    update.message.reply_text("\n".join(lines))

# ================== BUTTON HANDLER ==================
def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    chat_id = query.message.chat.id
    user_id = query.from_user.id
    name = query.from_user.full_name
    action = query.data
    now = datetime.datetime.now()
    user = ensure_user(chat_id, user_id, name)

    if action == "work":
        user["work_start"] = now
        if now.time() > datetime.time(hour=9, minute=0):
            user["daily_fines"] += LATE_WORK_FINE
            user["monthly_fines"] += LATE_WORK_FINE
            txt = f"✅ {name} 上班打卡 {now.strftime('%H:%M:%S')} (迟到罚款 ${LATE_WORK_FINE})"
        else:
            txt = f"✅ {name} 上班打卡 {now.strftime('%H:%M:%S')}"
        query.edit_message_text(txt, reply_markup=make_inline_menu())

    elif action == "off":
        if user.get("work_start"):
            dur = now - user["work_start"]
            user["work_time"] += dur
            user["pure_work_time"] = user["work_time"] - user["total_activity_time"]
            user["work_start"] = None
        txt = f"✅ {name} 下班打卡，总工时 {format_td(user['work_time'])}, 纯工时 {format_td(user['pure_work_time'])}"
        query.edit_message_text(txt, reply_markup=make_inline_menu())

    elif action == "back":
        if not user["activities"] or user["activities"][-1].get("end"):
            query.edit_message_text("⚠️ 当前没有活动", reply_markup=make_inline_menu())
            return
        last = user["activities"][-1]
        last["end"] = now
        dur = last["end"] - last["start"]
        user["total_activity_time"] += dur
        fine = 0
        conf = ACTIVITY_LIMITS.get(last["type"])
        if conf and dur.total_seconds() > conf["limit_min"]*60:
            fine = conf["fine"]
            user["daily_fines"] += fine
            user["monthly_fines"] += fine
        txt = f"✅ {name} 完成 {NAMES[last['type']]} 用时 {format_td(dur)}"
        if fine: txt += f"\n⚠️ 超时罚款 ${fine}"
        query.edit_message_text(txt, reply_markup=make_inline_menu())

    else:  # start activity
        user["activities"].append({"type": action, "start": now, "end": None})
        txt = f"✅ {name} 开始 {NAMES[action]} {now.strftime('%H:%M:%S')}"
        query.edit_message_text(txt, reply_markup=make_inline_menu())

# ================== DAILY JOB ==================
def daily_reset(context: CallbackContext):
    now = datetime.datetime.now()
    for chat_id, users in group_data.items():
        lines = [f"📅 每日总结 - {now.strftime('%Y-%m-%d')}"]
        for u, d in users.items():
            lines.append(f"{d['name']} 工时 {format_td(d['work_time'])}, 罚款 ${d['daily_fines']}")
            d["activities"] = []
            d["daily_fines"] = 0
            d["work_time"] = datetime.timedelta()
            d["pure_work_time"] = datetime.timedelta()
            d["total_activity_time"] = datetime.timedelta()
        context.bot.send_message(chat_id, "\n".join(lines))

# ================== MONTHLY JOB ==================
def monthly_reset(context: CallbackContext):
    now = datetime.datetime.now()
    for chat_id, users in group_data.items():
        lines = [f"📅 月度总结 - {now.strftime('%Y-%m')}"]
        for u, d in users.items():
            lines.append(f"{d['name']} 本月罚款 ${d['monthly_fines']}")
            d["monthly_fines"] = 0
        context.bot.send_message(chat_id, "\n".join(lines))

# ================== DISPATCHER ==================
dispatcher = Dispatcher(bot, None, workers=0, use_context=True)
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CommandHandler("report", report))
dispatcher.add_handler(CallbackQueryHandler(button_handler))

# JobQueue
job_queue = JobQueue(bot)
job_queue.set_dispatcher(dispatcher)
job_queue.start()

# Daily reset: 15:00 every day
job_queue.run_daily(daily_reset, time=datetime.time(hour=15, minute=0))
# Monthly reset: Day 1, 15:05
job_queue.run_monthly(
    monthly_reset,
    when=datetime.time(hour=15, minute=5),
    day=1
)

# ================== FLASK ROUTES ==================
@app.route("/", methods=["GET"])
def index():
    return "Bot is running!", 200

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return "ok", 200

def set_webhook():
    bot.delete_webhook()
    success = bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Webhook set: {success} -> {WEBHOOK_URL}")

if __name__ == "__main__":
    set_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
