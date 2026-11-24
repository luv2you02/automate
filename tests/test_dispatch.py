"""배차 엔진 순수 계산 테스트 (PRD 8장 T1~T7, T15~T17 + 경계값).

모든 결과 차량은 validate_truck 으로 R1·R2·R3 를 다시 검사한다.
"""
import random

import pytest

from app.config import DispatchRules
from app.services.dispatch import Item, Truck, classify, max_ton_for, plan, validate_truck

R = DispatchRules()
_id = [0]


def it(ton, *, wide=False, long=False, region="R1", source="route", cust="C1", dlv=None) -> Item:
    _id[0] += 1
    return Item(_id[0], f"P{_id[0]}", ton * 1000, "WIDE" if wide else "NORMAL", long, region, source, cust,
                dlv or f"D{cust}")


def run(items):
    res = plan(items, R, "2026-11-24")
    for t in res.trucks:
        assert validate_truck(t, R) == [], (t.truck_id, validate_truck(t, R))
    assigned = {i.id for t in res.trucks for i in t.items}
    assert assigned.isdisjoint(res.unassigned)
    assert assigned | set(res.unassigned) == {i.id for i in items}  # 빠지거나 중복된 제품 없음
    return res


def test_T1_wide_13_13():
    res = run([it(13, wide=True), it(13, wide=True)])
    assert len(res.trucks) == 1 and res.trucks[0].total_kg == 26000 and res.trucks[0].truck_id.endswith("W01")


def test_T2_wide_13_5_x2():
    a, b = it(13.5, wide=True), it(13.5, wide=True)
    res = run([a, b])
    assert res.trucks == [] and res.unassigned == {a.id: "UNDER_MIN", b.id: "UNDER_MIN"}


def test_T3_normal_13_5_x2():
    res = run([it(13.5), it(13.5)])
    assert len(res.trucks) == 1 and res.trucks[0].total_kg == 27000 and res.trucks[0].truck_id.endswith("N01")


def test_T4_normal_with_long_caps_at_26():
    res = run([it(13.5), it(13.5, long=True)])
    assert res.trucks == [] and set(res.unassigned.values()) == {"UNDER_MIN"}


def test_T5_no_mixing_wide_and_normal():
    res = run([it(12.5, wide=True), it(12.5)])
    assert res.trucks == [] and len(res.unassigned) == 2


def test_T6_single_over_max():
    a = it(27, wide=True)
    assert run([a]).unassigned == {a.id: "OVER_MAX"}
    b = it(26.5, long=True)  # 장척 일반재 단독 26.5t > 26t
    assert run([b]).unassigned == {b.id: "OVER_MAX"}
    c = it(26.5)  # 장척 아닌 일반재는 27t까지 → 혼자서 차량 1대
    res = run([c])
    assert len(res.trucks) == 1 and not res.unassigned


def test_T7_and_length_boundaries():
    assert classify(3400, 10000, R) == ("NORMAL", False)
    assert classify(3401, 10000, R) == ("WIDE", False)
    assert classify(2000, 17000, R) == ("NORMAL", False)
    assert classify(2000, 17001, R) == ("NORMAL", True)
    assert max_ton_for("WIDE", False, R) == 26
    assert max_ton_for("NORMAL", False, R) == 27
    assert max_ton_for("NORMAL", True, R) == 26


@pytest.mark.parametrize("tons,ok", [((25,), True), ((24.999,), False), ((13, 14), True), ((13.5, 13.6), False)])
def test_weight_range_boundaries(tons, ok):
    t = Truck("x", "NORMAL", "R1", "route", [it(x) for x in tons])
    assert (validate_truck(t, R) == []) is ok


def test_T15_different_city_not_mixed():
    a = it(13, region="경상남도 창원시", source="address")
    b = it(13, region="경상남도 김해시", source="address")
    res = run([a, b])
    assert res.trucks == [] and set(res.unassigned.values()) == {"UNDER_MIN"}


def test_T16_same_city_different_customers_mix():
    res = run([it(13, region="경상남도 창원시", source="address", cust="A"),
               it(13, region="경상남도 창원시", source="address", cust="B")])
    assert len(res.trucks) == 1 and res.trucks[0].total_kg == 26000


def test_T17_same_customer_first():
    a1, a2, a3 = (it(13, cust="A"), it(13, cust="A"), it(12, cust="A"))
    b = it(13, cust="B")
    res = run([a1, a2, a3, b])
    assert len(res.trucks) == 2 and not res.unassigned
    # 한 차량은 A사 제품만으로 채워진다 (고객사 우선)
    assert any({i.customer_code for i in t.items} == {"A"} for t in res.trucks)


def test_address_and_route_regions_never_mix():  # T30 의 엔진 부분
    res = run([it(13, region="경상남도 창원시", source="address"), it(13, region="M1EN64", source="route")])
    assert res.trucks == []


def test_same_destination_preferred_over_region():
    # 같은 지역, 고객사가 모두 다르지만 도착지 X 가 둘 → 그 둘이 먼저 묶인다
    x1 = it(12.6, cust="A", dlv="X")
    x2 = it(12.6, cust="B", dlv="X")
    y1 = it(12.6, cust="C", dlv="Y")
    res = run([x1, y1, x2])
    assert len(res.trucks) == 1
    assert {i.dlv_code for i in res.trucks[0].items} == {"X"}


def test_existing_truck_ids_not_reused():
    res = plan([it(13), it(13)], R, "2026-11-24", existing_ids={"2026-11-24-N01"})
    assert res.trucks[0].truck_id == "2026-11-24-N02"


def test_random_loads_always_valid_and_fast():
    rng = random.Random(7)
    items = [it(round(rng.uniform(0.3, 6.0), 3), wide=rng.random() < 0.1, long=rng.random() < 0.05,
                region=rng.choice(["R1", "R2", "R3", "R4"]), cust=rng.choice("ABCDEFG")) for _ in range(1200)]
    res = run(items)
    assert res.elapsed_sec < 3.0
    # 그룹별로 남은 중량이 25t 이상이면 차량을 더 만들 수 있었다는 뜻 → 그런 그룹은 없어야 한다
    left: dict = {}
    by_id = {i.id: i for i in items}
    for pid, reason in res.unassigned.items():
        i = by_id[pid]
        if reason == "UNDER_MIN":
            left[(i.category, i.region)] = left.get((i.category, i.region), 0) + i.kg
    assert all(kg < 25000 for kg in left.values()), left
