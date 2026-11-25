import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from uuid import uuid4

from .report import size
from .storage import now


class Telegram:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", self.token) or not re.fullmatch(r"-?[0-9]+", self.chat):
            raise ValueError("Нужны TELEGRAM_BOT_TOKEN и числовой TELEGRAM_CHAT_ID")

    def send(self, text):
        data = {"chat_id": int(self.chat), "text": text, "link_preview_options": {"is_disabled": True}}
        if text.startswith("📊 ВЫКЛАДКА AVITO"):
            entities = []
            offset = 0
            for line in text.split("\n"):
                length = size(line)
                if line.startswith(("📊 ", "🏬 ", "🟠 ", "🔴 ", "⚠️ ")):
                    entities.append({"type": "bold", "offset": offset, "length": length})
                offset += length + 1
            data["entities"] = entities
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=json.dumps(data).encode(), headers={"Content-Type": "application/json"},
        )
        return self.request(request)

    def send_document(self, text, day, part):
        boundary = uuid4().hex
        caption = f"📊 ВЫКЛАДКА AVITO · {day}\nПолный список по проектам"
        fields = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{self.chat}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="avito-{day}-{part}.txt"\r\n'
            'Content-Type: text/plain; charset=utf-8\r\n\r\n'
        )
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendDocument",
            data=fields.encode() + text.encode() + f"\r\n--{boundary}--\r\n".encode(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        return self.request(request)

    def request(self, request):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.load(response)
            if body.get("ok") and body.get("result", {}).get("message_id"):
                return "SENT", str(body["result"]["message_id"])
            return "UNCERTAIN", None
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    delay = max(1, int(json.load(exc)["parameters"]["retry_after"]))
                except (ValueError, KeyError, TypeError, OSError):
                    delay = 60
                return "RATE_LIMIT", str(int(time.time()) + delay)
            return ("FAILED", str(exc.code)) if 400 <= exc.code < 500 else ("UNCERTAIN", None)
        except (OSError, ValueError, http.client.HTTPException):
            return "UNCERTAIN", None


def deliver(store, telegram, prefix, *, allowed_event_projects=None):
    db = store.db
    ordered = prefix.startswith(("daily:", "first:"))
    rows = db.execute(
        "SELECT * FROM deliveries WHERE substr(key,1,?)=? ORDER BY rowid",
        (len(prefix), prefix),
    ).fetchall()
    for row in rows:
        if prefix == "event:" and allowed_event_projects is not None:
            if row["key"].split(":", 2)[1] not in allowed_event_projects:
                continue
        state, receipt = row["state"], row["receipt"]
        if state == "SENT":
            continue
        if state == "SENDING":
            with db:
                db.execute("UPDATE deliveries SET state='UNCERTAIN' WHERE key=?", (row["key"],))
                db.execute(
                    "UPDATE delivery_attempts SET outcome='UNCERTAIN' WHERE key=? AND attempt=?",
                    (row["key"], row["tries"]),
                )
        if state == "RATE_LIMIT" and int(receipt) > time.time():
            return state
        if state not in ("PENDING", "RATE_LIMIT") or row["tries"] >= 3:
            if ordered or state == "RATE_LIMIT":
                return state
            continue
        attempt = row["tries"] + 1
        with db:
            claim = db.execute(
                "UPDATE deliveries SET state='SENDING',tries=? WHERE key=? AND state=? AND tries=?",
                (attempt, row["key"], state, row["tries"]),
            )
            if not claim.rowcount:
                continue
            db.execute(
                "INSERT INTO delivery_attempts VALUES (?,?,?,?)", (row["key"], attempt, now(), "SENDING")
            )
        key_parts = row["key"].split(":")
        if len(key_parts) == 4 and key_parts[0] == "daily" and key_parts[2] == "document":
            state, receipt = telegram.send_document(row["body"], key_parts[1], key_parts[3])
        else:
            state, receipt = telegram.send(row["body"])
        if state == "RATE_LIMIT" and attempt >= 3:
            state, receipt = "FAILED", "429"
        with db:
            db.execute(
                "UPDATE deliveries SET state=?,receipt=? WHERE key=?", (state, receipt, row["key"]),
            )
            db.execute(
                "UPDATE delivery_attempts SET outcome=? WHERE key=? AND attempt=?",
                (state, row["key"], attempt),
            )
        if state in ("RATE_LIMIT", "UNCERTAIN") or (ordered and state != "SENT"):
            return state
    return "DONE"
