"""엑셀 파싱 (PRD F1).

입력 파일 하나에 시트 2개가 있고, 시트 이름이 아니라 컬럼 구성으로 종류를 판별한다.
- 제품 시트: `제품번호`, `폭 (mm)` 컬럼 → 제품 1개 = 1행
- 경매 시트: `진행상태`, `주문번호` 컬럼 → (경매번호 × 고객) 1건 = 1행
코드 컬럼(경매번호, 업체코드, 고객코드, 인도처코드)은 모두 문자열로 읽는다.
"""
from __future__ import annotations

import hashlib
import io
import itertools
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd

SheetKind = Literal["product", "auction", "delivery", "unknown"]

STEEL_DENSITY_KG_PER_MM3 = 7.85e-6


def norm_col(name: Any) -> str:
    """컬럼명 비교용: 공백 제거 + 소문자."""
    return re.sub(r"\s+", "", str(name)).lower()


# 표준 필드 → 원본 컬럼 별칭 (norm_col 적용 후 비교)
PRODUCT_ALIASES: dict[str, list[str]] = {
    "product_no": ["제품번호", "제품no", "코일번호"],
    "auction_no": ["경매번호"],
    "auction_date": ["경매일자", "경매일"],
    "customer_code": ["업체코드", "고객코드"],
    "customer": ["업체명", "고객명", "고객사"],
    "dlv_code": ["인도처코드"],
    "dlv_name": ["인도처명"],
    "thickness_mm": ["두께(mm)", "두께"],
    "width_mm": ["폭(mm)", "폭"],
    "length_mm": ["길이(mm)", "길이"],
    "weight_kg": ["중량", "실중량"],
    "spec": ["규격약호", "규격"],
    "shipped": ["출하여부", "출하상태", "출하일자", "출고일"],
}

AUCTION_ALIASES: dict[str, list[str]] = {
    "auction_no": ["경매번호"],
    "auction_date": ["경매일자", "경매일"],
    "customer_code": ["고객코드", "업체코드"],
    "customer": ["고객명", "업체명"],
    "status": ["진행상태"],
    "order_no": ["주문번호"],
    "qty": ["수량"],
    "weight_kg": ["중량"],
    "route": ["운송경로"],
    "dlv_code": ["인도처코드"],
    "dlv_name": ["인도처명"],
    "ship_plan_date": ["출하예정일"],
}

DELIVERY_ALIASES: dict[str, list[str]] = {
    "dlv_code": ["인도처코드"],
    "dlv_name": ["인도처명"],
    "address": ["주소", "인도처주소", "도착지주소", "하차지주소"],
}

SHIPPED_TRUE = {"y", "o", "yes", "완료", "출하완료", "출고", "출고완료", "출하"}
SHIPPED_FALSE = {"n", "x", "no", "미출하", "미출고", "-"}


@dataclass
class ParsedSheet:
    name: str
    kind: SheetKind
    rows: int


@dataclass
class ParseResult:
    file_name: str
    file_hash: str                                   # SHA-256, 동일 파일 재업로드 경고용
    products: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)  # 데이터 오류 행
    sheets: list[ParsedSheet] = field(default_factory=list)
    has_length_column: bool = False
    has_shipped_column: bool = False


class ParseError(ValueError):
    """파일 자체를 읽을 수 없거나 필요한 시트가 없을 때."""


# ---------------------------------------------------------------- 값 정리
def clean_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() in {"nan", "none", "nat"}:
        return None
    return s


def clean_code(v: Any) -> str | None:
    """코드 값: 엑셀이 숫자로 저장해 '146016.0'처럼 읽혀도 원래 문자열로 복원."""
    s = clean_str(v)
    if s and re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    return s


def clean_num(v: Any) -> float | None:
    s = clean_str(v)
    if s is None:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def normalize_name(v: Any) -> str | None:
    """표시용 이름 정리: 앞뒤 공백, 전각 괄호/공백, 연속 공백."""
    s = clean_str(v)
    if s is None:
        return None
    s = s.replace("（", "(").replace("）", ")").replace("　", " ")
    return re.sub(r"\s+", " ", s).strip()


def clean_date(v: Any) -> str | None:
    """'20261123', '2026-11-23', Timestamp → 'YYYY-MM-DD'."""
    s = clean_code(v)
    if s is None:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return s


def parse_shipped(v: Any) -> bool:
    """출하여부 값 판정 (PRD F1 1.4). 날짜가 있으면 출하완료."""
    s = clean_str(v)
    if s is None:
        return False
    low = s.lower()
    if low in SHIPPED_TRUE:
        return True
    if low in SHIPPED_FALSE:
        return False
    return bool(re.search(r"\d{4}[-./]?\d{1,2}[-./]?\d{1,2}", s))


def calc_length_mm(weight_kg: float, thickness_mm: float, width_mm: float) -> float:
    return weight_kg / (thickness_mm * width_mm * STEEL_DENSITY_KG_PER_MM3)


# ---------------------------------------------------------------- 시트 판별
def _col_map(columns: list[Any], aliases: dict[str, list[str]]) -> dict[str, Any]:
    """표준 필드 → 실제 컬럼명."""
    normed = {norm_col(c): c for c in columns}
    out = {}
    for fld, names in aliases.items():
        for n in names:
            if norm_col(n) in normed:
                out[fld] = normed[norm_col(n)]
                break
    return out


def detect_sheet_kind(columns: list[Any]) -> SheetKind:
    cols = {norm_col(c) for c in columns}
    if "제품번호" in cols and ({"폭(mm)", "폭"} & cols):
        return "product"
    if "진행상태" in cols and "주문번호" in cols:
        return "auction"
    if "인도처코드" in cols and ({"주소", "인도처주소", "도착지주소", "하차지주소"} & cols):
        return "delivery"
    return "unknown"


def _read_sheets(content: bytes) -> dict[str, pd.DataFrame]:
    try:
        return pd.read_excel(io.BytesIO(content), sheet_name=None, dtype=str)
    except Exception as e:
        raise ParseError(f"엑셀 파일을 읽을 수 없습니다: {type(e).__name__}") from e


# ---------------------------------------------------------------- 메인
def parse_workbook(content: bytes, file_name: str) -> ParseResult:
    result = ParseResult(file_name=file_name, file_hash=hashlib.sha256(content).hexdigest())
    sheets = _read_sheets(content)

    for sheet_name, df in sheets.items():
        df = df.dropna(how="all")
        kind = detect_sheet_kind(list(df.columns))
        result.sheets.append(ParsedSheet(sheet_name, kind, len(df)))
        if kind == "product":
            _parse_products(df, sheet_name, result)
        elif kind == "auction":
            _parse_orders(df, sheet_name, result)

    if not any(s.kind in ("product", "auction") for s in result.sheets):
        raise ParseError(
            "낙찰현황 시트를 찾지 못했습니다. 제품 시트(제품번호·폭 컬럼) 또는 "
            "경매 시트(진행상태·주문번호 컬럼)가 필요합니다."
        )
    return result


def _parse_products(df: pd.DataFrame, sheet: str, result: ParseResult) -> None:
    cm = _col_map(list(df.columns), PRODUCT_ALIASES)
    result.has_length_column |= "length_mm" in cm
    result.has_shipped_column |= "shipped" in cm
    known = set(cm.values())

    for idx, raw in df.iterrows():
        row_no = int(idx) + 2  # 엑셀 행 번호 (머리글 1행)
        g = lambda f: raw.get(cm[f]) if f in cm else None  # noqa: E731
        p: dict[str, Any] = {
            "product_no": clean_code(g("product_no")),
            "auction_no": clean_code(g("auction_no")),
            "auction_date": clean_date(g("auction_date")),
            "customer_code": clean_code(g("customer_code")),
            "customer": normalize_name(g("customer")),
            "dlv_code": clean_code(g("dlv_code")),
            "dlv_name": normalize_name(g("dlv_name")),
            "thickness_mm": clean_num(g("thickness_mm")),
            "width_mm": clean_num(g("width_mm")),
            "weight_kg": clean_num(g("weight_kg")),
            "spec": clean_str(g("spec")),
            "length_mm": None,
            "length_source": None,
            "shipped": parse_shipped(g("shipped")) if "shipped" in cm else None,
            "extra": {str(c): clean_str(raw[c]) for c in df.columns if c not in known and clean_str(raw[c])},
            "error": None,
        }

        problems = []
        if not p["product_no"]:
            problems.append("제품번호 없음")
        for fld, label in (("thickness_mm", "두께"), ("width_mm", "폭"), ("weight_kg", "중량")):
            if p[fld] is None or p[fld] <= 0:
                problems.append(f"{label} 값 오류({clean_str(g(fld)) or '빈칸'})")
        for fld, label in (("auction_no", "경매번호"), ("customer_code", "업체코드"), ("dlv_code", "인도처코드")):
            if not p[fld]:
                problems.append(f"{label} 없음")

        length_col = clean_num(g("length_mm")) if "length_mm" in cm else None
        if length_col and length_col > 0:
            p["length_mm"], p["length_source"] = length_col, "column"
        elif p["weight_kg"] and p["thickness_mm"] and p["width_mm"] and not problems:
            p["length_mm"] = round(calc_length_mm(p["weight_kg"], p["thickness_mm"], p["width_mm"]), 1)
            p["length_source"] = "calc"

        if problems:
            p["error"] = ", ".join(problems)
            result.errors.append({"sheet": sheet, "row": row_no, "product_no": p["product_no"], "reason": p["error"]})
        if p["product_no"]:  # 제품번호가 있으면 오류여도 저장 (데이터오류 상태로 조회 가능)
            result.products.append(p)


def _parse_orders(df: pd.DataFrame, sheet: str, result: ParseResult) -> None:
    cm = _col_map(list(df.columns), AUCTION_ALIASES)
    for idx, raw in df.iterrows():
        g = lambda f: raw.get(cm[f]) if f in cm else None  # noqa: E731
        o = {
            "auction_no": clean_code(g("auction_no")),
            "auction_date": clean_date(g("auction_date")),
            "customer_code": clean_code(g("customer_code")),
            "customer": normalize_name(g("customer")),
            "status": clean_str(g("status")),
            "order_no": clean_code(g("order_no")),
            "qty": int(clean_num(g("qty")) or 0),
            "weight_kg": clean_num(g("weight_kg")),
            "route": clean_code(g("route")),
            "dlv_code": clean_code(g("dlv_code")),
            "dlv_name": normalize_name(g("dlv_name")),
            "ship_plan_date": clean_date(g("ship_plan_date")),
        }
        if not (o["auction_no"] and o["customer_code"] and o["status"]):
            result.errors.append({"sheet": sheet, "row": int(idx) + 2, "product_no": None,
                                  "reason": "경매 시트: 경매번호/고객코드/진행상태 없음"})
            continue
        o["order_key"] = order_key(o)
        result.orders.append(o)


def order_key(o: dict) -> str:
    if o.get("order_no"):
        return o["order_no"]
    w = o.get("weight_kg")
    return f"{o['auction_no']}|{o['customer_code']}|{o['status']}|{int(w) if w is not None else ''}"


def parse_delivery_places(content: bytes) -> list[dict[str, Any]]:
    """인도처 주소표: 인도처코드, 주소 (인도처명 선택)."""
    rows: list[dict[str, Any]] = []
    for df in _read_sheets(content).values():
        if detect_sheet_kind(list(df.columns)) != "delivery":
            continue
        cm = _col_map(list(df.columns), DELIVERY_ALIASES)
        for _, raw in df.dropna(how="all").iterrows():
            code = clean_code(raw.get(cm["dlv_code"]))
            if not code:
                continue
            rows.append({
                "dlv_code": code,
                "dlv_name": normalize_name(raw.get(cm["dlv_name"])) if "dlv_name" in cm else None,
                "address": normalize_name(raw.get(cm["address"])),
            })
    if not rows:
        raise ParseError("인도처 주소표를 찾지 못했습니다. '인도처코드'와 '주소' 컬럼이 필요합니다.")
    return rows


# ---------------------------------------------------------------- 제품 ↔ 주문 매칭
@dataclass
class OrderMatch:
    order_key: str | None          # 확정된 주문 (없으면 None)
    order_no: str | None
    candidates: list[str]          # 확정 못 했을 때 후보 주문번호/키
    order_status: str | None       # 배차 판단용 주문 상태
    cancel_uncertain: bool = False  # 취소된 주문이 섞였는데 어느 제품이 취소분인지 확정 못 함 → 배차 보류


MATCH_SEARCH_LIMIT = 200_000
_CANCELLED = "__cancelled__"


def match_products_to_orders(products: list[dict], orders: list[dict],
                             absent: set[str] | frozenset = frozenset()) -> dict[str, OrderMatch]:
    """같은 (경매번호, 고객) 그룹 안에서 제품을 주문에 배정한다 (PRD F1 1.2).

    products: product_no, weight_kg 를 가진 dict (같은 그룹, DB에 있는 전부)
    orders:   같은 그룹의 경매 시트 행 (낙찰취소 포함 가능)
    absent:   이번 파일의 제품 시트에 이 그룹이 있었는데 빠진 제품번호 (취소 판단 보조 근거)

    규칙:
    1. 살아 있는 주문 수량 합 == 제품 수 → 취소분 제품은 DB에 없음 (원본처럼 제품 시트에서 이미 빠짐). 살아 있는 주문끼리만 매칭.
    2. 전체(취소 포함) 수량 합 == 제품 수 → 취소 주문도 매칭에 넣어 취소 주문이 자기 제품을 가져가게 한다.
    3. 그래도 안 되면, 파일에서 빠진 제품의 수량·중량이 취소분과 맞을 때 그 제품들을 취소로 본다.
    4. 끝내 못 정하면 후보를 붙이고, 취소가 섞여 있으면 배차를 보류(cancel_uncertain)한다.
    """
    live = [o for o in orders if o["status"] != "낙찰취소"]
    cancelled = [o for o in orders if o["status"] == "낙찰취소"]
    if not live:
        status = "낙찰취소" if orders else None
        return {p["product_no"]: OrderMatch(None, None, [], status) for p in products}

    live_qty = sum(o["qty"] for o in live)
    if not cancelled or live_qty == len(products):
        return _match_live(products, live)

    by_key = {o["order_key"]: o for o in live}

    def from_solution(sol: dict[str, str]) -> dict[str, OrderMatch]:
        out = {}
        for pno, k in sol.items():
            if k in by_key:
                out[pno] = OrderMatch(k, by_key[k].get("order_no"), [], by_key[k]["status"])
            else:
                out[pno] = OrderMatch(None, None, [], "낙찰취소")
        return out

    # 2) 취소 주문 하나하나 → 실패하면 취소분을 한 덩어리로
    if sum(o["qty"] for o in orders) == len(products):
        sol = _unique_assignment(products, live + cancelled)
        if sol is None:
            merged = {"order_key": _CANCELLED, "status": "낙찰취소", "qty": sum(o["qty"] for o in cancelled),
                      "weight_kg": sum(o["weight_kg"] or 0 for o in cancelled)}
            sol = _unique_assignment(products, live + [merged])
        if sol is not None:
            return from_solution(sol)

    # 3) 이번 파일에서 빠진 제품 = 취소분 (수량·중량이 맞을 때만)
    gone = [p for p in products if p["product_no"] in absent]
    c_qty = sum(o["qty"] for o in cancelled)
    c_kg = sum(o["weight_kg"] or 0 for o in cancelled)
    if gone and len(gone) == c_qty and abs(sum(p["weight_kg"] or 0 for p in gone) - c_kg) < 1:
        rest = [p for p in products if p["product_no"] not in absent]
        out = {p["product_no"]: OrderMatch(None, None, [], "낙찰취소") for p in gone}
        out.update(_match_live(rest, live))
        return out

    # 4) 판단 불가 → 보류
    cands = [o.get("order_no") or o["order_key"] for o in live] + ["낙찰취소"]
    return {p["product_no"]: OrderMatch(None, None, cands, "낙찰확정", cancel_uncertain=True) for p in products}


def _match_live(products: list[dict], live: list[dict]) -> dict[str, OrderMatch]:
    """취소가 없는(또는 취소분 제품이 이미 빠진) 경우의 매칭."""
    if len(live) == 1:
        o = live[0]
        return {p["product_no"]: OrderMatch(o["order_key"], o.get("order_no"), [], o["status"]) for p in products}

    solution = _unique_assignment(products, live)
    if solution is not None:
        by_key = {o["order_key"]: o for o in live}
        return {
            pno: OrderMatch(k, by_key[k].get("order_no"), [], by_key[k]["status"]) for pno, k in solution.items()
        }

    # 확정 불가: 후보 전부 붙이고, 배차 판단은 보수적으로 (하나라도 미입금이면 미입금)
    cands = [o.get("order_no") or o["order_key"] for o in live]
    statuses = {o["status"] for o in live}
    status = "낙찰확정" if statuses == {"낙찰확정"} else "낙찰"
    return {p["product_no"]: OrderMatch(None, None, cands, status) for p in products}


def _unique_assignment(products: list[dict], orders: list[dict]) -> dict[str, str] | None:
    """주문별 수량·중량 합계와 정확히 맞는 배정이 유일하면 {product_no: order_key}."""
    if any(p.get("weight_kg") is None for p in products):
        return None
    if sum(o["qty"] for o in orders) != len(products):
        return None
    orders = sorted(orders, key=lambda o: o["qty"])  # 수량이 적은 주문부터 → 탐색 폭 최소화
    weights = [round(p["weight_kg"]) for p in products]
    solutions: list[dict[int, str]] = []
    budget = [MATCH_SEARCH_LIMIT]

    def rec(oi: int, remaining: tuple[int, ...], acc: dict[int, str]) -> None:
        if len(solutions) > 1 or budget[0] <= 0:
            return
        if oi == len(orders) - 1:  # 마지막 주문은 남은 제품 전부
            o = orders[oi]
            if len(remaining) == o["qty"] and sum(weights[i] for i in remaining) == round(o["weight_kg"] or 0):
                solutions.append({**acc, **{i: o["order_key"] for i in remaining}})
            return
        o = orders[oi]
        target = round(o["weight_kg"] or 0)
        for combo in itertools.combinations(remaining, o["qty"]):
            budget[0] -= 1
            if budget[0] <= 0 or len(solutions) > 1:
                return
            if sum(weights[i] for i in combo) == target:
                rest = tuple(i for i in remaining if i not in combo)
                rec(oi + 1, rest, {**acc, **{i: o["order_key"] for i in combo}})

    rec(0, tuple(range(len(products))), {})
    if len(solutions) != 1 or budget[0] <= 0:
        return None
    return {products[i]["product_no"]: k for i, k in solutions[0].items()}
