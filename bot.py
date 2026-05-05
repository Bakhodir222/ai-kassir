import logging
import re
import os
import json
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import gspread
from google.oauth2.service_account import Credentials

# ─── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Config (из переменных окружения) ──────────────────────────────────────
BOT_TOKEN        = os.environ["BOT_TOKEN"]
SPREADSHEET_ID   = os.environ["SPREADSHEET_ID"]   # ID Google таблицы
SHEET_NAME       = os.environ.get("SHEET_NAME", "Приход")
GROUP_CHAT_ID    = int(os.environ["GROUP_CHAT_ID"])  # ID группы (отрицательное число)

# Имена менеджеров → как записываем в таблицу
# Ключ = часть Telegram username или display name (в нижнем регистре)
MANAGERS = {
    "aziza":   "Азиза",
    "zilola":  "Зилола",
    "dilnoza": "Дилноза",
}

TARIFF       = "Стандарт"
CONTRACT_SUM = 500_000

# ─── Google Sheets ──────────────────────────────────────────────────────────
def get_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_info = json.loads(os.environ["GOOGLE_CREDS_JSON"])
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)


def append_row(row: list):
    """Добавляет строку в конец таблицы."""
    sheet = get_sheet()
    sheet.append_row(row, value_input_option="USER_ENTERED")


def get_all_fios() -> list:
    """Возвращает все ФИО из таблицы для проверки дублей."""
    sheet = get_sheet()
    values = sheet.col_values(3)  # столбец C = ФИО
    return [v.strip().lower() for v in values[1:] if v.strip()]  # пропускаем заголовок


# ─── Парсер сообщения ───────────────────────────────────────────────────────
def is_phone(s: str) -> bool:
    clean = re.sub(r"[\s\-\(\)\+\.]", "", s)
    return bool(re.match(r"^\d{9,15}$", clean))


def parse_amount(s: str):
    s = s.strip().replace(" ", "").rstrip(".")
    if not s:
        return None
    if re.match(r"^\d+\.\d{3}$", s):
        return int(s.replace(".", ""))
    clean = re.sub(r"[^\d]", "", s)
    if not clean:
        return None
    v = int(clean)
    return v * 1000 if 0 < v < 1000 else v


def parse_message(text: str):
    """
    Парсит текст сообщения и возвращает словарь с данными клиента.
    Возвращает None если сообщение не похоже на чек.
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    tl = text.lower()

    # Проверяем ключевые слова оплаты
    PAYMENT_KEYWORDS = [
        "тулик", "тўлик", "брон", "туланди", "колди",
        "толик", "toliq", "tolov", "толов", "минг"
    ]
    if not any(kw in tl for kw in PAYMENT_KEYWORDS):
        return None

    # Пропускаем системные сообщения Alif и служебные тексты
    SKIP_PHRASES = [
        "alif:", "pul o'tkazmasi", "тулик туламаганла",
        "тулик ёзипсизде", "assalomu", "xaqiqiy chek",
        "ochirildi", "profilimga", "togirlab"
    ]
    if any(skip in tl for skip in SKIP_PHRASES):
        return None

    # Извлекаем ФИО (первая строка без телефона/хэндла/ключевых слов)
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
        if re.match(r"^\d{4}$", line):  # год
            continue
        if not fio:
            fio = line
    fio = re.sub(r"\s+\d{9,15}$", "", fio).strip()

    if not fio:
        return None

    # Определяем статус и суммы
    paid = 0
    status = "Тулик"

    is_tulik  = any(kw in tl for kw in ["тулик", "тўлик", "толик", "toliq"])
    has_koldi = any(kw in tl for kw in ["колди", "qoldi"])
    has_bron  = "брон" in tl
    has_tulandy = any(kw in tl for kw in ["туланди", "толов", "tolov"])

    if is_tulik and not has_koldi and not has_bron:
        paid = CONTRACT_SUM
        status = "Тулик"

    elif is_tulik and has_koldi:
        # "200+300=500 тулик" — полная оплата частями
        paid = CONTRACT_SUM
        status = "Тулик"

    elif has_bron and is_tulik and not has_tulandy:
        # "Брон 200+300 тулик булди"
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
        # "100минг толов"
        m2 = re.search(r"(\d+)\s*минг\s*(?:толов|tolov)", tl)
        if m2:
            paid = int(m2.group(1)) * 1000
        status = "Брон"

    if paid == 0 and is_tulik:
        paid = CONTRACT_SUM

    return {
        "fio":    fio,
        "phone":  phone,
        "paid":   paid,
        "status": status,
    }


def detect_manager(sender_name: str) -> str:
    """Определяет менеджера по имени отправителя."""
    sl = sender_name.lower()
    for key, display in MANAGERS.items():
        if key in sl:
            return display
    return sender_name  # если не нашли — пишем как есть


def check_duplicate(fio: str, existing_fios: list) -> bool:
    return fio.strip().lower() in existing_fios


# ─── Обработчик сообщений ───────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat

    # Реагируем только на нашу группу
    if chat.id != GROUP_CHAT_ID:
        return

    # Определяем текст — из самого сообщения или из пересланного
    text = ""
    sender_name = ""

    if msg.forward_origin or msg.forward_from:
        # Пересланное сообщение
        text = msg.text or msg.caption or ""
        # Менеджер = тот кто переслал (реальный отправитель в группе)
        sender = msg.from_user
        sender_name = f"{sender.first_name or ''} {sender.last_name or ''}".strip()
    else:
        text = msg.text or msg.caption or ""
        sender = msg.from_user
        sender_name = f"{sender.first_name or ''} {sender.last_name or ''}".strip()

    if not text:
        return

    # Парсим
    data = parse_message(text)
    if data is None:
        return  # не похоже на чек — молчим

    manager = detect_manager(sender_name)
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    # Проверка дубля
    note = ""
    try:
        existing = get_all_fios()
        if check_duplicate(data["fio"], existing):
            # Ищем дату первой записи
            sheet = get_sheet()
            all_rows = sheet.get_all_values()
            first_date = ""
            for row in all_rows[1:]:
                if len(row) >= 3 and row[2].strip().lower() == data["fio"].strip().lower():
                    first_date = row[0]
                    break
            note = f"Дубль (первый чек: {first_date})"
    except Exception as e:
        logger.warning(f"Ошибка при проверке дублей: {e}")

    # Формируем строку для таблицы
    # A=Дата B=Менеджер C=ФИО D=Телефон E=Тариф F=Сумма G=Оплата H=Остаток I=Статус J=Примечание
    # Остаток будет вычислен через формулу =F{row}-G{row}
    try:
        sheet = get_sheet()
        next_row = len(sheet.get_all_values()) + 1
        formula_remainder = f"=F{next_row}-G{next_row}"

        row = [
            now,
            manager,
            data["fio"],
            data["phone"],
            TARIFF,
            CONTRACT_SUM,
            data["paid"],
            formula_remainder,
            data["status"],
            note,
        ]
        append_row(row)

        # Подтверждение в группу
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


async def handle_unrecognized(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Этот хэндлер НЕ используется — бот молчит на нераспознанные."""
    pass


# ─── Запуск ─────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_message))
    logger.info("Бот запущен. Слушаю группу...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
