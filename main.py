import asyncio
import logging
import os
import json
import re
from datetime import datetime, date

## Telegram ##
from telegram import (
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# Google Sheets
import gspread
from oauth2client.service_account import ServiceAccountCredentials


# ============================================================
# CONFIG
# ============================================================

TOKEN = "8428053990:AAF5GvsOr6JNgtZdqNyK0FDW1iBDZs3ygW4"
ADMIN_ID = 433247695

SPREAD_NAME = "Документи водіїв"
SHEET_NAME = "Drivers"

logging.basicConfig(level=logging.INFO)


# ============================================================
# GOOGLE AUTH (Railway + local)
# ============================================================

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

creds_raw = os.getenv("CREDENTIALS_JSON")

if creds_raw:
    creds_json = json.loads(creds_raw)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
else:
    print("⚠️ ENV missing — using local credentials.json")
    with open("credentials.json", "r") as f:
        creds_json = json.load(f)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)

client = gspread.authorize(creds)
sheet = client.open(SPREAD_NAME).worksheet(SHEET_NAME)

REQUIRED_COLUMNS = ["FULL_NAME", "TELEGRAM", "TYPE", "PLATE", "DOC_NAME", "DATE"]
if sheet.row_values(1) != REQUIRED_COLUMNS:
    sheet.delete_rows(1)
    sheet.insert_row(REQUIRED_COLUMNS, 1)


# ============================================================
# STATES
# ============================================================

(
    REG_ENTER_NAME,
    ADD_SELECT_TYPE,
    ADD_ENTER_PLATE,
    ADD_SELECT_DOC,
    ADD_ENTER_CUSTOM_DOC,
    ADD_ENTER_DATE,
    UPDATE_SELECT_DOC,
    UPDATE_ENTER_DATE,
    DELETE_SELECT_DOC,
) = range(9)


# ============================================================
# HELPERS
# ============================================================

def norm(text: str) -> str:
    return " ".join(text.upper().split())


def valid_plate(text: str) -> bool:
    return re.fullmatch(r"[A-ZА-Я]{2}[0-9]{4}[A-ZА-Я]{2}", text.upper()) is not None


def user_exists(uid) -> bool:
    return any(str(r["TELEGRAM"]) == str(uid) for r in sheet.get_all_records())


def get_user_docs(uid):
    return [r for r in sheet.get_all_records() if str(r["TELEGRAM"]) == str(uid)]


def get_valid_docs(uid):
    """Тільки реальні документи: без пустих номерів, назв і дат."""
    return [
        r for r in sheet.get_all_records()
        if str(r["TELEGRAM"]) == str(uid)
        and r["PLATE"]
        and r["DOC_NAME"]
        and r["DATE"]
    ]


def get_user_plates(uid):
    return sorted(
        {
            r["PLATE"]
            for r in sheet.get_all_records()
            if str(r["TELEGRAM"]) == str(uid) and r["PLATE"]
        }
    )


DOC_LABELS = {
    "TP": "ТЕХ ПАСПОРТ",
    "BC": "БІЛИЙ СЕРТИФІКАТ",
    "TO": "ТЕХ ОГЛЯД",
    "TACO": "КАЛІБРОВКА ТАХО",
    "INS": "СТРАХОВИЙ ПОЛІС",
    "GREEN": "ЗЕЛЕНА КАРТА",
}


# ============================================================
# START / REGISTRATION
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Точка входу: якщо користувач новий — просимо імʼя, інакше показуємо меню."""
    chat_id = update.effective_chat.id
    message = update.effective_message

    if not user_exists(chat_id):
        await message.reply_text(
            "Ви вперше користуєтесь ботом.\n"
            "Будь ласка, введіть ваше ІМ’Я ТА ПРІЗВИЩЕ:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return REG_ENTER_NAME

    # Відомий користувач — показуємо меню
    await message.reply_text(
        "Головне меню:",
        reply_markup=ReplyKeyboardMarkup(
            [
                ["➕ ДОДАТИ ДОКУМЕНТ", "📄 МОЇ ДОКУМЕНТИ"],
                ["✏️ ОНОВИТИ ДОКУМЕНТ", "🗑 ВИДАЛИТИ ДОКУМЕНТ"],
            ],
            resize_keyboard=True,
        ),
    )
    return ConversationHandler.END


async def register_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Збереження ПІБ при реєстрації."""
    full = update.message.text.strip()

    if len(full.split()) < 2:
        await update.message.reply_text("Введіть ім’я та прізвище повністю 📝")
        return REG_ENTER_NAME

    uid = update.message.chat_id

    # На всяк випадок, якщо рядок вже є — не дублюємо
    rows = sheet.get_all_records()
    existing = [r for r in rows if str(r["TELEGRAM"]) == str(uid)]
    if not existing:
        sheet.append_row([full, str(uid), "", "", "", ""])
    else:
        # Оновимо імʼя, якщо воно змінилось
        for i, r in enumerate(rows, start=2):
            if str(r["TELEGRAM"]) == str(uid):
                sheet.update_cell(i, 1, full)
                break

    await update.message.reply_text("Реєстрацію завершено ✔")

    # Показуємо меню
    await start(update, context)
    return ConversationHandler.END


# ============================================================
# ADD DOCUMENT
# ============================================================

async def add_doc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("🚗 АВТО", callback_data="AUTO")],
        [InlineKeyboardButton("🛞 ПРИЧІП", callback_data="TRAILER")],
    ]

    await update.message.reply_text(
        "Починаємо додавання документа…",
        reply_markup=ReplyKeyboardRemove(),
    )

    await update.message.reply_text(
        "Оберіть тип транспорту:",
        reply_markup=InlineKeyboardMarkup(kb),
    )

    return ADD_SELECT_TYPE


async def add_doc_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    context.user_data["vehicle_type"] = q.data
    await q.edit_message_text("Введіть номер (AA1234BB):")
    return ADD_ENTER_PLATE


async def add_doc_plate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plate = update.message.text.upper().strip()

    if not valid_plate(plate):
        await update.message.reply_text("❗ Неправильний формат. Приклад: AA1234BB")
        return ADD_ENTER_PLATE

    context.user_data["plate"] = plate

    kb = [[InlineKeyboardButton(v, callback_data=k)] for k, v in DOC_LABELS.items()]
    kb.append([InlineKeyboardButton("ІНШЕ", callback_data="CUSTOM")])

    await update.message.reply_text(
        "Оберіть документ:",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return ADD_SELECT_DOC


async def add_doc_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "CUSTOM":
        await q.edit_message_text("Введіть назву документа:")
        return ADD_ENTER_CUSTOM_DOC

    context.user_data["doc_name"] = DOC_LABELS[q.data]
    await q.edit_message_text("Введіть дату (ДД.ММ.РРРР):")
    return ADD_ENTER_DATE


async def add_custom_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["doc_name"] = norm(update.message.text)
    await update.message.reply_text("Введіть дату (ДД.ММ.РРРР):")
    return ADD_ENTER_DATE


async def add_doc_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    try:
        d = datetime.strptime(text, "%d.%m.%Y").date()
    except Exception:
        await update.message.reply_text("❗ Неправильний формат дати")
        return ADD_ENTER_DATE

    if d < date.today():
        await update.message.reply_text("❗ Дата не може бути в минулому")
        return ADD_ENTER_DATE

    uid = update.message.chat_id
    rows = sheet.get_all_records()
    user_rows = [r for r in rows if str(r["TELEGRAM"]) == str(uid)]

    if not user_rows:
        await update.message.reply_text(
            "❗ Ваш профіль у таблиці не знайдено.\n"
            "Натисніть /start і зареєструйтесь заново."
        )
        return ConversationHandler.END

    full = user_rows[0]["FULL_NAME"]

    sheet.append_row(
        [
            full,
            str(uid),
            context.user_data["vehicle_type"],
            context.user_data["plate"],
            context.user_data["doc_name"],
            text,
        ]
    )

    await update.message.reply_text("Документ додано ✔")
    await start(update, context)
    return ConversationHandler.END


# ============================================================
# MY VEHICLES
# ============================================================

async def my_vehicles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plates = get_user_plates(update.message.chat_id)

    if not plates:
        await update.message.reply_text("У вас немає транспорту.")
        return

    await update.message.reply_text("\n".join(f"• {p}" for p in plates))


# ============================================================
# MY DOCS
# ============================================================

async def my_docs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    docs = get_valid_docs(update.message.chat_id)

    if not docs:
        await update.message.reply_text("Документів немає.")
        return

    txt = "\n".join(
        f"{d['PLATE']} | {d['DOC_NAME']} — {d['DATE']}" for d in docs
    )
    await update.message.reply_text(txt)


# ============================================================
# UPDATE DOCUMENT
# ============================================================

async def update_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    docs = get_valid_docs(update.message.chat_id)

    if not docs:
        await update.message.reply_text("Документів немає.")
        return ConversationHandler.END

    kb = [
        [
            InlineKeyboardButton(
                f"{d['PLATE']} — {d['DOC_NAME']}",
                callback_data=f"{d['PLATE']}|{d['DOC_NAME']}",
            )
        ]
        for d in docs
    ]

    await update.message.reply_text(
        "Починаємо оновлення документа…",
        reply_markup=ReplyKeyboardRemove(),
    )

    await update.message.reply_text(
        "Оберіть документ:",
        reply_markup=InlineKeyboardMarkup(kb),
    )

    return UPDATE_SELECT_DOC


async def update_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    plate, doc = q.data.split("|")
    context.user_data["plate"] = plate
    context.user_data["doc"] = doc

    await q.edit_message_text("Введіть нову дату (ДД.ММ.РРРР):")
    return UPDATE_ENTER_DATE


async def update_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    try:
        d = datetime.strptime(text, "%d.%m.%Y").date()
    except Exception:
        await update.message.reply_text("❗ Неправильний формат")
        return UPDATE_ENTER_DATE

    if d < date.today():
        await update.message.reply_text("❗ Дата не може бути в минулому")
        return UPDATE_ENTER_DATE

    uid = update.message.chat_id
    rows = sheet.get_all_records()

    for i, r in enumerate(rows, start=2):
        if (
            str(r["TELEGRAM"]) == str(uid)
            and r["PLATE"] == context.user_data["plate"]
            and r["DOC_NAME"] == context.user_data["doc"]
        ):
            sheet.update_cell(i, 6, text)

    await update.message.reply_text("Оновлено ✔")
    await start(update, context)
    return ConversationHandler.END


# ============================================================
# DELETE DOCUMENT
# ============================================================

async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    docs = get_valid_docs(update.message.chat_id)

    if not docs:
        await update.message.reply_text("Немає документів.")
        return ConversationHandler.END

    kb = [
        [
            InlineKeyboardButton(
                f"{d['PLATE']} — {d['DOC_NAME']}",
                callback_data=f"{d['PLATE']}|{d['DOC_NAME']}",
            )
        ]
        for d in docs
    ]

    await update.message.reply_text(
        "Починаємо видалення документа…",
        reply_markup=ReplyKeyboardRemove(),
    )

    await update.message.reply_text(
        "Оберіть документ:",
        reply_markup=InlineKeyboardMarkup(kb),
    )

    return DELETE_SELECT_DOC


async def delete_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    plate, doc = q.data.split("|")
    uid = q.from_user.id

    rows = sheet.get_all_records()
    for i, r in enumerate(rows, start=2):
        if r["PLATE"] == plate and r["DOC_NAME"] == doc and str(r["TELEGRAM"]) == str(
            uid
        ):
            sheet.delete_rows(i)
            break

    await q.edit_message_text("Документ видалено ✔")

    await q.message.reply_text(
        "Головне меню:",
        reply_markup=ReplyKeyboardMarkup(
            [
                ["➕ ДОДАТИ ДОКУМЕНТ", "📄 МОЇ ДОКУМЕНТИ"],
                ["✏️ ОНОВИТИ ДОКУМЕНТ", "🗑 ВИДАЛИТИ ДОКУМЕНТ"],
            ],
            resize_keyboard=True,
        ),
    )

    return ConversationHandler.END


# ============================================================
# REMINDERS
# ============================================================

REMINDER_DAYS = {30, 25, 20, 14, 7, 3, 2, 1, 0}


async def reminders_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    print(f"[reminders_job] Fired at {now}")
    app = context.application

    hour = datetime.now().hour
    if not (11 <= hour < 21):
        return

    today = date.today()
    rows = sheet.get_all_records()

    for r in rows:
        if not r["DATE"]:
            continue

        try:
            d = datetime.strptime(r["DATE"], "%d.%m.%Y").date()
        except Exception:
            continue

        days = (d - today).days
        if days not in REMINDER_DAYS:
            continue

        uid = int(r["TELEGRAM"])

        if days < 0:
            msg_user = f"⛔ ПРОСТРОЧЕНО: {r['DOC_NAME']} ({r['PLATE']})"
        elif days == 0:
            msg_user = f"❗ СЬОГОДНІ закінчується {r['DOC_NAME']} ({r['PLATE']})"
        else:
            msg_user = f"⚠️ Через {days} днів закінчується {r['DOC_NAME']} ({r['PLATE']})"

        msg_admin = f"📣 {r['FULL_NAME']} → {msg_user}"

        if uid != ADMIN_ID:
            try:
                await app.bot.send_message(uid, msg_user)
            except Exception:
                pass

        try:
            await app.bot.send_message(ADMIN_ID, msg_admin)
        except Exception:
            pass


# ============================================================
# POST_INIT (WEBHOOK REMOVE + JOB QUEUE)
# ============================================================

async def post_init(app: Application):
    print("[post_init] Running…")

    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        print("[post_init] Webhook deleted")
    except Exception as e:
        print("[post_init] Webhook delete error:", e)

    try:
        app.job_queue.run_repeating(
            reminders_job,
            interval=3600,   # щогодини
            first=10,        # перший запуск через 10 секунд
        )
        print("[post_init] Job queue started")
    except Exception as e:
        print("[post_init] Job queue error:", e)

    try:
        app.create_task(
            app.bot.send_message(ADMIN_ID, "🔄 Бот перезавантажено і job_queue активний.")
        )
        print("[post_init] Admin notified")
    except Exception as e:
        print("[post_init] Notify admin error:", e)


# ============================================================
# MAIN
# ============================================================

def main():
    print("Building Application…")

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    print("App OK")

    # --- Registration (/start) ---
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("start", start)],
            states={
                REG_ENTER_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, register_save)
                ],
            },
            fallbacks=[CommandHandler("start", start)],
        )
    )

    # --- Add document ---
    app.add_handler(
        ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("➕ ДОДАТИ ДОКУМЕНТ"), add_doc_start)],
            states={
                ADD_SELECT_TYPE: [CallbackQueryHandler(add_doc_type)],
                ADD_ENTER_PLATE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_doc_plate)
                ],
                ADD_SELECT_DOC: [CallbackQueryHandler(add_doc_name)],
                ADD_ENTER_CUSTOM_DOC: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_custom_doc)
                ],
                ADD_ENTER_DATE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_doc_date)
                ],
            },
            fallbacks=[CommandHandler("start", start)],
        )
    )

    # --- Update document ---
    app.add_handler(
        ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("✏️ ОНОВИТИ ДОКУМЕНТ"), update_start)],
            states={
                UPDATE_SELECT_DOC: [CallbackQueryHandler(update_select)],
                UPDATE_ENTER_DATE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, update_save)
                ],
            },
            fallbacks=[CommandHandler("start", start)],
        )
    )

    # --- Delete document ---
    app.add_handler(
        ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("🗑 ВИДАЛИТИ ДОКУМЕНТ"), delete_start)],
            states={DELETE_SELECT_DOC: [CallbackQueryHandler(delete_process)]},
            fallbacks=[CommandHandler("start", start)],
        )
    )

    # --- Simple handlers ---
    app.add_handler(MessageHandler(filters.Regex("🚘 МОЇ ТРАНСПОРТИ"), my_vehicles))
    app.add_handler(MessageHandler(filters.Regex("📄 МОЇ ДОКУМЕНТИ"), my_docs))

    print("BOT RUNNING 🚀")

    try:
        asyncio.run(app.run_polling())
    except RuntimeError:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(app.run_polling())


if __name__ == "__main__":
    main()
