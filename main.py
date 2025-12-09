import asyncio
import logging
import os
import json
import re
from datetime import datetime, date

# Telegram #
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

TOKEN = "8428053990:AAF5GvsOr6JNgtZdqNyKOFDW1iBDZs3ygW4"      
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
        r
        for r in sheet.get_all_records()
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


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["➕ ДОДАТИ ДОКУМЕНТ", "📄 МОЇ ДОКУМЕНТИ"],
            ["✏️ ОНОВИТИ ДОКУМЕНТ", "🗑 ВИДАЛИТИ ДОКУМЕНТ"],
        ],
        resize_keyboard=True,
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
# CANCEL (для всіх сценаріїв)
# ============================================================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скасування будь-якого сценарію і повернення в меню."""
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        msg = q.message
    else:
        msg = update.message

    await msg.reply_text(
        "Дію скасовано. Повертаюсь у головне меню.",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


# ============================================================
# START / REGISTRATION
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Якщо користувач новий — просимо імʼя, інакше показуємо меню."""
    chat_id = update.effective_chat.id
    message = update.effective_message

    if not user_exists(chat_id):
        await message.reply_text(
            "Ви вперше користуєтесь ботом.\n"
            "Будь ласка, введіть ваше ІМ’Я ТА ПРІЗВИЩЕ:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return REG_ENTER_NAME

    await message.reply_text("Головне меню:", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


async def register_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    full = update.message.text.strip()

    if len(full.split()) < 2:
        await update.message.reply_text("Введіть ім’я та прізвище повністю 📝")
        return REG_ENTER_NAME

    uid = update.message.chat_id

    rows = sheet.get_all_records()
    existing = [r for r in rows if str(r["TELEGRAM"]) == str(uid)]
    if not existing:
        sheet.append_row([full, str(uid), "", "", "", ""])
    else:
        for i, r in enumerate(rows, start=2):
            if str(r["TELEGRAM"]) == str(uid):
                sheet.update_cell(i, 1, full)
                break

    await update.message.reply_text("Реєстрацію завершено ✔")
    await update.message.reply_text("Головне меню:", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


# ============================================================
# ADD DOCUMENT
# ============================================================

async def add_doc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("🚗 АВТО", callback_data="AUTO")],
        [InlineKeyboardButton("🛞 ПРИЧІП", callback_data="TRAILER")],
        [InlineKeyboardButton("❌ СКАСУВАТИ", callback_data="CANCEL")],
    ]

    await update.message.reply_text(
        "Починаємо додавання документа…", reply_markup=ReplyKeyboardRemove()
    )

    await update.message.reply_text(
        "Оберіть тип транспорту:", reply_markup=InlineKeyboardMarkup(kb)
    )

    return ADD_SELECT_TYPE


async def add_doc_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "CANCEL":
        return await cancel(update, context)

    context.user_data["vehicle_type"] = q.data
    await q.edit_message_text(
        "Введіть номер (AA1234BB) або натисніть 🔙 СКАСУВАТИ:"
    )
    return ADD_ENTER_PLATE


async def add_doc_plate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plate = update.message.text.upper().strip()

    if plate == "🔙 СКАСУВАТИ":
        return await cancel(update, context)

    if not valid_plate(plate):
        await update.message.reply_text(
            "❗ Неправильний формат. Приклад: AA1234BB",
            reply_markup=ReplyKeyboardMarkup(
                [["🔙 СКАСУВАТИ"]], resize_keyboard=True
            ),
        )
        return ADD_ENTER_PLATE

    context.user_data["plate"] = plate

    kb = [[InlineKeyboardButton(v, callback_data=k)] for k, v in DOC_LABELS.items()]
    kb.append([InlineKeyboardButton("ІНШЕ", callback_data="CUSTOM")])
    kb.append([InlineKeyboardButton("❌ СКАСУВАТИ", callback_data="CANCEL")])

    await update.message.reply_text(
        "Оберіть документ:", reply_markup=InlineKeyboardMarkup(kb)
    )
    return ADD_SELECT_DOC


async def add_doc_name(update, context):
    q = update.callback_query
    await q.answer()

    if q.data == "CANCEL":
        return await cancel(update, context)

    # Якщо інше — просимо назву
    if q.data == "CUSTOM":
        await q.edit_message_text(
            "Введіть назву документа або натисніть 🔙 СКАСУВАТИ:"
        )
        return ADD_ENTER_CUSTOM_DOC

    # Якщо обрана стандартна назва
    context.user_data["doc_name"] = DOC_LABELS[q.data]

    await q.edit_message_text(
        "Введіть дату (ДД.ММ.РРРР) або натисніть 🔙 СКАСУВАТИ:"
    )
    return ADD_ENTER_DATE

async def add_custom_doc(update, context):
    text = update.message.text.strip()

    if text == "🔙 СКАСУВАТИ":
        return await cancel(update, context)

    context.user_data["doc_name"] = norm(text)

    await update.message.reply_text(
        "Введіть дату завершення терміну дії (ДД.ММ.РРРР) або натисніть 🔙 СКАСУВАТИ:",
        reply_markup=ReplyKeyboardMarkup([["🔙 СКАСУВАТИ"]], resize_keyboard=True)
    )

    return ADD_ENTER_DATE


async def add_doc_date(update, context):
    text = update.message.text.strip()

    if text == "🔙 СКАСУВАТИ":
        return await cancel(update, context)

    try:
        d = datetime.strptime(text, "%d.%m.%Y").date()
    except:
        await update.message.reply_text(
            "❗ Неправильний формат дати. Введіть ще раз або натисніть 🔙 СКАСУВАТИ:",
            reply_markup=ReplyKeyboardMarkup([["🔙 СКАСУВАТИ"]], resize_keyboard=True)
        )
        return ADD_ENTER_DATE

    if d < date.today():
        await update.message.reply_text(
            "❗ Дата не може бути в минулому. Введіть ще раз або натисніть 🔙 СКАСУВАТИ:",
            reply_markup=ReplyKeyboardMarkup([["🔙 СКАСУВАТИ"]], resize_keyboard=True)
        )
        return ADD_ENTER_DATE

    # додаємо у таблицю
    uid = update.message.chat_id
    rows = sheet.get_all_records()
    user_rows = [r for r in rows if str(r["TELEGRAM"]) == str(uid)]

    if not user_rows:
        await update.message.reply_text(
            "❗ Вас не знайдено у таблиці. Натисніть /start.",
            reply_markup=main_menu_keyboard()
        )
        return ConversationHandler.END

    full_name = user_rows[0]["FULL_NAME"]

    sheet.append_row([
        full_name,
        str(uid),
        context.user_data["vehicle_type"],
        context.user_data["plate"],
        context.user_data["doc_name"],
        text
    ])

    await update.message.reply_text(
        "Документ додано ✔",
        reply_markup=main_menu_keyboard()
    )

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

async def my_docs(update, context):
    uid = update.message.chat_id
    docs = get_user_docs(uid)

    if not docs:
        await update.message.reply_text("Документів немає.")
        return

    today = date.today()
    processed = []

    # Обробляємо кожен документ
    for d in docs:
        try:
            exp = datetime.strptime(d["DATE"], "%d.%m.%Y").date()
        except:
            continue

        days_left = (exp - today).days

        # Формуємо статус
        if days_left < 0:
            status = f"(прострочено {abs(days_left)} дн.)"
        elif days_left == 0:
            status = "(сьогодні)"
        else:
            status = f"(залишилось {days_left} дн.)"

        processed.append({
            "plate": d["PLATE"],
            "name": d["DOC_NAME"],
            "date": d["DATE"],
            "days": days_left,
            "status": status
        })

    # Сортуємо від найменшого days_left (найближча дата)
    processed.sort(key=lambda x: x["days"])

    # Формуємо текст
    lines = []
    for d in processed:
        lines.append(
             f"{d['plate']} | {d['name']} — {d['date']} {d['status']}"
    )
    lines = []
    for d in processed:
       lines.append(
          f"{d['plate']} | {d['name']} — {d['date']} {d['status']}"
          )
       lines.append("")  # порожній рядок між документами

await update.message.reply_text("\n".join(lines).strip())lines.append("")  # порожній рядок між документами

await update.message.reply_text("\n".join(lines).strip())
# ============================================================
# UPDATE DOCUMENT
# ============================================================

# ============================================================
# UPDATE DOCUMENT
# ============================================================

async def update_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Старт сценарію оновлення документа."""
    uid = update.message.chat_id
    docs = get_valid_docs(uid)

    if not docs:
        await update.message.reply_text("Документів немає.")
        return ConversationHandler.END

    # Кнопки з усіма документами + скасування
    kb = [
        [
            InlineKeyboardButton(
                f"{d['PLATE']} — {d['DOC_NAME']}",
                callback_data=f"{d['PLATE']}|{d['DOC_NAME']}",
            )
        ]
        for d in docs
    ]
    kb.append([InlineKeyboardButton("❌ СКАСУВАТИ", callback_data="CANCEL")])

    # Одне повідомлення: і «починаємо», і «оберіть»
    await update.message.reply_text(
        "Починаємо оновлення документа…\n\nОберіть документ:",
        reply_markup=InlineKeyboardMarkup(kb),
    )

    return UPDATE_SELECT_DOC


async def update_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Користувач обрав документ, тепер просимо нову дату."""
    q = update.callback_query
    await q.answer()

    # Обробка інлайн-скасування
    if q.data == "CANCEL":
        return await cancel(update, context)

    # Розбираємо plate | doc_name
    plate, doc = q.data.split("|", maxsplit=1)
    context.user_data["plate"] = plate
    context.user_data["doc"] = doc

    # Редагуємо старе повідомлення, щоби показати, що обрано
    await q.edit_message_text(f"Обрано: {plate} — {doc}")

    # І ОКРЕМО нове повідомлення з полем введення + клавіатурою «скасувати»
    await q.message.reply_text(
        "Введіть нову дату (ДД.ММ.РРРР) або натисніть 🔙 СКАСУВАТИ:",
        reply_markup=ReplyKeyboardMarkup(
            [["🔙 СКАСУВАТИ"]],
            resize_keyboard=True,
        ),
    )

    return UPDATE_ENTER_DATE


async def update_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Зберігаємо нову дату документа."""
    text = update.message.text.strip()

    # Натиснули кнопку скасування замість дати
    if text == "🔙 СКАСУВАТИ":
        return await cancel(update, context)

    # Перевіряємо формат дати
    try:
        d = datetime.strptime(text, "%d.%m.%Y").date()
    except Exception:
        await update.message.reply_text(
            "❗ Неправильний формат дати. Спробуйте ще раз.",
            reply_markup=ReplyKeyboardMarkup(
                [["🔙 СКАСУВАТИ"]], resize_keyboard=True
            ),
        )
        return UPDATE_ENTER_DATE

    if d < date.today():
        await update.message.reply_text(
            "❗ Дата не може бути в минулому.",
            reply_markup=ReplyKeyboardMarkup(
                [["🔙 СКАСУВАТИ"]], resize_keyboard=True
            ),
        )
        return UPDATE_ENTER_DATE

    # Оновлюємо рядок у таблиці
    uid = update.message.chat_id
    rows = sheet.get_all_records()

    for i, r in enumerate(rows, start=2):
        if (
            str(r["TELEGRAM"]) == str(uid)
            and r["PLATE"] == context.user_data.get("plate")
            and r["DOC_NAME"] == context.user_data.get("doc")
        ):
            sheet.update_cell(i, 6, text)

    # Повертаємося в головне меню
    await update.message.reply_text(
        "Оновлено ✔",
        reply_markup=main_menu_keyboard(),
    )
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
    kb.append([InlineKeyboardButton("❌ СКАСУВАТИ", callback_data="CANCEL")])

    await update.message.reply_text(
        "Починаємо видалення документа…", reply_markup=ReplyKeyboardRemove()
    )

    await update.message.reply_text(
        "Оберіть документ:", reply_markup=InlineKeyboardMarkup(kb)
    )

    return DELETE_SELECT_DOC


async def delete_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "CANCEL":
        return await cancel(update, context)

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

    await q.message.reply_text(
        "Головне меню:", reply_markup=main_menu_keyboard()
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
    if not (9 <= hour < 18):
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
            msg_user = (
                f"⚠️ Через {days} днів закінчується {r['DOC_NAME']} ({r['PLATE']})"
            )

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
            interval=3600,  # щогодини
            first=10,       # перший запуск через 10 секунд
        )
        print("[post_init] Job queue started")
    except Exception as e:
        print("[post_init] Job queue error:", e)

    try:
        app.create_task(
            app.bot.send_message(
                ADMIN_ID, "🔄 Бот перезавантажено і job_queue активний."
            )
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

    # Глушимо службові оновлення (join/left, pinned і т.д.)
    app.add_handler(MessageHandler(filters.StatusUpdate.ALL, lambda u, c: None))

    # --- Registration (/start) ---
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("start", start)],
            states={
                REG_ENTER_NAME: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND, register_save
                    ),
                ],
            },
            fallbacks=[CommandHandler("start", start)],
        )
    )

    # --- Add document ---
    app.add_handler(
        ConversationHandler(
            entry_points=[
                MessageHandler(filters.Regex("➕ ДОДАТИ ДОКУМЕНТ"), add_doc_start)
            ],
            states={
                ADD_SELECT_TYPE: [CallbackQueryHandler(add_doc_type)],
                ADD_ENTER_PLATE: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND, add_doc_plate
                    ),
                ],
                ADD_SELECT_DOC: [CallbackQueryHandler(add_doc_name)],
                ADD_ENTER_CUSTOM_DOC: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND, add_custom_doc
                    )
                ],
                ADD_ENTER_DATE: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND, add_doc_date
                    )
                ],
            },
            fallbacks=[CommandHandler("start", start)],
        )
    )

    # --- Update document ---
    app.add_handler(
        ConversationHandler(
            entry_points=[
                MessageHandler(filters.Regex("✏️ ОНОВИТИ ДОКУМЕНТ"), update_start)
            ],
            states={
                UPDATE_SELECT_DOC: [CallbackQueryHandler(update_select)],
                UPDATE_ENTER_DATE: [
                    MessageHandler(
                        filters.TEXT & ~filters.COMMAND, update_save
                    )
                ],
            },
            fallbacks=[CommandHandler("start", start)],
        )
    )

    # --- Delete document ---
    app.add_handler(
        ConversationHandler(
            entry_points=[
                MessageHandler(filters.Regex("🗑 ВИДАЛИТИ ДОКУМЕНТ"), delete_start)
            ],
            states={
                DELETE_SELECT_DOC: [CallbackQueryHandler(delete_process)],
            },
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
