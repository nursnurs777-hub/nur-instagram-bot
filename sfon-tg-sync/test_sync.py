"""Проверка логики без сети: подменяем SFON и Telegram."""
import os
import tempfile

os.environ["DB_PATH"] = tempfile.mktemp(suffix=".sqlite3")
os.environ["SFON_API_KEY"] = "x"
os.environ["TG_BOT_TOKEN"] = "x"
os.environ["POST_DELAY_SEC"] = "0"
os.environ["MAX_NEW_PER_RUN"] = "100"
os.environ["QUIET_HOURS"] = ""

import sync  # noqa: E402

calls = []
next_id = [100]
CATALOG = []


def fake_tg(method, data=None, files=None):
    calls.append((method, data))
    if method == "sendPhoto":
        next_id[0] += 1
        return {"message_id": next_id[0]}
    if method == "sendMediaGroup":
        import json
        n = len(json.loads(data["media"]))
        res = []
        for _ in range(n):
            next_id[0] += 1
            res.append({"message_id": next_id[0]})
        return res
    if method == "deleteMessages":
        if FAIL_DELETE:
            raise sync.TelegramError("message can't be deleted")
        return True
    return True


FAIL_DELETE = False
sync.tg = fake_tg
sync.sfon_fetch_old_products = lambda: [dict(p) for p in CATALOG]


def item(pid, price, photos=1, **kw):
    d = {"productId": pid, "state": "OLD", "modelName": f"iPhone {pid}", "memoryName": "128 GB",
         "colorName": "Black", "batteryState": 88, "year": 2022, "averagePrice": price,
         "storeName": "Nur Electronics", "storeAddress": "Астана, ул. Тест 1", "storeWhatsapp": "+7 (701) 000-00-00",
         "photos": [{"url": f"https://shop.sfon.kz/storage/{pid}_{i}.jpg"} for i in range(photos)]}
    d.update(kw)
    return d


def methods():
    m = [c[0] for c in calls]
    calls.clear()
    return m


# Запуск 1: три товара с фото + один без фото
CATALOG[:] = [item(1, 250000), item(2, 180000, photos=3), item(3, 90000), item(4, 50000, photos=0)]
sync.run()
m = methods()
assert m.count("sendPhoto") == 2 and m.count("sendMediaGroup") == 1, m
print("1) новые: ок —", m)

# Запуск 2: ничего не изменилось
sync.run()
assert methods() == []
print("2) без изменений: ок")

# Запуск 3: у №1 сменилась цена, №3 продан
CATALOG[:] = [item(1, 230000), item(2, 180000, photos=3)]
sync.run()
m = methods()
assert m == ["deleteMessages", "sendPhoto", "deleteMessages"], m
print("3) продан + цена изменилась: ок —", m)

# Запуск 4: удаление запрещено (старый пост) -> помечаем «ПРОДАНО»
FAIL_DELETE = True
CATALOG[:] = [item(1, 230000)]
sync.run()
m = methods()
assert m == ["deleteMessages", "editMessageCaption"], m
print("4) удалить нельзя → пометка ПРОДАНО: ок")
FAIL_DELETE = False

# Запуск 5: защита — SFON вернул пусто при большом количестве постов
CATALOG[:] = [item(i, 100000 + i) for i in range(10, 30)]
sync.run(); methods()
CATALOG[:] = []
sync.run()
assert methods() == []
print("5) защита от пустого ответа SFON: ок")

# Подпись
cap = sync.caption(item(7, 199990, oldPrice=220000, ramName="6 GB", description="Отличное состояние <без царапин>"))
assert len(cap) <= 1024 and "&lt;без" in cap
print("\nПример подписи:\n" + cap)

# Запуск 6: поменялся только формат подписи (добавили WhatsApp) — редактируем, не перевыкладываем
sync.store_reset = None
CATALOG[:] = [item(i, 100000 + i) for i in range(10, 30)]
sync.run(); methods()
sync.WHATSAPP_NUMBER = "8 701 111 22 33"
sync.run()
m = methods()
assert m == ["editMessageCaption"] * 20, m
sync.run()
assert methods() == []
cap = sync.caption(item(10, 100010))
assert "wa.me/77011112233?text=" in cap and "Nur Electronics" not in cap, cap
print("6) смена формата → редактирование подписи: ок\n" + cap)

# Запуск 7: лимит новых постов и тихие часы
sync.MAX_NEW_PER_RUN = 1
CATALOG[:] = CATALOG + [item(i, 50000 + i) for i in range(40, 43)]
sync.run(); m = methods()
assert m.count("sendPhoto") == 1, m
sync.QUIET_HOURS = "%d-%d" % ((__import__("time").gmtime().tm_hour + 5) % 24, ((__import__("time").gmtime().tm_hour + 6) % 24))
sync.run(); m = methods()
assert "sendPhoto" not in m, m
print("7) один новый пост за запуск, тихие часы: ок")

# 8: неполные карточки не публикуются, а уже выложенные удаляются
sync.QUIET_HOURS = ""; sync.MAX_NEW_PER_RUN = 100
CATALOG[:] = [item(100, None), item(101, 90000, batteryState=None), item(102, 90000, colorName=None), item(103, 90000)]
sync.run(); m = methods()
assert m.count("sendPhoto") == 1, m
print("8) публикуются только полные карточки: ок")
