"""Наличие SFON: новые (наличие/последняя штука/альтернативы/нет), б/у с фото, отправка фото, изоляция филиалов."""
import asyncio
import json
from types import SimpleNamespace as NS

from app.catalog import Catalog
from app.stock import StockService, base_model
from app.tools import ToolExecutor

NEW = [
    {"modelName": "iPhone 17 Pro Max eSIM", "memoryName": "256 GB", "colorName": "Cosmic Orange", "count": 3,
     "averagePrice": 640000, "state": "NEW", "photos": [], "storeName": "NUR Астана"},
    {"modelName": "iPhone 17 Pro Max eSIM", "memoryName": "512 GB", "colorName": "Deep Blue", "count": 1,
     "averagePrice": 740000, "state": "NEW", "photos": []},
    {"modelName": "iPhone 17 Pro Max SIM+eSIM", "memoryName": "256 GB", "colorName": "Silver", "count": 1,
     "averagePrice": 690000, "state": "NEW", "photos": []},
    {"modelName": "iPhone 17 Pro eSIM", "memoryName": "256 GB", "colorName": "Silver", "count": 2,
     "averagePrice": 600000, "state": "NEW", "photos": []},
    {"modelName": "iPhone 16 Pro Max", "memoryName": "512 GB", "colorName": "Black", "count": 1,
     "averagePrice": 700000, "state": "NEW", "photos": []},
]
OLD = [
    {"modelName": "iPhone 15 Pro", "memoryName": "256 GB", "colorName": "Natural", "averagePrice": 330000,
     "batteryState": 88, "year": 2023, "productId": 501, "state": "OLD",
     "description": "Мелкие потёртости на рамке. IMEI 356789104523411",
     "photos": [{"url": f"https://shop.sfon.kz/storage/a{i}.jpg"} for i in range(6)]},
    {"modelName": "iPhone 15 Pro", "memoryName": "128 GB", "colorName": "Black", "averagePrice": 290000,
     "batteryState": 79, "year": 2023, "productId": 502, "state": "OLD", "description": "", "photos": []},
]


class FakeSfon:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    async def search(self, flt, page=0, size=100):
        self.calls.append(flt)
        if self.fail:
            from app.stock import SfonError
            raise SfonError(401, self.fail, "ключ истёк")
        rows = NEW if flt["state"] == "NEW" else OLD
        return {"content": rows, "last": True}

    async def company(self):
        return {"companyDto": {"companyName": "NUR"}, "stores": [{"id": 1, "storeName": "Астана"}]}

    async def old_product(self, pid):
        return {"productInfo": None}

    async def aclose(self):
        pass


def svc(fail=None):
    return StockService("astana", FakeSfon(fail), whatsapp="https://wa.me/77028081111")


def run(coro):
    return asyncio.run(coro)


def test_base_model_strips_sim():
    assert base_model("iPhone 17 Pro Max eSIM") == "iphone 17 pro max"
    assert base_model("iPhone 17 Pro (SIM+eSIM)") == "iphone 17 pro"
    assert base_model("iPhone 17 Pro 2 SIM") == "iphone 17 pro"
    assert base_model("iPhone 17") == "iphone 17"


def test_check_new_in_stock_last_one_alternatives_and_missing():
    s = svc()
    r = run(s.check_new("iPhone 17 Pro Max", "256", "orange", "esim"))
    assert r["in_stock"] and r["variants"][0]["count"] == 3 and "last_one" not in r["variants"][0]
    r = run(s.check_new("iPhone 17 Pro Max", "512", "blue"))
    assert r["in_stock"] and r["variants"][0]["last_one"] and "последняя штука" in r["note"]
    assert "wa.me/77028081111" in r["note"]
    r = run(s.check_new("iPhone 17 Pro Max", "1TB"))
    assert r["status"] == "no_such_config" and len(r["alternatives"]) == 3
    assert "Вам важен именно" in r["note"]
    # «17 Pro» не путается с «17 Pro Max»
    r = run(s.check_new("iPhone 17 Pro", "256"))
    assert [(v["model"], v["sim"]) for v in r["variants"]] == [("iPhone 17 Pro", "только eSIM")]
    r = run(s.check_new("iPhone 18 Pro"))
    assert r["status"] == "out_of_stock" and "WhatsApp" in r["note"]
    # цены новых из SFON не отдаются — только из прайса
    assert "price" not in json.dumps(run(s.check_new("iPhone 17 Pro Max")), ensure_ascii=False).replace("get_exact_price", "")


def test_search_used_masks_and_photos():
    s = svc()
    r = run(s.search_used("15 про"))
    assert r["found"] == 2 and r["units"][0]["product_id"] == 502  # дешевле первым
    u = next(x for x in r["units"] if x["product_id"] == 501)
    assert u["price_kzt"] == 330000 and u["battery_percent"] == 88 and u["photos"] == 6
    assert "356789104523411" not in u["description"] and "потёртости" in u["description"]
    assert "не придумывай" in r["note"]
    assert run(s.search_used("15 про", battery_from=85))["found"] == 1


def test_sfon_unavailable_sends_to_manager():
    s = svc(fail="API_KEY_EXPIRED")
    r = run(s.check_new("iPhone 17 Pro"))
    assert r["status"] == "unavailable" and "менеджер" in r["note"]


def ex_with_stock(discontinued=True):
    cat = Catalog({"iphone": [["Модель", "Память", "Цена", "Показать"], ["iPhone 17 Pro", "256GB", "600 000 ₸", "Да"]]})
    return ToolExecutor(cat, {}, discontinued_rule=discontinued, stock=svc())


def test_tools_photos_queue_and_discontinued():
    ex = ex_with_stock()
    out, err = run(ex.arun("send_used_photos", {"product_id": 501}))
    r = json.loads(out)
    assert r["status"] == "queued" and r["photos"] == 4 and len(ex.media[0]["urls"]) == 4
    r = json.loads(run(ex.arun("send_used_photos", {"product_id": 502}))[0])
    assert r["status"] == "no_photos" and "WhatsApp" in r["note"]
    # новый 16 Pro Max — «снят с производства» (правило Астаны), б/у 15 Pro — можно
    r = json.loads(run(ex.arun("check_stock", {"model": "iPhone 16 Pro Max"}))[0])
    assert r["status"] == "discontinued"
    r = json.loads(run(ex.arun("search_used", {"model": "iPhone 15 Pro"}))[0])
    assert r["found"] == 2
    # рассрочка на б/у считается от цены экземпляра
    r = json.loads(run(ex.arun("calculate_kaspi_installment", {"basis": "used", "product_id": 501}))[0])
    assert r["cash_price"] == 330000 and r["installment_price"] == 396000
    # обычные инструменты через arun работают как раньше
    r = json.loads(run(ex.arun("get_exact_price", {"model": "iPhone 17 Pro", "memory": "256"}))[0])
    assert r["items"][0]["price_kzt"] == 600000


def test_tradein_topup_for_used():
    ex = ex_with_stock()
    ex.state["tradein"] = {"status": "ok", "estimate": 100000, "device": {"model": "iPhone 12"}}
    r = json.loads(run(ex.arun("calculate_tradein_topup", {"used_product_id": 501}))[0])
    assert r["topup"] == 230000 and "(б/у)" in r["new_device"]


def test_no_stock_tools_without_key(monkeypatch):
    from dataclasses import replace
    from app.agent import SalesAgent
    from app.config import Settings
    monkeypatch.delenv("SMARTPHONE_SYSTEM_API_KEY_SHYMKENT", raising=False)
    assert StockService.from_env("shymkent") is None
    a = SalesAgent(replace(Settings(), web_search_enabled=False), client=NS(messages=None))
    assert "check_stock" not in [t["name"] for t in a._tools()]
    b = SalesAgent(replace(Settings(), web_search_enabled=False), client=NS(messages=None), stock=svc())
    names = [t["name"] for t in b._tools()]
    assert {"check_stock", "search_used", "send_used_photos"} <= set(names)
    assert "НАЛИЧИЕ ПО УЧЁТНОЙ СИСТЕМЕ" in b._system({}, None)[0]["text"]
    assert "НАЛИЧИЕ ПО УЧЁТНОЙ СИСТЕМЕ" not in a._system({}, None)[0]["text"]


def test_processor_sends_photos_after_text(tmp_path):
    from app.agent import AgentResult
    from app.instagram import Event
    from tests.test_flow import FakeAgent, FakeIG, make, settle

    class IG(FakeIG):
        def __init__(self):
            super().__init__()
            self.images = []

        async def send_image(self, uid, url):
            self.images.append(url)
            return f"img-{len(self.images)}"

    class Agent(FakeAgent):
        async def reply(self, history, state, catalog, images=None, **kw):
            return AgentResult("Отправляю фото 👇", state, ["send_used_photos"],
                               [{"label": "iPhone 15 Pro, 256GB", "urls": ["u1", "u2"], "product_id": 501}])

    async def go():
        proc, _, _ = make(tmp_path)
        proc.ig, proc.agent = IG(), Agent()
        await proc.handle_event(Event(kind="message", user_id="U1", account_id="BIZ", mid="m1", text="да, скиньте"))
        await settle(proc)
        assert proc.ig.sent[0][1] == "Отправляю фото 👇" and proc.ig.images == ["u1", "u2"]
        # echo собственного фото не ставит бота на паузу
        assert proc.store.message_role_by_mid("img-1") == "assistant"
        await proc.handle_event(Event(kind="echo", user_id="U1", account_id="BIZ", mid="img-2"))
        await settle(proc)
        assert proc.store.get_conv("U1")["paused_until"] is None
        assert any("отправил клиенту фото" in (m["content"] or "") for m in proc.store.history("U1", 10))
    asyncio.run(go())


def test_real_sfon_names_and_russian_colors():
    rows = [
        {"modelName": "17 Pro Max e-Sim", "memoryName": "256GB", "colorName": "ORANGE", "count": 3, "state": "NEW"},
        {"modelName": "17 Pro Max Sim", "memoryName": "256GB", "colorName": "Blue", "count": 2, "state": "NEW"},
        {"modelName": "17 Pro e-Sim", "memoryName": "256GB", "colorName": "Blue", "count": 1, "state": "NEW"},
        {"modelName": "17 Sim", "memoryName": "256GB", "colorName": "SAGE", "count": 1, "state": "NEW"},
    ]

    class F(FakeSfon):
        async def search(self, flt, page=0, size=100):
            return {"content": rows if flt["state"] == "NEW" else [], "last": True}

    s = StockService("astana", F(), whatsapp="wa")
    r = run(s.check_new("iPhone 17 Pro Max", "256", "оранжевый", "esim"))
    assert r["in_stock"] and r["variants"][0]["model"] == "iPhone 17 Pro Max" and r["variants"][0]["sim"] == "только eSIM"
    r = run(s.check_new("iPhone 17 Pro Max", "256", "Cosmic Orange", "sim+esim"))
    assert r["status"] == "no_such_config" and {a["color"] for a in r["alternatives"]} == {"ORANGE", "Blue"}
    r = run(s.check_new("iPhone 17 Pro", "256", "синий"))
    assert r["in_stock"] and r["variants"][0]["last_one"]
    r = run(s.check_new("iPhone 17", "256", "зелёный шалфей"))
    assert r["in_stock"] and r["variants"][0]["model"] == "iPhone 17"
