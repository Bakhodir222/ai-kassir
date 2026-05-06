"""
Запускается один раз — создаёт лист "Контакты" и переносит туда
все ФИО/телефоны/юзернеймы из листа "Приход".
"""
import os
import re
import json
import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
PRIKHOD_SHEET  = os.environ.get("SHEET_NAME", "Приход")
CONTACTS_SHEET = "Контакты"

# Столбцы в листе Приход (1-based, с учётом нового формата со столбцом №)
# A=№  B=Дата  C=Менеджер  D=ФИО  E=Телефон/Username
COL_FIO     = 4   # D
COL_CONTACT = 5   # E

def get_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_info = json.loads(os.environ["GOOGLE_CREDS_JSON"])
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    return gspread.authorize(creds)

def split_contact(contact_str):
    """Разбивает '998901234567 / @username' на телефон и юзернейм."""
    phone = ""
    username = ""
    parts = [p.strip() for p in contact_str.split("/")]
    for part in parts:
        if part.startswith("@"):
            username = part
        elif re.sub(r"[^\d]", "", part):
            phone = part
    return phone, username

def main():
    client = get_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    # Читаем лист Приход
    prikhod = spreadsheet.worksheet(PRIKHOD_SHEET)
    all_rows = prikhod.get_all_values()
    print(f"Строк в Приходе: {len(all_rows)} (включая заголовок)")

    # Собираем уникальные контакты
    # Ключ = нормализованный телефон (если есть) или ФИО
    contacts = {}  # key -> {fio, phone, username}

    for row in all_rows[1:]:  # пропускаем заголовок
        if len(row) < COL_FIO:
            continue
        fio = row[COL_FIO - 1].strip()
        contact_str = row[COL_CONTACT - 1].strip() if len(row) >= COL_CONTACT else ""

        if not fio:
            continue

        phone, username = split_contact(contact_str)
        phone_digits = re.sub(r"[^\d]", "", phone)

        # Ключ для дедупликации
        key = phone_digits if phone_digits else fio.lower()

        if key not in contacts:
            contacts[key] = {"fio": fio, "phone": phone, "username": username}
        else:
            # Дополняем существующую запись если чего-то не хватало
            existing = contacts[key]
            if not existing["phone"] and phone:
                existing["phone"] = phone
            if not existing["username"] and username:
                existing["username"] = username
            # Берём более полное ФИО (длиннее)
            if len(fio) > len(existing["fio"]):
                existing["fio"] = fio

    print(f"Уникальных контактов: {len(contacts)}")

    # Создаём или очищаем лист Контакты
    try:
        contacts_sheet = spreadsheet.worksheet(CONTACTS_SHEET)
        contacts_sheet.clear()
        print("Лист 'Контакты' очищен")
    except gspread.WorksheetNotFound:
        contacts_sheet = spreadsheet.add_worksheet(
            title=CONTACTS_SHEET, rows=1000, cols=5
        )
        print("Лист 'Контакты' создан")

    # Заголовок
    header = ["ФИО", "Телефон", "@Username", "Дата добавления", "Источник"]
    contacts_sheet.append_row(header)

    # Записываем контакты
    rows_to_insert = []
    for c in contacts.values():
        rows_to_insert.append([
            c["fio"],
            c["phone"],
            c["username"],
            "",           # дата — пустая для старых
            "Приход",     # источник
        ])

    if rows_to_insert:
        contacts_sheet.append_rows(rows_to_insert, value_input_option="USER_ENTERED")

    print(f"Записано {len(rows_to_insert)} контактов в лист 'Контакты'")
    print("Готово!")

if __name__ == "__main__":
    main()
