"""Report rows in configured target tabs that no matching rule can inspect."""
from collections import defaultdict


def unmapped_targets(project, adapter):
    from .model import Address, ScanError, canonical, cell_value, id_state

    book = project["target_book"]
    by_tab = defaultdict(list)
    for route in project["routes"]:
        by_tab[route["target"]["gid"]].append(route["target"])
    diagnostics = []
    for tab in project.get("coverage_targets", []):
        gid = tab["gid"]
        rules = by_tab[gid]
        if any(not r.get("where") and not r.get("first_col") and not r.get("last_col") for r in rules):
            continue
        try:
            start, rows = adapter.grid(book, tab)
            header_row = tab["header_row"]
            if start != 0 or len(rows) < header_row:
                raise ScanError("Не найдены заголовки листа")
            headers = [canonical(cell_value(cell)) for cell in rows[header_row - 1]]
            required = {tab.get("title", "Title"), "Id"}
            for rule in rules:
                if rule["header_row"] != header_row:
                    raise ScanError("У правила и проверки охвата разные строки заголовков")
                required.update(rule.get("where", {}))
            fields = {}
            for key in sorted(required):
                matching = [i for i, title in enumerate(headers) if title == canonical(key)]
                if len(matching) != 1:
                    raise ScanError(f"Заголовок {key!r} отсутствует или повторяется")
                fields[key] = matching[0]
        except ScanError as exc:
            diagnostics.append({"code": "READ_ERROR", "detail": f'{tab["name"]}: {exc}'})
            continue

        def cell_at(row, key):
            index = fields[key]
            return row[index] if index < len(row) else {}

        uncovered, with_id, links = 0, 0, []
        for i, row in enumerate(rows[header_row:], header_row + 1):
            id_cell = cell_at(row, "Id")
            if not cell_value(cell_at(row, tab.get("title", "Title"))) and id_state(id_cell) == "EMPTY":
                continue
            selected = any(
                all(canonical(cell_value(cell_at(row, field))) == canonical(value)
                    for field, value in rule.get("where", {}).items())
                for rule in rules
            )
            if selected:
                continue
            uncovered += 1
            if id_state(id_cell) != "EMPTY":
                with_id += 1
                links.append(Address(book, gid, i, fields["Id"]).link)
        if uncovered:
            diagnostics.append({
                "code": "UNMAPPED_TARGET",
                "detail": f'{tab["name"]}: строк вне правил {uncovered}, из них с данными в поле Id {with_id}',
                "rows": uncovered,
                "with_id": with_id,
                "links": links,
            })
    return diagnostics
