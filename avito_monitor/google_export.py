"""Read explicitly configured Google Sheets exports for report-only monitoring."""
from io import BytesIO
from pathlib import PurePosixPath
from urllib.error import URLError
from urllib.request import urlopen
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile
import math
import re

from .model import ScanError


X = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
MAX_EXPORT = 32 * 1024 * 1024
MAX_UNPACKED = 256 * 1024 * 1024


def strings(node):
    return "".join(piece.text or "" for piece in node.iter(X + "t")) if node is not None else ""


def exported_cell(node, shared):
    kind = node.get("t", "n")
    raw = node.findtext(X + "v")
    formula = node.find(X + "f")
    if kind == "inlineStr":
        value = {"stringValue": strings(node.find(X + "is"))}
    elif kind == "e":
        value = {"errorValue": {"message": raw or "Ошибка ячейки"}}
    elif kind == "str":
        # Google exports some physically empty formatted cells as t="str"
        # with an empty v. Retain the formula flag when a formula is present.
        value = {"stringValue": raw} if raw else {}
    elif raw is None or raw == "":
        value = {}
    elif kind == "s":
        value = {"stringValue": shared[int(raw)]}
    elif kind == "b" and raw in {"0", "1"}:
        value = {"boolValue": raw == "1"}
    elif kind == "n":
        number = float(raw)
        if not math.isfinite(number):
            raise ValueError("Некорректное число")
        value = {"numberValue": int(number) if number.is_integer() else number}
    else:
        raise ValueError("Неизвестный тип ячейки")
    if formula is not None:
        return {"userEnteredValue": {"formulaValue": "=" + (formula.text or "")}, "effectiveValue": value}
    if value == {"stringValue": ""}:
        return {}
    return {"userEnteredValue": value, "effectiveValue": value} if value else {}


def read_workbook(payload, names):
    with ZipFile(BytesIO(payload)) as archive:
        if sum(member.file_size for member in archive.infolist()) > MAX_UNPACKED:
            raise ScanError("Объём экспорта Google Sheets превышает лимит")
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relations = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {relation.get("Id"): relation.get("Target") for relation in relations}
        shared_path = "xl/sharedStrings.xml"
        shared = (
            [strings(entry) for entry in ET.fromstring(archive.read(shared_path))]
            if shared_path in archive.namelist() else []
        )
        grids = {}
        for sheet in workbook.findall(f"{X}sheets/{X}sheet"):
            title = sheet.attrib["name"]
            if title not in names:
                continue
            target = targets[sheet.attrib[R + "id"]]
            path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            if not path.startswith("xl/worksheets/") or ".." in PurePosixPath(path).parts or title in grids:
                raise ValueError("Неоднозначный лист XLSX")
            root = ET.fromstring(archive.read(path))
            grid = []
            for row in root.findall(f"{X}sheetData/{X}row"):
                number = int(row.attrib["r"])
                if not len(grid) < number <= 1048576:
                    raise ValueError("Неверный порядок строк XLSX")
                grid.extend([] for _ in range(number - len(grid)))
                parsed = grid[-1]
                for cell in row.findall(X + "c"):
                    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]*)", cell.attrib["r"])
                    if not match or int(match[2]) != number:
                        raise ValueError("Неверный адрес ячейки XLSX")
                    column = 0
                    for letter in match[1]:
                        column = column * 26 + ord(letter) - ord("A") + 1
                    if not len(parsed) < column <= 16384:
                        raise ValueError("Неверный порядок колонок XLSX")
                    parsed.extend({} for _ in range(column - len(parsed)))
                    parsed[-1] = exported_cell(cell, shared)
            grids[title] = grid
        return grids


class GoogleExportReadOnly:
    def __init__(self, titles):
        self.titles = titles
        self.books = {}

    def begin_scan(self):
        self.books.clear()

    def tabs(self, book):
        return [{"gid": int(gid), "name": title} for gid, title in self.titles.get(book, {}).items()]

    def grid(self, book, spec):
        titles = self.titles.get(book, {})
        title = titles.get(str(spec["gid"]))
        if not title:
            raise ScanError("Для выгрузки не задано имя листа")
        if book not in self.books:
            try:
                url = f"https://docs.google.com/spreadsheets/d/{book}/export?format=xlsx"
                with urlopen(url, timeout=30) as response:
                    payload = response.read(MAX_EXPORT + 1)
                if len(payload) > MAX_EXPORT:
                    raise ScanError("Размер экспорта Google Sheets превышает лимит")
                self.books[book] = read_workbook(payload, set(titles.values()))
            except (OSError, URLError, BadZipFile, ET.ParseError, KeyError, IndexError, ValueError, ScanError):
                self.books[book] = None
        grids = self.books[book]
        if grids is None:
            raise ScanError("Экспорт Google Sheets недоступен или повреждён")
        if title not in grids:
            raise ScanError("Указанный лист отсутствует в экспорте Google Sheets")
        start, last = spec.get("first_col", 0), spec.get("last_col")
        return start, [row[start:last] for row in grids[title]]
