"""Read the current status of a known listing through the Avito API."""
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json
import os
import re

from .model import ScanError


BASE_URL = "https://api.avito.ru"


class AvitoStatuses:
    def __init__(self):
        self.accounts = {}

    def request(self, path, *, headers=None, data=None):
        try:
            request = Request(BASE_URL + path, data=data, headers=headers or {})
            with urlopen(request, timeout=15) as response:
                value = json.load(response)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (OSError, ValueError):
            raise ScanError("Avito не вернул статус объявления") from None

    def observe(self, project, listing, technical_id=None, *, require_binding=False):
        prefix = project["avito_credential_prefix"]
        if not re.fullmatch(r"[1-9][0-9]*", listing or ""):
            raise ScanError("У позиции нет подтверждённого номера объявления")
        if prefix not in self.accounts:
            client_id = os.environ.get(prefix + "_CLIENT_ID")
            secret = os.environ.get(prefix + "_CLIENT_SECRET")
            if not client_id or not secret:
                raise ScanError("Для аккаунта Avito не настроен доступ к статусу")
            params = urlencode({"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret})
            issued = self.request("/token", data=params.encode(), headers={
                "Content-Type": "application/x-www-form-urlencoded",
            })
            token = issued.get("access_token")
            if not isinstance(token, str) or not token:
                raise ScanError("Avito не выдал токен чтения")
            headers = {"Authorization": "Bearer " + token}
            account = str(self.request("/core/v1/accounts/self", headers=headers).get("id"))
            if not re.fullmatch(r"[1-9][0-9]*", account):
                raise ScanError("Avito не подтвердил номер аккаунта")
            self.accounts[prefix] = (account, headers)
        account, headers = self.accounts[prefix]
        path = f"/core/v1/accounts/{account}/items/{listing}/"
        item = self.request(path, headers=headers)
        if "id" in item and str(item["id"]) != listing:
            raise ScanError("Avito вернул другое объявление")
        registered = item.get("autoload_item_id")
        if technical_id is not None and (registered or require_binding) and str(registered) != str(technical_id):
            raise ScanError("Технический Id объявления в Avito не совпал с таблицей")
        status = item.get("status")
        if not isinstance(status, str):
            status = ""
        outcome = {
            "active": "ACTIVE",
            "removed": "INACTIVE_CONFIRMED",
            "archived": "INACTIVE_CONFIRMED",
            "closed": "INACTIVE_CONFIRMED",
        }.get(status, "UNKNOWN")
        return {
            "source": "item-status",
            "marketplace_id": listing,
            "outcome": outcome,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "reference": f"{path} · status={status}" if outcome != "UNKNOWN" else path,
        }
