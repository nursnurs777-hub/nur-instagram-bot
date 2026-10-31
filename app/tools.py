"""Инструменты, которые вызывает модель. Все цены и расчёты — только здесь, в коде."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from .catalog import Catalog, SIM_LABELS, format_price, norm, normalize_memory, normalize_sim
from .tradein import Condition, installment, quote, topup

log = logging.getLogger(__name__)

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "search_products",
        "description": (
            "Поиск по актуальному розничному прайсу магазина (новые устройства). Используй, когда клиент "
            "спрашивает о товаре или цене без точной комплектации, или чтобы узнать, какие варианты есть. "
            "Запрос можно писать как клиент: «айфон 18 про макс», «макбук эйр 13», «PS5», «airwrap». "
            "Пустой результат НЕ означает, что товара нет на складе — только что его нет в прайсе."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Название товара, можно с памятью/SIM/цветом"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_exact_price",
        "description": (
            "Точная цена конкретной конфигурации из прайса. Если память или SIM не указаны и вариантов "
            "несколько — вернёт все варианты, чтобы ты уточнил у клиента. Не подставляй цену другой памяти или SIM."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Модель, например «iPhone 18 Pro Max», «MacBook Air 13” M5»"},
                "memory": {"type": "string", "description": "Память или конфигурация: «256GB», «1TB», «16/512GB»"},
                "sim": {"type": "string", "enum": ["esim", "sim+esim", "dual"],
                        "description": "esim — только eSIM; sim+esim — физическая SIM + eSIM; dual — две физические SIM (Dual SIM)"},
                "color": {"type": "string"},
            },
            "required": ["model"],
        },
    },
    {
        "name": "quote_iphone_tradein",
        "description": (
            "Предварительная оценка СДАВАЕМОГО клиентом iPhone по таблице выкупа и утверждённым вычетам. "
            "Вызывай, как только известны модель, память и процент аккумулятора — про SIM, коробку, царапины, "
            "ремонт и повреждения НЕ спрашивай. Передавай эти поля, только если клиент сам о них сказал; "
            "неуказанные считаются лучшим случаем (коробка есть, без царапин и повреждений, без ремонта, всё работает). "
            "Результат сохраняется в карточке диалога; повторно вызывай, если клиент изменил или добавил данные "
            "о старом телефоне. Samsung и другие бренды не принимаются."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Например «iPhone 15 Pro»"},
                "memory": {"type": "string", "description": "«128GB», «256GB», «1TB»"},
                "sim": {"type": "string", "enum": ["esim", "sim+esim", "dual"],
                        "description": "Только если клиент сам указал. Не спрашивай: без SIM инструмент вернёт цену для каждого варианта"},
                "battery_percent": {"type": "integer", "minimum": 0, "maximum": 100},
                "has_box": {"type": "boolean"},
                "minor_scratches": {"type": "boolean"},
                "was_repaired": {"type": "boolean", "description": "Был ремонт или вскрытие"},
                "screen_or_body_damage": {"type": "boolean", "description": "Существенные повреждения экрана/корпуса"},
                "all_functions_work": {"type": "boolean"},
            },
            "required": ["model", "memory", "battery_percent"],
        },
    },
    {
        "name": "calculate_tradein_topup",
        "description": (
            "Доплата при обмене: цена НОВОГО iPhone из прайса минус СОХРАНЁННАЯ оценка старого. "
            "Данные старого телефона заново не нужны — они берутся из карточки диалога. "
            "Вызывай после quote_iphone_tradein, когда известны модель, память и SIM нового телефона."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "new_model": {"type": "string"},
                "new_memory": {"type": "string"},
                "new_sim": {"type": "string", "enum": ["esim", "sim+esim", "dual"]},
                "new_color": {"type": "string", "description": "Цвет, если цена в прайсе зависит от цвета"},
                "used_product_id": {"type": "integer",
                                    "description": "Если клиент берёт Б/У телефон из наличия: product_id из search_used"},
            },
            "required": [],
        },
    },
    {
        "name": "calculate_kaspi_installment",
        "description": (
            "Рассрочка Kaspi (обычно 0-0-24): к цене за наличные добавляется наценка, итог делится на месяцы. "
            "basis='product' — для товара из прайса (нужны model и, если вариантов несколько, memory/sim). "
            "basis='tradein_topup' — для последней рассчитанной доплаты по Trade-in. "
            "Цены в прайсе — за наличные; эту сумму и платёж в месяц называй только из ответа инструмента."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "basis": {"type": "string", "enum": ["product", "tradein_topup", "used"]},
                "product_id": {"type": "integer", "description": "Для basis='used': product_id б/у из search_used"},
                "model": {"type": "string"},
                "memory": {"type": "string"},
                "sim": {"type": "string", "enum": ["esim", "sim+esim", "dual"]},
                "color": {"type": "string"},
                "months": {"type": "integer", "description": "По умолчанию 24"},
            },
            "required": ["basis"],
        },
    },
]


STOCK_TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "check_stock",
        "description": (
            "Наличие НОВОГО (запечатанного) товара на складе этого филиала по учётной системе. Вызывай, когда клиент "
            "спрашивает «есть?», хочет приехать или купить, и перед тем как сказать «есть в наличии». "
            "Возвращает: in_stock, варианты с количеством, last_one (последняя штука) или ближайшие альтернативы. "
            "Цены новых — НЕ отсюда, а из прайса (get_exact_price)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Модель, например «iPhone 17 Pro Max»"},
                "memory": {"type": "string"},
                "color": {"type": "string"},
                "sim": {"type": "string", "enum": ["esim", "sim+esim", "dual"]},
            },
            "required": ["model"],
        },
    },
    {
        "name": "search_used",
        "description": (
            "Б/у устройства в наличии этого филиала: каждый экземпляр со своей ценой, аккумулятором, годом, "
            "описанием и количеством фото. Вызывай, когда клиент спрашивает б/у / бывший в употреблении телефон. "
            "Цена б/у — отсюда."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Модель; можно не указывать, чтобы увидеть всё"},
                "memory": {"type": "string"},
                "battery_from": {"type": "integer", "description": "Минимальный процент аккумулятора"},
                "max_price": {"type": "number", "description": "Бюджет клиента, ₸"},
            },
        },
    },
    {
        "name": "send_used_photos",
        "description": (
            "Отправить клиенту в Direct фото конкретного б/у экземпляра (до 4). Вызывай ТОЛЬКО после того, как "
            "клиент согласился посмотреть фото. Фото уйдут сразу после твоего текстового ответа."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "integer", "description": "product_id из search_used"}},
            "required": ["product_id"],
        },
    },
]


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


# Новые iPhone 13–16 Pro / Pro Max не продаём: сняты с производства. В Trade-in принимаем как обычно.
_DISCONTINUED_RX = re.compile(r"(?<![0-9])(13|14|15|16) ?pro( ?max)?(?![a-z])")
_OTHER_DEVICES = ("macbook", "ipad", "watch", "airpods", "samsung", "galaxy")


def discontinued_iphone(text: str | None) -> str | None:
    """«айфон 15 про макс» → «iPhone 15 Pro Max»; для остальных запросов — None."""
    s = norm(text)
    if not s or any(d in s for d in _OTHER_DEVICES):
        return None
    m = _DISCONTINUED_RX.search(s)
    if not m:
        return None
    return f"iPhone {m.group(1)} Pro" + (" Max" if m.group(2) else "")


# Ожидаемые модели: пока в прайсе нет цены, бот называет дату ориентировочно и отправляет к менеджеру.
# Когда в таблице появится цена, правило перестаёт срабатывать само.
UPCOMING_MODELS = {
    "iPhone Duo": {"aliases": ("duo", "дуо"), "eta": "примерно 25 октября"},
}


def upcoming_model(text: str | None) -> str | None:
    words = set(norm(text).split())
    if words & {"macbook", "ipad", "watch", "airpods", "samsung", "galaxy", "surface"}:
        return None
    for label, cfg in UPCOMING_MODELS.items():
        if words & set(cfg["aliases"]):
            return label
    return None


def _upcoming_unpriced(label: str, items) -> bool:
    """True, если в прайсе нет ни одной позиции этой модели с ценой."""
    key = norm(label)
    return not any(p.price is not None and norm(p.model) == key for p in items)


def _upcoming_info(label: str) -> dict:
    eta = UPCOMING_MODELS[label]["eta"]
    return {
        "status": "expected",
        "expected_model": label,
        "expected_arrival": eta,
        "note": (f"{label}: поступление ожидается {eta} — называй дату только как ориентировочную. "
                 "Цену и возможность предзаказа клиент уточняет у менеджера в WhatsApp. "
                 "Не обещай наличие, не называй и не придумывай цену, не считай доплату и рассрочку."),
    }


def _discontinued_result(label: str) -> dict:
    return {
        "found": 0,
        "status": "discontinued",
        "model": label,
        "reply_to_client": (f"{label} официально снят с производства компанией Apple, и мы эту модель не продаём. "
                            "Хотите, подберу вам более новую модель iPhone?"),
        "note": ("Ответь клиенту этим текстом. Не говори про восстановленные телефоны, если клиент сам не спросил, "
                 "не отправляй ссылки. Новые модели предлагай только из прайса (search_products). "
                 "Если клиент сам спрашивает именно Б/У и доступен search_used — покажи б/у из наличия. "
                 "Если клиент хочет СДАТЬ эту модель — оценивай по Trade-in как обычно."),
    }


class ToolExecutor:
    """Выполняет вызовы инструментов. `state` — изменяемый словарь карточки диалога (сохраняется в БД)."""

    def __init__(self, catalog: Catalog, state: dict, kaspi_markup_percent: float = 20, kaspi_months: int = 24,
                 discontinued_rule: bool = True, stock=None):
        self.discontinued_rule = discontinued_rule
        self.stock = stock          # StockService филиала или None
        self.media: list[dict] = []  # фото, которые надо отправить клиенту после текста
        self.catalog = catalog
        self.state = state
        self.kaspi_markup = kaspi_markup_percent
        self.kaspi_months = kaspi_months

    def run(self, name: str, args: dict) -> tuple[str, bool]:
        """Возвращает (json-строка результата, is_error)."""
        try:
            handler = getattr(self, f"_t_{name}")
        except AttributeError:
            return _dump({"error": f"Неизвестный инструмент {name}"}), True
        try:
            return _dump(handler(**args)), False
        except TypeError as e:
            return _dump({"error": f"Неверные параметры: {e}"}), True
        except Exception as e:  # не роняем диалог из-за одного инструмента
            log.exception("Ошибка инструмента %s", name)
            return _dump({"error": f"Инструмент временно недоступен: {e.__class__.__name__}"}), True

    async def arun(self, name: str, args: dict) -> tuple[str, bool]:
        """Как run, но умеет инструменты, которым нужна сеть (наличие SFON)."""
        handler = getattr(self, f"_a_{name}", None)
        if handler is None:
            return self.run(name, args)
        try:
            return _dump(await handler(**args)), False
        except TypeError as e:
            return _dump({"error": f"Неверные параметры: {e}"}), True
        except Exception as e:
            log.exception("Ошибка инструмента %s", name)
            return _dump({"error": f"Инструмент временно недоступен: {e.__class__.__name__}"}), True

    # ------------------------------------------------------------------ наличие (SFON)
    def _no_stock(self) -> dict:
        return {"error": "Проверка наличия для этого филиала не подключена — наличие уточняет менеджер."}

    async def _a_check_stock(self, model: str, memory: str | None = None, color: str | None = None,
                             sim: str | None = None) -> dict:
        if not self.stock:
            return self._no_stock()
        if self.discontinued_rule and (label := discontinued_iphone(model)):
            return _discontinued_result(label)
        return await self.stock.check_new(model, memory, color, sim)

    async def _a_search_used(self, model: str | None = None, memory: str | None = None,
                             battery_from: int | None = None, max_price: float | None = None) -> dict:
        if not self.stock:
            return self._no_stock()
        return await self.stock.search_used(model, memory, battery_from, max_price)

    async def _a_send_used_photos(self, product_id: int) -> dict:
        if not self.stock:
            return self._no_stock()
        unit = await self.stock.used_unit(int(product_id))
        if unit is None:
            return {"error": "Экземпляр не найден (возможно, уже продан). Предложи другой или менеджера."}
        if not unit.photos:
            return {"status": "no_photos",
                    "note": f"У этого экземпляра нет фото в базе. Предложи посмотреть фото у менеджера в WhatsApp: "
                            f"{self.stock.whatsapp or 'WhatsApp менеджера'}"}
        urls = unit.photos[:4]
        if any(m.get("product_id") == unit.product_id for m in self.media):
            return {"status": "already_queued", "photos": len(urls)}
        self.media.append({"product_id": unit.product_id, "label": unit.label(), "urls": urls})
        return {"status": "queued", "photos": len(urls),
                "note": ("Фото отправятся сразу после твоего сообщения. Напиши коротко, что отправляешь фото. "
                         "Не описывай состояние, которого нет в description; если клиент спросит про царапины, а по "
                         "описанию это не ясно — направь к менеджеру.")}

    async def _used_price(self, product_id) -> tuple[object, dict | None]:
        if not self.stock:
            return None, self._no_stock()
        unit = await self.stock.used_unit(int(product_id))
        if unit is None:
            return None, {"error": "Б/у экземпляр не найден — возможно, уже продан."}
        if unit.price is None:
            return None, {"error": "У этого б/у не указана цена — предложи уточнить у менеджера."}
        return unit, None

    async def _a_calculate_kaspi_installment(self, basis: str, product_id: int | None = None, **kw) -> dict:
        if basis != "used":
            return self._t_calculate_kaspi_installment(basis, **kw)
        if not product_id:
            return {"error": "Для б/у укажи product_id из search_used."}
        unit, err = await self._used_price(product_id)
        if err:
            return err
        months = kw.get("months") or self.kaspi_months
        if months != self.kaspi_months:
            return {"error": f"Наценка известна только для рассрочки на {self.kaspi_months} мес."}
        calc = installment(int(unit.price), self.kaspi_markup, months)
        return {"product": unit.label() + " (б/у)", **calc, **self._inst_text(calc),
                "note": "Цена б/у — за наличные; в рассрочку дороже на наценку. Одобрение — за Kaspi."}

    async def _a_calculate_tradein_topup(self, used_product_id: int | None = None, **kw) -> dict:
        if not used_product_id:
            if not kw.get("new_model") or not kw.get("new_memory"):
                return {"error": "Укажи модель и память нового телефона (или used_product_id для б/у)."}
            return self._t_calculate_tradein_topup(**kw)
        saved = self.state.get("tradein")
        if not saved:
            return {"error": "Нет сохранённой оценки старого телефона — сначала quote_iphone_tradein."}
        if saved.get("status") != "ok":
            return {"error": "Старый телефон требует индивидуальной оценки менеджером — доплату не считаем."}
        unit, err = await self._used_price(used_product_id)
        if err:
            return err
        return self._topup_for(unit.label() + " (б/у)", int(unit.price), saved)

    # ------------------------------------------------------------------ розница
    def _t_search_products(self, query: str) -> dict:
        if self.discontinued_rule and (label := discontinued_iphone(query)):
            return _discontinued_result(label)
        found = self.catalog.search(query)
        upcoming = upcoming_model(query)
        if upcoming and _upcoming_unpriced(upcoming, found):
            return {"found": 0, **_upcoming_info(upcoming)}
        if not found:
            return {"found": 0, "note": "В прайсе не найдено. Это не доказывает отсутствие товара — предложи уточнить у менеджера."}
        return {"found": len(found), "items": [p.to_tool() for p in found]}

    def _t_get_exact_price(self, model: str, memory: str | None = None, sim: str | None = None,
                           color: str | None = None) -> dict:
        if self.discontinued_rule and (label := discontinued_iphone(model)):
            return _discontinued_result(label)
        items, exact = self.catalog.find_exact(model, memory, sim, color)
        upcoming = upcoming_model(model)
        if upcoming and _upcoming_unpriced(upcoming, items):
            return {"found": 0, **_upcoming_info(upcoming)}
        if not items:
            return {"found": 0, "note": "Такой конфигурации в прайсе нет. Не подставляй цену другой модели/памяти."}
        res: dict = {"found": len(items), "exact_model_match": exact, "items": [p.to_tool() for p in items]}
        if len(items) > 1:
            res["note"] = "Несколько вариантов — уточни у клиента недостающий параметр или покажи 1–2 варианта."
        if color:
            res["color_note"] = "Цвет в прайсе обычно не указан; наличие конкретного цвета подтверждает менеджер."
        return res

    # ------------------------------------------------------------------ trade-in
    def _t_quote_iphone_tradein(self, model: str, memory: str, battery_percent: int,
                                has_box: bool | None = None, minor_scratches: bool | None = None,
                                was_repaired: bool | None = None, screen_or_body_damage: bool | None = None,
                                all_functions_work: bool | None = None, sim: str | None = None) -> dict:
        if "samsung" in model.lower() or "galaxy" in model.lower():
            return {"status": "not_accepted", "note": "Samsung в Trade-in не принимается."}
        items = self.catalog.tradein_lookup(model, memory, sim)
        if not items:
            options = [t.label() for t in self.catalog.tradein_lookup(model)]
            return {"status": "not_found",
                    "note": "Этой модели/памяти нет в таблице выкупа — предложи оценку у менеджера.",
                    "available_for_model": options or None}
        # SIM не спрашиваем: если цена выкупа зависит от SIM, оцениваем все варианты сразу
        sim_variants = []
        if len(items) > 1:
            priced = [t for t in items if t.max_price is not None]
            if not priced:
                return {"status": "manager_review", "note": "Цена выкупа не указана в таблице — нужна оценка менеджера."}
            priced.sort(key=lambda t: t.max_price)
            sim_variants = priced
            items = [priced[0]]  # меньшая цена — основная (для доплаты по умолчанию)
        item = items[0]
        if item.max_price is None:
            return {"status": "manager_review", "note": "Цена выкупа не указана в таблице — нужна оценка менеджера."}

        # Не указанное клиентом считаем лучшим случаем и прямо оговариваем это в ответе
        assumed = []
        if has_box is None:
            has_box = True
            assumed.append("есть коробка")
        if minor_scratches is None and screen_or_body_damage is None:
            assumed.append("нет царапин и повреждений")
        minor_scratches = bool(minor_scratches)
        screen_or_body_damage = bool(screen_or_body_damage)
        if was_repaired is None:
            was_repaired = False
            assumed.append("телефон не ремонтировался")
        if all_functions_work is None:
            all_functions_work = True
            assumed.append("всё работает исправно")
        cond = Condition(battery_percent, has_box, minor_scratches, was_repaired,
                         screen_or_body_damage, all_functions_work)
        result = quote(item.max_price, cond)
        device = {"model": item.model, "memory": item.memory,
                  "sim": None if sim_variants else SIM_LABELS.get(item.sim or "")}
        variants = []
        if result["status"] == "ok":
            for t in sim_variants:
                variants.append({"sim": SIM_LABELS.get(t.sim or "", t.sim), "estimate": quote(t.max_price, cond)["estimate"]})
        saved = {
            "device": device,
            "condition": cond.__dict__,
            "assumed": assumed,
            "quoted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **result,
        }
        if variants:
            saved["variants"] = variants
        self.state["tradein"] = saved
        self.state.pop("last_topup", None)
        # Базовую цену и вычеты модели не показываем: клиенту называем только итоговую сумму.
        out = {k: v for k, v in result.items() if k not in ("base_price", "deductions", "total_deduction")}
        out.update({"device": device, "saved": True,
                    "note": "Сумма предварительная, окончательная — после осмотра. "
                            "Называй клиенту только итоговую сумму, без вычетов и их причин."})
        if result["status"] == "ok":
            out["estimate_text"] = format_price(result["estimate"])
            if assumed:
                out["assumed_condition"] = assumed
                out["say_to_client"] = ("Обязательно скажи, что это цена при условии: " + ", ".join(assumed) + ". "
                                        "Если что-то не так, сумма может быть ниже — пусть клиент напишет, "
                                        "и ты пересчитаешь. Отдельно об этом не расспрашивай.")
            if variants:
                out["by_sim"] = [{"sim": v["sim"], "estimate_text": format_price(v["estimate"])} for v in variants]
                out.pop("estimate", None)
                out.pop("estimate_text", None)
                out["sim_note"] = ("Цена выкупа зависит от типа SIM. Не спрашивай SIM — назови клиенту обе суммы "
                                   "из by_sim одним сообщением. Если клиент сам уточнит SIM, пересчитай с sim.")
        return out

    def _t_calculate_tradein_topup(self, new_model: str, new_memory: str, new_sim: str | None = None,
                                   new_color: str | None = None) -> dict:
        saved = self.state.get("tradein")
        if not saved:
            return {"error": "Нет сохранённой оценки старого телефона — сначала собери данные и вызови quote_iphone_tradein."}
        if saved.get("status") != "ok":
            return {"error": "Старый телефон требует индивидуальной оценки менеджером — доплату автоматически не считаем."}
        if self.discontinued_rule and (label := discontinued_iphone(new_model)):
            return _discontinued_result(label)
        items, _ = self.catalog.find_exact(new_model, new_memory, new_sim, new_color)
        if (upcoming := upcoming_model(new_model)) and _upcoming_unpriced(upcoming, items):
            return _upcoming_info(upcoming)
        items = [p for p in items if normalize_memory(new_memory) == p.memory]
        if new_sim:
            items = [p for p in items if p.sim in (normalize_sim(new_sim), None)]
        if not items:
            return {"error": "Новый телефон в такой конфигурации не найден в прайсе — доплату не называй."}
        if len(items) > 1:
            by_color = len({p.sim for p in items}) == 1 and any(p.color for p in items)
            return {"status": "need_color" if by_color else "need_sim",
                    "note": ("Цена нового телефона зависит от цвета — уточни цвет у клиента." if by_color
                             else "Цена нового телефона зависит от SIM — уточни у клиента."),
                    "options": [p.to_tool() for p in items]}
        new = items[0]
        if new.price is None:
            return {"error": "Цена нового телефона в прайсе не указана — доплату не называй, предложи уточнить у менеджера."}
        return self._topup_for(new.label(), new.price, saved)


    def _topup_for(self, label: str, price: int, saved: dict) -> dict:
        calc = topup(price, saved["estimate"])
        result = {
            "old_device": saved["device"],
            "new_device": label,
            **calc,
            "new_price_text": format_price(price),
            "tradein_estimate_text": format_price(saved["estimate"]),
            "topup_text": format_price(calc["topup"]),
            "note": "Доплата предварительная: оценка старого телефона подтверждается после осмотра.",
        }
        if saved.get("variants"):
            result["by_old_sim"] = [
                {"old_sim": v["sim"], "tradein_estimate_text": format_price(v["estimate"]),
                 "topup_text": format_price(topup(price, v["estimate"])["topup"])}
                for v in saved["variants"]]
            result["note"] += (" SIM старого телефона неизвестна — назови доплату для обоих вариантов из by_old_sim, "
                               "не спрашивая SIM.")
            for k in ("tradein_estimate", "tradein_estimate_text", "topup", "topup_text"):
                result.pop(k, None)
        self.state["last_topup"] = {"new_device": label, "new_price": price,
                                    "tradein_estimate": saved["estimate"], "topup": calc["topup"]}
        return result

    # ------------------------------------------------------------------ рассрочка
    def _t_calculate_kaspi_installment(self, basis: str, model: str | None = None, memory: str | None = None,
                                       sim: str | None = None, color: str | None = None,
                                       months: int | None = None) -> dict:
        months = months or self.kaspi_months
        if months != self.kaspi_months:
            return {"error": f"Наценка известна только для рассрочки на {self.kaspi_months} мес. "
                             "Другие сроки уточнит менеджер."}
        if basis == "tradein_topup":
            lt = self.state.get("last_topup")
            if not lt:
                return {"error": "Доплата по Trade-in ещё не рассчитана — сначала calculate_tradein_topup."}
            calc = installment(lt["topup"], self.kaspi_markup, months)
            return {"basis": f"доплата за {lt['new_device']} с Trade-in", **calc, **self._inst_text(calc),
                    "note": "Доплата предварительная; одобрение рассрочки — за Kaspi."}
        if not model:
            return {"error": "Укажи модель товара."}
        items, _ = self.catalog.find_exact(model, memory, sim, color)
        if (upcoming := upcoming_model(model)) and _upcoming_unpriced(upcoming, items):
            return _upcoming_info(upcoming)
        if not items:
            return {"error": "Товар не найден в прайсе — сумму не называй."}
        if len(items) > 1:
            return {"status": "need_details", "note": "Несколько вариантов — уточни память/SIM.",
                    "options": [p.to_tool() for p in items]}
        p = items[0]
        if p.price is None:
            return {"error": "Цена в прайсе не указана — рассрочку не считаем."}
        calc = installment(p.price, self.kaspi_markup, months)
        return {"product": p.label(), **calc, **self._inst_text(calc),
                "note": "Цена в прайсе — за наличные; в рассрочку дороже на наценку. Одобрение — за Kaspi."}

    @staticmethod
    def _inst_text(c: dict) -> dict:
        return {"cash_price_text": format_price(c["cash_price"]),
                "installment_price_text": format_price(c["installment_price"]),
                "monthly_payment_text": format_price(c["monthly_payment"])}


def state_summary(state: dict) -> str:
    """Краткая карточка диалога для системного промпта."""
    lines = []
    t = state.get("tradein")
    if t:
        d = t["device"]
        name = ", ".join(x for x in (d.get("model"), d.get("memory"), d.get("sim")) if x)
        c = t.get("condition", {})
        cond = (f"аккумулятор {c.get('battery_percent')}%, коробка: {'есть' if c.get('has_box') else 'нет'}, "
                f"царапины: {'да' if c.get('minor_scratches') else 'нет'}, ремонт: {'был' if c.get('was_repaired') else 'не было'}")
        if t.get("status") == "ok" and t.get("variants"):
            by = "; ".join(f"{v['sim']} — {format_price(v['estimate'])}" for v in t["variants"])
            lines.append(f"Сохранённая оценка Trade-in (старый телефон клиента): {name}, SIM не уточнена; {cond}; "
                         f"предварительно: {by}.")
        elif t.get("status") == "ok":
            lines.append(f"Сохранённая оценка Trade-in (старый телефон клиента): {name}; {cond}; "
                         f"предварительно {format_price(t['estimate'])}.")
        else:
            lines.append(f"Старый телефон клиента: {name}; {cond}; требуется индивидуальная оценка менеджером.")
    lt = state.get("last_topup")
    if lt:
        lines.append(f"Последний расчёт доплаты: {lt['new_device']} — {format_price(lt['new_price'])} − "
                     f"{format_price(lt['tradein_estimate'])} = {format_price(lt['topup'])}.")
    return "\n".join(lines)
