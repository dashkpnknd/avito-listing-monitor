from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from avito_monitor.feed_evidence import compare_feeds


class FeedEvidenceTests(TestCase):
    def test_distinguishes_tag_omission_from_record_removal(self):
        before = "examples/feed-before.xml"
        self.assertEqual(
            "ROW_PRESENT_WITHOUT_ID",
            compare_feeds(before, "examples/feed-id-missing.xml", "000123")["outcome"],
        )
        self.assertEqual(
            "ROW_ABSENT_FROM_XML",
            compare_feeds(before, "examples/feed-row-removed.xml", "000123")["outcome"],
        )

    def test_empty_feed_does_not_pass_as_one_removed_ad(self):
        with TemporaryDirectory() as temp:
            after = Path(temp) / "empty.xml"
            after.write_text("<Ads/>")
            result = compare_feeds("examples/feed-before.xml", after, "000123")
            self.assertEqual("INCONCLUSIVE", result["outcome"])
            self.assertEqual("OTHER_ROWS_CHANGED", result["reason"])

    def test_duplicate_title_requires_stable_extra_field(self):
        with TemporaryDirectory() as temp:
            before = Path(temp) / "before.xml"
            after = Path(temp) / "after.xml"
            row_a = "<Ad><Id>a</Id><Title>Model</Title><Color>Blue</Color></Ad>"
            row_b = "<Ad><Id>b</Id><Title>Model</Title><Color>Green</Color></Ad>"
            before.write_text(f"<Ads>{row_a}{row_b}</Ads>")
            after.write_text(f"<Ads>{row_b}</Ads>")
            self.assertEqual("INCONCLUSIVE", compare_feeds(before, after, "a")["outcome"])
            self.assertEqual(
                "ROW_ABSENT_FROM_XML",
                compare_feeds(before, after, "a", ("Title", "Color"))["outcome"],
            )
