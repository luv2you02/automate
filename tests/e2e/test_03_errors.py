"""오류 처리: 잘못된 파일, 서버 꺼짐, 동시 실행 잠금 (PRD F1 1.5, 4장)."""
from __future__ import annotations

import concurrent.futures as cf

import pytest

from tests.e2e.conftest import shot, tab, upload_via_ui
from tests.fixtures.builders import SAMPLE, big_file, error_files

pytestmark = pytest.mark.e2e
FILES = error_files()


def test_missing_values_reported(page, server):
    upload_via_ui(page, FILES["missing_values.xlsx"], "missing_values.xlsx")
    txt = page.locator("#upload-result").inner_text()
    page.click("#upload-result details.fold summary >> text=데이터 오류")
    shot(page, "e2e-errors-missing-values")
    assert "데이터 오류 3건" in txt
    d = server.api("GET", "/api/dispatch").json()
    err = [p for p in d["unassigned"] if p["status"] == "데이터오류"]
    assert len(err) == 3 and all(not p["refund_notice"] for p in err)


@pytest.mark.parametrize("name", ["empty_sheet.xlsx", "unrelated.xlsx", "not_excel.xlsx"])
def test_unreadable_files(page, name):
    upload_via_ui(page, FILES[name], name, expect_ok=False)
    txt = page.locator("#preview").inner_text()
    assert "읽을 수 없음" in txt or "찾지 못했습니다" in txt
    assert page.is_disabled("#btn-upload")


def test_product_only_file_behaviour(server):
    """제품 시트만 있는 파일 → 주문 정보가 없어 '입금대기'. (PRD에 정의 없음: 관찰 기록)"""
    r = server.api("POST", "/api/upload", files={"files": ("p.xlsx", FILES["product_only.xlsx"])}).json()
    f = r["files"][0]
    assert f.get("error") is None and f["new_cnt"] == 4  # 앞 10건 중 6건은 이전 테스트에서 이미 들어감
    # 이 제품들의 주문은 앞 파일(경매 시트 포함)로 이미 DB에 있으므로, 주문 상태대로 미배차/입금대기가 된다
    statuses = {server.api("POST", "/api/chat", json={"question": pno}).json()["rows"][0]["status"]
                for pno in ["FE00636204", "FD48053001", "FD48091403", "FD48455603"]}  # 이번에 새로 들어간 4건
    assert statuses <= {"미배차", "입금대기"}


def test_non_excel_extension_rejected_in_ui(page):
    tab(page, "upload")
    page.set_input_files("#upload-files", files=[{"name": "memo.txt", "mimeType": "text/plain", "buffer": b"hello"}])
    page.wait_for_selector(".toast.error")
    assert "엑셀 파일" in page.locator(".toast.error").last.inner_text()


def test_concurrent_write_lock(server):
    """업로드(느림)와 배차를 동시에 → 하나는 409 로 거절되어야 한다."""
    big = big_file(3000)
    with cf.ThreadPoolExecutor(2) as ex:
        up = ex.submit(server.api, "POST", "/api/upload", files={"files": ("big.xlsx", big)})
        import time

        time.sleep(0.3)
        disp = [server.api("POST", "/api/dispatch/run", json={}) for _ in range(3)]
        up_res = up.result()
    codes = [r.status_code for r in disp]
    assert up_res.status_code == 200
    assert 409 in codes, codes
    assert "진행 중" in next(r for r in disp if r.status_code == 409).json()["detail"]


def test_server_down_message(page, server):
    tab(page, "dispatch")
    server.stop()
    try:
        page.click("#btn-dispatch")
        page.wait_for_selector(".toast.error")
        assert "서버에 연결할 수 없습니다" in page.locator(".toast.error").last.inner_text()
        page.evaluate("checkHealth()")
        page.wait_for_selector("#health.error")
        shot(page, "e2e-errors-server-down")
        assert page.is_enabled("#btn-dispatch")
    finally:
        server.start()
