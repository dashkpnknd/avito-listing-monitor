"""Read one named listing through the installed Avito price monitor."""
from datetime import datetime, timezone
import argparse
import json
import re


def observe(fetch_active, config, marketplace_id):
    items = fetch_active(config)
    return {
        "source": "price-monitor-active-list",
        "marketplace_id": marketplace_id,
        "outcome": "ACTIVE" if marketplace_id in items else "NOT_SEEN",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "reference": "existing-price-monitor /core/v1/items",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--autoload-book", required=True)
    parser.add_argument("--marketplace-id", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]+", args.marketplace_id):
        parser.error("Номер объявления должен состоять из цифр")
    try:
        from app.monitor import Config, fetch_active_avito_items
        from app.projects import all_project_configs

        projects = all_project_configs(Config.from_environment())
        matches = [p for p in projects if args.autoload_book in p.autoload_spreadsheet_ids]
        if len(matches) != 1:
            raise ValueError("Unknown or duplicate configuration")
        print(json.dumps(observe(fetch_active_avito_items, matches[0], args.marketplace_id)))
    except Exception as exc:
        # Avoid echoing configuration or HTTP response details.
        parser.exit(2, f"Не удалось получить статус через монитор цен ({type(exc).__name__})\n")


if __name__ == "__main__":
    main()
