from datetime import datetime, timezone
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zoneinfo import ZoneInfo
import argparse
import json
import sqlite3
import sys

from .adapters import Fixture, GoogleReadOnly
from .avito import AvitoStatuses
from .config import load_config
from .feed_evidence import compare_feeds
from .google_export import GoogleExportReadOnly
from .model import Address, ScanError, scan
from .pilot import inspect_feed, single_removal
from .project_cycle import can_notify, reconcile_project, save_plan
from .removals import plan_removals, record_avito_evidence, record_feed_evidence, record_manual_restore, rehearse_removals
from .report import included, report
from .schedule import commit_daily, due_jobs, remember
from .storage import encode, locked_db
from .telegram import Telegram, deliver


def main():
    cli = argparse.ArgumentParser(description="Мониторинг выкладки Avito")
    cli.add_argument("--config", required=True)
    cli.add_argument("--db", required=True)
    cli.add_argument("--fixture", help="Локальные тестовые данные вместо Google")
    commands = cli.add_subparsers(dest="command", required=True)
    commands.add_parser("tick", help="Проверить расписание и подготовить сводку без отправки")
    for name in ("check", "status", "registry"):
        commands.add_parser(name).add_argument("--project")
    register = commands.add_parser("register", help="Подключить проект по двум ссылкам без карты листов")
    register.add_argument("--project", required=True)
    register.add_argument("--name", required=True)
    register.add_argument("--city")
    register.add_argument("--source", required=True)
    register.add_argument("--target", required=True)
    register.add_argument("--launch-date", required=True)
    register.add_argument("--avito-profile")
    lookup = commands.add_parser("listing-status", help="Прочитать статус конкретного объявления из API Avito")
    lookup.add_argument("--project", required=True)
    lookup.add_argument("--listing-id", required=True)
    lookup.add_argument("--technical-id")
    daily = commands.add_parser("report")
    daily.add_argument("--commit-report", action="store_true", help="Зафиксировать сводку для отправки")
    daily.add_argument("--details", action="store_true", help="Показать отдельные позиции и ссылки в консоли")
    delivery = commands.add_parser("deliver")
    delivery.add_argument("--daily", metavar="YYYY-MM-DD", nargs="?", const="today")
    commands.add_parser("dispatch", help="Отправить сегодняшнюю сводку и оперативные уведомления")
    pilot = commands.add_parser("pilot")
    pilot.add_argument("--project", required=True)
    pilot.add_argument("--target", required=True)
    pilot.add_argument("--operation", required=True)
    pilot.add_argument("--active-evidence", type=Path, help="Наблюдение ACTIVE до очистки для проверки без XML")
    pilot.add_argument("--save-fixture", type=Path, help="Записать результат в новый локальный файл")
    feed = commands.add_parser("inspect-feed")
    feed.add_argument("--xml", required=True)
    feed.add_argument("--old-id", required=True)
    feed.add_argument("--title", required=True)
    compare = commands.add_parser("compare-feed")
    compare.add_argument("--before", required=True)
    compare.add_argument("--after", required=True)
    compare.add_argument("--old-id", required=True)
    compare.add_argument("--match-field", action="append")
    for name in ("plan-removals", "rehearse-removals", "release-removal-hold", "trace", "confirm-feed", "record-avito", "observe-restore"):
        p = commands.add_parser(name)
        p.add_argument("--project", required=True)
        if name in {"confirm-feed", "record-avito", "observe-restore"}:
            p.add_argument("--operation", required=True)
        if name == "confirm-feed":
            p.add_argument("--before", required=True)
            p.add_argument("--after", required=True)
            p.add_argument("--match-field", action="append")
        if name == "record-avito":
            p.add_argument("--evidence", required=True, type=Path)
        if name == "rehearse-removals":
            p.add_argument("--save-fixture", required=True, type=Path)
        if name == "trace":
            p.add_argument("--cell", required=True)
        if name == "release-removal-hold":
            p.add_argument("--reason", required=True)
    args = cli.parse_args()
    try:
        return run(args)
    except (ScanError, ValueError, KeyError, IndexError, TypeError, OSError, sqlite3.Error, ParseError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2


def run(args):
    if args.command == "register":
        from .registration import register_project

        with locked_db(args.db):
            print(encode(register_project(args)))
        return 0
    config = load_config(args.config)
    if args.command == "report" and args.commit_report and args.details:
        raise ValueError("Подробности доступны при просмотре отчёта в консоли")
    project_filter = getattr(args, "project", None)
    selected = [p for p in config["projects"] if not project_filter or p["id"] == project_filter]
    if project_filter and not selected:
        raise ValueError("Проект не найден")
    if args.command == "listing-status":
        if not selected[0].get("avito_credential_prefix"):
            raise ScanError("Подключите профиль Avito для выбранного проекта")
        print(encode(AvitoStatuses().observe(
            selected[0], args.listing_id, args.technical_id, require_binding=args.technical_id is not None,
        )))
        return 0
    if args.command == "inspect-feed":
        print(encode(inspect_feed(args.xml, args.old_id, args.title)))
        return 0
    if args.command == "compare-feed":
        fields = args.match_field or ["Title"]
        evidence = compare_feeds(args.before, args.after, args.old_id, fields)
        print(encode(evidence))
        return 1 if evidence["outcome"] == "INCONCLUSIVE" else 0
    role = None
    if "sync" in config and args.command in {"deliver", "dispatch"}:
        role = "delivery"
    elif "sync" in config and args.command in {"report", "status", "registry"}:
        role = "report"
    with locked_db(args.db, role=role) as store:
        if args.command in ("status", "registry"):
            for project in selected:
                project_id = project["id"]
                if args.command == "registry":
                    rows = store.db.execute(
                        "SELECT * FROM id_registry WHERE project=? ORDER BY saved", (project_id,)
                    ).fetchall()
                    result = []
                    for row in rows:
                        events = store.db.execute(
                            "SELECT at,kind,state,evidence FROM id_operation_events WHERE operation=? ORDER BY seq",
                            (row["operation"],),
                        ).fetchall()
                        result.append(dict(row) | {
                            "data": json.loads(row["data"]),
                            "events": [dict(event) | {"evidence": json.loads(event["evidence"])} for event in events],
                        })
                    print(encode(result))
                    continue
                snap = store.snapshot(project_id)
                good = store.complete_snapshot(project_id)
                hold = store.removal_hold(project_id)
                sync = None
                if "sync" in config:
                    files = store.db.execute("SELECT book,checked FROM sync_books WHERE project=?", (project_id,))
                    task = store.db.execute(
                        "SELECT due,attempts,error FROM sync_tasks WHERE project=?", (project_id,),
                    ).fetchone()
                    sync = {"file_checks": {row["book"]: row["checked"] for row in files},
                            "pending": dict(task) if task else None}
                print(encode({
                    "project": project_id,
                    "last_attempt": snap["attempted"] if snap else None,
                    "last_read_without_error": snap["successful"] if snap else None,
                    "last_complete_snapshot": good["checked"] if good else None,
                    "removal_hold": json.loads(hold["details"]) if hold else None,
                    "diagnostics": json.loads(snap["diagnostics"]) if snap else [],
                    "sync": sync,
                    "issues": [dict(row) | {"data": json.loads(row["data"])} for row in store.issues(project_id)],
                    "delivery_problems": [dict(row) for row in store.db.execute(
                        "SELECT key,state,tries,receipt FROM deliveries WHERE state IN ('SENDING','UNCERTAIN','FAILED','RATE_LIMIT')"
                    )],
                }))
            return 0
        if args.command in {"confirm-feed", "record-avito", "observe-restore", "plan-removals", "rehearse-removals", "release-removal-hold", "trace"}:
            return run_operator_command(args, store, selected[0], config)
        if args.command in {"deliver", "dispatch"}:
            if args.fixture or not config.get("telegram_enabled"):
                raise ValueError("Отправка Telegram отключена")
            local_day = datetime.now(ZoneInfo(config["report_timezone"])).date()
            allowed = {
                p["id"] for p in selected
                if p["mode"] == "monitoring" and p.get("notifications_enabled", True)
                and (p.get("notify_during_grace", True) or included(p, local_day))
            }
            bot = Telegram()
            if args.command == "dispatch":
                if "sync" in config:
                    from .incremental import daily_if_due

                    daily_if_due(
                        store, config, [p for p in selected if p["mode"] == "monitoring"],
                        datetime.now(timezone.utc), queue=True,
                    )
                daily_result = deliver(store, bot, f"daily:{local_day.isoformat()}:")
                # A 429 may apply to the whole chat. Leave the other queue for the next tick.
                event_result = (
                    "RATE_LIMIT" if daily_result == "RATE_LIMIT"
                    else deliver(store, bot, "event:", allowed_event_projects=allowed)
                )
                print(encode({"daily": daily_result, "events": event_result}))
                return 0 if daily_result == event_result == "DONE" else 1
            daily = local_day.isoformat() if args.daily == "today" else args.daily
            if daily:
                datetime.strptime(daily, "%Y-%m-%d")
            prefix = f"daily:{daily}:" if daily else "event:"
            result = deliver(store, bot, prefix, allowed_event_projects=allowed)
            print(result)
            return 0 if result == "DONE" else 1
        if args.command == "pilot" and not args.fixture:
            raise ValueError("Протокол pilot запускается только с --fixture")
        active = [p for p in selected if p["mode"] == "monitoring"]
        if args.command == "check" and project_filter and selected[0]["mode"] == "draft":
            active = selected
        if args.command == "pilot" and not active:
            raise ValueError("Тестовый проект должен быть в режиме monitoring")
        cutoff = datetime.now(timezone.utc)
        if "sync" in config and args.command in {"tick", "report"}:
            from .incremental import saved_report, tick

            if args.command == "report":
                if args.commit_report:
                    if args.fixture or not config.get("telegram_enabled"):
                        raise ValueError("Фиксация сводки для отправки отключена")
                    day = cutoff.astimezone(ZoneInfo(config["report_timezone"])).date().isoformat()
                    bodies = commit_daily(
                        store, day, cutoff.isoformat(), saved_report(store, config, active, cutoff),
                    )
                else:
                    bodies = saved_report(store, config, active, cutoff, fixture=bool(args.fixture), detailed=args.details)
                print("\n\n".join(bodies))
                return 0
            adapters = {}
            stats, bodies = tick(
                store, config, active, cutoff, fixture=bool(args.fixture),
                reader=lambda project: read_adapter(args, config, project, adapters),
            )
            if bodies:
                print("\n\n".join(bodies))
            if stats["failed"]:
                print(encode(stats), file=sys.stderr)
            return int(bool(stats["failed"]))
        scheduled_day = None
        queue_scheduled = bool(config.get("telegram_enabled") and not args.fixture)
        if args.command == "tick":
            if "schedule" not in config:
                raise ScanError("Укажите подтверждённое расписание в конфигурации")
            check_due, scheduled_day = due_jobs(store, config, cutoff, queue=queue_scheduled)
            if not check_due and not scheduled_day:
                return 0
        adapters = {}
        if args.command == "pilot":
            adapter = read_adapter(args, config, active[0], adapters)
            if args.save_fixture:
                if store.get_operation(args.operation):
                    raise ScanError("Операция уже существует. Проверьте журнал и сохранённую копию")
                adapter.save_new_copy(args.save_fixture)
            evidence = json.loads(args.active_evidence.read_text(encoding="utf-8")) if args.active_evidence else None
            print(single_removal(store, adapter, active[0], args.target, args.operation, active_evidence=evidence))
            return 0
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
        observer = AvitoStatuses()
        for project in active:
            try:
                adapter = read_adapter(args, config, project, adapters)
                if hasattr(adapter, "begin_scan"):
                    adapter.begin_scan()
                positions, diagnostics = scan(project, adapter)
            except ScanError as exc:
                positions, diagnostics = {}, [{"code": "READ_ERROR", "detail": str(exc)}]
            live_tick = args.command == "tick" and not args.fixture
            _, positions, diagnostics = reconcile_project(
                store, project, positions, diagnostics, checked_at,
                notify=can_notify(config, project, checked_at, args.fixture),
                observer=observer if live_tick else None, apply=live_tick,
            )
            problematic |= bool(diagnostics)
            if args.command == "check":
                print(encode({"project": project["id"], "matched": len(positions), "diagnostics": len(diagnostics)}))
        if args.command == "tick":
            remember(store, "CHECK", checked_at, checked_at, {"incomplete": problematic})
        if args.command == "report" or scheduled_day:
            bodies = report(store, active, cutoff, config["report_timezone"], detailed=getattr(args, "details", False))
            if commit_report or (scheduled_day and queue_scheduled):
                bodies = commit_daily(store, day, checked_at, bodies)
            if scheduled_day:
                kind = "DAILY_QUEUE" if queue_scheduled else "DAILY_PREVIEW"
                remember(store, kind, scheduled_day, checked_at, bodies)
            print("\n\n".join(bodies))
            return 0
        return 1 if problematic else 0


def read_adapter(args, config, project, adapters):
    if args.fixture:
        kind = "fixture"
    elif project["source_book"] in config.get("google_xlsx_exports", {}):
        kind = "export"
    else:
        kind = "api"
    if kind not in adapters:
        if kind == "fixture":
            adapters[kind] = Fixture.from_path(args.fixture)
        elif kind == "export":
            adapters[kind] = GoogleExportReadOnly(config["google_xlsx_exports"])
        else:
            adapters[kind] = GoogleReadOnly()
    return adapters[kind]


def run_operator_command(args, store, project, config):
    if args.command == "release-removal-hold":
        reason = args.reason.strip()
        if not reason:
            raise ScanError("Укажите причину снятия блокировки")
        print(encode({"released": store.release_removal_hold(project["id"], reason)}))
        return 0
    if args.command == "trace":
        snapshot = store.snapshot(project["id"])
        if not snapshot:
            raise ScanError("Сначала запустите проверку проекта")
        links = []
        for position in json.loads(snapshot["data"]).values():
            source = Address(**position["source"]).link
            target = Address(**position["target"]).link
            if args.cell in (source, target):
                links.append({key: position[key] for key in ("title", "category", "state", "issue", "price")}
                             | {"source": source, "target": target})
        diagnostics = [d for d in json.loads(snapshot["diagnostics"]) if args.cell in d.get("links", [])]
        print(encode({"checked_at": snapshot["attempted"], "positions": links, "diagnostics": diagnostics}))
        return 0 if links or diagnostics else 1
    if args.command == "confirm-feed":
        fields = args.match_field or ["Title"]
        print(encode(record_feed_evidence(store, project, args.operation, args.before, args.after, fields)))
        return 0
    if args.command == "record-avito":
        observation = json.loads(args.evidence.read_text(encoding="utf-8"))
        print(encode(record_avito_evidence(store, project, args.operation, observation)))
        return 0
    if args.command == "rehearse-removals" and not args.fixture:
        raise ScanError("Для репетиции нужен локальный снимок --fixture")
    adapter = read_adapter(args, config, project, {})
    positions, diagnostics = scan(project, adapter)
    if args.command == "observe-restore":
        if diagnostics:
            raise ScanError("Проверка неполная, восстановление не подтверждено")
        print(record_manual_restore(store, project, args.operation, positions))
        return 0
    plan = plan_removals(store, project, positions, diagnostics)
    save_plan(store, plan, datetime.now(timezone.utc).isoformat())
    if args.command == "rehearse-removals" and not plan["reasons"]:
        result = rehearse_removals(store, adapter, project, positions, plan, args.save_fixture)
        print(encode({"plan": plan, "operations": result}))
    else:
        print(encode(plan))
    return 1 if plan["reasons"] else 0


if __name__ == "__main__":
    sys.exit(main())
