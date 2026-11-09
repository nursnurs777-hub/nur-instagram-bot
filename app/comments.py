"""Комментарии под постами Instagram: короткий ответ под комментарием + подробный ответ в директ.

Схема:
- эмодзи, похвала, отметки друзей, ссылки — не отвечаем;
- негатив — не отвечаем публично, присылаем тревогу владельцу (Телеграм);
- вопрос/интерес — публично «Ответили вам в директ 📩» и личное сообщение по сути (один раз на комментарий).
"""
from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class CommentEvent:
    account_id: str
    comment_id: str
    from_id: str
    username: str
    text: str
    media_id: str = ""
    parent_id: str = ""


def parse_comment_events(payload: dict) -> list[CommentEvent]:
    out: list[CommentEvent] = []
    if payload.get("object") != "instagram":
        return out
    for entry in payload.get("entry", []):
        account_id = str(entry.get("id", ""))
        for ch in entry.get("changes", []) or []:
            if ch.get("field") not in ("comments", "live_comments"):
                continue
            v = ch.get("value") or {}
            if not v.get("id"):
                continue
            frm = v.get("from") or {}
            out.append(CommentEvent(
                account_id=account_id, comment_id=str(v["id"]), from_id=str(frm.get("id", "")),
                username=frm.get("username", "") or "", text=(v.get("text") or "").strip(),
                media_id=str((v.get("media") or {}).get("id", "")), parent_id=str(v.get("parent_id") or ""),
            ))
    return out


KZ_LETTERS = re.compile(r"[әіңғүұқөһ]", re.I)
NEGATIVE_RX = re.compile(
    r"(обман|развод|мошен|кидал|кинули|ужас|отврат|не советую|жалоб|верните деньги|брак|подделк|"
    r"алдады|алдап|алаяқ|өтірік|сапасыз|ақшамды қайтар)", re.I)
INTEREST_RX = re.compile(
    r"(\?|цен|сколько|скока|почем|стоим|наличи|есть|рассроч|кредит|trade|трейд|обмен|доставк|адрес|где вы|"
    r"куплю|купить|хочу|интерес|прайс|скидк|қанша|бағасы|бар ма|барма|бөліп|несие|ауыстыр|алғым|қайда|"
    r"\d{2,3}\s*(pro|про|max|макс)|айфон|iphone|airpods|ipad|macbook|apple watch|dyson)", re.I)
PRAISE_RX = re.compile(r"^(класс|супер|круто|топ|огонь|красиво|вау|wow|nice|бомба|керемет|тамаша|әдемі|"
                       r"ура|спасибо|рахмет|рақмет|молодцы|жарайсыңдар|👍|❤️|🔥)[\s!.)]*$", re.I)

PUBLIC_RU = ["Ответили вам в директ 📩", "Написали вам в директ 📩", "Подробности отправили в директ 📩"]
PUBLIC_KZ = ["Директке жауап жаздық 📩", "Толығырақ директке жібердік 📩", "Директке жаздық 📩"]


def classify(text: str) -> str:
    """'skip' | 'negative' | 'answer'."""
    t = (text or "").strip()
    if not t:
        return "skip"
    if NEGATIVE_RX.search(t):
        return "negative"
    letters = re.sub(r"[^a-zA-Zа-яА-ЯёЁәіңғүұқөһӘІҢҒҮҰҚӨҺ0-9]", "", re.sub(r"@\S+", "", t))
    if not letters:
        return "skip"  # только эмодзи или только отметки друзей (розыгрыш)
    if "http" in t.lower() or "www." in t.lower():
        return "skip"
    if PRAISE_RX.match(t):
        return "skip"
    return "answer" if INTEREST_RX.search(t) else "skip"


def public_reply(text: str) -> str:
    return random.choice(PUBLIC_KZ if KZ_LETTERS.search(text or "") else PUBLIC_RU)


COMMENT_SYSTEM = (
    "СЛУЖЕБНО: клиент оставил КОММЕНТАРИЙ под публикацией магазина в Instagram, а ты пишешь ему ПЕРВОЕ личное "
    "сообщение в директ. Коротко поздоровайся, скажи, что пишешь по его комментарию, и сразу ответь по сути "
    "(цены и наличие — только через инструменты). Если в комментарии не назван товар, смотри подпись к "
    "публикации — комментарий почти всегда про товар из поста; назови его цену. Если и из подписи непонятно, "
    "какой товар, — спроси одним вопросом. Закончи одним лёгким вопросом, чтобы клиент ответил. 1–3 предложения, язык — как в комментарии.")
