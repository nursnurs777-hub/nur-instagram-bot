"""Сквозные проверки без сети: вебхук, пауза менеджера, склейка сообщений, повтор вебхука."""
import asyncio
import hashlib
import hmac
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from app.agent import AgentResult, build_messages, strip_markdown
from app.catalog import Catalog, parse_text_dump
from app.config import Settings
from app.db import Store, utcnow
from app.instagram import Attachment, Event, parse_events, split_message, verify_signature
from app.processor import Processor

FIXTURE = Path(__file__).parent / "fixtures" / "price_nur.txt"


# ------------------------------------------------------------ unit

def test_signature():
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_signature("secret", body, sig)
    assert not verify_signature("secret", body + b" ", sig)
    assert not verify_signature("secret", body, None)
    assert not verify_signature("", body, sig)


def test_parse_events_message_and_echo():
    payload = {"object": "instagram", "entry": [{"id": "BIZ", "time": 1, "messaging": [
        {"sender": {"id": "U1"}, "recipient": {"id": "BIZ"}, "timestamp": 1,
         "message": {"mid": "m1", "text": "Сколько стоит айфон?"}},
        {"sender": {"id": "BIZ"}, "recipient": {"id": "U1"}, "timestamp": 2,
         "message": {"mid": "m2", "text": "Здравствуйте", "is_echo": True}},
        {"sender": {"id": "U1"}, "recipient": {"id": "BIZ"}, "timestamp": 3,
         "message": {"mid": "m3", "attachments": [{"type": "image", "payload": {"url": "https://x/y.jpg"}}]}},
        {"sender": {"id": "U1"}, "recipient": {"id": "BIZ"}, "timestamp": 4, "read": {"mid": "m2"}},
    ]}]}
    evs = parse_events(payload)
    assert [e.kind for e in evs] == ["message", "echo", "message"]
    assert all(e.user_id == "U1" for e in evs)
    assert evs[2].attachments[0].url == "https://x/y.jpg"


def test_split_message():
    long = ("Предложение номер один. " * 30 + "\n\n") * 3
    parts = split_message(long, 300)
    assert all(len(p) <= 300 for p in parts)
    assert "".join(parts).replace(" ", "").replace("\n", "") == long.replace(" ", "").replace("\n", "")
    assert split_message("коротко") == ["коротко"]


def test_strip_markdown():
    assert strip_markdown("**Цена:** 630 000 ₸\n## Итог\n- пункт") == "Цена: 630 000 ₸\nИтог\n• пункт"


def test_build_messages_merges_and_marks_manager():
    hist = [
        {"role": "assistant", "content": "старое"},
        {"role": "user", "content": "привет"},
        {"role": "user", "content": "есть 17 про?"},
        {"role": "manager", "content": "Да, есть"},
        {"role": "user", "content": "а цена?"},
    ]
    msgs = build_messages(hist)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"][0]["text"] == "привет\nесть 17 про?"
    assert msgs[1]["content"][0]["text"].startswith("[Ответ менеджера]")


# ------------------------------------------------------------ интеграция с фейками

class FakeIG:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.n = 0

    async def send_text(self, user_id, text):
        self.n += 1
        self.sent.append((user_id, text))
        return [f"bot-{self.n}"]

    async def typing(self, *a, **k):
        pass

    async def download(self, url, max_bytes=None):
        return None


class FakeAgent:
    def __init__(self, delay=0.0):
        self.calls: list[list[dict]] = []
        self.delay = delay

    async def reply(self, history, state, catalog, images=None, extra_system=None, extra_user_note=None):
        self.calls.append(history)
        await asyncio.sleep(self.delay)
        users = [h["content"] for h in history if h["role"] == "user"]
        return AgentResult(f"ответ на: {' | '.join(users[-2:])}", {**state, "seen": len(users)})


class FakeCatalogs:
    def __init__(self):
        self.cat = Catalog(parse_text_dump(FIXTURE.read_text(encoding="utf-8")))

    async def get(self, force=False):
        return self.cat


def make(tmp_path, **over):
    params = dict(database_url=f"sqlite:///{tmp_path}/t.db", debounce_seconds=0.05,
                  echo_grace_seconds=0.05, allowed_user_ids=[], ig_account_id="BIZ", bot_enabled=True,
                  automation_echo_seconds=0, automation_texts=[], reconcile_enabled=False)
    params.update(over)
    s = replace(Settings(), **params)
    ig, agent = FakeIG(), FakeAgent()
    proc = Processor(s, Store(s.database_url), ig, agent, FakeCatalogs())
    return proc, ig, agent


def msg(mid, text, uid="U1"):
    return Event(kind="message", user_id=uid, account_id="BIZ", mid=mid, text=text)


def echo(mid, text, uid="U1"):
    return Event(kind="echo", user_id=uid, account_id="BIZ", mid=mid, text=text)


async def settle(proc, t=0.3):
    await asyncio.sleep(t)
    while proc._bg:
        await asyncio.gather(*list(proc._bg), return_exceptions=True)


def test_debounce_merges_messages(tmp_path):
    async def go():
        proc, ig, agent = make(tmp_path)
        await proc.handle_event(msg("a", "Здравствуйте"))
        await proc.handle_event(msg("b", "сколько стоит 17 про?"))
        await settle(proc)
        assert len(ig.sent) == 1 and len(agent.calls) == 1
        assert "сколько стоит 17 про?" in ig.sent[0][1]
        conv = proc.store.get_conv("U1")
        assert conv["state"]["seen"] == 2
        assert proc.store.unprocessed("U1") == []
    asyncio.run(go())


def test_duplicate_webhook_ignored(tmp_path):
    async def go():
        proc, ig, _ = make(tmp_path)
        await proc.handle_event(msg("a", "привет"))
        await proc.handle_event(msg("a", "привет"))
        await settle(proc)
        assert len(ig.sent) == 1
    asyncio.run(go())


def test_own_echo_does_not_pause(tmp_path):
    async def go():
        proc, ig, _ = make(tmp_path)
        await proc.handle_event(msg("a", "привет"))
        await settle(proc)
        await proc.handle_event(echo("bot-1", ig.sent[0][1]))  # echo собственного ответа
        await settle(proc)
        conv = proc.store.get_conv("U1")
        assert conv["paused_until"] is None
        await proc.handle_event(msg("b", "а цена?"))
        await settle(proc)
        assert len(ig.sent) == 2
    asyncio.run(go())


def test_manager_reply_pauses_bot_then_resumes(tmp_path):
    async def go():
        proc, ig, agent = make(tmp_path)
        await proc.handle_event(echo("mgr-1", "Здравствуйте, это менеджер"))
        await settle(proc)
        conv = proc.store.get_conv("U1")
        assert conv["paused_until"] is not None

        await proc.handle_event(msg("a", "а скидка будет?"))
        await settle(proc)
        assert ig.sent == []  # бот молчит во время паузы
        assert len(proc.store.unprocessed("U1")) == 1

        # пауза истекла, менеджер так и не ответил → бот отвечает на сохранённое сообщение
        proc.store.update_conv("U1", paused_until=utcnow() - timedelta(seconds=1))
        await proc.tick()
        await settle(proc)
        assert len(ig.sent) == 1 and "скидка" in ig.sent[0][1]
        assert any(h["role"] == "manager" for h in agent.calls[-1])
    asyncio.run(go())


def test_manager_answers_during_generation(tmp_path):
    async def go():
        proc, ig, agent = make(tmp_path)
        agent.delay = 0.3
        await proc.handle_event(msg("a", "есть ps5?"))
        await asyncio.sleep(0.15)  # бот уже генерирует
        await proc.handle_event(echo("mgr-1", "Да, есть, приезжайте"))
        await settle(proc, 0.5)
        assert ig.sent == []  # ответ бота отменён
    asyncio.run(go())


def test_manual_mode_blocks(tmp_path):
    async def go():
        proc, ig, _ = make(tmp_path)
        proc.store.get_conv("U1")
        proc.store.update_conv("U1", manual_mode=True)
        await proc.handle_event(msg("a", "привет"))
        await settle(proc)
        assert ig.sent == []
    asyncio.run(go())


def test_allowlist(tmp_path):
    async def go():
        proc, ig, _ = make(tmp_path, allowed_user_ids=["TESTER"])
        await proc.handle_event(msg("a", "привет", uid="STRANGER"))
        await proc.handle_event(msg("b", "привет", uid="TESTER"))
        await settle(proc)
        assert [u for u, _ in ig.sent] == ["TESTER"]
    asyncio.run(go())


def test_opt_out_flag(tmp_path):
    async def go():
        proc, ig, _ = make(tmp_path)
        await proc.handle_event(msg("a", "спасибо, больше не пишите"))
        await settle(proc)
        assert proc.store.get_conv("U1")["opted_out"] is True
    asyncio.run(go())


def test_webhook_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("IG_VERIFY_TOKEN", "vt")
    monkeypatch.setenv("IG_APP_SECRET", "sec")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/w.db")
    monkeypatch.setenv("PRICE_FILE", str(FIXTURE))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    import importlib
    import app.config, app.main
    importlib.reload(app.config)
    importlib.reload(app.main)
    from fastapi.testclient import TestClient

    with TestClient(app.main.app) as client:
        r = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"})
        assert r.status_code == 200 and r.text == "42"
        assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no",
                                              "hub.challenge": "1"}).status_code == 403
        body = json.dumps({"object": "instagram", "entry": []}).encode()
        assert client.post("/webhook", content=body).status_code == 401
        sig = "sha256=" + hmac.new(b"sec", body, hashlib.sha256).hexdigest()
        assert client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig}).status_code == 200
        assert client.get("/health").json() == {"ok": True}
        assert client.get("/privacy").status_code == 200


def test_agent_tool_loop():
    """Цикл модели: tool_use → результат инструмента → финальный текст; состояние сохраняется."""
    from types import SimpleNamespace as NS
    from app.agent import SalesAgent

    calls = []

    class FakeMessages:
        async def create(self, **kw):
            calls.append(kw)
            if len(calls) == 1:
                return NS(stop_reason="tool_use", content=[
                    NS(type="text", text="Сейчас посчитаю."),
                    NS(type="tool_use", id="t1", name="quote_iphone_tradein", input=dict(
                        model="iPhone 15 Pro", memory="256GB", battery_percent=82, has_box=True,
                        minor_scratches=True, was_repaired=False, screen_or_body_damage=False,
                        all_functions_work=True))])
            return NS(stop_reason="end_turn", content=[NS(type="text", text="**Предварительно** 215 000 ₸")])

    agent = SalesAgent(replace(Settings(), web_search_enabled=False), client=NS(messages=FakeMessages()))
    cat = Catalog(parse_text_dump(FIXTURE.read_text(encoding="utf-8")))
    res = asyncio.run(agent.reply([{"role": "user", "content": "15 pro 256, 82%, коробка есть"}], {}, cat))
    assert res.text == "Предварительно 215 000 ₸"
    assert res.state["tradein"]["estimate"] == 215000
    tool_result = calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and '"estimate": 215000' in tool_result["content"]
    assert calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Сохранённая оценка" not in calls[0]["system"][1]["text"]


def test_prompt_rendered_with_store_details():
    from app.agent import render_prompt
    tpl = Path("prompts/system_prompt.md").read_text(encoding="utf-8")
    s = replace(Settings(), store_whatsapp="")
    out = render_prompt(tpl, s)
    assert "{{" not in out
    assert "NUR Electronics" in out and "@nurelectronics.kz" in out and "Төле би, 52" in out
    assert "KopKop" not in out and "77079931111" not in out
    assert "не выдумывай номер" in out
    out2 = render_prompt(tpl, replace(s, store_whatsapp="https://wa.me/77000000000"))
    assert "https://wa.me/77000000000" in out2


# ------------------------------------------------------------ повторные касания

def _ripe(proc, user_hours_ago, bot_hours_ago):
    now = utcnow()
    proc.store.update_conv("U1", last_user_at=now - timedelta(hours=user_hours_ago),
                           last_bot_at=now - timedelta(hours=bot_hours_ago), followup_sent=False)


@pytest.mark.parametrize("hours,user_ago,expect", [
    ((0, 24), 4, 1),     # в окне часов и в 23-часовом окне Meta → одно напоминание
    ((0, 24), 22.5, 1),  # утром после ночи, окно Meta ещё открыто
    ((0, 24), 23.5, 0),  # окно Meta (с запасом 23 ч) истекло → не пишем
    ((0, 0), 4, 0),      # вне разрешённых часов → не пишем
])
def test_followup_respects_window(tmp_path, hours, user_ago, expect):
    async def go():
        proc, ig, _ = make(tmp_path, followup_enabled=True,
                           followup_hour_start=hours[0], followup_hour_end=hours[1])
        await proc.handle_event(msg("a", "сколько стоит 17 про?"))
        await settle(proc)
        assert len(ig.sent) == 1
        _ripe(proc, user_ago, user_ago - 0.01 if user_ago < 3.5 else user_ago - 0.5)
        await proc.tick()
        await settle(proc)
        assert len(ig.sent) == 1 + expect
        await proc.tick()  # второго напоминания не бывает
        await settle(proc)
        assert len(ig.sent) == 1 + expect
    asyncio.run(go())


def test_followup_default_hours():
    s = Settings()
    assert (s.followup_hour_start, s.followup_hour_end) == (9, 22)



# ------------------------------------------------------------ автоответы рекламы / ChatPlace

def test_fast_automation_echo_does_not_pause(tmp_path):
    async def go():
        proc, ig, agent = make(tmp_path, automation_echo_seconds=6, debounce_seconds=0.3,
                               automation_texts=["иә, қалған соманы"])
        await proc.handle_event(msg("a", "0•0•24 бар ма?"))
        await proc.handle_event(echo("auto-1", "Иә, қалған соманы бөліп төлеуге болады. Қай модель?"))
        await settle(proc, 0.5)
        conv = proc.store.get_conv("U1")
        assert conv["paused_until"] is None
        assert ig.sent == []  # на вопрос уже ответил автоответ — бот не дублирует
        await proc.handle_event(msg("b", "18 pro max 256"))
        await settle(proc, 0.5)
        assert len(ig.sent) == 1 and "18 pro max 256" in ig.sent[0][1]
    asyncio.run(go())


def test_known_automation_text_does_not_pause(tmp_path):
    async def go():
        proc, ig, _ = make(tmp_path, automation_texts=["iphone моделін, жады көлемін"])
        await proc.handle_event(echo("auto-2", "iPhone моделін, жады көлемін және жағдайын жазыңыз."))
        await settle(proc)
        assert proc.store.get_conv("U1")["paused_until"] is None
    asyncio.run(go())


def test_slow_manager_echo_still_pauses(tmp_path):
    async def go():
        proc, ig, _ = make(tmp_path, automation_echo_seconds=6)
        await proc.handle_event(msg("a", "скидка будет?"))
        await settle(proc)
        proc.store.update_conv("U1", last_user_at=utcnow() - timedelta(seconds=40))
        await proc.handle_event(echo("mgr-9", "Здравствуйте, это менеджер"))
        await settle(proc)
        assert proc.store.get_conv("U1")["paused_until"] is not None
    asyncio.run(go())


# ------------------------------------------------------------ пропущенные вебхуки

def test_reconcile_picks_up_missed_message(tmp_path):
    async def go():
        proc, ig, _ = make(tmp_path)
        await proc.handle_event(msg("a", "0•0•24 бар ма?"))
        await settle(proc)
        assert len(ig.sent) == 1
        later = (utcnow() + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        old = (utcnow() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+0000")

        async def recent(uid, limit=6):
            return [{"id": "m-missed", "from_id": "U1", "text": "18 pro max 256", "created_time": later},
                    {"id": "bot-1", "from_id": "BIZ", "text": "ответ", "created_time": old},
                    {"id": "a", "from_id": "U1", "text": "0•0•24 бар ма?", "created_time": old}]
        ig.recent_messages = recent
        n = await proc.reconcile(utcnow(), timedelta(hours=3))
        await settle(proc)
        assert n == 1 and len(ig.sent) == 2 and "18 pro max 256" in ig.sent[1][1]
        # повторная проверка ничего не дублирует
        assert await proc.reconcile(utcnow(), timedelta(hours=3)) == 0
    asyncio.run(go())



def test_empty_service_echo_ignored(tmp_path):
    """Карточка «Благодарим за ваш отзыв» приходит пустым echo — бот должен ответить клиенту."""
    async def go():
        proc, ig, _ = make(tmp_path, debounce_seconds=0.3)
        await proc.handle_event(msg("a", "18 про Макс голубой"))
        await proc.handle_event(echo("card-1", ""))
        await settle(proc, 0.6)
        assert proc.store.get_conv("U1")["paused_until"] is None
        assert len(ig.sent) == 1 and "голубой" in ig.sent[0][1]
    asyncio.run(go())


def test_recover_stuck_after_service_card(tmp_path):
    async def go():
        proc, ig, _ = make(tmp_path)
        # так выглядело в базе до исправления: клиент → пустой «ответ менеджера», клиент обработан, бот молчал
        proc.store.get_conv("U1")
        proc.store.add_message("U1", "user", "12 айфон 128 гб 80 емкост", "u-1", True)
        proc.store.add_message("U1", "manager", "[вложение: template]", "card-2")
        proc.store.update_conv("U1", last_user_at=utcnow(), paused_until=utcnow() + timedelta(minutes=5))
        n = await proc.recover_stuck(utcnow())
        await settle(proc)
        assert n == 1 and len(ig.sent) == 1 and "12 айфон" in ig.sent[0][1]
        # живой ответ менеджера не трогаем
        proc.store.get_conv("U2")
        proc.store.add_message("U2", "user", "скидка?", "u-2", True)
        proc.store.add_message("U2", "manager", "50-60 шамасында", "mgr-2")
        proc.store.update_conv("U2", last_user_at=utcnow())
        assert await proc.recover_stuck(utcnow()) == 0
    asyncio.run(go())



def test_build_messages_ends_with_user_after_noise_and_autoreply():
    h = [{"role": "user", "content": "18 про Макс голубой"},
         {"role": "manager", "content": "[вложение: template]"}]
    m = build_messages(h)
    assert m[-1]["role"] == "user" and len(m) == 1
    h2 = [{"role": "user", "content": "0•0•24 бар ма?"},
          {"role": "manager", "content": "[автоответ] Иә, болады"}]
    m2 = build_messages(h2)
    assert m2[-1]["role"] == "user"



def test_recover_stuck_after_fallback():
    import tempfile, os
    d = tempfile.mkdtemp()
    st = Store(f"sqlite:///{d}/t.db")
    st.get_conv("U3")
    st.add_message("U3", "user", "18 про Макс голубой", "u-3", True)
    st.add_message("U3", "manager", "[вложение: template]", "c-3")
    st.add_message("U3", "assistant", "Извините, сейчас не получается ответить автоматически. Напишите…", "b-3")
    st.update_conv("U3", last_user_at=utcnow())
    assert st.convs_stuck(utcnow(), timedelta(hours=23))[0][0] == "U3"



def test_recover_unanswered_and_no_loop(tmp_path):
    async def go():
        proc, ig, _ = make(tmp_path)
        proc.store.get_conv("U4")
        mid = proc.store.add_message("U4", "user", "есть 17 про?", "u-4", False)
        from sqlalchemy import update as upd
        from app.db import messages as mt
        with proc.store.engine.begin() as c:
            c.execute(upd(mt).where(mt.c.id == mid).values(created_at=utcnow() - timedelta(minutes=10)))
        proc.store.update_conv("U4", last_user_at=utcnow() - timedelta(minutes=10))
        assert await proc.recover_unanswered(utcnow()) == 1
        await settle(proc)
        assert len(ig.sent) == 1
        # второй раз в течение часа тот же диалог не трогаем
        assert await proc.recover_unanswered(utcnow()) == 0
    asyncio.run(go())
