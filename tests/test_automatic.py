from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
import json

from avito_monitor.adapters import Fixture, literal
from avito_monitor.config import load_config
from avito_monitor.model import scan
from avito_monitor.registration import register_project


class AutomaticMatchingTests(TestCase):
    def setUp(self):
        self.fixture = Fixture.from_path("examples/fixture.json")
        self.project = load_config("examples/config.json")["projects"][0]
        self.project.pop("routes")
        self.price = self.fixture.books["demo-price-north", 10]
        self.feed = self.fixture.books["demo-feed-north", 20]

    def test_matches_split_blocks_without_routes(self):
        positions, diagnostics = scan(self.project, self.fixture)
        self.assertEqual([], diagnostics)
        self.assertEqual(4, len(positions))
        self.assertEqual(["NEEDS_PUBLICATION", "NEEDS_REMOVAL"], sorted(
            position.issue for position in positions.values() if position.issue
        ))
        variants = {position.title: position.report_title for position in positions.values()}
        self.assertEqual("iPhone Example, 128 ГБ, синий, eSIM, ОЗУ 4 ГБ, Новое", variants["iPhone Example, синий"])
        self.assertEqual("Samsung Galaxy Example+, 256 ГБ, серый, SIM, ОЗУ 12 ГБ, Новое", variants["Galaxy Example+"])

    def test_unknown_sim_is_explicit_without_guessing(self):
        self.price[1][1] = literal("Не знаю")
        self.feed[2][5] = literal("Не знаю")
        positions, diagnostics = scan(self.project, self.fixture)
        self.assertEqual([], diagnostics)
        publication, = [position for position in positions.values() if position.issue == "NEEDS_PUBLICATION"]
        self.assertEqual("iPhone Example, 128 ГБ, синий, SIM не указана, ОЗУ 4 ГБ, Новое", publication.report_title)

    def test_duplicate_source_preserves_other_known_positions(self):
        self.price.append(deepcopy(self.price[2]))
        positions, diagnostics = scan(self.project, self.fixture)
        self.assertIn("MATCH_UNCERTAIN", [warning["code"] for warning in diagnostics])
        self.assertEqual(3, len(positions))
        self.assertFalse(any(position.issue == "NEEDS_REMOVAL" for position in positions.values()))

    def test_same_technical_id_on_unmatched_target_prevents_removal(self):
        duplicate = deepcopy(self.feed[3])
        duplicate[3] = literal("unmatched phone model")
        self.feed.append(duplicate)
        positions, diagnostics = scan(self.project, self.fixture)
        self.assertIn("Повторяется Id или номер объявления", [warning["detail"] for warning in diagnostics])
        self.assertFalse(any(position.issue == "NEEDS_REMOVAL" for position in positions.values()))

    def test_register_accepts_links_and_needs_no_route_config(self):
        with TemporaryDirectory() as directory:
            config = Path(directory) / "projects.json"
            config.write_text(json.dumps({"report_timezone": "Europe/Moscow", "projects": []}))
            args = SimpleNamespace(
                config=config, project="new-city", name="LimeStore", city="New City",
                source="https://docs.google.com/spreadsheets/d/example-source/edit?gid=0",
                target="https://docs.google.com/spreadsheets/d/example-target/edit#gid=10",
                launch_date="2026-11-25", avito_profile="AVITO_NEW_CITY",
            )
            self.assertEqual("registered", register_project(args)["result"])
            self.assertEqual("already_registered", register_project(args)["result"])
            project, = load_config(config)["projects"]
            self.assertEqual("example-source", project["source_book"])
            self.assertEqual("example-target", project["target_book"])
            self.assertNotIn("routes", project)
