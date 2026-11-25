from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
import json

from avito_monitor.adapters import Fixture, literal
from avito_monitor.config import load_config
from avito_monitor.model import ScanError, scan
from avito_monitor.pilot import single_removal
from avito_monitor.removals import (
    apply_scheduled_removals, follow_operations, plan_removals, record_avito_evidence,
    record_feed_evidence, record_manual_restore,
)
from avito_monitor.storage import locked_db


OPERATION = "96b70afd-b617-47ac-8913-839a5e7f102c"
TARGET = "https://docs.google.com/spreadsheets/d/demo-feed-north/edit#gid=20&range=A4"


class RemovalWorkflowTests(TestCase):
    def setUp(self):
        self.project = load_config("examples/config.json")["projects"][0]
        self.fixture = Fixture.from_path("examples/fixture.json")
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.directory = Path(workspace.name)
        lock = locked_db(self.directory / "state.sqlite")
        self.store = lock.__enter__()
        self.addCleanup(lock.__exit__, None, None, None)

    def test_plan_holds_across_new_baselines_until_reviewed(self):
        self.project["removal_limits"] = {
            "max_candidates": 4, "max_newly_empty": 0, "max_lost_matches": 0,
        }
        before, diagnostics = scan(self.project, self.fixture)
        self.assertIn("COMPLETE_BASELINE_REQUIRED", plan_removals(self.store, self.project, before, diagnostics)["reasons"])
        self.store.reconcile(self.project, before, diagnostics, "baseline")
        self.fixture.books["demo-price-north", 10][1][4] = {}
        after, diagnostics = scan(self.project, self.fixture)
        result = plan_removals(self.store, self.project, after, diagnostics)
        self.assertEqual(1, result["newly_empty"])
        self.assertIn("TOO_MANY_NEWLY_EMPTY", result["reasons"])
        self.store.reconcile(self.project, after, diagnostics, "after")
        self.assertEqual(["REVIEW_HOLD"], plan_removals(self.store, self.project, after, diagnostics)["reasons"])
        self.assertTrue(self.store.release_removal_hold(self.project["id"], "Проверена тестовая смена цены"))
        self.assertEqual([], plan_removals(self.store, self.project, after, diagnostics)["reasons"])

    def test_one_operation_keeps_proof_through_manual_restore(self):
        stored_path = self.directory / "copied.json"
        self.fixture.save_new_copy(stored_path)
        self.assertEqual(
            "WAITING_FEED_CONFIRMATION", single_removal(self.store, self.fixture, self.project, TARGET, OPERATION),
        )
        saved = Fixture.from_path(stored_path)
        self.assertEqual({}, saved.books["demo-feed-north", 20][3][0])
        self.assertEqual(literal(9123456789), saved.books["demo-feed-north", 20][3][1])
        self.assertEqual(literal("000123"), Fixture.from_path("examples/fixture.json").books["demo-feed-north", 20][3][0])
        blocked = record_feed_evidence(
            self.store, self.project, OPERATION, "examples/feed-before.xml", "examples/feed-id-missing.xml", ("Title",),
        )
        self.assertEqual("WAITING_FEED_CONFIRMATION", blocked["state"])
        verified = record_feed_evidence(
            self.store, self.project, OPERATION, "examples/feed-before.xml", "examples/feed-row-removed.xml", ("Title",),
        )
        self.assertEqual("WAITING_AVITO_CONFIRMATION", verified["state"])
        observation = {
            "marketplace_id": "9123456789", "observed_at": datetime.now(timezone.utc).isoformat(),
            "source": "price-monitor-active-list", "outcome": "NOT_SEEN", "reference": "fixture",
        }
        self.assertEqual("WAITING_AVITO_CONFIRMATION", record_avito_evidence(
            self.store, self.project, OPERATION, observation,
        )["state"])
        with self.assertRaisesRegex(ScanError, "не подтверждает"):
            record_avito_evidence(self.store, self.project, OPERATION, dict(observation, outcome="INACTIVE_CONFIRMED"))
        with self.assertRaisesRegex(ScanError, "не совпадает"):
            record_avito_evidence(self.store, self.project, OPERATION, dict(observation, marketplace_id="123"))
        self.assertEqual("REMOVED_FROM_AVITO", record_avito_evidence(
            self.store, self.project, OPERATION, dict(observation, source="item-status", outcome="INACTIVE_CONFIRMED"),
        )["state"])
        saved.books["demo-price-north", 10][2][4] = literal(500)
        saved.books["demo-feed-north", 20][3][0] = literal("000123")
        positions, _ = scan(self.project, saved)
        self.assertEqual("RESTORED", record_manual_restore(self.store, self.project, OPERATION, positions))
        record = self.store.get_operation(OPERATION)
        self.assertEqual("000123", json.loads(record["data"])["id_cell"]["userEnteredValue"]["stringValue"])
        feed = self.store.latest_operation_event(OPERATION, "FEED_EVIDENCE")
        self.assertEqual(64, len(json.loads(feed["evidence"])["after_sha256"]))

    def test_coverage_reports_a_populated_unmapped_tab_and_variant(self):
        self.project["coverage_targets"] = [
            {"gid": 20, "header_row": 2, "name": "Телефоны"},
            {"gid": 21, "header_row": 2, "name": "Другие товары"},
        ]
        self.fixture.books["demo-feed-north", 21] = [
            [literal("Товар")], [literal("Title"), literal("Id")],
            [literal("Незнакомый товар"), literal("retained")],
        ]
        phones = self.fixture.books["demo-feed-north", 20]
        phones.append([literal("ignored"), {}, literal("Other"), *([{}] * 8)])
        matched, warnings = scan(self.project, self.fixture)
        self.assertEqual(4, len(matched))
        self.assertEqual([("UNMAPPED_TARGET", 1, 1)] * 2, [
            (warning["code"], warning["rows"], warning["with_id"]) for warning in warnings
        ])

    def test_scheduled_clear_status_and_manual_restore_without_feed(self):
        self.project["removal_confirmation"] = "avito_status"

        class Observer:
            outcome = "ACTIVE"

            def observe(self, project, listing, technical_id, **options):
                if options.get("require_binding"):
                    assert technical_id == "000123"
                return {
                    "source": "item-status", "marketplace_id": listing, "outcome": self.outcome,
                    "observed_at": datetime.now(timezone.utc).isoformat(), "reference": "fixture",
                }

        observer = Observer()
        positions, _ = scan(self.project, self.fixture)
        apply_scheduled_removals(self.store, self.project, self.fixture, observer, positions)
        position = next(p for p in positions.values() if p.target.link == TARGET)
        row = self.store.latest_backup(self.project["id"], position.identity)
        self.assertEqual("WAITING_AVITO_CONFIRMATION", row["state"])
        self.assertEqual({}, self.fixture.books["demo-feed-north", 20][3][0])
        self.assertEqual("000123", json.loads(row["data"])["id_cell"]["userEnteredValue"]["stringValue"])
        positions, _ = scan(self.project, self.fixture)
        self.assertEqual([], follow_operations(self.store, self.project, observer, positions))
        self.assertEqual("WAITING_AVITO_CONFIRMATION", self.store.get_operation(row["operation"])["state"])
        observer.outcome = "INACTIVE_CONFIRMED"
        self.assertEqual([], follow_operations(self.store, self.project, observer, positions))
        self.assertEqual("REMOVED_FROM_AVITO", self.store.get_operation(row["operation"])["state"])
        self.fixture.books["demo-price-north", 10][2][4] = literal(500)
        self.fixture.books["demo-feed-north", 20][3][0] = literal("000123")
        positions, _ = scan(self.project, self.fixture)
        self.assertEqual([], follow_operations(self.store, self.project, observer, positions))
        self.assertEqual("RESTORED", self.store.get_operation(row["operation"])["state"])
