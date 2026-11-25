"""Check whether a selected Ad survives between two local XML snapshots."""
from collections import Counter
from xml.etree import ElementTree as ET
import re

from .model import ScanError


def read_ads(path):
    root = ET.parse(path).getroot()
    if root.tag != "Ads":
        raise ScanError("Ожидался XML с корнем Ads")
    return root, root.findall("Ad")


def values(ad, fields):
    return tuple((ad.findtext(field) or "").strip() for field in fields)


def compare_feeds(before, after, old_id, match_fields=("Title",), *, expected_title=None):
    if not old_id.strip():
        raise ScanError("Нужен непустой исходный Id")
    if not match_fields or any(
        not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", field) or field.lower() in ("id", "avitoid")
        for field in match_fields
    ):
        raise ScanError("Для поиска той же строки укажите поля, которые сохраняются при очистке Id")

    first_root, first = read_ads(before)
    last_root, last = read_ads(after)
    result = {"before_entries": len(first), "after_entries": len(last)}
    target = [ad for ad in first if values(ad, ("Id",)) == (old_id,)]
    if len(target) != 1:
        return dict(result, outcome="INCONCLUSIVE", reason="BASELINE_ID_NOT_UNIQUE")
    if expected_title is not None and values(target[0], ("Title",)) != (expected_title.strip(),):
        return dict(result, outcome="INCONCLUSIVE", reason="BASELINE_TITLE_CHANGED")
    if first_root.attrib != last_root.attrib:
        return dict(result, outcome="INCONCLUSIVE", reason="FEED_ATTRIBUTES_CHANGED")

    signature = values(target[0], match_fields)
    if not all(signature) or sum(values(ad, match_fields) == signature for ad in first) != 1:
        return dict(result, outcome="INCONCLUSIVE", reason="BASELINE_POSITION_NOT_UNIQUE")

    if any(values(ad, ("Id",)) == (old_id,) for ad in last):
        return dict(result, outcome="ORIGINAL_ID_PRESENT")
    same_position = [ad for ad in last if values(ad, match_fields) == signature]
    if len(same_position) > 1:
        return dict(result, outcome="INCONCLUSIVE", reason="AFTER_POSITION_NOT_UNIQUE")
    if same_position:
        found_id = values(same_position[0], ("Id",))[0]
        status = "ROW_PRESENT_WITH_OTHER_ID" if found_id else "ROW_PRESENT_WITHOUT_ID"
        return dict(result, outcome=status)

    counter_fields = ("Id", *match_fields)
    others = Counter(values(ad, counter_fields) for ad in first if ad is not target[0])
    if not others:
        return dict(result, outcome="INCONCLUSIVE", reason="NO_CONTROL_ROWS")
    if others != Counter(values(ad, counter_fields) for ad in last):
        return dict(result, outcome="INCONCLUSIVE", reason="OTHER_ROWS_CHANGED")
    return dict(result, outcome="ROW_ABSENT_FROM_XML")
