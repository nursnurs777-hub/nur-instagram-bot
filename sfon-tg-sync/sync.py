#!/usr/bin/env python3
"""
Синхронизация б/у телефонов из SFON Open API в Telegram-канал.

Объявление товара = альбом со всеми фото (если фото больше 10 — несколько альбомов подряд)
+ сообщение-описание с кнопками: ❤️ 🔥 👀 со счётчиками и «💬 Написать в WhatsApp».

Каждый запуск (раз в 10 минут):
  • новый полностью заполненный товар → публикуем объявление;
  • изменились цена/описание/АКБ… → правим текст описания на месте (фото, кнопки и голоса остаются);
  • заменили фото (количество то же) → меняем этот снимок в альбоме на месте;
  • добавили/убрали фото → публикуем новое объявление целиком, старое удаляем, а если Telegram
    уже не даёт удалить — помечаем «🔄 ОБЪЯВЛЕНИЕ ОБНОВЛЕНО» со ссылкой на новое и убираем кнопки;
  • товара нет в SFON (продан) → удаляем объявление, если нельзя — «❌ ПРОДАНО» без кнопок.
Голоса привязаны к ID товара в SFON и переживают перепубликацию.
Нажатия кнопок принимает вебхук (app/sfon_reactions.py в сервисе nur-instagram-bot).

Настройки — через переменные окружения (см. README.md).
"""

import hashlib
import html
import json
import logging
import math
import os
import sys
import time
import urllib.parse

import requests

from tgcommon import EMPTY_KEYBOARD, State, TgError, keyboard, post_link, secret_for, tg_call

# ---------------------------------------------------------------- настройки

SFON_API_KEY = os.environ.get("SFON_API_KEY", "")
SFON_BASE_URL = os.environ.get("SFON_BASE_URL", "https://open-api.sfon.kz/open-api").rstrip("/")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHANNEL = os.environ.get("TG_CHANNEL", "@nurelectronics01")

PRODUCT_TYPE_ID = os.environ.get("SFON_PRODUCT_TYPE_ID")  # только этот тип товара
STORE_ID = os.environ.get("SFON_STORE_ID")                # только один магазин

WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "")   # если не задан — WhatsApp магазина из SFON
SHOW_YEAR = os.environ.get("SHOW_YEAR", "1") == "1"

# База состояния: общая база сервиса nur-instagram-bot (там же голоса). DATABASE_URL — старая база, из неё
# один раз переносятся записи. Без обеих — локальный файл SQLite (для тестов).
STATE_DB_URL = os.environ.get("NUR_DB_URL") or os.environ.get("DATABASE_URL")
OLD_DB_URL = os.environ.get("DATABASE_URL") if os.environ.get("NUR_DB_URL") else None
DB_PATH = os.environ.get("DB_PATH", "state.sqlite3")
TABLE_NAME = os.environ.get("TABLE_NAME", "posts")

# Вебхук для кнопок: адрес сервиса, принимающего нажатия, и ключ филиала в нём
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL", "").rstrip("/")
WEBHOOK_KEY = os.environ.get("WEBHOOK_KEY", "")

MAX_PHOTOS = int(os.environ.get("MAX_PHOTOS", "30"))             # 3 альбома по 10; больше — предупреждение в логе
ALBUM_SIZE = 10                                                   # предел Telegram
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "40"))
MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", "1"))     # новых товаров за запуск
MIGRATE_PER_RUN = int(os.environ.get("MIGRATE_PER_RUN", "0"))     # старых постов без кнопок → в новый формат (0 = только при изменении)
QUIET_HOURS = os.environ.get("QUIET_HOURS", "23-8")               # ночью ничего не публикуем (правки и удаление — можно)
POST_DELAY_SEC = float(os.environ.get("POST_DELAY_SEC", "4"))
SAFETY_MIN_RATIO = float(os.environ.get("SAFETY_MIN_RATIO", "0.3"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync")

UPDATED_TEXT = ("🔄 <b>ОБЪЯВЛЕНИЕ ОБНОВЛЕНО</b>\n\n"
                "Добавлены новые фотографии устройства, включая его состояние.\n\n"
                "Актуальные фотографии, цена и возможность покупки — в новой публикации:\n{link}")
OUTDATED_PHOTOS = "❌ Неактуально — актуальные фото в новом объявлении{link}"
SOLD_TEXT = "❌ <b>ПРОДАНО</b>"


def tg(method, data=None, files=None):
    return tg_call(TG_BOT_TOKEN, method, data, files)


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
            r = requests.post(f"{SFON_BASE_URL}/v1/products/search", params={"page": page, "size": 100},
                              json=body, headers={"X-API-Key": SFON_API_KEY}, timeout=30)
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


def log_company():
    try:
        r = requests.get(f"{SFON_BASE_URL}/v1/company", headers={"X-API-Key": SFON_API_KEY}, timeout=30)
        if r.ok:
            cd = r.json().get("companyDto") or {}
            log.info("Компания: %s", cd.get("companyName"))
    except Exception as ex:  # noqa: BLE001 — справочная информация
        log.warning("Не удалось получить данные компании: %s", ex)


def norm_photo_id(ph):
    """Стабильный id фото: id из SFON, иначе ссылка без параметров."""
    if ph.get("id") is not None:
        return f"id:{ph['id']}"
    u = urllib.parse.urlsplit(ph.get("url") or "")
    return f"url:{u.netloc.lower()}{u.path}"


def photo_list(p):
    """[(id, url)] без повторов, в порядке SFON."""
    out, seen = [], set()
    for ph in p.get("photos") or []:
        if not ph.get("url"):
            continue
        pid = norm_photo_id(ph)
        if pid not in seen:
            seen.add(pid)
            out.append((pid, ph["url"]))
    if len(out) > MAX_PHOTOS:
        log.warning("Товар %s: %d фото, публикуются первые %d (MAX_PHOTOS)", p.get("productId"), len(out), MAX_PHOTOS)
        out = out[:MAX_PHOTOS]
    return out


def split_albums(photos):
    """Делим на альбомы по ≤10 фото, примерно поровну (11 → 6+5, а не 10+1)."""
    if not photos:
        return []
    k = math.ceil(len(photos) / ALBUM_SIZE)
    size = math.ceil(len(photos) / k)
    return [photos[i:i + size] for i in range(0, len(photos), size)]


def missing_fields(p):
    """Публикуем только полностью заполненные карточки: модель, память, цвет, АКБ, цена, фото."""
    checks = {
        "модель": p.get("modelName") or p.get("familyName"),
        "память": p.get("memoryName"),
        "цвет": p.get("colorName"),
        "АКБ": p.get("batteryState") is not None,
        "цена": (p.get("averagePrice") or 0) > 0,
        "фото": [u for u in (p.get("photos") or []) if u.get("url")],
    }
    return [name for name, ok in checks.items() if not ok]


# ---------------------------------------------------------------- текст объявления

def fmt_price(v):
    return None if v is None else f"{int(round(v)):,}".replace(",", " ") + " ₸"


def gb(v):
    v = (v or "").strip()
    return f"{v} ГБ" if v.isdigit() else v


def title_of(p):
    fam, model = (p.get("familyName") or "").strip(), (p.get("modelName") or "").strip()
    if fam and model and fam.lower() not in model.lower():
        return f"{fam} {model}"
    return model or fam or "Телефон"


def memory_of(p):
    return " / ".join(x for x in [gb(p.get("memoryName")), p.get("ramName") and f"RAM {gb(p['ramName'])}"] if x)


def desc_text(p):
    e = lambda s: html.escape(str(s))  # noqa: E731
    lines = [f"📱 <b>{e(title_of(p))}</b>", ""]
    if memory_of(p):
        lines.append(f"💾 Память: {e(memory_of(p))}")
    lines.append(f"🎨 Цвет: {e(p.get('colorName'))}")
    lines.append(f"🔋 Аккумулятор: {e(p.get('batteryState'))}%")
    if SHOW_YEAR and p.get("year"):
        lines.append(f"📅 Год: {e(p['year'])}")
    price, old = fmt_price(p.get("averagePrice")), fmt_price(p.get("oldPrice"))
    if old and (p.get("oldPrice") or 0) > (p.get("averagePrice") or 0):
        lines.append(f"💰 Цена: <b>{e(price)}</b>  <s>{e(old)}</s>")
    else:
        lines.append(f"💰 Цена: <b>{e(price)}</b>")
    desc = (p.get("description") or "").strip()
    if desc:
        lines += ["", "📝 " + e(desc if len(desc) <= 1500 else desc[:1499].rstrip() + "…")]
    extra = []
    if p.get("storeAddress"):
        addr = e(p["storeAddress"])
        if p.get("storeAddressLink"):
            addr = f'<a href="{html.escape(p["storeAddressLink"], quote=True)}">{addr}</a>'
        extra.append(f"📍 {addr}")
    if p.get("storeTelephone"):
        extra.append(f"☎️ {e(p['storeTelephone'])}")
    if extra:
        lines += [""] + extra
    return "\n".join(lines)


def wa_url(p):
    wa = "".join(ch for ch in (WHATSAPP_NUMBER or p.get("storeWhatsapp") or "") if ch.isdigit())
    if len(wa) == 11 and wa.startswith("8"):
        wa = "7" + wa[1:]
    if not wa:
        return None
    msg = f"Здравствуйте! Интересует {title_of(p)} {memory_of(p)} за {fmt_price(p.get('averagePrice'))}"
    return f"https://wa.me/{wa}?text={urllib.parse.quote(msg)}"


def legacy_fingerprint(p):
    """Отпечаток данных из прежней версии бота: по нему видно, менялся ли товар со старым постом без кнопок."""
    keys = ["modelName", "familyName", "memoryName", "ramName", "colorName", "averagePrice", "oldPrice",
            "description", "batteryState", "year", "storeName", "storeAddress", "storeTelephone", "storeWhatsapp"]
    payload = {k: p.get(k) for k in keys}
    urls = []
    for ph in p.get("photos") or []:
        if ph.get("url") and ph["url"] not in urls:
            urls.append(ph["url"])
    payload["photos"] = urls[:10]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def text_hash(p):
    return hashlib.sha256(f"{desc_text(p)}|{wa_url(p)}".encode()).hexdigest()[:20]


# ---------------------------------------------------------------- отправка в Telegram

def _download(url):
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def send_album(photos, notify):
    """Альбом (или одно фото) без подписи. Возвращает id сообщений по порядку фото."""
    urls = [u for _, u in photos]
    silent = {} if notify else {"disable_notification": "true"}

    def send(upload):
        if len(urls) == 1:
            if upload:
                return [tg("sendPhoto", {"chat_id": TG_CHANNEL, **silent}, {"photo": ("1.jpg", _download(urls[0]))})["message_id"]]
            return [tg("sendPhoto", {"chat_id": TG_CHANNEL, "photo": urls[0], **silent})["message_id"]]
        media, files = [], {}
        for i, u in enumerate(urls):
            item = {"type": "photo", "media": u}
            if upload:
                files[f"f{i}"] = (f"{i}.jpg", _download(u))
                item["media"] = f"attach://f{i}"
            media.append(item)
        res = tg("sendMediaGroup", {"chat_id": TG_CHANNEL, "media": json.dumps(media), **silent}, files or None)
        return [m["message_id"] for m in res]

    try:
        return send(upload=False)
    except TgError as ex:
        if ex.retry_after:
            raise
        log.info("Отправка по ссылке не удалась (%s), загружаем файлы", ex)
        return send(upload=True)


def replace_photo(message_id, url):
    try:
        tg("editMessageMedia", {"chat_id": TG_CHANNEL, "message_id": message_id,
                                "media": json.dumps({"type": "photo", "media": url})})
    except TgError as ex:
        if ex.not_modified:
            return
        if ex.retry_after or ex.gone:
            raise
        tg("editMessageMedia", {"chat_id": TG_CHANNEL, "message_id": message_id,
                                "media": json.dumps({"type": "photo", "media": "attach://f"})},
           {"f": ("1.jpg", _download(url))})


def try_tg(method, data):
    """Правка, которой не страшно «сообщение не найдено / не изменилось». False — правка не удалась."""
    try:
        tg(method, data)
        return True
    except TgError as ex:
        if ex.not_modified or ex.gone:
            return True
        log.warning("%s: %s", method, ex)
        return False


def delete_messages(ids):
    ids = [m for m in ids if m]
    if not ids:
        return True
    try:
        tg("deleteMessages", {"chat_id": TG_CHANNEL, "message_ids": json.dumps(ids)})
        return True
    except TgError as ex:
        log.info("Удалить %s не удалось (%s)", ids, ex)
        return False


# ---------------------------------------------------------------- операции с объявлением

class Sync:
    def __init__(self, state):
        self.state = state

    def publish(self, pid, p, row, notify):
        """Новое объявление (или замена старого). Состояние сохраняется после каждого шага: если что-то
        упадёт, следующий запуск продолжит с того же места и не отправит альбом второй раз."""
        st = self.state
        photos = photo_list(p)
        albums = split_albums(photos)
        plan = [[i for i, _ in a] for a in albums]
        key = "|".join(sorted(i for i, _ in photos))
        if DRY_RUN:
            log.info("[DRY] объявление %s: %d фото в %d альбом(ах)\n%s", pid, len(photos), len(albums), desc_text(p))
            return None

        row = dict(row) if row else {"product_id": pid, "message_ids": [], "photo_ids": None, "desc_message_id": None}
        pend = row.get("pending")
        if not (pend and pend.get("key") == key and pend.get("plan") == plan):
            if pend:  # набор фото поменялся посреди операции — убираем недоотправленное
                delete_messages([m for chunk in pend.get("sent", []) for m in chunk])
            pend = {"key": key, "plan": plan, "sent": []}
        for i, album in enumerate(albums):
            if i < len(pend["sent"]):
                continue
            pend["sent"].append(send_album(album, notify=notify and i == 0))
            row["pending"] = pend
            st.save_post(row)

        with st.lock(pid):
            kb = keyboard(pid, st.counts(pid), wa_url(p))
            d = tg("sendMessage", {"chat_id": TG_CHANNEL, "text": desc_text(p), "parse_mode": "HTML",
                                   "reply_markup": json.dumps(kb), "disable_notification": "true",
                                   "link_preview_options": json.dumps({"is_disabled": True})})["message_id"]
            new_row = {"product_id": pid, "message_ids": [m for chunk in pend["sent"] for m in chunk],
                       "photo_ids": [i for chunk in plan for i in chunk], "desc_message_id": d,
                       "text_hash": text_hash(p), "wa_url": wa_url(p), "pending": None}
            st.save_post(new_row)
            if row.get("message_ids") or row.get("desc_message_id"):
                st.archive_add(pid, row["message_ids"], row.get("desc_message_id"), post_link(TG_CHANNEL, d))
        return new_row

    def replace_in_place(self, pid, p, row):
        """Количество фото то же, часть заменили — меняем снимки в альбоме. False — нужна перепубликация."""
        old, msgs = list(row["photo_ids"]), row["message_ids"]
        new = photo_list(p)
        new_ids = {i for i, _ in new}
        if len(old) != len(new) or len(msgs) != len(old):
            return False
        out_pos = [k for k, x in enumerate(old) if x not in new_ids]
        incoming = [(i, u) for i, u in new if i not in set(old)]
        if len(out_pos) != len(incoming):
            return False
        if DRY_RUN:
            log.info("[DRY] замена %d фото товара %s", len(out_pos), pid)
            return True
        for pos, (nid, url) in zip(out_pos, incoming):
            try:
                replace_photo(msgs[pos], url)
            except TgError as ex:
                log.warning("Не удалось заменить фото товара %s: %s — перепубликуем", pid, ex)
                return False
            old[pos] = nid
            row = {**row, "photo_ids": old}
            self.state.save_post(row)
        log.info("Заменено фото товара %s на месте: %d шт.", pid, len(out_pos))
        return True

    def edit_text(self, pid, p, row):
        """Изменились данные (цена, АКБ, описание…) — правим описание на месте. False — сообщения больше нет."""
        if DRY_RUN:
            log.info("[DRY] правка текста товара %s", pid)
            return True
        st = self.state
        with st.lock(pid):
            kb = keyboard(pid, st.counts(pid), wa_url(p))
            try:
                tg("editMessageText", {"chat_id": TG_CHANNEL, "message_id": row["desc_message_id"],
                                       "text": desc_text(p), "parse_mode": "HTML", "reply_markup": json.dumps(kb),
                                       "link_preview_options": json.dumps({"is_disabled": True})})
            except TgError as ex:
                if ex.gone:
                    return False
                if not ex.not_modified:
                    raise
            st.save_post({**row, "text_hash": text_hash(p), "wa_url": wa_url(p)})
        return True

    def remove_sold(self, pid, row):
        pend_ids = [m for chunk in (row.get("pending") or {}).get("sent", []) for m in chunk]
        if DRY_RUN:
            log.info("[DRY] удаление товара %s", pid)
            return
        if not delete_messages(row["message_ids"] + [row.get("desc_message_id")] + pend_ids):
            if row.get("desc_message_id"):
                try_tg("editMessageText", {"chat_id": TG_CHANNEL, "message_id": row["desc_message_id"],
                                           "text": SOLD_TEXT, "parse_mode": "HTML",
                                           "reply_markup": json.dumps(EMPTY_KEYBOARD)})
            if row["message_ids"]:
                try_tg("editMessageCaption", {"chat_id": TG_CHANNEL, "message_id": row["message_ids"][0],
                                              "caption": SOLD_TEXT, "parse_mode": "HTML"})
            delete_messages(pend_ids)
        self.state.delete_post(pid)

    def process_archive(self):
        """Старые версии объявлений: удалить, а если нельзя — пометить «обновлено» и убрать кнопки."""
        for a in self.state.archive_list():
            if DRY_RUN:
                continue
            if delete_messages(a["message_ids"] + [a["desc_message_id"]]):
                self.state.archive_done(a["id"])
                continue
            link = a["new_url"] or ""
            ok = True
            if a["desc_message_id"]:
                ok &= try_tg("editMessageText", {"chat_id": TG_CHANNEL, "message_id": a["desc_message_id"],
                                                 "text": UPDATED_TEXT.format(link=link), "parse_mode": "HTML",
                                                 "reply_markup": json.dumps(EMPTY_KEYBOARD),
                                                 "link_preview_options": json.dumps({"is_disabled": True})})
                cap = OUTDATED_PHOTOS.format(link=f":\n{link}" if link else "")
            else:  # старый формат: подпись была на первом фото
                cap = UPDATED_TEXT.format(link=link)
            if a["message_ids"]:
                ok &= try_tg("editMessageCaption", {"chat_id": TG_CHANNEL, "message_id": a["message_ids"][0],
                                                    "caption": cap, "parse_mode": "HTML"})
            if ok or a["attempts"] >= 5:
                self.state.archive_done(a["id"])
            else:
                self.state.archive_retry(a["id"])


# ---------------------------------------------------------------- подготовка

def migrate_old_db(state):
    """Один раз переносим записи из старой базы (таблица TABLE_NAME) в общую — как посты старого формата."""
    if not OLD_DB_URL or state.all_posts():
        return
    try:
        import psycopg
        with psycopg.connect(OLD_DB_URL) as old:
            rows = old.execute(f"SELECT product_id, fingerprint, message_ids FROM {TABLE_NAME}").fetchall()
    except Exception as ex:  # noqa: BLE001
        log.warning("Старая база не прочитана: %s", ex)
        return
    for pid, fp, mids in rows:
        state.save_post({"product_id": int(pid), "message_ids": json.loads(mids), "photo_ids": None,
                         "desc_message_id": None, "legacy_fingerprint": fp})
    log.info("Перенесено из старой базы: %d постов", len(rows))


def ensure_webhook():
    if not (WEBHOOK_BASE_URL and WEBHOOK_KEY) or DRY_RUN:
        return
    want = f"{WEBHOOK_BASE_URL}/sfon-tg/{WEBHOOK_KEY}"
    try:
        info = tg("getWebhookInfo")
        if info.get("url") != want:
            tg("setWebhook", {"url": want, "secret_token": secret_for(TG_BOT_TOKEN),
                              "allowed_updates": json.dumps(["callback_query"])})
            log.info("Вебхук кнопок установлен: %s", want)
        elif info.get("last_error_message"):
            log.warning("Вебхук кнопок: последняя ошибка «%s»", info["last_error_message"])
    except TgError as ex:
        log.warning("Вебхук не настроен: %s", ex)


# ---------------------------------------------------------------- основной цикл

def run(state=None):
    if not DRY_RUN:
        missing = [n for n, v in [("SFON_API_KEY", SFON_API_KEY), ("TG_BOT_TOKEN", TG_BOT_TOKEN)] if not v]
        if missing:
            log.error("Не заданы переменные: %s", ", ".join(missing))
            return 2
        if os.environ.get("RENDER") and not STATE_DB_URL:
            log.error("На Render нужна база (NUR_DB_URL / DATABASE_URL), иначе посты будут дублироваться")
            return 2

    state = state or State(TABLE_NAME, url=STATE_DB_URL, sqlite_path=DB_PATH)
    migrate_old_db(state)
    ensure_webhook()
    sync = Sync(state)
    posted = state.all_posts()

    log_company()
    products = sfon_fetch_old_products()
    current, skipped = {}, {}
    for p in products:
        pid = p.get("productId")
        if pid is None or p.get("state", "OLD") != "OLD":
            continue
        miss = missing_fields(p)
        if miss:
            for f in miss:
                skipped[f] = skipped.get(f, 0) + 1
            continue
        current[int(pid)] = p
    if skipped:
        log.info("Пропущены товары с незаполненными полями: %s", skipped)
    log.info("SFON: б/у всего %d, полностью заполнены %d; объявлений в канале: %d", len(products), len(current), len(posted))

    if posted and len(current) < len(posted) * SAFETY_MIN_RATIO and len(posted) >= 10:
        log.error("SFON вернул подозрительно мало товаров (%d при %d объявлениях) — удаление пропущено",
                  len(current), len(posted))
        sold = []
    else:
        sold = [pid for pid in posted if pid not in current]

    stats = dict(sold=0, new=0, republished=0, migrated=0, edited=0, photos_replaced=0, deferred=0, errors=0)

    # 1. Проданные — убираем (в любое время суток)
    for pid in sold:
        try:
            sync.remove_sold(pid, posted[pid])
            stats["sold"] += 1
            log.info("Удалено объявление товара %s (нет в SFON)", pid)
        except Exception as ex:  # noqa: BLE001
            stats["errors"] += 1
            log.error("Товар %s: удаление не удалось: %s", pid, ex)

    # 2. Правки на месте (в любое время) и очередь публикаций
    queue = []  # (приоритет, вид, pid, p, row)
    for pid, p in current.items():
        row = posted.get(pid)
        try:
            if row is None:
                queue.append((3, "new", pid, p, None))
            elif row.get("pending"):
                queue.append((0, "new" if not row["message_ids"] and not row.get("desc_message_id") else "republished",
                              pid, p, row))
            elif not row.get("desc_message_id"):
                # старый пост без кнопок: не трогаем, пока товар не изменился; изменился — перевыкладываем с кнопками
                old_fp = (row.get("legacy_fingerprint") or "").partition(":")[0]
                if old_fp != legacy_fingerprint(p):
                    queue.append((1, "republished", pid, p, row))
                elif MIGRATE_PER_RUN > 0:
                    queue.append((2, "migrated", pid, p, row))
            else:
                old_set, new_set = set(row["photo_ids"] or []), {i for i, _ in photo_list(p)}
                if old_set != new_set:
                    if sync.replace_in_place(pid, p, row):
                        stats["photos_replaced"] += 1
                        row = state.get_post(pid) or row
                    else:
                        queue.append((1, "republished", pid, p, row))
                        continue
                if row.get("text_hash") != text_hash(p):
                    if sync.edit_text(pid, p, row):
                        stats["edited"] += 1
                        log.info("Описание товара %s обновлено на месте", pid)
                    else:
                        queue.append((1, "republished", pid, p, row))
        except Exception as ex:  # noqa: BLE001 — один товар не ломает весь запуск
            stats["errors"] += 1
            log.error("Товар %s: %s", pid, ex)

    # 3. Публикации — днём, с лимитами
    queue.sort(key=lambda q: q[0])
    quiet = is_quiet_now()
    if quiet and queue:
        log.info("Тихие часы (%s): публикации отложены до утра", QUIET_HOURS)
    budget = {"all": MAX_POSTS_PER_RUN, "new": MAX_NEW_PER_RUN, "migrated": MIGRATE_PER_RUN}
    for _, kind, pid, p, row in queue:
        if quiet or budget["all"] <= 0 or budget.get(kind, 1) <= 0:
            stats["deferred"] += 1
            continue
        try:
            sync.publish(pid, p, row, notify=(kind == "new"))
        except Exception as ex:  # noqa: BLE001
            stats["errors"] += 1
            log.error("Товар %s: публикация не удалась (повторим в следующий раз): %s", pid, ex)
            continue
        budget["all"] -= 1
        if kind in budget:
            budget[kind] -= 1
        stats[kind] += 1
        log.info("%s товар %s", {"new": "Опубликован", "republished": "Перепубликован",
                                 "migrated": "Переведён в новый формат"}[kind], pid)
        if not DRY_RUN:
            time.sleep(POST_DELAY_SEC)

    # 4. Старые версии объявлений
    try:
        sync.process_archive()
        state.cleanup_callbacks()
    except Exception as ex:  # noqa: BLE001
        log.error("Архивация: %s", ex)

    log.info("Готово: новых %(new)d, перепубликовано %(republished)d, переведено в новый формат %(migrated)d, "
             "текст исправлен %(edited)d, фото заменено %(photos_replaced)d, удалено %(sold)d, "
             "отложено %(deferred)d, ошибок %(errors)d", stats)
    return 0


if __name__ == "__main__":
    sys.exit(run())
