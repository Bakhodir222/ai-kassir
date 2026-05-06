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
CONTACTS_SHEET = "Контакты"
GROUP_CHAT_ID  = int(os.environ["GROUP_CHAT_ID"])

MANAGERS = {
    "aziza":   "Азиза",
    "zilola":  "Зилола",
    "dilnoza": "Дилноза",
}

TARIFF       = "Стандарт"
CONTRACT_SUM = 500000

# Столбцы листа Приход (1-based)
# A=№  B=Дата  C=Менеджер  D=ФИО  E=Контакт  F=Тариф  G=Сумма  H=Оплата  I=Остаток  J=Статус  K=Примечание
COL_P_NUM     = 1
COL_P_DATE    = 2
COL_P_MANAGER = 3
COL_P_FIO     = 4
COL_P_CONTACT = 5
COL_P_TARIFF  = 6
COL_P_SUM     = 7
COL_P_PAID    = 8
COL_P_REM     = 9
COL_P_STATUS  = 10
COL_P_NOTE    = 11

# Столбцы листа Контакты (1-based)
# A=ФИО  B=Телефон  C=@Username  D=Дата добавления  E=Источник
COL_C_FIO      = 1
COL_C_PHONE    = 2
COL_C_USERNAME = 3
COL_C_DATE     = 4
COL_C_SOURCE   = 5

def get_spreadsheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_info = json.loads(os.environ["GOOGLE_CREDS_JSON"])
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)

def normalize_phone(s):
    return re.sub(r"[^\d]", "", s or "")

def is_phone(s):
    clean = normalize_phone(s)
    return bool(re.match(r"^\d{9,15}$", clean))

def parse_amount(s):
    s = str(s).strip().replace(" ", "").rstrip(".")
    if not s: return None
    if re.match(r"^\d+\.\d{3}$", s): return int(s.replace(".", ""))
    clean = re.sub(r"[^\d]", "", s)
    if not clean: return None
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

    fio = ""; phone = ""; username = ""

    for line in lines:
        if line.startswith("@"):
            if not username: username = line.strip()
            continue
        if is_phone(line):
            if not phone: phone = line.strip()
            continue
        if any(kw in line.lower() for kw in PAYMENT_KEYWORDS + ["россия", "usa", "сша"]):
            continue
        if re.match(r"^\d{4}$", line): continue
        if not fio: fio = line

    fio = re.sub(r"\s+\d{9,15}$", "", fio).strip()
    if not fio: return None

    contact_parts = []
    if phone: contact_parts.append(phone)
    if username: contact_parts.append(username)
    contact = " / ".join(contact_parts)

    paid = 0; status = "Тулик"
    is_tulik    = any(kw in tl for kw in ["тулик", "тўлик", "толик", "toliq"])
    has_koldi   = any(kw in tl for kw in ["колди", "qoldi"])
    has_bron    = "брон" in tl
    has_tulandy = any(kw in tl for kw in ["туланди", "толов", "tolov"])

    if is_tulik and not has_koldi and not has_bron:
        paid = CONTRACT_SUM; status = "Тулик"
    elif is_tulik and has_koldi:
        paid = CONTRACT_SUM; status = "Тулик"
    elif has_bron and is_tulik and not has_tulandy:
        paid = CONTRACT_SUM; status = "Тулик"
    elif has_bron and not is_tulik:
        m = re.search(r"брон\s+([\d\.\s]+)", tl)
        if m:
            v = parse_amount(m.group(1).strip().split()[0])
            if v: paid = v
        status = "Брон"
    elif has_tulandy:
        m = re.search(r"([\d\.]+)\s*(?:туланди|толов|tolov)", tl)
        if m:
            v = parse_amount(m.group(1))
            if v: paid = v
        m2 = re.search(r"(\d+)\s*минг\s*(?:толов|tolov)", tl)
        if m2: paid = int(m2.group(1)) * 1000
        status = "Брон"

    if paid == 0 and is_tulik: paid = CONTRACT_SUM

    return {
        "fio": fio, "phone": phone, "username": username,
        "contact": contact, "paid": paid, "status": status,
    }

def detect_manager(sender_name):
    sl = sender_name.lower()
    for key, display in MANAGERS.items():
        if key in sl: return display
    return sender_name

def find_in_contacts(contacts_sheet, fio, phone, username):
    """
    Ищет контакт в листе Контакты по любому из полей:
    - совпадение телефона (если есть)
    - совпадение @username (если есть)
    - совпадение ФИО (как запасной вариант)
    Возвращает (row_index, existing_record) или (None, None)
    """
    all_rows = contacts_sheet.get_all_values()
    phone_digits   = normalize_phone(phone)
    username_lower = username.lower() if username else ""
    fio_lower      = fio.strip().lower()

    for i, row in enumerate(all_rows[1:], start=2):
        row_fio      = row[COL_C_FIO - 1].strip().lower()      if len(row) >= COL_C_FIO      else ""
        row_phone    = normalize_phone(row[COL_C_PHONE - 1])    if len(row) >= COL_C_PHONE    else ""
        row_username = row[COL_C_USERNAME - 1].strip().lower()  if len(row) >= COL_C_USERNAME else ""

        # Совпадение по телефону
        if phone_digits and row_phone and phone_digits == row_phone:
            return i, row
        # Совпадение по @username
        if username_lower and row_username and username_lower == row_username:
            return i, row
        # Совпадение по ФИО (только если нет телефона и юзернейма в обеих записях)
        if fio_lower == row_fio and not phone_digits and not row_phone and not username_lower and not row_username:
            return i, row

    return None, None

def update_contact(contacts_sheet, row_index, phone, username):
    """Дополняет существующий контакт новыми данными если их не было."""
    row = contacts_sheet.row_values(row_index)
    row_phone    = row[COL_C_PHONE - 1].strip()    if len(row) >= COL_C_PHONE    else ""
    row_username = row[COL_C_USERNAME - 1].strip() if len(row) >= COL_C_USERNAME else ""

    updated = False
    if phone and not row_phone:
        contacts_sheet.update_cell(row_index, COL_C_PHONE, phone)
        updated = True
    if username and not row_username:
        contacts_sheet.update_cell(row_index, COL_C_USERNAME, username)
        updated = True

    return updated

def add_contact(contacts_sheet, fio, phone, username, now):
    """Добавляет новый контакт в лист Контакты."""
    contacts_sheet.append_row([fio, phone, username, now, "Приход"], value_input_option="USER_ENTERED")

def find_original_in_prikhod(prikhod_sheet, fio, phone, username):
    """Ищет оригинальную строку в листе Приход для пометки дубля."""
    all_rows = prikhod_sheet.get_all_values()
    phone_digits   = normalize_phone(phone)
    username_lower = username.lower() if username else ""
    fio_lower      = fio.strip().lower()

    for i, row in enumerate(all_rows[1:], start=2):
        row_fio     = row[COL_P_FIO - 1].strip().lower()     if len(row) >= COL_P_FIO     else ""
        row_contact = row[COL_P_CONTACT - 1].strip()         if len(row) >= COL_P_CONTACT else ""
        row_phone   = normalize_phone(row_contact)
        row_uname   = ""
        for part in row_contact.split("/"):
            part = part.strip()
            if part.startswith("@"): row_uname = part.lower()

        if row_fio != fio_lower: continue
        if phone_digits and row_phone and phone_digits == row_phone: return i, row
        if username_lower and row_uname and username_lower == row_uname: return i, row
        if not phone_digits and not row_phone: return i, row

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
        logger.info("Не распознано — пропускаю")
        return

    logger.info(f"Распознан чек: {data}")
    manager = detect_manager(sender_name)
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    try:
        spreadsheet  = get_spreadsheet()
        prikhod      = spreadsheet.worksheet(SHEET_NAME)
        contacts_sh  = spreadsheet.worksheet(CONTACTS_SHEET)

        # Ищем в базе контактов
        dup_contact_row, existing = find_in_contacts(
            contacts_sh, data["fio"], data["phone"], data["username"]
        )

        if dup_contact_row is not None:
            # ── ДУБЛЬ ──
            logger.info(f"Дубль найден в Контактах строка {dup_contact_row}")

            # Дополняем контакт новыми данными (телефон/юзернейм)
            was_updated = update_contact(contacts_sh, dup_contact_row, data["phone"], data["username"])
            if was_updated:
                logger.info("Контакт дополнен новыми данными")

            # Помечаем оригинал в Приходе
            orig_row, _ = find_original_in_prikhod(
                prikhod, data["fio"], data["phone"], data["username"]
            )
            original_date = existing[COL_C_DATE - 1] if len(existing) >= COL_C_DATE else ""

            if orig_row:
                old_note = prikhod.cell(orig_row, COL_P_NOTE).value or ""
                new_note = f"{old_note} | Дубль чек: {now}".strip(" |")
                prikhod.update_cell(orig_row, COL_P_NOTE, new_note)
                logger.info(f"Оригинал помечен в строке {orig_row}")

            await msg.reply_text(
                f"⚠️ Дубль!\n"
                f"👤 {data['fio']} уже есть в базе контактов\n"
                f"📅 Первый чек: {original_date or '—'}\n"
                f"Новая строка не добавлена — отметка поставлена в оригинале."
                + (f"\n🔗 Контакт дополнен новыми данными." if was_updated else "")
            )

        else:
            # ── НОВЫЙ КЛИЕНТ ──
            # 1. Добавляем в Контакты
            add_contact(contacts_sh, data["fio"], data["phone"], data["username"], now)
            logger.info(f"Добавлен в Контакты: {data['fio']}")

            # 2. Добавляем в Приход
            all_rows  = prikhod.get_all_values()
            next_row  = len(all_rows) + 1
            client_num = len(all_rows)
            formula   = f"=G{next_row}-H{next_row}"

            row = [
                client_num, now, manager, data["fio"], data["contact"],
                TARIFF, CONTRACT_SUM, data["paid"], formula, data["status"], "",
            ]
            prikhod.append_row(row, value_input_option="USER_ENTERED")
            logger.info(f"Записано в Приход строка {next_row}: {data['fio']}")

            status_emoji = "✅" if data["status"] == "Тулик" else "🕐"
            await msg.reply_text(
                f"{status_emoji} Записано:\n"
                f"👤 {data['fio']}\n"
                f"📞 {data['contact'] or '—'}\n"
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
