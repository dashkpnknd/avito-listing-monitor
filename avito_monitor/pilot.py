import json
import uuid

from .adapters import Fixture, GoogleRemovalWriter
from .feed_evidence import read_ads
from .model import ScanError, id_state, removal_blocked, scan
from .removals import avito_observation


def single_removal(
    store, adapter, project, target_link, operation, *,
    trigger="MANUAL_FIXTURE_PILOT", active_evidence=None, planned_position=None,
):
    """Reserve the original value and clear exactly one revalidated technical Id."""
    if not isinstance(adapter, Fixture) and not (
        isinstance(adapter, GoogleRemovalWriter) and project.get("auto_remove")
        and project.get("removal_confirmation") == "avito_status"
    ):
        raise ScanError("Очистка недоступна для этого проекта")
    if str(uuid.UUID(operation)) != operation:
        raise ValueError("Требуется UUID операции")
    prior = store.db.execute("SELECT * FROM id_registry WHERE operation=?", (operation,)).fetchone()
    if prior:
        data = json.loads(prior["data"])
        if prior["project"] != project["id"] or data["target_link"] != target_link:
            raise ScanError("Операция принадлежит другой позиции")
        if prior["state"] != "RESERVED":
            return prior["state"]
    def rescan():
        if hasattr(adapter, "begin_scan"):
            adapter.begin_scan()
        return scan(project, adapter)

    positions, diagnostics = rescan()
    if removal_blocked(project, diagnostics):
        raise ScanError("Сопоставление неполное. Очистка заблокирована.")
    candidates = [pos for pos in positions.values() if pos.target.link == target_link]
    if len(candidates) != 1 or candidates[0].issue != "NEEDS_REMOVAL":
        raise ScanError("Для позиции не подтверждена пустая цена и непустой Id")
    position = candidates[0]
    if planned_position is not None and position != planned_position:
        raise ScanError("Позиция изменилась после составления плана")
    direct = project.get("removal_confirmation") == "avito_status"
    confirmed_active = None
    if direct:
        _, confirmed_active = avito_observation(position.marketplace_id, active_evidence)
        if confirmed_active["outcome"] != "ACTIVE":
            raise ScanError("Перед очисткой нужен подтверждённый активный статус объявления")
    refreshed, diagnostics = rescan()
    if removal_blocked(project, diagnostics) or refreshed.get(position.identity) != position:
        raise ScanError("Данные изменились перед резервированием")
    try:
        reserved = store.reserve(operation, project["id"], position, trigger=trigger)
    except ValueError as exc:
        raise ScanError(str(exc)) from None
    if reserved != "RESERVED":
        raise ScanError("Резервирование не подтверждено")
    if confirmed_active:
        store.record_operation_evidence(operation, "RESERVED", "RESERVED", "AVITO_ACTIVE_BEFORE_CLEAR", confirmed_active)
    refreshed, diagnostics = rescan()
    if removal_blocked(project, diagnostics) or refreshed.get(position.identity) != position:
        raise ScanError("Данные изменились перед очисткой")
    store.transition(operation, "RESERVED", "WRITE_STARTED")
    adapter.clear_id(position.target, position.id_cell)
    refreshed, diagnostics = rescan()
    after = refreshed.get(position.identity)
    if removal_blocked(project, diagnostics) or not after or after.target != position.target or id_state(after.id_cell) != "EMPTY":
        raise ScanError("Результат очистки требует ручной проверки")
    waiting = "WAITING_AVITO_CONFIRMATION" if direct else "WAITING_FEED_CONFIRMATION"
    store.record_operation_evidence(operation, "WRITE_STARTED", waiting, "CLEAR_CONFIRMED", {})
    return waiting


def inspect_feed(path, old_id, title):
    """Never infer Avito status from the XML. Check both Id and a known unique Title."""
    _, ads = read_ads(path)
    return {
        "entries": len(ads),
        "old_id_present": any((ad.findtext("Id") or "").strip() == old_id for ad in ads),
        "same_title_present": any((ad.findtext("Title") or "").strip() == title for ad in ads),
        "missing_or_blank_id": sum(not (ad.findtext("Id") or "").strip() for ad in ads),
    }
