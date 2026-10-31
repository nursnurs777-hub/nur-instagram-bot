"""Наличие из учётной системы SFON (Open API, только чтение).

У каждого филиала свой ключ: SMARTPHONE_SYSTEM_API_KEY_<KEY> (например, _ASTANA). Без ключа инструменты
наличия филиалу не выдаются, и он работает как раньше. Наличие одного филиала другому не показывается.

Бот держит снимок склада (новые + б/у) и обновляет его не чаще раза в STOCK_CACHE_SECONDS.
Новые: строка = модель + цвет + память + RAM + магазин, count — штук. Цены новых бот берёт из Google-таблицы.
Б/у: строка = конкретный экземпляр (productId) со своей ценой, аккумулятором, годом, описанием и фото.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field

import httpx

from .catalog import SIM_LABELS, format_price, norm, normalize_memory, normalize_sim
from .media import mask_sensitive

log = logging.getLogger(__name__)

API = "https://open-api.sfon.kz/open-api"
MAX_PAGES = 30
PAGE_SIZE = 100
MAX_PHOTOS = 4

AUTH_ERRORS = {"API_KEY_MISSING", "API_KEY_INVALID", "API_KEY_DISABLED", "API_KEY_EXPIRED", "COMPANY_INACTIVE"}

_SIM_WORDS_RX = re.compile(r"(?<![a-z0-9])(?:[12] ?)?(?:e ?sim|sim)(?![a-z])|(?<![a-z])(?:dual|nano|только|only|физ\w*)(?![a-z])")


_SIM_ORIG_RX = re.compile(r"\s*\(?\b(?:dual\s*sim|[12]\s*sim|e-?\s?sim|sim)(?:\s*\+\s*e-?sim)?\b\)?", re.I)

# цвета в SFON — по-английски («ORANGE», «SAGE»); клиенты пишут по-русски
COLOR_RU = {
    "черн": "black", "чёрн": "black", "бел": "white", "син": "blue", "голуб": "blue", "оранж": "orange",
    "сереб": "silver", "сер": "gray", "зелен": "green", "зелён": "green", "шалфе": "sage", "роз": "pink",
    "фиол": "purple", "лаванд": "lavender", "золот": "gold", "натурал": "natural", "пустын": "desert",
    "титан": "titanium", "красн": "red", "желт": "yellow", "жёлт": "yellow", "бордо": "burgundy", "бургун": "burgundy",
    "графит": "graphite", "косм": "cosmic", "небесн": "sky", "туман": "mist", "ледян": "glacier",
}


def color_key(c: str | None) -> str:
    s = norm(c)
    for ru, en in COLOR_RU.items():
        s = re.sub(rf"\b{ru}\w*", en, s)
    return s


def pretty_model(name: str | None) -> str:
    """«17 Pro Max e-Sim» → «iPhone 17 Pro Max»."""
    s = re.sub(r"\s+", " ", _SIM_ORIG_RX.sub(" ", name or "")).strip()
    return ("iPhone " + s) if re.match(r"^\d", s) else s


def base_model(name: str | None) -> str:
    """«iPhone 17 Pro eSIM» → «iphone 17 pro» (SIM-пометки из названия убираются)."""
    s = _SIM_WORDS_RX.sub(" ", norm(name))
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class StockItem:
    state: str                      # NEW | OLD
    model: str                      # как в SFON (с пометкой SIM)
    base: str                       # нормализованная модель без SIM
    sim: str | None
    memory: str | None
    ram: str | None
    color: str | None
    count: int = 1
    price: float | None = None      # для б/у — цена экземпляра
    battery: int | None = None
    year: int | None = None
    description: str = ""
    product_id: int | None = None
    photos: list[str] = field(default_factory=list)
    store: str = ""
    search_text: str = ""

    def label(self) -> str:
        bits = [pretty_model(self.model)]
        for x in (self.memory, self.color):
            if x:
                bits.append(x)
        return ", ".join(bits)


def _item(row: dict) -> StockItem:
    model = (row.get("modelName") or "").strip()
    mem = normalize_memory(row.get("memoryName") or row.get("memoryCode"))
    color = (row.get("colorName") or "").strip() or None
    sim = normalize_sim(model) if "sim" in norm(model) or "dual" in norm(model) else None
    photos = [p.get("url") for p in row.get("photos") or [] if p.get("url")]
    it = StockItem(
        state=row.get("state") or "NEW", model=model, base=base_model(model), sim=sim, memory=mem,
        ram=(row.get("ramName") or None), color=color, count=int(row.get("count") or 1),
        price=row.get("averagePrice"), battery=row.get("batteryState"), year=row.get("year"),
        description=mask_sensitive((row.get("description") or "").strip())[:300],
        product_id=row.get("productId"), photos=photos, store=row.get("storeName") or "",
    )
    it.search_text = norm(" ".join(x for x in (row.get("productTypeName"), row.get("familyName"), model, mem,
                                              color) if x))
    return it


class SfonClient:
    def __init__(self, api_key: str, timeout: float = 20):
        self.key = api_key
        self.http = httpx.AsyncClient(timeout=timeout, headers={"X-API-Key": api_key})

    async def _req(self, method: str, path: str, **kw) -> dict:
        r = await self.http.request(method, API + path, **kw)
        if r.status_code >= 400:
            try:
                err = r.json()
            except ValueError:
                err = {"message": r.text[:200]}
            raise SfonError(r.status_code, err.get("code") or "", err.get("message") or "")
        return r.json()

    async def company(self) -> dict:
        return await self._req("GET", "/v1/company")

    async def search(self, flt: dict, page: int = 0, size: int = PAGE_SIZE) -> dict:
        return await self._req("POST", "/v1/products/search", params={"page": page, "size": size}, json=flt)

    async def old_product(self, product_id: int) -> dict:
        return await self._req("GET", f"/v1/products/old/{product_id}")

    async def aclose(self):
        await self.http.aclose()


class SfonError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"SFON {status} {code}: {message}")
        self.status, self.code = status, code

    @property
    def auth(self) -> bool:
        return self.code in AUTH_ERRORS or self.status == 401


class StockService:
    """Снимок склада одного филиала."""

    def __init__(self, branch_key: str, client: SfonClient, store_id: int | None = None,
                 cache_seconds: int = 120, whatsapp: str = ""):
        self.branch_key = branch_key
        self.client = client
        self.store_id = store_id
        self.cache_seconds = cache_seconds
        self.whatsapp = whatsapp
        self.new: list[StockItem] = []
        self.old: list[StockItem] = []
        self.loaded_at = 0.0
        self.error: str | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls, branch_key: str, whatsapp: str = "") -> "StockService | None":
        up = branch_key.upper()
        key = os.getenv(f"SMARTPHONE_SYSTEM_API_KEY_{up}", "").strip()
        if not key:
            return None
        sid = os.getenv(f"SMARTPHONE_SYSTEM_STORE_ID_{up}", "").strip()
        cache = int(os.getenv("STOCK_CACHE_SECONDS", "120") or 120)
        return cls(branch_key, SfonClient(key), int(sid) if sid.isdigit() else None, cache, whatsapp)

    # ---------------------------------------------------------------- загрузка
    async def _fetch_all(self, state: str) -> list[StockItem]:
        flt: dict = {"state": state}
        if self.store_id:
            flt["storeId"] = self.store_id
        out: list[StockItem] = []
        for page in range(MAX_PAGES):
            data = await self.client.search(flt, page)
            out += [_item(r) for r in data.get("content") or []]
            if data.get("last", True):
                break
        return out

    async def refresh(self, force: bool = False) -> None:
        if not force and self.loaded_at and time.monotonic() - self.loaded_at < self.cache_seconds:
            return
        async with self._lock:
            if not force and self.loaded_at and time.monotonic() - self.loaded_at < self.cache_seconds:
                return
            try:
                new, old = await asyncio.gather(self._fetch_all("NEW"), self._fetch_all("OLD"))
                self.new, self.old, self.loaded_at, self.error = new, old, time.monotonic(), None
                log.info("[%s] SFON: новых групп %s (%s шт.), б/у %s", self.branch_key, len(new),
                         sum(i.count for i in new), len(old))
            except SfonError as e:
                self.error = e.code or str(e)
                log.error("[%s] SFON недоступен: %s%s", self.branch_key, e,
                          " — проверьте ключ SMARTPHONE_SYSTEM_API_KEY (срок действия?)" if e.auth else "")
                self.loaded_at = time.monotonic() - self.cache_seconds + 60  # повторим через минуту
            except Exception as e:
                self.error = e.__class__.__name__
                log.exception("[%s] SFON: ошибка загрузки наличия", self.branch_key)
                self.loaded_at = time.monotonic() - self.cache_seconds + 60

    async def startup_report(self) -> None:
        """Пишет в лог магазины и пример данных — чтобы проверить, как названы модели."""
        try:
            c = await self.client.company()
            stores = [(s.get("id"), s.get("storeName"), s.get("address")) for s in c.get("stores") or []]
            log.info("[%s] SFON компания: %s; магазины: %s", self.branch_key,
                     (c.get("companyDto") or {}).get("companyName"), stores)
            if len(stores) > 1 and not self.store_id:
                log.warning("[%s] В SFON несколько магазинов — задайте SMARTPHONE_SYSTEM_STORE_ID_%s, "
                            "чтобы показывать наличие только своего", self.branch_key, self.branch_key.upper())
        except Exception as e:
            log.error("[%s] SFON /company: %s", self.branch_key, e)
        await self.refresh(force=True)
        for it in self.new[:15]:
            log.info("[%s] SFON новый: %r | память=%s | цвет=%s | sim=%s | шт=%s | фото=%s", self.branch_key,
                     it.model, it.memory, it.color, it.sim, it.count, len(it.photos))
        for it in self.old[:10]:
            log.info("[%s] SFON б/у #%s: %r | %s | %s | акб=%s%% | %s | %s ₸ | фото=%s | %r", self.branch_key,
                     it.product_id, it.model, it.memory, it.color, it.battery, it.year, it.price, len(it.photos),
                     it.description[:80])

    # ---------------------------------------------------------------- поиск
    @staticmethod
    def _match_model(items: list[StockItem], model: str) -> list[StockItem]:
        key = base_model(model)
        exact = [i for i in items if i.base == key or i.base == "iphone " + key or "iphone " + i.base == key]
        if exact:
            return exact
        q = key.split()
        nums = {t for t in q if any(c.isdigit() for c in t)}
        out = []
        for i in items:
            words = set(i.search_text.split()) | set(i.base.split())
            if nums and not nums <= words:
                continue
            if all(t in words for t in q):
                out.append(i)
        return out

    @staticmethod
    def _filter(items, memory=None, color=None, sim=None):
        mem = normalize_memory(memory) if memory else None
        if mem:
            items = [i for i in items if i.memory == mem]
        sk = normalize_sim(sim) if sim else None
        if sk:
            items = [i for i in items if i.sim in (sk, None)]
        if color:
            c = color_key(color)
            words = set(c.split())
            items = [i for i in items if i.color and (
                c in color_key(i.color) or color_key(i.color) in c or words & set(color_key(i.color).split()))]
        return items

    def _variant(self, i: StockItem) -> dict:
        d = {"model": pretty_model(i.model), "memory": i.memory, "color": i.color, "count": i.count}
        if i.sim:
            d["sim"] = SIM_LABELS.get(i.sim, i.sim)
        if i.count == 1:
            d["last_one"] = True
        return d

    def _wa(self) -> str:
        return self.whatsapp or "WhatsApp менеджера"

    async def check_new(self, model: str, memory: str | None = None, color: str | None = None,
                        sim: str | None = None) -> dict:
        await self.refresh()
        if self.error and not self.new:
            return {"status": "unavailable",
                    "note": f"Наличие сейчас не проверить. Скажи, что наличие уточнит менеджер: {self._wa()}"}
        same = self._match_model(self.new, model)
        if not same:
            return {"status": "out_of_stock", "in_stock": False,
                    "note": (f"Этой модели сейчас нет в наличии. Сразу направь клиента в WhatsApp для уточнения "
                             f"наличия и поступления: {self._wa()}. Цену из прайса можно назвать, но не обещай наличие.")}
        exact = self._filter(same, memory, color, sim)
        if exact:
            last = all(i.count == 1 for i in exact) and len(exact) == 1
            res = {"status": "in_stock", "in_stock": True, "variants": [self._variant(i) for i in exact[:8]],
                   "note": "Есть в наличии. Цену нового бери только из прайса (get_exact_price)."}
            if last:
                res["note"] += (f" Это последняя штука: скажи об этом и попроси перед приездом забронировать у "
                                f"менеджера в WhatsApp: {self._wa()}")
            return res
        alts = sorted(same, key=lambda i: (i.memory or "", i.color or ""))
        return {"status": "no_such_config", "in_stock": False,
                "alternatives": [self._variant(i) for i in alts[:10]],
                "note": ("Такой конфигурации нет, но есть эта модель в других вариантах (alternatives). Предложи "
                         "1–2 ближайших и спроси: «Вам важен именно этот цвет/объём памяти?». Если клиенту нужен "
                         f"только его вариант — направь в WhatsApp: {self._wa()}, менеджер может подобрать модель "
                         "со склада. Цены новых — из прайса.")}

    async def search_used(self, model: str | None = None, memory: str | None = None,
                          battery_from: int | None = None, max_price: float | None = None) -> dict:
        await self.refresh()
        if self.error and not self.old:
            return {"status": "unavailable",
                    "note": f"Наличие б/у сейчас не проверить — направь к менеджеру: {self._wa()}"}
        items = self._match_model(self.old, model) if model else list(self.old)
        items = self._filter(items, memory)
        if battery_from:
            items = [i for i in items if (i.battery or 0) >= battery_from]
        if max_price:
            items = [i for i in items if i.price is not None and i.price <= max_price]
        if not items:
            return {"status": "none", "found": 0,
                    "note": f"Подходящих б/у сейчас нет. Предложи новую модель из прайса или уточнение у менеджера: {self._wa()}"}
        items.sort(key=lambda i: (i.price is None, i.price or 0))
        units = []
        for i in items[:6]:
            u = {"product_id": i.product_id, "model": pretty_model(i.model), "memory": i.memory, "color": i.color,
                 "battery_percent": i.battery, "year": i.year, "photos": len(i.photos),
                 "price_kzt": i.price, "price_text": format_price(int(i.price)) if i.price else "цена не указана"}
            if i.sim:
                u["sim"] = SIM_LABELS.get(i.sim, i.sim)
            if i.description:
                u["description"] = i.description
            units.append(u)
        return {"status": "ok", "found": len(items), "units": units,
                "note": ("Б/у из наличия магазина, цена — цена экземпляра. Состояние описывай ТОЛЬКО по description; "
                         "не придумывай царапины и дефекты. Если у экземпляра есть фото — предложи: «Скинуть фото?» "
                         "и отправляй (send_used_photos) только после согласия. Если фото нет или по описанию/фото "
                         f"нельзя ответить про состояние — направь к менеджеру в WhatsApp: {self._wa()}.")}

    async def used_unit(self, product_id: int) -> StockItem | None:
        await self.refresh()
        for i in self.old:
            if i.product_id == product_id:
                return i
        try:
            data = await self.client.old_product(product_id)
            info = data.get("productInfo")
            return _item(info) if info else None
        except Exception as e:
            log.warning("[%s] SFON товар %s: %s", self.branch_key, product_id, e)
            return None

    async def aclose(self):
        await self.client.aclose()
