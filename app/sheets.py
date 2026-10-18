"""Загрузка прайса из Google Sheets с кешированием.

Два способа доступа:
  1. GOOGLE_SERVICE_ACCOUNT_JSON — JSON ключа сервисного аккаунта (таблицу нужно расшарить на его email).
     Таблица остаётся приватной. Рекомендуется.
  2. GOOGLE_API_KEY — ключ API; таблица должна быть открыта «всем, у кого есть ссылка» (просмотр).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from urllib.parse import quote

import requests

from .catalog import Catalog, parse_text_dump
from .config import Settings

log = logging.getLogger(__name__)

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


class SheetsError(RuntimeError):
    pass


class SheetsClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self._creds = None
        if settings.google_service_account_json:
            from google.oauth2 import service_account

            raw = settings.google_service_account_json
            info = json.loads(raw) if raw.lstrip().startswith("{") else json.load(open(raw))
            self._creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    def _auth(self) -> tuple[dict, dict]:
        if self._creds is not None:
            from google.auth.transport.requests import Request

            if not self._creds.valid:
                self._creds.refresh(Request())
            return {"Authorization": f"Bearer {self._creds.token}"}, {}
        if self.s.google_api_key:
            return {}, {"key": self.s.google_api_key}
        raise SheetsError("Не задан GOOGLE_SERVICE_ACCOUNT_JSON или GOOGLE_API_KEY")

    def fetch_all(self) -> dict[str, list[list[str]]]:
        sid = self.s.spreadsheet_id
        if not sid:
            raise SheetsError("Не задан SPREADSHEET_ID")
        headers, params = self._auth()
        meta = requests.get(f"{SHEETS_API}/{sid}", headers=headers, timeout=20,
                            params={**params, "fields": "sheets.properties.title"})
        if meta.status_code != 200:
            raise SheetsError(f"Sheets API {meta.status_code}: {meta.text[:300]}")
        titles = [s["properties"]["title"] for s in meta.json().get("sheets", [])]
        ranges = [f"'{t}'" for t in titles]
        # batchGet ожидает повторяющийся параметр ranges
        query = "&".join(f"ranges={quote(r)}" for r in ranges)
        resp = requests.get(f"{SHEETS_API}/{sid}/values:batchGet?{query}", headers=headers, timeout=30,
                            params={**params, "valueRenderOption": "FORMATTED_VALUE"})
        if resp.status_code != 200:
            raise SheetsError(f"Sheets API {resp.status_code}: {resp.text[:300]}")
        out: dict[str, list[list[str]]] = {}
        for title, vr in zip(titles, resp.json().get("valueRanges", [])):
            out[title] = [[str(c) for c in row] for row in vr.get("values", [])]
        return out


class CatalogProvider:
    """Отдаёт актуальный Catalog. Обновляет раз в CATALOG_CACHE_SECONDS; при ошибке держит последнюю удачную копию."""

    def __init__(self, settings: Settings):
        self.s = settings
        self._catalog: Catalog | None = None
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()
        self._client: SheetsClient | None = None

    def _load_sync(self) -> Catalog:
        if self.s.price_file:
            with open(self.s.price_file, encoding="utf-8") as f:
                return Catalog(parse_text_dump(f.read()), self.s.tradein_sheet)
        if self._client is None:
            self._client = SheetsClient(self.s)
        return Catalog(self._client.fetch_all(), self.s.tradein_sheet)

    async def get(self, force: bool = False) -> Catalog:
        fresh = time.monotonic() - self._loaded_at < self.s.catalog_cache_seconds
        if self._catalog is not None and fresh and not force:
            return self._catalog
        async with self._lock:
            fresh = time.monotonic() - self._loaded_at < self.s.catalog_cache_seconds
            if self._catalog is not None and fresh and not force:
                return self._catalog
            try:
                cat = await asyncio.to_thread(self._load_sync)
                self._catalog, self._loaded_at = cat, time.monotonic()
                log.info("Каталог обновлён: %s", cat.stats())
            except Exception:
                log.exception("Не удалось обновить каталог")
                if self._catalog is None:
                    raise
                # Старая копия устарела не сильнее чем на минуты; повторим попытку через минуту
                self._loaded_at = time.monotonic() - self.s.catalog_cache_seconds + 60
            return self._catalog
