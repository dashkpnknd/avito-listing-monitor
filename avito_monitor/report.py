from collections import Counter
from datetime import date, timedelta
from zoneinfo import ZoneInfo
import json


LABELS = {
    "NEEDS_PUBLICATION": "🟠 НЕ ВСЕ ВЫЛОЖЕНЫ",
    "NEEDS_REMOVAL": "🔴 УБРАТЬ ИЗ ВЫКЛАДКИ",
}


def included(project, as_of):
    start = date.fromisoformat(project["launch_date"])
    return start <= as_of and as_of >= start + timedelta(days=project["report_exclude_days"])


def size(text):
    return len(text.encode("utf-16-le")) // 2


def report(store, projects, cutoff, zone):
    local = cutoff.astimezone(ZoneInfo(zone))
    timestamp = local.strftime("%d.%m.%Y, %H:%M %Z")
    heading = f"📊 ВЫКЛАДКА AVITO · {timestamp}"
    blocks = []
    incomplete = []
    eligible = [p for p in projects if included(p, local.date())]
    for project in eligible:
        project_id = project["id"]
        snapshot = store.snapshot(project_id)
        name = " | ".join(filter(None, (project["name"], project.get("city"))))
        if not snapshot or snapshot["attempted"] != cutoff.isoformat():
            incomplete.append(f"• {name} · отсутствует текущая проверка")
            continue
        diagnostics = json.loads(snapshot["diagnostics"])
        if diagnostics:
            codes = Counter(item["code"] for item in diagnostics)
            summary = ", ".join(f"{code} × {n}" for code, n in sorted(codes.items()))
            incomplete.append(f"• {name} · {summary}")
        items = [row for row in store.issues(project_id) if row["seen"] == cutoff.isoformat()]
        lines = []
        for kind, label in LABELS.items():
            entries = [json.loads(row["data"]) for row in items if row["kind"] == kind]
            if not entries:
                continue
            counts = Counter(entry["category"] for entry in entries)
            if lines:
                lines.append("")
            lines.append(f"{label} — {len(entries)}")
            lines.extend(f"• {category} — {count}" for category, count in sorted(counts.items()))
            if project.get("details_url"):
                lines.append(project["details_url"])
            else:
                lines.extend(f'↳ {item["title"]} · {item["target"]}' for item in entries)
        if lines:
            blocks.append((name, lines))
    if incomplete:
        blocks.append(("⚠️ Не удалось проверить полностью", incomplete))
    if not blocks:
        return [f"{heading}\n\n" + ("Проблем не найдено" if eligible else "Нет проектов для сводки")]
    limit = 3850
    drafts = []
    part = ""
    for label, lines in blocks:
        current_label = label
        for line in lines:
            if size(line) > limit - size(label) - 200:
                raise ValueError("Строка отчёта слишком длинная. Настройте ссылку на реестр.")
            content = f"{current_label}\n{line}" if current_label else line
            if size(part + content) + 5 > limit:
                drafts.append(part.rstrip())
                part = ""
                content = f"{label} (продолжение)\n{line}"
            part += content + "\n"
            current_label = ""
        part += "\n"
    if part:
        drafts.append(part.rstrip())
    return [
        f"{heading}{f' · {index}/{len(drafts)}' if len(drafts) > 1 else ''}\n\n{draft}"
        for index, draft in enumerate(drafts, 1)
    ]
