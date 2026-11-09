import asyncio

from app.comments import classify, parse_comment_events, public_reply
from tests.test_flow import make, settle


def test_classify():
    assert classify("🔥🔥🔥") == "skip"
    assert classify("@friend1 @friend2") == "skip"
    assert classify("Класс!") == "skip"
    assert classify("Аз ғана ол қанша? 800000тг ма?") == "answer"
    assert classify("сколько стоит 17 про макс") == "answer"
    assert classify("Обманщики, не советую") == "negative"
    assert classify("заходите на http://spam.ru") == "skip"


def test_public_reply_language():
    assert "директ" in public_reply("сколько стоит?").lower()
    assert "директке" in public_reply("бағасы қанша?").lower()


def test_parse_comment_events():
    p = {"object": "instagram", "entry": [{"id": "BIZ", "changes": [{"field": "comments", "value": {
        "id": "C1", "text": "цена?", "from": {"id": "U9", "username": "kli"}, "media": {"id": "M1"}}}]}]}
    ev = parse_comment_events(p)[0]
    assert (ev.account_id, ev.comment_id, ev.from_id, ev.text) == ("BIZ", "C1", "U9", "цена?")


def test_handle_comment_flow(tmp_path):
    async def go():
        proc, ig, agent = make(tmp_path)
        ig.private, ig.public = [], []

        async def private_reply(cid, text):
            ig.private.append((cid, text)); return "dm-1"

        async def reply_to_comment(cid, text):
            ig.public.append((cid, text)); return "r-1"
        ig.private_reply, ig.reply_to_comment = private_reply, reply_to_comment
        from app.comments import CommentEvent
        await proc.handle_comment(CommentEvent("BIZ", "C1", "U9", "kli", "сколько стоит 17 про?"))
        await proc.handle_comment(CommentEvent("BIZ", "C1", "U9", "kli", "сколько стоит 17 про?"))  # повтор
        await proc.handle_comment(CommentEvent("BIZ", "C2", "U9", "kli", "🔥🔥"))
        await settle(proc)
        assert len(ig.private) == 1 and ig.private[0][0] == "C1"
        assert len(ig.public) == 1 and "директ" in ig.public[0][1].lower()
        # echo личного ответа пришёл под другим ID директа — контекст переносится
        from app.instagram import Event
        await proc.handle_event(Event(kind="echo", user_id="IGSID9", account_id="BIZ", mid="dm-1", text="..."))
        await settle(proc)
        hist = proc.store.history("IGSID9", 5)
        assert any("комментарий" in h["content"] for h in hist)
    asyncio.run(go())
