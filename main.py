import logging
from telegram import (
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove
)
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    Filters
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import schedule
import threading
import time
import re

# ========== CONFIG ========== #

TOKEN = "8428053990:AAF5GvsOr6JNgtZdqNyKOFDW1iBDZs3ygW4"
ADMIN_ID = 433247695

SPREAD_NAME = "Документи водіїв"
SHEET_NAME = "Drivers"

# ========== GOOGLE SHEETS ========== #

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
sheet = client.open(SPREAD_NAME).worksheet(SHEET_NAME)

# ========== KEYBOARDS ========== #

main_keyboard = ReplyKeyboardMarkup(
    [
        ["🚘 МОЇ ТРАНСПОРТИ"],
        ["➕ ДОДАТИ ДОКУМЕНТ", "📄 МОЇ ДОКУМЕНТИ"],
        ["✏️ ОНОВИТИ ДОКУМЕНТ", "🗑 ВИДАЛИТИ ДОКУМЕНТ"]
    ],
    resize_keyboard=True
)

register_keyboard = ReplyKeyboardMarkup(
    [["🔰 ЗАРЕЄСТРУВАТИСЯ"]],
    resize_keyboard=True
)

DOC_LABELS = {
    "TP": "ТЕХ ПАСПОРТ",
    "BC": "БІЛИЙ СЕРТИФІКАТ",
    "TO": "ТЕХ ОГЛЯД",
    "TACO": "КАЛІБРОВКА ТАХО",
    "INS": "СТРАХОВИЙ ПОЛІС",
    "GREEN": "ЗЕЛЕНА КАРТА",
}

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
    DELETE_SELECT_DOC
) = range(9)

# ========== HELPERS ========== #

def norm(text):
    return " ".join(text.upper().split())

def user_exists(user_id):
    rows = sheet.get_all_records()
    return any(str(r["TELEGRAM"]) == str(user_id) for r in rows)

def get_user_docs(user_id):
    return [
        r for r in sheet.get_all_records()
        if str(r["TELEGRAM"]) == str(user_id) and r["DOC_NAME"]
    ]

def get_user_plates(user_id):
    plates = set()
    for r in sheet.get_all_records():
        if str(r["TELEGRAM"]) == str(user_id):
            if r["PLATE"]:
                plates.add(r["PLATE"])
    return sorted(list(plates))

def valid_plate(text):
    return re.fullmatch(r"[A-ZА-Я]{2}[0-9]{4}[A-ZА-Я]{2}", text.upper()) is not None


# ========== START ========== #

def start(update, context):
    user_id = update.message.chat_id
    if not user_exists(user_id):
        update.message.reply_text(
            "Ви вперше користуєтеся ботом.\nБудь ласка, зареєструйтесь:",
            reply_markup=register_keyboard
        )
        return
    update.message.reply_text("Головне меню:", reply_markup=main_keyboard)


# ========== REGISTRATION ========== #

def register_start(update, context):
    update.message.reply_text(
        "Введіть ваше ІМ’Я ТА ПРІЗВИЩЕ:",
        reply_markup=ReplyKeyboardRemove()   # ← повністю ховає кнопку
    )
    return REG_ENTER_NAME

def register_save(update, context):
    full = update.message.text.strip()

    if full.startswith("🔰") or len(full.split()) < 2:
        update.message.reply_text("Будь ласка напишіть ім’я та прізвище ТЕКСТОМ 📝")
        return REG_ENTER_NAME

    uid = update.message.chat_id
    sheet.append_row([full, str(uid), "", "", "", ""])

    # NEW FIX ↓↓↓↓↓↓↓↓↓↓↓↓↓↓↓↓↓↓↓↓↓
    update.message.reply_text("Реєстрацію завершено ✔")
    update.message.reply_text("Головне меню:", reply_markup=main_keyboard)
    # NEW FIX ↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑

    return ConversationHandler.END


# ========== ADD DOCUMENT ========== #

def add_doc_start(update, context):
    keyboard = [
        [InlineKeyboardButton("🚗 АВТО", callback_data="TYPE_AUTO")],
        [InlineKeyboardButton("🛞 ПРИЧІП", callback_data="TYPE_TRAILER")]
    ]
    update.message.reply_text("Оберіть тип транспорту:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADD_SELECT_TYPE

def add_doc_type(update, context):
    q = update.callback_query
    q.answer()

    context.user_data["vehicle_type"] = q.data.replace("TYPE_", "")
    q.edit_message_text("Введіть номер (формат AA1234BB):")
    return ADD_ENTER_PLATE

def add_doc_plate(update, context):
    plate = update.message.text.upper().strip()

    if not valid_plate(plate):
        update.message.reply_text("❗ Формат неправильний. Приклад: **AA1234BB**")
        return ADD_ENTER_PLATE

    context.user_data["plate"] = plate

    keyboard = [
        [InlineKeyboardButton("ТЕХ ПАСПОРТ", callback_data="DOC_TP")],
        [InlineKeyboardButton("БІЛИЙ СЕРТИФІКАТ", callback_data="DOC_BC")],
        [InlineKeyboardButton("ТЕХ ОГЛЯД", callback_data="DOC_TO")],
        [InlineKeyboardButton("КАЛІБРОВКА ТАХО", callback_data="DOC_TACO")],
        [InlineKeyboardButton("СТРАХОВИЙ ПОЛІС", callback_data="DOC_INS")],
        [InlineKeyboardButton("ЗЕЛЕНА КАРТА", callback_data="DOC_GREEN")],
        [InlineKeyboardButton("ІНШЕ", callback_data="DOC_CUSTOM")],
    ]

    update.message.reply_text("Оберіть документ:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADD_SELECT_DOC

def add_doc_name(update, context):
    q = update.callback_query
    q.answer()

    if q.data == "DOC_CUSTOM":
        q.edit_message_text("Введіть НАЗВУ документа:")
        return ADD_ENTER_CUSTOM_DOC

    code = q.data.replace("DOC_", "")  # TP, BC, TO...
    context.user_data["doc_name"] = DOC_LABELS.get(code, code)
    q.edit_message_text("Введіть дату (ДД.ММ.РРРР):")
    return ADD_ENTER_DATE

def add_custom_doc(update, context):
    name = norm(update.message.text)
    context.user_data["doc_name"] = name
    update.message.reply_text("Дата документа (ДД.ММ.РРРР):")
    return ADD_ENTER_DATE

def add_doc_date(update, context):
    date_text = update.message.text.strip()
    try:
        datetime.strptime(date_text, "%d.%m.%Y")
    except:
        update.message.reply_text("Невірний формат дати. Приклад: 12.05.2025")
        return ADD_ENTER_DATE

    uid = update.message.chat_id
    full = [r["FULL_NAME"] for r in sheet.get_all_records() if str(r["TELEGRAM"]) == str(uid)][0]

    sheet.append_row([
        full,
        str(uid),
        context.user_data["vehicle_type"],
        context.user_data["plate"],
        context.user_data["doc_name"],
        date_text
    ])

    update.message.reply_text("Документ додано ✔", reply_markup=main_keyboard)
    return ConversationHandler.END


# ========== MY VEHICLES ========== #

def my_vehicles(update, context):
    uid = update.message.chat_id
    plates = get_user_plates(uid)

    if not plates:
        update.message.reply_text("У вас ще немає зареєстрованих номерів.")
        return

    text = "Ваші транспортні засоби:\n\n"
    for p in plates:
        text += f"• {p}\n"
    update.message.reply_text(text)


# ========== MY DOCS ========== #

def my_docs(update, context):
    docs = get_user_docs(update.message.chat_id)

    if not docs:
        update.message.reply_text("Документів немає.")
        return

    text = "Ваші документи:\n\n"
    for r in docs:
        text += f"{r['TYPE']} | {r['PLATE']} | {r['DOC_NAME']} — {r['DATE']}\n"

    update.message.reply_text(text)


# ========== UPDATE DOC ========== #

def update_start(update, context):
    docs = get_user_docs(update.message.chat_id)
    if not docs:
        update.message.reply_text("Документів немає.")
        return ConversationHandler.END

    kb = [[InlineKeyboardButton(f"{d['PLATE']} — {d['DOC_NAME']}", callback_data=d['PLATE'] + "|" + d['DOC_NAME'])] for d in docs]

    update.message.reply_text("Оберіть документ:", reply_markup=InlineKeyboardMarkup(kb))
    return UPDATE_SELECT_DOC

def update_select(update, context):
    q = update.callback_query
    q.answer()

    plate, doc = q.data.split("|")
    context.user_data["plate"] = plate
    context.user_data["doc"] = doc

    q.edit_message_text("Введіть нову дату (ДД.ММ.РРРР):")
    return UPDATE_ENTER_DATE

def update_save(update, context):
    new_date = update.message.text.strip()
    try:
        datetime.strptime(new_date, "%d.%m.%Y")
    except:
        update.message.reply_text("Невірна дата. Спробуйте ще раз.")
        return UPDATE_ENTER_DATE

    uid = update.message.chat_id

    rows = sheet.get_all_records()
    for i, r in enumerate(rows, start=2):
        if (
            str(r["TELEGRAM"]) == str(uid)
            and r["PLATE"] == context.user_data["plate"]
            and r["DOC_NAME"] == context.user_data["doc"]
        ):
            sheet.update_cell(i, 6, new_date)

    update.message.reply_text("Оновлено ✔", reply_markup=main_keyboard)
    return ConversationHandler.END


# ========== DELETE DOC ========== #

def delete_start(update, context):
    docs = get_user_docs(update.message.chat_id)
    if not docs:
        update.message.reply_text("Документів немає.")
        return ConversationHandler.END

    kb = [
        [InlineKeyboardButton(
            f"{d['PLATE']} — {d['DOC_NAME']}",
            callback_data=d['PLATE'] + "|" + d['DOC_NAME']
        )]
        for d in docs
    ]

    update.message.reply_text("Оберіть документ:", reply_markup=InlineKeyboardMarkup(kb))
    return DELETE_SELECT_DOC

def delete_process(update, context):
    q = update.callback_query
    q.answer()
    plate, doc = q.data.split("|")
    uid = q.message.chat_id

    rows = sheet.get_all_records()

    for i, r in enumerate(rows, start=2):
        if (
            str(r["TELEGRAM"]) == str(uid)
            and r["PLATE"] == plate
            and r["DOC_NAME"] == doc
        ):
            sheet.delete_rows(i)
            break

    q.edit_message_text("Документ видалено ✔")
    return ConversationHandler.END


# ========== REMINDERS ========== #

REMINDER_DAYS = {30, 25, 20, 14, 7, 3, 2, 1, 0}

def check_documents():
    rows = sheet.get_all_records()

    for r in rows:
        if not r["DOC_NAME"]:
            continue

        try:
            exp = datetime.strptime(r["DATE"], "%d.%m.%Y").date()
        except:
            continue

        days = (exp - datetime.now().date()).days
        uid = str(r["TELEGRAM"])
        name = r["FULL_NAME"]
        plate = r["PLATE"]
        doc = r["DOC_NAME"]

        # стандартне повідомлення водію
        if days == 30:
            user_msg = f"⚠️ Через 30 днів закінчується {doc} ({plate})"
        elif days == 25:
            user_msg = f"⚠️ Через 25 днів закінчується {doc} ({plate})"
        elif days == 20:
            user_msg = f"⚠️ Через 20 днів закінчується {doc} ({plate})"
        elif days == 14:
            user_msg = f"⚠️ Через 14 днів закінчується {doc} ({plate})"
        elif days == 7:
            user_msg = f"⚠️ Через 7 днів закінчується {doc} ({plate})"
        elif days == 3:
            user_msg = f"⚠️ Через 3 дні закінчується {doc} ({plate})"
        elif days == 2:
            user_msg = f"⚠️ Через 2 дні закінчується {doc} ({plate})"
        elif days == 1:
            user_msg = f"⚠️ Через 1 день закінчується {doc} ({plate})"
        elif days == 0:
            user_msg = f"❗ СЬОГОДНІ закінчується {doc} ({plate})"
        elif days < 0:
            user_msg = f"⛔ ПРОСТРОЧЕНО: {doc} ({plate})"
        else:
            continue

        # повідомлення адміну
        admin_msg = f"📣 {name} → {user_msg}"

        # 1️⃣ Водію надсилаємо тільки його повідомлення
        if uid != str(ADMIN_ID):
            try:
                updater.bot.send_message(uid, user_msg)
            except:
                pass

        # 2️⃣ Адміну надсилаємо ТІЛЬКИ адмінське повідомлення
        try:
            updater.bot.send_message(ADMIN_ID, admin_msg)
        except:
            pass

def scheduler_loop():
    while True:
        schedule.run_pending()
        time.sleep(5)

schedule.every(1).minutes.do(check_documents)


# ========== RUN ========== #

updater = Updater(TOKEN, use_context=True)
dp = updater.dispatcher

# Registration
dp.add_handler(ConversationHandler(
    entry_points=[MessageHandler(Filters.regex("🔰 ЗАРЕЄСТРУВАТИСЯ"), register_start)],
    states={REG_ENTER_NAME: [MessageHandler(Filters.text, register_save)]},
    fallbacks=[]
))

# Add document
dp.add_handler(ConversationHandler(
    entry_points=[MessageHandler(Filters.regex("➕ ДОДАТИ ДОКУМЕНТ"), add_doc_start)],
    states={
        ADD_SELECT_TYPE: [CallbackQueryHandler(add_doc_type)],
        ADD_ENTER_PLATE: [MessageHandler(Filters.text, add_doc_plate)],
        ADD_SELECT_DOC: [CallbackQueryHandler(add_doc_name)],
        ADD_ENTER_CUSTOM_DOC: [MessageHandler(Filters.text, add_custom_doc)],
        ADD_ENTER_DATE: [MessageHandler(Filters.text, add_doc_date)],
    },
    fallbacks=[]
))

# Update doc
dp.add_handler(ConversationHandler(
    entry_points=[MessageHandler(Filters.regex("✏️ ОНОВИТИ ДОКУМЕНТ"), update_start)],
    states={
        UPDATE_SELECT_DOC: [CallbackQueryHandler(update_select)],
        UPDATE_ENTER_DATE: [MessageHandler(Filters.text, update_save)],
    },
    fallbacks=[]
))

# Delete doc
dp.add_handler(ConversationHandler(
    entry_points=[MessageHandler(Filters.regex("🗑 ВИДАЛИТИ ДОКУМЕНТ"), delete_start)],
    states={
        DELETE_SELECT_DOC: [CallbackQueryHandler(delete_process)],
    },
    fallbacks=[]
))

dp.add_handler(MessageHandler(Filters.regex("📄 МОЇ ДОКУМЕНТИ"), my_docs))
dp.add_handler(MessageHandler(Filters.regex("🚘 МОЇ ТРАНСПОРТИ"), my_vehicles))
dp.add_handler(CommandHandler("start", start))

threading.Thread(target=scheduler_loop, daemon=True).start()

print("BOT RUNNING 🚀")
updater.start_polling()
updater.idle()
