"""Тестовая страница: доступ по токену, диалог с памятью, фото, режим менеджера, сброс."""
import io
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from PIL import Image

FIXTURE = Path(__file__).parent / "fixtures" / "price_nur.txt"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "adm")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("PRICE_FILE", str(FIXTURE))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    import importlib
    import app.config, app.testchat, app.main
    importlib.reload(app.config); importlib.reload(app.testchat); importlib.reload(app.main)
    from fastapi.testclient import TestClient
    from app.agent import AgentResult

    calls = []

    async def reply(history, state, catalog, images=None, **kw):
        calls.append((history, images))
        users = [h["content"] for h in history if h["role"] == "user"]
        return AgentResult(f"ответ #{len(users)}: {users[-1]}", {**state, "n": len(users)}, ["search_products"])

    async def create(**kw):
        return NS(content=[NS(type="text", text="AirPods Pro, кейс поцарапан")])

    with TestClient(app.main.app) as c:
        agent = app.main.app.state.proc.agent
        agent.reply = reply
        agent.client = NS(messages=NS(create=create))
        c.calls = calls
        yield c


H = {"X-Admin-Token": "adm"}


def test_page_and_auth(client):
    page = client.get("/test")
    assert page.status_code == 200 and "NUR Electronics" in page.text
    assert client.get("/test/api/catalog").status_code == 401
    r = client.get("/test/api/catalog", headers=H).json()
    assert r["products"] == 109 and r["tradein"] == 93


def test_dialog_memory_and_reset(client):
    r1 = client.post("/test/api/message", data={"session": "s1", "text": "привет"}, headers=H).json()
    assert r1["reply"] == "ответ #1: привет" and r1["tools"] == ["search_products"]
    r2 = client.post("/test/api/message", data={"session": "s1", "text": "а цена?"}, headers=H).json()
    assert r2["reply"] == "ответ #2: а цена?"
    h = client.get("/test/api/history", params={"session": "s1"}, headers=H).json()
    assert [m["role"] for m in h["messages"]] == ["user", "assistant", "user", "assistant"]
    client.post("/test/api/reset", data={"session": "s1"}, headers=H)
    assert client.get("/test/api/history", params={"session": "s1"}, headers=H).json()["messages"] == []


def test_photo_upload_stores_text_only(client):
    buf = io.BytesIO(); Image.new("RGB", (3000, 2000), "white").save(buf, "PNG")
    r = client.post("/test/api/message", data={"session": "s2", "text": "что это?"},
                    files=[("files", ("a.png", buf.getvalue(), "image/png"))], headers=H).json()
    assert r["photo_notes"] == ["[фото: AirPods Pro, кейс поцарапан]"]
    history, images = client.calls[-1]
    assert len(images) == 1 and images[0].media_type == "image/jpeg"
    stored = client.get("/test/api/history", params={"session": "s2"}, headers=H).json()["messages"][0]["content"]
    assert stored == "[фото: AirPods Pro, кейс поцарапан] что это?"


def test_manager_mode(client):
    r = client.post("/test/api/message", data={"session": "s3", "text": "Да, есть в наличии", "as_manager": "true"},
                    headers=H).json()
    assert r["reply"] == "" and "менеджер" in r["note"]
    h = client.get("/test/api/history", params={"session": "s3"}, headers=H).json()
    assert h["messages"][0]["role"] == "manager"
