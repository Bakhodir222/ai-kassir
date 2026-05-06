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

# Столбцы (1-based для gspread):
# A=№  B=Дата  C=Менеджер  D=ФИО  E=Телефон  F=Тариф  G=Сумма  H=Оплата  I=Остаток(=G-H)  J=Статус  K=Примечание

COL_NUM      = 1   # A
COL_DATE     = 2   # B
COL_MANAGER  = 3   # C
COL_FIO      = 4   # D
COL_PHONE    = 5   # E
COL_TARIFF   = 6   # F
COL_SUM      = 7   # G
COL_PAID     = 8   # H
COL_REM      = 9   # I
COL_STATUS   = 10  # J
COL_NOTE     = 11  # K

def get_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_info = json.loads(os.environ["GOOGLE_CREDS_JSON"])
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)

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

def find_duplicate_row(sheet, fio):
    """
    Ищет строку с таким же ФИО в таблице.
    Возвращает (sheet_row_index, original_date) или (None, None).
    sheet_row_index — номер строки в таблице (1-based, включая заголовок).
    """
    all_rows = sheet.get_all_values()
    fio_lower = fio.strip().lower()
    for i, row in enumerate(all_rows[1:], start=2):  # строки данных начиная с 2
        if len(row) >= COL_FIO and row[COL_FIO - 1].strip().lower() == fio_lower:
            original_date = row[COL_DATE - 1] if len(row) >= COL_DATE else ""
            return i, original_date
    return None, None

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

    logger.info(f"Сообщение от {sender_name}: {text[:60]}")

    data = parse_message(text)
    if data is None:
        logger.info("Не распознано как чек — пропускаю")
        return

    logger.info(f"Распознан чек: {data}")

    manager = detect_manager(sender_name)
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    try:
        logger.info("Подключаюсь к Google Sheets...")
        sheet = get_sheet()
        all_rows = sheet.get_all_values()
        logger.info(f"Всего строк в таблице: {len(all_rows)}")

        # Проверяем дубль
        dup_row_index, original_date = find_duplicate_row(sheet, data["fio"])

        if dup_row_index is not None:
            # ── ДУБЛЬ: не добавляем новую строку, помечаем оригинал ──
            logger.info(f"Дубль найден в строке {dup_row_index}: {data['fio']}")

            # Читаем текущую заметку оригинала (столбец K)
            original_note_cell = sheet.cell(dup_row_index, COL_NOTE).value or ""
            dup_note = f"Дубль чек: {now}"
            if original_note_cell:
                new_note = original_note_cell + f" | {dup_note}"
            else:
                new_note = dup_note

            # Обновляем ячейку примечания оригинальной строки
            sheet.update_cell(dup_row_index, COL_NOTE, new_note)
            logger.info(f"Обновлена заметка в строке {dup_row_index}")

            await msg.reply_text(
                f"⚠️ Дубль!\n"
                f"👤 {data['fio']} уже есть в таблице (строка {dup_row_index - 1}, первый чек: {original_date})\n"
                f"Новая строка не добавлена. Отметка поставлена в оригинальной записи."
            )

        else:
            # ── НОВЫЙ КЛИЕНТ: добавляем строку ──
            next_row = len(all_rows) + 1
            # Номер клиента = количество строк данных (без заголовка)
            client_num = len(all_rows)  # all_rows включает заголовок, поэтому -1+1 = len
            formula_remainder = f"=G{next_row}-H{next_row}"

            row = [
                client_num,        # A — №
                now,               # B — Дата
                manager,           # C — Менеджер
                data["fio"],       # D — ФИО
                data["phone"],     # E — Телефон
                TARIFF,            # F — Тариф
                CONTRACT_SUM,      # G — Сумма договора
                data["paid"],      # H — Оплата
                formula_remainder, # I — Остаток =G-H
                data["status"],    # J — Статус
                "",                # K — Примечание
            ]
            sheet.append_row(row, value_input_option="USER_ENTERED")
            logger.info(f"Записано в строку {next_row}: {data['fio']}")

            status_emoji = "✅" if data["status"] == "Тулик" else "🕐"
            await msg.reply_text(
                f"{status_emoji} Записано:\n"
                f"👤 {data['fio']}\n"
                f"📞 {data['phone'] or '—'}\n"
                f"💰 {data['paid']:,} сум | {data['status']}",
                parse_mode=None
            )

    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await msg.reply_text(f"❌ Ошибка: {e}")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))
    logger.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
