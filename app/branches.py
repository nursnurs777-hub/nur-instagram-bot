"""Филиалы: у каждого свой Instagram-аккаунт, адрес, контакты и (при желании) своя таблица цен.

Основной филиал (Астана) задаётся старыми переменными окружения STORE_*, IG_ACCESS_TOKEN, IG_ACCOUNT_ID.
Дополнительные — в файле branches.json (несекретные данные) + переменная IG_ACCESS_TOKEN_<KEY> с маркером.
ID аккаунта можно не указывать: при запуске бот сам спросит его у Instagram по маркеру.

Ключ диалога в базе: у основного филиала — просто IGSID клиента (как было), у остальных — «<key>:<IGSID>».
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import ROOT, Settings

log = logging.getLogger(__name__)

DEFAULT_DELIVERY_ASTANA = "Для Астаны действует бесплатная доставка при заказе от 35 000 ₸."
DEFAULT_DELIVERY_OTHER = ("Условия доставки по городу (стоимость и сроки) уточняет менеджер — "
                          "не называй сумму и сроки сам.")


@dataclass
class Branch:
    key: str
    city: str
    city_in: str                 # «в Астане», «в Шымкенте»
    name: str
    instagram: str
    address: str
    hours: str
    whatsapp: str = ""
    phone: str = ""
    delivery: str = ""
    spreadsheet_id: str = ""
    sheet_gids: str = ""         # запасной список листов «имя:gid,…» для своей таблицы
    access_token: str = field(default="", repr=False)
    account_id: str = ""
    primary: bool = False

    @property
    def active(self) -> bool:
        return bool(self.access_token)

    def conv_key(self, igsid: str) -> str:
        return igsid if self.primary else f"{self.key}:{igsid}"

    def contact_line(self) -> str:
        bits = [f"Instagram {self.instagram}", f"адрес: {self.address}", f"часы: {self.hours}"]
        if self.whatsapp:
            bits.append(f"WhatsApp: {self.whatsapp}")
        if self.phone:
            bits.append(f"телефон: {self.phone}")
        return f"{self.city} — " + ", ".join(bits)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def primary_from_settings(s: Settings) -> Branch:
    city = _env("STORE_CITY", "Астана")
    return Branch(
        key=_env("BRANCH_KEY", "astana"),
        city=city,
        city_in=_env("STORE_CITY_IN", "в Астане" if city == "Астана" else f"в г. {city}"),
        name=s.store_name, instagram=s.store_instagram, address=s.store_address, hours=s.store_hours,
        whatsapp=s.store_whatsapp, phone=_env("STORE_PHONE"),
        delivery=_env("STORE_DELIVERY", DEFAULT_DELIVERY_ASTANA if city == "Астана" else DEFAULT_DELIVERY_OTHER),
        spreadsheet_id=s.spreadsheet_id, access_token=s.ig_access_token, account_id=s.ig_account_id,
        primary=True,
    )


def load_branches(s: Settings, path: str | None = None) -> list[Branch]:
    """Основной филиал + филиалы из branches.json. Без маркера филиал считается неактивным."""
    primary = primary_from_settings(s)
    out = [primary]
    p = Path(path or _env("BRANCHES_FILE", str(ROOT / "branches.json")))
    if not p.exists():
        return out
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        log.exception("Не удалось прочитать %s", p)
        return out
    for raw in data:
        key = str(raw.get("key", "")).strip().lower()
        if not key or key == primary.key or any(b.key == key for b in out):
            log.warning("Филиал пропущен: пустой или повторяющийся key=%r", key)
            continue
        up = key.upper()
        city = raw.get("city", key.title())
        b = Branch(
            key=key, city=city, city_in=raw.get("city_in", f"в г. {city}"),
            name=raw.get("name", primary.name), instagram=raw.get("instagram", ""),
            address=raw.get("address", ""), hours=raw.get("hours", primary.hours),
            whatsapp=raw.get("whatsapp", ""), phone=raw.get("phone", ""),
            delivery=raw.get("delivery", DEFAULT_DELIVERY_OTHER),
            spreadsheet_id=raw.get("spreadsheet_id") or primary.spreadsheet_id,
            sheet_gids=raw.get("sheet_gids", ""),
            access_token=_env(f"IG_ACCESS_TOKEN_{up}"),
            account_id=_env(f"IG_ACCOUNT_ID_{up}", str(raw.get("account_id", ""))),
        )
        out.append(b)
        if not b.active:
            log.warning("Филиал %s (%s) без маркера IG_ACCESS_TOKEN_%s — отвечать в его Instagram бот не будет",
                        b.key, b.city, up)
    return out


def branches_note(current: Branch, all_branches: list[Branch]) -> str:
    """Абзац для промпта: где этот филиал и какие ещё есть."""
    others = [b for b in all_branches if b.key != current.key]
    if not others:
        return (f"У магазина один адрес — {current.city_in}. Не приглашай покупателей в другие города "
                "и несуществующие филиалы.")
    lines = [f"Ты отвечаешь в Instagram филиала {current.city_in}. Твой город по умолчанию — {current.city}: "
             "адрес, часы, контакты и доставку называй для него.",
             "Другие филиалы магазина:"]
    lines += [f"- {b.contact_line()}" for b in others]
    lines.append("Если клиент находится в другом городе или спрашивает про другой филиал — дай контакты нужного "
                 "филиала. Цены и наличие в другом филиале уточняет его менеджер; не обещай, что они совпадают. "
                 "Других филиалов, кроме перечисленных, нет.")
    return "\n".join(lines)
