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

from .model import id_state


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@contextmanager
def locked_db(path):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
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
            CREATE TABLE IF NOT EXISTS id_registry (
                operation TEXT PRIMARY KEY, project TEXT NOT NULL, identity TEXT NOT NULL,
                saved TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                key TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
                tries INTEGER NOT NULL DEFAULT 0, receipt TEXT
            );
            CREATE TABLE IF NOT EXISTS delivery_attempts (
                key TEXT NOT NULL, attempt INTEGER NOT NULL, at TEXT NOT NULL,
                outcome TEXT NOT NULL, PRIMARY KEY(key, attempt)
            );
            CREATE TABLE IF NOT EXISTS daily (
                day TEXT PRIMARY KEY, cutoff TEXT NOT NULL, bodies TEXT NOT NULL
            );
        """)

    def reconcile(self, project, positions, diagnostics, checked_at, notifications=False):
        project_id = project["id"]
        prior = self.db.execute("SELECT * FROM snapshots WHERE project=?", (project_id,)).fetchone()
        baseline = not prior or not prior["successful"] or prior["config"] != encode(project)
        changes = []
        with self.db:
            self.record_health(project, diagnostics, checked_at, notifications)
            for identity, position in positions.items():
                if position.state not in ("POSITIVE", "EMPTY") or id_state(position.id_cell) == "INVALID":
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
            if not diagnostics:
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
                restore = "\nПрежний Id сохранён в реестре." if item.get("restore_available") else ""
                key = digest([project_id, identity, cycle])
                self.db.execute("INSERT OR IGNORE INTO deliveries(key,body) VALUES (?,?)", (
                    f"event:{project_id}:{key}",
                    f'{label} · {project["name"]}\n{item["title"]}\n{item["target"]}{restore}',
                ))

    def record_health(self, project, diagnostics, checked_at, notifications):
        project_id = project["id"]
        latest = self.db.execute("SELECT * FROM health WHERE project=?", (project_id,)).fetchone()
        active_episode = latest and latest["closed"] is None
        if active_episode and not diagnostics:
            self.db.execute("UPDATE health SET closed=? WHERE project=?", (checked_at, project_id))
            self.db.execute(
                "UPDATE deliveries SET state='SUPERSEDED' WHERE key=? AND state IN ('PENDING','RATE_LIMIT')",
                (f'event:{project_id}:health:{latest["episode"]}',),
            )
        if diagnostics and not active_episode:
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

    def reserve(self, operation, project, position):
        context = asdict(position)
        context["source_link"] = position.source.link
        context["target_link"] = position.target.link
        context["reason"] = "CONFIRMED_EMPTY_PRICE"
        context["trigger"] = "MANUAL_FIXTURE_PILOT"
        payload = encode(context)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO id_registry VALUES (?,?,?,?,?,?)",
                (operation, project, position.identity, now(), "RESERVED", payload),
            )
        saved = self.db.execute(
            "SELECT * FROM id_registry WHERE operation=?", (operation,)
        ).fetchone()
        if saved["project"] != project or saved["data"] != payload:
            raise ValueError("Операция уже связана с другим снимком")
        return saved["state"]

    def transition(self, operation, expected, state):
        with self.db:
            result = self.db.execute(
                "UPDATE id_registry SET state=? WHERE operation=? AND state=?",
                (state, operation, expected),
            )
            if result.rowcount != 1:
                raise RuntimeError("Состояние операции изменилось")


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()
