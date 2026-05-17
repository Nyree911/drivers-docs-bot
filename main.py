import asyncio
import logging
import os
import json
import re
import requests
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import logging

logging.basicConfig(level=logging.INFO)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)

# Telegram 
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

# Google Sheets #1

import gspread
from oauth2client.service_account import ServiceAccountCredentials


# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("❌ BOT_TOKEN is missing. Set it in Railway Variables.") 
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

QUEUE_SHEET_NAME = "QueueWatch"
queue_sheet = client.open(SPREAD_NAME).worksheet(QUEUE_SHEET_NAME)

QUEUE_REQUIRED_COLUMNS = [
    "TELEGRAM",
    "FULL_NAME",
    "CHECKPOINT",
    "TARGET_DATETIME",
    "TARGET_VEHICLES",
    "IS_ACTIVE",
    "ALERT_STARTED",
    "LAST_QUEUE_TEXT",
    "LAST_CHECK_AT",
]

if queue_sheet.row_values(1) != QUEUE_REQUIRED_COLUMNS:
    queue_sheet.delete_rows(1)
    queue_sheet.insert_row(QUEUE_REQUIRED_COLUMNS, 1)

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
    QUEUE_SELECT_CHECKPOINT,
    QUEUE_ENTER_TARGET_DATETIME,
    QUEUE_ASK_VEHICLES,
    QUEUE_ENTER_TARGET_VEHICLES,
) = range(13)


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
            ["🛃 ХОЧУ СТАТИ В ЧЕРГУ", "⛔ ЗУПИНИТИ ЧЕРГУ"],
            ["📋 МОЇ ЧЕРГИ", "🌍 ЗАВАНТАЖЕНІСТЬ КОРДОНІВ"],
        ],
        resize_keyboard=True,
    )

def get_user_full_name(uid) -> str:
    """Бере FULL_NAME з таблиці по TELEGRAM id."""
    for r in sheet.get_all_records():
        if str(r.get("TELEGRAM", "")) == str(uid):
            name = (r.get("FULL_NAME") or "").strip()
            return name if name else "Без імені"
    return "Без імені"

def get_active_queue_watch(uid):
    for r in queue_sheet.get_all_records():
        if str(r.get("TELEGRAM", "")) == str(uid) and str(r.get("IS_ACTIVE", "")).upper() == "TRUE":
            return r
    return None


def upsert_queue_watch(uid, full_name, checkpoint, target_datetime, target_vehicles=""):
    rows = queue_sheet.get_all_records()

    for i, r in enumerate(rows, start=2):
        if str(r.get("TELEGRAM", "")) == str(uid):
            queue_sheet.update(f"A{i}:I{i}", [[
                str(uid),
                full_name,
                checkpoint,
                target_datetime,
                str(target_vehicles) if target_vehicles else "",
                "TRUE",
                "FALSE",
                "",
                "",
            ]])
            return

    queue_sheet.append_row([
        str(uid),
        full_name,
        checkpoint,
        target_datetime,
        str(target_vehicles) if target_vehicles else "",
        "TRUE",
        "FALSE",
        "",
        "",
    ])

def tg_user_label(user) -> str:
    """Формує читабельний підпис користувача."""
    if not user:
        return "Невідомий користувач"
    uname = f"@{user.username}" if getattr(user, "username", None) else "без username"
    full = user.full_name if getattr(user, "full_name", None) else "без імені"
    return f"{full} ({uname})"


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str):
    """Надсилає повідомлення адміну. Помилки не валять бота."""
    try:
        await context.bot.send_message(ADMIN_ID, text)
    except Exception:
        pass

DOC_LABELS = {
    "TP": "ТЕХ ПАСПОРТ",
    "BC": "БІЛИЙ СЕРТИФІКАТ",
    "TO": "ТЕХ ОГЛЯД",
    "TACO": "КАЛІБРОВКА ТАХО",
    "INS": "СТРАХОВИЙ ПОЛІС",
    "GREEN": "ЗЕЛЕНА КАРТА",
}

CHECKPOINTS = {
    "KRAKIVETS": "Краківець – Корчова",
    "RAVA": "Рава-Руська – Хребенне",
    "SHEHYNI": "Шегині – Медика",
    "YAHODYN": "Ягодин – Дорогуськ",
}

WORKLOAD_API_URL = "https://back.echerha.gov.ua/api/v4/workload/1"

CHECKPOINT_TITLE_MAP = {
    "Краківець – Корчова": "Краківець – Корчова (для вантажівок ≥ 7,5 тонн)",
    "Рава-Руська – Хребенне": "Рава-Руська – Хребенне (для вантажівок ≥ 7,5 тонн)",
    "Шегині – Медика": "Шегині – Медика (для вантажівок ≥ 7,5 тонн)",
    "Ягодин – Дорогуськ": "Ягодин – Дорогуськ (для вантажівок ≥ 7,5 тонн)",
}


def minutes_to_text(minutes: int) -> str:
    days = minutes // 1440
    hours = (minutes % 1440) // 60
    mins = minutes % 60

    parts = []

    if days:
        parts.append(f"{days} дн")
    if hours:
        parts.append(f"{hours} год")
    if mins or not parts:
        parts.append(f"{mins} хв")

    return " ".join(parts)


def fetch_workload_data():
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://echerha.gov.ua",
        "Referer": "https://echerha.gov.ua/",
        "x-client-locale": "uk",
        "x-user-agent": "UABorder/3.5.0 Web/1.1.0 User/guest",
        "User-Agent": "Mozilla/5.0",
    }

    resp = requests.get(WORKLOAD_API_URL, headers=headers, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("data", [])
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

    context.user_data.clear()

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
        # --- ADMIN LOG ---
    await notify_admin(
        context,
        "✅ Реєстрація/оновлення ПІБ\n"
        f"👤 {tg_user_label(update.effective_user)}\n"
        f"🆔 {uid}\n"
        f"📛 FULL_NAME: {full}"
    )
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
    await q.edit_message_text("Оберіть тип транспорту")

    await q.message.reply_text(
        "Введіть номер (AA1234BB) або натисніть 🔙 СКАСУВАТИ:",
        reply_markup=ReplyKeyboardMarkup(
            [["🔙 СКАСУВАТИ"]],
            resize_keyboard=True,
        ),
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
    plate = context.user_data["plate"]
    doc_name = context.user_data["doc_name"]
    vehicle_type = context.user_data["vehicle_type"]

    rows = sheet.get_all_records()

    # знаходимо ПІБ користувача (як було)
    user_rows = [r for r in rows if str(r["TELEGRAM"]) == str(uid)]
    if not user_rows:
        await update.message.reply_text("❗ Вас не знайдено у таблиці. Натисніть /start.",
                                        reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    full_name = user_rows[0]["FULL_NAME"]

    # 1) шукаємо існуючий рядок для (uid + plate + doc_name)
    found_row_index = None
    old_date = None

    for i, r in enumerate(rows, start=2):  # start=2 бо 1-й рядок — заголовки
        if (
            str(r.get("TELEGRAM")) == str(uid)
            and (r.get("PLATE") or "").strip().upper() == plate
            and (r.get("DOC_NAME") or "").strip().upper() == doc_name.strip().upper()
        ):
            found_row_index = i
            old_date = r.get("DATE")
            break

    # 2) якщо знайшли — оновлюємо, якщо ні — додаємо новий
    if found_row_index:
        sheet.update_cell(found_row_index, 3, vehicle_type)  # TYPE
        sheet.update_cell(found_row_index, 6, text)          # DATE
        action = f"♻️ Перезаписано (було {old_date} → стало {text})"
    else:
        sheet.append_row([full_name, str(uid), vehicle_type, plate, doc_name, text])
        action = "➕ Додано новий"

    await notify_admin(
        context,
        f"{action}\n"
        f"👤 {tg_user_label(update.effective_user)}\n"
        f"🆔 {uid}\n"
        f"🚘 {vehicle_type} | {plate}\n"
        f"📄 {doc_name}\n"
        f"📅 {text}"
    )

    await update.message.reply_text("Готово ✔", reply_markup=main_menu_keyboard())
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

    # Обробляємо документи
    for d in docs:
        raw_date = d.get("DATE", "").strip()
        if not raw_date:
            continue

        # Парсимо дату
        try:
            exp_date = datetime.strptime(raw_date, "%d.%m.%Y").date()
        except:
            continue

        # Дні до закінчення
        days_left = (exp_date - today).days

        # Формуємо статус
        if days_left < 0:
            status = f"(прострочено {abs(days_left)} дн.)"
        elif days_left == 0:
            status = "(сьогодні)"
        else:
            status = f"(залиш. {days_left} дн.)"

        processed.append({
            "plate": d["PLATE"],
            "doc": d["DOC_NAME"],
            "date": raw_date,
            "days": days_left,
            "status": status
        })

    # Сортування від найближчої дати → до дальньої
    processed.sort(key=lambda x: x["days"])

    # Формування гарного тексту з порожніми рядками
    lines = []
    for p in processed:
        block = (
            f"{p['plate']} | {p['doc']}\n"
            f"   Дата завершення: {p['date']} {p['status']}"
        )
        lines.append(block)
        lines.append("")  # порожній рядок між документами

    # Видалити останній пустий рядок
    text = "\n".join(lines).rstrip()

    await update.message.reply_text(text)


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

    old_date = None
    updated = False

    for i, r in enumerate(rows, start=2):
        if (
            str(r["TELEGRAM"]) == str(uid)
            and r["PLATE"] == context.user_data.get("plate")
            and r["DOC_NAME"] == context.user_data.get("doc")
        ):
            old_date = r.get("DATE")
            sheet.update_cell(i, 6, text)
            updated = True
            break

    # --- ADMIN LOG ---
    await notify_admin(
        context,
        "✏️ Оновлено документ\n"
        f"👤 {tg_user_label(update.effective_user)}\n"
        f"🆔 {uid}\n"
        f"🚘 {context.user_data.get('plate')}\n"
        f"📄 {context.user_data.get('doc')}\n"
        f"📅 було: {old_date} → стало: {text}\n"
        f"✅ {'так' if updated else 'ні (не знайдено рядок)'}"
    )

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
    deleted = False

    for i, r in enumerate(rows, start=2):
        if (
            r["PLATE"] == plate
            and r["DOC_NAME"] == doc
            and str(r["TELEGRAM"]) == str(uid)
        ):
            sheet.delete_rows(i)
            deleted = True
            break

    await q.edit_message_text("Документ видалено ✔")

    await q.message.reply_text(
        "Головне меню:", reply_markup=main_menu_keyboard()
    )

        # --- ADMIN LOG ---
    await notify_admin(
        context,
        "🗑 Видалено документ\n"
        f"👤 {tg_user_label(q.from_user)}\n"
        f"🆔 {uid}\n"
        f"🚘 {plate}\n"
        f"📄 {doc}\n"
        f"✅ {'так' if deleted else 'ні (не знайдено рядок)'}"
    )
    return ConversationHandler.END


# ============================================================
# QUEUE WATCH
# ============================================================

async def queue_watch_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton(v, callback_data=f"QUEUE_CP:{k}")] for k, v in CHECKPOINTS.items()]
    kb.append([InlineKeyboardButton("❌ СКАСУВАТИ", callback_data="QUEUE_CANCEL")])

    await update.message.reply_text(
        "Оберіть пункт пропуску:",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return QUEUE_SELECT_CHECKPOINT


async def queue_watch_select_checkpoint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "QUEUE_CANCEL":
        return await cancel(update, context)

    if not q.data.startswith("QUEUE_CP:"):
        return QUEUE_SELECT_CHECKPOINT

    code = q.data.split("QUEUE_CP:", 1)[1]
    context.user_data["queue_checkpoint_code"] = code
    context.user_data["queue_checkpoint_name"] = CHECKPOINTS[code]

    await q.edit_message_text(f"Обрано: {CHECKPOINTS[code]}")
    await q.message.reply_text(
        "Введіть бажаний час перетину у форматі ДД.ММ.РРРР ГГ:ХХ\n"
        "Наприклад: 25.05.2026 14:00",
        reply_markup=ReplyKeyboardMarkup([["🔙 СКАСУВАТИ"]], resize_keyboard=True),
    )
    return QUEUE_ENTER_TARGET_DATETIME


async def queue_watch_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "🔙 СКАСУВАТИ":
        return await cancel(update, context)

    try:
        target_dt = datetime.strptime(text, "%d.%m.%Y %H:%M").replace(
            tzinfo=ZoneInfo("Europe/Kyiv")
        )
    except Exception:
        await update.message.reply_text(
            "❗ Неправильний формат. Введіть так: 25.05.2026 14:00",
            reply_markup=ReplyKeyboardMarkup([["🔙 СКАСУВАТИ"]], resize_keyboard=True),
        )
        return QUEUE_ENTER_TARGET_DATETIME

    now = datetime.now(ZoneInfo("Europe/Kyiv"))

    if target_dt <= now:
        await update.message.reply_text(
            "❗ Ця дата/час вже в минулому. Введіть майбутній час.",
            reply_markup=ReplyKeyboardMarkup([["🔙 СКАСУВАТИ"]], resize_keyboard=True),
        )
        return QUEUE_ENTER_TARGET_DATETIME

    max_dt = now + timedelta(days=10)
    if target_dt > max_dt:
        await update.message.reply_text(
            "❗ Можна обрати дату не більше ніж на 10 днів вперед.",
            reply_markup=ReplyKeyboardMarkup([["🔙 СКАСУВАТИ"]], resize_keyboard=True),
        )
        return QUEUE_ENTER_TARGET_DATETIME

    context.user_data["queue_target_datetime"] = text

    kb = [
        [InlineKeyboardButton("🚚 Додати поріг по машинах", callback_data="QUEUE_VEHICLES_YES")],
        [InlineKeyboardButton("⏱ Тільки по часу", callback_data="QUEUE_VEHICLES_NO")],
        [InlineKeyboardButton("❌ СКАСУВАТИ", callback_data="QUEUE_VEHICLES_CANCEL")],
    ]

    await update.message.reply_text(
        "Хочете додати ще поріг по кількості машин у черзі?",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return QUEUE_ASK_VEHICLES


async def queue_watch_choose_vehicles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "QUEUE_VEHICLES_CANCEL":
        return await cancel(update, context)

    if q.data == "QUEUE_VEHICLES_NO":
        uid = q.from_user.id
        full_name = get_user_full_name(uid)
        checkpoint = context.user_data["queue_checkpoint_name"]
        target_datetime = context.user_data["queue_target_datetime"]

        upsert_queue_watch(uid, full_name, checkpoint, target_datetime, "")

        await q.edit_message_text("Обрано: тільки сповіщення по часу.")
        await q.message.reply_text(
            f"Заявку збережено ✔\n\n"
            f"Пункт пропуску: {checkpoint}\n"
            f"Бажаний перетин: {target_datetime}\n"
            f"Поріг по машинах: не задано",
            reply_markup=main_menu_keyboard(),
        )

        await notify_admin(
            context,
            "🛃 Активовано моніторинг черги\n"
            f"👤 {tg_user_label(q.from_user)}\n"
            f"🆔 {uid}\n"
            f"📍 {checkpoint}\n"
            f"🕒 {target_datetime}\n"
            f"🚚 Машини: не задано"
        )

        context.user_data.pop("queue_target_datetime", None)
        return ConversationHandler.END

    if q.data == "QUEUE_VEHICLES_YES":
        await q.edit_message_text("Обрано: додати поріг по машинах.")
        await q.message.reply_text(
            "Введіть кількість машин, наприклад: 20",
            reply_markup=ReplyKeyboardMarkup([["🔙 СКАСУВАТИ"]], resize_keyboard=True),
        )
        return QUEUE_ENTER_TARGET_VEHICLES

    return QUEUE_ASK_VEHICLES


async def queue_watch_save_vehicles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "🔙 СКАСУВАТИ":
        return await cancel(update, context)

    if not text.isdigit():
        await update.message.reply_text(
            "❗ Введіть тільки число. Наприклад: 20",
            reply_markup=ReplyKeyboardMarkup([["🔙 СКАСУВАТИ"]], resize_keyboard=True),
        )
        return QUEUE_ENTER_TARGET_VEHICLES

    target_vehicles = int(text)

    if target_vehicles <= 0:
        await update.message.reply_text(
            "❗ Кількість машин має бути більшою за 0.",
            reply_markup=ReplyKeyboardMarkup([["🔙 СКАСУВАТИ"]], resize_keyboard=True),
        )
        return QUEUE_ENTER_TARGET_VEHICLES

    uid = update.message.chat_id
    full_name = get_user_full_name(uid)
    checkpoint = context.user_data["queue_checkpoint_name"]
    target_datetime = context.user_data["queue_target_datetime"]

    upsert_queue_watch(uid, full_name, checkpoint, target_datetime, target_vehicles)

    await update.message.reply_text(
        f"Заявку збережено ✔\n\n"
        f"Пункт пропуску: {checkpoint}\n"
        f"Бажаний перетин: {target_datetime}\n"
        f"Поріг по машинах: {target_vehicles}\n\n"
        f"Бот сповістить, коли раніше спрацює або час, або кількість машин.",
        reply_markup=main_menu_keyboard(),
    )

    await notify_admin(
        context,
        "🛃 Активовано моніторинг черги\n"
        f"👤 {tg_user_label(update.effective_user)}\n"
        f"🆔 {uid}\n"
        f"📍 {checkpoint}\n"
        f"🕒 {target_datetime}\n"
        f"🚚 Машини: {target_vehicles}"
    )

    context.user_data.pop("queue_target_datetime", None)
    return ConversationHandler.END



async def queue_watch_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.chat_id
    rows = queue_sheet.get_all_records()
    stopped = False

    for i, r in enumerate(rows, start=2):
        if str(r.get("TELEGRAM", "")) == str(uid):
            queue_sheet.update_cell(i, 6, "FALSE")  # IS_ACTIVE
            stopped = True
            break

    if stopped:
        await update.message.reply_text(
            "Нагадування про чергу зупинено ✔",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await update.message.reply_text(
            "Активної заявки на чергу немає.",
            reply_markup=main_menu_keyboard(),
        )



async def my_queues(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = update.message.chat_id
        is_admin = uid == ADMIN_ID

        rows = queue_sheet.get_all_records()
        print("MY_QUEUES rows count:", len(rows))

        if is_admin:
            filtered = [
                r for r in rows
                if str(r.get("IS_ACTIVE", "")).upper() == "TRUE"
            ]
        else:
            filtered = [
                r for r in rows
                if str(r.get("TELEGRAM", "")).strip() == str(uid)
                and str(r.get("IS_ACTIVE", "")).upper() == "TRUE"
            ]

        print("MY_QUEUES filtered count:", len(filtered), "uid:", uid)

        if not filtered:
            await update.message.reply_text(
                "Активних черг немає.",
                reply_markup=main_menu_keyboard(),
            )
            return

        lines = []

        for i, r in enumerate(filtered, start=1):
            full_name = str(r.get("FULL_NAME", "Без імені")).strip()
            checkpoint = str(r.get("CHECKPOINT", "—")).strip()
            target_dt = str(r.get("TARGET_DATETIME", "—")).strip()
            target_vehicles = str(r.get("TARGET_VEHICLES", "")).strip()
            last_queue = str(r.get("LAST_QUEUE_TEXT", "немає даних")).strip()
            last_check = str(r.get("LAST_CHECK_AT", "ще не перевірялось")).strip()

            vehicles_text = target_vehicles if target_vehicles else "не задано"

            if is_admin:
                block = (
                    f"{i}. {full_name}\n"
                    f"Пункт: {checkpoint}\n"
                    f"Бажаний перетин: {target_dt}\n"
                    f"Поріг по машинах: {vehicles_text}\n"
                    f"Остання черга: {last_queue}\n"
                    f"Перевірено: {last_check}"
                )
            else:
                block = (
                    f"{i}. {checkpoint}\n"
                    f"Бажаний перетин: {target_dt}\n"
                    f"Поріг по машинах: {vehicles_text}\n"
                    f"Остання черга: {last_queue}\n"
                    f"Перевірено: {last_check}"
                )

            lines.append(block)

        text = "\n\n".join(lines)

        await update.message.reply_text(
            text,
            reply_markup=main_menu_keyboard(),
        )

    except Exception as e:
        print("MY_QUEUES ERROR:", e)
        await update.message.reply_text(
            f"Не вдалося отримати черги.\nПомилка: {e}",
            reply_markup=main_menu_keyboard(),
        )

async def border_load(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        items = fetch_workload_data()
    except Exception as e:
        await update.message.reply_text(
            f"Не вдалося отримати дані по кордонах.\nПомилка: {e}",
            reply_markup=main_menu_keyboard(),
        )
        return

    monitored_titles = {
        "Краківець – Корчова": "Краківець – Корчова (для вантажівок ≥ 7,5 тонн)",
        "Рава-Руська – Хребенне": "Рава-Руська – Хребенне (для вантажівок ≥ 7,5 тонн)",
        "Шегині – Медика": "Шегині – Медика (для вантажівок ≥ 7,5 тонн)",
        "Ягодин – Дорогуськ": "Ягодин – Дорогуськ (для вантажівок ≥ 7,5 тонн)",
    }

    lines = []
    now_text = datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y %H:%M")

    for short_name, full_title in monitored_titles.items():
        found = None

        for item in items:
            title = (item.get("title") or "").strip()
            if title == full_title:
                found = item
                break

        if not found:
            lines.append(f"{short_name}\nЧерга: немає даних\n")
            continue

        wait_minutes = int((found.get("wait_time") or 0) / 60)
        queue_text = minutes_to_text(wait_minutes)
        vehicles = int(found.get("vehicle_in_active_queues_counts") or 0)
        is_paused = bool(found.get("is_paused"))

        pause_text = "так" if is_paused else "ні"

        block = (
            f"{short_name}\n"
            f"Черга: {queue_text}\n"
            f"Машин в активних чергах: {vehicles}\n"
            f"Пауза: {pause_text}"
        )
        lines.append(block)

    text = (
        f"🌍 Завантаженість кордонів станом на {now_text}\n\n"
        + "\n\n".join(lines)
    )

    await update.message.reply_text(
        text,
        reply_markup=main_menu_keyboard(),
    )

# ============================================================
# REMINDERS
# ============================================================

REMINDER_DAYS = {30, 25, 20, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0}


async def reminders_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    print(f"[reminders_job] Fired at {now}")
    app = context.application

    hour = datetime.now(ZoneInfo("Europe/Kyiv")).hour
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

        try:
            uid = int(r["TELEGRAM"])
        except Exception:
            continue

        if days < 0:
            msg_user = f"⛔ ПРОСТРОЧЕНО: {r['DOC_NAME']} ({r['PLATE']})"
        elif days == 0:
            msg_user = f"❗ СЬОГОДНІ закінчується {r['DOC_NAME']} ({r['PLATE']})"
        elif days in REMINDER_DAYS:
            msg_user = (
                f"⚠️ Через {days} днів закінчується {r['DOC_NAME']} ({r['PLATE']})"
            )
        else:
            continue

        msg_admin = f"📣 {r['FULL_NAME']} → {msg_user}"

        if uid != ADMIN_ID:
            try:
                await app.bot.send_message(uid, msg_user)
            except Exception as e:
                print(f"Помилка відправки користувачу {uid}: {e}")

        try:
            await app.bot.send_message(ADMIN_ID, msg_admin)
        except Exception as e:
            print(f"Помилка відправки адміну: {e}")


# ============================================================
# QUEUE WATCH JOB
# ============================================================

def parse_target_datetime(text: str) -> datetime:
    return datetime.strptime(text.strip(), "%d.%m.%Y %H:%M").replace(
        tzinfo=ZoneInfo("Europe/Kyiv")
    )


def parse_queue_duration_to_minutes(text: str) -> int:
    """
    Приклади:
    '2 дні 2 години 25 хв'
    '2 дні 25 хв'
    '3 години 10 хв'
    '45 хв'
    """
    s = text.lower().strip()

    days = 0
    hours = 0
    minutes = 0

    m = re.search(r"(\d+)\s*д", s)
    if m:
        days = int(m.group(1))

    m = re.search(r"(\d+)\s*г", s)
    if m:
        hours = int(m.group(1))

    m = re.search(r"(\d+)\s*хв", s)
    if m:
        minutes = int(m.group(1))

    return days * 24 * 60 + hours * 60 + minutes


def get_active_queue_rows():
    rows = queue_sheet.get_all_records()
    result = []
    for i, r in enumerate(rows, start=2):
        if str(r.get("IS_ACTIVE", "")).upper() == "TRUE":
            result.append((i, r))
    return result


async def fetch_checkpoint_queue_text(checkpoint_name: str) -> str | None:
    url = "https://back.echerha.gov.ua/api/v4/workload/1"

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://echerha.gov.ua",
        "Referer": "https://echerha.gov.ua/",
        "x-client-locale": "uk",
        "x-user-agent": "UABorder/3.5.0 Web/1.1.0 User/guest",
        "User-Agent": "Mozilla/5.0",
    }

    title_map = {
        "Краківець – Корчова": "Краківець – Корчова (для вантажівок ≥ 7,5 тонн)",
        "Рава-Руська – Хребенне": "Рава-Руська – Хребенне (для вантажівок ≥ 7,5 тонн)",
        "Шегині – Медика": "Шегині – Медика (для вантажівок ≥ 7,5 тонн)",
        "Ягодин – Дорогуськ": "Ягодин – Дорогуськ (для вантажівок ≥ 7,5 тонн)",
    }

    target_title = title_map.get(checkpoint_name)
    if not target_title:
        print("FETCH ERROR: unknown checkpoint_name:", checkpoint_name)
        return None

    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        print("FETCH ERROR: request failed:", e)
        return None

    items = payload.get("data", [])
    for item in items:
        title = (item.get("title") or "").strip()

        if title != target_title:
            continue

        wait_minutes = int((item.get("wait_time") or 0) / 60)
     

        days = wait_minutes // (24 * 60)
        hours = (wait_minutes % (24 * 60)) // 60
        minutes = wait_minutes % 60

        parts = []
        if days:
            parts.append(f"{days} дні" if days not in {1} else f"{days} день")
        if hours:
            parts.append(f"{hours} години" if hours not in {1} else f"{hours} година")
        if minutes or not parts:
            parts.append(f"{minutes} хв")

        queue_text = " ".join(parts)

        print("FETCH OK:", checkpoint_name, "|", wait_minutes, "min |", queue_text)
        return queue_text

    print("FETCH ERROR: checkpoint not found:", checkpoint_name)
    return None

async def queue_watch_job(context: ContextTypes.DEFAULT_TYPE):
    print("QUEUE JOB START")

    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    active_rows = get_active_queue_rows()

    print("active_rows:", len(active_rows))

    for row_index, r in active_rows:
        try:
            if str(r.get("IS_ACTIVE", "")).upper() != "TRUE":
                continue

            checkpoint_name = r.get("CHECKPOINT")
            uid = int(r["TELEGRAM"])
            target_dt = parse_target_datetime(r["TARGET_DATETIME"])
            target_vehicles_raw = str(r.get("TARGET_VEHICLES", "")).strip()

            minutes_until_target = int((target_dt - now).total_seconds() / 60)

            items = fetch_workload_data()
            found = None

            target_title = CHECKPOINT_TITLE_MAP.get(checkpoint_name)
            for item in items:
                if (item.get("title") or "").strip() == target_title:
                    found = item
                    break

            if not found:
                continue

            wait_minutes = int((found.get("wait_time") or 0) / 60)
            vehicles_count = int(found.get("vehicle_in_active_queues_counts") or 0)
            queue_text = minutes_to_text(wait_minutes)

            now_str = now.strftime("%d.%m.%Y %H:%M")
            queue_sheet.update(f"H{row_index}:I{row_index}", [[
                queue_text,
                now_str
            ]])

            print("queue_minutes:", wait_minutes)
            print("minutes_until_target:", minutes_until_target)
            print("vehicles_count:", vehicles_count)
            print("target_vehicles_raw:", target_vehicles_raw)

            time_trigger = wait_minutes >= max(minutes_until_target - 30, 0)

            vehicles_trigger = False
            if target_vehicles_raw.isdigit():
                vehicles_trigger = vehicles_count >= int(target_vehicles_raw)

            if time_trigger or vehicles_trigger:
                reason = "за часом"
                if vehicles_trigger and not time_trigger:
                    reason = "за кількістю машин"
                elif vehicles_trigger and time_trigger:
                    reason = "за часом / машинами"

                text = (
                    f"🚨 ЧАС СТАВАТИ В ЧЕРГУ\n\n"
                    f"Пункт пропуску: {checkpoint_name}\n"
                    f"Причина спрацювання: {reason}\n"
                    f"Поточна черга: {queue_text}\n"
                    f"Машин в активних чергах: {vehicles_count}\n\n"
                    f"Натисни «⛔ ЗУПИНИТИ ЧЕРГУ», коли вже став."
                )

                await context.bot.send_message(uid, text)

        except Exception as e:
            print("QUEUE JOB ERROR:", e)

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
            interval=10800,  # що 3 години
            first=10,        # перший запуск через 10 секунд
        )

        app.job_queue.run_repeating(
            queue_watch_job,
            interval=30,
            first=20,
        )

        print("[post_init] Job queue started")
    except Exception as e:
        print("[post_init] Job queue error:", e)

    try:
        await app.bot.send_message(
            ADMIN_ID, "🔄 Бот перезавантажено і job_queue активний."
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

    app.add_handler(MessageHandler(filters.StatusUpdate.ALL, lambda u, c: None))

    # --- Registration (/start) ---
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("start", start)],
            states={
                REG_ENTER_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, register_save),
                ],
            },
            fallbacks=[
                CommandHandler("start", start),
                MessageHandler(filters.Regex(r"^🔙 СКАСУВАТИ$"), cancel),
            ],
            allow_reentry=True,
        )
    )

    # --- Add document ---
    app.add_handler(
        ConversationHandler(
            entry_points=[
                MessageHandler(filters.TEXT & filters.Regex(r".*ДОДАТИ ДОКУМЕНТ.*"), add_doc_start)
            ],
            states={
                ADD_SELECT_TYPE: [CallbackQueryHandler(add_doc_type)],
                ADD_ENTER_PLATE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_doc_plate),
                ],
                ADD_SELECT_DOC: [CallbackQueryHandler(add_doc_name)],
                ADD_ENTER_CUSTOM_DOC: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_custom_doc)
                ],
                ADD_ENTER_DATE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_doc_date)
                ],
            },
            fallbacks=[
                CommandHandler("start", start),
                MessageHandler(filters.Regex(r"^🔙 СКАСУВАТИ$"), cancel),
            ],
            allow_reentry=True,
        )
    )

    # --- Update document ---
    app.add_handler(
        ConversationHandler(
            entry_points=[
                MessageHandler(filters.TEXT & filters.Regex(r".*ОНОВИТИ ДОКУМЕНТ.*"), update_start)
            ],
            states={
                UPDATE_SELECT_DOC: [CallbackQueryHandler(update_select)],
                UPDATE_ENTER_DATE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, update_save)
                ],
            },
            fallbacks=[
                CommandHandler("start", start),
                MessageHandler(filters.Regex(r"^🔙 СКАСУВАТИ$"), cancel),
            ],
            allow_reentry=True,
        )
    )

    # --- Delete document ---
    app.add_handler(
        ConversationHandler(
            entry_points=[
                MessageHandler(filters.TEXT & filters.Regex(r".*ВИДАЛИТИ ДОКУМЕНТ.*"), delete_start)
            ],
            states={
                DELETE_SELECT_DOC: [CallbackQueryHandler(delete_process)],
            },
            fallbacks=[
                CommandHandler("start", start),
                MessageHandler(filters.Regex(r"^🔙 СКАСУВАТИ$"), cancel),
            ],
            allow_reentry=True,
        )
    )

    # --- Queue watch ---
    app.add_handler(
        ConversationHandler(
            entry_points=[
                MessageHandler(filters.TEXT & filters.Regex(r".*ХОЧУ СТАТИ В ЧЕРГУ.*"), queue_watch_start)
            ],
            states={
                QUEUE_SELECT_CHECKPOINT: [
                    CallbackQueryHandler(
                        queue_watch_select_checkpoint,
                        pattern=r"^(QUEUE_CP:|QUEUE_CANCEL)"
                    )
                ],
                QUEUE_ENTER_TARGET_DATETIME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, queue_watch_save)
                ],
                QUEUE_ASK_VEHICLES: [
                    CallbackQueryHandler(
                        queue_watch_choose_vehicles,
                        pattern=r"^(QUEUE_VEHICLES_YES|QUEUE_VEHICLES_NO|QUEUE_VEHICLES_CANCEL)$"
                    )
                ],
                QUEUE_ENTER_TARGET_VEHICLES: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, queue_watch_save_vehicles)
                ],
            },
            fallbacks=[
                CommandHandler("start", start),
                MessageHandler(filters.Regex(r"^🔙 СКАСУВАТИ$"), cancel),
            ],
            allow_reentry=True,
        )
    )

    # --- Simple handlers ---
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r".*МОЇ ДОКУМЕНТИ.*"), my_docs))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r".*ЗУПИНИТИ ЧЕРГУ.*"), queue_watch_stop))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r".*МОЇ ЧЕРГИ.*"), my_queues))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r".*ЗАВАНТАЖЕНІСТЬ КОРДОНІВ.*"), border_load))

    print("BOT RUNNING 🚀")

    try:
        asyncio.run(app.run_polling())
    except RuntimeError:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(app.run_polling())

if __name__ == "__main__":
    main()
