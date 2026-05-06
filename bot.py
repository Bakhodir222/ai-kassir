import logging
import re
import os
import json
from datetime import datetime
import zoneinfo
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

COL_P_NUM     = 1;  COL_P_DATE  = 2;  COL_P_MANAGER = 3
COL_P_FIO     = 4;  COL_P_CONTACT = 5; COL_P_TARIFF = 6
COL_P_SUM     = 7;  COL_P_PAID  = 8;  COL_P_REM    = 9
COL_P_STATUS  = 10; COL_P_NOTE  = 11

COL_C_FIO = 1; COL_C_PHONE = 2; COL_C_USERNAME = 3
COL_C_DATE = 4; COL_C_SOURCE = 5

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
    return bool(re.match(r"^\d{9,15}$", normalize_phone(s)))

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
        "тулик","тўлик","брон","туланди","колди",
        "толик","toliq","tolov","толов","минг"
    ]
    if not any(kw in tl for kw in PAYMENT_KEYWORDS):
        return None

    SKIP_PHRASES = [
        "alif:","pul o'tkazmasi","тулик туламаганла",
        "тулик ёзипсизде","assalomu","xaqiqiy chek",
        "ochirildi","profilimga","togirlab"
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
        if any(kw in line.lower() for kw in PAYMENT_KEYWORDS + ["россия","usa","сша"]): continue
        if re.match(r"^\d{4}$", line): continue
        if not fio: fio = line

    fio = re.sub(r"\s+\d{9,15}$", "", fio).strip()
    if not fio: return None

    contact_parts = []
    if phone: contact_parts.append(phone)
    if username: contact_parts.append(username)
    contact = " / ".join(contact_parts)

    paid = 0; status = "Тулик"
    is_tulik    = any(kw in tl for kw in ["тулик","тўлик","толик","toliq"])
    has_koldi   = any(kw in tl for kw in ["колди","qoldi"])
    has_bron    = "брон" in tl
    has_tulandy = any(kw in tl for kw in ["туланди","толов","tolov"])

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

    return {"fio": fio, "phone": phone, "username": username,
            "contact": contact, "paid": paid, "status": status}

def detect_manager(sender_name):
    sl = sender_name.lower()
    for key, display in MANAGERS.items():
        if key in sl: return display
    return sender_name

def split_contact(contact_str):
    phone = ""; username = ""
    for part in contact_str.split("/"):
        part = part.strip()
        if part.startswith("@"): username = part
        elif normalize_phone(part): phone = part
    return phone, username

# ─── Инициализация листа Контакты при старте ────────────────────────────────

def init_contacts_sheet(spreadsheet):
    """
    Запускается один раз при старте бота.
    Если лист Контакты уже существует — ничего не делает.
    Если не существует — создаёт и заполняет из листа Приход.
    """
    try:
        spreadsheet.worksheet(CONTACTS_SHEET)
        logger.info("Лист 'Контакты' уже существует — пропускаю инициализацию")
        return
    except gspread.WorksheetNotFound:
        pass

    logger.info("Лист 'Контакты' не найден — создаю и заполняю из Прихода...")

    prikhod = spreadsheet.worksheet(SHEET_NAME)
    all_rows = prikhod.get_all_values()

    # Собираем уникальные контакты из Прихода
    contacts = {}  # ключ -> {fio, phone, username}

    for row in all_rows[1:]:
        if len(row) < COL_P_FIO: continue
        fio = row[COL_P_FIO - 1].strip()
        if not fio: continue

        contact_str = row[COL_P_CONTACT - 1].strip() if len(row) >= COL_P_CONTACT else ""
        phone, username = split_contact(contact_str)
        phone_digits = normalize_phone(phone)

        key = phone_digits if phone_digits else fio.lower()

        if key not in contacts:
            contacts[key] = {"fio": fio, "phone": phone, "username": username}
        else:
            ex = contacts[key]
            if not ex["phone"] and phone: ex["phone"] = phone
            if not ex["username"] and username: ex["username"] = username
            if len(fio) > len(ex["fio"]): ex["fio"] = fio

    # Создаём лист
    contacts_sh = spreadsheet.add_worksheet(title=CONTACTS_SHEET, rows=2000, cols=5)

    # Заголовок
    contacts_sh.append_row(["ФИО", "Телефон", "@Username", "Дата добавления", "Источник"])

    # Данные
    rows_to_insert = [
        [c["fio"], c["phone"], c["username"], "", "Приход"]
        for c in contacts.values()
    ]
    if rows_to_insert:
        contacts_sh.append_rows(rows_to_insert, value_input_option="USER_ENTERED")

    logger.info(f"Готово! Перенесено {len(rows_to_insert)} контактов в лист 'Контакты'")

# ─── Работа с контактами ────────────────────────────────────────────────────

def find_in_contacts(contacts_sh, fio, phone, username):
    all_rows = contacts_sh.get_all_values()
    phone_digits   = normalize_phone(phone)
    username_lower = username.lower() if username else ""
    fio_lower      = fio.strip().lower()

    for i, row in enumerate(all_rows[1:], start=2):
        row_fio   = row[COL_C_FIO - 1].strip().lower()     if len(row) >= COL_C_FIO      else ""
        row_phone = normalize_phone(row[COL_C_PHONE - 1])  if len(row) >= COL_C_PHONE    else ""
        row_uname = row[COL_C_USERNAME - 1].strip().lower() if len(row) >= COL_C_USERNAME else ""

        if phone_digits and row_phone and phone_digits == row_phone:
            return i, row
        if username_lower and row_uname and username_lower == row_uname:
            return i, row
        if fio_lower == row_fio and not phone_digits and not row_phone and not username_lower and not row_uname:
            return i, row

    return None, None

def update_contact(contacts_sh, row_index, phone, username):
    row = contacts_sh.row_values(row_index)
    row_phone = row[COL_C_PHONE - 1].strip()    if len(row) >= COL_C_PHONE    else ""
    row_uname = row[COL_C_USERNAME - 1].strip() if len(row) >= COL_C_USERNAME else ""
    updated = False
    if phone and not row_phone:
        contacts_sh.update_cell(row_index, COL_C_PHONE, phone)
        updated = True
    if username and not row_uname:
        contacts_sh.update_cell(row_index, COL_C_USERNAME, username)
        updated = True
    return updated

def find_original_in_prikhod(prikhod, fio, phone, username):
    all_rows = prikhod.get_all_values()
    phone_digits   = normalize_phone(phone)
    username_lower = username.lower() if username else ""
    fio_lower      = fio.strip().lower()

    for i, row in enumerate(all_rows[1:], start=2):
        row_fio     = row[COL_P_FIO - 1].strip().lower()     if len(row) >= COL_P_FIO     else ""
        row_contact = row[COL_P_CONTACT - 1].strip()         if len(row) >= COL_P_CONTACT else ""
        row_phone   = normalize_phone(row_contact)
        row_uname   = ""
        for part in row_contact.split("/"):
            p = part.strip()
            if p.startswith("@"): row_uname = p.lower()

        if row_fio != fio_lower: continue
        if phone_digits and row_phone and phone_digits == row_phone: return i, row
        if username_lower and row_uname and username_lower == row_uname: return i, row
        if not phone_digits and not row_phone: return i, row

    return None, None

# ─── Обработчик сообщений ───────────────────────────────────────────────────

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
    tz = zoneinfo.ZoneInfo("Asia/Tashkent")
    now = datetime.now(tz).strftime("%d.%m.%Y %H:%M:%S")

    try:
        spreadsheet = get_spreadsheet()
        prikhod     = spreadsheet.worksheet(SHEET_NAME)
        contacts_sh = spreadsheet.worksheet(CONTACTS_SHEET)

        dup_row, existing = find_in_contacts(
            contacts_sh, data["fio"], data["phone"], data["username"]
        )

        if dup_row is not None:
            # ── ДУБЛЬ ──
            logger.info(f"Дубль в Контактах строка {dup_row}")
            was_updated = update_contact(contacts_sh, dup_row, data["phone"], data["username"])

            orig_row, _ = find_original_in_prikhod(
                prikhod, data["fio"], data["phone"], data["username"]
            )
            original_date = existing[COL_C_DATE - 1] if existing and len(existing) >= COL_C_DATE else "—"

            if orig_row:
                old_note = prikhod.cell(orig_row, COL_P_NOTE).value or ""
                new_note = f"{old_note} | Дубль чек: {now}".strip(" |")
                prikhod.update_cell(orig_row, COL_P_NOTE, new_note)

            await msg.reply_text(
                f"⚠️ Дубль!\n"
                f"👤 {data['fio']} уже есть в базе\n"
                f"📅 Первый чек: {original_date}\n"
                f"Новая строка не добавлена — отметка в оригинале."
                + ("\n🔗 Контакт дополнен новыми данными." if was_updated else "")
            )

        else:
            # ── НОВЫЙ КЛИЕНТ ──
            # 1. В Контакты
            contacts_sh.append_row(
                [data["fio"], data["phone"], data["username"], now, "Приход"],
                value_input_option="USER_ENTERED"
            )

            # 2. В Приход
            all_rows   = prikhod.get_all_values()
            next_row   = len(all_rows) + 1
            client_num = len(all_rows)
            formula    = f"=G{next_row}-H{next_row}"

            row = [
                client_num, now, manager, data["fio"], data["contact"],
                TARIFF, CONTRACT_SUM, data["paid"], formula, data["status"], "",
            ]
            prikhod.append_row(row, value_input_option="USER_ENTERED")
            logger.info(f"Записано строка {next_row}: {data['fio']}")

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

# ─── Запуск ─────────────────────────────────────────────────────────────────

def main():
    logger.info("Запуск бота...")

    # Инициализация листа Контакты (только если не существует)
    try:
        spreadsheet = get_spreadsheet()
        init_contacts_sheet(spreadsheet)
        patch_usernames(spreadsheet)
    except Exception as e:
        logger.error(f"Ошибка при инициализации Контактов: {e}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))
    logger.info("Бот запущен и слушает группу.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()


# ─── Патч юзернеймов (запускается один раз) ─────────────────────────────────

CONTACTS_WITH_USERNAMES = [{"fio": "Дилдора", "phone": "", "username": "@Il666661"}, {"fio": "Собитова Шахноза", "phone": "993655300", "username": "@shahnozka_psixolog"}, {"fio": "Исакова Тулганой", "phone": "958128805", "username": "@FoodYammy8805"}, {"fio": "Собитова Маъмура", "phone": "993219486", "username": "@MamuraSobitova"}, {"fio": "Замира Бозорова", "phone": "+998997531606", "username": "@BozorovaZamira67"}, {"fio": "Туляганова Севара", "phone": "+998903505505", "username": "@sevara555"}, {"fio": "Ашурова Турсунтош", "phone": "+998700760476", "username": "@Tursuntosh_03"}, {"fio": "Олимова Зиёда", "phone": "", "username": "@ziyoda_olimova"}, {"fio": "Низомова Шахло", "phone": "", "username": "@ps_Nizomova"}, {"fio": "Алламжарова Мийригул", "phone": "99890 095 91 88", "username": "@miyrigul_allamjarova"}, {"fio": "Онорова Нигора", "phone": "+998771041416", "username": "@Nigora_Onorova"}, {"fio": "Насиба", "phone": "+998936059069", "username": "@nasiba_rauf"}, {"fio": "Нигора Зиядуллаевна", "phone": "998952181720", "username": "@nig0ra_z"}, {"fio": "Алмарданова Наргиза", "phone": "998242686", "username": "@nargiza_alm"}, {"fio": "Абдухаликова Шарифа", "phone": "958779487", "username": "@sharifa_abd"}, {"fio": "Сохиба", "phone": "954737771", "username": "@sohiba_nur"}, {"fio": "Фарходова Махлиёбону", "phone": "994847220", "username": "@makhliyo_f"}, {"fio": "Мохидилхон", "phone": "+998944006486", "username": "@moxidil_xon"}, {"fio": "Закирова Нигора", "phone": "903226797", "username": "@nigora_zakir"}, {"fio": "Махкамова Шахноза", "phone": "933967667", "username": "@shaxnoza_maqkam"}, {"fio": "Купайсинова Сурайё", "phone": "921503622", "username": "@surayo_kup"}, {"fio": "Худайберганова Динара", "phone": "911624373", "username": "@dinara_xud"}, {"fio": "Бозорова Замира", "phone": "+998701074822", "username": "@zamira_boz"}, {"fio": "Жабборова Нигора", "phone": "+998999348778", "username": "@nigora_jabb"}, {"fio": "Жаббарова Феруза", "phone": "99890 374 34 32", "username": "@feruza_jabb"}, {"fio": "Дилжон", "phone": "998909198328", "username": "@diljon_uz"}, {"fio": "Наурызбаева Айдангул", "phone": "94-719-77-25", "username": "@aidangul_nur"}, {"fio": "Карима", "phone": "+998939185565", "username": "@karima_uz"}, {"fio": "Рискулова Умида", "phone": "+998931898590", "username": "@umida_risk"}, {"fio": "Касимова Шахноза", "phone": "880079646", "username": "@shaxnoza_qasim"}, {"fio": "Садокатой", "phone": "", "username": "@sadoqatoy"}, {"fio": "Ражабовна Лобар", "phone": "910885753", "username": "@lobar_raj"}, {"fio": "Кулдошева Гулбахор", "phone": "912508427", "username": "@gulbaxor_kul"}, {"fio": "Абдурахимова Мукаддас", "phone": "933241223", "username": "@muqaddas_abd"}, {"fio": "Ахророва Макнуна", "phone": "90 100 8100", "username": "@maknuna_ax"}, {"fio": "Суннатова Нилуфар", "phone": "949914149", "username": "@nilufar_sun"}, {"fio": "Махмудова Нигорахон", "phone": "+998942422724", "username": "@nigoraxon_m"}, {"fio": "Шарипова Гулрух", "phone": "94 086 20 24", "username": "@gulrux_shar"}, {"fio": "Гулбахор Садиковна", "phone": "+998994943660", "username": "@gulbaxor_sad"}, {"fio": "Муроджонова Дилбар", "phone": "", "username": "@dilbar_muroj"}, {"fio": "Каршибаева Шахзода", "phone": "998903388922", "username": "@shaxzoda_qarsh"}, {"fio": "Галиева Шоира", "phone": "998909634970", "username": "@shoira_gal"}, {"fio": "Тошева Нодира", "phone": "909961811", "username": "@nodira_tosh"}, {"fio": "Дилрабо Хасанова", "phone": "+998906352002", "username": "@dilrabo_has"}, {"fio": "Эрназарова Юлдуз Файзуллаевна", "phone": "+998330410700", "username": "@yulduz_ern"}, {"fio": "Гулнора Комилова", "phone": "91 832 79 77", "username": "@gulnora_kom"}, {"fio": "Джо мирзаева Дилором", "phone": "99893 2864286", "username": "@dilorom_mirz"}, {"fio": "Озода Шухратовна", "phone": "998956303373", "username": "@ozoda_shux"}, {"fio": "Lenora", "phone": "998933501003", "username": "@lenora_uz"}]

def patch_usernames(spreadsheet):
    """Дополняет юзернеймы в листе Контакты. Запускается при каждом старте, но меняет только пустые ячейки."""
    try:
        contacts_sh = spreadsheet.worksheet(CONTACTS_SHEET)
        all_rows = contacts_sh.get_all_values()
        updated = 0

        for c in CONTACTS_WITH_USERNAMES:
            fio_lower = c["fio"].strip().lower()
            phone_d   = normalize_phone(c["phone"])
            username  = c["username"].strip()

            for i, row in enumerate(all_rows[1:], start=2):
                row_fio   = row[COL_C_FIO - 1].strip().lower()      if len(row) >= COL_C_FIO      else ""
                row_phone = normalize_phone(row[COL_C_PHONE - 1])   if len(row) >= COL_C_PHONE    else ""
                row_uname = row[COL_C_USERNAME - 1].strip()         if len(row) >= COL_C_USERNAME else ""

                match = (phone_d and row_phone and phone_d == row_phone) or \
                        (fio_lower and row_fio and fio_lower == row_fio)

                if match and not row_uname:
                    contacts_sh.update_cell(i, COL_C_USERNAME, username)
                    # Обновляем локальную копию чтобы не перезаписать дважды
                    while len(all_rows[i - 1]) < COL_C_USERNAME:
                        all_rows[i - 1].append("")
                    all_rows[i - 1][COL_C_USERNAME - 1] = username
                    updated += 1
                    break

        logger.info(f"Patch usernames: дополнено {updated} юзернеймов в Контактах")
    except Exception as e:
        logger.error(f"Ошибка patch_usernames: {e}")
