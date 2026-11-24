"""배차 결과 독립 검증: 앱 코드를 쓰지 않고 '엑셀 내보내기' 결과만 읽어서 PRD 규칙을 다시 검사한다."""
from __future__ import annotations

import io
from collections import defaultdict

import pandas as pd

MIN, WIDE_MAX, NORMAL_MAX, LONG_MAX = 25.0, 26.0, 27.0, 26.0
WIDE_MM, LONG_MM = 3400, 17000


def check_export(xlsx: bytes) -> dict:
    sheets = pd.read_excel(io.BytesIO(xlsx), sheet_name=None)
    assigned = sheets["편성제품"]
    trucks = sheets["차량요약"]
    unassigned = sheets["미배차"]
    violations = []
    by_truck = defaultdict(list)
    for _, r in assigned.iterrows():
        by_truck[r["차량번호"]].append(r)
    for tid, rows in by_truck.items():
        live = [r for r in rows if r["상태"] == "편성"]
        if not live:
            continue
        ton = sum(float(r["중량(kg)"]) for r in live) / 1000
        wide = {float(r["폭(mm)"]) > WIDE_MM for r in live}
        long_ = any(float(r["길이(mm)"] or 0) > LONG_MM for r in live)
        regions = {r["지역"] for r in live}
        if len(wide) > 1:
            violations.append((tid, "R2 광폭·일반 혼적"))
        if len(regions) > 1:
            violations.append((tid, f"R3 지역 혼적 {regions}"))
        cap = WIDE_MAX if True in wide and len(wide) == 1 else (LONG_MAX if long_ else NORMAL_MAX)
        if ton < MIN - 1e-6:
            violations.append((tid, f"R1 하한 미달 {ton:.3f}"))
        if ton > cap + 1e-6:
            violations.append((tid, f"R1 상한 초과 {ton:.3f} > {cap}"))
        # 차량요약의 합계와 일치하는지
        summary = trucks.loc[trucks["차량번호"] == tid, "합계톤"]
        if len(summary) and abs(float(summary.iloc[0]) - ton) > 0.001:
            violations.append((tid, f"요약 합계 불일치 {summary.iloc[0]} vs {ton:.3f}"))

    # 미배차가 같은 (분류, 지역)에서 25t 이상 남아 있으면 차량을 더 만들 수 있었다는 신호
    leftovers = defaultdict(float)
    for _, r in unassigned.iterrows():
        if r["상태"] != "미배차":
            continue
        cat = "WIDE" if float(r["폭(mm)"]) > WIDE_MM else "NORMAL"
        leftovers[(cat, r["지역"])] += float(r["중량(kg)"]) / 1000
    missed = {k: round(v, 3) for k, v in leftovers.items() if v >= MIN}

    # 고객사 우선: 차량당 고객사 수
    cust_per_truck = [len({r["고객사"] for r in rows if r["상태"] == "편성"}) for rows in by_truck.values()]
    return {
        "trucks": len(by_truck),
        "violations": violations,
        "missed_groups": missed,
        "single_customer_trucks": sum(1 for c in cust_per_truck if c == 1),
        "max_customers_in_truck": max(cust_per_truck, default=0),
    }
