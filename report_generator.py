"""
report_generator.py — симулятор генератора отчётов (tkinter + Google Sheets).

Запрашивает период, подразделение и число строк, генерирует случайные данные
и записывает в Google Таблицу новый лист с оформленным отчётом:
шапка-«документ», таблица с границами и чередованием цветов, итог,
ширины колонок, закреплённая шапка.

Защита от ошибок:
- ВСЁ форматирование отправляется ОДНИМ пакетным запросом batchUpdate
  (методы *_request + batch_update из google_sheets.py) — это 2 запроса
  на запись вместо ~80, квота Google (60/мин) не превышается;
- если квота всё же превышена (429), операция автоматически повторяется
  через 61 секунду, статус виден в окне приложения;
- неверные даты и пустые поля перехватываются с понятным сообщением.
"""

import datetime as dt
import random
import re
import time
import tkinter as tk
from tkinter import messagebox, ttk

from google_sheets import GoogleSheets, find_service_account_key, load_env

# ------------------------------ конфигурация -------------------------------
ENV = load_env()
CREDENTIALS_PATH = ENV.get("CREDENTIALS_PATH") or find_service_account_key()
SPREADSHEET_ID = ENV.get("SPREADSHEET_ID", "")

CLIENTS = ["ООО «Вектор»", "ИП Смирнов А.А.", "АО «Титан»", "ООО «Северный ветер»",
           "ИП Козлова В.Н.", "ООО «Прогресс»", "ЗАО «Вега»", "ООО «Сириус»",
           "ИП Мельник Д.С.", "ООО «Аврора»"]
MANAGERS = ["Иванов И.", "Петрова М.", "Сидоров К.", "Волкова Е.", "Орлов Д."]
DEALS = ["Разработка сайта", "Рекламная кампания", "Техподдержка",
         "Подписка на ПО", "Консалтинг", "Поставка и монтаж"]
DEPARTMENTS = ["Продажи", "Логистика", "Маркетинг"]

# Палитра отчёта (R, G, B)
DARK_BLUE = (31, 56, 100)
BLUE = (68, 114, 196)
LIGHT_BLUE = (217, 226, 243)
GREEN = (198, 239, 206)
GRAY = (128, 128, 128)
WHITE = (255, 255, 255)
MONEY_FMT = '#,##0.00" ₽"'

RETRY_ATTEMPTS = 3          # сколько раз повторять операцию при ошибке 429
RETRY_PAUSE = 61            # секунд ожидания: квота Google сбрасывается каждую минуту


def parse_date(text: str) -> dt.date:
    """Понимает ДД.ММ.ГГГГ и ДД.ММ (год — текущий)."""
    text = text.strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    m = re.match(r"^(\d{1,2})\.(\d{1,2})$", text)
    if m:
        return dt.date(dt.date.today().year, int(m.group(2)), int(m.group(1)))
    raise ValueError(f"Не удалось разобрать дату: {text}")


def with_retry(fn, what: str, on_wait=None):
    """Выполняет операцию; при ошибке 429 (квота записей/мин) ждёт и повторяет."""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as e:
            err = str(e)
            if ("429" in err or "RATE_LIMIT" in err or "Quota" in err) \
                    and attempt < RETRY_ATTEMPTS:
                if on_wait:
                    on_wait()
                time.sleep(RETRY_PAUSE)
                continue
            raise Exception(f"{what}: {e}")


class ReportGeneratorApp:
    def __init__(self, root: tk.Tk, sheets: GoogleSheets):
        self.root = root
        self.sheets = sheets
        root.title("Генератор отчётов")
        root.geometry("440x340")
        root.resizable(False, False)

        pad = {"padx": 14, "pady": 3}
        tk.Label(root, text="Автоматический отчёт в Google Sheets",
                 font=("Segoe UI", 13, "bold")).pack(pady=(14, 8))

        today = dt.date.today()
        tk.Label(root, text="Дата начала отчёта (ДД.ММ.ГГГГ):").pack(anchor="w", **pad)
        self.start_var = tk.StringVar(value=today.replace(day=1).strftime("%d.%m.%Y"))
        ttk.Entry(root, textvariable=self.start_var).pack(**pad)

        tk.Label(root, text="Дата окончания отчёта (ДД.ММ.ГГГГ):").pack(anchor="w", **pad)
        self.end_var = tk.StringVar(value=today.strftime("%d.%m.%Y"))
        ttk.Entry(root, textvariable=self.end_var).pack(**pad)

        tk.Label(root, text="Подразделение:").pack(anchor="w", **pad)
        self.dept_var = tk.StringVar(value=DEPARTMENTS[0])
        ttk.Combobox(root, textvariable=self.dept_var, values=DEPARTMENTS,
                     state="readonly").pack(**pad)

        tk.Label(root, text="Строк в отчёте:").pack(anchor="w", **pad)
        self.count_var = tk.IntVar(value=7)
        ttk.Spinbox(root, from_=3, to=15, textvariable=self.count_var).pack(**pad)

        self.btn = ttk.Button(root, text="Сгенерировать отчёт", command=self.generate)
        self.btn.pack(pady=10)

        self.status_var = tk.StringVar(value="Готов к работе")
        tk.Label(root, textvariable=self.status_var, wraplength=410,
                 fg="gray").pack(padx=14)

    # ------------------------- генерация данных ----------------------------
    def _make_rows(self, start: dt.date, end: dt.date, n: int):
        rows, span = [], (end - start).days
        for _ in range(n):
            day = start + dt.timedelta(days=random.randint(0, max(span, 0)))
            rows.append({
                "client": random.choice(CLIENTS),
                "manager": random.choice(MANAGERS),
                "deal": random.choice(DEALS),
                "amount": round(random.uniform(15_000, 450_000), 2),
                "date": day.strftime("%d.%m.%Y"),
            })
        return rows

    # ------------------------------ кнопка ---------------------------------
    def generate(self):
        try:
            start = parse_date(self.start_var.get())
            end = parse_date(self.end_var.get())
            count = int(self.count_var.get())
        except (ValueError, tk.TclError) as e:
            messagebox.showerror("Ввод", f"Проверьте поля: {e}")
            return
        if start > end:
            start, end = end, start

        self.btn.config(state="disabled")
        self.status_var.set("Формирую и записываю отчёт… (занят 10–20 секунд)")
        self.root.update_idletasks()
        try:
            dept = self.dept_var.get()
            rows = self._make_rows(start, end, count)
            title = self.sheets.create_sheet(
                f"Отчёт {dept} {start:%d.%m}-{end:%d.%m}")
            self._write_report(title, start, end, dept, rows)
            self.status_var.set(f"Готово! Отчёт записан на лист «{title}»")
            messagebox.showinfo("Готово", f"Отчёт записан на лист\n«{title}»")
        except Exception as e:
            self.status_var.set("Ошибка при генерации")
            messagebox.showerror("Ошибка", str(e))
        finally:
            self.btn.config(state="normal")

    # --------------------------- запись отчёта -----------------------------
    def _write_report(self, title, start, end, dept, rows):
        s = self.sheets
        rng = lambda a1: f"'{title}'!{a1}"          # диапазоны в кавычках
        now = dt.datetime.now()
        n = len(rows)
        header, first_data = 5, 6
        last_data = first_data + n - 1
        total_row = last_data + 1

        block = [
            ["ОТЧЁТ О ПРОДАЖАХ", "", "", "", "", ""],
            [f"Период: {start:%d.%m.%Y} — {end:%d.%m.%Y}   ·   Подразделение: {dept}",
             "", "", "", "", ""],
            [f"Сформирован: {now:%d.%m.%Y %H:%M} · данные симулированы",
             "", "", "", "", ""],
            ["", "", "", "", "", ""],
            ["№", "Клиент", "Менеджер", "Сделка", "Сумма, ₽", "Дата сделки"],
        ]
        for i, r in enumerate(rows, start=1):
            block.append([i, r["client"], r["manager"], r["deal"],
                          r["amount"], r["date"]])
        block.append(["", "", "", "ИТОГО:", sum(r["amount"] for r in rows), ""])

        def quota_wait():
            self.status_var.set("Превышена квота Google (429) — жду 60 секунд "
                                "и повторяю…")
            self.root.update_idletasks()

        # Запрос №1: данные (с автоповтором при 429)
        with_retry(lambda: s.write_range(rng(f"A1:F{total_row}"), block),
                   "Запись данных", on_wait=quota_wait)

        # Собираем ВСЁ форматирование в один список операций
        reqs = [
            s.merge_request(rng("A1:F1")),
            s.format_request(rng("A1:F1"), bold=True, font_size=16,
                             text_color=WHITE, bg_color=DARK_BLUE,
                             h_align="CENTER", v_align="MIDDLE"),
            s.merge_request(rng("A2:F2")),
            s.format_request(rng("A2:F2"), italic=True, font_size=11,
                             text_color=GRAY, h_align="CENTER"),
            s.merge_request(rng("A3:F3")),
            s.format_request(rng("A3:F3"), font_size=9, text_color=GRAY,
                             h_align="CENTER"),
            s.format_request(rng(f"A{header}:F{header}"), bold=True,
                             text_color=WHITE, bg_color=BLUE,
                             h_align="CENTER", v_align="MIDDLE", wrap_text=True),
        ]
        for idx, row_i in enumerate(range(first_data, last_data + 1)):
            reqs.append(s.format_request(rng(f"A{row_i}:F{row_i}"),
                                         bg_color=LIGHT_BLUE if idx % 2 == 0 else WHITE))
            reqs.append(s.format_request(rng(f"A{row_i}"), h_align="CENTER"))
            reqs.append(s.format_request(rng(f"E{row_i}"),
                                         number_format=MONEY_FMT, h_align="RIGHT"))
            reqs.append(s.format_request(rng(f"F{row_i}"), h_align="CENTER"))
        reqs.append(s.borders_request(rng(f"A{header}:F{last_data}")))
        reqs.append(s.format_request(rng(f"A{total_row}:F{total_row}"),
                                     bold=True, bg_color=GREEN))
        reqs.append(s.format_request(rng(f"E{total_row}"),
                                     number_format=MONEY_FMT, h_align="RIGHT"))
        reqs.append(s.borders_request(rng(f"A{total_row}:F{total_row}"),
                                      inner_horizontal=False, inner_vertical=False))
        for col, width in (("A", 45), ("B", 200), ("C", 130),
                           ("D", 200), ("E", 130), ("F", 110)):
            reqs.append(s.column_width_request(title, col, width))
        reqs.append(s.freeze_request(title, header))

        # Запрос №2: всё форматирование разом (с автоповтором при 429)
        with_retry(lambda: s.batch_update(reqs),
                   "Форматирование отчёта", on_wait=quota_wait)


def main():
    if not CREDENTIALS_PATH:
        raise SystemExit("Не найден JSON-ключ сервисного аккаунта в папке проекта.")
    if not SPREADSHEET_ID or "HERE" in SPREADSHEET_ID:
        raise SystemExit("Укажите SPREADSHEET_ID в файле .env "
                         "(кусок URL таблицы между /d/ и /edit).")
    sheets = GoogleSheets(CREDENTIALS_PATH, SPREADSHEET_ID)
    root = tk.Tk()
    ReportGeneratorApp(root, sheets)
    root.mainloop()


if __name__ == "__main__":
    main()