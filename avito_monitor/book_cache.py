"""Use stored CellData for unchanged books and observe only selected live reads."""
import json
import zlib

from .model import ReadQuota, ScanError
from .storage import encode


class CachedBooks:
    def __init__(self, reader, previous, refresh):
        self.reader = reader
        self.previous = {book: json.loads(zlib.decompress(data)) for book, data in previous.items()}
        self.refresh = set(refresh)
        self.new = {book: {"tabs": None, "grids": {}} for book in refresh}
        self.failed = False
        self.quota = None

    def capture(self, fn, *args):
        try:
            return fn(*args)
        except ReadQuota as exc:
            self.quota = exc
            raise
        except ScanError:
            self.failed = True
            raise

    def tabs(self, book):
        if book not in self.refresh:
            return self.previous[book]["tabs"]
        record = self.new[book]
        if record["tabs"] is None:
            record["tabs"] = self.capture(self.reader.tabs, book)
        return record["tabs"]

    def prefetch(self, book, tabs):
        if book in self.refresh and hasattr(self.reader, "prefetch"):
            self.capture(self.reader.prefetch, book, tabs)

    def grid(self, book, spec):
        first, last = spec.get("first_col", 0), spec.get("last_col")
        key = f'{spec["gid"]}:{first}:{last}'
        if book not in self.refresh:
            grid = self.previous[book]["grids"].get(key)
            if grid is None:
                self.failed = True
                raise ScanError("В базе отсутствует диапазон сохранённого листа")
            return grid
        grids = self.new[book]["grids"]
        if key not in grids:
            start, rows = self.capture(self.reader.grid, book, spec)
            grids[key] = [start, rows]
        return grids[key]

    def blobs(self):
        return {book: zlib.compress(encode(data).encode(), level=1) for book, data in self.new.items()}
