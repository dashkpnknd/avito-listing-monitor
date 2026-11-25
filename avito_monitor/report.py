from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import json

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
    start = date.fromisoformat(project["launch_date"])
    return start <= as_of and as_of >= start + timedelta(days=project["report_exclude_days"])


def size(text):
    return len(text.encode("utf-16-le")) // 2


def report(store, projects, cutoff, zone, *, max_age=None, health=None, detailed=False):
    local = cutoff.astimezone(ZoneInfo(zone))
    timestamp = local.strftime("%d.%m.%Y, %H:%M %Z")
    heading = f"📊 ВЫКЛАДКА AVITO · {timestamp}"
    blocks = []
    incomplete = []
    eligible = [p for p in projects if included(p, local.date())]
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
        counts = Counter(item["category"] for item in items.values())
        lines.extend(f"• {category} — {count} шт." for category, count in sorted(counts.items(), key=lambda item: item[0].casefold()))
        if not counts:
            lines.append("• Нет позиций")
        if not detailed:
            continue
        for identity, item in items.items():
            line = f'↳ {item["title"]} · {item["target"]}'
            if item.get("previous_id") is not None:
                line += f' · прежний Id {item["previous_id"]}'
            if kind == "NEEDS_REMOVAL" and identity in pending:
                line += " · снятие на проверке"
            lines.append(line)
    if project.get("details_url"):
        lines.extend(["", project["details_url"]])
    return lines


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
        for line in lines:
            continuation = f"{label} (продолжение)"
            if size(continuation) + size(line) + 1 > limit:
                line = line.encode("utf-16-le")[: 2 * (limit - size(continuation) - 2)].decode("utf-16-le", "ignore") + "…"
            if size(part + "\n" + line) > limit:
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
