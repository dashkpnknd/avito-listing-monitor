"""Durable Google change ingestion and bounded, per-project refreshes."""
from datetime import datetime, timezone

from .book_cache import CachedBooks
from .drive import CursorRejected
from .model import ReadQuota, ScanError, scan
from .storage import digest


SOURCE = 1
TARGET = 2
BOTH = SOURCE | TARGET
SCHEMA_VERSION = 1


class ReadBudget:
    def __init__(self, limit):
        self.remaining = limit

    def take(self):
        if self.remaining <= 0:
            raise ReadQuota("Следующий запуск продолжит чтение Google Sheets")
        self.remaining -= 1

    def exhausted(self):
        self.remaining = 0


def uses_drive(project, config):
    return project["source_book"] not in config.get("google_xlsx_exports", {})


def enqueue(store, project, mask, due, *, changed):
    store.db.execute(
        """INSERT INTO sync_tasks(project,mask,changed,due) VALUES (?,?,?,?)
           ON CONFLICT(project) DO UPDATE SET
           mask=sync_tasks.mask | excluded.mask,
           changed=max(sync_tasks.changed,excluded.changed),
           due=CASE WHEN sync_tasks.attempts=0 THEN min(sync_tasks.due,excluded.due) ELSE sync_tasks.due END""",
        (project, mask, int(changed), due),
    )


def stage_projects(store, projects, cutoff):
    known = {row["project"]: row for row in store.db.execute("SELECT * FROM sync_projects")}
    with store.db:
        for project_id in known.keys() - projects.keys():
            for table in ("sync_tasks", "sync_books", "sync_projects", "sync_avito"):
                store.db.execute(f"DELETE FROM {table} WHERE project=?", (project_id,))
        for project_id, project in projects.items():
            fingerprint = digest([SCHEMA_VERSION, project])
            previous = known.get(project_id)
            if previous is None or previous["fingerprint"] != fingerprint:
                store.db.execute("DELETE FROM sync_books WHERE project=?", (project_id,))
                store.db.execute("DELETE FROM sync_tasks WHERE project=?", (project_id,))
                store.db.execute("DELETE FROM sync_avito WHERE project=?", (project_id,))
                store.db.execute(
                    "INSERT OR REPLACE INTO sync_projects VALUES (?,?,?,NULL)", (project_id, fingerprint, cutoff),
                )
                enqueue(store, project_id, BOTH, cutoff, changed=True)
            elif previous["audit_due"] <= cutoff:
                enqueue(store, project_id, BOTH, cutoff, changed=False)
            elif previous["formula_due"] is not None and previous["formula_due"] <= cutoff:
                enqueue(store, project_id, SOURCE, cutoff, changed=False)


def file_routes(projects):
    by_file = {}
    for project in projects.values():
        for role, mask in (("source_book", SOURCE), ("target_book", TARGET)):
            by_file.setdefault(project[role], []).append((project["id"], mask))
    return by_file


def sync_journal(store, settings, projects, cutoff, drive):
    by_file = file_routes(projects)
    if not by_file:
        return
    known = {row["book"]: row["stream"] for row in store.db.execute("SELECT * FROM sync_file_drives")}
    names = {"", *settings.get("shared_drive_ids", []), *(known[book] for book in by_file.keys() & known.keys())}
    for name in sorted(names):
        stream = name or None
        scoped = by_file if not name else {
            book: entries for book, entries in by_file.items() if known.get(book) in (None, name)
        }
        state = store.db.execute("SELECT * FROM sync_journal WHERE stream=?", (name,)).fetchone()
        if state and state["poll_due"] > cutoff:
            continue
        cursor = state["cursor"] if state else ""
        last = state["caught_up"] if state else None
        try:
            if not cursor:
                cursor = drive().start(stream)
                with store.db:
                    if state or name:
                        for entries in scoped.values():
                            for project, mask in entries:
                                enqueue(store, project, mask, cutoff, changed=True)
                    store.db.execute("INSERT OR REPLACE INTO sync_journal VALUES (?,?,?,?,NULL)", (name, cursor, cutoff, None))
            for _ in range(settings["max_change_pages"]):
                next_cursor, more, files = drive().page(stream, cursor)
                with store.db:
                    for file in files:
                        for project, mask in scoped.get(file, ()):
                            enqueue(store, project, mask, cutoff, changed=True)
                    last = None if more else cutoff
                    next_poll = cutoff if more else cutoff + settings["poll_every_seconds"]
                    store.db.execute(
                        "INSERT OR REPLACE INTO sync_journal VALUES (?,?,?,?,NULL)",
                        (name, next_cursor, next_poll, last),
                    )
                cursor = next_cursor
                if not more:
                    break
        except CursorRejected as exc:
            with store.db:
                for entries in scoped.values():
                    for project, mask in entries:
                        enqueue(store, project, mask, cutoff, changed=True)
                store.db.execute(
                    "INSERT OR REPLACE INTO sync_journal VALUES (?,?,?,?,?)",
                    (name, "", cutoff + settings["poll_every_seconds"], None, str(exc)),
                )
        except ScanError as exc:
            with store.db:
                store.db.execute(
                    "INSERT OR REPLACE INTO sync_journal VALUES (?,?,?,?,?)",
                    (name, cursor, cutoff + max(60, settings["poll_every_seconds"]), last, str(exc)),
                )


def discover_drives(store, books, drive):
    for book in books:
        if store.db.execute("SELECT 1 FROM sync_file_drives WHERE book=?", (book,)).fetchone():
            continue
        try:
            stream = drive().file_drive(book)
        except ScanError:
            continue
        with store.db:
            store.db.execute("INSERT OR IGNORE INTO sync_file_drives VALUES (?,?)", (book, stream))


def pending(store, limit, cutoff):
    return store.db.execute(
        "SELECT * FROM sync_tasks WHERE due<=? ORDER BY due,changed DESC,project LIMIT ?", (cutoff, limit),
    ).fetchall()


def save_refresh(store, project, books, mask, cutoff, audit_seconds, formula_due):
    with store.db:
        store.db.executemany(
            "INSERT OR REPLACE INTO sync_books VALUES (?,?,?,?)",
            [(project, book, blob, cutoff) for book, blob in books.items()],
        )
        if mask == BOTH:
            store.db.execute("UPDATE sync_projects SET audit_due=? WHERE project=?", (cutoff + audit_seconds, project))
        if mask & SOURCE:
            store.db.execute("UPDATE sync_projects SET formula_due=? WHERE project=?", (formula_due, project))
        store.db.execute("DELETE FROM sync_tasks WHERE project=?", (project,))


def retry(store, task, cutoff, error, *, quota=False):
    attempts = task["attempts"] + int(not quota)
    delay = 60 if quota else min(1800, 60 * 2 ** min(attempts - 1, 5))
    with store.db:
        store.db.execute(
            "UPDATE sync_tasks SET attempts=?,due=?,error=? WHERE project=?",
            (attempts, cutoff + delay, error, task["project"]),
        )


def run_refreshes(store, config, projects, cutoff, read, reconcile):
    settings = config["sync"]
    stats = {"refreshed": 0, "failed": 0, "deferred": 0}
    for task in pending(store, settings["max_projects_per_tick"], cutoff):
        project_id, mask = task["project"], task["mask"]
        project = projects[project_id]
        previous = {row["book"]: row["data"] for row in store.db.execute(
            "SELECT book,data FROM sync_books WHERE project=?", (project_id,),
        )}
        refresh = set()
        for role, flag in (("source_book", SOURCE), ("target_book", TARGET)):
            book = project[role]
            if mask & flag or book not in previous:
                refresh.add(book)
                mask |= flag
        try:
            reader = read(project)
            if hasattr(reader, "begin_scan"):
                reader.begin_scan()
            adapter = CachedBooks(reader, previous, refresh)
            positions, diagnostics = scan(project, adapter)
            if adapter.quota:
                raise adapter.quota
            if adapter.failed:
                raise ScanError(next(
                    (item["detail"] for item in diagnostics if item["code"] == "READ_ERROR"),
                    "Чтение изменившегося файла не завершено",
                ))
        except ReadQuota as exc:
            retry(store, task, cutoff, str(exc), quota=True)
            stats["deferred"] += 1
            continue
        except ScanError as exc:
            at = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
            reconcile(project, {}, [{"code": "READ_ERROR", "detail": str(exc)}], at, False, has_pending_write=False)
            retry(store, task, cutoff, str(exc))
            stats["failed"] += 1
            continue

        blobs = adapter.blobs()
        checked_at = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        changed_books = any(previous.get(book) != blob for book, blob in blobs.items())
        needs_candidate_retry = bool(project.get("auto_remove") and any(
            p.issue == "NEEDS_REMOVAL" for p in positions.values()
        ))
        has_pending_write = store.db.execute(
            "SELECT 1 FROM id_registry WHERE project=? AND state='WRITE_STARTED'", (project_id,),
        ).fetchone()
        mutated = reconcile(project, positions, diagnostics, checked_at,
                            changed_books or needs_candidate_retry, has_pending_write=bool(has_pending_write))
        drive_backed = uses_drive(project, config)
        audit_minutes = settings["audit_every_minutes"] if drive_backed else settings["export_every_minutes"]
        formula_due = None
        if drive_backed and any("formulaValue" in p.price_cell.get("userEnteredValue", {}) for p in positions.values()):
            formula_due = cutoff + settings["formula_every_minutes"] * 60
        save_refresh(store, project_id, blobs, mask, cutoff, audit_minutes * 60, formula_due)
        if mutated:
            with store.db:
                enqueue(store, project_id, TARGET, cutoff, changed=True)
        stats["refreshed"] += 1
    return stats


def report_health(store, config, project, cutoff, *, check_journal=True):
    settings = config["sync"]
    task = store.db.execute("SELECT changed,error FROM sync_tasks WHERE project=?", (project["id"],)).fetchone()
    if task and (task["changed"] or task["error"]):
        return "обновление в очереди" if not task["error"] else "не удалось обновить таблицы"
    checked = store.db.execute(
        "SELECT count(*),min(checked) FROM sync_books WHERE project=?", (project["id"],),
    ).fetchone()
    if checked[0] != 2:
        return "ожидает первой полной проверки"
    drive_backed = uses_drive(project, config)
    max_age = settings["max_snapshot_age_minutes"] if drive_backed else settings["export_every_minutes"] + 30
    if cutoff - checked[1] > max_age * 60:
        return "данные одной из таблиц устарели"
    formula = store.db.execute("SELECT formula_due FROM sync_projects WHERE project=?", (project["id"],)).fetchone()
    if formula and formula[0] is not None and cutoff - formula[0] > 30 * 60:
        return "результаты формул прайса ждут проверки"
    if check_journal and drive_backed:
        routes = store.db.execute("SELECT stream FROM sync_file_drives WHERE book IN (?,?)",
                                  (project["source_book"], project["target_book"])).fetchall()
        streams = {row["stream"] for row in routes}
        if len(routes) < 2:
            streams.update(("", *settings.get("shared_drive_ids", [])))
        for name in streams:
            state = store.db.execute("SELECT caught_up,error FROM sync_journal WHERE stream=?", (name,)).fetchone()
            if (not state or state["error"] or state["caught_up"] is None
                    or cutoff - state["caught_up"] > settings["max_journal_age_minutes"] * 60):
                return "журнал изменений Google Drive пока не актуален"
    return None
