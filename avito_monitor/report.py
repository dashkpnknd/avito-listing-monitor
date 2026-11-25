from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import json
import re

from .storage import encode


LABELS = {
    "NEEDS_PUBLICATION": "🟠 НЕ ВСЕ ВЫЛОЖЕНЫ ПОЗИЦИИ:",
    "NEEDS_REMOVAL": "🔴 УБРАТЬ ИЗ ВЫКЛАДКИ ПОЗИЦИИ:",
}

DIVIDER = "━━━━━━━━━━━━━━━━━━━━"

DIAGNOSTIC_LABELS = {
    "MATCH_UNCERTAIN": "нет точной пары",
    "INVALID_CELL": "сомнительная цена или Id",
    "READ_ERROR": "ошибка чтения",
    "UNMAPPED_TARGET": "листы со строками вне правил",
    "REMOVAL_HOLD": "остановлены операции с Id",
    "REMOVAL_STATUS": "не удалось проверить операцию с Id",
}


def included(project, as_of):
    if project["launch_date"] is None:
        return False
    start = date.fromisoformat(project["launch_date"])
    return start <= as_of and as_of >= start + timedelta(days=project["report_exclude_days"])


def size(text):
    return len(text.encode("utf-16-le")) // 2


def report(store, projects, cutoff, zone, *, max_age=None, health=None, detailed=False, first_report=False):
    local = cutoff.astimezone(ZoneInfo(zone))
    timestamp = local.strftime("%d.%m.%Y, %H:%M %Z")
    heading = f"📊 ВЫКЛАДКА AVITO · {timestamp}"
    if first_report:
        heading += " · первая сверка"
    blocks = []
    incomplete = []
    eligible = [p for p in projects if first_report or included(p, local.date())]
    for project in eligible:
        project_id = project["id"]
        snapshot = store.snapshot_summary(project_id)
        name = " | ".join(filter(None, (project["name"], project.get("city"))))
        reason = health(project) if health else None
        if not reason and (not snapshot or snapshot["config"] != encode(project)):
            reason = "отсутствует текущая проверка"
        if not reason and max_age is None and snapshot["attempted"] != cutoff.isoformat():
            reason = "отсутствует текущая проверка"
        if not reason and max_age is not None:
            last_checked = snapshot["successful"] or snapshot["attempted"]
            if cutoff - datetime.fromisoformat(last_checked) > max_age:
                reason = "данные устарели"
        if reason:
            incomplete.append(f"• {name} · {reason}")
            continue
        diagnostics = json.loads(snapshot["diagnostics"])
        if max_age is not None:
            api = store.db.execute("SELECT diagnostics FROM sync_avito WHERE project=?", (project_id,)).fetchone()
            if api:
                diagnostics.extend(json.loads(api["diagnostics"]))
        if diagnostics:
            codes = Counter(item["code"] for item in diagnostics)
            summary = ", ".join(f"{DIAGNOSTIC_LABELS.get(code, code)} — {n}" for code, n in sorted(codes.items()))
            incomplete.append(f"• {name} · {summary}")
            for issue in diagnostics:
                if detailed and issue["code"] == "UNMAPPED_TARGET":
                    incomplete.append(f'↳ {issue["detail"]}')
        lines = project_lines(store, project, snapshot, detailed=detailed)
        if lines:
            blocks.append((f"🏬 {name}", lines))
    if incomplete:
        blocks.append(("⚠️ Не удалось проверить полностью", incomplete))
    if not blocks:
        return [f"{heading}\n\n" + ("Проблем не найдено" if eligible else "Нет проектов для сводки")]
    return paginate(heading, blocks)


def project_lines(store, project, snapshot, *, detailed):
    entries = {kind: {} for kind in LABELS}
    for row in store.issues(project["id"]):
        if row["seen"] == snapshot["attempted"]:
            entries[row["kind"]][row["identity"]] = json.loads(row["data"])
    pending = {}
    for operation in store.db.execute(
        """SELECT identity,state,data FROM id_registry WHERE project=?
           AND state IN ('WRITE_STARTED','WAITING_FEED_CONFIRMATION','WAITING_AVITO_CONFIRMATION')
           ORDER BY saved""", (project["id"],),
    ):
        data = json.loads(operation["data"])
        pending[operation["identity"]] = {
            "category": data.get("category") or "Прочее",
            "title": data["title"], "target": data["target_link"],
            "report_title": data.get("report_title"),
        }
    removals = entries["NEEDS_REMOVAL"]
    for identity, item in pending.items():
        removals.setdefault(identity, item)
    if not any(entries.values()):
        return []

    lines = []
    for kind, label in LABELS.items():
        lines.extend(["", label])
        items = entries[kind]
        categories = defaultdict(list)
        for identity, item in items.items():
            categories[item["category"]].append((identity, item))
        if not categories:
            lines.append("• Нет позиций")
        for index, category in enumerate(sorted(categories, key=str.casefold)):
            positions = categories[category]
            names = Counter(position_name(item) for _, item in positions)
            if index:
                lines.append("")
            lines.append(f"• {category} — {len(positions)} шт.")
            for identity, item in sorted(positions, key=position_order):
                name = position_name(item)
                cell = item["target"].partition("&range=")[2]
                line = f"  ↳ {name}"
                if detailed:
                    line += f' · {item["target"]}'
                elif cell and names[name] > 1:
                    line += f" · {cell}"
                if item.get("previous_id") is not None:
                    line += f' · прежний Id {item["previous_id"]}'
                if identity in pending and kind == "NEEDS_REMOVAL":
                    line += " · снятие на проверке"
                lines.append(line)
    if detailed and project.get("details_url"):
        lines.extend(["", project["details_url"]])
    return lines


def position_order(entry):
    _, item = entry
    match = re.search(r"&range=([A-Z]+)([0-9]+)$", item["target"])
    return (position_name(item).casefold(), int(match[2]) if match else 0, item["target"])


def position_name(item):
    return " ".join((item.get("report_title") or item["title"]).split())


def paginate(heading, blocks):
    limit = 4096 - size(heading) - 32
    separator = f"\n\n{DIVIDER}\n\n"
    drafts = []
    part = ""
    for label, lines in blocks:
        block = "\n".join([label, *lines]).rstrip()
        if size(block) <= limit:
            if part and size(part + separator + block) > limit:
                drafts.append(part)
                part = ""
            part = part + separator + block if part else block
            continue
        if part:
            drafts.append(part)
        part = label
        section = category = ""
        for index, line in enumerate(lines):
            continuation = f"{label} (продолжение)"
            if line in LABELS.values():
                section, category = line, ""
            elif section and line.startswith("• ") and line.endswith(" шт."):
                category = line
                continuation += f"\n\n{section}"
            elif line.startswith("  ↳ ") and category:
                continuation += f"\n\n{section}\n{category}"
            room = max(0, limit - size(continuation) - 1)
            if size(line) > room:
                line = line.encode("utf-16-le")[: 2 * max(0, room - 1)].decode("utf-16-le", "ignore") + "…"
            reserve = 0
            if line == category and index + 1 < len(lines):
                reserve = min(160, size(lines[index + 1]) + 1)
            if size(part + "\n" + line) + reserve > limit:
                drafts.append(part.rstrip())
                part = continuation
            part += "\n" + line
        part = part.rstrip()
    if part:
        drafts.append(part)
    return [
        f"{heading}{f' · {index}/{len(drafts)}' if len(drafts) > 1 else ''}\n\n{draft}"
        for index, draft in enumerate(drafts, 1)
    ]
