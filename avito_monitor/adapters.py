from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode
from tempfile import NamedTemporaryFile
import json
import os

from .model import ReadQuota, ScanError, col_name


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
        self.writable_path = None
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

    def tabs(self, book):
        return [{"gid": gid, "name": str(gid)} for candidate, gid in self.books if candidate == book]

    def serialise(self):
        result = {}
        for (book, gid), rows in self.books.items():
            result.setdefault(book, {})[str(gid)] = rows
        return result

    def save_new_copy(self, path):
        path = Path(path).resolve()
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(self.serialise(), file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        self.writable_path = path
        return self

    def clear_id(self, address, expected):
        row = self.books[address.book, address.gid][address.row - 1]
        if row[address.col] != expected:
            raise ScanError("Id изменился до очистки")
        if self.writable_path:
            on_disk = self.from_path(self.writable_path)
            if self.books != on_disk.books:
                raise ScanError("Локальная копия изменилась перед записью")
        row[address.col] = {}
        if not self.writable_path:
            return
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.writable_path.parent, delete=False,
        ) as file:
            temp = Path(file.name)
            try:
                json.dump(self.serialise(), file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
                file.close()
                os.replace(temp, self.writable_path)
                saved = self.from_path(self.writable_path)
                if saved.books != self.books:
                    raise ScanError("Изменение не подтверждено чтением сохранённой копии")
            finally:
                temp.unlink(missing_ok=True)


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
        self.budget = None

    def begin_scan(self):
        self.metadata.clear()
        self.cache.clear()

    def get(self, book, params):
        from requests import RequestException
        from google.auth.exceptions import GoogleAuthError

        url = f"https://sheets.googleapis.com/v4/spreadsheets/{book}?{urlencode(params, doseq=True)}"
        if self.budget:
            self.budget.take()
        try:
            response = self.session.get(url, timeout=(5, 40))
            if response.status_code == 429:
                if self.budget:
                    self.budget.exhausted()
                raise ReadQuota("Google Sheets ограничил запросы, чтение продолжится позже")
            if response.status_code != 200:
                raise ScanError(f"Google Sheets HTTP {response.status_code}")
            payload = response.json()
            if not isinstance(payload, dict):
                raise ScanError("Google Sheets вернул данные неизвестного формата")
            return payload
        except (RequestException, ValueError, GoogleAuthError):
            raise ScanError("Google Sheets не вернул читаемый ответ") from None

    def tabs(self, book):
        if book not in self.metadata:
            metadata = self.get(book, {"fields": "sheets(properties(sheetId,title,hidden,gridProperties))"})
            try:
                self.metadata[book] = {
                    s["properties"]["sheetId"]: s["properties"] for s in metadata["sheets"]
                    if "gridProperties" in s["properties"]
                }
            except (KeyError, TypeError):
                raise ScanError("Google Sheets не вернул сведения о листах") from None
        return [
            {"gid": gid, "name": item["title"]}
            for gid, item in self.metadata[book].items() if not item.get("hidden")
        ]

    def grid(self, book, spec):
        gid = spec["gid"]
        start = spec.get("first_col", 0)
        last = spec.get("last_col")
        key = book, gid, start, last
        if key in self.cache:
            cached = self.cache[key]
            if isinstance(cached, ScanError):
                raise cached
            return cached
        self.tabs(book)
        self.fetch_grids(book, [key])
        return self.cache[key]

    def prefetch(self, book, tabs):
        keys = [(book, tab["gid"], 0, None) for tab in tabs if (book, tab["gid"], 0, None) not in self.cache]
        for offset in range(0, len(keys), 5):
            group = keys[offset:offset + 5]
            try:
                self.fetch_grids(book, group)
            except ReadQuota:
                raise
            except ScanError:
                # A single unreadable sheet must not hide the remaining tabs.
                for key in group:
                    if key not in self.cache:
                        try:
                            self.fetch_grids(book, [key])
                        except ReadQuota:
                            raise
                        except ScanError as exc:
                            self.cache[key] = exc

    def fetch_grids(self, book, keys):
        ranges = []
        for _, gid, start, last in keys:
            properties = self.metadata[book].get(gid)
            if not properties:
                raise ScanError("Настроенный лист не найден")
            width = properties["gridProperties"]["columnCount"]
            if not 0 <= start < (last or width) <= width:
                raise ScanError("Диапазон колонок вне листа")
            title = properties["title"].replace("'", "''")
            ranges.append(f"'{title}'!{col_name(start)}1:{col_name((last or width) - 1)}")
        raw = self.get(book, {
            "ranges": ranges,
            "fields": "sheets(properties(sheetId),data(startRow,startColumn,rowData(values(userEnteredValue,effectiveValue,formattedValue))))",
        })
        try:
            sheets = {sheet["properties"]["sheetId"]: sheet.get("data", []) for sheet in raw["sheets"]}
            if len(sheets) != len(raw["sheets"]) or set(sheets) != {key[1] for key in keys}:
                raise ValueError
            for key in keys:
                _, gid, start, _ = key
                data, = sheets[gid]
                if data.get("startRow", 0) != 0 or data.get("startColumn", 0) != start:
                    raise ValueError
                rows = [row.get("values", []) for row in data.get("rowData", [])]
                self.cache[key] = start, rows
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ScanError("Ответ Google не соответствует запрошенным листам") from None


class GoogleRemovalWriter(GoogleReadOnly):
    """Clear one exact, previously verified cell in the authorized target."""

    SCOPE = "https://www.googleapis.com/auth/spreadsheets"

    def __init__(self, target_book):
        allowed = os.environ.get("AVITO_MONITOR_WRITABLE_BOOKS", "").split()
        if target_book not in allowed:
            raise ScanError("Для таблицы автозагрузки не выдано разрешение на запись")
        super().__init__()
        self.target_book = target_book

    def clear_id(self, address, expected):
        from requests import RequestException
        from google.auth.exceptions import GoogleAuthError

        if address.book != self.target_book:
            raise ScanError("Очистка за пределами разрешённой таблицы")
        entered = expected.get("userEnteredValue", {})
        value = entered.get("stringValue", entered.get("numberValue"))
        display = expected.get("formattedValue", str(value))
        if value is None or display != str(value):
            raise ScanError("Формат ячейки Id не допускает точную условную очистку")
        request = {"findReplace": {
            "find": display, "replacement": "", "matchCase": True, "matchEntireCell": True,
            "searchByRegex": False, "includeFormulas": False,
            "range": {
                "sheetId": address.gid,
                "startRowIndex": address.row - 1, "endRowIndex": address.row,
                "startColumnIndex": address.col, "endColumnIndex": address.col + 1,
            },
        }}
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{address.book}:batchUpdate"
        try:
            response = self.session.post(url, json={"requests": [request]}, timeout=(5, 20))
            response.raise_for_status()
            result = response.json()["replies"][0].get("findReplace", {})
        except (RequestException, GoogleAuthError, ValueError, KeyError, IndexError, TypeError):
            raise ScanError("Google не подтвердил результат очистки Id") from None
        if result.get("occurrencesChanged") != 1:
            raise ScanError("Google не нашёл ожидаемый Id в согласованной ячейке")
