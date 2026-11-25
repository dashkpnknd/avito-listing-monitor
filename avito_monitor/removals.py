"""Safety checks and evidence for a proposed technical Id removal."""
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import hashlib
import json
import uuid

from .feed_evidence import compare_feeds
from .model import ScanError, cell_value, id_state, removal_blocked
from .storage import digest, encode


def plan_removals(store, project, positions, diagnostics):
    candidates = [p for p in positions.values() if p.issue == "NEEDS_REMOVAL"]
    result = {
        "project": project["id"],
        "candidates": [{"title": p.title, "target": p.target.link} for p in candidates],
        "reasons": [],
    }
    reasons = result["reasons"]
    hold = store.removal_hold(project["id"])
    if hold:
        reasons.append("REVIEW_HOLD")
    if project["mode"] != "monitoring":
        reasons.append("PROJECT_NOT_MONITORING")
    if removal_blocked(project, diagnostics):
        reasons.append("INCOMPLETE_MATCHING")
    limits = project.get("removal_limits")
    if not limits:
        reasons.append("LIMITS_NOT_CONFIGURED")
        return result
    baseline = store.complete_snapshot(project["id"])
    if not baseline or baseline["config"] != encode(project):
        reasons.append("COMPLETE_BASELINE_REQUIRED")
        return result
    previous = json.loads(baseline["data"])
    newly_empty = sum(
        p.state == "EMPTY" and old["state"] == "POSITIVE"
        for key, p in positions.items() if (old := previous.get(key))
    )
    lost = len(previous.keys() - positions.keys())
    result.update(newly_empty=newly_empty, lost_matches=lost, baseline_at=baseline["checked"])
    exceeded = [
        label for label, count, limit in [
            ("TOO_MANY_CANDIDATES", len(candidates), limits["max_candidates"]),
            ("TOO_MANY_NEWLY_EMPTY", newly_empty, limits["max_newly_empty"]),
            ("TOO_MANY_LOST_MATCHES", lost, limits["max_lost_matches"]),
        ] if count > limit
    ]
    reasons.extend(exceeded)
    if exceeded:
        store.place_removal_hold(project["id"], result)
    return result


def rehearse_removals(store, adapter, project, positions, plan, output_path):
    from .adapters import Fixture
    from .pilot import single_removal

    if not isinstance(adapter, Fixture) or plan["reasons"]:
        raise ScanError("План нельзя выполнить. Очистка доступна только на локальной копии")
    candidates = []
    for position in positions.values():
        if position.issue != "NEEDS_REMOVAL":
            continue
        latest = store.latest_backup(project["id"], position.identity)
        if latest and latest["state"] not in {"RESTORED", "RESERVED"}:
            raise ScanError("Для позиции уже сохранена операция. Проверьте её прежде, чем начинать повторную")
        origin = [project["id"], position.identity, position.id_cell, latest["operation"] if latest else ""]
        operation = (
            latest["operation"] if latest and latest["state"] == "RESERVED"
            else str(uuid.uuid5(uuid.NAMESPACE_URL, digest(origin)))
        )
        candidates.append((position, operation))
    if not candidates:
        return []
    adapter.save_new_copy(output_path)
    result = []
    for position, operation in candidates:
        state = single_removal(
            store, adapter, project, position.target.link, operation, trigger="AUTOMATED_FIXTURE_PILOT",
        )
        result.append({"operation": operation, "target": position.target.link, "state": state})
    return result


def apply_scheduled_removals(store, project, adapter, observer, positions):
    from .pilot import single_removal

    changed = False
    for position in positions.values():
        if position.issue != "NEEDS_REMOVAL":
            continue
        latest = store.latest_backup(project["id"], position.identity)
        if latest and latest["state"] not in {"RESTORED", "RESERVED"}:
            continue
        old_id = cell_value(position.id_cell)
        observation = observer.observe(project, position.marketplace_id, old_id, require_binding=True)
        operation = latest["operation"] if latest and latest["state"] == "RESERVED" else str(uuid.uuid4())
        single_removal(
            store, adapter, project, position.target.link, operation,
            trigger="SCHEDULED_GOOGLE", active_evidence=observation, planned_position=position,
        )
        changed = True
    return changed


def follow_operations(store, project, observer, positions, *, poll=True, recover_write=True):
    rows = store.db.execute(
        """SELECT * FROM id_registry WHERE project=? AND state IN (
               'WRITE_STARTED', 'WAITING_AVITO_CONFIRMATION', 'REMOVED_FROM_AVITO'
           ) ORDER BY saved""", (project["id"],),
    ).fetchall()
    warnings = []
    for row in rows:
        data = json.loads(row["data"])
        current = positions.get(row["identity"])
        same_id = current and (
            current.id_cell.get("userEnteredValue") == data["id_cell"].get("userEnteredValue")
        )
        if same_id and current.state == "POSITIVE":
            record_manual_restore(store, project, row["operation"], positions)
            continue
        if row["state"] == "REMOVED_FROM_AVITO":
            continue
        try:
            if row["state"] == "WRITE_STARTED":
                if not recover_write:
                    continue
                if (
                    not current or current.target.link != data["target_link"] or current.state != "EMPTY"
                    or id_state(current.id_cell) != "EMPTY"
                    or not store.latest_operation_event(row["operation"], "AVITO_ACTIVE_BEFORE_CLEAR")
                ):
                    raise ScanError("Нужна проверка результата предыдущей записи")
                store.record_operation_evidence(
                    row["operation"], "WRITE_STARTED", "WAITING_AVITO_CONFIRMATION", "CLEAR_CONFIRMED", {},
                )
            if not poll:
                continue
            observation = observer.observe(project, data.get("marketplace_id"), cell_value(data["id_cell"]))
            previous = store.latest_operation_event(row["operation"], "AVITO_OBSERVATION")
            if not previous or json.loads(previous["evidence"])["outcome"] != observation["outcome"]:
                record_avito_evidence(store, project, row["operation"], observation)
        except ScanError as exc:
            warnings.append({"code": "REMOVAL_STATUS", "detail": str(exc), "links": [data["target_link"]]})
    return warnings


def operation_for(store, operation, project):
    row = store.get_operation(operation)
    if not row or row["project"] != project["id"]:
        raise ScanError("Операция не принадлежит указанному проекту")
    return row, json.loads(row["data"])


def record_feed_evidence(store, project, operation, before, after, match_fields):
    row, data = operation_for(store, operation, project)
    if store.latest_operation_event(operation, "AVITO_ACTIVE_BEFORE_CLEAR"):
        raise ScanError("Для этой операции выбрана прямая проверка статуса Авито")
    if row["state"] not in {"WAITING_FEED_CONFIRMATION", "WAITING_AVITO_CONFIRMATION"}:
        raise ScanError("Операция не ожидает подтверждения XML")
    old_id = cell_value(data["id_cell"])
    # Run the verdict on immutable bytes that are also hashed for the journal.
    first, last = Path(before).read_bytes(), Path(after).read_bytes()
    evidence = compare_feeds(
        BytesIO(first), BytesIO(last), str(old_id), match_fields, expected_title=data["title"],
    )
    evidence["before_sha256"] = hashlib.sha256(first).hexdigest()
    evidence["after_sha256"] = hashlib.sha256(last).hexdigest()
    evidence["match_fields"] = list(match_fields)
    new = (
        "WAITING_AVITO_CONFIRMATION" if evidence["outcome"] == "ROW_ABSENT_FROM_XML"
        else "WAITING_FEED_CONFIRMATION"
    )
    store.record_operation_evidence(operation, row["state"], new, "FEED_EVIDENCE", evidence)
    return dict(evidence, state=new)


def avito_observation(listing, observation):
    if not isinstance(observation, dict):
        raise ScanError("Нужно наблюдение за статусом объявления")
    if not listing or observation.get("marketplace_id") != listing:
        raise ScanError("Номер объявления не совпадает с привязкой, сохранённой до очистки")
    source = observation.get("source")
    result = observation.get("outcome")
    allowed = {
        "price-monitor-active-list": {"ACTIVE", "NOT_SEEN"},
        "item-status": {"ACTIVE", "INACTIVE_CONFIRMED", "UNKNOWN"},
        "autoload-report": {"ACTIVE", "INACTIVE_CONFIRMED", "UNKNOWN"},
    }
    if result not in allowed.get(source, set()):
        raise ScanError("Этот источник не подтверждает указанный статус")
    try:
        checked = datetime.fromisoformat(observation["observed_at"].replace("Z", "+00:00"))
        if checked.tzinfo is None or checked > datetime.now(timezone.utc):
            raise ValueError
    except (KeyError, AttributeError, ValueError):
        raise ScanError("Укажите фактическое время наблюдения с часовым поясом ISO 8601") from None
    if not isinstance(observation.get("reference"), str) or not observation["reference"].strip():
        raise ScanError("Нужна ссылка или номер записи проверки")
    evidence = {key: observation[key] for key in ("source", "outcome", "marketplace_id", "observed_at", "reference")}
    return checked, evidence


def record_avito_evidence(store, project, operation, observation):
    row, data = operation_for(store, operation, project)
    if row["state"] != "WAITING_AVITO_CONFIRMATION":
        raise ScanError("Операция ещё не ожидает подтверждения статуса Авито")
    checked, evidence = avito_observation(data.get("marketplace_id"), observation)
    direct = store.latest_operation_event(operation, "AVITO_ACTIVE_BEFORE_CLEAR")
    milestone = store.latest_operation_event(operation, "CLEAR_CONFIRMED" if direct else "FEED_EVIDENCE")
    if not milestone or checked < datetime.fromisoformat(milestone["at"]):
        raise ScanError("Статус нужно проверить после подтверждённой очистки Id" if direct else
                        "Объявление нужно проверить после подтверждения итогового XML")
    previous = store.latest_operation_event(operation, "AVITO_OBSERVATION")
    if previous and checked < datetime.fromisoformat(json.loads(previous["evidence"])["observed_at"].replace("Z", "+00:00")):
        raise ScanError("Уже записано более позднее наблюдение")
    next_state = "REMOVED_FROM_AVITO" if evidence["outcome"] == "INACTIVE_CONFIRMED" else row["state"]
    store.record_operation_evidence(operation, row["state"], next_state, "AVITO_OBSERVATION", evidence)
    return dict(evidence, state=next_state)


def record_manual_restore(store, project, operation, positions):
    row, data = operation_for(store, operation, project)
    current = positions.get(row["identity"])
    if (
        not current or current.state != "POSITIVE"
        or id_state(current.id_cell) != "PRESENT"
        or current.id_cell.get("userEnteredValue") != data["id_cell"].get("userEnteredValue")
    ):
        raise ScanError("Не подтверждены вернувшаяся цена и точный исходный Id")
    if row["state"] == "RESTORED":
        return "RESTORED"
    if row["state"] not in {"WRITE_STARTED", "WAITING_FEED_CONFIRMATION", "WAITING_AVITO_CONFIRMATION", "REMOVED_FROM_AVITO"}:
        raise ScanError("Эта операция ещё не дошла до очистки Id")
    store.record_operation_evidence(
        operation, row["state"], "RESTORED", "MANUAL_RESTORE_OBSERVED", {"target": current.target.link},
    )
    return "RESTORED"
