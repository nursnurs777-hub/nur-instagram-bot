"""Загрузка прайса из Google Sheets с кешированием.

Способы доступа (по приоритету):
  1. GOOGLE_SERVICE_ACCOUNT_JSON — сервисный аккаунт, таблица может быть закрытой.
  2. GOOGLE_API_KEY — ключ API, таблица открыта по ссылке.
  3. Без ключей (по умолчанию) — таблица открыта «всем, у кого есть ссылка»: каждый лист скачивается
     как CSV по адресу export?format=csv&gid=<id листа>. Список листов берётся со страницы htmlview,
     а если её разобрать не удалось — из SHEET_GIDS («имя:gid,имя:gid»).
Если способ 1 или 2 не сработал, бот пробует способ 3.
"""
from __future__ import annotations

import asyncio
import json
import csv
import html
import io
import logging
import re
import time
from urllib.parse import quote

import requests

from .catalog import Catalog, parse_text_dump
from .config import Settings

log = logging.getLogger(__name__)

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
DOCS = "https://docs.google.com/spreadsheets/d"


def parse_htmlview_gids(page: str) -> list[tuple[str, str]]:
    """Находит пары (имя листа, gid) на странице htmlview открытой таблицы."""
    found: list[tuple[str, str]] = []
    # 1) скрипт: items.push({name: "…", pageUrl: "…", gid: "…"}) — имя в JS-экранировании (\u0417…)
    for m in re.finditer(r'name:\s*"((?:[^"\\]|\\.)*)"[^}]*?gid:\s*"(\d+)"', page, flags=re.S):
        try:
            name = json.loads(f'"{m.group(1)}"')
        except ValueError:
            name = m.group(1)
        found.append((html.unescape(name).strip(), m.group(2)))
    # 2) разметка: <li id="sheet-button-GID"><a …>NAME</a>
    if not found:
        for m in re.finditer(r'id="sheet-button-(\d+)"[^>]*>\s*<a[^>]*>([^<]+)</a>', page, flags=re.S):
            found.append((html.unescape(m.group(2)).strip(), m.group(1)))
    uniq: list[tuple[str, str]] = []
    for item in found:
        if item[0] and item not in uniq:
            uniq.append(item)
    return uniq


def parse_gid_setting(raw: str) -> list[tuple[str, str]]:
    out = []
    for part in (raw or "").split(","):
        if ":" in part:
            name, gid = part.rsplit(":", 1)
            if name.strip() and gid.strip().isdigit():
                out.append((name.strip(), gid.strip()))
    return out


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

    @property
    def has_credentials(self) -> bool:
        return self._creds is not None or bool(self.s.google_api_key)

    def fetch_all(self) -> dict[str, list[list[str]]]:
        if not self.s.spreadsheet_id:
            raise SheetsError("Не задан SPREADSHEET_ID")
        if self.has_credentials:
            try:
                return self._fetch_api()
            except SheetsError as e:
                log.warning("Sheets API не сработал (%s) — читаю открытую таблицу без ключа", str(e)[:120])
        return self._fetch_public()

    # ---------------------------------------------------------------- открытая таблица без ключа
    def _sheet_gids(self) -> list[tuple[str, str]]:
        sid = self.s.spreadsheet_id
        try:
            r = requests.get(f"{DOCS}/{sid}/htmlview", timeout=20)
            if r.status_code == 200:
                found = parse_htmlview_gids(r.text)
                if found:
                    return found
        except requests.RequestException as e:
            log.warning("htmlview недоступен: %s", e)
        gids = parse_gid_setting(self.s.sheet_gids)
        if not gids:
            raise SheetsError("Не удалось получить список листов таблицы. Проверьте, что она открыта по ссылке, "
                              "или задайте SHEET_GIDS.")
        return gids

    def _fetch_public(self) -> dict[str, list[list[str]]]:
        sid = self.s.spreadsheet_id
        out: dict[str, list[list[str]]] = {}
        for name, gid in self._sheet_gids():
            r = requests.get(f"{DOCS}/{sid}/export", params={"format": "csv", "gid": gid}, timeout=30)
            ctype = r.headers.get("content-type", "")
            if r.status_code != 200 or "text/html" in ctype:
                raise SheetsError(f"Лист «{name}» не скачался ({r.status_code}). Таблица должна быть открыта "
                                  "«всем, у кого есть ссылка».")
            text = r.content.decode("utf-8-sig")
            out[name] = [row for row in csv.reader(io.StringIO(text))]
        return out

    # ---------------------------------------------------------------- Sheets API
    def _fetch_api(self) -> dict[str, list[list[str]]]:
        sid = self.s.spreadsheet_id
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
