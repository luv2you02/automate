import pytest

from app.services.parser import (
    ParseError, calc_length_mm, detect_sheet_kind, match_products_to_orders, normalize_name, parse_shipped,
    parse_workbook,
)
from tests.helpers import O, P, make_xlsx


def test_detect_sheet_kind():
    assert detect_sheet_kind(["경매번호", "제품번호", "폭 (mm)", "중량"]) == "product"
    assert detect_sheet_kind(["경매번호", "진행상태", "주문번호"]) == "auction"
    assert detect_sheet_kind(["인도처코드", "주소"]) == "delivery"
    assert detect_sheet_kind(["아무거나"]) == "unknown"


def test_sheet_names_do_not_matter():
    content = make_xlsx([P("FA00000001", 5)], product_sheet="제품", auction_sheet="경매결과")
    r = parse_workbook(content, "x.xlsx")
    assert {s.kind for s in r.sheets} == {"product", "auction"}
    assert len(r.products) == 1 and len(r.orders) == 1


def test_codes_kept_as_strings_and_kg():
    r = parse_workbook(make_xlsx([P("FA00000001", 2.887, cust="A43619", auc="2026110202")]), "x.xlsx")
    p = r.products[0]
    assert p["customer_code"] == "A43619" and p["auction_no"] == "2026110202"
    assert p["weight_kg"] == 2887


def test_length_calculated_when_missing():  # T28
    r = parse_workbook(make_xlsx([P("FA00000001", 3.925, t=20, w=2500)]), "x.xlsx")
    p = r.products[0]
    assert p["length_source"] == "calc"
    assert p["length_mm"] == pytest.approx(calc_length_mm(3925, 20, 2500), abs=0.1)
    assert p["length_mm"] == pytest.approx(10000, abs=1)
    assert r.has_length_column is False


def test_length_column_used():
    r = parse_workbook(make_xlsx([P("FA00000001", 5, length=18000)], with_length=True), "x.xlsx")
    assert r.products[0]["length_mm"] == 18000 and r.products[0]["length_source"] == "column"


@pytest.mark.parametrize("v,expected", [
    ("Y", True), ("완료", True), ("출하완료", True), ("2026-11-24", True), ("20261124", True),
    ("N", False), ("미출하", False), (None, False), ("", False),
])
def test_parse_shipped(v, expected):
    assert parse_shipped(v) is expected


def test_name_normalization():  # T31
    assert normalize_name("MH밴딩 울산공장 ") == "MH밴딩 울산공장"
    assert normalize_name("서광철강산업（주） 본사") == "서광철강산업(주) 본사"


def test_data_error_rows():
    content = make_xlsx([P("FA00000001", 5), P("FA00000002", 5)])
    import io

    import pandas as pd

    sheets = pd.read_excel(io.BytesIO(content), sheet_name=None, dtype=str)
    sheets["Sheet1"].loc[1, "중량"] = None
    sheets["Sheet1"].loc[0, "폭 (mm)"] = "abc"
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for n, df in sheets.items():
            df.to_excel(xw, sheet_name=n, index=False)
    r = parse_workbook(buf.getvalue(), "x.xlsx")
    assert len(r.errors) == 2
    reasons = " ".join(e["reason"] for e in r.errors)
    assert "중량" in reasons and "폭" in reasons
    assert len(r.products) == 2  # 제품번호가 있으면 데이터오류 상태로 저장


def test_not_an_auction_file():
    import io

    import pandas as pd

    buf = io.BytesIO()
    pd.DataFrame({"이름": ["a"]}).to_excel(buf, index=False)
    with pytest.raises(ParseError):
        parse_workbook(buf.getvalue(), "x.xlsx")
    with pytest.raises(ParseError):
        parse_workbook(b"not excel", "x.xlsx")


# ---------- 제품 ↔ 주문 매칭 ----------
def _prods(*kgs):
    return [{"product_no": f"P{i}", "weight_kg": kg} for i, kg in enumerate(kgs)]


def _ord(key, status, qty, kg):
    return {"order_key": key, "order_no": key if status == "낙찰확정" else None, "status": status, "qty": qty,
            "weight_kg": kg}


def test_match_single_order():
    m = match_products_to_orders(_prods(1000, 2000), [_ord("D1", "낙찰확정", 2, 3000)])
    assert {v.order_no for v in m.values()} == {"D1"}


def test_match_multiple_orders_by_weight():  # T25 형태
    prods = _prods(5291, 5095, 4909, 2217, 3266)
    m = match_products_to_orders(prods, [_ord("D_A", "낙찰확정", 1, 4909), _ord("D_B", "낙찰확정", 4, 15869)])
    assert m["P2"].order_no == "D_A"
    assert all(m[f"P{i}"].order_no == "D_B" for i in (0, 1, 3, 4))


def test_match_ambiguous_gives_candidates():
    m = match_products_to_orders(_prods(1000, 1000), [_ord("D1", "낙찰확정", 1, 1000), _ord("D2", "낙찰확정", 1, 1000)])
    assert all(v.order_no is None and set(v.candidates) == {"D1", "D2"} for v in m.values())
    assert all(v.order_status == "낙찰확정" for v in m.values())


def test_match_cancelled_rows_ignored():
    m = match_products_to_orders(_prods(1000), [_ord("D1", "낙찰확정", 1, 1000), _ord("x|y|낙찰취소|500", "낙찰취소", 1, 500)])
    assert m["P0"].order_no == "D1"


def test_match_only_cancelled():
    m = match_products_to_orders(_prods(1000), [_ord("k", "낙찰취소", 1, 1000)])
    assert m["P0"].order_status == "낙찰취소"


def test_match_no_orders():
    m = match_products_to_orders(_prods(1000), [])
    assert m["P0"].order_status is None


# ---------- R-01: 일부 주문만 취소 ----------
def test_partial_cancel_assigns_products_to_cancelled_order():
    prods = _prods(13000, 6000, 6000)
    m = match_products_to_orders(prods, [_ord("D1", "낙찰확정", 1, 13000), _ord("k|취소", "낙찰취소", 2, 12000)])
    assert m["P0"].order_no == "D1" and m["P0"].order_status == "낙찰확정"
    assert m["P1"].order_status == m["P2"].order_status == "낙찰취소"
    assert not any(v.cancel_uncertain for v in m.values())


def test_partial_cancel_three_orders():
    prods = _prods(5000, 4000, 3000, 2000)
    orders = [_ord("D1", "낙찰확정", 1, 5000), _ord("D2", "낙찰확정", 2, 5000), _ord("k|취소", "낙찰취소", 1, 4000)]
    m = match_products_to_orders(prods, orders)
    assert m["P0"].order_no == "D1"
    assert m["P1"].order_status == "낙찰취소"
    assert m["P2"].order_no == m["P3"].order_no == "D2"


def test_partial_cancel_uses_absent_products_when_weights_ambiguous():
    # 같은 중량이라 수량·중량만으로는 어느 것이 취소분인지 모름 → 이번 파일에서 빠진 제품으로 판단
    prods = _prods(1000, 1000, 1000)
    orders = [_ord("D1", "낙찰확정", 2, 2000), _ord("k|취소", "낙찰취소", 1, 1000)]
    m = match_products_to_orders(prods, orders, absent={"P2"})
    assert m["P2"].order_status == "낙찰취소"
    assert m["P0"].order_no == m["P1"].order_no == "D1"


def test_partial_cancel_undecidable_is_held():
    prods = _prods(1000, 1000, 1000)
    orders = [_ord("D1", "낙찰확정", 2, 2000), _ord("k|취소", "낙찰취소", 1, 1000)]
    m = match_products_to_orders(prods, orders)  # 빠진 제품 정보도 없음
    assert all(v.cancel_uncertain and v.order_no is None for v in m.values())


def test_original_sample_style_cancel_unaffected():
    # 원본처럼 취소분 제품이 처음부터 제품 시트에 없으면 (살아 있는 수량 == 제품 수) 기존대로
    m = match_products_to_orders(_prods(1000, 2000), [_ord("D1", "낙찰확정", 2, 3000), _ord("k|취소", "낙찰취소", 1, 500)])
    assert {v.order_no for v in m.values()} == {"D1"} and not any(v.cancel_uncertain for v in m.values())
