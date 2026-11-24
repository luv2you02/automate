"""하이브리드 검색 (PRD F6).

1. 정규식 정확 일치: 제품번호 [A-Z]{2}\\d{8}, 주문번호 D\\d{9}, 경매번호 \\d{10}
2. 이름 부분 일치: 고객사명·인도처명·주소·지역 ((주), 주식회사, 공백, 괄호 무시)
3. FAISS 벡터 검색 (위에서 결과가 없을 때만)
결과는 항상 SQLite 최신 행으로 구성한다.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from ..embeddings import Embedder
from ..vector_store import VectorStore

PRODUCT_NO_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2}\d{8}(?!\d)")
ORDER_NO_RE = re.compile(r"(?<![A-Za-z0-9])[Dd]\d{9}(?!\d)")
AUCTION_NO_RE = re.compile(r"(?<![A-Za-z0-9])\d{10}(?!\d)")

VECTOR_MIN_SCORE = 0.35
MAX_ROWS = 500

# 질문에서 이름 후보를 뽑을 때 떼어낼 말
_STOPWORDS = {
    "배차", "배차됐어", "됐어", "되었나요", "어떻게", "됐나요", "상태", "알려줘", "알려주세요", "조회", "확인",
    "제품", "주문", "경매", "건", "거", "것", "오늘", "내일", "가는", "나가는", "출하", "현황", "어디", "언제",
    "있어", "있나요", "뭐야", "좀", "해줘", "주세요",
}
_SUFFIXES = ("으로", "로", "에서", "에게", "는", "은", "이", "가", "을", "를", "의", "에", "건", "거")


def normalize_name(name: str | None) -> str:
    """비교용 이름 정규화: (주)/㈜/주식회사, 괄호, 공백, 대소문자 무시."""
    if not name:
        return ""
    s = name.replace("（", "(").replace("）", ")").replace("㈜", "")
    s = re.sub(r"\(주\)|주식회사|\(유\)|유한회사", "", s)
    s = re.sub(r"[\s()\[\]·.,\-_/]", "", s)
    return s.lower()


@dataclass
class SearchResult:
    match_type: str                  # exact | name | vector | none
    rows: list[dict] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)  # 무엇으로 찾았는지 (표시용)


def _tokens(question: str) -> list[str]:
    out = []
    for t in re.split(r"[\s,?!.]+", question):
        t = t.strip()
        for suf in _SUFFIXES:
            if len(t) > len(suf) + 1 and t.endswith(suf):
                t = t[: -len(suf)]
                break
        if len(t) >= 2 and t not in _STOPWORDS:
            out.append(t)
    return out


BASE_SELECT = """
SELECT p.*, d.address AS dlv_address, o.ship_plan_date AS order_ship_plan_date
FROM products p
LEFT JOIN delivery_places d ON d.dlv_code = p.dlv_code
LEFT JOIN orders o ON o.order_key = p.order_key
"""


def _fetch(conn: sqlite3.Connection, where: str, args: list) -> list[dict]:
    sql = f"{BASE_SELECT} WHERE {where} ORDER BY p.customer, p.order_no, p.product_no LIMIT {MAX_ROWS}"
    return [dict(r) for r in conn.execute(sql, args)]


def search(conn: sqlite3.Connection, vs: VectorStore | None, embedder: Embedder | None, question: str,
           k: int = 10) -> SearchResult:
    q = (question or "").strip()
    if not q:
        return SearchResult("none")

    # 1) 정확 일치
    pnos = sorted({m.upper() for m in PRODUCT_NO_RE.findall(q)})
    onos = sorted({m.upper() for m in ORDER_NO_RE.findall(q)})
    anos = sorted(set(AUCTION_NO_RE.findall(q)))
    if pnos or onos or anos:
        conds, args = [], []
        if pnos:
            conds.append(f"p.product_no IN ({','.join('?' * len(pnos))})")
            args += pnos
        for o in onos:
            conds.append("(p.order_no = ? OR p.order_candidates LIKE ?)")
            args += [o, f'%"{o}"%']
        if anos:
            conds.append(f"p.auction_no IN ({','.join('?' * len(anos))})")
            args += anos
        return SearchResult("exact", _fetch(conn, " OR ".join(conds), args), pnos + onos + anos)

    # 2) ~ 4) 이름·지역 (R-05: 이름 '전체' 일치 → 지역 → 이름 '일부' 일치 순)
    qn = normalize_name(q)
    raw_toks = _tokens(q)
    toks = [t for t in (normalize_name(t) for t in raw_toks) if len(t) >= 2]
    customers = conn.execute("SELECT DISTINCT customer_code, customer FROM products WHERE customer IS NOT NULL").fetchall()
    places = conn.execute(
        "SELECT DISTINCT p.dlv_code, p.dlv_name FROM products p WHERE p.dlv_name IS NOT NULL").fetchall()

    def full(name: str | None) -> int:
        """질문에 이름 전체가 들어 있으면 이름 길이."""
        n = normalize_name(name)
        return len(n) if len(n) >= 2 and n in qn else 0

    def partial(name: str | None) -> int:
        """질문의 토큰이 이름 안에 들어 있으면 토큰 길이."""
        n = normalize_name(name)
        return max((len(t) for t in toks if t in n), default=0) if n else 0

    def by_name(score) -> SearchResult | None:
        cust_hits = [(score(c["customer"]), c) for c in customers]
        place_hits = [(score(d["dlv_name"]), d) for d in places]
        best_c = max((s for s, _ in cust_hits), default=0)
        best_p = max((s for s, _ in place_hits), default=0)
        if best_p and best_p > best_c:
            codes = [d["dlv_code"] for s, d in place_hits if s == best_p]
            names = [d["dlv_name"] for s, d in place_hits if s == best_p]
            return SearchResult("name", _fetch(conn, f"p.dlv_code IN ({','.join('?' * len(codes))})", codes), names)
        if best_c:
            codes = [c["customer_code"] for s, c in cust_hits if s == best_c]
            names = [c["customer"] for s, c in cust_hits if s == best_c]
            return SearchResult("name", _fetch(conn, f"p.customer_code IN ({','.join('?' * len(codes))})", codes), names)
        return None

    found = by_name(full)                       # 2) "명강스틸 2공장으로 가는 거", "신우"
    if found:
        return found
    found = _region_search(conn, raw_toks)      # 3) "울산 가는 제품", "경남 김해", "창원시", "M1EN64"
    if found:
        return found
    found = by_name(partial)                    # 4) "명강" → (주)명강스틸
    if found:
        return found

    # 5) 주소 일부 (예: 도로명)
    for t in raw_toks:
        rows = _fetch(conn, "(d.address LIKE ? OR p.region LIKE ?)", [f"%{t}%", f"%{t}%"])
        if rows:
            return SearchResult("name", rows, [t])

    # 3) 벡터 검색
    if vs is not None and embedder is not None and vs.size:
        try:
            vec = embedder.embed([q])[0]
        except Exception:
            return SearchResult("none")
        hits = vs.search(vec, k=k, min_score=VECTOR_MIN_SCORE)
        if hits:
            ids = [i for i, _ in hits]
            rows = _fetch(conn, f"p.id IN ({','.join('?' * len(ids))})", ids)
            order = {i: n for n, i in enumerate(ids)}
            rows.sort(key=lambda r: order.get(r["id"], 1e9))
            return SearchResult("vector", rows, [])

    return SearchResult("none")


def _region_search(conn: sqlite3.Connection, tokens: list[str]) -> SearchResult | None:
    """질문에 지역을 뜻하는 말이 있으면 그 지역 제품. 여러 개면 모두 만족 (예: '경남 김해')."""
    from .regions import _SIDO

    sido_of = {v: k for k, vs in _SIDO.items() for v in vs}
    regions = [r[0] for r in conn.execute("SELECT DISTINCT region FROM products WHERE region IS NOT NULL")]
    city_stems = {}
    for reg in regions:
        last = reg.split()[-1]
        if len(last) >= 3 and last[-1] in "시군":
            city_stems[last[:-1]] = last           # '김해' → '김해시'
    route_codes = {reg.upper() for reg in regions if re.fullmatch(r"[A-Za-z0-9]{4,}", reg)}

    conds, args, labels = [], [], []
    for t in tokens:
        if t in sido_of:
            conds.append("(p.region LIKE ? OR d.address LIKE ?)")
            args += [f"{sido_of[t]}%", f"{t}%"]
        elif len(t) >= 2 and t[-1] in "시군구":
            conds.append("(p.region LIKE ? OR d.address LIKE ?)")
            args += [f"%{t}%", f"%{t}%"]
        elif t in city_stems:
            conds.append("(p.region LIKE ? OR d.address LIKE ?)")
            args += [f"%{city_stems[t]}%", f"%{city_stems[t]}%"]
        elif t.upper() in route_codes:
            conds.append("(p.region = ?)")
            args.append(t.upper())
        else:
            continue
        labels.append(t)
    if not conds:
        return None
    rows = _fetch(conn, " AND ".join(conds), args)
    return SearchResult("region", rows, labels) if rows else None


def row_summary(r: dict) -> dict:
    """화면 표용 요약."""
    cands = json.loads(r["order_candidates"]) if r.get("order_candidates") else []
    return {
        "product_no": r["product_no"],
        "order_no": r.get("order_no"),
        "order_candidates": cands,
        "auction_no": r.get("auction_no"),
        "customer": r.get("customer"),
        "dlv_name": r.get("dlv_name"),
        "address": r.get("dlv_address"),
        "region": r.get("region"),
        "region_source": r.get("region_source"),
        "status": r["status"],
        "truck_id": r.get("truck_id"),
        "unassigned_reason": r.get("unassigned_reason"),
        "refund_notice": bool(r.get("refund_notice")),
        "carryover_days": r.get("carryover_days", 0),
        "category": r.get("category"),
        "is_long": bool(r.get("is_long")),
        "width_mm": r.get("width_mm"),
        "thickness_mm": r.get("thickness_mm"),
        "length_mm": r.get("length_mm"),
        "length_source": r.get("length_source"),
        "weight_ton": round((r.get("weight_kg") or 0) / 1000, 3),
        "ship_plan_date": r.get("ship_plan_date") or r.get("order_ship_plan_date"),
        "shipped_at": r.get("shipped_at"),
        "needs_review": bool(r.get("needs_review")),
        "review_note": r.get("review_note"),
    }
