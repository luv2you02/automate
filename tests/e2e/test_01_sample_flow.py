"""샘플 파일 전체 흐름 (PRD F1~F8, T9·T10·T11·T12·T19·T20·T23~T27). 테스트는 순서대로 같은 서버 상태를 이어 쓴다."""
from __future__ import annotations

import time

import pytest

from tests.e2e.conftest import kpis, shot, tab, upload_via_ui
from tests.e2e.rule_check import check_export
from tests.fixtures.builders import SAMPLE, next_day, with_columns

pytestmark = pytest.mark.e2e
CTX: dict = {}


def drag(page, src_sel: str, dst_sel: str, pos=None):
    """대상을 먼저 화면에 보이게 한 뒤 끈다. (끄는 도중 페이지가 스크롤되면 sticky 미배차 패널이 움직여
    다른 제품을 잡게 된다 — Playwright 조작 특성이며 앱 버그 아님)"""
    page.locator(dst_sel).scroll_into_view_if_needed()
    page.locator(src_sel).drag_to(page.locator(dst_sel), target_position=pos or {"x": 120, "y": 60})


def test_01_upload_sample(page, server):
    upload_via_ui(page, SAMPLE)
    k = kpis(page)
    shot(page, "e2e-01-upload-result")
    assert k == {"신규": 1095, "중복": 0, "갱신": 0, "오류": 0}
    assert page.locator("#cnt-noaddr").inner_text() == "50"
    st = server.api("GET", "/api/dashboard").json()["status"]
    assert st["입금대기"]["count"] == 47 and st["미배차"]["count"] == 1048  # T24
    assert page.console_errors == []


def test_02_reupload_same_file(page, server):  # T9, T26
    vec_before = server.api("GET", "/api/health").json()["faiss"]["vectors"]
    tab(page, "upload")
    page.set_input_files("#upload-files", str(SAMPLE))
    page.wait_for_selector("#preview .note.warn")
    assert "이미 올린 적 있는 파일" in page.locator("#preview").inner_text()
    page.click("#btn-upload")
    page.wait_for_selector("#upload-result .kpis")
    assert kpis(page) == {"신규": 0, "중복": 1095, "갱신": 0, "오류": 0}
    assert "임베딩" not in page.locator("#upload-result").inner_text()  # 새로 임베딩한 것 없음
    assert server.api("GET", "/api/health").json()["faiss"]["vectors"] == vec_before


def test_03_dispatch_and_independent_rule_check(page, server):
    tab(page, "dispatch")
    t0 = time.perf_counter()
    page.click("#btn-dispatch")
    page.wait_for_selector(".run-summary")
    CTX["dispatch_ui_sec"] = time.perf_counter() - t0
    shot(page, "e2e-03-dispatch")
    xlsx = server.api("GET", "/api/export").content
    res = check_export(xlsx)
    CTX["rule_check"] = res
    assert res["violations"] == [], res["violations"]
    assert res["missed_groups"] == {}, res["missed_groups"]
    assert page.locator(".truck").count() == res["trucks"] == 94


def test_04_chat_queries_and_mms(page, server):
    d = server.api("GET", "/api/dispatch").json()
    un = next(p for p in d["unassigned"] if p["refund_notice"])
    asg = d["trucks"][0]["products"][0]
    waiting = d["waiting_payment"][0]
    cases = {
        "product_assigned": (asg["product_no"], "편성"),
        "product_unassigned": (f"{un['product_no']} 배차됐어?", "미배차"),
        "waiting": (waiting["product_no"], "입금대기"),
        "order": ("D261121029 어떻게 됐어?", None),
        "auction": ("2026110202 경매 건", None),
        "customer_plain": ("명강스틸", None),
        "customer_corp": ("(주)명강스틸 배차 현황", None),
        "dlv": ("명강스틸 2공장으로 가는 제품", None),
        "lowercase_pno": (asg["product_no"].lower(), "편성"),
    }
    results = {}
    tab(page, "chat")
    page.click("#btn-clear-chat")
    for key, (q, expect_status) in cases.items():
        r = server.api("POST", "/api/chat", json={"question": q}).json()
        results[key] = r
        assert r["found"], (key, q)
        if expect_status:
            assert r["rows"][0]["status"] == expect_status, (key, r["rows"][0]["status"])
        for m in r["messages"]:
            assert m["bytes"] <= 2000
            has_refund = "환불까지 2주" in m["text"]
            should = any(x["status"] == "미배차" and x["unassigned_reason"] in ("OVER_MAX", "UNDER_MIN")
                         for x in r["rows"] if (x["customer"] or "고객") == m["customer"])
            assert has_refund == should, (key, m["customer"])
    assert results["customer_plain"]["rows"] == results["customer_corp"]["rows"]
    assert results["dlv"]["matched"] == ["명강스틸 2공장"]
    nf = server.api("POST", "/api/chat", json={"question": "ZZ99999999 어디 있어?"}).json()
    assert nf["found"] is False and nf["message"] == "해당 제품을 찾을 수 없습니다."

    # UI: 질의 → MMS 복사 → 클립보드 비교
    page.fill("#chat-input", "D261121029 어떻게 됐어?")
    page.press("#chat-input", "Enter")
    page.wait_for_selector(".mms textarea")
    text = page.input_value(".mms textarea")
    page.click(".mms [data-copy]")
    page.wait_for_timeout(300)
    clip = page.evaluate("navigator.clipboard.readText()")
    assert clip.replace("\r\n", "\n") == text
    shot(page, "e2e-04-chat")
    CTX["chat"] = {k: (v["match_type"], len(v["rows"])) for k, v in results.items()}


def test_05_drag_and_drop_rules(page, server):
    tab(page, "dispatch")
    page.wait_for_selector(".truck")
    pair = page.evaluate("""() => {
      const d = state.dispatch;
      const wide = d.unassigned.find(p => p.status === '미배차' && p.category === 'WIDE');
      const nt = d.trucks.find(t => t.category === 'NORMAL');
      let ok = null, regionBad = null;
      for (const p of d.unassigned.filter(p => p.status === '미배차' && p.category === 'NORMAL')) {
        const t = d.trucks.find(t => t.category === 'NORMAL' && t.region === p.region && checkMove(p, null, t) === null);
        if (t && !ok) ok = {pno: p.product_no, truck: t.truck_id};
        const t2 = d.trucks.find(t => t.category === 'NORMAL' && t.region !== p.region);
        if (t2 && !regionBad) regionBad = {pno: p.product_no, truck: t2.truck_id};
      }
      return {wide: wide.product_no, normalTruck: nt.truck_id, ok, regionBad};
    }""")
    calls = []
    page.on("request", lambda r: "/api/dispatch/move" in r.url and calls.append(r.url))
    for pno, tid, expect in ((pair["wide"], pair["normalTruck"], "R2"), (pair["regionBad"]["pno"], pair["regionBad"]["truck"], "R3")):
        drag(page, f'#side-list .prow[data-pno="{pno}"]', f'.truck[data-truck="{tid}"]')
        page.wait_for_selector(".toast.error")
        assert expect in page.locator(".toast.error").last.inner_text()
        page.wait_for_timeout(200)
    assert calls == []  # 금지 이동은 서버를 호출하지 않는다
    drag(page, f'#side-list .prow[data-pno="{pair["ok"]["pno"]}"]', f'.truck[data-truck="{pair["ok"]["truck"]}"]')
    page.wait_for_selector(".toast.success")
    after = server.api("GET", "/api/dispatch").json()
    t = next(t for t in after["trucks"] if t["truck_id"] == pair["ok"]["truck"])
    assert any(p["product_no"] == pair["ok"]["pno"] for p in t["products"])
    res = check_export(server.api("GET", "/api/export").content)
    assert res["violations"] == []
    # 원상복구: 다시 미배차로
    drag(page, f'.truck[data-truck="{pair["ok"]["truck"]}"] .prow[data-pno="{pair["ok"]["pno"]}"]', "#unassigned-drop", {"x": 150, "y": 40})
    page.wait_for_timeout(800)
    shot(page, "e2e-05-drag")


def test_06_move_api_lower_bound_from_source(server):
    """출발 차량을 25t 아래로 만드는 이동은 거부되어야 한다."""
    d = server.api("GET", "/api/dispatch").json()
    t = next(t for t in d["trucks"] if len(t["products"]) >= 2)
    heavy = max(t["products"], key=lambda p: p["weight_ton"])
    r = server.api("POST", "/api/dispatch/move", json={"product_no": heavy["product_no"], "to_truck_id": None})
    assert r.status_code == 422 and "R1" in r.json()["detail"]


def test_07_persistence(page, browser, server):  # T12
    tab(page, "dispatch")
    page.select_option("#f-cat", "WIDE")
    page.wait_for_timeout(300)
    page.reload()
    page.wait_for_selector(".truck")
    assert page.input_value("#f-cat") == "WIDE" and page.locator(".truck").count() == 5
    page.click("#btn-reset-filters")
    before = server.api("GET", "/api/dashboard").json()
    server.restart()
    after = server.api("GET", "/api/dashboard").json()
    for k in ("total_products", "active_trucks", "assigned_ton"):
        assert before[k] == after[k], k
    assert server.api("POST", "/api/chat", json={"question": "D261121029"}).json()["found"]
    ctx = browser.new_context()
    p2 = ctx.new_page()
    p2.goto(server.url)
    p2.click("#tabbtn-dispatch")
    p2.wait_for_selector(".truck")
    assert p2.locator(".truck").count() == 94
    ctx.close()


def test_08_next_day_file(page, server):  # T10, T19, T27
    d = server.api("GET", "/api/dispatch").json()
    assigned_before = {p["product_no"]: t["truck_id"] for t in d["trucks"] for p in t["products"]}
    waiting = d["waiting_payment"][0]
    # 이미 편성된 제품이 있는 그룹을 취소, 다른 편성 제품 하나는 중량을 크게 늘림
    tgt_truck = d["trucks"][3]
    victim = tgt_truck["products"][0]
    bump = d["trucks"][10]["products"][0]
    dash = server.api("POST", "/api/chat", json={"question": victim["product_no"]}).json()["rows"][0]
    w = server.api("POST", "/api/chat", json={"question": waiting["product_no"]}).json()["rows"][0]
    content, info = next_day(confirm_group=(w["auction_no"], _cust_code(server, w["product_no"])),
                             cancel_group=(dash["auction_no"], _cust_code(server, victim["product_no"])),
                             bump_product=(bump["product_no"], 9000), new_products=6)
    upload_via_ui(page, content, "next_day.xlsx")
    k = kpis(page)
    txt = page.locator("#upload-result").inner_text()
    shot(page, "e2e-08-next-day")
    CTX["next_day"] = {"kpis": k, "info": info, "text": txt[:600]}
    assert k["신규"] == 6
    assert "입금대기→미배차" in txt and "편성→낙찰취소" in txt
    assert "확인 필요" in txt
    tab(page, "dispatch")
    page.click("#btn-dispatch")
    page.wait_for_selector(".run-summary")
    after = server.api("GET", "/api/dispatch").json()
    # T19: 기존 편성 제품의 차량은 그대로
    now = {p["product_no"]: t["truck_id"] for t in after["trucks"] for p in t["products"]}
    moved = [p for p, t in assigned_before.items() if p in now and now[p] != t]
    assert moved == []
    # 확인 필요 차량이 맨 위에
    assert page.locator(".truck").first.get_attribute("class").count("review") == 1
    res = check_export(server.api("GET", "/api/export").content)
    CTX["after_next_day_rule_check"] = res


def _cust_code(server, pno):
    # 업체코드는 화면 API 에 없어서, 경매번호 + 고객사명으로 경매 시트에서 찾는다
    from tests.fixtures.builders import read_sample

    s = read_sample()["Sheet1"]
    return s.loc[s["제품번호"] == pno, "업체코드"].iloc[0]


def test_09_shipped_and_length_columns(page, server):  # T20, T28
    d = server.api("GET", "/api/dispatch").json()
    truck = next(t for t in d["trucks"] if not t["needs_review"])
    shipped = {p["product_no"] for p in truck["products"]}
    upload_via_ui(page, with_columns(shipped=shipped), "with_columns.xlsx")
    txt = page.locator("#upload-result").inner_text()
    flat = " ".join(txt.split())  # <b>6</b>건 → inner_text 에서 줄바꿈이 끼므로 공백 정규화
    CTX["shipped_text"] = flat[:600]
    assert f"편성→출하완료 {len(shipped)} 건" in flat
    after = server.api("GET", "/api/dispatch").json()
    assert truck["truck_id"] not in [t["truck_id"] for t in after["trucks"]]
    r = server.api("POST", "/api/chat", json={"question": next(iter(shipped))}).json()
    assert r["rows"][0]["status"] == "출하완료" and r["messages"][0]["text"].startswith("[출하 안내]")
    assert r["rows"][0]["length_source"] == "column"
    assert page.console_errors == []


def test_99_report(server):
    import json

    from tests.e2e.conftest import SHOTS

    (SHOTS.parent / "e2e_flow_results.json").write_text(json.dumps(CTX, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
