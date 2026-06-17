import logging
import re
import os
import json
import zoneinfo
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN       = os.environ["BOT_TOKEN"]
GOOGLE_CREDS    = os.environ["GOOGLE_CREDS_JSON"]
CONFIG_SHEET_ID = os.environ["CONFIG_SPREADSHEET_ID"]
TZ              = zoneinfo.ZoneInfo("Asia/Tashkent")

_config_cache: dict = {}
_config_loaded_at = None

def get_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS), scopes=scopes
    )
    return gspread.authorize(creds)

def load_config():
    global _config_cache, _config_loaded_at
    client = get_gspread_client()
    sh = client.open_by_key(CONFIG_SHEET_ID).worksheet("Конфиг")
    rows = sh.get_all_values()
    cfg = {}
    for row in rows[1:]:
        if len(row) < 5 or not row[0].strip(): continue
        try:
            group_id = int(row[0].strip())
        except ValueError:
            continue
        managers = {}
        for pair in row[4].split(","):
            pair = pair.strip()
            if ":" in pair:
                k, v = pair.split(":", 1)
                managers[k.strip().lower()] = v.strip()
        tariff_prices = {}
        if len(row) > 8 and row[8].strip():
            for pair in row[8].split(","):
                pair = pair.strip()
                if ":" in pair:
                    k, v = pair.split(":", 1)
                    try: tariff_prices[k.strip()] = int(v.strip())
                    except: pass
        cfg[group_id] = {
            "spreadsheet_id": row[1].strip(),
            "sheet_name":     row[2].strip() or "Приход",
            "contract_sum":   int(row[3].strip()) if row[3].strip() else 500000,
            "managers":       managers,
            "tariff":         row[5].strip() if len(row) > 5 else "Стандарт",
            "usd_rate":       int(row[7].strip()) if len(row) > 7 and row[7].strip() else 0,
            "tariff_prices":  tariff_prices,
        }
    _config_cache = cfg
    _config_loaded_at = datetime.now(TZ)
    logger.info(f"Конфиг загружен: {len(cfg)} групп: {list(cfg.keys())}")

def get_config(group_id: int):
    global _config_loaded_at
    if _config_loaded_at is None or (datetime.now(TZ) - _config_loaded_at).seconds > 3600:
        try:
            load_config()
        except Exception as e:
            logger.error(f"Ошибка загрузки конфига: {e}")
    return _config_cache.get(group_id)

processed_ids: set = set()
MAX_CACHE = 2000

PAYMENT_KEYWORDS = [
    "тулик","туллик","тўлик","брон","бронь","туланди","тулади","колди","колли",
    "толик","toliq","tolov","толов","минг","тулов","yana","яна","тўлов","to'liq"
]
SKIP_PHRASES = [
    "alif:","pul o'tkazmasi","assalomu","xaqiqiy chek","ochirildi","profilimga",
    "togirlab","tug'ilgan","muborak","таблицада","этибор","записано",
    "доплата принята","записана","bakhodir","ии-кассир","✅","⚠️","🕐"
]

def is_phone(s):
    return bool(re.match(r"^\d{9,15}$", re.sub(r"[\s\-\(\)\+\.]", "", s)))

def normalize_phone(s):
    return re.sub(r"[^\d]", "", s or "")

def parse_amount(s):
    s = str(s).strip().replace(" ", "").rstrip(".")
    if not s: return None
    if re.match(r"^\d+\.\d{3}$", s): return int(s.replace(".", ""))
    clean = re.sub(r"[^\d]", "", s)
    if not clean: return None
    v = int(clean)
    return v * 1000 if 0 < v < 1000 else v

TARIFF_MAP = {
    # key (substring) -> (display_name, contract_sum)
    # Checked in order — VIP first since it's higher value
    'вип':  ('ВИП',  10_800_000),
    'vip':  ('ВИП',  10_800_000),
    'про':  ('Про',   8_400_000),
    'pro':  ('Про',   8_400_000),
}

def detect_tariff(tl: str, cfg: dict):
    """
    Returns (tariff_name, contract_sum).
    If cfg has tariff_prices dict, uses it. Otherwise uses defaults.
    Checks VIP before Про to avoid false match.
    """
    prices = cfg.get("tariff_prices", {})  # optional override: {"ВИП": 10800000, "Про": 8400000}
    for key, (name, default_sum) in TARIFF_MAP.items():
        if key in tl:
            return name, prices.get(name, default_sum)
    # Fallback to config default
    return cfg.get("tariff", "Стандарт"), cfg.get("contract_sum", 500_000)

def parse_amount_usd(s: str, usd_rate: int) -> int | None:
    """Parse amounts like '100$' or '$100' → сум"""
    m = re.match(r'(\d+)\s*\$', s.strip())
    if not m: m = re.match(r'\$\s*(\d+)', s.strip())
    if m: return int(m.group(1)) * usd_rate
    return None

def parse_amount(s):
    if not s: return None
    s = str(s).strip()
    # "8млн350" → 8350000
    m = re.match(r'(\d+)млн(\d*)', s.lower())
    if m:
        return int(m.group(1)) * 1_000_000 + (int(m.group(2)) * 1000 if m.group(2) else 0)
    s = s.replace(" ", "").rstrip(".")
    # 8.400.000 → 8400000
    if re.match(r'^\d+\.\d{3}\.\d{3}$', s): return int(s.replace(".", ""))
    # 8.300.000 (typo with extra zero) handled above
    # 8300.000 → 8300000 OR 8.300 → 8300000
    if re.match(r'^\d+\.\d{3}$', s):
        v = int(s.replace(".", ""))
        return v * 1000 if v < 10000 else v
    clean = re.sub(r"[^\d]", "", s)
    if not clean: return None
    v = int(clean)
    return v * 1000 if 100 <= v <= 9999 else v

def parse_message(text: str, cfg: dict):
    """
    Parse a payment message. cfg contains contract_sum, tariff_prices, usd_rate etc.
    Returns dict with fio, phone, username, contact, tariff, contract_sum, paid, status.
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    tl = text.lower()
    if not any(kw in tl for kw in PAYMENT_KEYWORDS): return None
    if any(skip in tl for skip in SKIP_PHRASES): return None

    usd_rate = cfg.get("usd_rate", 0)  # 0 means USD not supported

    fio = ""; phone = ""; username = ""
    skip_line_words = [
        'брон','бронь','колди','колдик','колдм','тариф','апгрейд','накт','стандарт','про','вип',
        'pro','vip','upgrade','тулик','туланди','тулади','тулов','скидка','учун','сум','http',
        'click','amalga','tolov','oshirildi','нахт','туланди','бн'
    ]
    for line in lines:
        if line.startswith("@") and len(line) > 1:
            if not username: username = line.strip()
            continue
        if is_phone(line):
            if not phone: phone = line.strip()
            continue
        ll = line.lower()
        if any(w in ll for w in skip_line_words + PAYMENT_KEYWORDS): continue
        if re.match(r'^[\d\s\.\+\-\(\)\$]+$', line): continue
        if not fio: fio = line
    fio = re.sub(r"\s+\d{9,15}$", "", fio).strip()
    if not fio: return None

    contact = " / ".join(filter(None, [phone, username]))

    # Detect tariff and contract sum from text
    tariff, contract_sum = detect_tariff(tl, cfg)

    paid = 0; status = "Брон"
    is_tulik    = any(kw in tl for kw in ["тулик","туллик","тўлик","толик","toliq","to'liq"])
    has_koldi   = any(kw in tl for kw in ["колди","qoldi","колли","колдик","колдм"])
    has_bron    = any(kw in tl for kw in ["брон","бронь"])
    has_tulandy = any(kw in tl for kw in ["туланди","тулади","толов","tolov","тулов"])
    has_yana    = "яна" in tl or "yana" in tl

    if is_tulik and not has_koldi:
        paid = contract_sum; status = "Тулик"
    elif has_koldi:
        # колди = remaining balance → paid = contract - remaining
        # Pattern: "X колди Y" or "колди Y" or "яна колди Y"
        koldi_m = re.search(r'(?:яна\s+)?колди[мк]?\s*([\d\s\.\$млн]+)', tl)
        if not koldi_m:
            koldi_m = re.search(r'([\d\s\.\$млн]+)\s*колди', tl)
        if koldi_m:
            raw = koldi_m.group(1).strip()
            # Try USD
            remaining = None
            if '$' in raw and usd_rate:
                usd_m = re.search(r'(\d+)\s*\$', raw)
                if usd_m: remaining = int(usd_m.group(1)) * usd_rate
            if remaining is None:
                remaining = parse_amount(raw)
            if remaining and remaining > 0:
                paid = max(0, contract_sum - remaining)

        # Also look for explicit bron/tulandy amount before koldi
        if paid == 0:
            bron_m = re.search(r'(?:брон|бронь)[^\d]*([\d\s\.\+\$]+?)(?:\s*(?:яна|колд|апгрейд|тариф|$))', tl)
            if bron_m:
                raw = bron_m.group(1).strip()
                nums = re.findall(r'[\d\.]+', raw)
                total = sum(parse_amount(n) or 0 for n in nums if parse_amount(n) and parse_amount(n) < contract_sum * 2)
                if total: paid = total
            tulandy_m = re.search(r'([\d\s\.\+\$млн]+?)\s*(?:туланди|тулади|туланди)', tl)
            if tulandy_m and not paid:
                v = parse_amount(tulandy_m.group(1).strip().split()[-1])
                if v: paid = v

        status = "Тулик" if paid >= contract_sum else "Брон"
    elif has_bron and not is_tulik:
        bron_m = re.search(r'(?:брон|бронь)[^\d]*([\d\s\.\+]+?)(?:\s*(?:яна|колд|$))', tl)
        if bron_m:
            nums = re.findall(r'[\d\.]+', bron_m.group(1))
            total = sum(parse_amount(n) or 0 for n in nums if parse_amount(n) and parse_amount(n) < contract_sum * 2)
            if total: paid = total
        if not paid:
            m2 = re.search(r'(?:брон|бронь)[^\d]*([\d\s\.]+)', tl)
            if m2:
                v = parse_amount(m2.group(1).strip().split()[0])
                if v: paid = v
        status = "Брон"
    elif has_tulandy:
        m = re.search(r"([\d\.]+)\s*(?:туланди|тулади|толов|tolov|тулов)", tl)
        if m:
            v = parse_amount(m.group(1))
            if v: paid = v
        status = "Тулик" if paid >= contract_sum else "Брон"

    # USD in paid amount
    if paid == 0 and usd_rate and '$' in tl:
        usd_m = re.search(r'(\d+)\s*\$\s*(?:туланди|тулади|брон|нахт)', tl)
        if usd_m: paid = int(usd_m.group(1)) * usd_rate

    if paid == 0 and is_tulik: paid = contract_sum; status = "Тулик"

    return {"fio": fio, "phone": phone, "username": username,
            "contact": contact, "tariff": tariff,
            "contract_sum": contract_sum, "paid": paid, "status": status}

def detect_manager(sender_name: str, managers: dict) -> str:
    sl = sender_name.lower()
    for key, display in managers.items():
        if key in sl: return display
    return sender_name

COL_NUM=1; COL_DATE=2; COL_MGR=3; COL_FIO=4; COL_CONTACT=5
COL_TARIFF=6; COL_SUM=7; COL_PAID=8; COL_REM=9; COL_STATUS=10; COL_NOTE=11
COL_C_FIO=1; COL_C_PHONE=2; COL_C_UNAME=3; COL_C_DATE=4; COL_C_SRC=5

def get_spreadsheet(cfg):
    return get_gspread_client().open_by_key(cfg["spreadsheet_id"])

def ensure_contacts_sheet(spreadsheet):
    try:
        return spreadsheet.worksheet("Контакты")
    except gspread.WorksheetNotFound:
        sh = spreadsheet.add_worksheet(title="Контакты", rows=2000, cols=5)
        sh.append_row(["ФИО","Телефон","@Username","Дата добавления","Источник"])
        return sh

def find_in_contacts(contacts_sh, fio, phone, username):
    all_rows = contacts_sh.get_all_values()
    pd = normalize_phone(phone); ul = username.lower() if username else ""; fl = fio.strip().lower()
    for i, row in enumerate(all_rows[1:], start=2):
        r_fio   = row[COL_C_FIO-1].strip().lower()   if len(row) >= COL_C_FIO   else ""
        r_phone = normalize_phone(row[COL_C_PHONE-1]) if len(row) >= COL_C_PHONE else ""
        r_uname = row[COL_C_UNAME-1].strip().lower() if len(row) >= COL_C_UNAME else ""
        if pd and r_phone and pd == r_phone: return i, row
        if ul and r_uname and ul == r_uname: return i, row
        if fl == r_fio and not pd and not r_phone and not ul and not r_uname: return i, row
    return None, None

def update_contact(contacts_sh, row_index, phone, username):
    row = contacts_sh.row_values(row_index)
    updated = False
    r_phone = row[COL_C_PHONE-1].strip() if len(row) >= COL_C_PHONE else ""
    r_uname = row[COL_C_UNAME-1].strip() if len(row) >= COL_C_UNAME else ""
    if phone and not r_phone:
        contacts_sh.update_cell(row_index, COL_C_PHONE, phone); updated = True
    if username and not r_uname:
        contacts_sh.update_cell(row_index, COL_C_UNAME, username); updated = True
    return updated

def find_in_prikhod(prikhod, fio, phone, username):
    all_rows = prikhod.get_all_values()
    pd = normalize_phone(phone); ul = username.lower() if username else ""; fl = fio.strip().lower()
    for i, row in enumerate(all_rows[1:], start=2):
        r_fio     = row[COL_FIO-1].strip().lower()     if len(row) >= COL_FIO     else ""
        r_contact = row[COL_CONTACT-1].strip()         if len(row) >= COL_CONTACT else ""
        r_phone   = normalize_phone(r_contact)
        r_uname   = next((p.strip().lower() for p in r_contact.split("/") if p.strip().startswith("@")), "")
        if r_fio != fl: continue
        if pd and r_phone and pd == r_phone: return i, row
        if ul and r_uname and ul == r_uname: return i, row
        if not pd and not r_phone: return i, row
    return None, None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.effective_message
    chat = update.effective_chat
    if not chat: return

    global processed_ids
    msg_id = msg.message_id
    if msg_id in processed_ids: return
    processed_ids.add(msg_id)
    if len(processed_ids) > MAX_CACHE:
        processed_ids = set(list(processed_ids)[-1000:])

    cfg = get_config(chat.id)
    if cfg is None: return

    text = msg.text or msg.caption or ""
    sender = msg.from_user
    sender_name = f"{sender.first_name or ''} {sender.last_name or ''}".strip()
    if not text: return

    logger.info(f"[{chat.id}] {sender_name}: {text[:60]}")

    data = parse_message(text, cfg)
    if data is None: return
    tariff       = data.get("tariff", cfg.get("tariff", "Стандарт"))
    contract_sum = data.get("contract_sum", cfg["contract_sum"])

    logger.info(f"Чек: {data}")
    manager = detect_manager(sender_name, cfg["managers"])
    now = datetime.now(TZ).strftime("%d.%m.%Y %H:%M:%S")

    try:
        spreadsheet = get_spreadsheet(cfg)
        prikhod     = spreadsheet.worksheet(cfg["sheet_name"])
        contacts_sh = ensure_contacts_sheet(spreadsheet)

        dup_row, existing = find_in_contacts(contacts_sh, data["fio"], data["phone"], data["username"])

        if dup_row is not None:
            was_updated = update_contact(contacts_sh, dup_row, data["phone"], data["username"])
            orig_row, orig_data = find_in_prikhod(prikhod, data["fio"], data["phone"], data["username"])
            orig_date   = existing[COL_C_DATE-1] if existing and len(existing) >= COL_C_DATE else "—"
            orig_status = orig_data[COL_STATUS-1].strip() if orig_data and len(orig_data) >= COL_STATUS else ""

            if orig_status == "Брон" and data["status"] == "Тулик":
                prikhod.update_cell(orig_row, COL_PAID,   contract_sum)
                prikhod.update_cell(orig_row, COL_STATUS, "Тулик")
                old_note = prikhod.cell(orig_row, COL_NOTE).value or ""
                prikhod.update_cell(orig_row, COL_NOTE, f"{old_note} | Доплата: {now}".strip(" |"))
                await msg.reply_text(
                    f"✅ Доплата принята!\n👤 {data['fio']}\n"
                    f"💰 Брон → Тулик | {contract_sum:,} сум"
                    + ("\n🔗 Контакт дополнен." if was_updated else "")
                )
            elif orig_status == "Брон" and data["status"] == "Брон" and data["paid"] > 0:
                try: old_paid = int(str(orig_data[COL_PAID-1]).replace(",","").replace(" ","") or 0)
                except: old_paid = 0
                new_paid   = min(old_paid + data["paid"], contract_sum)
                new_status = "Тулик" if new_paid >= contract_sum else "Брон"
                prikhod.update_cell(orig_row, COL_PAID,   new_paid)
                prikhod.update_cell(orig_row, COL_STATUS, new_status)
                old_note = prikhod.cell(orig_row, COL_NOTE).value or ""
                prikhod.update_cell(orig_row, COL_NOTE, f"{old_note} | Доплата {data['paid']:,}: {now}".strip(" |"))
                emoji = "✅" if new_status == "Тулик" else "🕐"
                await msg.reply_text(
                    f"{emoji} Доплата!\n👤 {data['fio']}\n"
                    f"💰 {old_paid:,} → {new_paid:,} | {new_status}"
                )
            else:
                if orig_row:
                    old_note = prikhod.cell(orig_row, COL_NOTE).value or ""
                    prikhod.update_cell(orig_row, COL_NOTE, f"{old_note} | Дубль чек: {now}".strip(" |"))
                await msg.reply_text(
                    f"⚠️ Дубль!\n👤 {data['fio']} уже есть в базе\n📅 Первый чек: {orig_date}"
                    + ("\n🔗 Контакт дополнен." if was_updated else "")
                )
        else:
            contacts_sh.append_row(
                [data["fio"], data["phone"], data["username"], now, "Приход"],
                value_input_option="USER_ENTERED"
            )
            all_rows = prikhod.get_all_values()
            next_row = len(all_rows) + 1
            prikhod.append_row([
                len(all_rows), now, manager, data["fio"], data["contact"],
                tariff, contract_sum, data["paid"],
                f"=G{next_row}-H{next_row}", data["status"], ""
            ], value_input_option="USER_ENTERED")
            emoji = "✅" if data["status"] == "Тулик" else "🕐"
            await msg.reply_text(
                f"{emoji} Записано:\n👤 {data['fio']}\n"
                f"📞 {data['contact'] or '—'}\n"
                f"💰 {data['paid']:,} сум | {data['status']}",
                parse_mode=None
            )
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await msg.reply_text(f"❌ Ошибка: {e}")

def main():
    logger.info("Запуск универсального бота...")
    try:
        load_config()
    except Exception as e:
        logger.error(f"Ошибка загрузки конфига: {e}")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))
    logger.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
