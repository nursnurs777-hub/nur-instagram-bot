"""Цикл Claude: системный промпт + история + инструменты → текст ответа."""
from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic

from .catalog import Catalog
from .branches import Branch, branches_note, primary_from_settings
from .config import Settings
from .tools import TOOL_DEFINITIONS, ToolExecutor, state_summary

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

RUNTIME_RULES = """\
## КАК ПИСАТЬ В DIRECT (самое важное правило стиля, приоритет над остальными)

Пиши как живой продавец в переписке с телефона, а не как справочник.
- Обычно 1–2 коротких предложения, до ~250 символов. Длиннее — только если клиент сам попросил сравнение, \
расчёт Trade-in/рассрочки или подробности.
- Никаких списков цен. Максимум 1–2 варианта в одной фразе. Не перечисляй ассортимент.
- Если вопрос общий («есть айфоны?», «что есть из ноутбуков?», «сколько стоит айфон?») — коротко ответь и задай \
ОДИН уточняющий вопрос (модель, память, бюджет, для чего). Цены называй, когда понятна модель.
- Не добавляй того, о чём не спрашивали: про склад, часы работы, закрыт ли магазин, наличные, гарантию, WhatsApp. \
Про наличие говори, только когда клиент хочет приехать или купить. Что цена за наличные — упомяни, только если \
речь о способе оплаты или рассрочке.
- Без канцелярита («в ассортименте», «из актуального прайса», «уведомляем»). Пиши просто: «Да, есть», «Смотрите…».
- Один вопрос в конце, не два.

Примеры стиля (цифры в них условные — цены и суммы всегда бери только из инструментов):
Клиент: есть айфоны?
Ты: Да, есть 👍 Какую модель смотрите — 17, 17 Pro или 18 Pro?

Клиент: сколько 17 про?
Ты: iPhone 17 Pro 256 ГБ — 630 000 ₸ (SIM + eSIM) или 590 000 ₸ (только eSIM). Вам какая версия удобнее?

Клиент: хочу сдать свой 15 про
Ты: Можем посчитать) Сколько памяти и какой процент аккумулятора?

Клиент: а можно в рассрочку?
Ты: Да, Kaspi 0-0-24: за 17 Pro 256 ГБ выйдет 756 000 ₸, это 31 500 ₸ в месяц. Подсказать, как оформить?

## ТЕХНИЧЕСКИЕ ПРАВИЛА ЭТОГО ПОДКЛЮЧЕНИЯ (приоритет над разделом 22)

Доступные инструменты:
- search_products, get_exact_price — розничный прайс (Google Sheets). Любую цену бери только отсюда.
- quote_iphone_tradein — предварительная оценка старого iPhone по таблице выкупа; результат сохраняется в карточке диалога.
- calculate_tradein_topup — доплата = цена нового − сохранённая оценка. Арифметику сам не делай.
- calculate_kaspi_installment — цена в рассрочку Kaspi и платёж в месяц (для товара или для доплаты по Trade-in).
{web_search_line}
Инструментов get_stock и handoff_to_manager НЕТ: остатки не проверяются, автоматической передачи менеджеру нет. \
Для наличия, брони и оформления предлагай WhatsApp менеджера.

Формат: это Instagram Direct — он не отображает markdown. Пиши обычным текстом: без **, #, таблиц и \
markdown-ссылок. Ссылки — просто адресом. Списки — короткими строками, если без них никак.

Пометка [фото: …] — текстовое описание фото, которое клиент прислал раньше (сам файл не хранится). \
Опирайся на него, но помни: по фото нельзя гарантировать подлинность, память, IMEI и скрытые дефекты. \
Если в текущем сообщении есть само изображение — смотри на изображение.

Сообщения с пометкой [Ответ менеджера] написал живой сотрудник магазина. Учитывай их, не противоречь им \
и не повторяй уже сказанное.

Если ответ инструмента противоречит твоим ожиданиям — верь инструменту.\
"""


@dataclass
class IncomingImage:
    data: bytes
    media_type: str


@dataclass
class AgentResult:
    text: str
    state: dict
    tool_calls: list[str] = field(default_factory=list)


NO_WHATSAPP = ("(WhatsApp менеджера пока не указан — для брони, наличия и оформления предложи клиенту "
               "дождаться ответа менеджера здесь, в Direct, или приехать в магазин; не выдумывай номер)")


def render_prompt(template: str, s: Settings, branch: Branch | None = None,
                  branches: list[Branch] | None = None) -> str:
    b = branch or primary_from_settings(s)
    values = {
        "STORE_NAME": b.name,
        "STORE_INSTAGRAM": b.instagram,
        "STORE_CITY": b.city,
        "STORE_ADDRESS": b.address,
        "STORE_HOURS": b.hours,
        "WHATSAPP": b.whatsapp or NO_WHATSAPP,
        "DELIVERY": b.delivery,
        "BRANCHES_NOTE": branches_note(b, branches or [b]),
        "KASPI_MARKUP": f"{s.kaspi_markup_percent:g}",
    }
    for k, v in values.items():
        template = template.replace("{{" + k + "}}", v)
    return template


def _load_prompt(path: str, s: Settings, branch: Branch | None = None,
                 branches: list[Branch] | None = None) -> str:
    with open(path, encoding="utf-8") as f:
        return render_prompt(f.read(), s, branch, branches)


def strip_markdown(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"\1", text, flags=re.S)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1: \2", text)
    text = re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def build_messages(history: list[dict], images: list[IncomingImage] | None = None,
                   extra_user_note: str | None = None) -> list[dict]:
    """История из БД → messages для API. Соседние реплики одной роли склеиваются."""
    msgs: list[dict] = []
    for h in history:
        role = "user" if h["role"] == "user" else "assistant"
        text = h["content"] or ""
        if h["role"] == "manager":
            text = f"[Ответ менеджера] {text}"
        if not text.strip():
            continue
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"][0]["text"] += "\n" + text
        else:
            msgs.append({"role": role, "content": [{"type": "text", "text": text}]})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    if extra_user_note:
        if msgs and msgs[-1]["role"] == "user":
            msgs[-1]["content"].append({"type": "text", "text": extra_user_note})
        else:
            msgs.append({"role": "user", "content": [{"type": "text", "text": extra_user_note}]})
    if images and msgs and msgs[-1]["role"] == "user":
        for img in images[:4]:
            msgs[-1]["content"].insert(0, {
                "type": "image",
                "source": {"type": "base64", "media_type": img.media_type,
                           "data": base64.b64encode(img.data).decode()},
            })
    return msgs


class SalesAgent:
    def __init__(self, settings: Settings, client: AsyncAnthropic | None = None, branch: Branch | None = None,
                 branches: list[Branch] | None = None):
        self.s = settings
        self.client = client or AsyncAnthropic(api_key=settings.anthropic_api_key)
        self.branch = branch or primary_from_settings(settings)
        self.base_prompt = _load_prompt(settings.system_prompt_path, settings, self.branch, branches)

    def _tools(self) -> list[dict]:
        tools = list(TOOL_DEFINITIONS)
        if self.s.web_search_enabled:
            tools.append({"type": self.s.web_search_tool_type, "name": "web_search",
                          "max_uses": 3, "allowed_domains": self.s.web_search_domains})
        return tools

    def _system(self, state: dict, extra: str | None) -> list[dict]:
        tz = ZoneInfo(self.s.timezone)
        now = datetime.now(tz)
        is_open = self.s.work_hour_start <= now.hour < self.s.work_hour_end
        ws = ("- web_search — характеристики техники только с apple.com / support.apple.com. Используй для "
              "сравнений и новых моделей, о которых не уверен. Не ищи там цены магазина."
              if self.s.web_search_enabled else
              "- Поиска по сайту Apple нет: если не уверен в характеристике, честно скажи об этом.")
        runtime = RUNTIME_RULES.format(web_search_line=ws)
        ctx = [f"Сейчас: {WEEKDAYS_RU[now.weekday()]}, {now:%d.%m.%Y %H:%M} ({self.branch.city}). "
               f"Магазин сейчас {'открыт' if is_open else 'закрыт'}."]
        summary = state_summary(state)
        if summary:
            ctx.append("Карточка диалога (подтверждённые данные из инструментов):\n" + summary)
        if extra:
            ctx.append(extra)
        return [
            # длинная неизменная часть — кешируется, повторные запросы дешевле и быстрее
            {"type": "text", "text": self.base_prompt + "\n\n" + runtime, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "\n\n".join(ctx)},
        ]

    async def reply(self, history: list[dict], state: dict, catalog: Catalog,
                    images: list[IncomingImage] | None = None, extra_system: str | None = None,
                    extra_user_note: str | None = None) -> AgentResult:
        state = dict(state)
        executor = ToolExecutor(catalog, state, self.s.kaspi_markup_percent, self.s.kaspi_months)
        messages = build_messages(history, images, extra_user_note)
        if not messages:
            return AgentResult("", state)
        system = self._system(state, extra_system)
        tools = self._tools()
        called: list[str] = []

        for _ in range(MAX_TOOL_ROUNDS):
            resp = await self.client.messages.create(
                model=self.s.anthropic_model, max_tokens=self.s.max_tokens,
                system=system, tools=tools, messages=messages,
            )
            if resp.stop_reason == "pause_turn":  # серверный web_search ещё работает
                messages.append({"role": "assistant", "content": resp.content})
                continue
            if resp.stop_reason != "tool_use":
                text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
                return AgentResult(strip_markdown(text), state, called)

            results = []
            for block in resp.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                called.append(block.name)
                output, is_error = executor.run(block.name, dict(block.input or {}))
                log.info("tool %s(%s) -> %s", block.name, block.input, output[:300])
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output, "is_error": is_error})
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": results})

        log.warning("Превышено число шагов инструментов")
        return AgentResult("", state, called)
