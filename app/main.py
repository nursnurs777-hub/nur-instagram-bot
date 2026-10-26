"""FastAPI-приложение: вебхук Instagram, служебные эндпоинты, страницы для App Review."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import replace

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .agent import SalesAgent
from .branches import load_branches
from .config import settings
from .db import Store
from .instagram import InstagramClient, parse_events, verify_signature
from .processor import BranchRuntime, Processor
from .sheets import CatalogProvider
from .testchat import router as testchat_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = Store(settings.database_url)
    if store.engine.dialect.name == "sqlite":
        log.warning("База данных: SQLite (временный файл) — история диалогов пропадёт при перезапуске. Задайте DATABASE_URL.")
    else:
        u = store.engine.url
        log.info("База данных: %s, хост %s, база %s", store.engine.dialect.name, u.host, u.database)
    branches = load_branches(settings)
    from anthropic import AsyncAnthropic
    claude = AsyncAnthropic(api_key=settings.anthropic_api_key)
    providers: dict[str, CatalogProvider] = {}  # одна загрузка на таблицу, даже если она общая у филиалов
    runtimes = []
    for b in branches:
        if not b.primary and not b.active:
            continue
        sid = b.spreadsheet_id or settings.spreadsheet_id
        if sid not in providers:
            providers[sid] = CatalogProvider(replace(settings, spreadsheet_id=sid))
        runtimes.append(BranchRuntime(b, InstagramClient(b.access_token, settings.graph_api_version),
                                      SalesAgent(settings, claude, b, branches), providers[sid]))
    primary = runtimes[0]
    proc = Processor(settings, store, primary.ig, primary.agent, primary.catalogs, runtimes)
    app.state.store, app.state.catalogs, app.state.proc = store, primary.catalogs, proc
    log.info("Филиалы: %s", ", ".join(f"{rt.branch.city} ({rt.branch.instagram})" for rt in runtimes))
    await proc.resolve_accounts()
    for prov in providers.values():
        try:
            await prov.get()
        except Exception:
            log.exception("Каталог не загрузился при старте — повторим при первом сообщении")
    sched = asyncio.create_task(proc.scheduler())
    yield
    sched.cancel()
    for rt in runtimes:
        await rt.ig.aclose()


app = FastAPI(title=f"{settings.store_name} AI", lifespan=lifespan)
app.include_router(testchat_router)


@app.get("/health")
async def health():
    return {"ok": True}


# ---------------------------------------------------------------------------- вебхук

@app.get("/webhook")
async def verify(request: Request):
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and settings.ig_verify_token and \
            p.get("hub.verify_token") == settings.ig_verify_token:
        return PlainTextResponse(p.get("hub.challenge", ""))
    raise HTTPException(403, "verification failed")


@app.post("/webhook")
async def webhook(request: Request, x_hub_signature_256: str | None = Header(default=None)):
    body = await request.body()
    if not verify_signature(settings.ig_app_secret, body, x_hub_signature_256):
        log.warning("Неверная подпись вебхука")
        raise HTTPException(401, "bad signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(400, "bad json")
    proc: Processor = request.app.state.proc
    for ev in parse_events(payload):
        proc._spawn(proc.handle_event(ev))  # Meta ждёт быстрый 200 — обрабатываем в фоне
    return {"ok": True}


# ---------------------------------------------------------------------------- админка

def _check_admin(token: str | None):
    if not settings.admin_token or token != settings.admin_token:
        raise HTTPException(401, "unauthorized")


@app.post("/admin/manual/{user_id}")
async def set_manual(user_id: str, request: Request, on: bool = True,
                     x_admin_token: str | None = Header(default=None)):
    """Ручной режим: бот полностью молчит в этом диалоге, пока не выключите (?on=false)."""
    _check_admin(x_admin_token)
    store: Store = request.app.state.store
    await asyncio.to_thread(store.get_conv, user_id)
    await asyncio.to_thread(store.update_conv, user_id, manual_mode=on)
    return {"user_id": user_id, "manual_mode": on}


@app.get("/admin/conversation/{user_id}")
async def get_conversation(user_id: str, request: Request, x_admin_token: str | None = Header(default=None)):
    _check_admin(x_admin_token)
    store: Store = request.app.state.store
    conv = await asyncio.to_thread(store.get_conv, user_id)
    hist = await asyncio.to_thread(store.history, user_id, 50)
    return {"conversation": conv, "messages": hist}


@app.post("/admin/reload-catalog")
async def reload_catalog(request: Request, x_admin_token: str | None = Header(default=None)):
    _check_admin(x_admin_token)
    cat = await request.app.state.catalogs.get(force=True)
    return cat.stats()


@app.get("/admin/stats")
async def stats(request: Request, x_admin_token: str | None = Header(default=None)):
    _check_admin(x_admin_token)
    cat = await request.app.state.catalogs.get()
    return {"db": await asyncio.to_thread(request.app.state.store.stats), "catalog": cat.stats()}


# ---------------------------------------------------------------------------- страницы для Meta App Review

_PAGE = """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title><body style="font-family:system-ui;max-width:720px;margin:40px auto;padding:0 16px;line-height:1.6">
<h1>{title}</h1>{body}</body></html>"""


def _store() -> dict:
    return {"store": settings.store_name, "ig": settings.store_instagram, "addr": settings.store_address,
            "contact": f" в WhatsApp: {settings.store_whatsapp}" if settings.store_whatsapp
            else f" в Instagram {settings.store_instagram}"}


@app.get("/privacy", response_class=HTMLResponse)
async def privacy():
    return _PAGE.format(title="Политика конфиденциальности", body="""
<p>ИИ-ассистент магазина {store} ({ig}, {addr}) отвечает на сообщения, которые пользователи сами отправляют в Instagram Direct магазина.</p>
<p>Мы обрабатываем: идентификатор пользователя Instagram (IGSID), текст сообщений и вложения, отправленные в диалог.
Данные используются только для ответа на вопросы о товарах, ценах и Trade-in и хранятся в защищённой базе данных.
Фотографии не сохраняются: после распознавания хранится только их краткое текстовое описание.</p>
<p>Текст сообщений передаётся поставщику ИИ-модели (Anthropic) исключительно для формирования ответа.
Мы не продаём и не передаём данные третьим лицам для рекламы. Мы не запрашиваем пароли, коды и платёжные данные.</p>
<p>Чтобы удалить историю переписки, напишите в Direct «удалите мои данные» или свяжитесь с нами{contact}.</p>""".format(**_store()))


@app.get("/data-deletion", response_class=HTMLResponse)
async def data_deletion():
    return _PAGE.format(title="Удаление данных", body="""
<p>Чтобы удалить историю переписки с ИИ-ассистентом, напишите в Instagram Direct магазина «удалите мои данные»
или свяжитесь с нами{contact}. Мы удалим данные в течение 30 дней.</p>""".format(**_store()))
