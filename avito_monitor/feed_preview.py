"""Local XML previews with explicit handling of blank IDs.

The preview covers title, price and ID. It cannot be used as a production feed.
"""
from argparse import ArgumentParser
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET
import json

FIELDS = {"row", "id", "title", "price"}


def normalise_snapshot(rows):
    snapshot = {}
    seen_ids = set()
    for row in rows:
        if set(row) != FIELDS or type(row["row"]) is not int or row["row"] < 1:
            raise ValueError("Ожидались координаты строки, Id, заголовок и цена")
        if row["row"] in snapshot:
            raise ValueError("Номер строки повторяется")
        identifier = row["id"]
        if type(identifier) is int:
            identifier = str(identifier)
        if identifier is not None and (not isinstance(identifier, str) or not identifier.strip()):
            raise ValueError("Id должен быть непустой строкой, целым числом или null")
        if identifier in seen_ids:
            raise ValueError("Технический Id встречается в нескольких строках")
        if identifier is not None:
            seen_ids.add(identifier)
        title = row["title"]
        if not isinstance(title, str) or not title.strip():
            raise ValueError("У строки нет заголовка")
        try:
            price = Decimal(str(row["price"]))
            amount = format(price, "f") if price.is_finite() and 0 < price < 100_000_000 else None
        except InvalidOperation:
            amount = None
        snapshot[row["row"]] = {"id": identifier, "title": title, "price": amount}
    return snapshot


def render(snapshot, *, skip_empty_id):
    root = ET.Element("Ads")
    root.append(ET.Comment(" TEST PREVIEW ONLY. DO NOT UPLOAD. "))
    for row in snapshot.values():
        identifier = row["id"]
        if not row["price"] or (skip_empty_id and identifier is None):
            continue
        ad = ET.SubElement(root, "Ad")
        if identifier is not None:
            ET.SubElement(ad, "Id").text = identifier
        ET.SubElement(ad, "Title").text = row["title"]
        ET.SubElement(ad, "Price").text = row["price"]
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def compare(before, after, row_number):
    before = normalise_snapshot(before)
    after = normalise_snapshot(after)
    if before.keys() != after.keys() or row_number not in before:
        raise ValueError("Состав строк в снимках различается")
    edited = before[row_number]
    if not edited["id"] or not edited["price"]:
        raise ValueError("В контрольной строке нужны исходный Id и положительная цена")
    cleared = dict(edited, id=None)
    if after != (before | {row_number: cleared}):
        raise ValueError("Кроме выбранного Id изменились другие данные")
    if sum(row["title"] == edited["title"] for row in before.values()) != 1:
        raise ValueError("Заголовок контрольной строки должен быть уникальным")
    before_strict = render(before, skip_empty_id=True)
    after_strict = render(after, skip_empty_id=True)
    before_relaxed = render(before, skip_empty_id=False)
    after_relaxed = render(after, skip_empty_id=False)
    return {
        "before-strict.xml": before_strict,
        "after-strict.xml": after_strict,
        "before-omit-id.xml": before_relaxed,
        "after-omit-id.xml": after_relaxed,
    }


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--after", required=True, type=Path)
    parser.add_argument("--row", required=True, type=int)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    previews = compare(
        json.loads(args.before.read_text(encoding="utf-8")),
        json.loads(args.after.read_text(encoding="utf-8")),
        args.row,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    for name, content in previews.items():
        (args.out / name).write_bytes(content)


if __name__ == "__main__":
    main()
