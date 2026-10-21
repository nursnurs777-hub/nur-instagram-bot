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
                "sim": {"type": "string", "enum": ["esim", "sim+esim"],
                        "description": "esim — только eSIM; sim+esim — физическая SIM + eSIM"},
                "color": {"type": "string"},
            },
            "required": ["model"],
        },
    },
    {
        "name": "quote_iphone_tradein",
        "description": (
            "Предварительная оценка СДАВАЕМОГО клиентом iPhone по таблице выкупа и утверждённым вычетам. "
            "Вызывай, когда известны модель, память и состояние (аккумулятор, коробка, царапины, ремонт, "
            "повреждения, работа функций). Результат сохраняется в карточке диалога; повторно вызывай только если "
            "клиент изменил данные о старом телефоне. Samsung и другие бренды не принимаются."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Например «iPhone 15 Pro»"},
                "memory": {"type": "string", "description": "«128GB», «256GB», «1TB»"},
                "sim": {"type": "string", "enum": ["esim", "sim+esim"],
                        "description": "Нужно только если в таблице есть оба варианта (iPhone 17 серии)"},
                "battery_percent": {"type": "integer", "minimum": 0, "maximum": 100},
                "has_box": {"type": "boolean"},
                "minor_scratches": {"type": "boolean"},
                "was_repaired": {"type": "boolean", "description": "Был ремонт или вскрытие"},
                "screen_or_body_damage": {"type": "boolean", "description": "Существенные повреждения экрана/корпуса"},
                "all_functions_work": {"type": "boolean"},
            },
            "required": ["model", "memory", "battery_percent", "has_box", "minor_scratches", "was_repaired",
                         "screen_or_body_damage", "all_functions_work"],
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
                "new_sim": {"type": "string", "enum": ["esim", "sim+esim"]},
            },
            "required": ["new_model", "new_memory"],
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
                "basis": {"type": "string", "enum": ["product", "tradein_topup"]},
                "model": {"type": "string"},
                "memory": {"type": "string"},
                "sim": {"type": "string", "enum": ["esim", "sim+esim"]},
                "color": {"type": "string"},
                "months": {"type": "integer", "description": "По умолчанию 24"},
            },
            "required": ["basis"],
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


def _discontinued_result(label: str) -> dict:
    return {
        "found": 0,
        "status": "discontinued",
        "model": label,
        "reply_to_client": (f"{label} официально снят с производства компанией Apple, и мы эту модель не продаём. "
                            "Хотите, подберу вам более новую модель iPhone?"),
        "note": ("Ответь клиенту этим текстом. Не говори про восстановленные телефоны, если клиент сам не спросил, "
                 "не отправляй ссылки. Новые модели предлагай только из прайса (search_products). "
                 "Если клиент хочет СДАТЬ эту модель — оценивай по Trade-in как обычно."),
    }


class ToolExecutor:
    """Выполняет вызовы инструментов. `state` — изменяемый словарь карточки диалога (сохраняется в БД)."""

    def __init__(self, catalog: Catalog, state: dict, kaspi_markup_percent: float = 20, kaspi_months: int = 24):
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

    # ------------------------------------------------------------------ розница
    def _t_search_products(self, query: str) -> dict:
        if label := discontinued_iphone(query):
            return _discontinued_result(label)
        found = self.catalog.search(query)
        if not found:
            return {"found": 0, "note": "В прайсе не найдено. Это не доказывает отсутствие товара — предложи уточнить у менеджера."}
        return {"found": len(found), "items": [p.to_tool() for p in found]}

    def _t_get_exact_price(self, model: str, memory: str | None = None, sim: str | None = None,
                           color: str | None = None) -> dict:
        if label := discontinued_iphone(model):
            return _discontinued_result(label)
        items, exact = self.catalog.find_exact(model, memory, sim, color)
        if not items:
            return {"found": 0, "note": "Такой конфигурации в прайсе нет. Не подставляй цену другой модели/памяти."}
        res: dict = {"found": len(items), "exact_model_match": exact, "items": [p.to_tool() for p in items]}
        if len(items) > 1:
            res["note"] = "Несколько вариантов — уточни у клиента недостающий параметр или покажи 1–2 варианта."
        if color:
            res["color_note"] = "Цвет в прайсе обычно не указан; наличие конкретного цвета подтверждает менеджер."
        return res

    # ------------------------------------------------------------------ trade-in
    def _t_quote_iphone_tradein(self, model: str, memory: str, battery_percent: int, has_box: bool,
                                minor_scratches: bool, was_repaired: bool, screen_or_body_damage: bool,
                                all_functions_work: bool, sim: str | None = None) -> dict:
        if "samsung" in model.lower() or "galaxy" in model.lower():
            return {"status": "not_accepted", "note": "Samsung в Trade-in не принимается."}
        items = self.catalog.tradein_lookup(model, memory, sim)
        if not items:
            options = [t.label() for t in self.catalog.tradein_lookup(model)]
            return {"status": "not_found",
                    "note": "Этой модели/памяти нет в таблице выкупа — предложи оценку у менеджера.",
                    "available_for_model": options or None}
        if len(items) > 1:
            return {"status": "need_sim", "note": "Цена выкупа зависит от типа SIM — уточни у клиента.",
                    "options": [t.to_tool() for t in items]}
        item = items[0]
        if item.max_price is None:
            return {"status": "manager_review", "note": "Цена выкупа не указана в таблице — нужна оценка менеджера."}

        cond = Condition(battery_percent, has_box, minor_scratches, was_repaired,
                         screen_or_body_damage, all_functions_work)
        result = quote(item.max_price, cond)
        device = {"model": item.model, "memory": item.memory, "sim": SIM_LABELS.get(item.sim or "")}
        saved = {
            "device": device,
            "condition": cond.__dict__,
            "quoted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **result,
        }
        self.state["tradein"] = saved
        self.state.pop("last_topup", None)
        # Базовую цену и вычеты модели не показываем: клиенту называем только итоговую сумму.
        out = {k: v for k, v in result.items() if k not in ("base_price", "deductions", "total_deduction")}
        out.update({"device": device, "saved": True,
                    "note": "Сумма предварительная, окончательная — после осмотра. "
                            "Называй клиенту только итоговую сумму, без вычетов и их причин."})
        if result["status"] == "ok":
            out["estimate_text"] = format_price(result["estimate"])
        return out

    def _t_calculate_tradein_topup(self, new_model: str, new_memory: str, new_sim: str | None = None) -> dict:
        saved = self.state.get("tradein")
        if not saved:
            return {"error": "Нет сохранённой оценки старого телефона — сначала собери данные и вызови quote_iphone_tradein."}
        if saved.get("status") != "ok":
            return {"error": "Старый телефон требует индивидуальной оценки менеджером — доплату автоматически не считаем."}
        if label := discontinued_iphone(new_model):
            return _discontinued_result(label)
        items, _ = self.catalog.find_exact(new_model, new_memory, new_sim)
        items = [p for p in items if normalize_memory(new_memory) == p.memory]
        if new_sim:
            items = [p for p in items if p.sim in (normalize_sim(new_sim), None)]
        if not items:
            return {"error": "Новый телефон в такой конфигурации не найден в прайсе — доплату не называй."}
        if len(items) > 1:
            return {"status": "need_sim", "note": "Цена нового телефона зависит от SIM — уточни у клиента.",
                    "options": [p.to_tool() for p in items]}
        new = items[0]
        if new.price is None:
            return {"error": "Цена нового телефона в прайсе не указана — доплату не называй, предложи уточнить у менеджера."}
        calc = topup(new.price, saved["estimate"])
        result = {
            "old_device": saved["device"],
            "new_device": new.label(),
            **calc,
            "new_price_text": format_price(new.price),
            "tradein_estimate_text": format_price(saved["estimate"]),
            "topup_text": format_price(calc["topup"]),
            "note": "Доплата предварительная: оценка старого телефона подтверждается после осмотра.",
        }
        self.state["last_topup"] = {k: result[k] for k in ("new_device", "new_price", "tradein_estimate", "topup")}
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
        if t.get("status") == "ok":
            lines.append(f"Сохранённая оценка Trade-in (старый телефон клиента): {name}; {cond}; "
                         f"предварительно {format_price(t['estimate'])}.")
        else:
            lines.append(f"Старый телефон клиента: {name}; {cond}; требуется индивидуальная оценка менеджером.")
    lt = state.get("last_topup")
    if lt:
        lines.append(f"Последний расчёт доплаты: {lt['new_device']} — {format_price(lt['new_price'])} − "
                     f"{format_price(lt['tradein_estimate'])} = {format_price(lt['topup'])}.")
    return "\n".join(lines)
