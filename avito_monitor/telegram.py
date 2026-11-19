import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request

from .storage import now


class Telegram:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", self.token) or not re.fullmatch(r"-?[0-9]+", self.chat):
            raise ValueError("Нужны TELEGRAM_BOT_TOKEN и числовой TELEGRAM_CHAT_ID")

    def send(self, text):
        data = {"chat_id": int(self.chat), "text": text, "link_preview_options": {"is_disabled": True}}
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=json.dumps(data).encode(), headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
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


def deliver(store, telegram, prefix):
    db = store.db
    rows = db.execute(
        "SELECT * FROM deliveries WHERE substr(key,1,?)=? ORDER BY rowid",
        (len(prefix), prefix),
    ).fetchall()
    for row in rows:
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
            if prefix.startswith("daily:") or state == "RATE_LIMIT":
                return state
            continue
        attempt = row["tries"] + 1
        with db:
            db.execute("UPDATE deliveries SET state='SENDING',tries=? WHERE key=?", (attempt, row["key"]))
            db.execute(
                "INSERT INTO delivery_attempts VALUES (?,?,?,?)", (row["key"], attempt, now(), "SENDING")
            )
        state, receipt = telegram.send(row["body"])
        with db:
            db.execute(
                "UPDATE deliveries SET state=?,receipt=? WHERE key=?", (state, receipt, row["key"]),
            )
            db.execute(
                "UPDATE delivery_attempts SET outcome=? WHERE key=? AND attempt=?",
                (state, row["key"], attempt),
            )
        if state in ("RATE_LIMIT", "UNCERTAIN") or (prefix.startswith("daily:") and state != "SENT"):
            return state
    return "DONE"
