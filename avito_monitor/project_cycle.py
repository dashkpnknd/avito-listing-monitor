"""Apply the same incident and removal rules to scheduled and manual scans."""
from datetime import datetime
from zoneinfo import ZoneInfo

from .adapters import GoogleRemovalWriter
from .model import ScanError, scan
from .removals import apply_scheduled_removals, follow_operations, plan_removals
from .report import included
from .storage import encode


def can_notify(config, project, checked_at, fixture):
    local_day = datetime.fromisoformat(checked_at).astimezone(ZoneInfo(config["report_timezone"])).date()
    return (
        project["mode"] == "monitoring" and config.get("telegram_enabled") and not fixture
        and project.get("notifications_enabled", True)
        and (project.get("notify_during_grace", True) or included(project, local_day))
    )


def save_plan(store, plan, checked_at):
    with store.db:
        store.db.execute(
            "INSERT INTO removal_plans(project,created,details) VALUES (?,?,?)",
            (plan["project"], checked_at, encode(plan)),
        )


def reconcile_project(store, project, positions, diagnostics, checked_at, *,
                      notify=False, observer=None, apply=False, poll=True, recover_write=True, budget=None):
    mutated = False
    if project.get("removal_limits"):
        plan = plan_removals(store, project, positions, diagnostics)
        save_plan(store, plan, checked_at)
        if apply and project.get("auto_remove") and not plan["reasons"] and plan["candidates"]:
            try:
                writer = GoogleRemovalWriter(project["target_book"])
                writer.budget = budget
                actual, warnings = scan(project, writer)
                fresh_plan = plan_removals(store, project, actual, warnings)
                save_plan(store, fresh_plan, checked_at)
                if not fresh_plan["reasons"]:
                    eligible = {key: pos for key, pos in actual.items() if pos == positions.get(key)}
                    # From this point an exception may follow a completed write. The caller
                    # must refresh the target before accepting another cached snapshot.
                    mutated = True
                    mutated = apply_scheduled_removals(store, project, writer, observer, eligible)
                    if mutated:
                        writer.begin_scan()
                        positions, diagnostics = scan(project, writer)
            except ScanError as exc:
                diagnostics.append({"code": "REMOVAL_STATUS", "detail": str(exc)})
        if store.removal_hold(project["id"]):
            diagnostics.append({"code": "REMOVAL_HOLD", "detail": "Операции с Id ждут ручной проверки пределов"})
    if observer is not None and project.get("avito_credential_prefix"):
        diagnostics.extend(follow_operations(store, project, observer, positions, poll=poll, recover_write=recover_write))
    store.reconcile(project, positions, diagnostics, checked_at, notify)
    return mutated, positions, diagnostics
