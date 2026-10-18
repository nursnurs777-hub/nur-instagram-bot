"""Оркестрация: входящие → склейка (debounce) → Claude → отправка; пауза при ответе менеджера; планировщик."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .agent import IncomingImage, SalesAgent
from .config import Settings
from .db import Store, utcnow
from .instagram import Event, InstagramClient
from .sheets import CatalogProvider

log = logging.getLogger(__name__)

def fallback_text(s: Settings) -> str:
    base = "Извините, сейчас не получается ответить автоматически."
    if s.store_whatsapp:
        return f"{base} Напишите, пожалуйста, менеджеру в WhatsApp: {s.store_whatsapp}"
    return f"{base} Менеджер ответит вам здесь в ближайшее время."

OPT_OUT_RX = re.compile(
    r"(не\s+пиш|больше\s+не\s+пиш|не\s+надо\s+пис|отпиш|жазба|жазбаңыз|хабарласпа|"
    r"stop\s+(texting|messaging|writing)|don'?t\s+(text|message|write))", re.I)

ATTACHMENT_NOTES = {
    "image": "[клиент отправил фото]",
    "audio": "[клиент отправил голосовое сообщение — расшифровка недоступна; вежливо попроси написать текстом]",
    "video": "[клиент отправил видео — просмотр недоступен]",
    "share": "[клиент переслал публикацию]",
    "ig_reel": "[клиент переслал Reels]",
    "story_mention": "[клиент упомянул магазин в истории]",
    "file": "[клиент отправил файл]",
}


def _render_user_text(ev: Event) -> str:
    parts = []
    if ev.reply_to_story:
        parts.append("[ответ на историю магазина]")
    for a in ev.attachments:
        parts.append(ATTACHMENT_NOTES.get(a.type, f"[вложение: {a.type}]"))
    if ev.text:
        parts.append(ev.text)
    return " ".join(parts).strip()


class Processor:
    def __init__(self, settings: Settings, store: Store, ig: InstagramClient, agent: SalesAgent,
                 catalogs: CatalogProvider):
        self.s = settings
        self.store = store
        self.ig = ig
        self.agent = agent
        self.catalogs = catalogs
        self._debounce: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending_images: dict[str, list[str]] = {}
        self._bg: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ утилиты
    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    def _lock(self, user_id: str) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    async def _db(self, fn, *a, **kw):
        return await asyncio.to_thread(fn, *a, **kw)

    # ------------------------------------------------------------------ вход
    async def handle_event(self, ev: Event) -> None:
        if self.s.ig_account_id and ev.user_id == self.s.ig_account_id:
            return
        if ev.kind == "echo":
            await self._handle_echo(ev)
        else:
            await self._handle_incoming(ev)

    async def _handle_incoming(self, ev: Event) -> None:
        if self.s.allowed_user_ids and ev.user_id not in self.s.allowed_user_ids:
            log.info("Пропуск %s: не в ALLOWED_USER_IDS", ev.user_id)
            return
        text = _render_user_text(ev)
        if not text:
            return
        await self._db(self.store.get_conv, ev.user_id)
        added = await self._db(self.store.add_message, ev.user_id, "user", text, ev.mid, False)
        if not added:
            return  # повтор вебхука
        now = utcnow()
        fields = {"last_user_at": now, "followup_sent": False}
        if OPT_OUT_RX.search(ev.text or ""):
            fields["opted_out"] = True
        await self._db(self.store.update_conv, ev.user_id, **fields)
        imgs = [a.url for a in ev.attachments if a.type == "image" and a.url]
        if imgs:
            self._pending_images.setdefault(ev.user_id, []).extend(imgs)
        self._schedule(ev.user_id)

    def _schedule(self, user_id: str, delay: float | None = None) -> None:
        old = self._debounce.get(user_id)
        if old and not old.done():
            old.cancel()

        async def later():
            try:
                await asyncio.sleep(self.s.debounce_seconds if delay is None else delay)
            except asyncio.CancelledError:
                return
            await self.process_user(user_id)

        self._debounce[user_id] = self._spawn(later())

    async def _handle_echo(self, ev: Event) -> None:
        role = await self._db(self.store.message_role_by_mid, ev.mid)
        if role in ("assistant", "manager"):
            return
        # Echo может прийти раньше, чем мы сохранили mid своего ответа — даём боту фору
        await asyncio.sleep(self.s.echo_grace_seconds)
        role = await self._db(self.store.message_role_by_mid, ev.mid)
        if role in ("assistant", "manager"):
            return
        now = utcnow()
        await self._db(self.store.get_conv, ev.user_id)
        await self._db(self.store.add_message, ev.user_id, "manager", _render_user_text(ev) or "[вложение]", ev.mid)
        # всё, что клиент написал до ответа менеджера, считаем обработанным менеджером
        await self._db(self.store.mark_all_processed_before, ev.user_id, now)
        await self._db(self.store.update_conv, ev.user_id,
                       paused_until=now + timedelta(minutes=self.s.manager_pause_minutes),
                       last_manager_at=now, followup_sent=True)
        t = self._debounce.get(ev.user_id)
        if t and not t.done():
            t.cancel()
        self._pending_images.pop(ev.user_id, None)
        log.info("Менеджер ответил %s — бот на паузе %s мин", ev.user_id, self.s.manager_pause_minutes)

    # ------------------------------------------------------------------ обработка
    def _blocked(self, conv: dict) -> str | None:
        if not self.s.bot_enabled:
            return "bot disabled"
        if conv["manual_mode"]:
            return "manual mode"
        if conv["paused_until"] and conv["paused_until"] > utcnow():
            return "manager pause"
        return None

    async def process_user(self, user_id: str, followup: bool = False) -> None:
        async with self._lock(user_id):
            conv = await self._db(self.store.get_conv, user_id)
            reason = self._blocked(conv)
            if reason:
                log.info("Не отвечаем %s: %s", user_id, reason)
                return
            pending = await self._db(self.store.unprocessed, user_id)
            if not pending and not followup:
                return
            history = await self._db(self.store.history, user_id, self.s.history_limit)

            images: list[IncomingImage] = []
            for url in self._pending_images.pop(user_id, [])[:4]:
                got = await self.ig.download(url)
                if got:
                    images.append(IncomingImage(*got))

            extra_system = extra_note = None
            if followup:
                extra_system = ("СЛУЖЕБНО: клиент не отвечает около 3 часов после твоего вопроса. Напиши ОДНО короткое "
                                "ненавязчивое напоминание на языке клиента по теме разговора (пример из раздела 19). "
                                "Без нового приветствия, если вы уже здоровались сегодня. Без ссылок и давления.")
                extra_note = "[служебное событие: прошло 3 часа без ответа клиента]"

            await self.ig.typing(user_id, True)
            try:
                catalog = await self.catalogs.get()
                result = await self.agent.reply(history, conv["state"], catalog, images,
                                                extra_system=extra_system, extra_user_note=extra_note)
                text = result.text
                new_state = result.state
            except Exception:
                log.exception("Ошибка генерации ответа для %s", user_id)
                text, new_state = ("" if followup else fallback_text(self.s)), conv["state"]

            if not text:
                await self._db(self.store.mark_processed, [m["id"] for m in pending])
                return

            # пока модель думала, мог ответить менеджер или клиент дописать
            conv = await self._db(self.store.get_conv, user_id)
            if self._blocked(conv):
                log.info("Ответ для %s отменён: вмешался менеджер", user_id)
                return
            if not followup:
                newer = await self._db(self.store.unprocessed, user_id)
                if len(newer) > len(pending):
                    log.info("Клиент %s дописал — перегенерируем", user_id)
                    self._schedule(user_id, 0.5)
                    return

            try:
                mids = await self.ig.send_text(user_id, text)
            except Exception:
                log.exception("Не удалось отправить сообщение %s", user_id)
                return
            now = utcnow()
            for i, mid in enumerate(mids or [None]):
                await self._db(self.store.add_message, user_id, "assistant", text if i == 0 else "", mid, True, now)
            await self._db(self.store.mark_processed, [m["id"] for m in pending])
            fields = {"state": new_state, "last_bot_at": now}
            if followup:
                fields["followup_sent"] = True
            await self._db(self.store.update_conv, user_id, **fields)

    # ------------------------------------------------------------------ планировщик
    async def scheduler(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка планировщика")
            await asyncio.sleep(60)

    async def tick(self) -> None:
        now = utcnow()
        if self.s.resume_after_pause:
            for uid in await self._db(self.store.convs_to_resume, now):
                await self._db(self.store.update_conv, uid, paused_until=None)
                log.info("Пауза менеджера для %s закончилась — отвечаем на ожидающие сообщения", uid)
                self._spawn(self.process_user(uid))
        await self._db(self.store.clear_expired_pauses, now)

        if self.s.followup_enabled:
            local = datetime.now(ZoneInfo(self.s.timezone))
            if self.s.work_hour_start <= local.hour < self.s.work_hour_end:
                uids = await self._db(self.store.convs_for_followup, now,
                                      timedelta(minutes=self.s.followup_after_minutes),
                                      timedelta(hours=self.s.window_hours))
                for uid in uids:
                    hist = await self._db(self.store.history, uid, 6)
                    last = next((h for h in reversed(hist) if h["content"]), None)
                    if not last or last["role"] != "assistant" or "?" not in last["content"][-200:]:
                        await self._db(self.store.update_conv, uid, followup_sent=True)
                        continue
                    self._spawn(self.process_user(uid, followup=True))
