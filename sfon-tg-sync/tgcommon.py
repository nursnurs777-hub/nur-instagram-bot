"""
Общая часть для синхронизации (sync.py, задачи по расписанию) и для приёма нажатий кнопок
(вебхук внутри сервиса nur-instagram-bot): Telegram API, кнопки, база состояния и голосов.

Таблицы (base = имя филиала, например «posts» или «posts_shymkent»):
  sfon_<base>          — активное объявление товара: альбом, сообщение-описание с кнопками, набор фото
  sfon_<base>_votes    — голоса: товар + пользователь Telegram → реакция (одна на пользователя)
  sfon_<base>_cb       — обработанные callback_query (защита от повторной доставки)
  sfon_<base>_archive  — старые объявления, которые ещё надо удалить или пометить «обновлено»
"""

import contextlib
import hashlib
import json
import logging
import sqlite3
import time

import requests

log = logging.getLogger("sfon.tg")

REACTIONS = [("h", "❤️"), ("f", "🔥"), ("e", "👀")]
REACTION_EMOJI = dict(REACTIONS)


# ---------------------------------------------------------------- Telegram

class TgError(Exception):
    def __init__(self, method, description, retry_after=None):
        super().__init__(f"{method}: {description}")
        self.description = description or ""
        self.retry_after = retry_after

    @property
    def not_modified(self):
        return "not modified" in self.description

    @property
    def gone(self):
        d = self.description.lower()
        return "not found" in d or "message_id_invalid" in d


def tg_call(token, method, data=None, files=None, wait=True, attempts=6):
    """Вызов Bot API. wait=False — не ждать при лимите (для вебхука), а сразу поднять TgError с retry_after."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    for attempt in range(attempts):
        try:
            r = requests.post(url, data=data, files=files, timeout=90)
            res = r.json()
        except (requests.RequestException, ValueError) as ex:
            if not wait or attempt == attempts - 1:
                raise TgError(method, f"сеть: {ex}") from ex
            time.sleep(3 * (attempt + 1))
            continue
        if res.get("ok"):
            return res["result"]
        retry = (res.get("parameters") or {}).get("retry_after")
        if retry and wait and attempt < attempts - 1:
            log.info("Лимит Telegram (%s), ждём %s сек", method, retry)
            time.sleep(retry + 1)
            continue
        raise TgError(method, res.get("description"), retry)
    raise TgError(method, "не удалось после повторов")


def secret_for(token):
    """Секрет вебхука выводится из токена: и задача, и вебхук знают токен, отдельно хранить нечего."""
    return hashlib.sha256(f"sfon-webhook:{token}".encode()).hexdigest()[:48]


def keyboard(product_id, counts, wa_url):
    rows = [[{"text": f"{emoji} {counts.get(code, 0)}", "callback_data": f"v:{product_id}:{code}"}
             for code, emoji in REACTIONS]]
    if wa_url:
        rows.append([{"text": "💬 Написать в WhatsApp", "url": wa_url}])
    return {"inline_keyboard": rows}


EMPTY_KEYBOARD = {"inline_keyboard": []}


def post_link(channel, message_id):
    ch = str(channel)
    if ch.startswith("@"):
        return f"https://t.me/{ch[1:]}/{message_id}"
    if ch.startswith("-100"):
        return f"https://t.me/c/{ch[4:]}/{message_id}"
    return ""


# ---------------------------------------------------------------- база состояния

class State:
    """Состояние одного филиала. Postgres (url) или SQLite (путь к файлу) — для тестов."""

    def __init__(self, base, url=None, sqlite_path=None, ensure=True):
        if not base.replace("_", "").isalnum():
            raise ValueError("имя таблицы: только латиница, цифры и _")
        self.t = f"sfon_{base}"
        if url:
            import psycopg
            self.conn = psycopg.connect(url, autocommit=True)
            self.pg = True
        else:
            self.conn = sqlite3.connect(sqlite_path or ":memory:", check_same_thread=False)
            self.conn.isolation_level = None
            self.pg = False
        self._in_tx = False
        if ensure:
            self.ensure_schema()

    def close(self):
        with contextlib.suppress(Exception):
            self.conn.close()

    def x(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql.replace("?", "%s") if self.pg else sql, params)
        return cur

    def ensure_schema(self):
        t = self.t
        self.x(f"CREATE TABLE IF NOT EXISTS {t} ("
               " product_id BIGINT PRIMARY KEY,"
               " message_ids TEXT NOT NULL,"          # альбом(ы): id сообщений по порядку фото
               " photo_ids TEXT,"                     # нормализованные id фото в том же порядке
               " desc_message_id BIGINT,"             # описание с кнопками (NULL — старый формат без кнопок)
               " text_hash TEXT,"
               " wa_url TEXT,"
               " pending TEXT,"                       # незавершённая перепубликация
               " legacy_fingerprint TEXT,"
               " updated_at BIGINT NOT NULL)")
        self.x(f"CREATE TABLE IF NOT EXISTS {t}_votes ("
               " product_id BIGINT NOT NULL, user_id BIGINT NOT NULL, reaction TEXT NOT NULL,"
               " updated_at BIGINT NOT NULL, PRIMARY KEY (product_id, user_id))")
        self.x(f"CREATE TABLE IF NOT EXISTS {t}_cb (callback_id TEXT PRIMARY KEY, created_at BIGINT NOT NULL)")
        self.x(f"CREATE TABLE IF NOT EXISTS {t}_archive ("
               " id TEXT PRIMARY KEY, product_id BIGINT NOT NULL, message_ids TEXT NOT NULL,"
               " desc_message_id BIGINT, new_url TEXT, attempts INT NOT NULL, created_at BIGINT NOT NULL)")

    # --- блокировка товара: голосование и синхронизация не мешают друг другу
    @contextlib.contextmanager
    def lock(self, product_id):
        if self._in_tx:
            yield
            return
        self._in_tx = True
        try:
            if self.pg:
                with self.conn.transaction():
                    self.x("SELECT pg_advisory_xact_lock(hashtext(?))", (f"{self.t}:{product_id}",))
                    yield
            else:
                self.x("BEGIN IMMEDIATE")
                try:
                    yield
                    self.x("COMMIT")
                except BaseException:
                    self.x("ROLLBACK")
                    raise
        finally:
            self._in_tx = False

    # --- объявления
    _cols = "product_id, message_ids, photo_ids, desc_message_id, text_hash, wa_url, pending, legacy_fingerprint"

    def _row(self, r):
        return {
            "product_id": int(r[0]), "message_ids": json.loads(r[1] or "[]"),
            "photo_ids": json.loads(r[2]) if r[2] else None, "desc_message_id": r[3],
            "text_hash": r[4], "wa_url": r[5], "pending": json.loads(r[6]) if r[6] else None,
            "legacy_fingerprint": r[7],
        }

    def all_posts(self):
        return {int(r[0]): self._row(r) for r in self.x(f"SELECT {self._cols} FROM {self.t}").fetchall()}

    def get_post(self, product_id):
        r = self.x(f"SELECT {self._cols} FROM {self.t} WHERE product_id = ?", (product_id,)).fetchone()
        return self._row(r) if r else None

    def save_post(self, row):
        vals = (row["product_id"], json.dumps(row.get("message_ids") or []),
                json.dumps(row["photo_ids"]) if row.get("photo_ids") is not None else None,
                row.get("desc_message_id"), row.get("text_hash"), row.get("wa_url"),
                json.dumps(row["pending"]) if row.get("pending") else None,
                row.get("legacy_fingerprint"), int(time.time()))
        self.x(f"INSERT INTO {self.t} ({self._cols}, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
               "ON CONFLICT (product_id) DO UPDATE SET message_ids = excluded.message_ids, "
               "photo_ids = excluded.photo_ids, desc_message_id = excluded.desc_message_id, "
               "text_hash = excluded.text_hash, wa_url = excluded.wa_url, pending = excluded.pending, "
               "legacy_fingerprint = excluded.legacy_fingerprint, updated_at = excluded.updated_at", vals)

    def delete_post(self, product_id):
        self.x(f"DELETE FROM {self.t} WHERE product_id = ?", (product_id,))

    # --- голоса
    def counts(self, product_id):
        rows = self.x(f"SELECT reaction, COUNT(*) FROM {self.t}_votes WHERE product_id = ? GROUP BY reaction",
                      (product_id,)).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def vote(self, product_id, user_id, reaction):
        """Одна реакция на пользователя: та же — снимается, другая — заменяет. Возвращает added/removed/changed."""
        cur = self.x(f"SELECT reaction FROM {self.t}_votes WHERE product_id = ? AND user_id = ?",
                     (product_id, user_id)).fetchone()
        if cur and cur[0] == reaction:
            self.x(f"DELETE FROM {self.t}_votes WHERE product_id = ? AND user_id = ?", (product_id, user_id))
            return "removed"
        self.x(f"INSERT INTO {self.t}_votes (product_id, user_id, reaction, updated_at) VALUES (?, ?, ?, ?) "
               "ON CONFLICT (product_id, user_id) DO UPDATE SET reaction = excluded.reaction, "
               "updated_at = excluded.updated_at", (product_id, user_id, reaction, int(time.time())))
        return "changed" if cur else "added"

    def first_time_callback(self, callback_id):
        cur = self.x(f"INSERT INTO {self.t}_cb (callback_id, created_at) VALUES (?, ?) "
                     "ON CONFLICT (callback_id) DO NOTHING", (str(callback_id), int(time.time())))
        return cur.rowcount == 1

    def cleanup_callbacks(self, days=7):
        self.x(f"DELETE FROM {self.t}_cb WHERE created_at < ?", (int(time.time()) - days * 86400,))

    # --- очередь архивации старых объявлений
    def archive_add(self, product_id, message_ids, desc_message_id, new_url):
        self.x(f"INSERT INTO {self.t}_archive (id, product_id, message_ids, desc_message_id, new_url, attempts,"
               " created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
               (f"{product_id}-{time.time_ns()}", product_id, json.dumps(message_ids), desc_message_id, new_url,
                int(time.time())))

    def archive_list(self):
        rows = self.x(f"SELECT id, product_id, message_ids, desc_message_id, new_url, attempts FROM {self.t}_archive"
                      " ORDER BY created_at").fetchall()
        return [{"id": r[0], "product_id": int(r[1]), "message_ids": json.loads(r[2]), "desc_message_id": r[3],
                 "new_url": r[4], "attempts": int(r[5])} for r in rows]

    def archive_done(self, aid):
        self.x(f"DELETE FROM {self.t}_archive WHERE id = ?", (aid,))

    def archive_retry(self, aid):
        self.x(f"UPDATE {self.t}_archive SET attempts = attempts + 1 WHERE id = ?", (aid,))


# ---------------------------------------------------------------- обработка нажатий

ANSWERS = {"added": "{e} Спасибо!", "changed": "{e} Голос изменён", "removed": "Голос снят"}


def handle_callback(token, channel, state, cq, schedule_refresh=None):
    """Обработка нажатия реакции. Идемпотентно: повторная доставка того же callback ничего не меняет."""
    cb_id = cq.get("id")
    data = cq.get("data") or ""
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    user_id = (cq.get("from") or {}).get("id")

    def answer(text=None):
        with contextlib.suppress(TgError):
            tg_call(token, "answerCallbackQuery", {"callback_query_id": cb_id, **({"text": text} if text else {})},
                    wait=False, attempts=1)

    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "v" or parts[2] not in REACTION_EMOJI or not parts[1].isdigit() or not user_id:
        answer()
        return "ignored"
    pid, code = int(parts[1]), parts[2]
    ch = str(channel)
    same_chat = (ch.startswith("@") and (chat.get("username") or "").lower() == ch[1:].lower()) or str(chat.get("id")) == ch

    with state.lock(pid):
        if not state.first_time_callback(cb_id):
            result = "duplicate"
        else:
            row = state.get_post(pid)
            if not same_chat or not row or not row.get("desc_message_id") or row["desc_message_id"] != msg.get("message_id"):
                result = "stale"
            else:
                result = state.vote(pid, user_id, code)
                kb = keyboard(pid, state.counts(pid), row.get("wa_url"))
                try:
                    tg_call(token, "editMessageReplyMarkup",
                            {"chat_id": channel, "message_id": row["desc_message_id"], "reply_markup": json.dumps(kb)},
                            wait=False, attempts=1)
                except TgError as ex:
                    if ex.retry_after and schedule_refresh:
                        schedule_refresh(pid, ex.retry_after)
                    elif not ex.not_modified:
                        log.warning("Кнопки товара %s не обновились: %s", pid, ex)

    if result == "stale":
        answer("Это объявление уже неактуально")
        if msg.get("message_id"):  # убираем кнопки со старого сообщения, чтобы по нему больше не голосовали
            with contextlib.suppress(TgError):
                tg_call(token, "editMessageReplyMarkup", {"chat_id": chat.get("id") or channel,
                        "message_id": msg["message_id"], "reply_markup": json.dumps(EMPTY_KEYBOARD)},
                        wait=False, attempts=1)
    elif result in ANSWERS:
        answer(ANSWERS[result].format(e=REACTION_EMOJI[code]))
    else:
        answer()
    return result


def refresh_keyboard(token, channel, state, pid):
    with state.lock(pid):
        row = state.get_post(pid)
        if not row or not row.get("desc_message_id"):
            return
        kb = keyboard(pid, state.counts(pid), row.get("wa_url"))
        try:
            tg_call(token, "editMessageReplyMarkup",
                    {"chat_id": channel, "message_id": row["desc_message_id"], "reply_markup": json.dumps(kb)})
        except TgError as ex:
            if not ex.not_modified:
                log.warning("Кнопки товара %s не обновились: %s", pid, ex)
