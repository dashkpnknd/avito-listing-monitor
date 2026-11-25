"""Run the production sync schedule and generate reports from committed state."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .avito import AvitoStatuses
from .drive import DriveChanges
from .project_cycle import can_notify, reconcile_project
from .removals import follow_operations
from .report import included, report
from .schedule import commit_daily, due_jobs, remember
from .storage import encode
from .sync import ReadBudget, discover_drives, report_health, run_refreshes, stage_projects, sync_journal, uses_drive


def poll_operations(store, config, projects, cutoff, observer):
    settings = config["sync"]
    candidates = store.db.execute(
        """SELECT DISTINCT i.project,s.due,s.diagnostics FROM id_registry i
           JOIN sync_projects p USING(project) LEFT JOIN sync_avito s USING(project)
           WHERE i.state='WAITING_AVITO_CONFIRMATION' AND (s.due IS NULL OR s.due<=?)
           ORDER BY coalesce(s.due,0),i.project LIMIT ?""",
        (cutoff, settings["max_projects_per_tick"]),
    ).fetchall()
    for row in candidates:
        project = projects[row["project"]]
        if not project.get("avito_credential_prefix"):
            continue
        diagnostics = follow_operations(store, project, observer, {}, recover_write=False)
        encoded = encode(diagnostics)
        with store.db:
            store.db.execute(
                "INSERT OR REPLACE INTO sync_avito VALUES (?,?,?)",
                (project["id"], cutoff + settings["avito_poll_minutes"] * 60, encoded),
            )
            if diagnostics and row["diagnostics"] in (None, "[]"):
                timestamp = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
                if can_notify(config, project, timestamp, False):
                    body = f'⚠️ {project["name"]} · не удалось проверить снятие через Avito'
                    store.db.execute(
                        "INSERT OR IGNORE INTO deliveries(key,body) VALUES (?,?)",
                        (f'event:{project["id"]}:avito:{cutoff}', body),
                    )
    with store.db:
        store.db.execute(
            """DELETE FROM sync_avito WHERE NOT EXISTS (
                   SELECT 1 FROM id_registry WHERE project=sync_avito.project AND state='WAITING_AVITO_CONFIRMATION'
               )"""
        )


def saved_report(store, config, active, cutoff, *, fixture=False, detailed=False, first_report=False):
    with store.db:
        store.db.execute("BEGIN")
        return report(
            store, active, cutoff, config["report_timezone"],
            max_age=timedelta(minutes=config["sync"]["max_snapshot_age_minutes"]),
            detailed=detailed, first_report=first_report,
            health=lambda project: report_health(
                store, config, project, int(cutoff.timestamp()), check_journal=not fixture,
            ),
        )


def daily_if_due(store, config, active, cutoff, *, queue, fixture=False):
    _, day = due_jobs(store, config, cutoff, queue=queue)
    if not day:
        return []
    local_day = cutoff.astimezone(ZoneInfo(config["report_timezone"])).date()
    if queue and not any(included(project, local_day) for project in active):
        return []
    bodies = saved_report(store, config, active, cutoff, fixture=fixture)
    if queue:
        bodies = commit_daily(store, day, cutoff.isoformat(), bodies)
    remember(store, "DAILY_QUEUE" if queue else "DAILY_PREVIEW", day, cutoff.isoformat(), bodies)
    return bodies


def queue_first_report(store, config, project, cutoff):
    project_id = project["id"]
    if store.db.execute(
        "SELECT 1 FROM scheduled_runs WHERE kind='FIRST_REPORT' AND slot=?", (project_id,),
    ).fetchone():
        return True
    snapshot = store.snapshot_summary(project_id)
    if not snapshot or snapshot["config"] != encode(project):
        return False
    if report_health(store, config, project, int(cutoff.timestamp())):
        return False
    bodies = saved_report(store, config, [project], cutoff, first_report=True)
    with store.db:
        store.db.execute(
            "INSERT INTO scheduled_runs(kind,slot,cutoff,data) VALUES ('FIRST_REPORT',?,?,?)",
            (project_id, cutoff.isoformat(), encode(bodies)),
        )
        store.db.executemany(
            "INSERT INTO deliveries(key,body) VALUES (?,?)",
            [(f"first:{project_id}:{index}", body) for index, body in enumerate(bodies, 1)],
        )
    return True


def tick(store, config, active, cutoff, *, reader, fixture=False, drive_factory=DriveChanges):
    projects = {project["id"]: project for project in active}
    at = int(cutoff.timestamp())
    settings = config["sync"]
    budget = ReadBudget(settings["max_sheet_requests_per_tick"])
    stage_projects(store, projects, at)
    drive = None

    def open_drive():
        nonlocal drive
        if drive is None:
            drive = drive_factory()
        return drive

    if not fixture:
        sync_journal(store, settings, {key: p for key, p in projects.items() if uses_drive(p, config)}, at, open_drive)
    observer = None if fixture else AvitoStatuses()

    def open_reader(project):
        result = reader(project)
        if hasattr(result, "budget"):
            result.budget = budget
        return result

    def reconcile(project, positions, diagnostics, checked_at, might_apply, *, has_pending_write):
        # Only an actual read can resolve a write whose outcome was unknown.
        is_read_failure = not positions and any(w["code"] == "READ_ERROR" for w in diagnostics)
        if not fixture and not is_read_failure and uses_drive(project, config):
            discover_drives(store, (project["source_book"], project["target_book"]), open_drive)
        mutated, _, _ = reconcile_project(
            store, project, positions, diagnostics, checked_at, observer=observer, poll=False, budget=budget,
            notify=can_notify(config, project, checked_at, fixture), apply=bool(observer and might_apply),
            recover_write=has_pending_write and not is_read_failure,
        )
        return mutated

    stats = run_refreshes(store, config, projects, at, open_reader, reconcile)
    if observer:
        poll_operations(store, config, projects, at, observer)
    preview = fixture or not config.get("telegram_enabled")
    bodies = daily_if_due(store, config, active, cutoff, queue=False, fixture=fixture) if preview else []
    return stats, bodies
