"""Тестовый чат: тот же бот (промпт, прайс, Trade-in, рассрочка, фото), но без Instagram.

Страница: GET /test. Доступ — по ADMIN_TOKEN (вводится на странице, хранится в браузере).
Диалоги хранятся в той же базе под id «web-test:<сессия>» и никогда не уходят в Instagram.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse

from .agent import IncomingImage
from .config import settings
from .db import utcnow
from .media import describe, shrink
from .tools import state_summary

router = APIRouter()
PREFIX = "web-test:"
PAGE = Path(__file__).with_name("testchat.html")


def _check(token: str | None) -> None:
    if not settings.admin_token or token != settings.admin_token:
        raise HTTPException(401, "Неверный ключ доступа (ADMIN_TOKEN)")


def _runtime(request: Request, branch: str | None):
    proc = request.app.state.proc
    if not branch:
        return proc.primary
    rt = proc.runtimes.get(branch)
    if rt is None:
        raise HTTPException(400, f"Нет филиала {branch}")
    return rt


def _uid(session: str, rt=None) -> str:
    session = re.sub(r"[^A-Za-z0-9_-]", "", session)[:40]
    if not session:
        raise HTTPException(400, "Пустая сессия")
    if rt is not None and not rt.branch.primary:
        return f"{PREFIX}{rt.branch.key}:{session}"
    return PREFIX + session


@router.get("/test", response_class=HTMLResponse)
async def page():
    html = PAGE.read_text(encoding="utf-8")
    return html.replace("{{STORE_NAME}}", settings.store_name).replace("{{STORE_INSTAGRAM}}", settings.store_instagram)


@router.get("/test/api/branches")
async def branches(request: Request, x_admin_token: str | None = Header(default=None)):
    _check(x_admin_token)
    return [{"key": rt.branch.key, "city": rt.branch.city, "instagram": rt.branch.instagram,
             "primary": rt.branch.primary} for rt in request.app.state.proc.runtimes.values()]


@router.post("/test/api/message")
async def message(request: Request, session: str = Form(...), text: str = Form(""),
                  as_manager: bool = Form(False), branch: str = Form(""), files: list[UploadFile] = File(default=[]),
                  x_admin_token: str | None = Header(default=None)):
    _check(x_admin_token)
    rt = _runtime(request, branch)
    uid = _uid(session, rt)
    st = request.app.state
    store = st.store
    text = text.strip()
    files = [f for f in files if f and f.filename][: settings.max_images_per_message]
    if not text and not files:
        raise HTTPException(400, "Пустое сообщение")

    conv = await asyncio.to_thread(store.get_conv, uid)

    if as_manager:  # проверка: как бот учитывает ответ живого менеджера
        await asyncio.to_thread(store.add_message, uid, "manager", text or "[вложение]")
        return {"reply": "", "note": "Сообщение записано как ответ менеджера. В Instagram бот после такого "
                                     f"молчал бы {settings.manager_pause_minutes} мин."}

    # фото: уменьшаем в памяти, описываем текстом, в базу — только текст
    images: list[IncomingImage] = []
    notes: list[str] = []
    limit = settings.max_image_mb * 1024 * 1024
    for f in files:
        data = await f.read(limit + 1)
        await f.close()
        if len(data) > limit:
            notes.append("[фото: файл слишком большой]")
            continue
        small = await asyncio.to_thread(shrink, data)
        del data
        if not small:
            notes.append("[фото: файл не распознан как изображение]")
            continue
        images.append(IncomingImage(*small))
        desc = await describe(rt.agent.client, settings.vision_model, small[0], small[1])
        notes.append(f"[фото: {desc}]" if desc else "[клиент отправил фото — описание недоступно]")

    content = " ".join(notes + ([text] if text else []))
    await asyncio.to_thread(store.add_message, uid, "user", content)
    history = await asyncio.to_thread(store.history, uid, settings.history_limit)

    catalog = await rt.catalogs.get()
    try:
        result = await rt.agent.reply(history, conv["state"], catalog, images)
    except Exception as e:
        raise HTTPException(502, f"Ошибка модели: {e.__class__.__name__}: {str(e)[:300]}")

    if result.text:
        await asyncio.to_thread(store.add_message, uid, "assistant", result.text)
    for m in result.media or []:
        await asyncio.to_thread(store.add_message, uid, "assistant", f"[бот отправил клиенту фото: {m.get('label', '')}]")
    await asyncio.to_thread(store.update_conv, uid, state=result.state)
    return {
        "reply": result.text,
        "tools": result.tool_calls,
        "photo_notes": notes,
        "card": state_summary(result.state),
        "media": [{"label": m.get("label"), "urls": m.get("urls", [])} for m in (result.media or [])],
    }


@router.get("/test/api/history")
async def history(request: Request, session: str, branch: str = "", x_admin_token: str | None = Header(default=None)):
    _check(x_admin_token)
    uid = _uid(session, _runtime(request, branch))
    rows = await asyncio.to_thread(request.app.state.store.history, uid, 100)
    conv = await asyncio.to_thread(request.app.state.store.get_conv, uid)
    return {"messages": [{"role": r["role"], "content": r["content"]} for r in rows if r["content"]],
            "card": state_summary(conv["state"])}


@router.post("/test/api/reset")
async def reset(request: Request, session: str = Form(...), branch: str = Form(""),
                x_admin_token: str | None = Header(default=None)):
    _check(x_admin_token)
    await asyncio.to_thread(request.app.state.store.delete_conversation, _uid(session, _runtime(request, branch)))
    return {"ok": True}


@router.get("/test/api/catalog")
async def catalog_info(request: Request, branch: str = "", x_admin_token: str | None = Header(default=None)):
    _check(x_admin_token)
    cat = await _runtime(request, branch).catalogs.get()
    return {**cat.stats(), "model": settings.anthropic_model, "checked_at": utcnow().isoformat(timespec="seconds")}
