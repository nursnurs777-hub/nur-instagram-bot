import asyncio
import logging
from datetime import timedelta

from app.db import Store, utcnow
from app.telegram import TelegramLogHandler, TelegramNotifier


def test_log_handler_alerts_and_counters():
    n = TelegramNotifier("t", ["1"])
    h = TelegramLogHandler(n)
    lg = logging.getLogger("app.processor.test_tg")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    lg.error("Ошибка генерации ответа для 123")
    lg.error("Ошибка генерации ответа для 123")  # повтор за 30 минут не шлём
    lg.info("Повторное касание для 55")
    lg.warning("Неотвеченное сообщение в диалоге 77 — отвечаем")
    lg.removeHandler(h)
    assert n._queue.qsize() == 1
    assert "Извините" in n._queue.get_nowait()[0]
    assert n.counters == {"errors": 2, "followups": 1, "recovered": 1}


def test_daily_stats_and_report(tmp_path):
    st = Store(f"sqlite:///{tmp_path}/t.db")
    now = utcnow()
    st.get_conv("A1")
    st.add_message("A1", "user", "сколько 18 про", "a", True, now - timedelta(hours=2))
    st.add_message("A1", "assistant", "800 000 ₸", "b", True, now - timedelta(hours=2))
    st.get_conv("shymkent:S1")
    st.add_message("shymkent:S1", "user", "есть 17?", "c", True, now - timedelta(hours=1))
    st.update_conv("shymkent:S1", last_user_at=now - timedelta(hours=1))
    st.update_conv("A1", last_user_at=now - timedelta(hours=2))
    d = st.daily_stats(now, timedelta(hours=24))
    assert d["astana"]["clients"] == 1 and d["astana"]["bot"] == 1
    assert d["shymkent"]["messages"] == 1
    assert [u[0] for u in d["unanswered"]] == ["shymkent:S1"]
    assert d["total_clients"] == 2 and d["answered_clients"] == 1


def test_token_hidden_from_logs():
    import logging as lg
    from app.telegram import HideTelegramRequests
    f = HideTelegramRequests()
    rec = lg.LogRecord("httpx", lg.INFO, "", 0, "GET https://api.telegram.org/bot123:ABC/getUpdates", None, None)
    assert f.filter(rec) is False
    rec2 = lg.LogRecord("httpx", lg.INFO, "", 0, "GET https://graph.instagram.com/v23.0/me", None, None)
    assert f.filter(rec2) is True


def test_report_format_html():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.processor import format_report
    now = datetime(2026, 11, 11, 4, 0)
    st = {"total_clients": 90, "answered_clients": 88,
          "astana": {"clients": 33, "messages": 129, "bot": 125, "manager": 1},
          "shymkent": {"clients": 57, "messages": 347, "bot": 293, "manager": 0},
          "unanswered": [("shymkent:1", "серый <b>", datetime(2026, 11, 10, 9, 44))]}
    t = format_report(st, {"loops": 4, "comments": 1, "negative": 1, "followups": 88, "recovered": 3},
                      ZoneInfo("Asia/Almaty"), now)
    assert "<b>Сбои: 4</b>" in t and "пауза из-за слишком частых ответов: 4" in t
    assert "Шымкент, 10.11 14:44 — «серый &lt;b&gt;»" in t and "10.11 09:00 — 11.11 09:00" in t
    print(t)
