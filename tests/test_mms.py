from app.services.mms import MMS_MAX_BYTES, REFUND_NOTICE, build_customer_mms, build_mms, mms_bytes


def row(pno, status="편성", order="D261121029", reason=None, ton=2.5, customer="(주)명강스틸", dlv="명강스틸 2공장",
        plan="2026-11-25", shipped_at=None):
    return {"product_no": pno, "status": status, "order_no": order, "order_candidates": [], "unassigned_reason": reason,
            "weight_ton": ton, "customer": customer, "dlv_name": dlv, "address": None, "ship_plan_date": plan,
            "shipped_at": shipped_at}


def test_bytes_euc_kr():
    assert mms_bytes("abc") == 3
    assert mms_bytes("가나") == 4
    assert mms_bytes("😀") == 2  # EUC-KR 불가 문자는 2바이트로 계산


def test_assigned_template():
    m = build_customer_mms("(주)명강스틸", [row("FD48282003"), row("FD48282004")])
    t = m["text"]
    assert t.startswith("[배차 안내]")
    assert "- 주문번호: D261121029" in t
    assert "- 제품번호: FD48282003 외 1건 (5.000톤)" in t
    assert "- 배차상태: 편성 완료" in t
    assert "- 출하예정: 2026-11-25" in t
    assert "- 도착지: 명강스틸 2공장" in t
    assert REFUND_NOTICE not in t and m["refund_notice"] is False


def test_unassigned_rule_reason_shows_refund():  # T21
    for reason in ("OVER_MAX", "UNDER_MIN"):
        m = build_customer_mms("A", [row("FD1", status="미배차", reason=reason)])
        assert REFUND_NOTICE in m["text"] and m["refund_notice"]
        assert "사유:" in m["text"]


def test_data_error_has_no_refund():  # T8 의 MMS 부분
    m = build_customer_mms("A", [row("FD1", status="데이터오류", reason="DATA_ERROR")])
    assert REFUND_NOTICE not in m["text"] and not m["refund_notice"]


def test_mixed_status_counts():
    m = build_customer_mms("A", [row("FD1"), row("FD2", status="미배차", reason="UNDER_MIN"), row("FD3")])
    assert "편성 완료 2건 / 미배차 1건" in m["text"]


def test_shipped_template():
    m = build_customer_mms("A", [row("FD1", status="출하완료", shipped_at="2026-11-24 10:00:00")])
    assert m["text"].startswith("[출하 안내]")
    assert "출하완료 (2026-11-24)" in m["text"]


def test_T14_long_message_truncated():
    rows = [row(f"FD{i:08d}", order=f"D26112{i:04d}", dlv="아주 긴 인도처 이름을 가진 공장 제2공장 하역장") for i in range(80)]
    m = build_customer_mms("A", rows)
    assert m["bytes"] <= MMS_MAX_BYTES
    assert m["truncated"] and "외 주문" in m["text"]
    assert "감사합니다." in m["text"]


def test_grouped_per_customer():
    msgs = build_mms([row("FD1", customer="A"), row("FD2", customer="B"), row("FD3", customer="A")])
    assert [m["customer"] for m in msgs] == ["A", "B"]
    assert msgs[0]["product_count"] == 2
