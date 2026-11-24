"""MMS 문구 생성 (PRD F7).

- 고객사별로 문구 1개 (MMS는 고객 한 곳에 보내므로)
- 주문별 블록: 주문번호 / 제품번호(외 N건, 톤) / 배차상태 / 출하예정 / 도착지
- 최대 2,000바이트 (EUC-KR 기준). 넘으면 뒤쪽 주문을 "외 주문 N건"으로 요약
- 배차 기준 때문에 선적 불가(OVER_MAX·UNDER_MIN)인 미배차가 있으면 항상 환불 안내 문구. DATA_ERROR 에는 넣지 않음
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict

MMS_MAX_BYTES = 2000
REFUND_NOTICE = "※ 주문 취소 시 환불까지 2주 정도 소요될 수 있습니다."
REFUND_REASONS = {"OVER_MAX", "UNDER_MIN"}

STATUS_LABEL = {
    "편성": "편성 완료",
    "미배차": "미배차",
    "출하완료": "출하완료",
    "입금대기": "입금 확인 대기",
    "낙찰취소": "낙찰 취소",
    "데이터오류": "확인 중",
}
STATUS_ORDER = ["출하완료", "편성", "미배차", "입금대기", "데이터오류", "낙찰취소"]


def mms_bytes(text: str) -> int:
    """통신사 기준 바이트 수 (EUC-KR, 인코딩 불가 문자는 2바이트로 계산)."""
    n = 0
    for ch in text:
        try:
            n += len(ch.encode("euc-kr"))
        except UnicodeEncodeError:
            n += 2
    return n


def _date(d: str | None) -> str | None:
    return d[:10] if d else None


def needs_refund_notice(rows: list[dict]) -> bool:
    return any(r["status"] == "미배차" and r.get("unassigned_reason") in REFUND_REASONS for r in rows)


def _status_line(rows: list[dict]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r["status"]] += 1
    if len(counts) == 1:
        st = next(iter(counts))
        if st == "출하완료":
            dates = sorted({_date(r.get("shipped_at")) for r in rows if r.get("shipped_at")})
            return f"출하완료 ({dates[-1]})" if dates else "출하완료"
        if st == "미배차" and not any(r.get("unassigned_reason") for r in rows):
            return "배차 대기"
        return STATUS_LABEL.get(st, st)
    return " / ".join(f"{STATUS_LABEL.get(s, s)} {counts[s]}건" for s in STATUS_ORDER if counts.get(s))


def _order_block(order_label: str, rows: list[dict]) -> list[str]:
    rows = sorted(rows, key=lambda r: r["product_no"])
    ton = sum(r.get("weight_ton") or 0 for r in rows)
    pno = rows[0]["product_no"] if len(rows) == 1 else f"{rows[0]['product_no']} 외 {len(rows) - 1}건 ({ton:.3f}톤)"
    lines = [f"- 주문번호: {order_label}", f"- 제품번호: {pno}", f"- 배차상태: {_status_line(rows)}"]
    if any(r["status"] == "미배차" and r.get("unassigned_reason") in REFUND_REASONS for r in rows):
        lines.append("- 사유: 차량 적재 기준(중량/폭/지역) 미충족으로 현재 선적이 어렵습니다.")
    plan = sorted({r["ship_plan_date"] for r in rows if r.get("ship_plan_date") and r["status"] in ("편성", "미배차")})
    if plan:
        lines.append(f"- 출하예정: {plan[0]}")
    dest = rows[0].get("dlv_name")
    if dest:
        addr = rows[0].get("address")
        lines.append(f"- 도착지: {dest}" + (f" ({addr})" if addr else ""))
    return lines


def build_customer_mms(customer: str, rows: list[dict], max_bytes: int = MMS_MAX_BYTES) -> dict:
    """한 고객사에 대한 MMS 문구. rows 는 search.row_summary 형식."""
    by_order: "OrderedDict[str, list[dict]]" = OrderedDict()
    for r in sorted(rows, key=lambda r: (r.get("order_no") or "~", r["product_no"])):
        label = r.get("order_no") or ("확인 중 (" + ", ".join(r["order_candidates"]) + ")" if r.get("order_candidates") else "확인 중")
        by_order.setdefault(label, []).append(r)

    all_shipped = all(r["status"] == "출하완료" for r in rows)
    refund = needs_refund_notice(rows)
    title = "[출하 안내]" if all_shipped else "[배차 안내]"
    head = [title, f"안녕하세요, {customer} 고객님."]
    if not all_shipped:
        head.append("주문하신 제품의 배차 현황을 안내드립니다.")
    tail = []
    if refund:
        tail += ["", REFUND_NOTICE]
    tail += ["", "문의사항은 담당자에게 연락 부탁드립니다.", "감사합니다."]

    blocks = [_order_block(label, rs) for label, rs in by_order.items()]
    orders = list(by_order.values())

    def render(n_blocks: int) -> str:
        body: list[str] = []
        for b in blocks[:n_blocks]:
            body += [""] + b
        rest = orders[n_blocks:]
        if rest:
            cnt = sum(len(o) for o in rest)
            ton = sum(r.get("weight_ton") or 0 for o in rest for r in o)
            body += ["", f"외 주문 {len(rest)}건 (제품 {cnt}건, {ton:.3f}톤)"]
        return "\n".join(head + body + tail)

    n = len(blocks)
    text = render(n)
    while mms_bytes(text) > max_bytes and n > 1:
        n -= 1
        text = render(n)
    truncated = n < len(blocks)
    if mms_bytes(text) > max_bytes:  # 주문 1개로도 넘치면 (도착지 주소가 아주 긴 경우 등) 끝을 자른다
        while mms_bytes(text) > max_bytes - 3:
            text = text[:-1]
        text += "..."
        truncated = True
    return {"customer": customer, "text": text, "bytes": mms_bytes(text), "max_bytes": max_bytes,
            "refund_notice": refund, "truncated": truncated, "product_count": len(rows)}


def build_mms(rows: list[dict]) -> list[dict]:
    """검색 결과 전체 → 고객사별 MMS 목록."""
    by_cust: "OrderedDict[str, list[dict]]" = OrderedDict()
    for r in rows:
        by_cust.setdefault(r.get("customer") or "고객", []).append(r)
    return [build_customer_mms(c, rs) for c, rs in by_cust.items()]
