"""Уведомления владельцу в Телеграм: тревоги о сбоях и ежедневный отчёт.

TELEGRAM_BOT_TOKEN — токен бота от @BotFather.
TELEGRAM_CHAT_ID — куда слать (id группы/канала/чата, можно несколько через запятую).
Пока TELEGRAM_CHAT_ID не задан, бот раз в минуту смотрит, в какие чаты его добавили, и пишет их id в лог.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time

import httpx

log = logging.getLogger(__name__)

# сообщение лога → понятный текст тревоги (None — не тревожить, только посчитать)
ALERTS: list[tuple[re.Pattern, str | None, str]] = [
    (re.compile(r"Ошибка генерации ответа для (\S+)"),
     "⚠️ ИИ не смог ответить клиенту {0} — ушло «Извините… напишите в WhatsApp». Проверьте диалог.", "errors"),
    (re.compile(r"Не удалось отправить сообщение (\S+)"),
     "⚠️ Не удалось отправить ответ клиенту {0} в Instagram.", "send_failed"),
    (re.compile(r"\[(\w+)\] SFON недоступен"),
     "⚠️ Склад SFON ({0}) недоступен — бот отправляет уточнять наличие к менеджеру.", "sfon"),
    (re.compile(r"Зацикливание с (\S+)"),
     "⚠️ Бот слишком часто отвечал {0} — поставлен на паузу на час.", "loops"),
    (re.compile(r"Ошибка планировщика"), "⚠️ Ошибка во фоновой проверке бота (см. логи Render).", "errors"),
    (re.compile(r"не удалось проверить маркер Instagram"),
     "⚠️ Не проверяется доступ к Instagram — возможно, истёк токен.", "errors"),
    (re.compile(r"Негативный комментарий (@\S*) \(([^)]*)\): (.*)"),
     "💬 Негативный комментарий {0} ({1}): «{2}». Бот не отвечал — посмотрите сами.", "negative"),
    (re.compile(r"Не удалось подписаться на комментарии"),
     "⚠️ Бот не смог подписаться на комментарии Instagram — нужно разрешение в настройках приложения Meta.", "errors"),
    (re.compile(r"\] ответили на комментарий"), None, "comments"),
    (re.compile(r"Неотвеченное сообщение в диалоге (\S+)"), None, "recovered"),
    (re.compile(r"Пропущенное сообщение от (\S+)"), None, "recovered"),
    (re.compile(r"Повторное касание для (\S+)"), None, "followups"),
]


class TelegramNotifier:
    def __init__(self, token: str, chat_ids: list[str]):
        self.token = token
        self.chat_ids = chat_ids
        self.http = httpx.AsyncClient(timeout=15)
        self.counters: dict[str, int] = {}
        self._recent: dict[str, float] = {}  # текст тревоги → когда отправлена (не повторяем 30 мин)
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._seen_chats: set[str] = set()
        self._offset = 0

    @classmethod
    def from_env(cls) -> "TelegramNotifier | None":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            return None
        chats = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
        return cls(token, chats)

    @property
    def api(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    # -------------------------------------------------------------- отправка
    def notify(self, text: str, dedupe: bool = True) -> None:
        now = time.monotonic()
        if dedupe:
            last = self._recent.get(text)
            if last and now - last < 1800:
                return
            self._recent[text] = now
        self._queue.put_nowait(text)

    async def _send(self, text: str) -> None:
        for chat in self.chat_ids:
            try:
                r = await self.http.post(f"{self.api}/sendMessage",
                                         json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True})
                if r.status_code >= 400:
                    log.info("Телеграм: не отправлено в %s: %s", chat, r.text[:200])
            except Exception as e:
                log.info("Телеграм: ошибка отправки: %s", e)

    async def run(self) -> None:
        """Фоновая задача: отправляет очередь, а пока чат не задан — ищет чаты, куда добавили бота."""
        last_poll = 0.0
        while True:
            try:
                if self.chat_ids:
                    text = await asyncio.wait_for(self._queue.get(), timeout=60)
                    await self._send(text)
                    await asyncio.sleep(1)
                else:
                    if time.monotonic() - last_poll > 60:
                        last_poll = time.monotonic()
                        await self._discover()
                    await asyncio.sleep(5)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.info("Телеграм: %s", e)
                await asyncio.sleep(30)

    async def _discover(self) -> None:
        r = await self.http.get(f"{self.api}/getUpdates", params={"offset": self._offset, "timeout": 0})
        if r.status_code >= 400:
            log.warning("Телеграм: токен не подходит (%s)", r.status_code)
            return
        for u in (r.json() or {}).get("result", []):
            self._offset = max(self._offset, u.get("update_id", 0) + 1)
            for key in ("message", "channel_post", "my_chat_member", "edited_message"):
                chat = (u.get(key) or {}).get("chat")
                if chat and str(chat.get("id")) not in self._seen_chats:
                    self._seen_chats.add(str(chat.get("id")))
                    title = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
                    log.warning("Телеграм: найден чат «%s» (%s), id=%s — задайте TELEGRAM_CHAT_ID", title,
                                chat.get("type"), chat.get("id"))

    async def aclose(self) -> None:
        await self.http.aclose()


class HideTelegramRequests(logging.Filter):
    """httpx пишет в лог полный адрес запроса, а в нём токен бота — такие строки не пишем."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return "api.telegram.org" not in record.getMessage()
        except Exception:
            return True


class TelegramLogHandler(logging.Handler):
    """Ловит важные строки логов бота и превращает их в тревоги/счётчики."""

    def __init__(self, notifier: TelegramNotifier):
        super().__init__(level=logging.INFO)
        self.n = notifier

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(("app.telegram", "httpx")):
            return
        try:
            msg = record.getMessage()
        except Exception:
            return
        for rx, text, counter in ALERTS:
            m = rx.search(msg)
            if m:
                self.n.counters[counter] = self.n.counters.get(counter, 0) + 1
                if text:
                    self.n.notify(text.format(*(m.groups() or ("",))))
                return
