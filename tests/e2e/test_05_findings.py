"""REVIEW.md 에 기록한 확인된 문제의 재현 테스트.

xfail(strict=True): 지금은 실패하는 것이 정상이다. 문제를 고치면 '통과(XPASS)'가 되어 strict 때문에 실패로 표시되므로,
그때 해당 xfail 표시를 지우면 된다. API 수준 재현이라 브라우저는 쓰지 않는다 (마커만 e2e).
"""
from __future__ import annotations

import time

import pytest

from tests.e2e.conftest import Server
from tests.helpers import O, P, make_places_xlsx, make_xlsx

pytestmark = pytest.mark.e2e


@pytest.fixture
def srv(tmp_path):
    s = Server(tmp_path / "데이터").start()
    yield s
    s.stop()


def up(s, content, name="t.xlsx"):
    r = s.api("POST", "/api/upload", files={"files": (name, content)})
    assert r.status_code == 200, r.text
    return r.json()["files"][0]


def prods(s):
    d = s.api("GET", "/api/dispatch?include_shipped=true").json()
    out = {p["product_no"]: p for t in d["trucks"] for p in t["products"]}
    out |= {p["product_no"]: p for p in d["unassigned"] + d["waiting_payment"]}
    return out


def chat(s, q):
    return s.api("POST", "/api/chat", json={"question": q}).json()


def test_R01_partial_cancel(srv):  # R-01 수정됨
    ps = [P("FA00000001", 13, cust="A"), P("FA00000002", 6, cust="A"), P("FA00000003", 6, cust="A")]
    o = [O("2026112001", "A", "낙찰확정", 1, 13000, "D261100001"), O("2026112001", "A", "낙찰확정", 2, 12000, "D261100002")]
    up(srv, make_xlsx(ps, o))
    up(srv, make_xlsx(ps[:1], [o[0], O("2026112001", "A", "낙찰취소", 2, 12000)]))
    r = chat(srv, "FA00000002")["rows"][0]
    assert r["status"] == "낙찰취소"


def test_R02_cannot_dismantle_underweight_truck(srv):  # R-02 수정됨
    ps = [P("FA00000001", 9, cust="A"), P("FA00000002", 9, cust="A"), P("FA00000003", 8, cust="B")]
    o = [O("2026112001", "A", "낙찰확정", 2, 18000, "D261100001"), O("2026112001", "B", "낙찰확정", 1, 8000, "D261100002")]
    up(srv, make_xlsx(ps, o))
    srv.api("POST", "/api/dispatch/run", json={})
    up(srv, make_xlsx(ps[:2], [o[0], O("2026112001", "B", "낙찰취소", 1, 8000)]))
    r = srv.api("POST", "/api/dispatch/move", json={"product_no": "FA00000001", "to_truck_id": None})
    assert r.status_code == 200, r.text


def test_R03_stale_truck_id_after_cancel(srv):  # R-03 수정됨 (재확정 파일은 R-10 으로 무시, 차량번호는 비워짐)
    ps = [P("FA00000001", 13, cust="A"), P("FA00000002", 13, cust="B")]
    o = [O("2026112001", "A", "낙찰확정", 1, 13000, "D261100001"), O("2026112001", "B", "낙찰확정", 1, 13000, "D261100002")]
    up(srv, make_xlsx(ps, o))
    srv.api("POST", "/api/dispatch/run", json={})
    up(srv, make_xlsx(ps[:1], [o[0], O("2026112001", "B", "낙찰취소", 1, 13000)]))
    up(srv, make_xlsx(ps, o))
    p = chat(srv, "FA00000002")["rows"][0]
    assert p["truck_id"] is None and not (p["status"] == "미배차" and p["truck_id"])


@pytest.mark.xfail(strict=True, reason="R-04: .xls 를 받는다고 하지만 xlrd 가 없어 읽지 못함")
def test_R04_xls_supported():
    import importlib.util

    assert importlib.util.find_spec("xlrd") is not None


def test_R05_region_query_loses_to_name_match(srv):  # R-05 수정됨
    up(srv, make_xlsx([P("FA00000001", 5, cust="A", cname="울산철강", dlv="X1", dname="울산철강 본사"),
                       P("FA00000002", 5, cust="B", cname="대한", dlv="X2", dname="대한 공장")]))
    srv.api("PUT", "/api/delivery-places/X2", json={"address": "울산광역시 남구 1"})
    srv.api("PUT", "/api/delivery-places/X1", json={"address": "경남 김해시 2"})
    rows = chat(srv, "울산 가는 제품")["rows"]
    assert "FA00000002" in [r["product_no"] for r in rows]


@pytest.mark.xfail(strict=True, reason="R-06: 임베딩 오류가 '해당 제품을 찾을 수 없습니다'로 표시됨")
def test_R06_embedding_error_reported_as_not_found(tmp_path, monkeypatch):
    import os

    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    os.environ["EMBEDDING_FAKE"] = "1"
    s = Settings(embedding_fake=True, data_dir=tmp_path / "d", open_browser=False)
    with TestClient(create_app(s)) as c:
        c.post("/api/upload", files={"files": ("t.xlsx", make_xlsx([P("FA00000001", 5)]))})
        c.app.state.embedder.embed = lambda texts: (_ for _ in ()).throw(RuntimeError("network"))
        r = c.post("/api/chat", json={"question": "의미로 찾는 아무 질문"}).json()
        assert r["message"] != "해당 제품을 찾을 수 없습니다."


def test_R07_backup_retention_by_days(srv):  # R-07 수정됨
    up(srv, make_xlsx([P("FA00000001", 5)]))
    first_daily = srv.api("GET", "/api/backups").json()["backups"][-1]["name"]
    time.sleep(1.05)  # 같은 초에 만들면 폴더 이름이 같아 덮어써진다 (R-07b) — 여기서는 보관 개수만 본다
    for _ in range(7):
        srv.api("POST", "/api/backup")
        time.sleep(1.05)  # 백업 이름이 초 단위
    names = [b["name"] for b in srv.api("GET", "/api/backups").json()["backups"]]
    assert first_daily in names


@pytest.mark.xfail(strict=True, reason="R-08: 일부 출하된 차량이 다른 제품 갱신 때 '25t 미만'으로 잘못 확인 필요 표시")
def test_R08_partially_shipped_false_flag(srv):
    o = [O("2026112001", "A", "낙찰확정", 1, 13000, "D261100001"), O("2026112001", "B", "낙찰확정", 1, 13000, "D261100002")]
    up(srv, make_xlsx([P("FA00000001", 13, cust="A"), P("FA00000002", 13, cust="B")], o))
    srv.api("POST", "/api/dispatch/run", json={})
    up(srv, make_xlsx([P("FA00000001", 13, cust="A", shipped="Y"), P("FA00000002", 13, cust="B", shipped="")], o, with_shipped=True))
    f = up(srv, make_xlsx([P("FA00000001", 13, cust="A", shipped="Y"), P("FA00000002", 13, cust="B", shipped="", spec="SS400")],
                          o, with_shipped=True))
    assert f["review_needed"] == []


@pytest.mark.xfail(strict=True, reason="R-09: 이미 확인 필요인 차량을 업로드마다 제품 수만큼 다시 보고 (8대 → 93건)")
def test_R09_review_flags_repeated_per_product(srv):
    ps = [P(f"FA0000000{i}", 3, cust="A") for i in range(1, 9)] + [P("FA00000009", 2.5, cust="B")]
    o = [O("2026112001", "A", "낙찰확정", 8, 24000, "D261100001"), O("2026112001", "B", "낙찰확정", 1, 2500, "D261100002")]
    up(srv, make_xlsx(ps, o))
    srv.api("POST", "/api/dispatch/run", json={})
    up(srv, make_xlsx(ps[:8], [o[0], O("2026112001", "B", "낙찰취소", 1, 2500)]))   # 차량 1대가 24t 로 → 확인 필요 1건
    f = up(srv, make_xlsx([P(f"FA0000000{i}", 3, cust="A", spec="SS400") for i in range(1, 9)], [o[0]]))  # 내용만 바뀐 재업로드
    assert len({x["truck_id"] for x in f["review_needed"]}) == len(f["review_needed"]) <= 1


def test_R10_older_file_rolls_back_state(srv):  # R-10 수정됨
    ps = [P("FA00000001", 13, cust="A")]
    old = [O("2026112001", "A", "낙찰", 1, 13000)]
    new = [O("2026112001", "A", "낙찰확정", 1, 13000, "D261100001")]
    up(srv, make_xlsx(ps, new))
    f = up(srv, make_xlsx(ps, old))  # 예전 상태 파일
    assert f["status_changes"] == {} or "warning" in str(f)


def test_R07b_same_second_backup_overwrites(srv):  # R-07b 수정됨
    names = set()
    for _ in range(3):
        names.add(srv.api("POST", "/api/backup").json()["name"])
    assert len(names) == 3


def test_places_fixture_builds():
    assert make_places_xlsx([("X", "경남 창원시")])
