from datetime import datetime, timezone
from xml.etree.ElementTree import ParseError
from zoneinfo import ZoneInfo
import argparse
import json
import sqlite3
import sys

from .adapters import Fixture, GoogleReadOnly
from .config import load_config
from .model import ScanError, scan
from .pilot import inspect_feed, single_removal
from .report import report
from .storage import encode, locked_db
from .telegram import Telegram, deliver


def main():
    cli = argparse.ArgumentParser(description="Мониторинг выкладки Avito")
    cli.add_argument("--config", required=True)
    cli.add_argument("--db", required=True)
    cli.add_argument("--fixture", help="Локальные тестовые данные вместо Google")
    commands = cli.add_subparsers(dest="command", required=True)
    for name in ("check", "status", "registry"):
        commands.add_parser(name).add_argument("--project")
    daily = commands.add_parser("report")
    daily.add_argument("--commit-report", action="store_true", help="Зафиксировать сводку для отправки")
    delivery = commands.add_parser("deliver")
    delivery.add_argument("--daily", metavar="YYYY-MM-DD", nargs="?", const="today")
    pilot = commands.add_parser("pilot")
    pilot.add_argument("--project", required=True)
    pilot.add_argument("--target", required=True)
    pilot.add_argument("--operation", required=True)
    feed = commands.add_parser("inspect-feed")
    feed.add_argument("--xml", required=True)
    feed.add_argument("--old-id", required=True)
    feed.add_argument("--title", required=True)
    args = cli.parse_args()
    try:
        return run(args)
    except (ScanError, ValueError, KeyError, IndexError, TypeError, OSError, sqlite3.Error, ParseError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2


def run(args):
    config = load_config(args.config)
    project_filter = getattr(args, "project", None)
    selected = [p for p in config["projects"] if not project_filter or p["id"] == project_filter]
    if project_filter and not selected:
        raise ValueError("Проект не найден")
    if args.command == "inspect-feed":
        print(encode(inspect_feed(args.xml, args.old_id, args.title)))
        return 0
    with locked_db(args.db) as store:
        if args.command in ("status", "registry"):
            for project in selected:
                project_id = project["id"]
                if args.command == "registry":
                    rows = store.db.execute(
                        "SELECT * FROM id_registry WHERE project=? ORDER BY saved", (project_id,)
                    ).fetchall()
                    print(encode([dict(row) | {"data": json.loads(row["data"])} for row in rows]))
                    continue
                snap = store.snapshot(project_id)
                good = store.db.execute(
                    "SELECT checked FROM complete_snapshots WHERE project=?", (project_id,)
                ).fetchone()
                print(encode({
                    "project": project_id,
                    "last_attempt": snap["attempted"] if snap else None,
                    "last_read_without_error": snap["successful"] if snap else None,
                    "last_complete_snapshot": good["checked"] if good else None,
                    "diagnostics": json.loads(snap["diagnostics"]) if snap else [],
                    "issues": [dict(row) | {"data": json.loads(row["data"])} for row in store.issues(project_id)],
                    "delivery_problems": [dict(row) for row in store.db.execute(
                        "SELECT key,state,tries,receipt FROM deliveries WHERE state IN ('SENDING','UNCERTAIN','FAILED','RATE_LIMIT')"
                    )],
                }))
            return 0
        if args.command == "deliver":
            if args.fixture or not config.get("telegram_enabled"):
                raise ValueError("Отправка Telegram отключена")
            daily = args.daily
            if daily == "today":
                daily = datetime.now(ZoneInfo(config["report_timezone"])).date().isoformat()
            if daily:
                datetime.strptime(daily, "%Y-%m-%d")
            prefix = f"daily:{daily}:" if daily else "event:"
            result = deliver(store, Telegram(), prefix)
            print(result)
            return 0 if result == "DONE" else 1
        if args.command == "pilot" and not args.fixture:
            raise ValueError("Протокол pilot запускается только с --fixture")
        active = [p for p in selected if p["mode"] == "monitoring"]
        if args.command == "check" and project_filter and selected[0]["mode"] == "draft":
            active = selected
        if args.command == "pilot" and not active:
            raise ValueError("Тестовый проект должен быть в режиме monitoring")
        adapter = None
        if active:
            adapter = Fixture.from_path(args.fixture) if args.fixture else GoogleReadOnly()
        if args.command == "pilot":
            print(single_removal(store, adapter, active[0], args.target, args.operation))
            return 0
        cutoff = datetime.now(timezone.utc)
        checked_at = cutoff.isoformat()
        day = cutoff.astimezone(ZoneInfo(config["report_timezone"])).date().isoformat()
        commit_report = getattr(args, "commit_report", False)
        if commit_report and (args.fixture or not config.get("telegram_enabled")):
            raise ValueError("Фиксация сводки для отправки отключена")
        existing = store.db.execute("SELECT bodies FROM daily WHERE day=?", (day,)).fetchone() if commit_report else None
        if existing:
            print("\n\n".join(json.loads(existing["bodies"])))
            return 0
        problematic = False
        for project in active:
            if hasattr(adapter, "begin_scan"):
                adapter.begin_scan()
            positions, diagnostics = scan(project, adapter)
            notifications = project["mode"] == "monitoring" and config.get("telegram_enabled") and not args.fixture
            store.reconcile(project, positions, diagnostics, checked_at, notifications)
            problematic |= bool(diagnostics)
            if args.command == "check":
                print(encode({"project": project["id"], "matched": len(positions), "diagnostics": len(diagnostics)}))
        if args.command == "report":
            bodies = report(store, active, cutoff, config["report_timezone"])
            if commit_report:
                with store.db:
                    store.db.execute("INSERT INTO daily VALUES (?,?,?)", (day, checked_at, encode(bodies)))
                    store.db.executemany(
                        "INSERT INTO deliveries(key,body) VALUES (?,?)",
                        [(f"daily:{day}:{index}", body) for index, body in enumerate(bodies, 1)],
                    )
            print("\n\n".join(bodies))
            return 0
        return 1 if problematic else 0


if __name__ == "__main__":
    sys.exit(main())
