"""Append a project to the existing service configuration atomically."""
from copy import deepcopy
from pathlib import Path
from tempfile import NamedTemporaryFile
import json
import os
import stat

from .config import spreadsheet_id, validate


def register_project(args):
    path = Path(args.config)
    config = json.loads(path.read_text(encoding="utf-8"))
    project = {
        "id": args.project,
        "name": args.name,
        "source_book": spreadsheet_id(args.source),
        "target_book": spreadsheet_id(args.target),
        "mode": "monitoring",
        "launch_date": args.launch_date,
        "report_exclude_days": 10,
    }
    if args.city:
        project["city"] = args.city
    if args.avito_profile:
        project["avito_credential_prefix"] = args.avito_profile
        project["removal_confirmation"] = "avito_status"
    prior = next((entry for entry in config["projects"] if entry["id"] == project["id"]), None)
    if prior == project:
        return {"project": project["id"], "result": "already_registered"}
    config["projects"].append(project)
    validate(deepcopy(config), path.parent)
    info = path.stat()
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temp = Path(file.name)
        try:
            if os.geteuid() == 0:
                os.fchown(file.fileno(), info.st_uid, info.st_gid)
            os.fchmod(file.fileno(), stat.S_IMODE(info.st_mode))
            json.dump(config, file, indent=2, ensure_ascii=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    return {"project": project["id"], "result": "registered"}
