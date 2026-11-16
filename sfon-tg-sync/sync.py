#!/usr/bin/env python3
"""
Синхронизация б/у телефонов из SFON Open API в Telegram-канал.

Логика одного запуска:
  1. Забираем из SFON все б/у товары (state=OLD), у которых есть фото.
  2. Новый товар -> публикуем пост (альбом из фото + подпись).
  3. Товар изменился (цена, описание, фото, батарея и т.д.) -> удаляем старый пост
     и публикуем новый.
  4. Товара больше нет в выдаче (продан / скрыт) -> удаляем пост.

Если Telegram не даёт удалить старое сообщение (у ботов бывает ограничение 48 часов),
пост не остаётся «висеть» как актуальный: подпись меняется на «ПРОДАНО» / «НЕАКТУАЛЬНО».

Настройки — через переменные окружения (см. README.md).
"""

import hashlib
import html
import json
import logging
import os
import sqlite3
import sys
import time

import requests

# ---------------------------------------------------------------- настройки

SFON_API_KEY = os.environ.get("SFON_API_KEY", "")
SFON_BASE_URL = os.environ.get("SFON_BASE_URL", "https://open-api.sfon.kz/open-api").rstrip("/")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHANNEL = os.environ.get("TG_CHANNEL", "@nurelectronics01")

# Необязательные фильтры
PRODUCT_TYPE_ID = os.environ.get("SFON_PRODUCT_TYPE_ID")  # ID типа «Смартфоны» (если в базе есть не только телефоны)
STORE_ID = os.environ.get("SFON_STORE_ID")                # только один магазин

DB_PATH = os.environ.get("DB_PATH", "state.sqlite3")
DATABASE_URL = os.environ.get("DATABASE_URL")             # если задан — храним состояние в Postgres

MAX_PHOTOS = int(os.environ.get("MAX_PHOTOS", "10"))           # Telegram: максимум 10 в альбоме
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "40"))
POST_DELAY_SEC = float(os.environ.get("POST_DELAY_SEC", "4"))   # пауза между постами (лимиты Telegram)
# Защита: если SFON вдруг вернул 0 товаров, а в канале много постов — ничего не удаляем
SAFETY_MIN_RATIO = float(os.environ.get("SAFETY_MIN_RATIO", "0.3"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync")


# ---------------------------------------------------------------- хранилище

class Store:
    """productId -> (fingerprint, [message_ids]). SQLite по умолчанию, Postgres если задан DATABASE_URL."""

    def __init__(self):
        if DATABASE_URL:
            import psycopg  # noqa: WPS433
            self.conn = psycopg.connect(DATABASE_URL, autocommit=True)
            self.ph = "%s"
        else:
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.isolation_level = None
            self.ph = "?"
        self._exec(
            "CREATE TABLE IF NOT EXISTS posts ("
            " product_id BIGINT PRIMARY KEY,"
            " fingerprint TEXT NOT NULL,"
            " message_ids TEXT NOT NULL,"
            " updated_at BIGINT NOT NULL)"
        )

    def _exec(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql.replace("?", self.ph), params)
        return cur

    def all(self):
        rows = self._exec("SELECT product_id, fingerprint, message_ids FROM posts").fetchall()
        return {int(r[0]): (r[1], json.loads(r[2])) for r in rows}

    def save(self, product_id, fingerprint, message_ids):
        self._exec("DELETE FROM posts WHERE product_id = ?", (product_id,))
        self._exec(
            "INSERT INTO posts (product_id, fingerprint, message_ids, updated_at) VALUES (?, ?, ?, ?)",
            (product_id, fingerprint, json.dumps(message_ids), int(time.time())),
        )

    def delete(self, product_id):
        self._exec("DELETE FROM posts WHERE product_id = ?", (product_id,))


# ---------------------------------------------------------------- SFON

def sfon_fetch_old_products():
    """Все б/у товары в наличии (все страницы). При любой ошибке — исключение, чтобы ничего не удалить по ошибке."""
    body = {"state": "OLD"}
    if PRODUCT_TYPE_ID:
        body["productTypeId"] = int(PRODUCT_TYPE_ID)
    if STORE_ID:
        body["storeId"] = int(STORE_ID)

    items, page = [], 0
    while True:
        for attempt in range(5):
            r = requests.post(
                f"{SFON_BASE_URL}/v1/products/search",
                params={"page": page, "size": 100},
                json=body,
                headers={"X-API-Key": SFON_API_KEY},
                timeout=30,
            )
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            break
        if r.status_code != 200:
            raise RuntimeError(f"SFON {r.status_code}: {r.text[:300]}")
        data = r.json()
        items.extend(data.get("content") or [])
        if data.get("last", True) or page + 1 >= (data.get("totalPages") or 0):
            break
        page += 1
    return items


def photo_urls(p):
    urls = []
    for ph in p.get("photos") or []:
        u = ph.get("url")
        if u and u not in urls:
            urls.append(u)
    return urls[:MAX_PHOTOS]


def fingerprint(p):
    """Всё, что видно в посте. Изменилось что-то из этого — пост перевыкладывается."""
    keys = ["modelName", "familyName", "memoryName", "ramName", "colorName", "averagePrice", "oldPrice",
            "description", "batteryState", "year", "storeName", "storeAddress", "storeTelephone", "storeWhatsapp"]
    payload = {k: p.get(k) for k in keys}
    payload["photos"] = photo_urls(p)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def fmt_price(v):
    if v is None:
        return None
    return f"{int(round(v)):,}".replace(",", " ") + " ₸"


def caption(p):
    e = lambda s: html.escape(str(s))  # noqa: E731
    title = p.get("modelName") or p.get("familyName") or "Телефон"
    specs = " / ".join(x for x in [p.get("memoryName"), p.get("ramName") and f"RAM {p['ramName']}"] if x)
    lines = [f"📱 <b>{e(title)}</b>" + (f" {e(specs)}" if specs else "")]
    if p.get("colorName"):
        lines.append(f"🎨 Цвет: {e(p['colorName'])}")
    if p.get("batteryState") is not None:
        lines.append(f"🔋 Аккумулятор: {e(p['batteryState'])}%")
    if p.get("year"):
        lines.append(f"📅 Год: {e(p['year'])}")
    lines.append("♻️ Состояние: б/у")

    price, old = fmt_price(p.get("averagePrice")), fmt_price(p.get("oldPrice"))
    if price:
        if old and p.get("oldPrice") and p.get("averagePrice") and p["oldPrice"] > p["averagePrice"]:
            lines.append(f"\n💰 <b>{e(price)}</b>  <s>{e(old)}</s>")
        else:
            lines.append(f"\n💰 <b>{e(price)}</b>")
    else:
        lines.append("\n💰 Цена: уточняйте")

    store = []
    if p.get("storeName"):
        store.append(f"🏪 {e(p['storeName'])}")
    if p.get("storeAddress"):
        addr = e(p["storeAddress"])
        if p.get("storeAddressLink"):
            addr = f'<a href="{html.escape(p["storeAddressLink"], quote=True)}">{addr}</a>'
        store.append(f"📍 {addr}")
    if p.get("storeTelephone"):
        store.append(f"☎️ {e(p['storeTelephone'])}")
    if p.get("storeWhatsapp"):
        wa = "".join(ch for ch in p["storeWhatsapp"] if ch.isdigit())
        store.append(f'💬 <a href="https://wa.me/{wa}">WhatsApp</a>' if wa else f"💬 {e(p['storeWhatsapp'])}")
    store_block = ("\n\n" + "\n".join(store)) if store else ""

    desc = (p.get("description") or "").strip()
    head = "\n".join(lines)
    # Лимит подписи в Telegram — 1024 символа (считаем с запасом, по экранированному тексту)
    if desc:
        room = 1000 - len(head) - len(store_block) - 4
        if room > 20:
            d = desc if len(desc) <= room else desc[: room - 1].rstrip() + "…"
            head += "\n\n📝 " + e(d)
    return head + store_block


# ---------------------------------------------------------------- Telegram

class TelegramError(Exception):
    pass


def tg(method, data=None, files=None):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    for attempt in range(6):
        try:
            r = requests.post(url, data=data, files=files, timeout=90)
            res = r.json()
        except (requests.RequestException, ValueError) as ex:
            log.warning("Telegram %s: сеть/ответ %s, повтор", method, ex)
            time.sleep(3 * (attempt + 1))
            continue
        if res.get("ok"):
            return res["result"]
        retry = (res.get("parameters") or {}).get("retry_after")
        if retry:
            log.info("Лимит Telegram, ждём %s сек", retry)
            time.sleep(retry + 1)
            continue
        raise TelegramError(f"{method}: {res.get('description')}")
    raise TelegramError(f"{method}: не удалось после повторов")


def _download(url):
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def post_product(p):
    """Публикует пост. Возвращает список message_id."""
    urls, cap = photo_urls(p), caption(p)
    if DRY_RUN:
        log.info("[DRY] пост %s: %d фото\n%s", p.get("productId"), len(urls), cap)
        return [0]

    def send(upload):
        if len(urls) == 1:
            if upload:
                return [tg("sendPhoto", {"chat_id": TG_CHANNEL, "caption": cap, "parse_mode": "HTML"},
                           {"photo": ("1.jpg", _download(urls[0]))})["message_id"]]
            return [tg("sendPhoto", {"chat_id": TG_CHANNEL, "photo": urls[0], "caption": cap,
                                     "parse_mode": "HTML"})["message_id"]]
        media, files = [], {}
        for i, u in enumerate(urls):
            item = {"type": "photo", "media": u}
            if upload:
                files[f"f{i}"] = (f"{i}.jpg", _download(u))
                item["media"] = f"attach://f{i}"
            if i == 0:
                item.update(caption=cap, parse_mode="HTML")
            media.append(item)
        res = tg("sendMediaGroup", {"chat_id": TG_CHANNEL, "media": json.dumps(media)}, files or None)
        return [m["message_id"] for m in res]

    try:
        return send(upload=False)
    except TelegramError as ex:
        # Telegram не смог сам скачать фото по ссылке — скачиваем и загружаем файлом
        log.info("Отправка по ссылке не удалась (%s), загружаем файлы", ex)
        return send(upload=True)


def remove_post(message_ids, mark):
    """Удаляет пост. Если удалить нельзя — меняет подпись на пометку (ПРОДАНО / НЕАКТУАЛЬНО)."""
    if DRY_RUN:
        log.info("[DRY] удаление %s", message_ids)
        return
    ids = [m for m in message_ids if m]
    if not ids:
        return
    try:
        tg("deleteMessages", {"chat_id": TG_CHANNEL, "message_ids": json.dumps(ids)})
        return
    except TelegramError as ex:
        log.warning("Не удалось удалить %s (%s) — помечаем «%s»", ids, ex, mark)
    try:
        tg("editMessageCaption", {"chat_id": TG_CHANNEL, "message_id": ids[0],
                                  "caption": f"❌ <b>{mark}</b>", "parse_mode": "HTML"})
    except TelegramError as ex:
        # Сообщение уже удалили вручную — это нормально
        log.warning("Не удалось пометить %s: %s", ids[0], ex)


# ---------------------------------------------------------------- основной цикл

def run():
    missing = [n for n, v in [("SFON_API_KEY", SFON_API_KEY), ("TG_BOT_TOKEN", TG_BOT_TOKEN)] if not v]
    if missing and not DRY_RUN:
        log.error("Не заданы переменные: %s", ", ".join(missing))
        return 2

    store = Store()
    posted = store.all()

    products = sfon_fetch_old_products()
    current = {}
    for p in products:
        pid = p.get("productId")
        if pid is None or p.get("state", "OLD") != "OLD" or not photo_urls(p):
            continue
        current[int(pid)] = p
    log.info("SFON: б/у всего %d, с фото %d; в канале постов: %d", len(products), len(current), len(posted))
    types = {}
    for p in current.values():
        key = f"{p.get('productTypeName')} (код {p.get('productTypeCode')})"
        types[key] = types.get(key, 0) + 1
    log.info("Типы товаров с фото: %s", types)

    # Защита от массового удаления при сбое на стороне SFON
    if posted and len(current) < len(posted) * SAFETY_MIN_RATIO and len(posted) >= 10:
        log.error("SFON вернул подозрительно мало товаров (%d при %d постах) — удаление пропущено",
                  len(current), len(posted))
        sold = []
    else:
        sold = [pid for pid in posted if pid not in current]

    stats = {"sold": 0, "updated": 0, "new": 0, "deferred": 0}

    # 1. Проданные / снятые — удаляем
    for pid in sold:
        remove_post(posted[pid][1], "ПРОДАНО")
        if not DRY_RUN:
            store.delete(pid)
        stats["sold"] += 1
        log.info("Удалён пост товара %s (нет в базе)", pid)

    # 2. Изменённые и новые — публикуем (с лимитом за запуск)
    queue = []
    for pid, p in current.items():
        fp = fingerprint(p)
        if pid in posted:
            if posted[pid][0] != fp:
                queue.append(("updated", pid, p, fp))
        else:
            queue.append(("new", pid, p, fp))
    # сначала обновления (чтобы в канале не висела старая цена), потом новые
    queue.sort(key=lambda x: x[0] != "updated")

    budget = MAX_POSTS_PER_RUN
    for kind, pid, p, fp in queue:
        if budget <= 0:
            stats["deferred"] += 1
            continue
        try:
            new_ids = post_product(p)
        except Exception as ex:  # noqa: BLE001 — один плохой товар не должен ломать весь запуск
            log.error("Не удалось опубликовать товар %s: %s", pid, ex)
            continue
        if kind == "updated":
            # новый пост уже вышел — теперь убираем старый
            remove_post(posted[pid][1], "НЕАКТУАЛЬНО — см. новый пост")
        if not DRY_RUN:
            store.save(pid, fp, new_ids)
        stats[kind] += 1
        budget -= 1
        log.info("%s товар %s → сообщения %s", "Обновлён" if kind == "updated" else "Опубликован", pid, new_ids)
        if not DRY_RUN:
            time.sleep(POST_DELAY_SEC)

    log.info("Готово: новых %(new)d, обновлено %(updated)d, удалено %(sold)d, отложено до след. запуска %(deferred)d",
             stats)
    return 0


if __name__ == "__main__":
    sys.exit(run())
