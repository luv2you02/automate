"""[데이터 초기화] 버튼: 확인 창, 확인 문구, 취소, 실제 초기화, 화면 정리."""
from __future__ import annotations

import pytest

from tests.e2e.conftest import shot, tab
from tests.fixtures.builders import SAMPLE

pytestmark = pytest.mark.e2e


def test_reset_button_flow(page, server):
    server.api("POST", "/api/upload", files={"files": ("s.xlsx", SAMPLE.read_bytes())})
    server.api("POST", "/api/dispatch/run", json={})
    tab(page, "chat")
    page.fill("#chat-input", "D261121029")
    page.press("#chat-input", "Enter")
    page.wait_for_selector(".mms")

    tab(page, "dashboard")
    page.click("#btn-reset")
    dlg = page.locator("#reset-dialog")
    assert dlg.is_visible()
    assert page.inner_text("#reset-count") == "1,095" and page.inner_text("#reset-trucks") == "94"
    assert page.is_disabled("#reset-submit")
    page.fill("#reset-confirm", "초기 화")
    assert page.is_disabled("#reset-submit")          # 정확히 '초기화'여야 활성화
    page.fill("#reset-confirm", "초기화")
    assert page.is_enabled("#reset-submit")
    shot(page, "reset-dialog")

    # 취소하면 아무것도 지워지지 않는다
    page.click("#reset-cancel")
    assert not dlg.is_visible()
    assert server.api("GET", "/api/health").json()["db"]["products"] == 1095

    # 실제 초기화
    page.click("#btn-reset")
    page.fill("#reset-confirm", "초기화")
    page.click("#reset-submit")
    page.wait_for_selector(".toast.success >> text=초기화했습니다")
    assert not dlg.is_visible()
    h = server.api("GET", "/api/health").json()
    assert h["db"]["products"] == 0 and h["faiss"]["vectors"] == 0
    assert any(b["kind"] == "prereset" for b in server.api("GET", "/api/backups").json()["backups"])
    # 화면도 비워진 상태
    assert page.locator("#cnt-trucks").inner_text() == ""
    page.wait_for_selector(".stat >> text=총 제품")
    assert page.locator(".stat").first.locator("b").inner_text() == "0"
    shot(page, "reset-done")
    tab(page, "chat")
    assert page.locator(".msg-user").count() == 0       # 지워진 데이터를 가리키는 대화 내역 정리
    tab(page, "dispatch")
    assert page.locator(".truck").count() == 0

    # 화면에서 '초기화 직전' 백업으로 되돌리기
    tab(page, "dashboard")
    page.click("#backup-fold summary")
    row = page.locator("#backup-list tr", has_text="초기화 직전")
    page.once("dialog", lambda d: d.accept())
    row.locator("[data-restore]").click()
    page.wait_for_selector(".toast.success >> text=되돌렸습니다")
    assert server.api("GET", "/api/health").json()["db"]["products"] == 1095
    assert len(server.api("GET", "/api/dispatch").json()["trucks"]) == 94
    page.wait_for_timeout(300)
    assert page.locator("#cnt-trucks").inner_text() == "94"
    shot(page, "reset-restored")
    assert page.console_errors == []
