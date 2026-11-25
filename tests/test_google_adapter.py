from unittest import TestCase

from avito_monitor.adapters import GoogleReadOnly, literal


class GoogleAdapterTests(TestCase):
    def test_single_batched_request_paths_and_typed_cells(self):
        reader = object.__new__(GoogleReadOnly)
        reader.metadata = {"book": {
            10: {"title": "Prices", "gridProperties": {"columnCount": 8}},
            20: {"title": "Today's stock", "gridProperties": {"columnCount": 4}},
        }}
        reader.cache = {}
        requests = []

        def get(book, params):
            requests.append((book, params["ranges"]))
            fields = params["fields"]
            self.assertEqual(fields.count("("), fields.count(")"))
            return {"sheets": [
                {"properties": {"sheetId": 20}, "data": [{}]},
                {"properties": {"sheetId": 10}, "data": [{"rowData": [{"values": [literal(500)]}]}]},
            ]}

        reader.get = get
        reader.prefetch("book", [{"gid": 10}, {"gid": 20}])
        self.assertEqual([("book", ["'Prices'!A1:H", "'Today''s stock'!A1:D"])], requests)
        self.assertEqual((0, [[literal(500)]]), reader.grid("book", {"gid": 10}))
        self.assertEqual((0, []), reader.grid("book", {"gid": 20}))
