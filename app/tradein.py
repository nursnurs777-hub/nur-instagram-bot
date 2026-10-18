"""Предварительная оценка Trade-in и расчёт доплаты. Только чистые функции — никакой арифметики на стороне модели."""
from __future__ import annotations

from dataclasses import dataclass

# Утверждённые вычеты (раздел 11 системного промпта). Меняются здесь.
BATTERY_NO_DEDUCTION_FROM = 85
BATTERY_RULES = [  # (минимум включительно, вычет)
    (80, 10_000),
    (75, 15_000),
]
BATTERY_MANAGER_BELOW = 75
DEDUCT_SCRATCHES = 5_000
DEDUCT_NO_BOX = 10_000
DEDUCT_REPAIRED = 5_000


@dataclass
class Condition:
    battery_percent: int
    has_box: bool
    minor_scratches: bool
    was_repaired: bool
    screen_or_body_damage: bool = False
    all_functions_work: bool = True


def quote(base_price: int, cond: Condition) -> dict:
    """Возвращает {'status': 'ok', ...} или {'status': 'manager_review', 'reasons': [...]}."""
    reasons = []
    if cond.battery_percent < BATTERY_MANAGER_BELOW:
        reasons.append(f"аккумулятор {cond.battery_percent}% (ниже {BATTERY_MANAGER_BELOW}%)")
    if cond.screen_or_body_damage:
        reasons.append("повреждения экрана или корпуса")
    if not cond.all_functions_work:
        reasons.append("не все функции работают")
    if reasons:
        return {"status": "manager_review", "reasons": reasons,
                "message": "Автоматическую сумму не называть — нужна индивидуальная оценка менеджером."}

    deductions: list[dict] = []
    if cond.battery_percent < BATTERY_NO_DEDUCTION_FROM:
        for minimum, amount in BATTERY_RULES:
            if cond.battery_percent >= minimum:
                deductions.append({"reason": f"аккумулятор {cond.battery_percent}%", "amount": amount})
                break
    if cond.minor_scratches:
        deductions.append({"reason": "мелкие царапины", "amount": DEDUCT_SCRATCHES})
    if not cond.has_box:
        deductions.append({"reason": "нет коробки", "amount": DEDUCT_NO_BOX})
    if cond.was_repaired:
        deductions.append({"reason": "был ремонт", "amount": DEDUCT_REPAIRED})

    total = sum(d["amount"] for d in deductions)
    return {
        "status": "ok",
        "base_price": base_price,
        "deductions": deductions,
        "total_deduction": total,
        "estimate": max(base_price - total, 0),
    }


def topup(new_price: int, tradein_estimate: int) -> dict:
    diff = new_price - tradein_estimate
    return {
        "new_price": new_price,
        "tradein_estimate": tradein_estimate,
        "topup": max(diff, 0),
        "tradein_exceeds_price": diff < 0,
    }
