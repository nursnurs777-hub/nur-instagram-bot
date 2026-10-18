"""Фото от клиентов: уменьшение в памяти и текстовое описание.

Файлы никогда не пишутся на диск. Скачанные байты живут только во время обработки сообщения,
в базу сохраняется лишь короткое текстовое описание.
"""
from __future__ import annotations

import base64
import io
import logging
import re

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

MAX_SIDE = 1568            # больше Claude всё равно уменьшит; меньше — меньше памяти и токенов
JPEG_QUALITY = 85
Image.MAX_IMAGE_PIXELS = 40_000_000  # защита от «бомб» из огромных картинок

DESCRIBE_PROMPT = """\
Ты помогаешь продавцу магазина электроники. Клиент прислал это фото в Instagram Direct.
Опиши его по-русски, кратко (до 400 символов), фактами, без догадок:
- что на фото (устройство, коробка, скриншот, документ, другое);
- модель и цвет, если их можно уверенно определить (иначе «модель не определить по фото»);
- видимое состояние: царапины, трещины, сколы, вмятины;
- значимый текст на экране или коробке (например, «Состояние аккумулятора: 87%», объём памяти).
Никогда не переписывай IMEI, серийные номера, номера карт, телефоны, ИИН, данные документов — \
пиши «[скрыто]». Если на фото нет ничего про технику, так и скажи одной фразой."""

_SENSITIVE = [
    (re.compile(r"\b\d{15,17}\b"), "[скрыто]"),                       # IMEI
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[скрыто]"),              # номера карт
    (re.compile(r"\b\d{12}\b"), "[скрыто]"),                          # ИИН
]


def shrink(data: bytes) -> tuple[bytes, str] | None:
    """Уменьшает фото до MAX_SIDE по большей стороне и перекодирует в JPEG. None — если это не картинка."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            return out.getvalue(), "image/jpeg"
    except Exception as e:
        log.warning("Не удалось обработать изображение: %s", e)
        return None


def mask_sensitive(text: str) -> str:
    for rx, repl in _SENSITIVE:
        text = rx.sub(repl, text)
    return text


async def describe(client, model: str, data: bytes, media_type: str, max_tokens: int = 300) -> str | None:
    """Короткое текстовое описание фото для истории диалога."""
    try:
        resp = await client.messages.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                             "data": base64.b64encode(data).decode()}},
                {"type": "text", "text": DESCRIBE_PROMPT},
            ]}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        text = re.sub(r"\s+", " ", text)[:600]
        return mask_sensitive(text) or None
    except Exception:
        log.exception("Не удалось описать фото")
        return None
