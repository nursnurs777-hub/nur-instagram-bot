"""Instagram API with Instagram Login: проверка подписи вебхука, разбор событий, отправка сообщений."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1000  # лимит Instagram на текст одного сообщения


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Заголовок X-Hub-Signature-256: «sha256=<hex>», HMAC тела запроса секретом приложения."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


@dataclass
class Attachment:
    type: str  # image | audio | video | file | share | story_mention | ...
    url: str | None = None


@dataclass
class Event:
    kind: str  # "message" — от клиента, "echo" — исходящее с аккаунта магазина
    user_id: str  # IGSID клиента
    account_id: str  # ID аккаунта магазина
    mid: str
    text: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    timestamp_ms: int = 0
    reply_to_story: bool = False


def parse_events(payload: dict) -> list[Event]:
    events: list[Event] = []
    if payload.get("object") != "instagram":
        return events
    for entry in payload.get("entry", []):
        account_id = str(entry.get("id", ""))
        for m in entry.get("messaging", []):
            msg = m.get("message")
            if not msg or msg.get("is_deleted") or not msg.get("mid"):
                continue  # прочтения, реакции, удаления и т.п.
            sender = str(m.get("sender", {}).get("id", ""))
            recipient = str(m.get("recipient", {}).get("id", ""))
            is_echo = bool(msg.get("is_echo"))
            atts = [Attachment(type=a.get("type", "unknown"), url=(a.get("payload") or {}).get("url"))
                    for a in msg.get("attachments", []) or []]
            events.append(Event(
                kind="echo" if is_echo else "message",
                user_id=recipient if is_echo else sender,
                account_id=account_id,
                mid=msg["mid"],
                text=msg.get("text", "") or "",
                attachments=atts,
                timestamp_ms=int(m.get("timestamp", 0) or 0),
                reply_to_story=bool((msg.get("reply_to") or {}).get("story")),
            ))
    return events


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Режет длинный ответ по абзацам, затем по предложениям."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    cur = ""
    for para in re.split(r"\n\s*\n", text):
        chunks = [para] if len(para) <= limit else re.split(r"(?<=[.!?])\s+", para)
        for i, ch in enumerate(chunks):
            while len(ch) > limit:  # очень длинное «предложение»
                if cur:
                    parts.append(cur)
                    cur = ""
                parts.append(ch[:limit])
                ch = ch[limit:]
            sep = "\n\n" if i == 0 else " "
            if len(cur) + len(sep) + len(ch) <= limit:
                cur = f"{cur}{sep}{ch}" if cur else ch
            else:
                if cur:
                    parts.append(cur)
                cur = ch
    if cur:
        parts.append(cur)
    return parts


class InstagramClient:
    def __init__(self, access_token: str, api_version: str = "v23.0"):
        self.token = access_token
        self.base = f"https://graph.instagram.com/{api_version}"
        self.http = httpx.AsyncClient(timeout=20)

    async def _post(self, payload: dict) -> dict:
        r = await self.http.post(f"{self.base}/me/messages", json=payload,
                                 headers={"Authorization": f"Bearer {self.token}"})
        if r.status_code >= 400:
            raise RuntimeError(f"Instagram API {r.status_code}: {r.text[:500]}")
        return r.json()

    async def send_text(self, user_id: str, text: str) -> list[str]:
        """Отправляет текст (с разбивкой); возвращает message_id каждой части."""
        mids = []
        for part in split_message(text):
            data = await self._post({"recipient": {"id": user_id}, "message": {"text": part}})
            if data.get("message_id"):
                mids.append(data["message_id"])
        return mids

    async def typing(self, user_id: str, on: bool = True) -> None:
        try:
            await self._post({"recipient": {"id": user_id}, "sender_action": "typing_on" if on else "typing_off"})
        except Exception as e:  # необязательная функция
            log.debug("typing indicator failed: %s", e)

    async def download(self, url: str, max_bytes: int = 5 * 1024 * 1024) -> tuple[bytes, str] | None:
        try:
            r = await self.http.get(url, follow_redirects=True)
            if r.status_code != 200 or len(r.content) > max_bytes:
                return None
            ctype = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            return r.content, ctype
        except Exception as e:
            log.warning("Не удалось скачать вложение: %s", e)
            return None

    async def aclose(self):
        await self.http.aclose()
