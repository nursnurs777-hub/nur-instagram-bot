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
import re
import time
import urllib.parse

import requests

# ---------------------------------------------------------------- настройки

SFON_API_KEY = os.environ.get("SFON_API_KEY", "")
SFON_BASE_URL = os.environ.get("SFON_BASE_URL", "https://open-api.sfon.kz/open-api").rstrip("/")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHANNEL = os.environ.get("TG_CHANNEL", "@nurelectronics01")

# Необязательные фильтры
PRODUCT_TYPE_ID = os.environ.get("SFON_PRODUCT_TYPE_ID")  # ID типа «Смартфоны» (если в базе есть не только телефоны)
STORE_ID = os.environ.get("SFON_STORE_ID")                # только один магазин

# Номер WhatsApp для ссылки под постом (если не задан — берётся WhatsApp магазина из SFON)
WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "")
SHOW_STORE_NAME = os.environ.get("SHOW_STORE_NAME", "0") == "1"
SHOW_YEAR = os.environ.get("SHOW_YEAR", "1") == "1"   # показывать год выпуска в посте   # название склада («Основной») не показываем

DB_PATH = os.environ.get("DB_PATH", "state.sqlite3")
DATABASE_URL = os.environ.get("DATABASE_URL")             # если задан — храним состояние в Postgres

MAX_PHOTOS = int(os.environ.get("MAX_PHOTOS", "10"))           # Telegram: максимум 10 в альбоме
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "40"))   # всего публикаций за запуск (обновления + новые)
MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", "1"))       # новых товаров за запуск: 1 = не чаще раза в 10 минут
# Тихие часы (по Астане): новые посты и перевыкладка не делаются, удаление проданных — всегда. Пусто = без тихих часов.
QUIET_HOURS = os.environ.get("QUIET_HOURS", "23-8")
TABLE_NAME = os.environ.get("TABLE_NAME", "posts")                  # своя таблица для каждого филиала в общей базе
POST_DELAY_SEC = float(os.environ.get("POST_DELAY_SEC", "4"))   # пауза между постами (лимиты Telegram)
# Защита: если SFON вдруг вернул 0 товаров, а в канале много постов — ничего не удаляем
SAFETY_MIN_RATIO = float(os.environ.get("SAFETY_MIN_RATIO", "0.3"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync")


# ---------------------------------------------------------------- хранилище

if not TABLE_NAME.replace("_", "").isalnum():
    raise SystemExit("TABLE_NAME: только латинские буквы, цифры и _")


def is_quiet_now():
    if not QUIET_HOURS.strip():
        return False
    try:
        from zoneinfo import ZoneInfo
        hour = __import__("datetime").datetime.now(ZoneInfo("Asia/Almaty")).hour
    except Exception:  # noqa: BLE001 — нет базы часовых поясов: Астана = UTC+5
        hour = (time.gmtime().tm_hour + 5) % 24
    start, end = (int(x) for x in QUIET_HOURS.split("-"))
    return start <= hour < end if start < end else (hour >= start or hour < end)


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
            f"CREATE TABLE IF NOT EXISTS {TABLE_NAME} ("
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
        rows = self._exec(f"SELECT product_id, fingerprint, message_ids FROM {TABLE_NAME}").fetchall()
        return {int(r[0]): (r[1], json.loads(r[2])) for r in rows}

    def save(self, product_id, fingerprint, message_ids):
        self._exec(f"DELETE FROM {TABLE_NAME} WHERE product_id = ?", (product_id,))
        self._exec(
            f"INSERT INTO {TABLE_NAME} (product_id, fingerprint, message_ids, updated_at) VALUES (?, ?, ?, ?)",
            (product_id, fingerprint, json.dumps(message_ids), int(time.time())),
        )

    def delete(self, product_id):
        self._exec(f"DELETE FROM {TABLE_NAME} WHERE product_id = ?", (product_id,))


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


def missing_fields(p):
    """Публикуем только полностью заполненные карточки: модель, память, цвет, АКБ, цена, фото."""
    checks = {
        "модель": p.get("modelName") or p.get("familyName"),
        "память": p.get("memoryName"),
        "цвет": p.get("colorName"),
        "АКБ": p.get("batteryState") is not None,
        "цена": (p.get("averagePrice") or 0) > 0,
        "фото": photo_urls(p),
    }
    return [name for name, ok in checks.items() if not ok]


def fingerprint(p):
    """Всё, что видно в посте. Изменилось что-то из этого — пост перевыкладывается."""
    keys = ["modelName", "familyName", "memoryName", "ramName", "colorName", "averagePrice", "oldPrice",
            "description", "batteryState", "year", "storeName", "storeAddress", "storeTelephone", "storeWhatsapp"]
    payload = {k: p.get(k) for k in keys}
    payload["photos"] = photo_urls(p)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def caption_hash(p):
    return hashlib.sha256(caption(p).encode()).hexdigest()[:16]


def split_fp(stored):
    """Сохранённый отпечаток: «данные:подпись». Старые записи — только данные."""
    data, _, cap = stored.partition(":")
    return data, cap


def fmt_price(v):
    if v is None:
        return None
    return f"{int(round(v)):,}".replace(",", " ") + " ₸"


def caption(p):
    e = lambda s: html.escape(str(s))  # noqa: E731
    fam, model = (p.get("familyName") or "").strip(), (p.get("modelName") or "").strip()
    if fam and model and fam.lower() not in model.lower():
        title = f"{fam} {model}"
    else:
        title = model or fam or "Телефон"

    def gb(v):
        v = (v or "").strip()
        return f"{v} ГБ" if v.isdigit() else v

    specs = " / ".join(x for x in [gb(p.get("memoryName")), p.get("ramName") and f"RAM {gb(p['ramName'])}"] if x)
    lines = [f"📱 <b>{e(title)}</b>" + (f" {e(specs)}" if specs else "")]
    if p.get("colorName"):
        lines.append(f"🎨 Цвет: {e(p['colorName'])}")
    if p.get("batteryState") is not None:
        lines.append(f"🔋 Аккумулятор: {e(p['batteryState'])}%")
    if SHOW_YEAR and p.get("year"):
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
    if SHOW_STORE_NAME and p.get("storeName"):
        store.append(f"🏪 {e(p['storeName'])}")
    if p.get("storeAddress"):
        addr = e(p["storeAddress"])
        if p.get("storeAddressLink"):
            addr = f'<a href="{html.escape(p["storeAddressLink"], quote=True)}">{addr}</a>'
        store.append(f"📍 {addr}")
    if p.get("storeTelephone"):
        store.append(f"☎️ {e(p['storeTelephone'])}")
    wa = "".join(ch for ch in (WHATSAPP_NUMBER or p.get("storeWhatsapp") or "") if ch.isdigit())
    if len(wa) == 11 and wa.startswith("8"):
        wa = "7" + wa[1:]
    if wa:
        msg = f"Здравствуйте! Интересует {title}" + (f" {specs}" if specs else "") + (f" за {price}" if price else "")
        link = f"https://wa.me/{wa}?text={urllib.parse.quote(msg)}"
        store.append(f'💬 <a href="{html.escape(link, quote=True)}"><b>Написать в WhatsApp</b></a>')
    store_block = ("\n\n" + "\n".join(store)) if store else ""

    desc = (p.get("description") or "").strip()
    head = "\n".join(lines)
    # Лимит подписи в Telegram — 1024 символа (считаем с запасом, по экранированному тексту)
    if desc:
        visible = lambda t: len(html.unescape(re.sub(r"<[^>]+>", "", t)))  # noqa: E731 — Telegram считает видимый текст
        room = 1000 - visible(head) - visible(store_block) - 4
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

    if os.environ.get("RENDER") and not DATABASE_URL and not DRY_RUN:
        log.error("На Render файловая система временная: задайте DATABASE_URL (Postgres), иначе посты будут дублироваться")
        return 2

    store = Store()
    posted = store.all()

    try:
        r = requests.get(f"{SFON_BASE_URL}/v1/company", headers={"X-API-Key": SFON_API_KEY}, timeout=30)
        if r.ok:
            c = r.json()
            cd = c.get("companyDto") or {}
            log.info("Компания: %s, тел. %s, адрес %s", cd.get("companyName"), cd.get("telephone"), cd.get("address"))
            for st in c.get("stores") or []:
                log.info("Магазин id=%s «%s»: адрес %s, тел. %s, WhatsApp %s", st.get("id"), st.get("storeName"),
                         st.get("address"), st.get("telephone"), st.get("telephoneWhatsapp"))
    except Exception as ex:  # noqa: BLE001 — справочная информация, не критично
        log.warning("Не удалось получить данные компании: %s", ex)

    products = sfon_fetch_old_products()
    current, skipped = {}, {}
    for p in products:
        pid = p.get("productId")
        if pid is None or p.get("state", "OLD") != "OLD":
            continue
        missing = missing_fields(p)
        if missing:
            for f in missing:
                skipped[f] = skipped.get(f, 0) + 1
            continue
        current[int(pid)] = p
    if skipped:
        log.info("Пропущены товары с незаполненными полями (сколько раз не хватает): %s", skipped)
    log.info("SFON: б/у всего %d, полностью заполнены %d; в канале постов: %d", len(products), len(current), len(posted))
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
    queue, recaption = [], []
    for pid, p in current.items():
        fp = f"{fingerprint(p)}:{caption_hash(p)}"
        if pid in posted:
            old_data, old_cap = split_fp(posted[pid][0])
            if old_data != fingerprint(p):
                queue.append(("updated", pid, p, fp))     # данные товара изменились — перевыкладываем
            elif old_cap != caption_hash(p):
                recaption.append((pid, p, fp))            # поменялся только формат подписи — редактируем
        else:
            queue.append(("new", pid, p, fp))

    stats["edited"] = 0
    for pid, p, fp in recaption:
        ids = [m for m in posted[pid][1] if m]
        if DRY_RUN or not ids:
            continue
        try:
            tg("editMessageCaption", {"chat_id": TG_CHANNEL, "message_id": ids[0],
                                      "caption": caption(p), "parse_mode": "HTML"})
        except TelegramError as ex:
            if "not modified" not in str(ex):
                log.error("Не удалось отредактировать пост товара %s: %s", pid, ex)
                continue
        store.save(pid, fp, posted[pid][1])
        stats["edited"] += 1
        log.info("Отредактирована подпись товара %s", pid)
        time.sleep(1)
    # сначала обновления (чтобы в канале не висела старая цена), потом новые
    queue.sort(key=lambda x: x[0] != "updated")

    budget, new_budget = MAX_POSTS_PER_RUN, MAX_NEW_PER_RUN
    quiet = is_quiet_now()
    if quiet and queue:
        log.info("Тихие часы (%s): публикации отложены до утра", QUIET_HOURS)
    for kind, pid, p, fp in queue:
        if quiet or budget <= 0 or (kind == "new" and new_budget <= 0):
            stats["deferred"] += 1
            continue
        if kind == "new":
            new_budget -= 1
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

    log.info("Готово: новых %(new)d, обновлено %(updated)d, подпись исправлена %(edited)d, удалено %(sold)d, отложено до след. запуска %(deferred)d",
             stats)
    return 0


if __name__ == "__main__":
    sys.exit(run())
