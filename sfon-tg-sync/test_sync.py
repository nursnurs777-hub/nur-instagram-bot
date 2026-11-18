"""Проверка логики без сети: SFON и Telegram подменены, состояние — SQLite в памяти.
Запуск: python test_sync.py"""
import json
import os

os.environ.update(QUIET_HOURS="", POST_DELAY_SEC="0", MAX_NEW_PER_RUN="100", WHATSAPP_NUMBER="+77028881111",
                  SFON_API_KEY="x", TG_BOT_TOKEN="x", TG_CHANNEL="@testchan")
for k in ("NUR_DB_URL", "DATABASE_URL", "RENDER"):
    os.environ.pop(k, None)

import sync  # noqa: E402
import tgcommon  # noqa: E402
from tgcommon import State, TgError, handle_callback  # noqa: E402


class FakeTG:
    def __init__(self):
        self.msgs, self.calls, self.next_id = {}, [], 100
        self.undeletable, self.fail = False, {}

    def __call__(self, token, method, data=None, files=None, wait=True, attempts=6):
        data = dict(data or {})
        self.calls.append(method)
        if self.fail.get(method):
            self.fail[method] -= 1
            raise TgError(method, "Bad Request: test failure")
        if method in ("sendPhoto", "sendMessage"):
            self.next_id += 1
            self.msgs[self.next_id] = {"kind": "photo" if method == "sendPhoto" else "text",
                                       "media": data.get("photo"), "text": data.get("text"),
                                       "kb": json.loads(data.get("reply_markup", "null") or "null")}
            return {"message_id": self.next_id}
        if method == "sendMediaGroup":
            out = []
            for m in json.loads(data["media"]):
                self.next_id += 1
                self.msgs[self.next_id] = {"kind": "photo", "media": m["media"], "text": None, "kb": None}
                out.append({"message_id": self.next_id})
            return out
        mid = int(data.get("message_id", 0))
        if method == "deleteMessages":
            if self.undeletable:
                raise TgError(method, "Bad Request: message can't be deleted")
            for i in json.loads(data["message_ids"]):
                self.msgs.pop(i, None)
            return True
        if method.startswith("edit"):
            if mid not in self.msgs:
                raise TgError(method, "Bad Request: message to edit not found")
            m = self.msgs[mid]
            if method == "editMessageText":
                m["text"] = data["text"]
            if method == "editMessageCaption":
                m["text"] = data["caption"]
            if method == "editMessageMedia":
                m["media"] = json.loads(data["media"])["media"]
            if "reply_markup" in data:
                m["kb"] = json.loads(data["reply_markup"])
            elif method in ("editMessageText", "editMessageCaption"):
                m["kb"] = None
            return True
        return True

    def take(self):
        c, self.calls = self.calls, []
        return c


tg = FakeTG()
sync.tg_call = tg
tgcommon.tg_call = tg
sync.log_company = lambda: None
sync._download = lambda url: b"jpeg"
CATALOG = []
sync.sfon_fetch_old_products = lambda: [json.loads(json.dumps(p)) for p in CATALOG]
PG = os.environ.get("TEST_PG_URL")  # проверка на настоящем Postgres
if PG:
    import psycopg
    with psycopg.connect(PG, autocommit=True) as c:
        for t in ("sfon_posts", "sfon_posts_votes", "sfon_posts_cb", "sfon_posts_archive"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
state = State("posts", url=PG) if PG else State("posts", sqlite_path=":memory:")


def run():
    sync.run(state)


def item(pid, price=380000, photos=(1, 2, 3), **kw):
    d = {"productId": pid, "state": "OLD", "familyName": "iPhone", "modelName": "17", "memoryName": "256",
         "colorName": "Mist Blue", "batteryState": 100, "averagePrice": price,
         "photos": [{"id": i, "url": f"https://shop.sfon.kz/storage/{pid}_{i}.jpg?v=1"} for i in photos]}
    d.update(kw)
    return d


def row(pid):
    return state.get_post(pid)


def desc(pid):
    return tg.msgs[row(pid)["desc_message_id"]]


def counts_on_buttons(pid):
    return [b["text"] for b in desc(pid)["kb"]["inline_keyboard"][0]]


CB = [0]


def press(pid, user, code, message_id=None, cb_id=None):
    CB[0] += 1
    cq = {"id": cb_id or f"cb{CB[0]}", "data": f"v:{pid}:{code}", "from": {"id": user},
          "message": {"message_id": message_id or row(pid)["desc_message_id"], "chat": {"id": -1, "username": "testchan"}}}
    return handle_callback("x", "@testchan", state, cq)


def ok(n, text):
    print(f"{n}) {text}: ок")


# 1. Новый товар: альбом из 3 фото + описание с кнопками
CATALOG[:] = [item(1)]
run()
c = tg.take()
assert c.count("sendMediaGroup") == 1 and c.count("sendMessage") == 1, c
assert counts_on_buttons(1) == ["❤️ 0", "🔥 0", "👀 0"]
kb = desc(1)["kb"]["inline_keyboard"]
assert kb[1][0]["text"] == "💬 Написать в WhatsApp" and "wa.me/77028881111" in kb[1][0]["url"]
t = desc(1)["text"]
assert "💾 Память: 256 ГБ" in t and "🔋 Аккумулятор: 100%" in t and "💰 Цена: <b>380 000 ₸</b>" in t
assert "реакц" not in t.lower() and "❤️" not in t
ok(1, "новое объявление: альбом + описание с кнопками")

# 2. Повторная синхронизация без изменений — ничего
run()
assert tg.take() == []
ok(2, "без изменений ничего не публикуется")

# 3. Голосование: одна реакция на человека, отмена, переключение, повторная доставка
assert press(1, 10, "h") == "added" and counts_on_buttons(1) == ["❤️ 1", "🔥 0", "👀 0"]
assert press(1, 10, "h") == "removed" and counts_on_buttons(1) == ["❤️ 0", "🔥 0", "👀 0"]
press(1, 10, "h")
assert press(1, 10, "f") == "changed" and counts_on_buttons(1) == ["❤️ 0", "🔥 1", "👀 0"]
press(1, 11, "f")
press(1, 12, "e")
assert counts_on_buttons(1) == ["❤️ 0", "🔥 2", "👀 1"]
assert press(1, 13, "h", cb_id="dup") == "added"
assert press(1, 13, "h", cb_id="dup") == "duplicate"
assert counts_on_buttons(1) == ["❤️ 1", "🔥 2", "👀 1"]
assert press(1, 14, "x") == "ignored"
tg.take()
ok(3, "голоса: одна реакция, отмена, смена, повторная доставка не считается")

# 4. Поменялась только цена — правка на месте, голоса и кнопки сохранены
mid_before = row(1)["desc_message_id"]
CATALOG[:] = [item(1, price=350000)]
run()
c = tg.take()
assert c == ["editMessageText"], c
assert row(1)["desc_message_id"] == mid_before and "350 000 ₸" in desc(1)["text"]
assert counts_on_buttons(1) == ["❤️ 1", "🔥 2", "👀 1"] and "350%20000" in desc(1)["kb"]["inline_keyboard"][1][0]["url"]
ok(4, "цена: правка описания на месте, голоса не обнулились")

# 5. Добавили 4-е фото — новое объявление целиком, старое удалено, голоса переехали
old = row(1)
CATALOG[:] = [item(1, price=350000, photos=(1, 2, 3, 4))]
run()
c = tg.take()
assert c.count("sendMediaGroup") == 1 and c.count("sendMessage") == 1 and "deleteMessages" in c, c
assert len(row(1)["message_ids"]) == 4 and row(1)["desc_message_id"] != old["desc_message_id"]
assert all(m not in tg.msgs for m in old["message_ids"] + [old["desc_message_id"]])
assert counts_on_buttons(1) == ["❤️ 1", "🔥 2", "👀 1"]
assert press(1, 20, "h", message_id=old["desc_message_id"]) == "stale"
assert counts_on_buttons(1) == ["❤️ 1", "🔥 2", "👀 1"]
tg.take()
ok(5, "3 → 4 фото: новое объявление, старое удалено, голоса сохранены, старые кнопки не принимают голоса")

# 6. Ещё 2 фото (4 → 6)
CATALOG[:] = [item(1, price=350000, photos=(1, 2, 3, 4, 5, 6))]
run()
tg.take()
assert len(row(1)["message_ids"]) == 6 and counts_on_buttons(1) == ["❤️ 1", "🔥 2", "👀 1"]
ok(6, "4 → 6 фото: перепубликация")

# 7. Заменили одно фото (количество то же) — замена снимка в альбоме на месте
mids = row(1)["message_ids"]
CATALOG[:] = [item(1, price=350000, photos=(1, 2, 3, 4, 5, 7))]
run()
c = tg.take()
assert c == ["editMessageMedia"], c
assert row(1)["message_ids"] == mids and "1_7.jpg" in tg.msgs[mids[5]]["media"]
ok(7, "замена фото без изменения количества: правка снимка в альбоме")

# 8. Поменялся только порядок фото / параметры ссылок — ничего
p = item(1, price=350000, photos=(7, 5, 4, 3, 2, 1))
for ph in p["photos"]:
    ph["url"] = ph["url"].replace("?v=1", "?v=2")
CATALOG[:] = [p]
run()
assert tg.take() == []
ok(8, "смена порядка фото — без перепубликации")

# 9. Сбой отправки описания: старое не удаляется, альбом повторно не отправляется
CATALOG[:] = [item(1, price=350000, photos=(1, 2, 3, 4, 5, 7, 8))]
old = row(1)
tg.fail["sendMessage"] = 1
run()
c = tg.take()
assert c.count("sendMediaGroup") == 1 and "deleteMessages" not in c, c
assert row(1)["desc_message_id"] == old["desc_message_id"] and row(1)["pending"]
assert old["desc_message_id"] in tg.msgs
run()
c = tg.take()
assert "sendMediaGroup" not in c and c.count("sendMessage") == 1 and "deleteMessages" in c, c
assert len(row(1)["message_ids"]) == 7 and not row(1)["pending"]
ok(9, "сбой описания: старое на месте, при повторе альбом не дублируется")

# 10. Сбой отправки альбома: ничего не удалено, со второй попытки — одна публикация
CATALOG[:] = [item(1, price=350000, photos=(1, 2, 3, 4, 5, 7, 8, 9))]
old = row(1)
tg.fail["sendMediaGroup"] = 2  # по ссылке и загрузкой файлом
run()
c = tg.take()
assert "deleteMessages" not in c and "sendMessage" not in c and row(1)["desc_message_id"] == old["desc_message_id"]
run()
c = tg.take()
assert c.count("sendMediaGroup") == 1 and c.count("sendMessage") == 1, c
ok(10, "сбой альбома: старое не тронуто, повтор без лишних альбомов")

# 11. Старое уже нельзя удалить — «ОБЪЯВЛЕНИЕ ОБНОВЛЕНО» со ссылкой, без кнопок
old = row(1)
tg.undeletable = True
CATALOG[:] = [item(1, price=350000, photos=(1, 2, 3, 4, 5, 7, 8, 9, 10))]
run()
tg.take()
od = tg.msgs[old["desc_message_id"]]
assert "ОБЪЯВЛЕНИЕ ОБНОВЛЕНО" in od["text"] and f"t.me/testchan/{row(1)['desc_message_id']}" in od["text"]
assert od["kb"] == {"inline_keyboard": []}
assert "Неактуально" in tg.msgs[old["message_ids"][0]]["text"]
assert counts_on_buttons(1) == ["❤️ 1", "🔥 2", "👀 1"]
assert not state.archive_list()
tg.undeletable = False
ok(11, "нельзя удалить: старое помечено «обновлено», кнопки убраны")

# 12. Больше 10 фото — несколько альбомов, ни одно фото не теряется
CATALOG[:] = [item(1, price=350000, photos=range(1, 26))]
run()
c = tg.take()
assert c.count("sendMediaGroup") == 3 and len(row(1)["message_ids"]) == 25, c
ok(12, "25 фото: 3 альбома (9+8+8), все фото опубликованы")

# 13. Продан: удаление; если нельзя — «ПРОДАНО» без кнопок
CATALOG[:] = [item(1, price=350000, photos=range(1, 26)), item(2)]
run()
tg.take()
d2 = row(2)
CATALOG[:] = [item(1, price=350000, photos=range(1, 26))]
tg.undeletable = True
run()
tg.take()
assert row(2) is None and "ПРОДАНО" in tg.msgs[d2["desc_message_id"]]["text"]
assert tg.msgs[d2["desc_message_id"]]["kb"] == {"inline_keyboard": []}
assert press(2, 30, "h", message_id=d2["desc_message_id"]) == "stale"
tg.undeletable = False
CATALOG[:] = []
tg.take()
ok(13, "продан: кнопки убраны, голосовать нельзя")

# 14. Старый пост без кнопок: не трогаем, пока товар не изменился; изменился — новый формат с кнопками
state.delete_post(1)
legacy = item(5)
tg.msgs[900] = {"kind": "photo", "media": "x", "text": "старая подпись", "kb": None}
state.save_post({"product_id": 5, "message_ids": [900], "photo_ids": None, "desc_message_id": None,
                 "legacy_fingerprint": sync.legacy_fingerprint(legacy) + ":abc"})
CATALOG[:] = [legacy]
run()
assert tg.take() == []
CATALOG[:] = [item(5, price=300000)]
run()
c = tg.take()
assert c.count("sendMessage") == 1 and 900 not in tg.msgs and row(5)["desc_message_id"], c
ok(14, "старые посты остаются, при изменении товара — перевыкладка с кнопками")

# 15. Тихие часы: перепубликация ждёт утра, правка цены — сразу
sync.QUIET_HOURS = "0-24"
sync.is_quiet_now = lambda: True
CATALOG[:] = [item(5, price=290000, photos=(1, 2, 3, 4))]
run()
c = tg.take()
assert "sendMediaGroup" not in c, c
sync.is_quiet_now = lambda: False
run()
assert tg.take().count("sendMediaGroup") == 1
ok(15, "ночью публикации ждут, правки — сразу")

# 16. Неполная карточка не публикуется
CATALOG[:] = [item(5, price=290000, photos=(1, 2, 3, 4)), item(6, price=None), item(7, batteryState=None)]
run()
assert tg.take() == [] and row(6) is None and row(7) is None
ok(16, "без цены/АКБ не публикуется")

# 17. Одновременные нажатия (только Postgres): 30 человек жмут разом — все голоса учтены
if PG:
    import threading
    def worker(u):
        st = State("posts", url=PG, ensure=False)
        cq = {"id": f"par{u}", "data": "v:5:h", "from": {"id": 1000 + u},
              "message": {"message_id": row(5)["desc_message_id"], "chat": {"id": -1, "username": "testchan"}}}
        handle_callback("x", "@testchan", st, cq)
        st.close()
    ths = [threading.Thread(target=worker, args=(u,)) for u in range(30)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert state.counts(5).get("h") == 30, state.counts(5)
    ok(17, "30 одновременных нажатий — все учтены (Postgres)")

print("\nПример описания:\n" + desc(5)["text"])
print("Кнопки:", [[b["text"] for b in r] for r in desc(5)["kb"]["inline_keyboard"]])
