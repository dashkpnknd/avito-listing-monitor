"""Project metadata projection for the existing price monitor."""
from hashlib import sha256
from pathlib import Path
import json


def project_metadata(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = value["projects"] if isinstance(value, dict) else value
    metadata = {}
    for row in entries:
        if not isinstance(row, dict):
            raise ValueError("Некорректная запись в реестре мониторинга цен")
        name = row["name"].strip()
        source = row["client_spreadsheet_id"].strip()
        target = row["autoload_spreadsheet_id"].strip()
        legacy_id = str(row.get("id") or sha256(f"{name}|{source}".encode()).hexdigest()[:12])
        if legacy_id in metadata:
            raise ValueError("ID проекта повторяется в реестре мониторинга цен")
        metadata[legacy_id] = {"name": name, "source_book": source, "target_book": target}
    return metadata
