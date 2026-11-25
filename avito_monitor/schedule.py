"""Select due check and report slots without contacting external services."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import json

from .storage import encode


def due_jobs(store, config, cutoff, *, queue=False):
    settings = config["schedule"]
    local = cutoff.astimezone(ZoneInfo(config["report_timezone"]))
    day = local.date().isoformat()
    kind = "DAILY_QUEUE" if queue else "DAILY_PREVIEW"
    hour, minute = map(int, settings["report_time"].split(":"))
    scheduled = datetime.combine(local.date(), time(hour, minute), tzinfo=local.tzinfo)
    delay = local - scheduled
    daily = (
        local.weekday() in settings["report_weekdays"]
        and timedelta(0) <= delay < timedelta(minutes=settings["report_window_minutes"])
        and not store.db.execute(
            "SELECT 1 FROM scheduled_runs WHERE kind=? AND slot=?", (kind, day),
        ).fetchone()
        and (not queue or not store.db.execute("SELECT 1 FROM daily WHERE day=?", (day,)).fetchone())
    )
    last = store.db.execute(
        "SELECT cutoff FROM scheduled_runs WHERE kind='CHECK' ORDER BY cutoff DESC LIMIT 1"
    ).fetchone()
    check = not last or cutoff >= datetime.fromisoformat(last["cutoff"]) + timedelta(
        minutes=settings["check_every_minutes"]
    )
    return check, day if daily else None


def commit_daily(store, day, checked_at, bodies, *, attachment=False):
    with store.db:
        inserted = store.db.execute(
            "INSERT OR IGNORE INTO daily VALUES (?,?,?)", (day, checked_at, encode(bodies)),
        )
        if inserted.rowcount:
            if attachment and len(bodies) > 8:
                parts = []
                sections = []
                length = 0
                for body in bodies:
                    size = len(body.encode()) + 2
                    if sections and length + size > 8 * 1024 * 1024:
                        parts.append("\n\n".join(sections))
                        sections, length = [], 0
                    sections.append(body)
                    length += size
                if sections:
                    parts.append("\n\n".join(sections))
                messages = [(f"daily:{day}:document:{index}", part) for index, part in enumerate(parts, 1)]
            else:
                messages = [(f"daily:{day}:{index}", body) for index, body in enumerate(bodies, 1)]
            store.db.executemany(
                "INSERT INTO deliveries(key,body) VALUES (?,?)",
                messages,
            )
            return bodies
        saved = store.db.execute("SELECT bodies FROM daily WHERE day=?", (day,)).fetchone()
        return json.loads(saved["bodies"])


def remember(store, kind, slot, cutoff, data):
    with store.db:
        store.db.execute(
            "INSERT OR IGNORE INTO scheduled_runs(kind,slot,cutoff,data) VALUES (?,?,?,?)",
            (kind, slot, cutoff, encode(data)),
        )
