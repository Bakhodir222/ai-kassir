import logging
import re
import os
import json
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN      = os.environ["BOT_TOKEN"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SHEET_NAME     = os.environ.get("SHEET_NAME", "Приход")
GROUP_CHAT_ID  = int(os.environ["GROUP_CHAT_ID"])

MANAGERS = {
    "aziza":   "Азиза",
    "zilola":  "Зилола",
    "dilnoza": "Дилноза",
}

TARIFF       = "Стандарт"
CONTRACT_SUM = 500000

def get_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_info = json.loads(os.environ["GOOGLE_CREDS_JSON"])
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)

def append_row(row):
    sheet = get_sheet()
    sheet.append_row(row, value_input_option="USER_ENTERED")

def get_all_fios():
    sheet = get_sheet()
    values = sheet.col_values(3)
    return [v.strip().lower() for v in values[1:] if v.strip()]

def is_phone(s):
    clean = re.sub(r"[\s\-\(\)\+\.]", "", s)
    return bool(re.match(r"^\d{9,15}$", clean))

def parse_amount(s):
    s = str(s).strip().replace(" ", "").rstrip(".")
    if not s:
        return None
    if re.match(r"^\d+\.\d{3}$", s):
        return int(s.replace(".", ""))
    clean = re.sub(r"[^\d]", "", s)
    if not clean:
        return None
    v = int(clean)
    return v * 1000 if 0 < v < 1000 else v

def parse_message(text):
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    tl = text.lower()

    PAYMENT_KEYWORDS = [
        "тулик", "тўлик", "брон", "туланди", "колди",
        "толик", "toliq", "tolov", "толов", "минг"
    ]
    if not any(kw in tl for kw in PAYMENT_KEYWORDS):
        return None

    SKIP_PHRASES = [
        "alif:", "pul o'tkazmasi", "тулик туламаганла",
        "тулик ёзипсизде", "assalomu", "xaqiqiy chek",
        "ochirildi", "profilimga", "togirlab"
    ]
    if any(skip in tl for skip in SKIP_PHRASES):
        return None

    fio = ""
    phone = ""
    for line in lines:
        if line.startswith("@"):
            continue
        if is_phone(line):
            if not phone:
                phone = line.strip()
            continue
        if any(kw in line.lower() for kw in PAYMENT_KEYWORDS + ["россия", "usa", "сша"]):
            continue
        if re.match(r"^\d{4}$", line):
            continue
        if not fio:
            fio = line
    fio = re.sub(r"\s+\d{9,15}$", "", fio).strip()

    if not fio:
        return None

    paid = 0
    status = "Тулик"

    is_tulik    = any(kw in tl for kw in ["тулик", "тўлик", "толик", "toliq"])
    has_koldi   = any(kw in tl for kw in ["колди", "qoldi"])
    has_bron    = "брон" in tl
    has_tulandy = any(kw in tl for kw in ["туланди", "толов", "tolov"])

    if is_tulik and not has_koldi and not has_bron:
        paid = CONTRACT_SUM
        status = "Тулик"
    elif is_tulik and has_koldi:
        paid = CONTRACT_SUM
        status = "Тулик"
    elif has_bron and is_tulik and not has_tulandy:
        paid = CONTRACT_SUM
        status = "Тулик"
    elif has_bron and not is_tulik:
        m = re.search(r"брон\s+([\d\.\s]+)", tl)
        if m:
            v = parse_amount(m.group(1).strip().split()[0])
            if v:
                paid = v
        status = "Брон"
    elif has_tulandy:
        m = re.search(r"([\d\.]+)\s*(?:туланди|толов|tolov)", tl)
        if m:
            v = parse_amount(m.group(1))
            if v:
                paid = v
        m2 = re.search(r"(\d+)\s*минг\s*(?:толов|tolov)", tl)
        if m2:
            paid = int(m2.group(1)) * 1000
        status = "Брон"

    if paid == 0 and is_tulik:
        paid = CONTRACT_SUM

    return {"fio": fio, "phone": phone, "paid": paid, "status": status}

def detect_manager(sender_name):
    sl = sender_name.lower()
    for key, display in MANAGERS.items():
        if key in sl:
            return display
    return sender_name

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat

    if chat.id != GROUP_CHAT_ID:
        return

    text = msg.text or msg.caption or ""
    sender = msg.from_user
    sender_name = f"{sender.first_name or ''} {sender.last_name or ''}".strip()

    if not text:
        return

    logger.info(f"Сообщение от {sender_name}: {text[:50]}")

    data = parse_message(text)
    if data is None:
        logger.info("Не распознано как чек — пропускаю")
        return

    logger.info(f"Распознан чек: {data}")

    manager = detect_manager(sender_name)
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    note = ""
    try:
        logger.info("Подключаюсь к Google Sheets...")
        existing = get_all_fios()
        logger.info(f"Получено {len(existing)} ФИО из таблицы")
        if data["fio"].strip().lower() in existing:
            sheet = get_sheet()
            all_rows = sheet.get_all_values()
            first_date = ""
            for row in all_rows[1:]:
                if len(row) >= 3 and row[2].strip().lower() == data["fio"].strip().lower():
                    first_date = row[0]
                    break
            note = f"Дубль (первый чек: {first_date})"
            logger.info(f"Найден дубль: {data['fio']}")
    except Exception as e:
        logger.error(f"Ошибка подключения к Sheets: {e}")
        await msg.reply_text(f"❌ Ошибка подключения к таблице: {e}")
        return

    try:
        logger.info(f"Записываю строку: {data['fio']}")
        sheet = get_sheet()
        next_row = len(sheet.get_all_values()) + 1
        formula_remainder = f"=F{next_row}-G{next_row}"

        row = [
            now, manager, data["fio"], data["phone"],
            TARIFF, CONTRACT_SUM, data["paid"],
            formula_remainder, data["status"], note,
        ]
        append_row(row)
        logger.info(f"Записано в строку {next_row}!")

        status_emoji = "✅" if data["status"] == "Тулик" else "🕐"
        dup_text = f"\n⚠️ {note}" if note else ""
        await msg.reply_text(
            f"{status_emoji} Записано:\n"
            f"👤 {data['fio']}\n"
            f"📞 {data['phone'] or '—'}\n"
            f"💰 {data['paid']:,} сум | {data['status']}{dup_text}",
            parse_mode=None
        )

    except Exception as e:
        logger.error(f"Ошибка записи в таблицу: {e}")
        await msg.reply_text(f"❌ Ошибка при записи в таблицу: {e}")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))
    logger.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
