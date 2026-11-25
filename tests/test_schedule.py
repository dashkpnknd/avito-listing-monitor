from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from avito_monitor.schedule import commit_daily, due_jobs, remember
from avito_monitor.storage import locked_db
from avito_monitor.telegram import deliver
from bridge.observe_active import observe


class ScheduleTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        lock = locked_db(Path(directory.name) / "cycle.sqlite")
        self.store = lock.__enter__()
        self.addCleanup(lock.__exit__, None, None, None)
        self.config = {
            "report_timezone": "Europe/Moscow",
            "schedule": {
                "check_every_minutes": 30, "report_time": "08:00",
                "report_window_minutes": 10,
                "report_weekdays": [0, 1, 2, 3, 4, 5, 6],
            },
        }
        self.cutoff = datetime(2026, 11, 20, 5, tzinfo=timezone.utc)

    def test_daily_only_in_window_and_once_per_date_in_preview(self):
        self.assertEqual((True, None), due_jobs(self.store, self.config, self.cutoff - timedelta(minutes=1)))
        self.assertEqual((True, "2026-11-20"), due_jobs(self.store, self.config, self.cutoff))
        at = self.cutoff.isoformat()
        remember(self.store, "CHECK", at, at, {})
        remember(self.store, "DAILY_PREVIEW", "2026-11-20", at, ["Preview"])
        self.assertEqual((False, None), due_jobs(self.store, self.config, self.cutoff + timedelta(minutes=1)))
        self.assertEqual((True, None), due_jobs(self.store, self.config, self.cutoff + timedelta(minutes=30)))
        self.assertEqual(0, self.store.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0])

    def test_daily_queue_keeps_first_text_and_sends_in_order_once(self):
        day = "2026-11-20"
        at = self.cutoff.isoformat()
        self.assertEqual(["one", "two"], commit_daily(self.store, day, at, ["one", "two"]))
        self.assertEqual(["one", "two"], commit_daily(self.store, day, at, ["changed"]))

        class FakeTelegram:
            sent = []
            def send(self, text):
                self.sent.append(text)
                return "SENT", str(len(self.sent))

        bot = FakeTelegram()
        self.assertEqual("DONE", deliver(self.store, bot, f"daily:{day}:"))
        self.assertEqual("DONE", deliver(self.store, bot, f"daily:{day}:"))
        self.assertEqual(["one", "two"], bot.sent)
        self.assertEqual((True, None), due_jobs(self.store, self.config, self.cutoff, queue=True))

    def test_disabled_project_is_not_sent_an_already_queued_event(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO deliveries(key,body) VALUES ('event:paused:a','old')")
        class Offline:
            def send(self, text):
                raise AssertionError("paused project was sent")
        self.assertEqual("DONE", deliver(self.store, Offline(), "event:", allowed_event_projects=set()))

    def test_long_report_is_one_document_and_delivery_is_idempotent(self):
        day = "2026-11-20"
        bodies = [f"Раздел {i}" for i in range(12)]
        commit_daily(self.store, day, self.cutoff.isoformat(), bodies, attachment=True)

        class Bot:
            sent = []

            def send_document(self, body, date, index):
                self.sent.append((body, date, index))
                return "SENT", "1"

        bot = Bot()
        self.assertEqual("DONE", deliver(self.store, bot, f"daily:{day}:"))
        self.assertEqual("DONE", deliver(self.store, bot, f"daily:{day}:"))
        self.assertEqual([("\n\n".join(bodies), day, "1")], bot.sent)

    def test_price_monitor_absence_stays_not_seen(self):
        self.assertEqual("ACTIVE", observe(lambda _: {"123": object()}, None, "123")["outcome"])
        self.assertEqual("NOT_SEEN", observe(lambda _: {}, None, "123")["outcome"])
