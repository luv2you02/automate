"""업로드 → 배차 → 조회 전체 흐름 (API 경유). PRD 8장 T8~T13, T19~T22, T27~T31."""
import json

from fastapi.testclient import TestClient

from app.main import create_app
from tests.helpers import O, P, chat, dispatch, make_places_xlsx, make_xlsx, products, upload


def test_T8_missing_weight_is_data_error_without_refund(client):
    import io

    import pandas as pd

    content = make_xlsx([P("FA00000001", 13), P("FA00000002", 13)])
    sheets = pd.read_excel(io.BytesIO(content), sheet_name=None, dtype=str)
    sheets["Sheet1"].loc[1, "중량"] = None
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for n, df in sheets.items():
            df.to_excel(xw, sheet_name=n, index=False)
    res = upload(client, buf.getvalue())
    assert res["files"][0]["error_cnt"] == 1
    dispatch(client)
    ps = products(client)
    assert ps["FA00000002"]["status"] == "데이터오류"
    assert ps["FA00000002"]["unassigned_reason"] == "DATA_ERROR"
    assert ps["FA00000002"]["refund_notice"] is False
    msg = chat(client, "FA00000002")["messages"][0]
    assert "환불" not in msg["text"]


def test_T9_and_T26_same_file_twice(client):
    content = make_xlsx([P("FA00000001", 13), P("FA00000002", 13)])
    first = upload(client, content)
    assert first["files"][0]["new_cnt"] == 2 and first["embedding"]["embedded"] == 2
    embedder = client.app.state.embedder
    calls_before = embedder.calls
    second = upload(client, content)
    f = second["files"][0]
    assert f["duplicate_file"] is True and f["new_cnt"] == 0 and f["dup_cnt"] == 2 and f["updated_cnt"] == 0
    assert second["embedding"]["embedded"] == 0
    assert embedder.calls == calls_before  # 중복은 임베딩 API 호출 0회


def test_preview_does_not_write(client):
    r = client.post("/api/upload?preview=true", files={"files": ("a.xlsx", make_xlsx([P("FA00000001", 13)]))})
    assert r.status_code == 200 and r.json()["files"][0]["products"] == 1
    assert client.get("/api/health").json()["db"]["products"] == 0


def test_T10_carryover_combined_with_new(client):
    upload(client, make_xlsx([P("FA00000001", 13, auc="2026112001")]))
    d1 = dispatch(client, "2026-11-24")
    assert d1["truck_count"] == 0 and d1["unassigned_by_reason"] == {"UNDER_MIN": 1}
    assert products(client)["FA00000001"]["refund_notice"] is True  # T21: 이월 0일이어도 표시
    upload(client, make_xlsx([P("FA00000002", 13, auc="2026112101")]))
    d2 = dispatch(client, "2026-11-25")
    assert d2["truck_count"] == 1
    ps = products(client)
    assert ps["FA00000001"]["status"] == ps["FA00000002"]["status"] == "편성"
    assert ps["FA00000001"]["truck_id"] == ps["FA00000002"]["truck_id"]


def test_carryover_days_increment(client):
    upload(client, make_xlsx([P("FA00000001", 10)]))
    dispatch(client, "2026-11-24")
    dispatch(client, "2026-11-24")  # 같은 날 다시 실행: 증가 없음
    assert products(client)["FA00000001"]["carryover_days"] == 0
    dispatch(client, "2026-11-25")
    dispatch(client, "2026-11-26")
    assert products(client)["FA00000001"]["carryover_days"] == 2


def test_T11_order_query_with_mms(client):
    content = make_xlsx([P("FA00000001", 13, cname="(주)명강스틸"), P("FA00000002", 13, cname="(주)명강스틸")])
    upload(client, content)
    dispatch(client)
    order_no = products(client)["FA00000001"]["order_no"]
    r = chat(client, f"{order_no} 어떻게 됐어?")
    assert r["found"] and r["match_type"] == "exact" and len(r["rows"]) == 2
    m = r["messages"][0]
    assert m["customer"] == "(주)명강스틸" and f"주문번호: {order_no}" in m["text"] and "편성 완료" in m["text"]
    assert m["bytes"] <= 2000


def test_T12_persists_across_restart(settings):
    with TestClient(create_app(settings)) as c:
        upload(c, make_xlsx([P("FA00000001", 13), P("FA00000002", 13)]))
        dispatch(c)
    with TestClient(create_app(settings)) as c:
        ps = products(c)
        assert ps["FA00000001"]["status"] == "편성"
        assert chat(c, "FA00000001")["rows"][0]["truck_id"] == ps["FA00000001"]["truck_id"]
        assert c.get("/api/health").json()["faiss"]["vectors"] == 2


def test_T13_unknown_product(client):
    upload(client, make_xlsx([P("FA00000001", 13)]))
    r = chat(client, "ZZ99999999 배차됐어?")
    assert r["found"] is False and r["message"] == "해당 제품을 찾을 수 없습니다." and r["messages"] == []


def test_T19_assigned_products_not_redispatched(client):
    upload(client, make_xlsx([P("FA00000001", 13), P("FA00000002", 13)]))
    dispatch(client, "2026-11-24")
    before = products(client)
    upload(client, make_xlsx([P("FA00000003", 12.5, auc="2026112101"), P("FA00000004", 12.5, auc="2026112101")]))
    d = dispatch(client, "2026-11-25")
    after = products(client)
    for pno in ("FA00000001", "FA00000002"):
        assert after[pno]["truck_id"] == before[pno]["truck_id"] == "2026-11-24-N01"
    assert d["truck_ids"] == ["2026-11-25-N01"]
    assert after["FA00000003"]["truck_id"] == "2026-11-25-N01"


def test_T20_shipped_column_marks_shipped(client):
    prods = [P("FA00000001", 13), P("FA00000002", 13)]
    content = make_xlsx(prods)
    upload(client, content)
    dispatch(client)
    shipped = [P("FA00000001", 13, shipped="Y"), P("FA00000002", 13, shipped="2026-11-24")]
    # 같은 주문으로 다시 (출하여부 컬럼만 추가)
    import io

    import pandas as pd

    orders_sheet = pd.read_excel(io.BytesIO(content), sheet_name="Sheet1 (2)", dtype=str)
    from tests.helpers import O

    o = orders_sheet.iloc[0]
    res = upload(client, make_xlsx(shipped, [O(o["경매번호"], o["고객코드"], "낙찰확정", 2, 26000, o["주문번호"])],
                                   with_shipped=True))
    assert res["files"][0]["status_changes"].get("편성→출하완료") == 2
    ps = products(client)
    assert ps["FA00000001"]["status"] == "출하완료"
    r = chat(client, "FA00000001")
    assert r["rows"][0]["status"] == "출하완료" and r["messages"][0]["text"].startswith("[출하 안내]")
    assert client.get("/api/dispatch").json()["trucks"] == []  # 출하된 차량은 기본 목록에서 빠짐


def test_shipped_column_absent_keeps_status(client):
    content = make_xlsx([P("FA00000001", 13), P("FA00000002", 13)])
    upload(client, content)
    dispatch(client)
    upload(client, make_xlsx([P("FA00000001", 13), P("FA00000002", 13)]))
    assert products(client)["FA00000001"]["status"] == "편성"


def test_T22_no_chat_model(client, monkeypatch):
    assert client.app.state.settings.chat_model is None

    def boom(*a, **k):
        raise AssertionError("LLM이 호출되면 안 됩니다")

    monkeypatch.setattr("app.routers.chat._polish_with_llm", boom)
    upload(client, make_xlsx([P("FA00000001", 13)]))
    r = chat(client, "FA00000001")
    assert r["found"] and r["llm_used"] is False and r["messages"][0]["text"]


def test_T24_waiting_payment_and_cancel(client):
    prods = [P("FA00000001", 13, auc="2026112001", cust="A"), P("FA00000002", 13, auc="2026112001", cust="B")]
    orders = [O("2026112001", "A", "낙찰", 1, 13000), O("2026112001", "B", "낙찰확정", 1, 13000, "D261100001")]
    upload(client, make_xlsx(prods, orders))
    ps = products(client)
    assert ps["FA00000001"]["status"] == "입금대기"
    assert ps["FA00000002"]["status"] == "미배차"
    # 다음 날: A 확정, B 취소
    orders2 = [O("2026112001", "A", "낙찰확정", 1, 13000, "D261100002"), O("2026112001", "B", "낙찰취소", 1, 13000)]
    res = upload(client, make_xlsx(prods, orders2))
    changes = res["files"][0]["status_changes"]
    assert changes == {"입금대기→미배차": 1, "미배차→낙찰취소": 1}  # T27
    ps = products(client)
    assert ps.get("FA00000002") is None  # 낙찰취소는 배차 화면 목록에 없음
    assert chat(client, "FA00000002")["rows"][0]["status"] == "낙찰취소"
    assert ps["FA00000001"]["status"] == "미배차" and ps["FA00000001"]["order_no"] == "D261100002"


def test_cancel_after_assignment_flags_truck(client):
    prods = [P("FA00000001", 13, cust="A"), P("FA00000002", 13, cust="B")]
    upload(client, make_xlsx(prods, [O("2026112001", "A", "낙찰확정", 1, 13000, "D261100001"),
                                     O("2026112001", "B", "낙찰확정", 1, 13000, "D261100002")]))
    dispatch(client)
    res = upload(client, make_xlsx(prods, [O("2026112001", "A", "낙찰확정", 1, 13000, "D261100001"),
                                           O("2026112001", "B", "낙찰취소", 1, 13000)]))
    f = res["files"][0]
    assert f["status_changes"] == {"편성→낙찰취소": 1}
    assert f["review_needed"] and f["review_needed"][0]["truck_id"] == "2026-11-24-N01"
    truck = client.get("/api/dispatch").json()["trucks"][0]
    assert truck["needs_review"] == 1 and truck["total_ton"] == 13.0


def test_R01_partial_cancel_unassigned(client):
    prods = [P("FA00000001", 13, cust="A"), P("FA00000002", 6, cust="A"), P("FA00000003", 6, cust="A")]
    o = [O("2026112001", "A", "낙찰확정", 1, 13000, "D261100001"), O("2026112001", "A", "낙찰확정", 2, 12000, "D261100002")]
    upload(client, make_xlsx(prods, o))
    res = upload(client, make_xlsx(prods[:1], [o[0], O("2026112001", "A", "낙찰취소", 2, 12000)]))
    assert res["files"][0]["status_changes"] == {"미배차→낙찰취소": 2}
    assert chat(client, "FA00000002")["rows"][0]["status"] == "낙찰취소"
    assert products(client)["FA00000001"]["status"] == "미배차"


def test_R01_partial_cancel_after_assignment_flags_truck(client):
    prods = [P("FA00000001", 13, cust="A"), P("FA00000002", 6, cust="A"), P("FA00000003", 6.5, cust="A")]
    o = [O("2026112001", "A", "낙찰확정", 1, 13000, "D261100001"), O("2026112001", "A", "낙찰확정", 2, 12500, "D261100002")]
    upload(client, make_xlsx(prods, o))
    assert dispatch(client)["truck_count"] == 1
    res = upload(client, make_xlsx(prods[:1], [o[0], O("2026112001", "A", "낙찰취소", 2, 12500)]))
    f = res["files"][0]
    assert f["status_changes"] == {"편성→낙찰취소": 2}
    assert {x["truck_id"] for x in f["review_needed"]} == {"2026-11-24-N01"}


def test_R01_undecidable_cancel_is_held_from_dispatch(client):
    prods = [P("FA00000001", 10, cust="A"), P("FA00000002", 10, cust="A"), P("FA00000003", 10, cust="A")]
    o = [O("2026112001", "A", "낙찰확정", 2, 20000, "D261100001"), O("2026112001", "A", "낙찰확정", 1, 10000, "D261100002")]
    upload(client, make_xlsx(prods, o))
    # 경매 시트만 다시 (제품 시트 없음) → 어느 10t 가 취소분인지 모름
    res = upload(client, make_xlsx(prods, [o[0], O("2026112001", "A", "낙찰취소", 1, 10000)], include_products=False))
    assert len(res["files"][0]["review_needed"]) == 3
    assert dispatch(client)["truck_count"] == 0           # 보류 제품은 배차하지 않음
    ps = products(client)
    assert all(ps[p]["needs_review"] for p in ("FA00000001", "FA00000002", "FA00000003"))
    # 제품 시트로 취소분이 확인되면 보류 해제
    res = upload(client, make_xlsx(prods[:2], [o[0], O("2026112001", "A", "낙찰취소", 1, 10000)]))
    ps = products(client)
    assert not ps["FA00000001"]["needs_review"] and ps["FA00000001"]["status"] == "미배차"
    assert chat(client, "FA00000003")["rows"][0]["status"] == "낙찰취소"


def test_R10_older_file_does_not_roll_back_confirmation(client):
    ps = [P("FA00000001", 13, cust="A")]
    upload(client, make_xlsx(ps, [O("2026112001", "A", "낙찰확정", 1, 13000, "D261100001")]))
    res = upload(client, make_xlsx(ps, [O("2026112001", "A", "낙찰", 1, 13000)]))
    f = res["files"][0]
    assert f["status_changes"] == {}
    assert f["stale_groups"] and f["stale_groups"][0]["db"] == "확정 1 · 낙찰 0 · 취소 0"
    p = products(client)["FA00000001"]
    assert p["status"] == "미배차" and p["order_no"] == "D261100001"


def test_R10_older_file_does_not_undo_cancel(client):
    ps = [P("FA00000001", 13, cust="A"), P("FA00000002", 13, cust="B")]
    o = [O("2026112001", "A", "낙찰확정", 1, 13000, "D261100001"), O("2026112001", "B", "낙찰확정", 1, 13000, "D261100002")]
    upload(client, make_xlsx(ps, o))
    upload(client, make_xlsx(ps[:1], [o[0], O("2026112001", "B", "낙찰취소", 1, 13000)]))
    res = upload(client, make_xlsx(ps, o))  # 취소 전 상태의 예전 파일
    f = res["files"][0]
    assert "낙찰취소→미배차" not in f["status_changes"]
    assert [g["customer_code"] for g in f["stale_groups"]] == ["B"]
    assert chat(client, "FA00000002")["rows"][0]["status"] == "낙찰취소"


def test_R10_normal_progression_still_applies(client):
    ps = [P("FA00000001", 13, cust="A")]
    upload(client, make_xlsx(ps, [O("2026112001", "A", "낙찰", 1, 13000)]))
    f = upload(client, make_xlsx(ps, [O("2026112001", "A", "낙찰확정", 1, 13000, "D261100001")]))["files"][0]
    assert f["status_changes"] == {"입금대기→미배차": 1} and f["stale_groups"] == []
    f = upload(client, make_xlsx(ps, [O("2026112001", "A", "낙찰취소", 1, 13000)]))["files"][0]
    assert f["status_changes"] == {"미배차→낙찰취소": 1} and f["stale_groups"] == []


def test_changed_weight_on_assigned_product_flags_review(client):
    upload(client, make_xlsx([P("FA00000001", 13), P("FA00000002", 13)]))
    dispatch(client)
    res = upload(client, make_xlsx([P("FA00000001", 13), P("FA00000002", 14.5)]))  # 27.5t > 27t
    f = res["files"][0]
    assert f["updated_cnt"] == 1
    ps = products(client)
    assert ps["FA00000002"]["status"] == "편성"  # 재배차하지 않음
    assert ps["FA00000002"]["needs_review"] is True and "R1" in ps["FA00000002"]["review_note"]


def test_T25_multi_order_matching_via_upload(client):
    prods = [P("FD40563202", 5.291, auc="2026110202", cust="145981"), P("FD40618602", 5.095, auc="2026110202", cust="145981"),
             P("FD41939903", 4.909, auc="2026110202", cust="145981"), P("FE01851702", 2.217, auc="2026110202", cust="145981"),
             P("FE01851802", 3.266, auc="2026110202", cust="145981")]
    orders = [O("2026110202", "145981", "낙찰확정", 1, 4909, "D261119757"),
              O("2026110202", "145981", "낙찰확정", 4, 15869, "D261115633")]
    upload(client, make_xlsx(prods, orders))
    ps = products(client)
    assert ps["FD41939903"]["order_no"] == "D261119757"
    assert {ps[p]["order_no"] for p in ("FD40563202", "FD40618602", "FE01851702", "FE01851802")} == {"D261115633"}


def test_ambiguous_order_match_listed_and_searchable(client):
    prods = [P("FA00000001", 1, cust="A"), P("FA00000002", 1, cust="A")]
    orders = [O("2026112001", "A", "낙찰확정", 1, 1000, "D261100011"), O("2026112001", "A", "낙찰확정", 1, 1000, "D261100012")]
    res = upload(client, make_xlsx(prods, orders))
    assert len(res["files"][0]["order_match_pending"]) == 2
    r = chat(client, "D261100012")
    assert len(r["rows"]) == 2 and r["rows"][0]["order_candidates"] == ["D261100011", "D261100012"]


def test_T28_length_calc_and_long_rule(client):
    # 길이 컬럼 없음: 13.5t, 두께 20, 폭 2500 → 계산 길이 34.4m → 장척 → 상한 26t
    upload(client, make_xlsx([P("FA00000001", 13.5, t=20), P("FA00000002", 13.5, t=20)]))
    ps = products(client)
    assert ps["FA00000001"]["length_source"] == "calc" and ps["FA00000001"]["is_long"] is True
    d = dispatch(client)
    assert d["truck_count"] == 0


def test_T4_long_from_length_column(client):
    content = make_xlsx([P("FA00000001", 13.5, length=12000), P("FA00000002", 13.5, length=18000)], with_length=True)
    upload(client, content)
    assert dispatch(client)["truck_count"] == 0
    ps = products(client)
    assert ps["FA00000001"]["refund_notice"] and ps["FA00000001"]["unassigned_reason"] == "UNDER_MIN"


def test_T29_same_route_different_places_mix(client):
    upload(client, make_xlsx([P("FA00000001", 13, cust="A", dlv="X1", route="M1EN64"),
                              P("FA00000002", 13, cust="B", dlv="X2", route="M1EN64")]))
    assert dispatch(client)["truck_count"] == 1


def test_T15_T16_T18_T30_address_regions(client):
    prods = [P("FA00000001", 13, cust="A", dlv="CW1", route="R1"),   # 창원 (주소 '경남')
             P("FA00000002", 13, cust="B", dlv="CW2", route="R2"),   # 창원 (주소 '경상남도') → T18 같은 지역
             P("FA00000003", 13, cust="C", dlv="GH1", route="R1"),   # 김해 → T15 다른 시
             P("FA00000004", 13, cust="D", dlv="NOADDR", route="R1")]  # 주소 없음 → 운송경로 R1 (T30)
    upload(client, make_xlsx(prods))
    r = client.post("/api/delivery-places", files={"file": ("p.xlsx", make_places_xlsx([
        ("CW1", "경남 창원시 성산구 공단로 1"), ("CW2", "경상남도 창원시 의창구 2"), ("GH1", "경남 김해시 주촌면 3")]))})
    assert r.status_code == 200, r.text
    ps = products(client)
    assert ps["FA00000001"]["region"] == ps["FA00000002"]["region"] == "경상남도 창원시"
    assert ps["FA00000003"]["region"] == "경상남도 김해시"
    assert ps["FA00000004"]["region"] == "R1" and ps["FA00000004"]["region_source"] == "route"
    d = dispatch(client)
    assert d["truck_count"] == 1  # 창원 A+B 만
    ps = products(client)
    assert ps["FA00000001"]["truck_id"] == ps["FA00000002"]["truck_id"]
    assert ps["FA00000003"]["status"] == ps["FA00000004"]["status"] == "미배차"


def test_manual_address_edit_and_validation(client):
    upload(client, make_xlsx([P("FA00000001", 13, dlv="X1")]))
    assert client.put("/api/delivery-places/X1", json={"address": "창원 어딘가"}).status_code == 400
    assert client.put("/api/delivery-places/NOPE", json={"address": "경남 창원시"}).status_code == 404
    assert client.put("/api/delivery-places/X1", json={"address": "경남 창원시 성산구"}).status_code == 200
    assert products(client)["FA00000001"]["region"] == "경상남도 창원시"
    places = client.get("/api/delivery-places").json()["places"]
    assert places[0]["region_source"] == "address"


def test_T31_trailing_space_name_searchable(client):
    upload(client, make_xlsx([P("FA00000001", 13, dlv="UL1", dname="MH밴딩 울산공장 ", cname="(주)엠에이치")]))
    assert products(client)["FA00000001"]["dlv_name"] == "MH밴딩 울산공장"
    r = chat(client, "MH밴딩 울산공장으로 가는 거")
    assert r["found"] and r["matched"] == ["MH밴딩 울산공장"]


def test_name_search_variants(client):
    upload(client, make_xlsx([P("FA00000001", 13, cust="A", cname="서광철강산업（주）", dlv="S1", dname="서광철강산업（주） 본사"),
                              P("FA00000002", 13, cust="B", cname="주식회사 신우", dlv="S2", dname="고연공단")]))
    assert chat(client, "서광철강산업 배차 현황")["rows"][0]["product_no"] == "FA00000001"
    assert chat(client, "신우")["rows"][0]["product_no"] == "FA00000002"
    assert chat(client, "고연공단 가는 제품")["matched"] == ["고연공단"]


def _region_fixture(client):
    upload(client, make_xlsx([
        P("FA00000001", 5, cust="A", cname="울산철강", dlv="X1", dname="울산철강 본사"),      # 이름엔 울산, 위치는 김해
        P("FA00000002", 5, cust="B", cname="대한", dlv="X2", dname="대한 공장"),              # 울산광역시
        P("FA00000003", 5, cust="C", cname="창성", dlv="X3", dname="창성 본사"),              # 창원시
        P("FA00000004", 5, cust="D", cname="경남산업", dlv="X4", dname="경남산업 본사", route="M1EN64"),  # 주소 없음
    ]))
    for code, addr in (("X1", "경남 김해시 주촌면 1"), ("X2", "울산광역시 남구 2"), ("X3", "경상남도 창원시 성산구 3")):
        assert client.put(f"/api/delivery-places/{code}", json={"address": addr}).status_code == 200


def test_R05_region_query_beats_partial_name(client):
    _region_fixture(client)
    r = chat(client, "울산 가는 제품")
    assert r["match_type"] == "region" and [x["product_no"] for x in r["rows"]] == ["FA00000002"]
    assert [x["product_no"] for x in chat(client, "창원시로 가는 거")["rows"]] == ["FA00000003"]
    assert [x["product_no"] for x in chat(client, "김해 쪽 배차 현황")["rows"]] == ["FA00000001"]   # 시 이름만
    assert [x["product_no"] for x in chat(client, "경남 김해")["rows"]] == ["FA00000001"]           # 시·도 + 시
    assert [x["product_no"] for x in chat(client, "M1EN64 제품")["rows"]] == ["FA00000004"]        # 운송경로


def test_R05_full_name_still_wins_over_region(client):
    _region_fixture(client)
    r = chat(client, "울산철강 본사로 가는 제품")
    assert r["match_type"] == "name" and [x["product_no"] for x in r["rows"]] == ["FA00000001"]
    r = chat(client, "경남산업")  # '경남'은 시·도 이름이지만 고객사 이름 전체가 일치
    assert [x["product_no"] for x in r["rows"]] == ["FA00000004"]
    r = chat(client, "울산철")  # 이름 일부 (지역어 아님) → 이름 일부 일치
    assert [x["product_no"] for x in r["rows"]] == ["FA00000001"]


def test_auction_number_query(client):
    upload(client, make_xlsx([P("FA00000001", 13, auc="2026110202"), P("FA00000002", 13, auc="2026110203")]))
    r = chat(client, "2026110202 경매 건")
    assert [x["product_no"] for x in r["rows"]] == ["FA00000001"]


# ---------- 수동 이동 ----------
def test_move_rules(client):
    # 고객사 우선 규칙으로 A(13+13=26t), B(12.6+12.6=25.2t) 차량이 만들어지고 C 1t 는 남는다
    upload(client, make_xlsx([P("FA00000001", 13, cust="A"), P("FA00000002", 13, cust="A"),
                              P("FA00000003", 12.6, cust="B"), P("FA00000004", 12.6, cust="B"),
                              P("FA00000005", 1.0, cust="C")]))
    dispatch(client)
    ps = products(client)
    t1, t2 = ps["FA00000001"]["truck_id"], ps["FA00000003"]["truck_id"]
    assert t1 != t2
    # 1t 짜리 미배차 → t2(25.2t)에 넣으면 26.2t: 허용
    r = client.post("/api/dispatch/move", json={"product_no": "FA00000005", "to_truck_id": t2})
    assert r.status_code == 200, r.text
    # t1(26t)에서 13t 빼서 t2로: t1 13t(하한 미달), t2 39t(상한 초과) → 거부
    r = client.post("/api/dispatch/move", json={"product_no": "FA00000001", "to_truck_id": t2})
    assert r.status_code == 422 and "R1" in r.json()["detail"]
    # t2 에서 1t 를 다시 미배차로: 25.2t 남음 → 허용
    r = client.post("/api/dispatch/move", json={"product_no": "FA00000005", "to_truck_id": None})
    assert r.status_code == 200
    assert products(client)["FA00000005"]["status"] == "미배차"
    assert client.post("/api/dispatch/move", json={"product_no": "NOPE", "to_truck_id": t2}).status_code == 422


def _underweight_truck(client):
    """9+9+8t 차량에서 8t 주문이 취소되어 18t '확인 필요' 차량을 만든다."""
    prods = [P("FA00000001", 9, cust="A"), P("FA00000002", 9, cust="A"), P("FA00000003", 8, cust="B")]
    o = [O("2026112001", "A", "낙찰확정", 2, 18000, "D261100001"), O("2026112001", "B", "낙찰확정", 1, 8000, "D261100002")]
    upload(client, make_xlsx(prods, o))
    dispatch(client)
    upload(client, make_xlsx(prods[:2], [o[0], O("2026112001", "B", "낙찰취소", 1, 8000)]))
    return prods, o


def test_R03_cancelled_product_leaves_truck(client):
    prods, o = _underweight_truck(client)
    t = client.get("/api/dispatch").json()["trucks"][0]
    assert [p["product_no"] for p in t["products"]] == ["FA00000001", "FA00000002"]  # 취소 제품은 빠짐
    assert t["total_ton"] == 18.0 and t["needs_review"] == 1
    assert "FA00000003 제외됨" in t["review_note"]
    assert chat(client, "FA00000003")["rows"][0]["truck_id"] is None
    # 취소 전 파일을 다시 올려도 (R-10) 취소는 유지되고 차량에 다시 붙지 않는다
    f = upload(client, make_xlsx(prods, o))["files"][0]
    assert [g["customer_code"] for g in f["stale_groups"]] == ["B"]
    r = chat(client, "FA00000003")["rows"][0]
    assert r["status"] == "낙찰취소" and r["truck_id"] is None


def test_R03_truck_deleted_when_all_cancelled(client):
    prods = [P("FA00000001", 13, cust="A"), P("FA00000002", 13, cust="A")]
    o = [O("2026112001", "A", "낙찰확정", 2, 26000, "D261100001")]
    upload(client, make_xlsx(prods, o))
    dispatch(client)
    upload(client, make_xlsx([], [O("2026112001", "A", "낙찰취소", 2, 26000)], include_products=False))
    assert client.get("/api/dispatch").json()["trucks"] == []


def test_R03_startup_repairs_stale_truck_ids(settings):
    from app.db import closing_conn

    with TestClient(create_app(settings)) as c:
        upload(c, make_xlsx([P("FA00000001", 13), P("FA00000002", 13)]))
        dispatch(c)
    with closing_conn(settings.db_path) as conn:  # 예전 버전이 남긴 상태를 흉내
        conn.execute("UPDATE products SET status='미배차' WHERE product_no='FA00000002'")
        conn.commit()
    with TestClient(create_app(settings)) as c:
        assert products(c)["FA00000002"]["truck_id"] is None


def test_R02_can_move_out_of_underweight_truck(client):
    _underweight_truck(client)
    r = client.post("/api/dispatch/move", json={"product_no": "FA00000001", "to_truck_id": None})
    assert r.status_code == 200, r.text
    t = client.get("/api/dispatch").json()["trucks"][0]
    assert t["total_ton"] == 9.0 and t["needs_review"] == 1  # 아직 규칙 밖 → 표시 유지


def test_R02_fill_underweight_truck_clears_review(client):
    _underweight_truck(client)
    upload(client, make_xlsx([P("FA00000009", 7.5, cust="C", auc="2026112101")]))
    r = client.post("/api/dispatch/move", json={"product_no": "FA00000009", "to_truck_id": "2026-11-24-N01"})
    assert r.status_code == 200, r.text
    t = client.get("/api/dispatch").json()["trucks"][0]
    assert t["total_ton"] == 25.5 and t["needs_review"] == 0 and t["review_note"] is None


def test_R02_fill_must_not_break_other_rules(client):
    _underweight_truck(client)
    upload(client, make_xlsx([P("FA00000009", 7, cust="C", auc="2026112101", w=3600),     # 광폭 → R2
                              P("FA00000010", 7, cust="D", auc="2026112102", route="R9")]))  # 다른 지역 → R3
    for pno, rule in (("FA00000009", "R2"), ("FA00000010", "R3")):
        r = client.post("/api/dispatch/move", json={"product_no": pno, "to_truck_id": "2026-11-24-N01"})
        assert r.status_code == 422 and rule in r.json()["detail"], r.text


def test_R02_valid_truck_still_protected(client):
    upload(client, make_xlsx([P("FA00000001", 13), P("FA00000002", 13)]))
    dispatch(client)
    r = client.post("/api/dispatch/move", json={"product_no": "FA00000001", "to_truck_id": None})
    assert r.status_code == 422 and "R1" in r.json()["detail"]


def test_R02_dismantle_truck(client):
    _underweight_truck(client)
    r = client.post("/api/dispatch/dismantle", json={"truck_id": "2026-11-24-N01"})
    assert r.status_code == 200 and r.json()["released"] == 2
    d = client.get("/api/dispatch").json()
    assert d["trucks"] == []
    assert {p["product_no"] for p in d["unassigned"]} == {"FA00000001", "FA00000002"}
    assert client.post("/api/dispatch/dismantle", json={"truck_id": "NOPE"}).status_code == 422


def test_move_rejects_wide_into_normal(client):
    upload(client, make_xlsx([P("FA00000001", 13), P("FA00000002", 13), P("FA00000003", 0.5, w=3500)]))
    dispatch(client)
    t = products(client)["FA00000001"]["truck_id"]
    r = client.post("/api/dispatch/move", json={"product_no": "FA00000003", "to_truck_id": t})
    assert r.status_code == 422 and "R2" in r.json()["detail"]


# ---------- 내보내기 / 대시보드 / 백업 ----------
def test_export_and_dashboard(client):
    upload(client, make_xlsx([P("FA00000001", 13), P("FA00000002", 13), P("FA00000003", 5)]))
    dispatch(client)
    r = client.get("/api/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    import io

    import pandas as pd

    sheets = pd.read_excel(io.BytesIO(r.content), sheet_name=None)
    assert set(sheets) == {"차량요약", "편성제품", "미배차"}
    assert len(sheets["편성제품"]) == 2 and len(sheets["미배차"]) == 1
    d = client.get("/api/dashboard").json()
    assert d["active_trucks"] == 1 and d["status"]["편성"]["count"] == 2 and d["assigned_ton"] == 26.0


def test_backup_and_restore(client, settings):
    upload(client, make_xlsx([P("FA00000001", 13)]))
    name = client.post("/api/backup").json()["name"]
    upload(client, make_xlsx([P("FA00000002", 13, auc="2026112101")]))
    assert client.get("/api/health").json()["db"]["products"] == 2
    assert name in [b["name"] for b in client.get("/api/backups").json()["backups"]]
    r = client.post("/api/restore", json={"name": name})
    assert r.status_code == 200, r.text
    assert client.get("/api/health").json()["db"]["products"] == 1
    assert client.post("/api/restore", json={"name": "../../etc"}).status_code == 400


def test_R07_backups_kept_by_days_not_count(client, settings):
    upload(client, make_xlsx([P("FA00000001", 13)]))
    auto = [b for b in client.get("/api/backups").json()["backups"] if b["kind"] == "auto"]
    assert len(auto) == 1
    names = {client.post("/api/backup").json()["name"] for _ in range(9)}  # 같은 초에 연달아 만들어도
    assert len(names) == 9                                                 # 이름이 겹치지 않는다 (R-07b)
    kept = {b["name"] for b in client.get("/api/backups").json()["backups"]}
    assert auto[0]["name"] in kept and names <= kept                        # 수동 9번에도 자동 백업 유지


def test_R07_prune_removes_only_older_than_7_days(settings):
    from datetime import date

    from app.services.backup import list_backups, prune

    for name in ["20261110-090000-auto", "20261117-090000-auto", "20261118-090000-auto", "20261124-090000-manual"]:
        d = settings.backup_dir / name
        d.mkdir(parents=True)
        (d / "app.db").write_bytes(b"x")
    removed = prune(settings, today=date(2026, 11, 24))
    assert sorted(removed) == ["20261110-090000-auto", "20261117-090000-auto"]  # 11/18~11/24 (7일) 유지
    assert [b["name"] for b in list_backups(settings)] == ["20261124-090000-manual", "20261118-090000-auto"]


def test_R07_restore_makes_safety_backup(client, settings):
    upload(client, make_xlsx([P("FA00000001", 13)]))
    name = client.post("/api/backup").json()["name"]
    upload(client, make_xlsx([P("FA00000002", 13, auc="2026112101")]))
    r = client.post("/api/restore", json={"name": name}).json()
    assert r["safety_backup"].endswith("prerestore") or "-prerestore" in r["safety_backup"]
    assert client.get("/api/health").json()["db"]["products"] == 1
    # 잘못 복원했으면 안전 백업으로 되돌릴 수 있다
    client.post("/api/restore", json={"name": r["safety_backup"]})
    assert client.get("/api/health").json()["db"]["products"] == 2


def _loaded(client):
    upload(client, make_xlsx([P("FA00000001", 13, dlv="X1"), P("FA00000002", 13, dlv="X1")]))
    client.put("/api/delivery-places/X1", json={"address": "경남 창원시 성산구 1"})
    dispatch(client)


def test_reset_requires_confirm_word(client):
    _loaded(client)
    for word in ("", "reset", "초기 화"):
        r = client.post("/api/reset", json={"confirm": word})
        assert r.status_code == 400
    assert client.get("/api/health").json()["db"]["products"] == 2


def test_reset_clears_data_and_keeps_places(client, settings):
    _loaded(client)
    r = client.post("/api/reset", json={"confirm": "초기화"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"]["products"] == 2 and body["deleted"]["trucks"] == 1 and body["kept_places"]
    h = client.get("/api/health").json()
    assert h["db"]["products"] == 0 and h["faiss"]["vectors"] == 0
    assert client.get("/api/dispatch").json() == {"trucks": [], "unassigned": [], "waiting_payment": []}
    assert client.get("/api/uploads").json()["uploads"] == []
    places = client.get("/api/delivery-places").json()["places"]
    assert [(p["dlv_code"], p["address"], p["route"]) for p in places] == [("X1", "경남 창원시 성산구 1", None)]
    assert body["backup"].endswith("-prereset")
    # 다시 올리면 정상 동작하고, 남긴 주소가 바로 적용된다
    upload(client, make_xlsx([P("FA00000001", 13, dlv="X1")]))
    p = products(client)["FA00000001"]
    assert p["region"] == "경상남도 창원시" and p["region_source"] == "address"
    assert client.get("/api/health").json()["faiss"]["vectors"] == 1


def test_reset_without_keeping_places(client):
    _loaded(client)
    client.post("/api/reset", json={"confirm": "초기화", "keep_places": False})
    assert client.get("/api/delivery-places").json()["places"] == []


def test_reset_can_be_undone_with_backup(client):
    _loaded(client)
    name = client.post("/api/reset", json={"confirm": "초기화"}).json()["backup"]
    r = client.post("/api/restore", json={"name": name})
    assert r.status_code == 200
    assert client.get("/api/health").json()["db"]["products"] == 2
    assert len(client.get("/api/dispatch").json()["trucks"]) == 1


def test_reset_respects_write_lock(client):
    from app import locks

    with locks.exclusive("업로드"):
        r = client.post("/api/reset", json={"confirm": "초기화"})
    assert r.status_code == 409


def test_daily_backup_created_on_upload(client, settings):
    upload(client, make_xlsx([P("FA00000001", 13)]))
    assert len(client.get("/api/backups").json()["backups"]) == 1
    upload(client, make_xlsx([P("FA00000002", 13, auc="2026112101")]))
    assert len(client.get("/api/backups").json()["backups"]) == 1  # 하루 1회


def test_status_history_recorded(client, settings):
    upload(client, make_xlsx([P("FA00000001", 13), P("FA00000002", 13)]))
    dispatch(client)
    from app.db import closing_conn

    with closing_conn(settings.db_path) as conn:
        hist = [tuple(r) for r in conn.execute(
            "SELECT h.from_status, h.to_status FROM status_history h JOIN products p ON p.id=h.product_id"
            " WHERE p.product_no='FA00000001' ORDER BY h.id")]
    assert hist == [(None, "미배차"), ("미배차", "편성")]


def test_embedding_failure_queued_and_retried(client, monkeypatch):
    upload(client, make_xlsx([P("FA00000001", 13)]))
    embedder = client.app.state.embedder
    real = embedder.embed

    def fail(texts):
        raise RuntimeError("network down")

    monkeypatch.setattr(embedder, "embed", fail)
    res = upload(client, make_xlsx([P("FA00000002", 13, auc="2026112101")]))
    assert res["embedding"]["error"] and res["embedding"]["pending"] == 1
    assert client.get("/api/dashboard").json()["embedding_pending"] == 1
    monkeypatch.setattr(embedder, "embed", real)
    res = upload(client, make_xlsx([P("FA00000003", 13, auc="2026112201")]))
    assert res["embedding"]["embedded"] == 2 and res["embedding"]["pending"] == 0
