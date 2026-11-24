"""경계값·지역 규칙 (PRD F3 R1~R3, T1~T7, T15~T18, T29, T30) — 새 서버에서 UI로 업로드·주소표·배차."""
from __future__ import annotations

import pytest

from tests.e2e.conftest import shot, tab, upload_via_ui
from tests.e2e.rule_check import check_export
from tests.fixtures.builders import ADDRESS_CASES, BOUNDARY_CASES, boundary

pytestmark = pytest.mark.e2e


def test_boundary_cases(page, server):
    main, places = boundary()
    upload_via_ui(page, main, "boundary.xlsx")
    page.set_input_files("#places-file", files=[{"name": "places.xlsx", "mimeType": "application/octet-stream", "buffer": places}])
    page.click("#btn-places")
    page.wait_for_selector("#places-result .note")
    tab(page, "dispatch")
    page.click("#btn-dispatch")
    page.wait_for_selector(".run-summary")
    shot(page, "e2e-boundary-dispatch")

    d = server.api("GET", "/api/dispatch").json()
    by_pno = {p["product_no"]: (t["truck_id"], p) for t in d["trucks"] for p in t["products"]}
    by_pno |= {p["product_no"]: (None, p) for p in d["unassigned"]}
    failures = []
    for route, (desc, items, n_trucks, reason) in BOUNDARY_CASES.items():
        pnos = [f"{pno}0000000"[:10] for pno, *_ in items]
        trucks = {by_pno[p][0] for p in pnos if by_pno[p][0]}
        reasons = {by_pno[p][1]["unassigned_reason"] for p in pnos if not by_pno[p][0]}
        if len(trucks) != n_trucks or (reason and reasons != {reason}):
            failures.append((route, desc, trucks, reasons))
    assert failures == []

    # 분류 경계: 폭 3,400 → 일반재, 3,401 → 광폭재 / 길이 17,000 → 장척 아님, 17,001 → 장척
    assert by_pno["BA10000000"][1]["category"] == "NORMAL"
    assert by_pno["BB10000000"][1]["category"] == "WIDE"
    assert by_pno["BD10000000"][1]["is_long"] is False
    assert by_pno["BE10000000"][1]["is_long"] is True

    # 주소 지역: 창원(경남/경상남도 표기 차이) 묶임, 김해 별도, 부산 광역시 묶임, 주소 없는 RK 는 창원과 안 섞임
    reg = {f"{p}0000000"[:10]: by_pno[f"{p}0000000"[:10]][1]["region"] for p, *_ in ADDRESS_CASES}
    assert reg["BK10000000"] == reg["BK20000000"] == "경상남도 창원시"
    assert reg["BK30000000"] == "경상남도 김해시"
    assert reg["BK40000000"] == reg["BK50000000"] == "부산광역시"
    assert reg["BK60000000"] == "RK"
    t = lambda p: by_pno[p][0]  # noqa: E731
    assert t("BK10000000") and t("BK10000000") == t("BK20000000")   # T16·T18
    assert t("BK40000000") and t("BK40000000") == t("BK50000000")   # 광역시
    assert t("BK30000000") is None and t("BK60000000") is None      # T15·T30

    res = check_export(server.api("GET", "/api/export").content)
    assert res["violations"] == []


def test_refund_notice_only_for_rule_reasons(server):  # T8·T21
    d = server.api("GET", "/api/dispatch").json()
    for p in d["unassigned"]:
        expected = p["status"] == "미배차" and p["unassigned_reason"] in ("OVER_MAX", "UNDER_MIN")
        assert p["refund_notice"] is expected, p["product_no"]
        r = server.api("POST", "/api/chat", json={"question": p["product_no"]}).json()
        assert ("환불까지 2주" in r["messages"][0]["text"]) is expected, p["product_no"]
