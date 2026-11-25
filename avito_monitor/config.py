from datetime import date
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import json
import re

from .legacy import project_metadata


def require(condition, message):
    if not condition:
        raise ValueError(message)


def spreadsheet_id(value):
    require(isinstance(value, str), "Укажите ID или ссылку Google Sheets")
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    url = urlsplit(value)
    match = re.fullmatch(r"/spreadsheets/d/([A-Za-z0-9_-]+)(?:/.*)?", url.path)
    require(url.scheme == "https" and url.netloc == "docs.google.com" and match, "Неверная ссылка Google Sheets")
    return match[1]


def load_config(path):
    path = Path(path)
    return validate(json.loads(path.read_text(encoding="utf-8")), path.parent)


def validate(config, config_directory):
    ZoneInfo(config["report_timezone"])
    require(type(config.get("telegram_enabled", False)) is bool, "telegram_enabled должен быть boolean")
    for book, titles in config.get("google_xlsx_exports", {}).items():
        require(re.fullmatch(r"[A-Za-z0-9_-]+", book), "Неверный ID книги XLSX")
        require(
            isinstance(titles, dict) and titles and all(
                gid.isdecimal() and isinstance(title, str) and title.strip() for gid, title in titles.items()
            ) and len(set(titles.values())) == len(titles),
            "Для выгрузки XLSX нужны уникальные точные названия листов",
        )
    if "schedule" in config:
        schedule = config["schedule"]
        require(re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", schedule["report_time"]),
                "Время сводки должно быть в формате ЧЧ:ММ")
        require(type(schedule["check_every_minutes"]) is int and 1 <= schedule["check_every_minutes"] <= 1440,
                "Интервал проверки должен быть от 1 до 1440 минут")
        require(type(schedule["report_window_minutes"]) is int and 1 <= schedule["report_window_minutes"] <= 1440,
                "Окно подготовки отчёта должно быть от 1 до 1440 минут")
        weekdays = schedule["report_weekdays"]
        require(isinstance(weekdays, list) and weekdays and all(type(x) is int and 0 <= x <= 6 for x in weekdays),
                "Дни отчёта задаются списком чисел от 0 (понедельник) до 6")
    if "sync" in config:
        require("schedule" in config and isinstance(config["sync"], dict), "Для синхронизации нужно расписание")
        settings = config["sync"]
        for key, default, low, high in (
            ("poll_every_seconds", 60, 60, 900),
            ("audit_every_minutes", 1440, 10, 1440),
            ("export_every_minutes", 60, 10, 1440),
            ("formula_every_minutes", 60, 10, 1440),
            ("max_snapshot_age_minutes", 1500, 15, 2880),
            ("max_journal_age_minutes", 5, 1, 60),
            ("max_projects_per_tick", 6, 1, 50),
            ("max_change_pages", 8, 1, 100),
            ("max_sheet_requests_per_tick", 40, 5, 59),
            ("avito_poll_minutes", 5, 1, 120),
        ):
            value = settings.setdefault(key, default)
            require(type(value) is int and low <= value <= high, f"sync.{key} должен быть от {low} до {high}")
        require(settings["max_snapshot_age_minutes"] > settings["audit_every_minutes"],
                "Допустимый возраст снимка должен превышать интервал контрольной сверки")
        drives = settings.get("shared_drive_ids", [])
        require(isinstance(drives, list) and all(
            isinstance(drive, str) and re.fullmatch(r"[A-Za-z0-9_-]+", drive) for drive in drives
        ) and len(set(drives)) == len(drives), "Укажите уникальные ID общих дисков")
    registry = {}
    if config.get("price_monitor_registry"):
        registry_path = config_directory / config["price_monitor_registry"]
        registry = project_metadata(registry_path)
    seen_ids, seen_targets = set(), set()
    for project in config["projects"]:
        project_id = project["id"]
        if "price_monitor_id" in project:
            shared = registry.get(project["price_monitor_id"])
            require(shared is not None, f"{project_id}: проект отсутствует в реестре мониторинга цен")
            require(
                not ({"source_book", "target_book"} & project.keys()),
                f"{project_id}: таблицы наследуются из реестра мониторинга цен",
            )
            project.setdefault("name", shared["name"])
            project.update({field: shared[field] for field in ("source_book", "target_book")})
        require(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project_id), "Недопустимый ID проекта")
        require(project_id not in seen_ids, f"Повторяется проект {project_id}")
        require(isinstance(project["name"], str) and project["name"].strip(), f"{project_id}: нет названия")
        require(project["mode"] in {"draft", "monitoring", "paused"}, f"{project_id}: неизвестный режим")
        require(
            project.get("removal_confirmation", "feed_then_avito") in {"feed_then_avito", "avito_status"},
            f"{project_id}: неизвестный способ подтверждения снятия",
        )
        for flag in ("notify_removal", "notifications_enabled", "notify_during_grace", "auto_remove"):
            require(type(project.get(flag, True)) is bool, f"{flag} должен быть boolean")
        if "avito_credential_prefix" in project:
            require(
                re.fullmatch(r"[A-Z][A-Z0-9_]*", project["avito_credential_prefix"]),
                f"{project_id}: неверное имя набора окружения Avito",
            )
        for role in ("source_book", "target_book"):
            project[role] = spreadsheet_id(project[role])
        require(project["source_book"] != project["target_book"], f"{project_id}: у таблиц совпадают ID")
        export_books = {project["source_book"], project["target_book"]} & config.get("google_xlsx_exports", {}).keys()
        require(len(export_books) != 1, f"{project_id}: укажите оба XLSX-экспорта проекта")
        if project.get("auto_remove"):
            require(
                project.get("removal_confirmation") == "avito_status" and project.get("removal_limits")
                and project.get("avito_credential_prefix") and not export_books,
                f"{project_id}: для очистки нужны Sheets API, пределы и проверка статуса Avito",
            )
        if project["mode"] == "monitoring":
            require(project["target_book"] not in seen_targets, "Повторяется таблица автозагрузки")
            seen_targets.add(project["target_book"])
        seen_ids.add(project_id)
        date.fromisoformat(project["launch_date"])
        require(
            type(project["report_exclude_days"]) is int and project["report_exclude_days"] >= 0,
            "report_exclude_days должен быть целым неотрицательным числом",
        )
        if "removal_limits" in project:
            limits = project["removal_limits"]
            expected = {"max_candidates", "max_newly_empty", "max_lost_matches"}
            require(
                isinstance(limits, dict) and set(limits) == expected and all(
                    type(value) is int and value >= 0 for value in limits.values()
                ),
                f"{project_id}: нужны три неотрицательных числовых предела",
            )
        target_tabs = set()
        for tab in project.get("coverage_targets", []):
            gid = tab["gid"]
            require(type(gid) is int and gid >= 0 and gid not in target_tabs, f"{project_id}: повтор или неверный gid проверки охвата")
            require(type(tab["header_row"]) is int and tab["header_row"] >= 1 and tab["name"], f"{project_id}: проверьте описание листа")
            target_tabs.add(gid)
        route_names = set()
        for route in project.get("routes", []):
            name = route["name"]
            require(name and name not in route_names, f"{project_id}: повтор правила сопоставления")
            route_names.add(name)
            for role in ("source", "target"):
                spec = route[role]
                require(type(spec["gid"]) is int and spec["gid"] >= 0, f"{name}: недопустимый gid")
                require(type(spec["header_row"]) is int and spec["header_row"] >= 1, f"{name}: недопустимая строка заголовков")
                require(spec["keys"] and len(set(spec["keys"])) == len(spec["keys"]), f"{name}: проверьте ключи")
                if "marketplace_header_row" in spec:
                    line = spec["marketplace_header_row"]
                    require(
                        role == "target" and bool(spec.get("marketplace_field")) and type(line) is int and line >= 1,
                        f"{name}: неверный заголовок номера объявления",
                    )
                require(
                    spec.get("marketplace_field", "").casefold() not in {"id", "price", "title"},
                    f"{name}: номер объявления должен быть в самостоятельном поле",
                )
                start = spec.get("first_col", 0)
                end = spec.get("last_col", start + 1)
                require(type(start) is int and type(end) is int and 0 <= start < end, f"{name}: недопустимый диапазон")
            require(len(route["source"]["keys"]) == len(route["target"]["keys"]), f"{name}: разные размеры ключа")
            require(route["source"]["price"], f"{name}: нет заголовка цены")
    return config
