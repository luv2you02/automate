"""TOP 5 수정의 화면 동작 확인 (R-01 보류 표시, R-02 확인 필요 차량 정리·해체, R-10 예전 파일 경고)."""
from __future__ import annotations

import pytest

from tests.e2e.conftest import shot, tab, upload_via_ui
from tests.helpers import O, P, make_xlsx

pytestmark = pytest.mark.e2e

PRODS = [P("FA00000001", 9, cust="A"), P("FA00000002", 9, cust="A"), P("FA00000003", 8, cust="B")]
ORDERS = [O("2026112001", "A", "낙찰확정", 2, 18000, "D261100001"), O("2026112001", "B", "낙찰확정", 1, 8000, "D261100002")]


def _drag(page, src, dst, pos=None):
    page.locator(dst).scroll_into_view_if_needed()
    page.locator(src).drag_to(page.locator(dst), target_position=pos or {"x": 120, "y": 40})


def test_review_truck_can_be_fixed_in_ui(page, server):
    server.api("POST", "/api/upload", files={"files": ("a.xlsx", make_xlsx(PRODS, ORDERS))})
    server.api("POST", "/api/dispatch/run", json={})
    # 8t 주문 취소 → 18t 확인 필요 차량. 화면에서는 예전 파일 경고도 확인하기 위해 UI 로 올린다
    upload_via_ui(page, make_xlsx(PRODS[:2], [ORDERS[0], O("2026112001", "B", "낙찰취소", 1, 8000)]), "cancel.xlsx")
    assert "확인 필요" in page.locator("#upload-result").inner_text()

    tab(page, "dispatch")
    card = page.locator('.truck[data-truck="2026-11-24-N01"]')
    card.wait_for()
    assert "review" in card.get_attribute("class")
    assert "FA00000003 제외됨" in card.inner_text()
    assert "해체" in card.inner_text()
    shot(page, "fix-r02-review-truck")

    # R-02: 확인 필요 차량에서 제품을 미배차로 끌어낼 수 있다
    card.locator(".truck-toggle").click()
    _drag(page, '.truck[data-truck="2026-11-24-N01"] .prow[data-pno="FA00000001"]', "#unassigned-drop", {"x": 150, "y": 40})
    page.wait_for_selector(".toast.success")
    d = server.api("GET", "/api/dispatch").json()
    assert d["trucks"][0]["total_ton"] == 9.0

    # [해체]: 확인 창 → 미배차로
    page.once("dialog", lambda dlg: dlg.accept())
    page.locator('.truck[data-truck="2026-11-24-N01"] [data-dismantle]').click()
    page.wait_for_selector(".toast.success >> text=해체")
    d = server.api("GET", "/api/dispatch").json()
    assert d["trucks"] == [] and {p["product_no"] for p in d["unassigned"]} == {"FA00000001", "FA00000002"}
    assert page.locator(".truck").count() == 0
    assert page.console_errors == []


def test_dismantle_cancel_keeps_truck(page, server):
    server.api("POST", "/api/dispatch/run", json={"date": "2026-11-25"})  # 앞 테스트의 두 제품 9+9=18 → 차량 안 됨
    server.api("POST", "/api/upload", files={"files": ("b.xlsx", make_xlsx([P("FA00000011", 13, cust="C", auc="2026112102"),
                                                                            P("FA00000012", 13, cust="C", auc="2026112102")]))})
    server.api("POST", "/api/dispatch/run", json={"date": "2026-11-25"})
    tab(page, "dispatch")
    page.wait_for_selector(".truck")
    page.once("dialog", lambda dlg: dlg.dismiss())
    page.locator(".truck [data-dismantle]").first.click()
    page.wait_for_timeout(400)
    assert len(server.api("GET", "/api/dispatch").json()["trucks"]) == 1


def test_older_file_warning_in_ui(page, server):
    # 취소된 B 주문을 다시 확정 상태로 담은 예전 파일
    upload_via_ui(page, make_xlsx(PRODS, ORDERS), "old.xlsx")
    txt = page.locator("#upload-result").inner_text()
    shot(page, "fix-r10-stale-warning")
    assert "예전 데이터로 보이는 주문 1건" in txt
    r = server.api("POST", "/api/chat", json={"question": "FA00000003"}).json()
    assert r["rows"][0]["status"] == "낙찰취소"


def test_held_products_shown_and_locked(page, server):
    ps = [P("FA00000021", 10, cust="D", auc="2026112103"), P("FA00000022", 10, cust="D", auc="2026112103"),
          P("FA00000023", 10, cust="D", auc="2026112103")]
    o = [O("2026112103", "D", "낙찰확정", 2, 20000, "D261100021"), O("2026112103", "D", "낙찰확정", 1, 10000, "D261100022")]
    server.api("POST", "/api/upload", files={"files": ("c.xlsx", make_xlsx(ps, o))})
    server.api("POST", "/api/upload", files={"files": ("d.xlsx", make_xlsx(ps, [o[0], O("2026112103", "D", "낙찰취소", 1, 10000)],
                                                                            include_products=False))})
    tab(page, "unassigned")
    page.wait_for_selector("text=확인 필요 (배차 보류)")
    shot(page, "fix-r01-held")
    tab(page, "dispatch")
    row = page.locator('#side-list .prow[data-pno="FA00000021"]')
    assert "locked" in row.get_attribute("class") and row.get_attribute("draggable") is None
