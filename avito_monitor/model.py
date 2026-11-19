from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import re
import unicodedata


class ScanError(Exception):
    """A safe description of a failed or incomplete read."""


def canonical(value):
    return " ".join(unicodedata.normalize("NFC", str(value)).casefold().split())


def cell_value(cell):
    value = cell.get("effectiveValue", cell.get("userEnteredValue", {}))
    for kind in ("stringValue", "numberValue", "boolValue"):
        if kind in value:
            return value[kind]
    return None


def price_state(cell):
    entered = cell.get("userEnteredValue", {})
    effective = cell.get("effectiveValue", {})
    if "errorValue" in effective:
        return "READ_ERROR", None
    if not entered and not effective:
        return "EMPTY", None
    value = cell_value(cell)
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3})+)(?:[.,][0-9]+)?", value):
            return "INVALID", None
        value = re.sub(r"\s", "", value).replace(",", ".")
    try:
        number = Decimal(str(value))
        if number.is_finite() and number > 0:
            return "POSITIVE", str(number)
    except InvalidOperation:
        pass
    return "INVALID", None


def id_state(cell):
    entered = cell.get("userEnteredValue", {})
    effective = cell.get("effectiveValue", {})
    if not entered and not effective:
        return "EMPTY"
    if "errorValue" in effective or "formulaValue" in entered:
        return "INVALID"
    value = cell_value(cell)
    if isinstance(value, str) and value.strip() and "stringValue" in entered:
        return "PRESENT"
    if type(value) is int and 0 <= value < 10**15 and "numberValue" in entered:
        return "PRESENT"
    return "INVALID"


def col_name(index):
    letters = ""
    while index >= 0:
        index, rest = divmod(index, 26)
        letters = chr(65 + rest) + letters
        index -= 1
    return letters


@dataclass(frozen=True)
class Address:
    book: str
    gid: int
    row: int
    col: int

    @property
    def link(self):
        return (
            f"https://docs.google.com/spreadsheets/d/{self.book}/edit"
            f"#gid={self.gid}&range={col_name(self.col)}{self.row}"
        )


@dataclass
class Position:
    identity: str
    title: str
    category: str
    source: Address
    target: Address
    price_cell: dict
    id_cell: dict
    state: str
    price: str | None
    issue: str | None


def extract(book, spec, grid, watch_field):
    start, rows = grid
    header_row = spec["header_row"]
    if start != spec.get("first_col", 0) or len(rows) < header_row:
        raise ScanError("Не найдена строка технических заголовков")
    headers = [canonical(cell_value(cell)) for cell in rows[header_row - 1]]
    allowed = {watch_field, *spec["keys"], *spec.get("where", {})}
    if spec.get("title"):
        allowed.add(spec["title"])
    if spec.get("category_field"):
        allowed.add(spec["category_field"])
    columns = {}
    for field in sorted(allowed):
        matches = [i for i, name in enumerate(headers) if name == canonical(field)]
        if len(matches) != 1:
            raise ScanError(f"Заголовок {field!r} отсутствует или повторяется")
        columns[field] = matches[0]

    def cell_at(row, field):
        index = columns[field]
        return row[index] if len(row) > index else {}

    entries = defaultdict(list)
    bad = []
    for rownum, row in enumerate(rows[header_row:], header_row + 1):
        if not any(row):
            continue
        if not all(
            canonical(cell_value(cell_at(row, field))) == canonical(value)
            for field, value in spec.get("where", {}).items()
        ):
            continue
        fields = [cell_value(cell_at(row, field)) for field in spec["keys"]]
        cell = cell_at(row, watch_field)
        address = Address(book, spec["gid"], rownum, start + columns[watch_field])
        if all(value is None or value == "" for value in fields) and not cell:
            continue
        if any(value is None or isinstance(value, bool) or not canonical(value) for value in fields):
            bad.append(address)
            continue
        key = tuple(canonical(value) for value in fields)
        title = cell_value(cell_at(row, spec["title"])) if spec.get("title") else None
        category = spec.get("category", "Прочее")
        if spec.get("category_field"):
            category_value = canonical(cell_value(cell_at(row, spec["category_field"])))
            category = {canonical(k): v for k, v in spec["categories"].items()}.get(
                category_value, "Прочее"
            )
        entries[key].append((address, cell, str(title or " · ".join(map(str, fields))), category))
    return entries, bad


def scan(project, adapter):
    positions = {}
    diagnostics = []
    claims = defaultdict(list)
    source_claims = defaultdict(list)
    for route in project["routes"]:
        source_spec = route["source"]
        target_spec = route["target"]
        try:
            source, missing_source = extract(
                project["source_book"], source_spec,
                adapter.grid(project["source_book"], source_spec), source_spec["price"],
            )
            target, missing_target = extract(
                project["target_book"], target_spec,
                adapter.grid(project["target_book"], target_spec), "Id",
            )
        except ScanError as exc:
            diagnostics.append({"code": "READ_ERROR", "detail": f'{route["name"]}: {exc}'})
            continue
        for address in missing_source + missing_target:
            diagnostics.append({"code": "MATCH_UNCERTAIN", "detail": "Неполный ключ", "links": [address.link]})
        for key in sorted(source.keys() | target.keys()):
            src = source.get(key, [])
            dst = target.get(key, [])
            if len(src) != 1 or len(dst) != 1:
                diagnostics.append({
                    "code": "MATCH_UNCERTAIN",
                    "detail": f'{route["name"]}: строк прайса {len(src)}, строк выгрузки {len(dst)}',
                    "links": [entry[0].link for entry in src + dst],
                })
                continue
            source_addr, price_cell, _, _ = src[0]
            target_addr, id_cell, title, category = dst[0]
            identity = json.dumps([
                project["source_book"], source_spec["gid"], project["target_book"],
                target_spec["gid"], route["name"], key,
            ], ensure_ascii=False, separators=(",", ":"))
            state, amount = price_state(price_cell)
            identifier = id_state(id_cell)
            if state in ("READ_ERROR", "INVALID") or identifier == "INVALID":
                diagnostics.append({
                    "code": "READ_ERROR" if state == "READ_ERROR" else "INVALID_CELL",
                    "detail": "Неразобранная цена или Id",
                    "links": [source_addr.link, target_addr.link],
                })
            issue = {
                ("POSITIVE", "EMPTY"): "NEEDS_PUBLICATION",
                ("EMPTY", "PRESENT"): "NEEDS_REMOVAL",
            }.get((state, identifier))
            positions[identity] = Position(
                identity, title, category, source_addr, target_addr,
                price_cell, id_cell, state, amount, issue,
            )
            claims[target_addr].append(identity)
            source_claims[source_addr].append(identity)
    for address, identities in [*claims.items(), *source_claims.items()]:
        if len(identities) > 1:
            diagnostics.append({
                "code": "MATCH_UNCERTAIN", "detail": "Одна ячейка входит в несколько правил",
                "links": [address.link],
            })
            for identity in identities:
                positions.pop(identity, None)
    return positions, diagnostics
