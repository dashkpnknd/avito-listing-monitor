from unittest import TestCase
from xml.etree import ElementTree as ET

from avito_monitor.feed_preview import compare


class FeedPreviewTest(TestCase):
    def setUp(self):
        self.selected = {"row": 3, "id": "000123", "title": "Watch A & B", "price": 17}
        self.other = {"row": 4, "id": "other", "title": "Watch C", "price": 18}
        self.before = [self.selected, self.other]
        self.after = [dict(self.selected, id=None), self.other]

    def test_same_row_can_remain_in_feed_when_only_id_tag_is_omitted(self):
        previews = compare(self.before, self.after, 3)
        before = ET.fromstring(previews["before-omit-id.xml"]).findall("Ad")
        after = ET.fromstring(previews["after-omit-id.xml"]).findall("Ad")
        self.assertEqual("000123", before[0].findtext("Id"))
        self.assertEqual("Watch A & B", after[0].findtext("Title"))
        self.assertIsNone(after[0].find("Id"))
        self.assertEqual(ET.tostring(before[1]), ET.tostring(after[1]))

    def test_explicit_filter_only_removes_selected_row(self):
        previews = compare(self.before, self.after, 3)
        before = ET.fromstring(previews["before-strict.xml"]).findall("Ad")
        after = ET.fromstring(previews["after-strict.xml"]).findall("Ad")
        self.assertEqual(["000123", "other"], [ad.findtext("Id") for ad in before])
        self.assertEqual(["other"], [ad.findtext("Id") for ad in after])

    def test_refuses_comparison_when_another_cell_changed(self):
        with self.assertRaisesRegex(ValueError, "другие данные"):
            compare(self.before, [self.after[0], dict(self.other, price=20)], 3)
