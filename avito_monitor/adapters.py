from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode
import json

from .model import ScanError, col_name


def literal(value):
    kinds = {str: "stringValue", int: "numberValue", float: "numberValue", bool: "boolValue"}
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    typed = {kinds[type(value)]: value}
    return {"userEnteredValue": typed, "effectiveValue": typed}


class Fixture:
    """Google-style CellData backed by a local, disposable copy."""

    def __init__(self, books):
        self.books = {
            (book, int(gid)): [[literal(value) for value in row] for row in rows]
            for book, sheets in books.items() for gid, rows in sheets.items()
        }

    @classmethod
    def from_path(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def grid(self, book, spec):
        rows = self.books.get((book, spec["gid"]))
        if rows is None:
            raise ScanError("Лист не найден")
        start = spec.get("first_col", 0)
        return start, [row[start:spec.get("last_col")] for row in rows]

    def clear_id(self, address, expected):
        row = self.books[address.book, address.gid][address.row - 1]
        if row[address.col] != expected:
            raise ScanError("Id изменился до очистки")
        row[address.col] = {}


class GoogleReadOnly:
    SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"

    def __init__(self):
        try:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
        except ImportError:
            raise ScanError("Установите зависимости Google Sheets из requirements-google.txt") from None
        try:
            credentials, _ = google.auth.default(scopes=[self.SCOPE])
        except google.auth.exceptions.GoogleAuthError:
            raise ScanError("Не удалось получить учётные данные Google") from None
        self.session = AuthorizedSession(credentials)
        self.metadata = {}
        self.cache = {}

    def begin_scan(self):
        self.metadata.clear()
        self.cache.clear()

    def get(self, book, params):
        from requests import RequestException
        from google.auth.exceptions import GoogleAuthError

        url = f"https://sheets.googleapis.com/v4/spreadsheets/{book}?{urlencode(params)}"
        try:
            response = self.session.get(url, timeout=(5, 40))
            if response.status_code != 200:
                raise ScanError(f"Google Sheets HTTP {response.status_code}")
            return response.json()
        except (RequestException, ValueError, GoogleAuthError):
            raise ScanError("Google Sheets не вернул читаемый ответ") from None

    def grid(self, book, spec):
        gid = spec["gid"]
        start = spec.get("first_col", 0)
        last = spec.get("last_col")
        key = book, gid, start, last
        if key in self.cache:
            return self.cache[key]
        if book not in self.metadata:
            metadata = self.get(book, {"fields": "sheets(properties(sheetId,title,gridProperties))"})
            self.metadata[book] = {s["properties"]["sheetId"]: s["properties"] for s in metadata["sheets"]}
        properties = self.metadata[book].get(gid)
        if not properties or "gridProperties" not in properties:
            raise ScanError("Настроенный лист не найден")
        width = properties["gridProperties"]["columnCount"]
        if not 0 <= start < (last or width) <= width:
            raise ScanError("Диапазон колонок вне листа")
        title = properties["title"].replace("'", "''")
        a1 = f"'{title}'!{col_name(start)}1:{col_name((last or width) - 1)}"
        raw = self.get(book, {
            "ranges": a1,
            "fields": "sheets(properties(sheetId),data(startRow,startColumn,rowData(values(userEnteredValue,effectiveValue,formattedValue)))",
        })
        sheets = raw.get("sheets", [])
        if len(sheets) != 1 or sheets[0]["properties"]["sheetId"] != gid:
            raise ScanError("Ответ Google относится к другому листу")
        data = sheets[0].get("data", [])
        if len(data) != 1 or data[0].get("startRow", 0) != 0 or data[0].get("startColumn", 0) != start:
            raise ScanError("Ответ Google не соответствует запрошенному диапазону")
        rows = [row.get("values", []) for row in data[0].get("rowData", [])]
        result = start, rows
        self.cache[key] = result
        return result
