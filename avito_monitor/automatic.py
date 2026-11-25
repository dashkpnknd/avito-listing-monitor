"""Discover the existing autoload template and join exact product variants."""
from collections import Counter, defaultdict
import json
import re

from .model import Address, ScanError, canonical, cell_value, describe_position, id_state, matched_position, report_columns, unique_positions


FIELDS = {
    "title": ("title", "название", "name"),
    "model": ("model", "модель"),
    "vendor": ("vendor", "brand", "производитель", "бренд"),
    "color": ("color", "colour", "цвет"),
    "memory": ("memorysize", "встроеннаяпамять"),
    "sim": ("simconfig", "simкарты", "sim"),
    "ram": ("ramsize", "оперативнаяпамять", "озу"),
    "condition": ("condition", "состояние", "состояниетовара"),
    "akb": ("akb", "акб", "batteryhealth", "состояниеаккумулятора", "состояниеаккумулятораiphone"),
    "imei": ("imei", "imeiтелефона"),
    "set": ("set", "комплект"),
    "sealed": ("boxsealed", "состояниекоробкителефона"),
    "history": ("devicehistory", "историясмартфона"),
    "sku": ("sku", "артикул"),
    "id": ("id",),
    "listing": ("номерабъявления", "avitoid", "avitoitemid"),
}
HEADERS = {alias: field for field, aliases in FIELDS.items() for alias in aliases}
PRODUCT_FIELDS = FIELDS.keys() - {"id", "listing"}


def header(cell):
    raw = re.sub(r"[^a-zа-я0-9]", "", canonical(cell_value(cell) or "").replace("ё", "е"))
    return "price" if re.fullmatch(r"(?:price|цена)[0-9]*", raw) else HEADERS.get(raw, "")


def at(row, index):
    return row[index] if index is not None and index < len(row) else {}


def avito_tab(tab):
    return re.search(r"\b(?:avito|авито)\b", canonical(tab["name"])) is not None


def discover(books, adapter, diagnostics):
    for book, role in books:
        try:
            tabs = adapter.tabs(book)
        except ScanError as exc:
            diagnostics.append({"code": "READ_ERROR", "detail": str(exc)})
            continue
        marked = [tab for tab in tabs if avito_tab(tab)]
        selected = marked or tabs
        if hasattr(adapter, "prefetch"):
            adapter.prefetch(book, selected)
        for tab in selected:
            try:
                _, rows = adapter.grid(book, tab)
            except ScanError as exc:
                diagnostics.append({"code": "READ_ERROR", "detail": f'{tab["name"]}: {exc}'})
                continue
            for number, row in enumerate(rows[:15], start=1):
                fields = [header(cell) for cell in row]
                found = set(fields)
                if role == "source" and "price" in found and PRODUCT_FIELDS & found:
                    yield role, book, tab, rows, number, fields
                    break
                if role == "target" and {"id", "title"} <= found:
                    yield role, book, tab, rows, number, fields
                    break
            else:
                if marked and rows:
                    diagnostics.append({"code": "READ_ERROR", "detail": f'{tab["name"]}: не найдены товарные заголовки'})


def value_at(row, col):
    value = cell_value(at(row, col))
    return "" if value is None or isinstance(value, bool) else canonical(value)


def source_index(sheet, index, diagnostics):
    _, book, tab, rows, header_row, fields = sheet
    origin = 0
    for price, field in enumerate(fields):
        if field != "price":
            continue
        columns = [(name, column) for column, name in enumerate(fields[origin:price], origin) if name in PRODUCT_FIELDS]
        origin = price + 1
        names = [name for name, _ in columns]
        if not set(names) & {"title", "model", "sku", "imei"} or len(names) != len(set(names)):
            diagnostics.append({"code": "READ_ERROR", "detail": f'{tab["name"]}: неоднозначные заголовки блока цены'})
            continue
        columns.sort()
        signature = tuple(name for name, _ in columns)
        for number, row in enumerate(rows[header_row:], start=header_row + 1):
            values = tuple(value_at(row, col) for _, col in columns)
            if not any(values):
                continue
            anchor = any(value and name in {"title", "model", "sku", "imei"} for name, value in zip(signature, values))
            if anchor:
                index[signature][values].append(Address(book, tab["gid"], number, price))


def target_positions(sheet, source, grids, positions, identifiers, diagnostics):
    _, book, tab, rows, header_row, fields = sheet
    names = Counter(field for field in fields if field)
    duplicates = names.keys() & (PRODUCT_FIELDS | {"id", "listing"})
    if any(names[name] > 1 for name in duplicates):
        diagnostics.append({"code": "READ_ERROR", "detail": f'{tab["name"]}: повторяются заголовки товара'})
        return
    columns = {field: col for col, field in enumerate(fields) if field}
    variant_columns = report_columns(rows[header_row - 1])
    # The displayed listing number can be on the human-readable line above
    # AvitoId. Prefer that explicit heading when it exists.
    if header_row > 1:
        preceding = [
            i for i, cell in enumerate(rows[header_row - 2])
            if canonical(cell_value(cell) or "") == "номер объявления"
        ]
        if len(preceding) == 1:
            columns["listing"] = preceding[0]
        elif preceding:
            diagnostics.append({"code": "READ_ERROR", "detail": f'{tab["name"]}: несколько номеров объявления'})
            return
    label = re.sub(r"^(?:avito|авито)\s*[-–—]?\s*", "", tab["name"], flags=re.I)
    for number, row in enumerate(rows[header_row:], start=header_row + 1):
        target = Address(book, tab["gid"], number, columns["id"])
        id_cell = at(row, target.col)
        title = cell_value(at(row, columns["title"]))
        if not title and not id_cell:
            continue
        listing = str(cell_value(at(row, columns.get("listing"))) or "")
        if re.fullmatch(r"[1-9][0-9]*", listing):
            identifiers["listing", listing].append(target)
        if id_state(id_cell) == "PRESENT":
            identifiers["id", str(cell_value(id_cell))].append(target)
        params = {field: value_at(row, col) for field, col in columns.items() if field in PRODUCT_FIELDS}
        phone = bool(params.get("model")) and "sim" in params and "memory" in params
        critical = {"imei", "akb"} & {name for name, value in params.items() if value}
        condition = params.get("condition")
        if condition and condition not in {"новое", "новый", "new"}:
            critical.add("condition")
        if phone:
            critical.update(("model", "sim", "memory", "color"))
            if params.get("ram") and params.get("vendor") != "apple":
                critical.add("ram")
        candidates = []
        for signature, variants in source.items():
            if not critical.issubset(signature) or any(name not in params for name in signature):
                continue
            if phone and params.get("vendor") and "vendor" not in signature and (
                params["vendor"] != "apple" or not params["model"].startswith("iphone")
            ):
                continue
            if any(not params.get(name) for name in critical):
                continue
            key = tuple(params[name] for name in signature)
            candidates.extend((address, signature, key) for address in variants.get(key, ()))
        if len(candidates) != 1:
            diagnostics.append({
                "code": "MATCH_UNCERTAIN",
                "detail": f'{tab["name"]}: точных строк прайса {len(candidates)}',
                "links": [target.link, *(address.link for address, _, _ in candidates)],
            })
            continue
        address, signature, key = candidates[0]
        source_cell = at(grids[address.book, address.gid][address.row - 1], address.col)
        category = label or "Прочее"
        if phone and params.get("vendor"):
            brand = cell_value(at(row, columns["vendor"]))
            category = "iPhone" if params["vendor"] == "apple" else str(brand)
        identity = json.dumps([
            address.book, address.gid, book, tab["gid"], "auto", signature, key, condition, params.get("vendor"),
        ], ensure_ascii=False, separators=(",", ":"))
        source_entry = (address, source_cell)
        title = str(title or " · ".join(key))
        target_entry = (target, id_cell, title, category,
                        listing if re.fullmatch(r"[1-9][0-9]*", listing) else None,
                        describe_position(title, row, variant_columns))
        # Keep all claims until duplicate source/target checks have run.
        if identity in positions:
            diagnostics.append({
                "code": "MATCH_UNCERTAIN", "detail": "Повторяется вариант товара",
                "links": [positions[identity][0].target.link, target.link],
            })
            positions[identity].append(matched_position(identity, source_entry, target_entry, diagnostics))
        else:
            positions[identity] = [matched_position(identity, source_entry, target_entry, diagnostics)]


def automatic_scan(project, adapter):
    diagnostics = []
    books = [(project["source_book"], "source"), (project["target_book"], "target")]
    source = defaultdict(lambda: defaultdict(list))
    grids = {}
    targets = []
    for sheet in discover(books, adapter, diagnostics):
        role, book, tab, rows, _, _ = sheet
        grids[book, tab["gid"]] = rows
        if role == "source":
            source_index(sheet, source, diagnostics)
        else:
            targets.append(sheet)
    if not source or not targets:
        diagnostics.append({"code": "READ_ERROR", "detail": "Не найдены данные прайса или автозагрузки"})
        return {}, diagnostics
    positions = {}
    identifiers = defaultdict(list)
    for sheet in targets:
        target_positions(sheet, source, grids, positions, identifiers, diagnostics)
    singles = {identity: items[0] for identity, items in positions.items() if len(items) == 1}
    duplicates = {item.source for items in positions.values() if len(items) > 1 for item in items}
    blocked = set()
    for duplicate in identifiers.values():
        if len(duplicate) > 1:
            blocked.update(duplicate)
            diagnostics.append({
                "code": "MATCH_UNCERTAIN", "detail": "Повторяется Id или номер объявления",
                "links": [address.link for address in duplicate],
            })
    singles = {
        identity: pos for identity, pos in singles.items()
        if pos.source not in duplicates and pos.target not in blocked
    }
    return unique_positions(singles, diagnostics), diagnostics
