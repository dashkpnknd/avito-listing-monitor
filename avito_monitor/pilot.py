import json
import uuid
import xml.etree.ElementTree as ET

from .adapters import Fixture
from .model import ScanError, id_state, scan


def single_removal(store, adapter, project, target_link, operation):
    """Exercise the reserve and clear protocol using exactly one local position."""
    if not isinstance(adapter, Fixture):
        raise ScanError("Очистка доступна только для локальной копии")
    if str(uuid.UUID(operation)) != operation:
        raise ValueError("Требуется UUID операции")
    prior = store.db.execute("SELECT * FROM id_registry WHERE operation=?", (operation,)).fetchone()
    if prior:
        data = json.loads(prior["data"])
        if prior["project"] != project["id"] or data["target_link"] != target_link:
            raise ScanError("Операция принадлежит другой позиции")
        if prior["state"] != "RESERVED":
            return prior["state"]
    positions, diagnostics = scan(project, adapter)
    if diagnostics:
        raise ScanError("Сопоставление неполное. Очистка заблокирована.")
    candidates = [pos for pos in positions.values() if pos.target.link == target_link]
    if len(candidates) != 1 or candidates[0].issue != "NEEDS_REMOVAL":
        raise ScanError("Для позиции не подтверждена пустая цена и непустой Id")
    position = candidates[0]
    refreshed, diagnostics = scan(project, adapter)
    if diagnostics or refreshed.get(position.identity) != position:
        raise ScanError("Данные изменились перед резервированием")
    if store.reserve(operation, project["id"], position) != "RESERVED":
        raise ScanError("Резервирование не подтверждено")
    refreshed, diagnostics = scan(project, adapter)
    if diagnostics or refreshed.get(position.identity) != position:
        raise ScanError("Данные изменились перед очисткой")
    store.transition(operation, "RESERVED", "WRITE_STARTED")
    adapter.clear_id(position.target, position.id_cell)
    refreshed, diagnostics = scan(project, adapter)
    after = refreshed.get(position.identity)
    if diagnostics or not after or after.target != position.target or id_state(after.id_cell) != "EMPTY":
        raise ScanError("Результат очистки требует ручной проверки")
    store.transition(operation, "WRITE_STARTED", "WAITING_FEED_CONFIRMATION")
    return "WAITING_FEED_CONFIRMATION"


def inspect_feed(path, old_id, title):
    """Never infer Avito status from the XML. Check both Id and a known unique Title."""
    root = ET.parse(path).getroot()
    if root.tag != "Ads":
        raise ScanError("Ожидался XML с корнем Ads")
    ads = root.findall("Ad")
    return {
        "entries": len(ads),
        "old_id_present": any((ad.findtext("Id") or "").strip() == old_id for ad in ads),
        "same_title_present": any((ad.findtext("Title") or "").strip() == title for ad in ads),
        "missing_or_blank_id": sum(not (ad.findtext("Id") or "").strip() for ad in ads),
    }
