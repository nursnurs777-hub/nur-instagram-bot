"""Оркестрация: входящие → склейка (debounce) → Claude → отправка; пауза при ответе менеджера; планировщик."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dataclasses import dataclass, field

from .agent import IncomingImage, SalesAgent
from .branches import Branch, primary_from_settings
from .config import Settings
from .db import Store, utcnow
from .instagram import Event, InstagramClient
from .media import describe, shrink
from .sheets import CatalogProvider

log = logging.getLogger(__name__)

def fallback_text(b) -> str:
    """b — Branch (или Settings, для совместимости)."""
    base = "Извините, сейчас не получается ответить автоматически."
    wa = getattr(b, "whatsapp", None) or getattr(b, "store_whatsapp", "")
    if wa:
        return f"{base} Напишите, пожалуйста, менеджеру в WhatsApp: {wa}"
    return f"{base} Менеджер ответит вам здесь в ближайшее время."


@dataclass
class BranchRuntime:
    """Всё, что нужно для ответа в Instagram конкретного филиала."""
    branch: Branch
    ig: InstagramClient
    agent: SalesAgent
    catalogs: CatalogProvider
    account_ids: set[str] = field(default_factory=set)  # ID аккаунта в вебхуках (из настроек или от Instagram)

    def __post_init__(self):
        if self.branch.account_id:
            self.account_ids.add(self.branch.account_id)

    async def resolve_account_ids(self) -> None:
        """Спрашивает у Instagram ID аккаунта по маркеру (на случай, если в настройках его нет или он другой)."""
        me = getattr(self.ig, "me", None)
        if not self.branch.active or me is None:
            return
        try:
            info = await me()
        except Exception as e:
            log.warning("Филиал %s: не удалось проверить маркер Instagram: %s", self.branch.key, e)
            return
        ids = {str(info.get(k)) for k in ("user_id", "id") if info.get(k)}
        self.account_ids |= ids
        if not self.branch.instagram and info.get("username"):
            self.branch.instagram = "@" + info["username"]
        log.info("Филиал %s (%s): Instagram @%s, ID %s", self.branch.key, self.branch.city,
                 info.get("username", "?"), ", ".join(sorted(ids)))

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
                 catalogs: CatalogProvider, runtimes: list[BranchRuntime] | None = None):
        self.s = settings
        self.store = store
        if not runtimes:
            branch = getattr(agent, "branch", None) or primary_from_settings(settings)
            runtimes = [BranchRuntime(branch, ig, agent, catalogs)]
        self.runtimes: dict[str, BranchRuntime] = {rt.branch.key: rt for rt in runtimes}
        self.primary = next((rt for rt in runtimes if rt.branch.primary), runtimes[0])
        self._unknown_accounts: set[str] = set()
        self._debounce: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # фото ждут обработки: (id сообщения в БД, ссылка Instagram). Ссылки живут только в памяти.
        self._pending_images: dict[str, list[tuple[int, str]]] = {}
        self._img_sem = asyncio.Semaphore(max(1, settings.image_concurrency))
        self._bg: set[asyncio.Task] = set()

    # основной филиал — для совместимости со старым кодом и тестами
    @property
    def ig(self):
        return self.primary.ig

    @ig.setter
    def ig(self, value):
        self.primary.ig = value

    @property
    def agent(self):
        return self.primary.agent

    @agent.setter
    def agent(self, value):
        self.primary.agent = value

    @property
    def catalogs(self):
        return self.primary.catalogs

    # ------------------------------------------------------------------ филиалы
    async def resolve_accounts(self) -> None:
        for rt in self.runtimes.values():
            await rt.resolve_account_ids()
        for rt in self.runtimes.values():  # Instagram филиала мог стать известен только сейчас
            if hasattr(rt.agent, "rebuild_prompt"):
                rt.agent.rebuild_prompt()

    def _match(self, account_id: str) -> BranchRuntime | None:
        return next((rt for rt in self.runtimes.values() if account_id in rt.account_ids), None)

    async def runtime_for_account(self, account_id: str) -> BranchRuntime:
        rt = self._match(account_id)
        if rt is None and account_id and any(r.branch.active and not r.account_ids for r in self.runtimes.values()):
            await self.resolve_accounts()  # маркер добавили, а ID ещё не знаем — спросим у Instagram
            rt = self._match(account_id)
        if rt is None:
            if len(self.runtimes) > 1 and account_id not in self._unknown_accounts:
                self._unknown_accounts.add(account_id)
                log.warning("Сообщение для аккаунта %s, который не привязан ни к одному филиалу — отвечаем как "
                            "основной филиал (%s). Проверьте маркеры IG_ACCESS_TOKEN_*.", account_id,
                            self.primary.branch.city)
            rt = self.primary
        return rt

    def split_key(self, key: str) -> tuple[BranchRuntime, str]:
        """Ключ диалога → (филиал, IGSID клиента)."""
        prefix, sep, rest = key.partition(":")
        if sep and prefix in self.runtimes and not self.runtimes[prefix].branch.primary:
            return self.runtimes[prefix], rest
        return self.primary, key

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
        rt = await self.runtime_for_account(ev.account_id)
        if ev.user_id in rt.account_ids or (self.s.ig_account_id and ev.user_id == self.s.ig_account_id):
            return
        key = rt.branch.conv_key(ev.user_id)
        log.info("[%s] %s от %s", rt.branch.key, "ответ с аккаунта" if ev.kind == "echo" else "сообщение", ev.user_id)
        if ev.kind == "echo":
            await self._handle_echo(ev, key)
        else:
            await self._handle_incoming(ev, key)

    async def _handle_incoming(self, ev: Event, key: str | None = None) -> None:
        key = key or ev.user_id
        if self.s.allowed_user_ids and ev.user_id not in self.s.allowed_user_ids:
            log.info("Пропуск %s: не в ALLOWED_USER_IDS", key)
            return
        text = _render_user_text(ev)
        if not text:
            return
        await self._db(self.store.get_conv, key)
        added = await self._db(self.store.add_message, key, "user", text, ev.mid, False)
        if not added:
            return  # повтор вебхука
        now = utcnow()
        fields = {"last_user_at": now, "followup_sent": False}
        if OPT_OUT_RX.search(ev.text or ""):
            fields["opted_out"] = True
        await self._db(self.store.update_conv, key, **fields)
        imgs = [a.url for a in ev.attachments if a.type == "image" and a.url]
        if imgs:
            queue = self._pending_images.setdefault(key, [])
            for url in imgs:
                if len(queue) < self.s.max_images_per_message:
                    queue.append((added, url))
        self._schedule(key)

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

    async def _handle_echo(self, ev: Event, key: str | None = None) -> None:
        key = key or ev.user_id
        role = await self._db(self.store.message_role_by_mid, ev.mid)
        if role in ("assistant", "manager"):
            return
        # Echo может прийти раньше, чем мы сохранили mid своего ответа — даём боту фору
        await asyncio.sleep(self.s.echo_grace_seconds)
        role = await self._db(self.store.message_role_by_mid, ev.mid)
        if role in ("assistant", "manager"):
            return
        now = utcnow()
        await self._db(self.store.get_conv, key)
        await self._db(self.store.add_message, key, "manager", _render_user_text(ev) or "[вложение]", ev.mid)
        # всё, что клиент написал до ответа менеджера, считаем обработанным менеджером
        await self._db(self.store.mark_all_processed_before, key, now)
        await self._db(self.store.update_conv, key,
                       paused_until=now + timedelta(minutes=self.s.manager_pause_minutes),
                       last_manager_at=now, followup_sent=True)
        t = self._debounce.get(key)
        if t and not t.done():
            t.cancel()
        self._pending_images.pop(key, None)
        log.info("Менеджер ответил %s — бот на паузе %s мин", key, self.s.manager_pause_minutes)

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
        if user_id.startswith("web-test:"):
            return  # тестовые диалоги со страницы /test в Instagram не отправляются
        rt, igsid = self.split_key(user_id)
        async with self._lock(user_id):
            conv = await self._db(self.store.get_conv, user_id)
            reason = self._blocked(conv)
            if reason:
                log.info("Не отвечаем %s: %s", user_id, reason)
                return
            pending = await self._db(self.store.unprocessed, user_id)
            if not pending and not followup:
                return
            images = await self._prepare_images(user_id)  # до чтения истории: описания фото попадут в неё
            history = await self._db(self.store.history, user_id, self.s.history_limit)

            extra_system = extra_note = None
            if followup:
                extra_system = ("СЛУЖЕБНО: клиент давно не отвечает на твой вопрос. Напиши ОДНО короткое "
                                "ненавязчивое напоминание на языке клиента по теме разговора (пример из раздела 19). "
                                "Без нового приветствия, если вы уже здоровались сегодня; если переписка была вчера, "
                                "можно коротко поздороваться. Без ссылок и давления.")
                extra_note = "[служебное событие: клиент не ответил на вопрос, это единственное напоминание]"

            await rt.ig.typing(igsid, True)
            try:
                catalog = await rt.catalogs.get()
                result = await rt.agent.reply(history, conv["state"], catalog, images,
                                                extra_system=extra_system, extra_user_note=extra_note)
                text = result.text
                new_state = result.state
            except Exception:
                log.exception("Ошибка генерации ответа для %s", user_id)
                text, new_state = ("" if followup else fallback_text(rt.branch)), conv["state"]

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
                mids = await rt.ig.send_text(igsid, text)
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

    # ------------------------------------------------------------------ фото
    async def _prepare_images(self, user_id: str) -> list[IncomingImage]:
        """Скачивает фото в память, уменьшает, описывает текстом и сохраняет в БД только текст.

        Возвращает уменьшенные картинки для текущего ответа; после ответа они просто выбрасываются.
        """
        out: list[IncomingImage] = []
        rt, _ = self.split_key(user_id)
        for msg_id, url in self._pending_images.pop(user_id, []):
            async with self._img_sem:  # не больше N фото одновременно на весь сервер
                note = await self._process_one_image(url, out, rt)
            await self._db(self._replace_photo_note, msg_id, note)
        return out

    async def _process_one_image(self, url: str, out: list[IncomingImage], rt: BranchRuntime | None = None) -> str:
        rt = rt or self.primary
        got = await rt.ig.download(url, max_bytes=self.s.max_image_mb * 1024 * 1024)
        if not got:
            return "[фото: не удалось загрузить или файл слишком большой]"
        small = await asyncio.to_thread(shrink, got[0])
        del got  # исходный файл больше не нужен
        if not small:
            return "[фото: файл не распознан как изображение]"
        out.append(IncomingImage(*small))
        text = await describe(rt.agent.client, self.s.vision_model, small[0], small[1])
        return f"[фото: {text}]" if text else "[клиент отправил фото — описание недоступно]"

    def _replace_photo_note(self, msg_id: int, note: str) -> None:
        rows = [m for m in self.store.history_by_ids([msg_id])]
        if not rows:
            return
        content = rows[0]["content"]
        placeholder = ATTACHMENT_NOTES["image"]
        content = content.replace(placeholder, note, 1) if placeholder in content else f"{note} {content}"
        self.store.update_message_content(msg_id, content)

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
            # Ночью не пишем; кто «созрел» ночью, получает напоминание утром, если 23-часовое окно ещё открыто
            if self.s.followup_hour_start <= local.hour < self.s.followup_hour_end:
                uids = await self._db(self.store.convs_for_followup, now,
                                      timedelta(minutes=self.s.followup_after_minutes),
                                      timedelta(hours=self.s.window_hours))
                for uid in uids:
                    hist = await self._db(self.store.history, uid, 6)
                    last = next((h for h in reversed(hist) if h["content"]), None)
                    if not last or last["role"] != "assistant" or "?" not in last["content"][-200:]:
                        await self._db(self.store.update_conv, uid, followup_sent=True)
                        continue
                    log.info("Повторное касание для %s", uid)
                    self._spawn(self.process_user(uid, followup=True))
