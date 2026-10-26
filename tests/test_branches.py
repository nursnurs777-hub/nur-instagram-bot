"""Несколько филиалов: маршрутизация по аккаунту Instagram, свой промпт и свой маркер для каждого."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path

from app.agent import render_prompt
from app.branches import Branch, branches_note, load_branches
from app.config import Settings
from app.db import Store
from app.instagram import Event
from app.processor import BranchRuntime, Processor
from tests.test_flow import FakeAgent, FakeCatalogs, FakeIG, settle

TPL = Path("prompts/system_prompt.md").read_text(encoding="utf-8")

SHYM = {"key": "shymkent", "city": "Шымкент", "city_in": "в Шымкенте", "instagram": "@nur.shymkent",
        "address": "Шымкент, пр. Тауке хана, 1", "hours": "ежедневно с 10:00 до 21:00",
        "whatsapp": "https://wa.me/77000000001", "delivery": "По Шымкенту доставка бесплатная от 50 000 ₸."}


def settings(tmp_path, **kw):
    base = dict(database_url=f"sqlite:///{tmp_path}/b.db", debounce_seconds=0.05, echo_grace_seconds=0.05,
                allowed_user_ids=[], ig_account_id="AST", ig_access_token="tok-ast", bot_enabled=True,
                store_whatsapp="https://wa.me/77028081111")
    base.update(kw)
    return replace(Settings(), **base)


def branches_file(tmp_path, data=(SHYM,)):
    p = tmp_path / "branches.json"
    p.write_text(json.dumps(list(data), ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_load_branches(tmp_path, monkeypatch):
    s = settings(tmp_path)
    monkeypatch.setenv("IG_ACCESS_TOKEN_SHYMKENT", "tok-shym")
    br = load_branches(s, branches_file(tmp_path))
    assert [b.key for b in br] == ["astana", "shymkent"]
    ast, shym = br
    assert ast.primary and ast.city == "Астана" and ast.access_token == "tok-ast"
    assert shym.access_token == "tok-shym" and shym.active and not shym.primary
    assert shym.spreadsheet_id == ast.spreadsheet_id  # по умолчанию общая таблица
    assert shym.conv_key("123") == "shymkent:123" and ast.conv_key("123") == "123"


def test_branch_without_token_inactive(tmp_path, monkeypatch):
    monkeypatch.delenv("IG_ACCESS_TOKEN_SHYMKENT", raising=False)
    br = load_branches(settings(tmp_path), branches_file(tmp_path))
    assert not br[1].active


def test_prompts_per_branch(tmp_path, monkeypatch):
    s = settings(tmp_path)
    monkeypatch.setenv("IG_ACCESS_TOKEN_SHYMKENT", "x")
    ast, shym = load_branches(s, branches_file(tmp_path))
    p_ast = render_prompt(TPL, s, ast, [ast, shym])
    p_shym = render_prompt(TPL, s, shym, [ast, shym])
    assert "{{" not in p_ast and "{{" not in p_shym
    assert "**Город:** Шымкент" in p_shym and "Тауке хана" in p_shym and "wa.me/77000000001" in p_shym
    assert "50 000 ₸" in p_shym and "35 000 ₸" not in p_shym           # своя доставка
    assert "Төле би" in p_shym                                        # знает про Астану как другой филиал
    assert "**Город:** Астана" in p_ast and "Тауке хана" in p_ast and "35 000 ₸" in p_ast
    # без доп. филиалов — как раньше
    only = render_prompt(TPL, s, ast, [ast])
    assert "один адрес — в Астане" in only


def make_proc(tmp_path, monkeypatch):
    s = settings(tmp_path)
    monkeypatch.setenv("IG_ACCESS_TOKEN_SHYMKENT", "tok-shym")
    ast, shym = load_branches(s, branches_file(tmp_path))
    shym.account_id = "SHY"
    cat = FakeCatalogs()
    rts = [BranchRuntime(ast, FakeIG(), FakeAgent(), cat), BranchRuntime(shym, FakeIG(), FakeAgent(), cat)]
    return Processor(s, Store(s.database_url), rts[0].ig, rts[0].agent, cat, rts), rts


def ev(account, uid, mid, text="", kind="message"):
    return Event(kind=kind, user_id=uid, account_id=account, mid=mid, text=text)


def test_routing_by_account(tmp_path, monkeypatch):
    async def go():
        proc, (ast, shym) = make_proc(tmp_path, monkeypatch)
        await proc.handle_event(ev("AST", "U1", "a1", "привет из Астаны"))
        await proc.handle_event(ev("SHY", "U2", "s1", "привет из Шымкента"))
        await settle(proc)
        assert [u for u, _ in ast.ig.sent] == ["U1"]          # ответ ушёл через маркер Астаны
        assert [u for u, _ in shym.ig.sent] == ["U2"]         # и через маркер Шымкента
        assert "Шымкента" in shym.ig.sent[0][1] and "Астаны" in ast.ig.sent[0][1]
        assert len(ast.agent.calls) == 1 and len(shym.agent.calls) == 1
        assert proc.store.history("shymkent:U2", 5) and not proc.store.history("U2", 5)
    asyncio.run(go())


def test_manager_pause_is_per_branch(tmp_path, monkeypatch):
    async def go():
        proc, (ast, shym) = make_proc(tmp_path, monkeypatch)
        await proc.handle_event(ev("SHY", "U2", "m1", "Здравствуйте, менеджер", kind="echo"))
        await settle(proc)
        assert proc.store.get_conv("shymkent:U2")["paused_until"] is not None
        await proc.handle_event(ev("SHY", "U2", "s2", "а цена?"))
        await proc.handle_event(ev("AST", "U1", "a2", "а цена?"))
        await settle(proc)
        assert shym.ig.sent == [] and len(ast.ig.sent) == 1
    asyncio.run(go())


def test_own_account_messages_ignored(tmp_path, monkeypatch):
    async def go():
        proc, (ast, shym) = make_proc(tmp_path, monkeypatch)
        await proc.handle_event(ev("SHY", "SHY", "x1", "сам себе"))
        await settle(proc)
        assert shym.ig.sent == []
    asyncio.run(go())


def test_account_id_resolved_from_token(tmp_path, monkeypatch):
    async def go():
        proc, (ast, shym) = make_proc(tmp_path, monkeypatch)
        shym.account_ids.clear()
        shym.branch.account_id = ""

        async def me():
            return {"user_id": "1784SHY", "id": "app-scoped", "username": "nur.shymkent"}
        shym.ig.me = me
        await proc.handle_event(ev("1784SHY", "U9", "r1", "сәлем"))
        await settle(proc)
        assert [u for u, _ in shym.ig.sent] == ["U9"] and ast.ig.sent == []
    asyncio.run(go())


def test_unknown_account_falls_back_to_primary(tmp_path, monkeypatch):
    async def go():
        proc, (ast, shym) = make_proc(tmp_path, monkeypatch)
        await proc.handle_event(ev("???", "U5", "z1", "hi"))
        await settle(proc)
        assert [u for u, _ in ast.ig.sent] == ["U5"]
    asyncio.run(go())


def test_branches_note_lists_others():
    a = Branch("astana", "Астана", "в Астане", "NUR", "@nurelectronics.kz", "Төле би 52", "10–20", primary=True)
    b = Branch("shymkent", "Шымкент", "в Шымкенте", "NUR", "@nur.shym", "Тауке хана 1", "10–21")
    note = branches_note(b, [a, b])
    assert "филиала в Шымкенте" in note and "Төле би 52" in note and "Тауке хана" not in note


def test_dual_sim_and_discontinued_flag():
    from app.catalog import Catalog, normalize_sim
    from app.tools import ToolExecutor
    assert normalize_sim("2 SIM (Dual SIM)") == "dual" and normalize_sim("Dual SIM") == "dual"
    assert normalize_sim("1 SIM + eSIM") == "sim+esim" and normalize_sim("только eSIM") == "esim"
    rows = [["Модель", "Память", "Тип SIM", "Цена", "Показать"],
            ["iPhone 17 Pro", "256GB", "1 SIM + eSIM", "600 000 ₸", "Да"],
            ["iPhone 17 Pro", "256GB", "2 SIM (Dual SIM)", "660 000 ₸", "Да"],
            ["iPhone 16 Pro Max", "256GB", "", "650 000 ₸", "Да"]]
    cat = Catalog({"iphone": rows})
    r = json.loads(ToolExecutor(cat, {}).run("get_exact_price", {"model": "iPhone 17 Pro", "memory": "256",
                                                                  "sim": "dual"})[0])
    assert [i["price_kzt"] for i in r["items"]] == [660000]
    r = json.loads(ToolExecutor(cat, {}).run("get_exact_price", {"model": "iPhone 17 Pro", "memory": "256",
                                                                  "sim": "sim+esim"})[0])
    assert [i["price_kzt"] for i in r["items"]] == [600000]
    # Астана: 16 Pro Max «снят с производства»; Шымкент продаёт по прайсу
    on = json.loads(ToolExecutor(cat, {}).run("get_exact_price", {"model": "iPhone 16 Pro Max"})[0])
    off = json.loads(ToolExecutor(cat, {}, discontinued_rule=False).run("get_exact_price",
                                                                       {"model": "iPhone 16 Pro Max"})[0])
    assert on["status"] == "discontinued" and off["items"][0]["price_kzt"] == 650000


def test_prompt_discontinued_section_per_branch(tmp_path, monkeypatch):
    s = settings(tmp_path)
    data = dict(SHYM, discontinued_rule=False)
    monkeypatch.setenv("IG_ACCESS_TOKEN_SHYMKENT", "x")
    ast, shym = load_branches(s, branches_file(tmp_path, [data]))
    p_ast, p_shym = render_prompt(TPL, s, ast, [ast, shym]), render_prompt(TPL, s, shym, [ast, shym])
    assert "снят с производства" in p_ast and "<!--" not in p_ast
    assert "снят с производства" not in p_shym and "прошлых поколений" in p_shym and "<!--" not in p_shym
