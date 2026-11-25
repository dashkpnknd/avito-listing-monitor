from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import json

from avito_monitor.adapters import Fixture, literal
from avito_monitor.config import load_config
from avito_monitor.legacy import project_metadata
from avito_monitor.model import ScanError, price_state, scan
from avito_monitor.pilot import inspect_feed, single_removal
from avito_monitor.report import included, report, size
from avito_monitor.storage import locked_db
from avito_monitor.telegram import deliver


OPERATION = "96b70afd-b617-47ac-8913-839a5e7f102c"
TARGET = "https://docs.google.com/spreadsheets/d/demo-feed-north/edit#gid=20&range=A4"


class MonitorTests(TestCase):
    def setUp(self):
        self.config = load_config("examples/config.json")
        self.north, self.south = self.config["projects"]
        self.fixture = Fixture.from_path("examples/fixture.json")
        self.price = self.fixture.books["demo-price-north", 10]
        self.feed = self.fixture.books["demo-feed-north", 20]
        self.enter_database()

    def enter_database(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        locked = locked_db(Path(directory.name) / "state.sqlite")
        self.store = locked.__enter__()
        self.addCleanup(locked.__exit__, None, None, None)

    def take_scan(self, project=None, hour=8, notifications=True):
        project = project or self.north
        positions, issues = scan(project, self.fixture)
        now = datetime(2026, 11, 19, hour, tzinfo=timezone.utc)
        self.store.reconcile(project, positions, issues, now.isoformat(), notifications)
        return positions, issues, now

    def test_matrix_and_separate_phone_blocks(self):
        positions, diagnostics = scan(self.north, self.fixture)
        self.assertEqual([], diagnostics)
        self.assertEqual(
            {"синий": "NEEDS_PUBLICATION", "белый": "NEEDS_REMOVAL", "чёрный": None, "Galaxy Example+": None},
            {p.title.split(", ")[-1]: p.issue for p in positions.values()},
        )

    def test_only_confirmed_empty_is_empty(self):
        for value in [0, -1, "нет", "под заказ", "  ", "120 кредит", True]:
            with self.subTest(value=value):
                self.assertEqual("INVALID", price_state(literal(value))[0])
        formula = {"userEnteredValue": {"formulaValue": '=""'}, "effectiveValue": {"stringValue": ""}}
        self.assertEqual("INVALID", price_state(formula)[0])
        self.assertEqual("READ_ERROR", price_state({"effectiveValue": {"errorValue": {"type": "REF"}}})[0])
        self.assertEqual(("POSITIVE", "15000"), price_state(literal("15\u00a0000")))
        self.assertEqual(("EMPTY", None), price_state({}))

    def test_diagnostic_preserves_incident_and_never_clears(self):
        self.take_scan()
        self.price[2][4] = literal(0)
        positions, diagnostics, _ = self.take_scan(hour=9)
        self.assertEqual(["INVALID_CELL"], [d["code"] for d in diagnostics])
        self.assertEqual(2, len(self.store.issues(self.north["id"])))
        with self.assertRaises(ScanError):
            single_removal(self.store, self.fixture, self.north, TARGET, OPERATION)
        self.assertEqual(literal("000123"), self.feed[3][0])
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM id_registry").fetchone()[0])

    def test_duplicate_or_removed_source_does_not_become_empty(self):
        self.price.append(deepcopy(self.price[2]))
        positions, diagnostics = scan(self.north, self.fixture)
        self.assertEqual(["MATCH_UNCERTAIN"], [d["code"] for d in diagnostics])
        self.assertFalse(any(p.target.link == TARGET for p in positions.values()))
        self.price.pop()
        self.price.pop(2)
        positions, diagnostics = scan(self.north, self.fixture)
        self.assertEqual(["MATCH_UNCERTAIN"], [d["code"] for d in diagnostics])
        self.assertFalse(any(p.target.link == TARGET for p in positions.values()))

    def test_baseline_close_and_reopen_once(self):
        self.take_scan()
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM deliveries").fetchone()[0])
        self.feed[2][0] = literal("filled")
        self.take_scan(hour=9)
        self.feed[2][0] = {}
        self.take_scan(hour=10)
        self.take_scan(hour=11)
        self.assertEqual(1, self.store.db.execute("SELECT count(*) FROM deliveries").fetchone()[0])
        self.feed[2][0] = literal("filled-again")
        self.take_scan(hour=12)
        self.assertEqual("SUPERSEDED", self.store.db.execute("SELECT state FROM deliveries").fetchone()[0])

    def test_pilot_preserves_exact_id_and_manual_restore(self):
        self.take_scan()
        avito_id = self.feed[3][1]
        self.assertEqual("WAITING_FEED_CONFIRMATION", single_removal(self.store, self.fixture, self.north, TARGET, OPERATION))
        self.assertEqual("WAITING_FEED_CONFIRMATION", single_removal(self.store, self.fixture, self.north, TARGET, OPERATION))
        self.assertEqual({}, self.feed[3][0])
        self.assertEqual(avito_id, self.feed[3][1])
        saved = self.store.db.execute("SELECT * FROM id_registry").fetchall()
        self.assertEqual(1, len(saved))
        reserved = json.loads(saved[0]["data"])["id_cell"]
        self.assertEqual("000123", reserved["userEnteredValue"]["stringValue"])
        self.price[2][4] = literal(30000)
        self.take_scan(hour=9)
        issues = [json.loads(i["data"]) for i in self.store.issues(self.north["id"])]
        self.assertTrue(any(i.get("restore_available") for i in issues))
        self.assertEqual({}, self.feed[3][0])
        self.feed[3][0] = reserved
        self.take_scan(hour=10)
        self.assertEqual(1, len(self.store.issues(self.north["id"])))

    def test_backup_failure_never_clears_id(self):
        with patch.object(self.store, "reserve", side_effect=OSError("fixture disk failure")):
            with self.assertRaises(OSError):
                single_removal(self.store, self.fixture, self.north, TARGET, OPERATION)
        self.assertEqual(literal("000123"), self.feed[3][0])

    def test_write_unknown_is_not_repeated(self):
        with patch.object(self.fixture, "clear_id", side_effect=OSError("fixture interrupted")) as clear:
            with self.assertRaises(OSError):
                single_removal(self.store, self.fixture, self.north, TARGET, OPERATION)
            self.assertEqual("WRITE_STARTED", single_removal(self.store, self.fixture, self.north, TARGET, OPERATION))
            clear.assert_called_once()

    def test_other_cycle_cannot_acquire_lock(self):
        with self.assertRaises(BlockingIOError):
            with locked_db(self.store.db.execute("PRAGMA database_list").fetchone()[2]):
                self.fail("Overlapping cycle started")

    def test_feed_keeps_row_without_id(self):
        before = inspect_feed("examples/feed-before.xml", "000123", "iPhone Example, белый")
        unresolved = inspect_feed("examples/feed-id-missing.xml", "000123", "iPhone Example, белый")
        removed = inspect_feed("examples/feed-row-removed.xml", "000123", "iPhone Example, белый")
        self.assertTrue(before["old_id_present"])
        self.assertFalse(unresolved["old_id_present"])
        self.assertTrue(unresolved["same_title_present"])
        self.assertFalse(removed["same_title_present"])

    def test_partial_project_does_not_hide_other_projects(self):
        self.take_scan()
        self.fixture.books.pop(("demo-price-north", 10))
        _, _, cutoff = self.take_scan(hour=9)
        self.take_scan(self.south, hour=9)
        text = "\n".join(report(self.store, [self.north, self.south], cutoff, "Europe/Moscow", detailed=True))
        self.assertIn("Не удалось проверить полностью", text)
        self.assertIn("Watch Example", text)
        self.assertNotIn("белый", text)
        snapshot = self.store.db.execute(
            "SELECT checked FROM complete_snapshots WHERE project=?", (self.north["id"],)
        ).fetchone()
        self.assertIn("T08:", snapshot["checked"])

    def test_report_launch_boundary_and_line_limit(self):
        self.south["launch_date"] = "2026-11-10"
        self.assertFalse(included(self.south, date(2026, 11, 19)))
        self.assertTrue(included(self.south, date(2026, 11, 20)))
        for number in range(200):
            title = f"Demo watch 🟠 {number:03}"
            self.fixture.books["demo-price-south", 30].append([literal(title), literal(10)])
            self.fixture.books["demo-feed-south", 40].append([{}, literal(title)])
        self.south["launch_date"] = "2026-11-01"
        _, _, cutoff = self.take_scan(self.south)
        pages = report(self.store, [self.south], cutoff, "Europe/Moscow", detailed=True)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(size(page) <= 4096 for page in pages))
        self.assertIn("Demo watch 🟠 199", pages[-1])
        self.assertIn(f"1/{len(pages)}", pages[0])
        self.assertTrue(all("🟠 НЕ ВСЕ ВЫЛОЖЕНЫ ПОЗИЦИИ:" in page for page in pages))

    def test_daily_summary_counts_categories_and_keeps_project_blocks(self):
        _, _, cutoff = self.take_scan()
        self.take_scan(self.south)
        text, = report(self.store, [self.north, self.south], cutoff, "Europe/Moscow")
        self.assertIn("🏬 Учебный магазин | Север\n\n🟠 НЕ ВСЕ ВЫЛОЖЕНЫ ПОЗИЦИИ:\n• iPhone — 1 шт.", text)
        self.assertIn("🔴 УБРАТЬ ИЗ ВЫКЛАДКИ ПОЗИЦИИ:\n• iPhone — 1 шт.", text)
        self.assertIn("• iPhone — 1 шт.\n  ↳ ", text)
        self.assertIn("iPhone Example, 128 ГБ, синий, eSIM, ОЗУ 4 ГБ, Новое", text)
        self.assertIn("iPhone Example, 128 ГБ, белый, eSIM, ОЗУ 4 ГБ, Новое", text)
        self.assertIn("Watch Example", text)
        self.assertIn("\n\n━━━━━━━━━━━━━━━━━━━━\n\n🏬 Учебный магазин | Юг", text)
        self.assertNotIn("https://", text)

    def test_summary_counts_an_unconfirmed_removal_once_until_confirmation(self):
        project = self.north
        _, _, cutoff = self.take_scan(project)
        single_removal(self.store, self.fixture, project, TARGET, OPERATION)
        before_refresh, = report(self.store, [project], cutoff, "Europe/Moscow")
        self.assertIn("УБРАТЬ ИЗ ВЫКЛАДКИ ПОЗИЦИИ:\n• iPhone — 1 шт.", before_refresh)
        _, _, cutoff = self.take_scan(project, hour=9)
        while_pending, = report(self.store, [project], cutoff, "Europe/Moscow")
        self.assertIn("УБРАТЬ ИЗ ВЫКЛАДКИ ПОЗИЦИИ:\n• iPhone — 1 шт.", while_pending)
        self.store.transition(OPERATION, "WAITING_FEED_CONFIRMATION", "REMOVED_FROM_AVITO")
        after_confirmation, = report(self.store, [project], cutoff, "Europe/Moscow")
        self.assertIn("УБРАТЬ ИЗ ВЫКЛАДКИ ПОЗИЦИИ:\n• Нет позиций", after_confirmation)

    def test_ambiguous_delivery_is_never_repeated(self):
        class Fake:
            sent = []
            def send(self, body):
                self.sent.append(body)
                return "UNCERTAIN", None

        with self.store.db:
            self.store.db.execute("INSERT INTO deliveries(key,body) VALUES ('daily:2026-11-19:1','test')")
        fake = Fake()
        self.assertEqual("UNCERTAIN", deliver(self.store, fake, "daily:2026-11-19:"))
        self.assertEqual("UNCERTAIN", deliver(self.store, fake, "daily:2026-11-19:"))
        self.assertEqual(["test"], fake.sent)

    def test_legacy_projection_does_not_import_credentials(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "projects.json"
            path.write_text(json.dumps([{
                "name": "Sample", "client_spreadsheet_id": "a", "autoload_spreadsheet_id": "b",
                "avito_client_secret": "demo-placeholder",
            }]))
            projected = project_metadata(path)
            self.assertEqual([{"name": "Sample", "source_book": "a", "target_book": "b"}], list(projected.values()))
