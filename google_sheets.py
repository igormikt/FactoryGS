"""
google_sheets.py — модуль работы с Google Sheets через сервисный аккаунт.
Класс GoogleSheets: CRUD-операции + методы форматирования для отчётов.
Все операции форматирования можно собирать в один batchUpdate-запрос
(методы *_request + batch_update) — это экономит квоту Google (60 зап./мин).
"""

from __future__ import annotations

import json
import os
import re
from typing import List, Optional, Union

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CellValue = Union[str, int, float]


# ---------------------------------------------------------------------------
# Вспомогательные функции конфигурации (.env и автопоиск JSON-ключа)
# ---------------------------------------------------------------------------
def load_env(path: Optional[str] = None) -> dict:
    """Простой загрузчик .env (KEY=VALUE) без сторонних библиотек."""
    base = os.path.dirname(os.path.abspath(__file__))
    path = path or os.path.join(base, ".env")
    env = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def find_service_account_key() -> Optional[str]:
    """Ищет JSON-ключ сервисного аккаунта в папке проекта."""
    base = os.path.dirname(os.path.abspath(__file__))
    for name in sorted(os.listdir(base)):
        if name.lower().endswith(".json"):
            try:
                with open(os.path.join(base, name), encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data.get("type") == "service_account":
                    return os.path.join(base, name)
            except Exception:
                continue
    return None


def _col_to_index(col: str) -> int:
    """'A' -> 0, 'B' -> 1, 'AA' -> 26."""
    idx = 0
    for ch in col.upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


_RANGE_RE = re.compile(
    r"^(?:(?P<sheet>[^!]+)!)?(?P<c1>[A-Za-z]+)(?P<r1>\d+)?"
    r"(?::(?P<c2>[A-Za-z]+)(?P<r2>\d+)?)?$"
)


class GoogleSheets:
    """Обёртка над Google Sheets API: CRUD + форматирование."""

    def __init__(self, credentials_path: str, spreadsheet_id: str):
        if not os.path.exists(credentials_path):
            raise FileNotFoundError(f"Не найден JSON-ключ: {credentials_path}")
        self.credentials_path = credentials_path
        self.spreadsheet_id = spreadsheet_id
        creds = service_account.Credentials.from_service_account_file(
            credentials_path, scopes=SCOPES
        )
        self.service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    # ------------------------- служебные -----------------------------------
    @staticmethod
    def _q(title: str) -> str:
        return f"'{title}'"

    def _meta(self) -> dict:
        return self.service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id).execute()

    def get_sheet_names(self) -> List[str]:
        return [s["properties"]["title"] for s in self._meta().get("sheets", [])]

    def _sheet_title(self, sheet_name: Optional[str]) -> str:
        if sheet_name:
            return sheet_name.strip("'\"")
        names = self.get_sheet_names()
        if not names:
            raise Exception("Таблица не содержит листов")
        return names[0]

    def _sheet_id(self, title: str) -> int:
        for s in self._meta().get("sheets", []):
            if s["properties"]["title"] == title:
                return s["properties"]["sheetId"]
        raise ValueError(f"Лист '{title}' не найден")

    def _grid_range(self, range_name: str) -> dict:
        m = _RANGE_RE.match(range_name.strip())
        if not m:
            raise ValueError(f"Не удалось разобрать диапазон: {range_name}")
        title = (m.group("sheet") or "").strip("'\"") or self._sheet_title(None)
        c1, r1, c2, r2 = m.group("c1"), m.group("r1"), m.group("c2"), m.group("r2")
        grid = {
            "sheetId": self._sheet_id(title),
            "startColumnIndex": _col_to_index(c1),
            "endColumnIndex": _col_to_index(c2 or c1) + 1,
        }
        if r1:
            grid["startRowIndex"] = int(r1) - 1
            grid["endRowIndex"] = int(r2 or r1)
        return grid

    @staticmethod
    def _clean(value):
        return value.replace("\xa0", " ") if isinstance(value, str) else value

    @staticmethod
    def _rgb(color) -> dict:
        r, g, b = color
        return {"red": r / 255, "green": g / 255, "blue": b / 255}

    def _batch(self, requests: list) -> dict:
        return self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id, body={"requests": requests}).execute()

    # ----------------------------- CRUD ------------------------------------
    def read_all_cells(self, sheet_name: Optional[str] = None) -> List[List[CellValue]]:
        title = self._sheet_title(sheet_name)
        result = (self.service.spreadsheets().values()
                  .get(spreadsheetId=self.spreadsheet_id, range=self._q(title))
                  .execute())
        return [[self._clean(c) for c in row] for row in result.get("values", [])]

    def write_range(self, range_name: str, values: List[List[CellValue]],
                    value_input_option: str = "RAW") -> dict:
        try:
            return (self.service.spreadsheets().values()
                    .update(spreadsheetId=self.spreadsheet_id, range=range_name,
                            valueInputOption=value_input_option,
                            body={"values": values}).execute())
        except HttpError as e:
            raise Exception(f"Ошибка записи данных: {e}")

    def update_cell(self, cell_address: str, value: CellValue,
                    sheet_name: Optional[str] = None,
                    value_input_option: str = "RAW") -> dict:
        if sheet_name:
            range_name = f"{self._q(self._sheet_title(sheet_name))}!{cell_address}"
        else:
            range_name = cell_address
        return self.write_range(range_name, [[value]], value_input_option)

    def append_row(self, values: List[CellValue], sheet_name: Optional[str] = None,
                   value_input_option: str = "RAW") -> dict:
        title = self._sheet_title(sheet_name)
        try:
            return (self.service.spreadsheets().values()
                    .append(spreadsheetId=self.spreadsheet_id, range=self._q(title),
                            valueInputOption=value_input_option,
                            body={"values": [values]}).execute())
        except HttpError as e:
            raise Exception(f"Ошибка добавления строки: {e}")

    def delete_row(self, row_number: int, sheet_name: Optional[str] = None) -> dict:
        sid = self._sheet_id(self._sheet_title(sheet_name))
        return self._batch([{
            "deleteDimension": {
                "range": {"sheetId": sid, "dimension": "ROWS",
                          "startIndex": row_number - 1, "endIndex": row_number}}}])

    def clear_range(self, range_name: str) -> dict:
        try:
            return (self.service.spreadsheets().values()
                    .clear(spreadsheetId=self.spreadsheet_id, range=range_name)
                    .execute())
        except HttpError as e:
            raise Exception(f"Ошибка очистки диапазона: {e}")

    # ------------------- листы и форматирование ----------------------------
    def create_sheet(self, title: str) -> str:
        names = self.get_sheet_names()
        candidate, i = title, 2
        while candidate in names:
            candidate = f"{title} ({i})"
            i += 1
        self._batch([{"addSheet": {"properties": {"title": candidate}}}])
        return candidate

    # --- строители запросов (НЕ отправляют, а возвращают операцию) ---------
    def merge_request(self, range_name: str, merge_type: str = "MERGE_ALL") -> dict:
        return {"mergeCells": {"range": self._grid_range(range_name),
                               "mergeType": merge_type}}

    def format_request(self, range_name: str, *, bold: Optional[bool] = None,
                       italic: Optional[bool] = None,
                       font_size: Optional[int] = None,
                       text_color: Optional[tuple] = None,
                       bg_color: Optional[tuple] = None,
                       h_align: Optional[str] = None, v_align: Optional[str] = None,
                       wrap_text: Optional[bool] = None,
                       number_format: Optional[str] = None,
                       number_format_type: str = "NUMBER") -> dict:
        uef, fields, tf = {}, [], {}
        if bold is not None:
            tf["bold"] = bold
            fields.append("userEnteredFormat.textFormat.bold")
        if italic is not None:
            tf["italic"] = italic
            fields.append("userEnteredFormat.textFormat.italic")
        if font_size is not None:
            tf["fontSize"] = font_size
            fields.append("userEnteredFormat.textFormat.fontSize")
        if text_color is not None:
            tf["foregroundColor"] = self._rgb(text_color)
            fields.append("userEnteredFormat.textFormat.foregroundColor")
        if tf:
            uef["textFormat"] = tf
        if bg_color is not None:
            uef["backgroundColor"] = self._rgb(bg_color)
            fields.append("userEnteredFormat.backgroundColor")
        if h_align:
            uef["horizontalAlignment"] = h_align
            fields.append("userEnteredFormat.horizontalAlignment")
        if v_align:
            uef["verticalAlignment"] = v_align
            fields.append("userEnteredFormat.verticalAlignment")
        if wrap_text is not None:
            uef["wrapStrategy"] = "WRAP" if wrap_text else "CLIP"
            fields.append("userEnteredFormat.wrapStrategy")
        if number_format:
            uef["numberFormat"] = {"type": number_format_type,
                                   "pattern": number_format}
            fields.append("userEnteredFormat.numberFormat")
        return {"repeatCell": {"range": self._grid_range(range_name),
                               "cell": {"userEnteredFormat": uef},
                               "fields": ",".join(fields)}}

    def borders_request(self, range_name: str, color=(0, 0, 0), top=True,
                        bottom=True, left=True, right=True,
                        inner_horizontal=True, inner_vertical=True) -> dict:
        style = {"style": "SOLID", "width": 1, "color": self._rgb(color)}
        borders = {name: style for name, flag in (
            ("top", top), ("bottom", bottom), ("left", left), ("right", right),
            ("innerHorizontal", inner_horizontal), ("innerVertical", inner_vertical))
            if flag}
        return {"updateBorders": {"range": self._grid_range(range_name), **borders}}

    def column_width_request(self, sheet_name: str, column: str,
                             width_px: int) -> dict:
        idx = _col_to_index(column)
        return {"updateDimensionProperties": {
            "range": {"sheetId": self._sheet_id(sheet_name),
                      "dimension": "COLUMNS",
                      "startIndex": idx, "endIndex": idx + 1},
            "properties": {"pixelSize": width_px},
            "fields": "pixelSize"}}

    def freeze_request(self, sheet_name: str, rows: int) -> dict:
        """Закрепить верхние строки (шапку).

        ВАЖНО: по схеме Google Sheets API в updateSheetProperties
        sheetId передаётся ВНУТРИ объекта properties, а не рядом с ним.
        """
        return {"updateSheetProperties": {
            "properties": {
                "sheetId": self._sheet_id(sheet_name),
                "gridProperties": {"frozenRowCount": rows},
            },
            "fields": "gridProperties.frozenRowCount"}}

    # --- одиночные вызовы (как раньше) и пакетный --------------------------
    def merge_cells(self, range_name: str, merge_type: str = "MERGE_ALL") -> dict:
        return self._batch([self.merge_request(range_name, merge_type)])

    def format_range(self, range_name: str, **kwargs) -> dict:
        return self._batch([self.format_request(range_name, **kwargs)])

    def set_borders(self, range_name: str, **kwargs) -> dict:
        return self._batch([self.borders_request(range_name, **kwargs)])

    def set_column_width(self, sheet_name: str, column: str, width_px: int) -> dict:
        return self._batch([self.column_width_request(sheet_name, column, width_px)])

    def freeze_rows(self, sheet_name: str, rows: int) -> dict:
        return self._batch([self.freeze_request(sheet_name, rows)])

    def batch_update(self, requests: list) -> dict:
        """Отправляет МНОГО операций форматирования ОДНИМ запросом (экономит квоту)."""
        return self._batch(requests)


# ---------------------------------------------------------------------------
# Точка входа для проверки модуля
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _env = load_env()
    _creds = _env.get("CREDENTIALS_PATH") or find_service_account_key()
    _sid = _env.get("SPREADSHEET_ID", "")
    if not _creds or not _sid or "HERE" in _sid:
        raise SystemExit("Создайте .env: укажите SPREADSHEET_ID "
                         "(и CREDENTIALS_PATH при необходимости).")
    sheets = GoogleSheets(_creds, _sid)
    print("Доступные листы:")
    for name in sheets.get_sheet_names():
        print(f" - {name}")
    print()
    print("Чтение всех ячеек из первого листа:")
    print("-" * 50)
    all_cells = sheets.read_all_cells()
    if not all_cells:
        print("Таблица пуста")
    else:
        for row_index, row in enumerate(all_cells, start=1):
            print(f"Строка {row_index}: {row}")
    print("-" * 50)
    print(f"Всего строк: {len(all_cells)}")