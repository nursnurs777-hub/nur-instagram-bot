"""Загрузка открытой таблицы без ключа: имитируем ответы Google."""
import csv
import io
from dataclasses import replace
from pathlib import Path

import pytest

import app.sheets as sheets
from app.catalog import Catalog, parse_text_dump
from app.config import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "price_nur.txt"
SHEETS = parse_text_dump(FIXTURE.read_text(encoding="utf-8"))
GIDS = {"iphone": "0", "iPad": "101", "MacBook": "102", "Apple Watch": "201", "Marshall": "202", "Камеры": "203",
        "Петлички": "204", "Дроны": "205", "AirPods": "206", "Другое": "207", "Игры": "208", "Закуп б/у": "301",
        "dyson": "401"}
BY_GID = {g: n for n, g in GIDS.items()}


def js_escape(s: str) -> str:
    return s.encode("unicode_escape").decode("ascii").replace("\\\\u", "\\u").replace("/", "\\/")


HTMLVIEW = "<script>" + "".join(
    f'items.push({{name: "{js_escape(n)}", pageUrl: "https:\\/\\/x\\/htmlview\\/sheet?gid={g}", gid: "{g}",initialSheet: false}});'
    for n, g in GIDS.items()) + "</script>"


class Resp:
    def __init__(self, status=200, text="", ctype="text/csv; charset=utf-8", data=None):
        self.status_code, self.text, self._data = status, text, data
        self.content = text.encode("utf-8")
        self.headers = {"content-type": ctype}

    def json(self):
        return self._data


def to_csv(rows):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue()


def fake_get(htmlview_ok=True, api_status=None):
    calls = []

    def get(url, params=None, **kw):
        calls.append(url)
        if "sheets.googleapis.com" in url:
            return Resp(api_status or 400, '{"error": {"message": "API key not valid"}}', "application/json")
        if url.endswith("/htmlview"):
            return Resp(200, HTMLVIEW, "text/html") if htmlview_ok else Resp(404, "", "text/html")
        if url.endswith("/export"):
            name = BY_GID[params["gid"]]
            return Resp(200, to_csv(SHEETS[name]))
        raise AssertionError(url)

    return get, calls


def settings(**kw):
    return replace(Settings(), spreadsheet_id="SID", google_api_key="", google_service_account_json="", **kw)


def test_htmlview_parsing():
    assert sheets.parse_htmlview_gids(HTMLVIEW) == list(GIDS.items())
    li = '<li id="sheet-button-301"><a href="#">Закуп б/у</a></li><li id="sheet-button-0"><a>iphone</a></li>'
    assert sheets.parse_htmlview_gids(li) == [("Закуп б/у", "301"), ("iphone", "0")]


def test_public_sheet_without_key(monkeypatch):
    get, calls = fake_get()
    monkeypatch.setattr(sheets.requests, "get", get)
    data = sheets.SheetsClient(settings()).fetch_all()
    assert set(data) == set(GIDS)
    cat = Catalog(data)
    assert cat.stats()["products"] == 109 and cat.stats()["tradein"] == 93
    assert not any("googleapis" in c for c in calls)


def test_fallback_to_gid_setting(monkeypatch):
    get, _ = fake_get(htmlview_ok=False)
    monkeypatch.setattr(sheets.requests, "get", get)
    data = sheets.SheetsClient(settings()).fetch_all()  # SHEET_GIDS по умолчанию
    assert Catalog(data).stats()["tradein"] == 93


def test_invalid_api_key_falls_back(monkeypatch):
    get, calls = fake_get()
    monkeypatch.setattr(sheets.requests, "get", get)
    data = sheets.SheetsClient(replace(settings(), google_api_key="wrong")).fetch_all()
    assert "Закуп б/у" in data
    assert any("googleapis" in c for c in calls)


def test_private_sheet_gives_clear_error(monkeypatch):
    def get(url, params=None, **kw):
        if url.endswith("/htmlview"):
            return Resp(200, HTMLVIEW, "text/html")
        return Resp(200, "<html>Sign in</html>", "text/html; charset=utf-8")

    monkeypatch.setattr(sheets.requests, "get", get)
    with pytest.raises(sheets.SheetsError, match="открыта"):
        sheets.SheetsClient(settings()).fetch_all()
