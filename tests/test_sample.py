"""실제 샘플 파일 검증 (PRD 8장 T23~T26 + 전체 배차 규칙 검증). 샘플 원본은 읽기만 한다."""
import io

import pandas as pd
import pytest

from app.db import closing_conn
from app.services.dispatch import load_truck, validate_truck
from app.services.parser import parse_workbook
from tests.conftest import SAMPLE_XLSX
from tests.helpers import chat, dispatch, upload

pytestmark = pytest.mark.skipif(not SAMPLE_XLSX.exists(), reason="샘플 파일 없음")


@pytest.fixture(scope="module")
def sample_bytes() -> bytes:
    return SAMPLE_XLSX.read_bytes()


def test_T23_sheet_detection_independent_of_names(sample_bytes):
    r = parse_workbook(sample_bytes, "s.xlsx")
    kinds = {s.kind: s.rows for s in r.sheets}
    assert kinds == {"product": 1095, "auction": 197}
    # 시트 이름을 바꿔도 동일
    sheets = pd.read_excel(io.BytesIO(sample_bytes), sheet_name=None, dtype=str)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for i, df in enumerate(sheets.values()):
            df.to_excel(xw, sheet_name=f"시트{i}", index=False)
    r2 = parse_workbook(buf.getvalue(), "renamed.xlsx")
    assert len(r2.products) == 1095 and len(r2.orders) == 197
    assert r.errors == []


def test_T24_T25_T26_and_full_dispatch(client, settings, sample_bytes):
    res = upload(client, sample_bytes, "sample.xlsx")
    f = res["files"][0]
    assert f["new_cnt"] == 1095 and f["error_cnt"] == 0 and f["order_match_pending"] == []

    with closing_conn(settings.db_path) as conn:
        status = dict(conn.execute("SELECT status, COUNT(*) FROM products GROUP BY status").fetchall())
        waiting_from_orders = conn.execute(
            "SELECT COUNT(*) FROM products p JOIN orders o ON o.auction_no=p.auction_no AND o.customer_code=p.customer_code"
            " WHERE o.status='낙찰'").fetchone()[0]
        # T24: '낙찰'(미입금) 9건 주문의 제품 → 입금대기, 취소 제품 없음, 나머지 → 미배차
        assert conn.execute("SELECT COUNT(*) FROM orders WHERE status='낙찰'").fetchone()[0] == 9
        assert status.get("입금대기") == waiting_from_orders > 0
        assert "낙찰취소" not in status
        assert status["미배차"] == 1095 - waiting_from_orders
        # T25
        bosung = dict(conn.execute(
            "SELECT product_no, order_no FROM products WHERE auction_no='2026110202' AND customer_code='145981'"))
        assert bosung["FD41939903"] == "D261119757"
        assert sorted(set(bosung.values())) == ["D261115633", "D261119757"]
        # T31: 끝 공백 제거
        assert conn.execute("SELECT COUNT(*) FROM products WHERE dlv_name LIKE '% '").fetchone()[0] == 0

    d = dispatch(client)
    assert d["truck_count"] > 0 and d["elapsed_sec"] < 3.0
    with closing_conn(settings.db_path) as conn:
        for (tid,) in conn.execute("SELECT truck_id FROM trucks"):
            assert validate_truck(load_truck(conn, tid), settings.rules) == [], tid
        # 지역·분류 그룹별 남은 중량 < 25t (차량을 더 만들 수 있었던 그룹이 없어야 함)
        for cat, region, ton in conn.execute(
                "SELECT category, region, SUM(weight_kg)/1000.0 FROM products WHERE status='미배차' GROUP BY category, region"):
            assert ton < 25, (cat, region, ton)
        # 고객사 → 인도처 우선: 차량당 고객사 수 평균이 낮아야 한다 (참고 지표)
        assert conn.execute("SELECT COUNT(*) FROM products WHERE status='편성'").fetchone()[0] == d["assigned_products"]

    # T26: 같은 파일 다시 → 신규 0, 임베딩 0
    calls = client.app.state.embedder.calls
    f2 = upload(client, sample_bytes, "sample.xlsx")
    assert f2["files"][0]["new_cnt"] == 0 and f2["files"][0]["duplicate_file"] is True
    assert f2["embedding"]["embedded"] == 0 and client.app.state.embedder.calls == calls

    # 조회
    r = chat(client, "D261121029 배차됐어?")
    assert r["found"] and len(r["rows"]) == 28 and r["messages"][0]["customer"] == "(주)명강스틸"
    assert r["messages"][0]["bytes"] <= 2000
    assert chat(client, "명강스틸 2공장")["matched"] == ["명강스틸 2공장"]
