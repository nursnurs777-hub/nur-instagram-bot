"""Каталог магазина: разбор листов Google Sheets, нормализация названий и поиск.

Модуль не ходит в сеть: на вход получает {имя_листа: [строки]}, где строка —
список ячеек как строк (так их отдаёт Sheets API с форматированными значениями).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Iterable

# ---------------------------------------------------------------- нормализация

_QUOTES = "”“\"″'’`«»"

# (шаблон, замена). Применяются по порядку к строке в нижнем регистре.
# Границы слов задаются вручную, потому что \b плохо работает с кириллицей.
_B = r"(?<![a-zа-я0-9])"
_E = r"(?![a-zа-я])"
_REPLACEMENTS: list[tuple[str, str]] = [
    (_B + r"(айфон|айфоне|айфона|ифон|iphon)" + _E, "iphone"),
    (_B + r"(айпад|айпэд)" + _E, "ipad"),
    (_B + r"(макбук|макбука|makbook)" + _E, "macbook"),
    (_B + r"(эйрподс|аирподс|эирподс|airpod)" + _E, "airpods"),
    (_B + r"(эпл|эппл|аппл)\s*(вотч|уотч|часы)" + _E, "apple watch"),
    (_B + r"(вотч|уотч)" + _E, "watch"),
    (_B + r"promax" + _E, "pro max"),
    (_B + r"про" + _E, "pro"),
    (_B + r"(макс|макса)" + _E, "max"),
    (_B + r"плюс" + _E, "plus"),
    (_B + r"мини" + _E, "mini"),
    (_B + r"(эйр|аир)" + _E, "air"),
    (_B + r"ультра" + _E, "ultra"),
    (_B + r"нео" + _E, "neo"),
    (_B + r"(дайсон|дисон)" + _E, "dyson"),
    (_B + r"(эйрврап|аирврап)" + _E, "airwrap"),
    (_B + r"(ps|пс)\s*5" + _E, "playstation 5"),
    (_B + r"(плейстейшн|плэйстейшн|плейстейшен)" + _E, "playstation"),
    (_B + r"playstation5" + _E, "playstation 5"),
    (_B + r"(есим|е-сим|e-sim|esim|эсим)" + _E, "esim"),
    (_B + r"(сим)" + _E, "sim"),
    (_B + r"(гб|гиг|bg)" + _E, "gb"),
    (_B + r"(тб|тера)" + _E, "tb"),
    (_B + r"(свитч)" + _E, "switch"),
    (_B + r"(нинтендо)" + _E, "nintendo"),
    (_B + r"(дрон)" + _E, "drone"),
]
_COMPILED = [(re.compile(p), r) for p, r in _REPLACEMENTS]


def norm(text: str | None) -> str:
    """Приводит название к каноническому виду для сравнения."""
    if not text:
        return ""
    s = str(text).lower().replace("ё", "е").replace("‑", "-").replace("\xa0", " ")
    for ch in _QUOTES:
        s = s.replace(ch, " ")
    # «256GB» → «256 gb», «17e» оставляем, «M5» оставляем
    s = re.sub(r"(\d)(gb|tb|гб|тб|bg|mm|мм)", r"\1 \2", s)
    for rx, repl in _COMPILED:
        s = rx.sub(repl, s)
    s = s.replace("мм", "mm")
    s = re.sub(r"[^a-zа-я0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(text: str | None) -> list[str]:
    return norm(text).split()


def parse_price(raw: str | None) -> int | None:
    """«480 000 ₸», «₸710 000», «75000» → int. Пустая ячейка → None."""
    if raw is None:
        return None
    digits = re.sub(r"[^\d]", "", str(raw))
    return int(digits) if digits else None


def format_price(value: int | None) -> str:
    if value is None:
        return "цена не указана"
    return f"{value:,}".replace(",", " ") + " ₸"


def normalize_memory(raw: str | None) -> str | None:
    """«256», «256гб», «256BG», «1тб», «1024» → «256GB» / «1TB»."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    if "/" in s:  # конфигурация MacBook «16/512GB»
        return re.sub(r"\s+", "", s).upper().replace("ГБ", "GB").replace("ТБ", "TB")
    m = re.search(r"(\d+)\s*([a-zа-я]*)", s)
    if not m:
        return None
    num, unit = int(m.group(1)), m.group(2)
    if unit.startswith(("t", "т")) or num in (1, 2, 4, 8) and not unit:
        return f"{num}TB"
    if num == 1024:
        return "1TB"
    if num == 2048:
        return "2TB"
    return f"{num}GB"


def normalize_sim(raw: str | None) -> str | None:
    """Канонические ключи: «sim+esim», «esim» или None (вариант не делится по SIM)."""
    if raw is None:
        return None
    s = norm(raw)
    if not s:
        return None
    if "sim" in s.replace("esim", "") or "nano" in s or "физ" in s or "dual" in s or "2" in s.split():
        return "sim+esim"
    if "esim" in s or "только" in s or "only" in s:
        return "esim"
    return None


SIM_LABELS = {"sim+esim": "SIM + eSIM", "esim": "только eSIM"}

# ---------------------------------------------------------------- модели данных


@dataclass
class Product:
    category: str
    model: str
    price: int | None
    memory: str | None = None
    sim: str | None = None  # канонический ключ
    connectivity: str | None = None
    color: str | None = None
    product_type: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def search_text(self) -> str:
        parts = [self.category, self.product_type, self.model, self.memory,
                 SIM_LABELS.get(self.sim or "", ""), self.connectivity, self.color,
                 *self.extra.values()]
        return norm(" ".join(p for p in parts if p))

    def label(self) -> str:
        bits = [self.model]
        if self.memory:
            bits.append(self.memory)
        if self.sim:
            bits.append(SIM_LABELS[self.sim])
        if self.connectivity:
            bits.append(self.connectivity)
        if self.color:
            bits.append(self.color)
        return ", ".join(bits)

    def to_tool(self) -> dict:
        d = {"category": self.category, "model": self.model, "label": self.label(),
             "price_kzt": self.price, "price_text": format_price(self.price)}
        for k in ("memory", "connectivity", "color", "product_type"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        if self.sim:
            d["sim"] = SIM_LABELS[self.sim]
        if self.price is None:
            d["note"] = "Цена в прайсе не указана — не называй сумму, предложи уточнить у менеджера."
        return d


@dataclass
class TradeInItem:
    id: str
    category: str
    model: str  # полное название, например «iPhone 15 Pro»
    memory: str | None
    size: str | None
    sim: str | None
    max_price: int | None

    def label(self) -> str:
        bits = [self.model]
        if self.memory:
            bits.append(self.memory)
        if self.size:
            bits.append(self.size)
        if self.sim:
            bits.append(SIM_LABELS[self.sim])
        return ", ".join(bits)

    def to_tool(self) -> dict:
        d = asdict(self)
        d["sim"] = SIM_LABELS.get(self.sim or "", None)
        d["label"] = self.label()
        return d


# ---------------------------------------------------------------- разбор листов

_SHOW_COLS = ("показать", "показывать")
_YES = ("да", "yes", "true", "1", "+")


def _col(header: list[str], *names: str) -> int | None:
    low = [h.strip().lower() for h in header]
    for n in names:
        for i, h in enumerate(low):
            if h.startswith(n):
                return i
    return None


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return str(row[idx]).strip()


def _visible(row: list[str], show_idx: int | None) -> bool:
    if show_idx is None:
        return True
    return _cell(row, show_idx).lower() in _YES


def parse_retail_sheet(sheet_name: str, rows: list[list[str]]) -> list[Product]:
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    i_model = _col(header, "модель", "model", "название")
    i_price = _col(header, "цена", "price")
    if i_model is None or i_price is None:
        return []
    i_type = _col(header, "продукт", "тип товара")
    i_mem = _col(header, "память", "конфигурация", "memory")
    i_sim = _col(header, "тип sim", "sim")
    i_conn = _col(header, "подключение")
    i_color = _col(header, "цвет", "color")
    i_show = _col(header, *_SHOW_COLS)
    known = {i for i in (i_model, i_price, i_type, i_mem, i_sim, i_conn, i_color, i_show) if i is not None}

    out: list[Product] = []
    for r in body:
        model = _cell(r, i_model)
        if not model or not _visible(r, i_show):
            continue
        extra = {header[i].strip(): _cell(r, i) for i in range(len(header))
                 if i not in known and header[i].strip() and _cell(r, i)}
        out.append(Product(
            category=sheet_name.strip(),
            model=model,
            price=parse_price(_cell(r, i_price)),
            memory=normalize_memory(_cell(r, i_mem)) if i_mem is not None else None,
            sim=normalize_sim(_cell(r, i_sim)),
            connectivity=_cell(r, i_conn).strip() or None,
            color=_cell(r, i_color) or None,
            product_type=_cell(r, i_type) or None,
            extra=extra,
        ))
    return out


def parse_tradein_sheet(rows: list[list[str]]) -> list[TradeInItem]:
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    i_id = _col(header, "id")
    i_cat = _col(header, "категория")
    i_model = _col(header, "модель")
    i_mem = _col(header, "память")
    i_size = _col(header, "размер")
    i_sim = _col(header, "тип sim")
    i_price = _col(header, "цена")
    i_show = _col(header, *_SHOW_COLS)
    out: list[TradeInItem] = []
    for r in body:
        model = _cell(r, i_model)
        if not model or not _visible(r, i_show):
            continue
        cat = _cell(r, i_cat) or "iPhone"
        full = model if norm(model).startswith(norm(cat)) else f"{cat} {model}"
        out.append(TradeInItem(
            id=_cell(r, i_id),
            category=cat,
            model=full,
            memory=normalize_memory(_cell(r, i_mem)),
            size=_cell(r, i_size) or None,
            sim=normalize_sim(_cell(r, i_sim)),
            max_price=parse_price(_cell(r, i_price)),
        ))
    return out


# ---------------------------------------------------------------- каталог


def _numeric_tokens(ts: Iterable[str]) -> set[str]:
    return {t for t in ts if any(ch.isdigit() for ch in t)}


def _model_key(model: str, category_hint: str | None = None) -> str:
    n = norm(model)
    if category_hint and norm(category_hint) == "iphone" and not n.startswith("iphone"):
        n = "iphone " + n
    # «iphone 17 air» и «iphone air» — разные написания одного; оставляем как есть
    return n


class Catalog:
    def __init__(self, sheets: dict[str, list[list[str]]], tradein_sheet: str = "Закуп б/у"):
        self.products: list[Product] = []
        self.tradein: list[TradeInItem] = []
        for name, rows in sheets.items():
            if name.strip().lower() == tradein_sheet.strip().lower():
                self.tradein = parse_tradein_sheet(rows)
            else:
                self.products.extend(parse_retail_sheet(name, rows))

    # -------- поиск по всему каталогу
    def search(self, query: str, limit: int = 8) -> list[Product]:
        q = tokens(query)
        if not q:
            return []
        q_num = _numeric_tokens(q)
        q_sim = normalize_sim(query) if "sim" in " ".join(q) else None
        scored: list[tuple[float, Product]] = []
        for p in self.products:
            if q_sim and p.sim and p.sim != q_sim:
                continue
            pt = p.search_text.split()
            pset = set(pt)
            if q_num and not q_num <= pset:
                continue  # «17 pro» не должен находить «18 pro»
            hits = sum(1 for t in q if t in pset or any(x.startswith(t) for x in pset if len(t) >= 3))
            if hits == 0:
                continue
            score = hits / len(q) - 0.001 * len(pt)
            scored.append((score, p))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            return []
        best = scored[0][0]
        return [p for s, p in scored if s >= best - 0.34][:limit]

    # -------- точная цена
    def find_exact(self, model: str, memory: str | None = None, sim: str | None = None,
                   color: str | None = None) -> tuple[list[Product], bool]:
        """Возвращает (варианты, exact_model_found)."""
        key = _model_key(model)
        cands = [p for p in self.products if norm(p.model) == key]
        if not cands and not key.startswith("iphone"):
            cands = [p for p in self.products if norm(p.model) == "iphone " + key]
        exact = bool(cands)
        if not cands:
            cands = self.search(model, limit=20)
        mem = normalize_memory(memory) if memory else None
        if mem:
            cands = [p for p in cands if p.memory and (p.memory == mem or norm(mem) in norm(p.memory))]
        sim_key = normalize_sim(sim) if sim else None
        if sim_key:
            cands = [p for p in cands if p.sim in (sim_key, None)]
        if color:
            c = norm(color)
            colored = [p for p in cands if p.color and c in norm(p.color)]
            if colored:
                cands = colored
        return cands, exact

    # -------- trade-in
    def tradein_lookup(self, model: str, memory: str | None = None, sim: str | None = None,
                       category: str = "iPhone") -> list[TradeInItem]:
        key = _model_key(model, category)
        items = [t for t in self.tradein if norm(t.category) == norm(category)]
        # «17e» в прайсе записан как «17E», «17 Air» — допускаем оба написания
        cands = [t for t in items if norm(t.model) == key]
        if not cands:
            cands = [t for t in items if norm(t.model).replace(" ", "") == key.replace(" ", "")]
        mem = normalize_memory(memory) if memory else None
        if mem:
            cands = [t for t in cands if t.memory == mem]
        sim_key = normalize_sim(sim) if sim else None
        if sim_key:
            cands = [t for t in cands if t.sim in (sim_key, None)]
        return cands

    def tradein_models(self, category: str = "iPhone") -> list[str]:
        seen: list[str] = []
        for t in self.tradein:
            if norm(t.category) == norm(category) and t.model not in seen:
                seen.append(t.model)
        return seen

    def stats(self) -> dict:
        cats: dict[str, int] = {}
        for p in self.products:
            cats[p.category] = cats.get(p.category, 0) + 1
        return {"products": len(self.products), "tradein": len(self.tradein), "by_category": cats}


def parse_text_dump(text: str) -> dict[str, list[list[str]]]:
    """Разбирает выгрузку вида «## Sheet name: X» + CSV (для тестов и офлайн-режима)."""
    import csv
    import io

    sheets: dict[str, list[list[str]]] = {}
    current: str | None = None
    buf: list[str] = []

    def flush():
        if current is not None:
            sheets[current] = [row for row in csv.reader(io.StringIO("\n".join(buf)))]

    for line in text.splitlines():
        m = re.match(r"^##\s*Sheet name:\s*(.+)$", line)
        if m:
            flush()
            current, buf = m.group(1).strip(), []
        elif current is not None:
            buf.append(line)
    flush()
    return sheets
