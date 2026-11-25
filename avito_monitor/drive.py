"""Read the Drive change journal using the service's Google identity."""
from urllib.parse import urlencode
from urllib.parse import quote

from .model import ScanError


class CursorRejected(ScanError):
    pass


class DriveChanges:
    SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"

    def __init__(self):
        try:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(scopes=[self.SCOPE])
        except ImportError:
            raise ScanError("Установите зависимости Google API") from None
        except google.auth.exceptions.GoogleAuthError:
            raise ScanError("Не настроен доступ к журналу Google Drive") from None
        self.session = AuthorizedSession(credentials)

    def get(self, path, params):
        from google.auth.exceptions import GoogleAuthError
        from requests import RequestException

        url = f"https://www.googleapis.com/drive/v3/{path}?{urlencode(params)}"
        try:
            response = self.session.get(url, timeout=(5, 12))
            if response.status_code == 410:
                raise CursorRejected("Google Drive отклонил сохранённую позицию журнала")
            if response.status_code != 200:
                raise ScanError(f"Журнал Google Drive HTTP {response.status_code}")
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (RequestException, ValueError, GoogleAuthError):
            raise ScanError("Журнал Google Drive недоступен") from None

    def start(self, drive_id):
        params = {"supportsAllDrives": "true"}
        if drive_id:
            params["driveId"] = drive_id
        token = self.get("changes/startPageToken", params).get("startPageToken")
        if not isinstance(token, str) or not token:
            raise ScanError("Google Drive не вернул позицию журнала")
        return token

    def page(self, drive_id, token):
        params = {
            "pageToken": token, "pageSize": 1000, "includeRemoved": "true",
            "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
            "fields": "nextPageToken,newStartPageToken,changes(fileId)",
        }
        if drive_id:
            params["driveId"] = drive_id
        payload = self.get("changes", params)
        more = payload.get("nextPageToken")
        cursor = more or payload.get("newStartPageToken")
        entries = payload.get("changes", [])
        if (
            not isinstance(cursor, str) or not cursor or (more and cursor == token)
            or not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries)
        ):
            raise ScanError("Google Drive вернул неполную страницу изменений")
        return cursor, bool(more), {entry["fileId"] for entry in entries if isinstance(entry.get("fileId"), str)}

    def file_drive(self, book):
        info = self.get(f"files/{quote(book, safe='')}", {"supportsAllDrives": "true", "fields": "id,driveId"})
        if info.get("id") != book:
            raise ScanError("Google Drive не подтвердил ID файла")
        return info.get("driveId") or ""
