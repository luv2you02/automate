"""4단계 검토용 엑셀 생성기. 샘플 원본(sample/)은 읽기만 한다.

정적 파일 만들기:  .venv\\Scripts\\python -m tests.fixtures.builders
(배차 결과에 따라 달라지는 '다음 날 파일', '출하여부 파일'은 E2E 테스트가 실행 중에 함수로 만든다.)
"""
from __future__ import annotations

import io
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "sample" / "후판_업체별_제품단위_낙찰현황_인도처추가.xlsx"
OUT = Path(__file__).resolve().parent
PRODUCT_SHEET, AUCTION_SHEET = "Sheet1", "Sheet1 (2)"
DENSITY = 7.85e-6


def read_sample() -> dict[str, pd.DataFrame]:
    return pd.read_excel(SAMPLE, sheet_name=None, dtype=str)


def to_xlsx(sheets: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, index=False)
    return buf.getvalue()


# ---------------------------------------------------------------- 다음 날 파일
def next_day(*, confirm_group: tuple[str, str] | None = None, cancel_group: tuple[str, str] | None = None,
             bump_product: tuple[str, int] | None = None, new_products: int = 6) -> tuple[bytes, dict]:
    """샘플 + 변경사항.
    confirm_group: (경매번호, 고객코드) '낙찰' → '낙찰확정'
    cancel_group:  (경매번호, 고객코드) '낙찰확정' → '낙찰취소' (제품 시트에서도 빠짐: 원본처럼)
    bump_product:  (제품번호, 추가 kg) 중량 변경
    new_products:  새 경매(2026112401)로 기존 고객·인도처·운송경로와 같은 제품 추가
    """
    s = read_sample()
    prod, auc = s[PRODUCT_SHEET].copy(), s[AUCTION_SHEET].copy()
    info: dict = {}

    if confirm_group:
        m = (auc["경매번호"] == confirm_group[0]) & (auc["고객코드"] == confirm_group[1]) & (auc["진행상태"] == "낙찰")
        auc.loc[m, "진행상태"] = "낙찰확정"
        auc.loc[m, "주문번호"] = "D261199901"
        auc.loc[m, "출하예정일"] = "20261126"
        info["confirmed_rows"] = int(m.sum())

    if cancel_group:
        m = (auc["경매번호"] == cancel_group[0]) & (auc["고객코드"] == cancel_group[1])
        auc.loc[m, "진행상태"] = "낙찰취소"
        auc.loc[m, "주문번호"] = None
        auc.loc[m, "출하예정일"] = None
        pm = (prod["경매번호"] == cancel_group[0]) & (prod["업체코드"] == cancel_group[1])
        info["cancelled_products"] = list(prod.loc[pm, "제품번호"])
        prod = prod.loc[~pm]

    if bump_product:
        pno, add = bump_product
        m = prod["제품번호"] == pno
        prod.loc[m, "중량"] = str(int(float(prod.loc[m, "중량"].iloc[0])) + add)
        info["bumped"] = pno

    if new_products:
        # 미배차가 남은 지역에 붙도록: 샘플에서 가장 흔한 (업체, 인도처) 조합을 복제
        base = prod.iloc[0]
        a = auc[(auc["경매번호"] == base["경매번호"]) & (auc["고객코드"] == base["업체코드"])].iloc[0]
        rows = []
        for i in range(new_products):
            r = base.copy()
            r["경매번호"], r["경매일자"] = "2026112401", "20261124"
            r["제품번호"] = f"FN{90000000 + i}"
            r["중량"] = str(2000 + i * 150)
            rows.append(r)
        prod = pd.concat([prod, pd.DataFrame(rows)], ignore_index=True)
        new_order = a.copy()
        new_order["경매번호"], new_order["경매일자"] = "2026112401", "20261124"
        new_order["진행상태"], new_order["주문번호"], new_order["출하예정일"] = "낙찰확정", "D261199902", "20261127"
        new_order["수량"] = str(new_products)
        new_order["중량"] = str(sum(2000 + i * 150 for i in range(new_products)))
        auc = pd.concat([auc, pd.DataFrame([new_order])], ignore_index=True)
        info["new_products"] = [r["제품번호"] for r in rows]
        info["new_route"] = a["운송경로"]

    return to_xlsx({AUCTION_SHEET: auc, PRODUCT_SHEET: prod}), info


# ---------------------------------------------------------------- 길이·출하여부 컬럼 추가
def with_columns(shipped: set[str] = frozenset(), long_products: set[str] = frozenset()) -> bytes:
    s = read_sample()
    prod = s[PRODUCT_SHEET].copy()
    w = prod["중량"].astype(float)
    length = (w / (prod["두께 (mm)"].astype(float) * prod["폭 (mm)"].astype(float) * DENSITY)).round(0)
    prod["길이"] = length.astype(int).astype(str)
    prod.loc[prod["제품번호"].isin(long_products), "길이"] = "17500"
    prod["출하여부"] = ["Y" if p in shipped else "" for p in prod["제품번호"]]
    return to_xlsx({AUCTION_SHEET: s[AUCTION_SHEET], PRODUCT_SHEET: prod})


# ---------------------------------------------------------------- 경계값
def _p(pno, kg, w, length, route, auc, cust, dlv):
    return {"경매번호": auc, "경매일자": "20261124", "업체코드": cust, "업체명": f"경계{cust}", "제품번호": pno,
            "인도처코드": dlv, "인도처명": f"경계인도처{dlv}", "규격약호": "SS275", "두께 (mm)": "100", "폭 (mm)": str(w),
            "중량": str(kg), "길이": str(length)}


BOUNDARY_CASES = {
    # 운송경로(=지역)별로 독립된 경우. 기대값: (차량 수, 미배차 사유)
    "RA": ("폭 3,400mm 두 개 13t+13t → 일반재 차량", [("BA1", 13000, 3400, 8000), ("BA2", 13000, 3400, 8000)], 1, None),
    "RB": ("폭 3,401mm 두 개 13t+13t → 광폭재 차량 26t", [("BB1", 13000, 3401, 8000), ("BB2", 13000, 3401, 8000)], 1, None),
    "RC": ("광폭 13.5t+13.5t = 27t > 26t → 미배차", [("BC1", 13500, 3500, 8000), ("BC2", 13500, 3500, 8000)], 0, "UNDER_MIN"),
    "RD": ("일반 13.5t+13.5t, 길이 17,000mm (장척 아님) → 27t 차량", [("BD1", 13500, 2500, 17000), ("BD2", 13500, 2500, 17000)], 1, None),
    "RE": ("일반 13.5t+13.5t, 한 개 17,001mm (장척) → 26t 초과 미배차", [("BE1", 13500, 2500, 17001), ("BE2", 13500, 2500, 10000)], 0, "UNDER_MIN"),
    "RF": ("일반 단독 25.000t → 차량 1대", [("BF1", 25000, 2500, 9000)], 1, None),
    "RG": ("광폭 단독 27t → 상한 초과", [("BG1", 27000, 3600, 9000)], 0, "OVER_MAX"),
    "RH": ("일반 단독 24.999t → 하한 미달", [("BH1", 24999, 2500, 9000)], 0, "UNDER_MIN"),
    "RI": ("장척 일반 단독 26.5t → 상한 초과(26t)", [("BI1", 26500, 2500, 18000)], 0, "OVER_MAX"),
    "RJ": ("광폭 12.5t + 일반 12.5t → 합짐 불가", [("BJ1", 12500, 3600, 8000), ("BJ2", 12500, 2500, 8000)], 0, "UNDER_MIN"),
}

# 주소표 검증용 (같은 경매번호의 다른 고객으로 넣고, 주소표로 지역을 바꾼다)
ADDRESS_CASES = [
    # (제품, kg, 인도처, 주소, 운송경로)
    ("BK1", 13000, "CW1", "경남 창원시 성산구 공단로 1", "RK"),
    ("BK2", 13000, "CW2", "경상남도 창원시 의창구 2", "RK2"),     # 표기 다름 + 운송경로 다름 → 같은 지역(창원)
    ("BK3", 13000, "GH1", "경남 김해시 주촌면 3", "RK"),            # 다른 시
    ("BK4", 13000, "BS1", "부산 강서구 녹산산단 4", "RL"),
    ("BK5", 13000, "BS2", "부산광역시 사하구 5", "RL2"),            # 광역시 전체가 한 지역
    ("BK6", 13000, "NA1", None, "RK"),                              # 주소 없음 → 운송경로 RK (창원과 섞이면 안 됨)
]


def boundary() -> tuple[bytes, bytes]:
    """(낙찰현황 파일, 인도처 주소표)"""
    prods, orders = [], []
    for i, (route, (_, items, _, _)) in enumerate(BOUNDARY_CASES.items()):
        auc, cust, dlv = f"20261124{i:02d}", f"B{route}", f"D{route}"
        for pno, kg, w, length in items:
            prods.append(_p(f"{pno}0000000"[:10], kg, w, length, route, auc, cust, dlv))
        orders.append({"경매일자": "20261124", "경매번호": auc, "고객코드": cust, "고객명": f"경계{cust}", "진행상태": "낙찰확정",
                       "수량": str(len(items)), "중량": str(sum(x[1] for x in items)), "주문번호": f"D2611990{i:02d}",
                       "운송경로": route, "인도처코드": dlv, "인도처명": f"경계인도처{dlv}", "출하예정일": "20261126"})
    for j, (pno, kg, dlv, _addr, route) in enumerate(ADDRESS_CASES):
        auc, cust = f"20261125{j:02d}", f"A{dlv}"
        prods.append(_p(f"{pno}0000000"[:10], kg, 2500, 8000, route, auc, cust, dlv))
        orders.append({"경매일자": "20261124", "경매번호": auc, "고객코드": cust, "고객명": f"주소{dlv}", "진행상태": "낙찰확정",
                       "수량": "1", "중량": str(kg), "주문번호": f"D2611991{j:02d}", "운송경로": route, "인도처코드": dlv,
                       "인도처명": f"주소인도처{dlv}", "출하예정일": "20261126"})
    main = to_xlsx({AUCTION_SHEET: pd.DataFrame(orders), PRODUCT_SHEET: pd.DataFrame(prods)})
    places = to_xlsx({"주소표": pd.DataFrame([{"인도처코드": d, "주소": a} for _, _, d, a, _ in ADDRESS_CASES if a])})
    return main, places


# ---------------------------------------------------------------- 오류 파일
def error_files() -> dict[str, bytes]:
    s = read_sample()
    prod, auc = s[PRODUCT_SHEET].head(6).copy(), s[AUCTION_SHEET]
    prod.iloc[0, prod.columns.get_loc("중량")] = None              # 필수값 누락
    prod.iloc[1, prod.columns.get_loc("폭 (mm)")] = "2,5OO"        # 문자 섞인 숫자 (O 대문자)
    prod.iloc[2, prod.columns.get_loc("인도처코드")] = None          # 인도처 없음
    files = {
        "missing_values.xlsx": to_xlsx({AUCTION_SHEET: auc, PRODUCT_SHEET: prod}),
        "empty_sheet.xlsx": to_xlsx({"Sheet1": pd.DataFrame()}),
        "product_only.xlsx": to_xlsx({PRODUCT_SHEET: s[PRODUCT_SHEET].head(10)}),
        "unrelated.xlsx": to_xlsx({"매출": pd.DataFrame({"월": ["1월"], "금액": [100]})}),
    }
    files["not_excel.xlsx"] = "엑셀이 아닌 텍스트 파일".encode("utf-8")
    return files


def big_file(n: int = 3000) -> bytes:
    """성능 측정용: 샘플을 복제해 n 건 (제품번호·경매번호를 바꿔 신규로)."""
    s = read_sample()
    prod, auc = s[PRODUCT_SHEET], s[AUCTION_SHEET]
    prods, aucs = [], []
    k = 0
    while sum(len(x) for x in prods) < n:
        p = prod.copy()
        a = auc.copy()
        p["경매번호"] = p["경매번호"].str[:-2] + f"{50 + k:02d}"
        a["경매번호"] = a["경매번호"].str[:-2] + f"{50 + k:02d}"
        p["제품번호"] = [f"FP{k:02d}{i:06d}" for i in range(len(p))]
        a["주문번호"] = [f"D9{k:02d}{i:06d}" if isinstance(o, str) else None for i, o in enumerate(a["주문번호"])]
        prods.append(p)
        aucs.append(a)
        k += 1
    return to_xlsx({AUCTION_SHEET: pd.concat(aucs), PRODUCT_SHEET: pd.concat(prods).head(n)})


if __name__ == "__main__":
    main, places = boundary()
    (OUT / "boundary.xlsx").write_bytes(main)
    (OUT / "boundary_places.xlsx").write_bytes(places)
    for name, data in error_files().items():
        (OUT / f"error_{name}").write_bytes(data)
    nd, _ = next_day(new_products=6)
    (OUT / "next_day_example.xlsx").write_bytes(nd)
    (OUT / "with_length_shipped_example.xlsx").write_bytes(with_columns())
    print("written to", OUT)
