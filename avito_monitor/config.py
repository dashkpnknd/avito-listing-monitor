from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import re

from .legacy import project_metadata


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    ZoneInfo(config["report_timezone"])
    require(type(config.get("telegram_enabled", False)) is bool, "telegram_enabled должен быть boolean")
    registry = {}
    if config.get("price_monitor_registry"):
        registry_path = Path(path).parent / config["price_monitor_registry"]
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
        require(type(project.get("notify_removal", False)) is bool, "notify_removal должен быть boolean")
        for role in ("source_book", "target_book"):
            require(re.fullmatch(r"[A-Za-z0-9_-]+", project[role]), f"{project_id}: неверный ID таблицы")
        require(project["source_book"] != project["target_book"], f"{project_id}: у таблиц совпадают ID")
        if project["mode"] == "monitoring":
            require(project["target_book"] not in seen_targets, "Повторяется таблица автозагрузки")
            seen_targets.add(project["target_book"])
        seen_ids.add(project_id)
        date.fromisoformat(project["launch_date"])
        require(
            type(project["report_exclude_days"]) is int and project["report_exclude_days"] >= 0,
            "report_exclude_days должен быть целым неотрицательным числом",
        )
        require(project["routes"], f"{project_id}: нет правил сопоставления")
        route_names = set()
        for route in project["routes"]:
            name = route["name"]
            require(name and name not in route_names, f"{project_id}: повтор правила сопоставления")
            route_names.add(name)
            for role in ("source", "target"):
                spec = route[role]
                require(type(spec["gid"]) is int and spec["gid"] >= 0, f"{name}: недопустимый gid")
                require(type(spec["header_row"]) is int and spec["header_row"] >= 1, f"{name}: недопустимая строка заголовков")
                require(spec["keys"] and len(set(spec["keys"])) == len(spec["keys"]), f"{name}: проверьте ключи")
                start = spec.get("first_col", 0)
                end = spec.get("last_col", start + 1)
                require(type(start) is int and type(end) is int and 0 <= start < end, f"{name}: недопустимый диапазон")
            require(len(route["source"]["keys"]) == len(route["target"]["keys"]), f"{name}: разные размеры ключа")
            require(route["source"]["price"], f"{name}: нет заголовка цены")
    return config
