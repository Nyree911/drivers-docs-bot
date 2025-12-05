import asyncio
import logging
import re
from datetime import datetime, date
###
import os
import json

from telegram import (
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
    Update
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

import gspread
from oauth2client.service_account import ServiceAccountCredentials


# ========== CONFIG ========== #

TOKEN = "8428053990:AAF5GvsOr6JNgtZdqNyKOFDW1iBDZs3ygW4"
ADMIN_ID = 433247695

SPREAD_NAME = "Документи водіїв"
SHEET_NAME = "Drivers"

logging.basicConfig(level=logging.INFO)


# ========== GOOGLE SHEETS ========== #

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


creds_json = json.loads(os.getenv("CREDENTIALS_JSON"))
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_json, scope)
client = gspread.authorize(creds)
sheet = client.open(SPREAD_NAME).worksheet(SHEET_NAME)

REQUIRED_COLUMNS = ["FULL_NAME", "TELEGRAM", "TYPE", "PLATE", "DOC_NAME", "DATE"]
existing = sheet.row_values(1)
if existing != REQUIRED_COLUMNS:
    sheet.delete_rows(1)
    sheet.insert_row(REQUIRED_COLUMNS, 1)


# ========== STATES ========== #

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


# ========== HELPERS ========== #

def norm(text):
    return " ".join(text.upper().split())


def valid_plate(text):
    return re.fullmatch(r"[A-ZА-Я]{2}[0-9]{4}[A-ZА-Я]{2}", text.upper()) is not None


def user_exists(uid):
    return any(str(r["TELEGRAM"]) == str(uid) for r in sheet.get_all_records())


def get_user_docs(uid):
    return [
        r
        for r in sheet.get_all_records()
        if str(r["TELEGRAM"]) == str(uid) and r["DOC_NAME"]
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


# ========== START ========== #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.chat_id

    if not user_exists(uid):
        await update.message.reply_text(
            "Ви вперше користуєтесь ботом.\nБудь ласка, зареєструйтесь:",
            reply_markup=ReplyKeyboardMarkup(
                [["🔰 ЗАРЕЄСТРУВАТИСЯ"]], resize_keyboard=True
            ),
        )
        return

    await update.message.reply_text(
        "Головне меню:",
        reply_markup=ReplyKeyboardMarkup(
            [
                ["➕ ДОДАТИ ДОКУМЕНТ", "📄 МОЇ ДОКУМЕНТИ"],
                ["✏️ ОНОВИТИ ДОКУМЕНТ", "🗑 ВИДАЛИТИ ДОКУМЕНТ"],
            ],
            resize_keyboard=True,
        ),
    )


# ========== REGISTRATION ========== #

async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введіть ваше ІМ’Я ТА ПРІЗВИЩЕ:", reply_markup=ReplyKeyboardRemove()
    )
    return REG_ENTER_NAME


async def register_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    full = update.message.text.strip()

    if full.startswith("🔰") or len(full.split()) < 2:
        await update.message.reply_text("Введіть ім’я та прізвище ТЕКСТОМ 📝")
        return REG_ENTER_NAME

    uid = update.message.chat_id
    sheet.append_row([full, str(uid), "", "", "", ""])

    await update.message.reply_text("Реєстрацію завершено ✔")
    await start(update, context)
    return ConversationHandler.END


# ========== ADD DOCUMENT ========== #

async def add_doc_start(update, context):
    kb = [
        [InlineKeyboardButton("🚗 АВТО", callback_data="TYPE_AUTO")],
        [InlineKeyboardButton("🛞 ПРИЧІП", callback_data="TYPE_TRAILER")],
    ]
    await update.message.reply_text(
        "Оберіть тип транспорту:", reply_markup=InlineKeyboardMarkup(kb)
    )
    return ADD_SELECT_TYPE


async def add_doc_type(update, context):
    q = update.callback_query
    await q.answer()

    context.user_data["vehicle_type"] = q.data.replace("TYPE_", "")
    await q.edit_message_text("Введіть номер (AA1234BB):")
    return ADD_ENTER_PLATE


async def add_doc_plate(update, context):
    plate = update.message.text.upper().strip()

    if not valid_plate(plate):
        await update.message.reply_text("❗ Неправильний формат. AA1234BB")
        return ADD_ENTER_PLATE

    context.user_data["plate"] = plate

    kb = [
        [InlineKeyboardButton(v, callback_data=f"DOC_{k}")]
        for k, v in DOC_LABELS.items()
    ]
    kb.append([InlineKeyboardButton("ІНШЕ", callback_data="DOC_CUSTOM")])

    await update.message.reply_text(
        "Оберіть документ:", reply_markup=InlineKeyboardMarkup(kb)
    )
    return ADD_SELECT_DOC


async def add_doc_name(update, context):
    q = update.callback_query
    await q.answer()

    if q.data == "DOC_CUSTOM":
        await q.edit_message_text("Введіть назву документа:")
        return ADD_ENTER_CUSTOM_DOC

    code = q.data.replace("DOC_", "")
    context.user_data["doc_name"] = DOC_LABELS[code]
    await q.edit_message_text("Введіть дату (ДД.ММ.РРРР):")
    return ADD_ENTER_DATE


async def add_custom_doc(update, context):
    context.user_data["doc_name"] = norm(update.message.text)
    await update.message.reply_text("Дата завершення терміну дії документа (ДД.ММ.РРРР):")
    return ADD_ENTER_DATE


async def add_doc_date(update, context):
    text = update.message.text.strip()

    # Перевірка формату
    try:
        d = datetime.strptime(text, "%d.%m.%Y").date()
    except:
        await update.message.reply_text("❗ Неправильний формат. Введіть: ДД.ММ.РРРР")
        return ADD_ENTER_DATE

    # Перевірка що дата майбутня або сьогодні
    today = date.today()
    if d < today:
        await update.message.reply_text("❗ Дата не може бути в минулому. Введіть актуальну дату.")
        return ADD_ENTER_DATE

    uid = update.message.chat_id

    full = [
        r["FULL_NAME"]
        for r in sheet.get_all_records()
        if str(r["TELEGRAM"]) == str(uid)
    ][0]

    sheet.append_row([
        full,
        str(uid),
        context.user_data["vehicle_type"],
        context.user_data["plate"],
        context.user_data["doc_name"],
        text
    ])

    await update.message.reply_text("Документ додано ✔")
    return ConversationHandler.END


# ========== MY VEHICLES ========== #

async def my_vehicles(update, context):
    plates = get_user_plates(update.message.chat_id)
    if not plates:
        await update.message.reply_text("Немає транспортних засобів.")
        return

    await update.message.reply_text(
        "Ваш транспорт:\n" + "\n".join(f"• {p}" for p in plates)
    )


# ========== MY DOCS ========== #

async def my_docs(update, context):
    docs = get_user_docs(update.message.chat_id)
    if not docs:
        await update.message.reply_text("Документів немає.")
        return

    text = "Ваші документи:\n\n" + "\n".join(
        f"{d['TYPE']} | {d['PLATE']} | {d['DOC_NAME']} — {d['DATE']}" for d in docs
    )

    await update.message.reply_text(text)


# ========== UPDATE DOC ========== #

async def update_start(update, context):
    docs = get_user_docs(update.message.chat_id)
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
        "Оберіть документ:", reply_markup=InlineKeyboardMarkup(kb)
    )
    return UPDATE_SELECT_DOC


async def update_select(update, context):
    q = update.callback_query
    await q.answer()

    plate, doc = q.data.split("|")
    context.user_data["plate"] = plate
    context.user_data["doc"] = doc

    await q.edit_message_text("Введіть нову дату (ДД.ММ.РРРР):")
    return UPDATE_ENTER_DATE


async def update_save(update, context):
    new_date = update.message.text.strip()

    # Перевірка формату
    try:
        d = datetime.strptime(new_date, "%d.%m.%Y").date()
    except:
        await update.message.reply_text("❗ Неправильний формат. Введіть ДД.ММ.РРРР")
        return UPDATE_ENTER_DATE

    # Перевірка: дата не може бути в минулому
    today = date.today()
    if d < today:
        await update.message.reply_text("❗ Дата не може бути в минулому. Введіть актуальну дату.")
        return UPDATE_ENTER_DATE

    rows = sheet.get_all_records()
    uid = update.message.chat_id

    for i, r in enumerate(rows, start=2):
        if (
            str(r["TELEGRAM"]) == str(uid)
            and r["PLATE"] == context.user_data["plate"]
            and r["DOC_NAME"] == context.user_data["doc"]
        ):
            sheet.update_cell(i, 6, new_date)

    await update.message.reply_text("Оновлено ✔")
    return ConversationHandler.END


# ========== DELETE DOC ========== #

async def delete_start(update, context):
    docs = get_user_docs(update.message.chat_id)
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
        "Оберіть документ:", reply_markup=InlineKeyboardMarkup(kb)
    )
    return DELETE_SELECT_DOC

async def expired_docs(update, context):
    uid = update.message.chat_id
    rows = sheet.get_all_records()

    expired = []

    today = datetime.now().date()

    for r in rows:
        if str(r["TELEGRAM"]) != str(uid):
            continue

        if not r["DATE"]:
            continue

        try:
            d = datetime.strptime(r["DATE"], "%d.%m.%Y").date()
        except:
            continue

        if d < today:
            expired.append(
                f"⛔ {r['DOC_NAME']} ({r['PLATE']}) — закінчився {r['DATE']}"
            )

    if not expired:
        await update.message.reply_text("У вас немає прострочених документів ✔")
        return

    text = "Ваші прострочені документи:\n\n" + "\n".join(expired)
    await update.message.reply_text(text)


async def delete_process(update, context):
    q = update.callback_query
    await q.answer()

    plate, doc = q.data.split("|")
    uid = q.from_user.id

    rows = sheet.get_all_records()

    for i, r in enumerate(rows, start=2):
        if (
            r["PLATE"] == plate
            and r["DOC_NAME"] == doc
            and str(r["TELEGRAM"]) == str(uid)
        ):
            sheet.delete_rows(i)
            break

    await q.edit_message_text("Документ видалено ✔")
    return ConversationHandler.END


# ========== REMINDERS ========== #

REMINDER_DAYS = {30, 25, 20, 14, 7, 3, 2, 1, 0}


async def reminders(app: Application):
    while True:
        now = datetime.now()
        hour = now.hour

        # Надсилати повідомлення лише між 11:00 та 21:00
        if 11 <= hour < 21:

            data = sheet.get_all_records()
            today = date.today()

            for r in data:

                if not r["DOC_NAME"]:
                    continue

                try:
                    d = datetime.strptime(r["DATE"], "%d.%m.%Y").date()
                except:
                    continue

                days = (d - today).days

                if days not in REMINDER_DAYS:
                    continue

                uid = int(r["TELEGRAM"])

                # Формуємо текст
                if days < 0:
                    msg_user = f"⛔ ПРОСТРОЧЕНО: {r['DOC_NAME']} ({r['PLATE']})"
                elif days == 0:
                    msg_user = f"❗ СЬОГОДНІ закінчується {r['DOC_NAME']} ({r['PLATE']})"
                else:
                    msg_user = f"⚠️ Через {days} днів закінчується {r['DOC_NAME']} ({r['PLATE']})"

                msg_admin = f"📣 {r['FULL_NAME']} → {msg_user}"

                # Водію
                if uid != ADMIN_ID:
                    try:
                        await app.bot.send_message(uid, msg_user)
                    except:
                        pass

                # Адміну
                try:
                    await app.bot.send_message(ADMIN_ID, msg_admin)
                except:
                    pass

        # Чекаємо 1 годину між перевірками
        await asyncio.sleep(3600)


# ========== POST_INIT (ВАЖЛИВО!) ========== #

async def post_init(app: Application):
    app.create_task(reminders(app))


# ========== RUN ========== #

# ---------- RUN CLEAN VERSION ---------- #

from telegram.ext import ApplicationBuilder

async def post_init(app):
    # Запускаємо асинхронний фоновий таск з нагадуваннями
    app.create_task(reminders(app))


# ---------- RUN CLEAN ---------- #

# ---------- RUN ---------- #

async def post_init(app):
    # запуститься ПІСЛЯ старту event loop — тут помилок більше нема
    app.create_task(reminders(app))


def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    # ----- РЕЄСТРАЦІЯ -----
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("🔰 ЗАРЕЄСТРУВАТИСЯ"), register_start)],
        states={
            REG_ENTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_save)]
        },
        fallbacks=[]
    ))

    # ----- ДОДАВАННЯ ДОКУМЕНТА -----
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("➕ ДОДАТИ ДОКУМЕНТ"), add_doc_start)],
        states={
            ADD_SELECT_TYPE: [CallbackQueryHandler(add_doc_type)],
            ADD_ENTER_PLATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_doc_plate)],
            ADD_SELECT_DOC: [CallbackQueryHandler(add_doc_name)],
            ADD_ENTER_CUSTOM_DOC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_custom_doc)],
            ADD_ENTER_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_doc_date)],
        },
        fallbacks=[]
    ))

    # ----- ОНОВЛЕННЯ ДОКУМЕНТА -----
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("✏️ ОНОВИТИ ДОКУМЕНТ"), update_start)],
        states={
            UPDATE_SELECT_DOC: [CallbackQueryHandler(update_select)],
            UPDATE_ENTER_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_save)],
        },
        fallbacks=[]
    ))

    # ----- ВИДАЛЕННЯ ДОКУМЕНТА -----
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("🗑 ВИДАЛИТИ ДОКУМЕНТ"), delete_start)],
        states={
            DELETE_SELECT_DOC: [CallbackQueryHandler(delete_process)],
        },
        fallbacks=[]
    ))

    # ----- ІНШІ КОМАНДИ -----
    app.add_handler(MessageHandler(filters.Regex("🚘 МОЇ ТРАНСПОРТИ"), my_vehicles))
    
    app.add_handler(MessageHandler(filters.Regex("📄 МОЇ ДОКУМЕНТИ"), my_docs))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("expired", expired_docs))

    print("BOT RUNNING 🚀")
    app.run_polling()


if __name__ == "__main__":
    main()
