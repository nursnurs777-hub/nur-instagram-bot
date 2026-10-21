import json
from pathlib import Path

import pytest

from app.catalog import Catalog, normalize_memory, normalize_sim, norm, parse_price, parse_text_dump
from app.tools import ToolExecutor, state_summary

FIXTURE = Path(__file__).parent / "fixtures" / "price_nur.txt"


@pytest.fixture(scope="module")
def catalog():
    return Catalog(parse_text_dump(FIXTURE.read_text(encoding="utf-8")))


def run(ex, name, **args):
    out, err = ex.run(name, args)
    return json.loads(out), err


# ------------------------------------------------------------ разбор

def test_parse_price_formats():
    assert parse_price("480 000 ₸") == 480000
    assert parse_price("₸1 050 000") == 1050000
    assert parse_price("75000") == 75000
    assert parse_price("") is None


def test_memory_and_sim():
    assert normalize_memory("256BG") == "256GB"
    assert normalize_memory("1тб") == "1TB"
    assert normalize_memory("512 гб") == "512GB"
    assert normalize_memory("16/512GB") == "16/512GB"
    assert normalize_sim("1 SIM + eSIM") == "sim+esim"
    assert normalize_sim("SIM + eSIM") == "sim+esim"
    assert normalize_sim("только eSIM") == "esim"
    assert normalize_sim("есим") == "esim"
    assert normalize_sim("") is None


def test_norm_aliases():
    assert norm("Айфон 18 про макс") == "iphone 18 pro max"
    assert norm("iPhone 17 ProMax") == "iphone 17 pro max"
    assert norm("PS 5") == "playstation 5"
    assert norm("макбук эйр 13”") == "macbook air 13"


def test_hidden_rows_skipped(catalog):
    names = [p.model for p in catalog.products]
    assert "Dyson Supersonic HD-17" not in names  # Показать = Нет
    assert "Dyson Airstrait HT 01" in names


def test_counts(catalog):
    st = catalog.stats()
    assert st["by_category"]["iphone"] == 45
    assert st["tradein"] == 93


# ------------------------------------------------------------ розничные цены

def test_exact_price_requires_sim_choice(catalog):
    ex = ToolExecutor(catalog, {})
    r, _ = run(ex, "get_exact_price", model="iPhone 17 Pro", memory="256")
    assert r["found"] == 2
    assert {i["price_kzt"] for i in r["items"]} == {630000, 590000}


def test_exact_price_esim(catalog):
    ex = ToolExecutor(catalog, {})
    r, _ = run(ex, "get_exact_price", model="айфон 18 про макс", memory="512 гб", sim="esim")
    assert r["found"] == 1 and r["items"][0]["price_kzt"] == 950000


def test_no_wrong_generation(catalog):
    r = catalog.search("iphone 17 pro 256")
    assert r and all("17" in p.model for p in r)


def test_empty_price_is_flagged(catalog):
    ex = ToolExecutor(catalog, {})
    r, _ = run(ex, "get_exact_price", model="iPhone Duo", memory="256GB", sim="esim")
    assert r["status"] == "expected" and "items" not in r


def test_search_misc(catalog):
    assert catalog.search("PS5")[0].price == 330000
    assert catalog.search("ipad air 11 256")[0].price == 400000
    assert catalog.search("apple watch se 44")[0].price == 140000
    assert catalog.search("айфон 18 про макс 512 есим")[0].price == 950000


def test_missing_product(catalog):
    ex = ToolExecutor(catalog, {})
    r, _ = run(ex, "get_exact_price", model="iPhone 15 Pro", memory="256")
    assert r["found"] == 0  # 15 Pro есть только в таблице выкупа, не в рознице


# ------------------------------------------------------------ trade-in

def test_tradein_example_from_prompt(catalog):
    state = {}
    ex = ToolExecutor(catalog, state)
    r, _ = run(ex, "quote_iphone_tradein", model="iPhone 15 Pro", memory="256", battery_percent=82,
               has_box=True, minor_scratches=True, was_repaired=False, screen_or_body_damage=False,
               all_functions_work=True)
    assert r["status"] == "ok"
    assert "base_price" not in r and "deductions" not in r  # вычеты клиенту не раскрываем
    assert r["estimate"] == 230000 - 10000 - 5000
    assert state["tradein"]["base_price"] == 230000
    assert state["tradein"]["estimate"] == 215000

    # клиент спрашивает доплату — данные старого телефона не передаём
    r2, _ = run(ex, "calculate_tradein_topup", new_model="iPhone 18 Pro Max", new_memory="256")
    assert r2["status"] == "need_sim"
    r3, _ = run(ex, "calculate_tradein_topup", new_model="iPhone 18 Pro Max", new_memory="256", new_sim="sim+esim")
    assert r3["topup"] == 830000 - 215000
    assert "615 000" in state_summary(state)


def test_tradein_all_deductions(catalog):
    ex = ToolExecutor(catalog, {})
    r, _ = run(ex, "quote_iphone_tradein", model="13 про", memory="128", battery_percent=76, has_box=False,
               minor_scratches=True, was_repaired=True, screen_or_body_damage=False, all_functions_work=True)
    assert r["estimate"] == 120000 - 15000 - 5000 - 10000 - 5000


@pytest.mark.parametrize("kw", [
    {"battery_percent": 74},
    {"screen_or_body_damage": True},
    {"all_functions_work": False},
])
def test_tradein_manager_review(catalog, kw):
    args = dict(model="iPhone 14", memory="128", battery_percent=90, has_box=True, minor_scratches=False,
                was_repaired=False, screen_or_body_damage=False, all_functions_work=True)
    args.update(kw)
    state = {}
    r, _ = run(ToolExecutor(catalog, state), "quote_iphone_tradein", **args)
    assert r["status"] == "manager_review"
    assert "estimate" not in r
    r2, _ = run(ToolExecutor(catalog, state), "calculate_tradein_topup", new_model="iPhone 17", new_memory="256")
    assert "error" in r2


def test_tradein_17_needs_sim(catalog):
    ex = ToolExecutor(catalog, {})
    r, _ = run(ex, "quote_iphone_tradein", model="iPhone 17 Pro", memory="256", battery_percent=100,
               has_box=True, minor_scratches=False, was_repaired=False, screen_or_body_damage=False,
               all_functions_work=True)
    assert r["status"] == "need_sim"


def test_tradein_samsung_rejected(catalog):
    r, _ = run(ToolExecutor(catalog, {}), "quote_iphone_tradein", model="Samsung S24", memory="256",
               battery_percent=90, has_box=True, minor_scratches=False, was_repaired=False,
               screen_or_body_damage=False, all_functions_work=True)
    assert r["status"] == "not_accepted"


def test_topup_without_quote(catalog):
    r, _ = run(ToolExecutor(catalog, {}), "calculate_tradein_topup", new_model="iPhone 17", new_memory="256")
    assert "error" in r


def test_battery_boundaries():
    from app.tradein import Condition, quote
    base = dict(has_box=True, minor_scratches=False, was_repaired=False)
    assert quote(100000, Condition(85, **base))["estimate"] == 100000
    assert quote(100000, Condition(84, **base))["estimate"] == 90000
    assert quote(100000, Condition(80, **base))["estimate"] == 90000
    assert quote(100000, Condition(79, **base))["estimate"] == 85000
    assert quote(100000, Condition(75, **base))["estimate"] == 85000
    assert quote(100000, Condition(74, **base))["status"] == "manager_review"


def test_kaspi_installment_product(catalog):
    r, _ = run(ToolExecutor(catalog, {}), "calculate_kaspi_installment", basis="product",
               model="iPhone 17 Pro", memory="256", sim="sim+esim")
    assert r["cash_price"] == 630000
    assert r["installment_price"] == 756000
    assert r["monthly_payment"] == 31500 and r["months"] == 24


def test_kaspi_installment_needs_details(catalog):
    r, _ = run(ToolExecutor(catalog, {}), "calculate_kaspi_installment", basis="product", model="iPhone 17 Pro")
    assert r["status"] == "need_details"


def test_kaspi_installment_topup(catalog):
    state = {}
    ex = ToolExecutor(catalog, state)
    run(ex, "quote_iphone_tradein", model="iPhone 15 Pro", memory="256", battery_percent=82, has_box=True,
        minor_scratches=True, was_repaired=False, screen_or_body_damage=False, all_functions_work=True)
    run(ex, "calculate_tradein_topup", new_model="iPhone 18 Pro Max", new_memory="256", new_sim="sim+esim")
    r, _ = run(ex, "calculate_kaspi_installment", basis="tradein_topup")
    assert r["cash_price"] == 615000 and r["installment_price"] == 738000 and r["monthly_payment"] == 30750


def test_kaspi_other_term_rejected(catalog):
    r, _ = run(ToolExecutor(catalog, {}), "calculate_kaspi_installment", basis="product",
               model="PlayStation 5 Slim", months=12)
    assert "error" in r


def test_installment_rounding():
    from app.tradein import installment
    r = installment(405000, 20, 24)
    assert r["installment_price"] == 486000 and r["monthly_payment"] == 20250
    assert installment(100001, 20, 24)["monthly_payment"] == 5001


# ------------------------------------------------------------ снятые с производства Pro

@pytest.mark.parametrize("q,label", [
    ("айфон 15 про", "iPhone 15 Pro"),
    ("iPhone 13 Pro Max 256", "iPhone 13 Pro Max"),
    ("16promax", "iPhone 16 Pro Max"),
    ("14 про макс", "iPhone 14 Pro Max"),
])
def test_discontinued_pro(catalog, q, label):
    ex = ToolExecutor(catalog, {})
    r, _ = run(ex, "search_products", query=q)
    assert r["status"] == "discontinued" and r["model"] == label
    assert r["reply_to_client"].startswith(f"{label} официально снят с производства")
    r2, _ = run(ex, "get_exact_price", model=q)
    assert r2["status"] == "discontinued"


@pytest.mark.parametrize("q", ["iPhone 17 Pro", "iPhone 15", "iPhone 16 Plus", "MacBook Pro 14", "iPad Pro 13", "айфон 18 про макс"])
def test_not_discontinued(q):
    from app.tools import discontinued_iphone
    assert discontinued_iphone(q) is None


def test_discontinued_still_accepted_in_tradein(catalog):
    r, _ = run(ToolExecutor(catalog, {}), "quote_iphone_tradein", model="iPhone 15 Pro", memory="256",
               battery_percent=90, has_box=True, minor_scratches=False, was_repaired=False,
               screen_or_body_damage=False, all_functions_work=True)
    assert r["status"] == "ok"


# ------------------------------------------------------------ iPhone Duo — ожидается

@pytest.mark.parametrize("q", ["iPhone Duo", "айфон дуо 512", "iphone duo 1tb esim"])
def test_iphone_duo_expected(catalog, q):
    ex = ToolExecutor(catalog, {})
    for name, kw in (("search_products", {"query": q}), ("get_exact_price", {"model": q})):
        r, _ = run(ex, name, **kw)
        assert r["status"] == "expected" and r["expected_model"] == "iPhone Duo"
        assert "25 октября" in r["expected_arrival"]
        assert "items" not in r and "price_kzt" not in str(r)


def test_iphone_duo_no_installment_or_topup(catalog):
    state = {}
    ex = ToolExecutor(catalog, state)
    r, _ = run(ex, "calculate_kaspi_installment", basis="product", model="iPhone Duo", memory="256GB")
    assert r["status"] == "expected"
    run(ex, "quote_iphone_tradein", model="iPhone 15 Pro", memory="256", battery_percent=90, has_box=True,
        minor_scratches=False, was_repaired=False, screen_or_body_damage=False, all_functions_work=True)
    r2, _ = run(ex, "calculate_tradein_topup", new_model="iPhone Duo", new_memory="256")
    assert r2["status"] == "expected"


def test_iphone_duo_rule_off_when_priced():
    from app.catalog import Catalog
    from app.tools import ToolExecutor as TE
    cat = Catalog({"iPhone": [["Модель", "Память", "SIM", "Цена", "Показать"],
                              ["iPhone Duo", "256GB", "только eSIM", "900 000", "Да"]]})
    r, _ = run(TE(cat, {}), "get_exact_price", model="iPhone Duo", memory="256GB")
    assert "status" not in r and r["items"][0]["price_kzt"] == 900000
