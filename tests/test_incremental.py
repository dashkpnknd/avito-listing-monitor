from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from avito_monitor.adapters import Fixture, literal
from avito_monitor.config import validate
from avito_monitor.drive import CursorRejected, DriveChanges
from avito_monitor.incremental import daily_if_due, queue_first_report, saved_report, tick
from avito_monitor.model import ReadQuota, ScanError
from avito_monitor.storage import locked_db


START = datetime(2026, 11, 25, 3, 0, tzinfo=timezone.utc)


class Reader(Fixture):
    def __init__(self, books):
        super().__init__(books)
        self.reads = Counter()
        self.failures = {}

    def grid(self, book, spec):
        self.reads[book] += 1
        if book in self.failures:
            raise self.failures[book]
        return super().grid(book, spec)


class Journal:
    def __init__(self):
        self.calls = []
        self.pages = {}

    def start(self, _stream):
        return "page-1"

    def file_drive(self, _book):
        return ""

    def page(self, _stream, token):
        self.calls.append(token)
        response = self.pages.pop(token, (token, False, set()))
        if isinstance(response, Exception):
            raise response
        return response


def sample_config(count, rows=1, **sync):
    projects = [
        {"id": f"shop-{i:03d}", "name": f"Магазин {i}", "mode": "monitoring",
         "launch_date": "2026-11-01", "report_exclude_days": 10,
         "source_book": f"source-{i}", "target_book": f"target-{i}"}
        for i in range(count)
    ]
    data = {}
    for i in range(count):
        data[f"source-{i}"] = {10: [["Title", "Price"]] + [[f"Позиция {r}", None] for r in range(rows)]}
        data[f"target-{i}"] = {20: [["Title", "Id"]] + [[f"Позиция {r}", None] for r in range(rows)]}
    config = validate({
        "report_timezone": "Europe/Moscow", "telegram_enabled": True,
        "schedule": {"check_every_minutes": 60, "report_time": "08:00", "report_window_minutes": 10,
                     "report_weekdays": list(range(7))},
        "sync": {"audit_every_minutes": 180, "max_snapshot_age_minutes": 210, **sync},
        "projects": projects,
    }, Path("."))
    return config, Reader(data)


class IncrementalTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.sqlite"
        self.journal = Journal()

    def update(self, store, config, reader, minute):
        return tick(store, config, config["projects"], START + timedelta(minutes=minute),
                    reader=lambda _: reader, drive_factory=lambda: self.journal)[0]

    def test_first_report_waits_for_sync_and_queues_once_without_launch_date(self):
        config, reader = sample_config(1)
        project, = config["projects"]
        project.update(launch_date=None, first_report=True)
        reader.books["source-0", 10][1][1] = literal(15000)
        with locked_db(self.path) as store:
            self.assertFalse(queue_first_report(store, config, project, START))
            self.update(store, config, reader, 0)
            self.assertTrue(queue_first_report(store, config, project, START))
            self.assertTrue(queue_first_report(store, config, project, START))
            key, body = store.db.execute("SELECT key,body FROM deliveries").fetchone()
            self.assertEqual("first:shop-000:1", key)
            self.assertIn("первая сверка", body)
            self.assertIn("Позиция 0", body)
            self.assertEqual([], daily_if_due(store, config, [project], START + timedelta(hours=2), queue=True))
            self.assertEqual(1, store.db.execute("SELECT count(*) FROM deliveries").fetchone()[0])

    def test_three_hundred_projects_change_on_one_side_and_report_without_reads(self):
        config, reader = sample_config(300, rows=100, max_projects_per_tick=6)
        with locked_db(self.path) as store:
            for minute in range(50):
                result = self.update(store, config, reader, minute)
                self.assertEqual(6, result["refreshed"])
            self.assertEqual(600, sum(reader.reads.values()))
            self.assertEqual(300, store.db.execute("SELECT count(*) FROM snapshots").fetchone()[0])
        reader.reads.clear()
        reader.books["source-299", 10][1][1] = literal(15000)
        self.journal.pages["page-1"] = ("page-2", False, {"source-299", "unregistered-file"})
        with locked_db(self.path) as store:
            self.assertEqual(1, self.update(store, config, reader, 51)["refreshed"])
            self.assertEqual({"source-299": 1}, dict(reader.reads))
            self.assertEqual(0, self.update(store, config, reader, 52)["refreshed"])
            self.assertEqual(0, self.update(store, config, reader, 119)["refreshed"])
        cutoff = START + timedelta(hours=2)
        # Telegram's timer reads the database while the sync process holds its
        # worker lock for a network bound task.
        with locked_db(self.path), locked_db(self.path, role="delivery") as store:
            bodies = daily_if_due(store, config, config["projects"], cutoff, queue=True)
            self.assertIn("Магазин 299", "\n".join(bodies))
            self.assertIn("НЕ ВСЕ ВЫЛОЖЕНЫ ПОЗИЦИИ:\n• 20 — 1 шт.", "\n".join(bodies))
            self.assertEqual([], daily_if_due(store, config, config["projects"], cutoff, queue=True))
            self.assertEqual(1, store.db.execute("SELECT count(*) FROM deliveries WHERE key LIKE 'event:%'").fetchone()[0])
        self.assertEqual({"source-299": 1}, dict(reader.reads))

    def test_failures_retry_only_changed_book_and_keep_other_projects_fresh(self):
        config, reader = sample_config(2)
        with locked_db(self.path) as store:
            self.update(store, config, reader, 0)
            reader.reads.clear()
            reader.failures["source-0"] = ScanError("Прайс недоступен")
            self.journal.pages["page-1"] = ("page-2", False, {"source-0", "target-1"})
            stats = self.update(store, config, reader, 1)
            self.assertEqual({"refreshed": 1, "failed": 1, "deferred": 0}, stats)
            self.assertEqual({"source-0": 1, "target-1": 1}, dict(reader.reads))
            bodies = saved_report(store, config, config["projects"], START + timedelta(minutes=1))
            self.assertIn("Магазин 0 · не удалось обновить таблицы", "\n".join(bodies))
            self.assertNotIn("Магазин 1 ·", "\n".join(bodies))
            reader.failures.clear()
            self.assertEqual(1, self.update(store, config, reader, 2)["refreshed"])
            self.assertEqual({"source-0": 2, "target-1": 1}, dict(reader.reads))

    def test_partial_journal_cursor_and_tasks_survive_restart(self):
        config, reader = sample_config(2, max_change_pages=1)
        with locked_db(self.path) as store:
            self.update(store, config, reader, 0)
            reader.reads.clear()
            self.journal.pages["page-1"] = ("page-2", True, {"source-0"})
            self.update(store, config, reader, 1)
            self.assertIsNone(store.db.execute("SELECT caught_up FROM sync_journal").fetchone()[0])
        with locked_db(self.path) as store:
            self.journal.pages["page-2"] = ("page-3", False, {"target-1"})
            self.update(store, config, reader, 2)
            self.assertEqual({"source-0": 1, "target-1": 1}, dict(reader.reads))
            self.assertEqual("page-3", store.db.execute("SELECT cursor FROM sync_journal").fetchone()[0])

    def test_quota_does_not_turn_a_partial_read_into_blank_prices(self):
        config, reader = sample_config(1)
        with locked_db(self.path) as store:
            self.update(store, config, reader, 0)
            reader.failures["source-0"] = ReadQuota("Лимит")
            self.journal.pages["page-1"] = ("page-2", False, {"source-0"})
            stats = self.update(store, config, reader, 1)
            self.assertEqual({"refreshed": 0, "failed": 0, "deferred": 1}, stats)
            task = store.db.execute("SELECT mask,attempts FROM sync_tasks").fetchone()
            self.assertEqual((1, 0), tuple(task))
            self.assertIn("Позиция 0", store.snapshot("shop-000")["data"])

    def test_rejected_token_queues_recovery(self):
        config, reader = sample_config(1)
        with locked_db(self.path) as store:
            self.update(store, config, reader, 0)
            self.journal.pages["page-1"] = CursorRejected("Курсор отклонён")
            self.update(store, config, reader, 1)
            self.assertEqual("", store.db.execute("SELECT cursor FROM sync_journal").fetchone()[0])
            self.update(store, config, reader, 2)
            self.assertEqual("page-1", store.db.execute("SELECT cursor FROM sync_journal").fetchone()[0])

    def test_empty_drive_page_with_new_cursor_is_valid(self):
        api = object.__new__(DriveChanges)
        api.get = lambda *_: {"newStartPageToken": "page-2"}
        self.assertEqual(("page-2", False, set()), api.page(None, "page-1"))

    def test_formula_poll_only_reads_the_price_book(self):
        config, reader = sample_config(2, formula_every_minutes=20)
        reader.books["source-1", 10][1][1] = {
            "userEnteredValue": {"formulaValue": "=VALUE(100)"},
            "effectiveValue": {"numberValue": 100},
        }
        with locked_db(self.path) as store:
            self.update(store, config, reader, 0)
            reader.reads.clear()
            reader.books["source-1", 10][1][1]["effectiveValue"]["numberValue"] = 200
            self.assertEqual(1, self.update(store, config, reader, 20)["refreshed"])
            self.assertEqual({"source-1": 1}, dict(reader.reads))
            self.assertIn('"price": "200"', store.snapshot("shop-001")["data"])

    def test_failed_shared_drive_does_not_hide_unrelated_projects(self):
        config, reader = sample_config(2)
        self.journal.file_drive = lambda book: "team-drive" if book.endswith("-0") else ""
        initial = self.journal.page

        def page(drive, token):
            if drive:
                raise ScanError("Недоступен диск")
            return initial(drive, token)

        self.journal.page = page
        with locked_db(self.path) as store:
            self.update(store, config, reader, 0)
            self.update(store, config, reader, 1)
            text = "\n".join(saved_report(store, config, config["projects"], START + timedelta(minutes=1)))
            self.assertIn("Магазин 0 ·", text)
            self.assertNotIn("Магазин 1 ·", text)
