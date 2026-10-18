"""Локальный чат с ботом в терминале — без Instagram. Удобно проверять промпт и цены до одобрения Meta.

    python -m scripts.chat                      # прайс из Google Sheets (нужны SPREADSHEET_ID и ключ)
    python -m scripts.chat --price-file tests/fixtures/price_nur.txt   # офлайн-прайс

Команды: /reset — новый диалог, /state — карточка диалога, /exit — выход.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import SalesAgent  # noqa: E402
from app.config import Settings  # noqa: E402
from app.sheets import CatalogProvider  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--price-file", help="Выгрузка прайса в формате «## Sheet name:» + CSV")
    args = ap.parse_args()

    s = Settings()
    if args.price_file:
        s = replace(s, price_file=args.price_file)
    if not s.anthropic_api_key:
        sys.exit("Задайте ANTHROPIC_API_KEY (например, в файле .env и `set -a; source .env`)")

    catalogs = CatalogProvider(s)
    catalog = await catalogs.get()
    print(f"Каталог: {catalog.stats()['products']} товаров, {catalog.stats()['tradein']} позиций выкупа.")
    agent = SalesAgent(s)
    history: list[dict] = []
    state: dict = {}

    while True:
        try:
            text = input("\nКлиент: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text == "/exit":
            break
        if text == "/reset":
            history, state = [], {}
            print("— новый диалог —")
            continue
        if text == "/state":
            print(state or "(пусто)")
            continue
        history.append({"role": "user", "content": text})
        res = await agent.reply(history[-s.history_limit:], state, await catalogs.get())
        state = res.state
        history.append({"role": "assistant", "content": res.text})
        tools = f"  [инструменты: {', '.join(res.tool_calls)}]" if res.tool_calls else ""
        print(f"\nБот: {res.text}{tools}")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    asyncio.run(main())
