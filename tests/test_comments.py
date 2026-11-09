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
        async def media_caption(mid):
            return "iPhone 18 Pro Max уже в наличии!"
        ig.private_reply, ig.reply_to_comment, ig.media_caption = private_reply, reply_to_comment, media_caption
        from app.comments import CommentEvent
        await proc.handle_comment(CommentEvent("BIZ", "C1", "U9", "kli", "сколько стоит 17 про?", "M1"))
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
        assert any("iPhone 18 Pro Max" in h["content"] for h in proc.store.history("U9", 5))
    asyncio.run(go())



def test_comment_context_added_to_existing_dm_without_duplicates(tmp_path):
    from app.db import Store
    st = Store(f"sqlite:///{tmp_path}/c.db")
    # у клиента уже была переписка в директе
    st.get_conv("IG1")
    st.add_message("IG1", "user", "здравствуйте, есть 17 про?", "old-1", True)
    st.add_message("IG1", "assistant", "Да, есть.", "old-2", True)
    # потом он оставил комментарий, бот ответил в директ (другой ID — из комментария)
    st.get_conv("C1")
    st.add_message("C1", "user", "[комментарий под публикацией] Какая цена?", "c:777", True)
    st.add_message("C1", "assistant", "Здравствуйте! iPhone 18 Pro Max — 800 000 ₸. Какую память смотрите?", "dm-77", True)
    assert st.copy_comment_context("C1", "IG1", "dm-77") is True
    h = st.history("IG1", 10)
    assert [x["role"] for x in h] == ["user", "assistant", "user", "assistant"]
    assert "Какая цена" in h[2]["content"] and "800 000" in h[3]["content"]
    # повторное echo ничего не дублирует
    assert st.copy_comment_context("C1", "IG1", "dm-77") is False
    assert len(st.history("IG1", 10)) == 4
