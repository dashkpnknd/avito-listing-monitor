from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import hashlib
import json
import os
import sqlite3

from .model import cell_value, id_state, removal_blocked


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@contextmanager
def locked_db(path, *, role=None):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    suffix = f".{role}.lock" if role else ".lock"
    fd = os.open(str(path) + suffix, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd_db = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd_db)
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        try:
            yield Store(db)
        finally:
            db.close()
    finally:
        os.close(fd)


class Store:
    def __init__(self, db):
        self.db = db
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS snapshots (
                project TEXT PRIMARY KEY, attempted TEXT NOT NULL, successful TEXT,
                config TEXT NOT NULL, data TEXT NOT NULL, diagnostics TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS complete_snapshots (
                project TEXT PRIMARY KEY, checked TEXT NOT NULL,
                config TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS health (
                project TEXT PRIMARY KEY, episode INTEGER NOT NULL, since TEXT NOT NULL, closed TEXT
            );
            CREATE TABLE IF NOT EXISTS incidents (
                project TEXT NOT NULL, identity TEXT NOT NULL, cycle INTEGER NOT NULL,
                kind TEXT NOT NULL, opened TEXT NOT NULL, seen TEXT NOT NULL,
                closed TEXT, data TEXT NOT NULL,
                PRIMARY KEY(project, identity, cycle)
            );
            CREATE INDEX IF NOT EXISTS incidents_open ON incidents(project,kind,identity) WHERE closed IS NULL;
            CREATE TABLE IF NOT EXISTS id_registry (
                operation TEXT PRIMARY KEY, project TEXT NOT NULL, identity TEXT NOT NULL,
                saved TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS id_operation_events (
                seq INTEGER PRIMARY KEY, operation TEXT NOT NULL,
                at TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, evidence TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS id_registry_project_state ON id_registry(project,state);
            CREATE TABLE IF NOT EXISTS removal_plans (
                seq INTEGER PRIMARY KEY, project TEXT NOT NULL, created TEXT NOT NULL, details TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS removal_holds (
                seq INTEGER PRIMARY KEY, project TEXT NOT NULL, raised TEXT NOT NULL,
                details TEXT NOT NULL, released TEXT, release_reason TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_removal_hold
                ON removal_holds(project) WHERE released IS NULL;
            CREATE TABLE IF NOT EXISTS deliveries (
                key TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
                tries INTEGER NOT NULL DEFAULT 0, receipt TEXT
            );
            CREATE TABLE IF NOT EXISTS delivery_attempts (
                key TEXT NOT NULL, attempt INTEGER NOT NULL, at TEXT NOT NULL,
                outcome TEXT NOT NULL, PRIMARY KEY(key, attempt)
            );
            CREATE TABLE IF NOT EXISTS scheduled_runs (
                kind TEXT NOT NULL, slot TEXT NOT NULL, cutoff TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(kind, slot)
            );
            CREATE TABLE IF NOT EXISTS daily (
                day TEXT PRIMARY KEY, cutoff TEXT NOT NULL, bodies TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_projects (
                project TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, audit_due INTEGER NOT NULL, formula_due INTEGER
            );
            CREATE TABLE IF NOT EXISTS sync_books (
                project TEXT NOT NULL, book TEXT NOT NULL, data BLOB NOT NULL, checked INTEGER NOT NULL,
                PRIMARY KEY(project, book)
            );
            CREATE TABLE IF NOT EXISTS sync_tasks (
                project TEXT PRIMARY KEY, mask INTEGER NOT NULL, changed INTEGER NOT NULL,
                due INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT
            );
            CREATE INDEX IF NOT EXISTS sync_tasks_due ON sync_tasks(due, changed);
            CREATE TABLE IF NOT EXISTS sync_journal (
                stream TEXT PRIMARY KEY, cursor TEXT NOT NULL, poll_due INTEGER NOT NULL,
                caught_up INTEGER, error TEXT
            );
            CREATE TABLE IF NOT EXISTS sync_file_drives (
                book TEXT PRIMARY KEY, stream TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_avito (
                project TEXT PRIMARY KEY, due INTEGER NOT NULL, diagnostics TEXT NOT NULL
            );
        """)

    def reconcile(self, project, positions, diagnostics, checked_at, notifications=False):
        project_id = project["id"]
        prior = self.db.execute("SELECT * FROM snapshots WHERE project=?", (project_id,)).fetchone()
        baseline = not prior or not prior["successful"] or prior["config"] != encode(project)
        changes = []
        prior_data = json.loads(prior["data"]) if prior and prior["successful"] else {}
        with self.db:
            self.record_health(project, diagnostics, checked_at, notifications, prior)
            for identity, position in positions.items():
                if position.state not in ("POSITIVE", "EMPTY") or id_state(position.id_cell) == "INVALID":
                    continue
                old = prior_data.get(identity)
                if (
                    old and not position.issue and not old["issue"] and old["state"] in ("POSITIVE", "EMPTY")
                    and id_state(old["id_cell"]) != "INVALID"
                ):
                    continue
                previous = self.db.execute(
                    """SELECT * FROM incidents WHERE project=? AND identity=?
                       ORDER BY cycle DESC LIMIT 1""", (project_id, identity)
                ).fetchone()
                current = previous and not previous["closed"]
                if current and previous["kind"] != position.issue:
                    self.db.execute(
                        "UPDATE incidents SET closed=? WHERE project=? AND identity=? AND cycle=?",
                        (checked_at, project_id, identity, previous["cycle"]),
                    )
                    previous_key = digest([project_id, identity, previous["cycle"]])
                    self.db.execute(
                        "UPDATE deliveries SET state='SUPERSEDED' WHERE key=? AND state IN ('PENDING','RATE_LIMIT')",
                        (f"event:{project_id}:{previous_key}",),
                    )
                    current = False
                if not position.issue:
                    continue
                detail = {
                    "title": position.title, "category": position.category,
                    "source": position.source.link, "target": position.target.link,
                }
                backup = self.latest_backup(project_id, identity)
                if backup and position.issue == "NEEDS_PUBLICATION":
                    detail["restore_available"] = True
                    detail["previous_id"] = cell_value(json.loads(backup["data"])["id_cell"])
                if current:
                    self.db.execute(
                        "UPDATE incidents SET seen=?,data=? WHERE project=? AND identity=? AND cycle=?",
                        (checked_at, encode(detail), project_id, identity, previous["cycle"]),
                    )
                else:
                    cycle = previous["cycle"] + 1 if previous else 1
                    self.db.execute("INSERT INTO incidents VALUES (?,?,?,?,?,?,NULL,?)", (
                        project_id, identity, cycle, position.issue, checked_at, checked_at, encode(detail),
                    ))
                    if not baseline and notifications:
                        changes.append((identity, cycle, position.issue, detail))
            read_failed = any(item["code"] == "READ_ERROR" for item in diagnostics)
            same_config = prior and prior["config"] == encode(project)
            timestamp = checked_at if not read_failed else (prior["successful"] if same_config else None)
            snapshot_data = encode({key: asdict(value) for key, value in positions.items()})
            if not removal_blocked(project, diagnostics):
                self.db.execute(
                    "INSERT OR REPLACE INTO complete_snapshots VALUES (?,?,?,?)",
                    (project_id, checked_at, encode(project), snapshot_data),
                )
            self.db.execute(
                """INSERT INTO snapshots VALUES (?,?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET
                   attempted=excluded.attempted, successful=excluded.successful, config=excluded.config,
                   data=excluded.data, diagnostics=excluded.diagnostics""",
                (project_id, checked_at, timestamp, encode(project),
                 snapshot_data, encode(diagnostics)),
            )
            for identity, cycle, kind, item in changes:
                if kind == "NEEDS_REMOVAL" and not project.get("notify_removal", False):
                    continue
                label = "Нужна выкладка" if kind == "NEEDS_PUBLICATION" else "Убрать из выкладки"
                restore = f'\nПрежний Id для ручного возврата — {item["previous_id"]}' if item.get("restore_available") else ""
                key = digest([project_id, identity, cycle])
                self.db.execute("INSERT OR IGNORE INTO deliveries(key,body) VALUES (?,?)", (
                    f"event:{project_id}:{key}",
                    f'{label} · {project["name"]}\n{item["title"]}\n{item["target"]}{restore}',
                ))

    def record_health(self, project, diagnostics, checked_at, notifications, prior):
        project_id = project["id"]
        latest = self.db.execute("SELECT * FROM health WHERE project=?", (project_id,)).fetchone()
        active_episode = latest and latest["closed"] is None
        codes = {item["code"] for item in diagnostics}
        previous_codes = {item["code"] for item in json.loads(prior["diagnostics"])} if prior else set()
        new_alert = bool(codes - previous_codes - {"MATCH_UNCERTAIN", "UNMAPPED_TARGET"})
        if active_episode and (not diagnostics or new_alert):
            self.db.execute("UPDATE health SET closed=? WHERE project=?", (checked_at, project_id))
            self.db.execute(
                "UPDATE deliveries SET state='SUPERSEDED' WHERE key=? AND state IN ('PENDING','RATE_LIMIT')",
                (f'event:{project_id}:health:{latest["episode"]}',),
            )
        if diagnostics and (not active_episode or new_alert):
            episode = latest["episode"] + 1 if latest else 1
            self.db.execute("INSERT OR REPLACE INTO health VALUES (?,?,?,NULL)", (project_id, episode, checked_at))
            if notifications:
                counts = {}
                for issue in diagnostics:
                    counts[issue["code"]] = counts.get(issue["code"], 0) + 1
                labels = ", ".join(f"{code} × {n}" for code, n in sorted(counts.items()))
                item_link = next((issue["links"][0] for issue in diagnostics if issue.get("links")), None)
                link = project.get("details_url") or item_link
                body = f'⚠️ {project["name"]} · проверка таблиц\n{checked_at}\n{labels}'
                if link:
                    body += f"\n{link}"
                self.db.execute(
                    "INSERT INTO deliveries(key,body) VALUES (?,?)",
                    (f"event:{project_id}:health:{episode}", body),
                )

    def latest_backup(self, project, identity):
        return self.db.execute(
            "SELECT * FROM id_registry WHERE project=? AND identity=? ORDER BY saved DESC LIMIT 1",
            (project, identity),
        ).fetchone()

    def issues(self, project):
        return self.db.execute(
            "SELECT * FROM incidents WHERE project=? AND closed IS NULL ORDER BY kind, identity",
            (project,),
        ).fetchall()

    def snapshot(self, project):
        return self.db.execute("SELECT * FROM snapshots WHERE project=?", (project,)).fetchone()

    def snapshot_summary(self, project):
        return self.db.execute(
            "SELECT attempted,successful,config,diagnostics FROM snapshots WHERE project=?", (project,),
        ).fetchone()

    def complete_snapshot(self, project):
        return self.db.execute("SELECT * FROM complete_snapshots WHERE project=?", (project,)).fetchone()

    def removal_hold(self, project):
        return self.db.execute(
            "SELECT * FROM removal_holds WHERE project=? AND released IS NULL", (project,),
        ).fetchone()

    def place_removal_hold(self, project, details):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO removal_holds(project,raised,details) VALUES (?,?,?)",
                (project, now(), encode(details)),
            )

    def release_removal_hold(self, project, reason):
        with self.db:
            result = self.db.execute(
                "UPDATE removal_holds SET released=?,release_reason=? WHERE project=? AND released IS NULL",
                (now(), reason, project),
            )
        return bool(result.rowcount)

    def get_operation(self, operation):
        return self.db.execute("SELECT * FROM id_registry WHERE operation=?", (operation,)).fetchone()

    def latest_operation_event(self, operation, kind):
        return self.db.execute(
            "SELECT * FROM id_operation_events WHERE operation=? AND kind=? ORDER BY seq DESC LIMIT 1",
            (operation, kind),
        ).fetchone()

    def reserve(self, operation, project, position, *, trigger="MANUAL_FIXTURE_PILOT"):
        context = asdict(position)
        context["source_link"] = position.source.link
        context["target_link"] = position.target.link
        context["reason"] = "CONFIRMED_EMPTY_PRICE"
        context["trigger"] = trigger
        payload = encode(context)
        with self.db:
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO id_registry VALUES (?,?,?,?,?,?)",
                (operation, project, position.identity, now(), "RESERVED", payload),
            )
            if inserted.rowcount:
                self.db.execute(
                    "INSERT INTO id_operation_events(operation,at,kind,state,evidence) VALUES (?,?,?,?,?)",
                    (operation, now(), "RESERVE_CONFIRMED", "RESERVED", "{}"),
                )
        saved = self.db.execute(
            "SELECT * FROM id_registry WHERE operation=?", (operation,)
        ).fetchone()
        if saved["project"] != project or saved["data"] != payload:
            raise ValueError("Операция уже связана с другим снимком")
        return saved["state"]

    def transition(self, operation, expected, state):
        self.record_operation_evidence(operation, expected, state, "STATE_TRANSITION", {})

    def record_operation_evidence(self, operation, expected, state, kind, evidence):
        with self.db:
            result = self.db.execute(
                "UPDATE id_registry SET state=? WHERE operation=? AND state=?",
                (state, operation, expected),
            )
            if result.rowcount != 1:
                raise RuntimeError("Состояние операции изменилось")
            self.db.execute(
                "INSERT INTO id_operation_events(operation,at,kind,state,evidence) VALUES (?,?,?,?,?)",
                (operation, now(), kind, state, encode(evidence)),
            )


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()
