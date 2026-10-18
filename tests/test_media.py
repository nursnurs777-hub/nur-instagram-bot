"""Фото: уменьшение в памяти, описание текстом, в БД — только текст."""
import asyncio
import io
from dataclasses import replace
from types import SimpleNamespace as NS

from PIL import Image

from app.media import mask_sensitive, shrink
from app.instagram import Attachment, Event
from tests.test_flow import FakeAgent, FakeIG, make, settle


def png(w=4000, h=3000):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_shrink_limits_size():
    data, ctype = shrink(png())
    assert ctype == "image/jpeg"
    with Image.open(io.BytesIO(data)) as img:
        assert max(img.size) == 1568
    assert shrink(b"not an image") is None


def test_mask_sensitive():
    s = mask_sensitive("IMEI 356789104523411, карта 4400 4303 8492 4376, аккумулятор 87%")
    assert "356789104523411" not in s and "4376" not in s and "87%" in s


class PhotoIG(FakeIG):
    def __init__(self, payload):
        super().__init__()
        self.payload = payload

    async def download(self, url, max_bytes=None):
        if max_bytes and len(self.payload) > max_bytes:
            return None
        return self.payload, "image/png"


class VisionAgent(FakeAgent):
    def __init__(self):
        super().__init__()
        self.images_seen = []
        self.vision_calls = []

        async def create(**kw):
            self.vision_calls.append(kw)
            return NS(content=[NS(type="text", text="iPhone 15 Pro, серый, трещина на экране. IMEI 356789104523411. "
                                                    "Аккумулятор 82%.")])
        self.client = NS(messages=NS(create=create))

    async def reply(self, history, state, catalog, images=None, **kw):
        self.images_seen.append(images or [])
        return await super().reply(history, state, catalog, images, **kw)


def photo_event(mid, text=""):
    return Event(kind="message", user_id="U1", account_id="BIZ", mid=mid, text=text,
                 attachments=[Attachment(type="image", url="https://cdn.example/x.jpg")])


def test_photo_described_and_only_text_stored(tmp_path):
    async def go():
        proc, _, _ = make(tmp_path)
        proc.ig = PhotoIG(png())
        proc.agent = VisionAgent()
        await proc.handle_event(photo_event("p1", "сколько дадите за такой?"))
        await settle(proc)

        stored = [m for m in proc.store.history("U1", 10) if m["role"] == "user"][0]["content"]
        assert stored.startswith("[фото: iPhone 15 Pro") and "трещина" in stored
        assert "356789104523411" not in stored            # IMEI скрыт
        assert "x.jpg" not in stored                       # ссылка не хранится
        assert "сколько дадите за такой?" in stored
        # модель для ответа получила и описание, и само уменьшенное фото
        assert "[фото: iPhone 15 Pro" in proc.agent.calls[-1][-1]["content"]
        assert len(proc.agent.images_seen[-1]) == 1
        assert proc.agent.images_seen[-1][0].media_type == "image/jpeg"
        assert proc.agent.vision_calls[0]["model"] == proc.s.vision_model
        assert proc._pending_images == {}

        # через сообщение бот помнит фото по тексту, без файла
        await proc.handle_event(Event(kind="message", user_id="U1", account_id="BIZ", mid="p2", text="а с коробкой?"))
        await settle(proc)
        hist = proc.agent.calls[-1]
        assert any("[фото: iPhone 15 Pro" in h["content"] for h in hist)
        assert proc.agent.images_seen[-1] == []
    asyncio.run(go())


def test_too_large_photo_skipped(tmp_path):
    async def go():
        proc, _, _ = make(tmp_path, max_image_mb=1)
        proc.ig = PhotoIG(b"x" * (2 * 1024 * 1024))
        proc.agent = VisionAgent()
        await proc.handle_event(photo_event("p1"))
        await settle(proc)
        stored = proc.store.history("U1", 5)[0]["content"]
        assert "слишком большой" in stored
        assert proc.agent.vision_calls == []
    asyncio.run(go())
