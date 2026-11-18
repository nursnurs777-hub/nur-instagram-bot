"""Кнопки реакций Telegram-каналов: защита вебхука секретом, голосование, повторная доставка."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SFON_TG_BOTS", json.dumps({"astana": {"token": "123:abc", "table": "posts",
                                                              "channel": "@chan"}}))
    monkeypatch.setenv("SFON_TG_DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/x.db")
    monkeypatch.setenv("SFON_TG_SQLITE", str(tmp_path / "tg.sqlite3"))
    from app import sfon_reactions
    import tgcommon
    sfon_reactions._ensured.clear()
    calls = []

    def fake(token, method, data=None, files=None, wait=True, attempts=6):
        calls.append((method, dict(data or {})))
        return True

    monkeypatch.setattr(tgcommon, "tg_call", fake)
    st = sfon_reactions._state("posts")
    st.save_post({"product_id": 7, "message_ids": [1, 2], "photo_ids": ["a", "b"], "desc_message_id": 3,
                  "wa_url": "https://wa.me/7"})
    st.close()
    app = FastAPI()
    app.include_router(sfon_reactions.router)
    return TestClient(app), calls, tgcommon


def _update(cb_id, user, code="h", mid=3):
    return {"update_id": 1, "callback_query": {"id": cb_id, "data": f"v:7:{code}", "from": {"id": user},
                                               "message": {"message_id": mid, "chat": {"id": -5, "username": "chan"}}}}


def test_rejects_without_secret(env):
    client, calls, _ = env
    assert client.post("/sfon-tg/astana", json=_update("c1", 1)).status_code == 403
    assert client.post("/sfon-tg/astana", json=_update("c1", 1),
                       headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 403
    assert client.post("/sfon-tg/other", json=_update("c1", 1)).status_code == 403
    assert calls == []


def test_vote_toggle_and_duplicate(env):
    client, calls, tgcommon = env
    h = {"X-Telegram-Bot-Api-Secret-Token": tgcommon.secret_for("123:abc")}
    assert client.post("/sfon-tg/astana", json=_update("c1", 1), headers=h).status_code == 200
    kb = json.loads([d for m, d in calls if m == "editMessageReplyMarkup"][-1]["reply_markup"])
    assert [b["text"] for b in kb["inline_keyboard"][0]] == ["❤️ 1", "🔥 0", "👀 0"]
    assert kb["inline_keyboard"][1][0]["url"] == "https://wa.me/7"
    client.post("/sfon-tg/astana", json=_update("c1", 1), headers=h)          # повторная доставка
    client.post("/sfon-tg/astana", json=_update("c2", 2, "f"), headers=h)
    kb = json.loads([d for m, d in calls if m == "editMessageReplyMarkup"][-1]["reply_markup"])
    assert [b["text"] for b in kb["inline_keyboard"][0]] == ["❤️ 1", "🔥 1", "👀 0"]
    client.post("/sfon-tg/astana", json=_update("c3", 1), headers=h)          # снять свой голос
    kb = json.loads([d for m, d in calls if m == "editMessageReplyMarkup"][-1]["reply_markup"])
    assert [b["text"] for b in kb["inline_keyboard"][0]] == ["❤️ 0", "🔥 1", "👀 0"]


def test_old_message_cannot_vote(env):
    client, calls, tgcommon = env
    h = {"X-Telegram-Bot-Api-Secret-Token": tgcommon.secret_for("123:abc")}
    client.post("/sfon-tg/astana", json=_update("c9", 1, mid=99), headers=h)
    edits = [d for m, d in calls if m == "editMessageReplyMarkup"]
    assert edits and edits[-1]["message_id"] == 99 and json.loads(edits[-1]["reply_markup"]) == {"inline_keyboard": []}
    answers = [d for m, d in calls if m == "answerCallbackQuery"]
    assert "неактуально" in answers[-1]["text"]
