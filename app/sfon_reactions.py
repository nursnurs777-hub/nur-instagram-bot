"""Кнопки реакций ❤️ 🔥 👀 под объявлениями б/у телефонов в Telegram-каналах (SFON → Telegram).

Публикует объявления задача по расписанию (sfon-tg-sync/sync.py), а нажатия кнопок Telegram присылает сюда:
POST /sfon-tg/<филиал>. Логика голосования — в sfon-tg-sync/tgcommon.py, общая с задачей.
На работу Instagram-бота не влияет: отдельный адрес, без общих данных.

Переменные окружения:
  SFON_TG_BOTS — JSON {"astana": {"token": "...", "table": "posts", "channel": "@..."}, "shymkent": {...}}
  SFON_TG_DATABASE_URL — база голосов (по умолчанию DATABASE_URL этого сервиса)
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, Body, Header, HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sfon-tg-sync"))
import tgcommon  # noqa: E402

log = logging.getLogger("sfon_reactions")
router = APIRouter()
_ensured: set[str] = set()
_pending_refresh: set[tuple[str, int]] = set()
_lock = threading.Lock()


def _bots() -> dict:
    try:
        return json.loads(os.environ.get("SFON_TG_BOTS") or "{}")
    except ValueError:
        log.error("SFON_TG_BOTS: неверный JSON")
        return {}


def _state(table: str) -> tgcommon.State:
    url = os.environ.get("SFON_TG_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    if url.startswith("postgresql+"):
        url = "postgresql" + url[url.index("://"):]
    pg = url.startswith(("postgres://", "postgresql://"))
    st = tgcommon.State(table, url=url if pg else None,
                        sqlite_path=None if pg else os.environ.get("SFON_TG_SQLITE", "/tmp/sfon_tg.sqlite3"),
                        ensure=table not in _ensured)
    _ensured.add(table)
    return st


def _schedule_refresh(key: str, bot: dict, pid: int, delay: float) -> None:
    """Telegram попросил подождать (много нажатий подряд) — обновим счётчики чуть позже, один раз."""
    with _lock:
        if (key, pid) in _pending_refresh:
            return
        _pending_refresh.add((key, pid))

    def run():
        with _lock:
            _pending_refresh.discard((key, pid))
        st = _state(bot["table"])
        try:
            tgcommon.refresh_keyboard(bot["token"], bot["channel"], st, pid)
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] отложенное обновление кнопок товара %s: %s", key, pid, e)
        finally:
            st.close()

    threading.Timer(float(delay) + 1, run).start()


@router.post("/sfon-tg/{key}")
def telegram_update(key: str, update: dict = Body(...),
                    x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    bot = _bots().get(key)
    if not bot or not x_telegram_bot_api_secret_token or not hmac.compare_digest(
            x_telegram_bot_api_secret_token, tgcommon.secret_for(bot["token"])):
        raise HTTPException(status_code=403)
    cq = update.get("callback_query")
    if not cq:
        return {"ok": True}
    st = _state(bot["table"])
    try:
        result = tgcommon.handle_callback(bot["token"], bot["channel"], st, cq,
                                          schedule_refresh=lambda pid, d: _schedule_refresh(key, bot, pid, d))
        log.info("[%s] реакция %s: %s", key, cq.get("data"), result)
    finally:
        st.close()
    return {"ok": True}
