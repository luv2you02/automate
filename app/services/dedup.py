"""중복 제거·누적·상태 전이 (PRD F2, F4).

흐름: 경매 시트 반영 → 제품 신규/중복/갱신 판별 → 영향받은 (경매번호, 고객) 그룹마다
      주문 매칭 + 지역·분류 계산 + 상태 전이 → 업로드 이력 기록.
SQLite가 상태의 기준이다. 임베딩은 content_hash 가 바뀐 제품만 다시 한다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from ..config import DispatchRules
from ..embeddings import Embedder
from ..vector_store import VectorStore
from .dispatch import classify, load_truck, refresh_truck, validate_truck
from .parser import ParseResult, match_products_to_orders
from .regions import resolve_region

log = logging.getLogger(__name__)

CONTENT_FIELDS = (
    "product_no", "auction_no", "auction_date", "customer_code", "customer", "dlv_code", "dlv_name",
    "thickness_mm", "width_mm", "length_mm", "length_source", "weight_kg", "spec", "error",
)


@dataclass
class UploadSummary:
    file_name: str
    duplicate_file: bool = False       # 같은 해시의 파일을 이미 올린 적 있음
    sheets: list[dict] = field(default_factory=list)
    new_cnt: int = 0
    dup_cnt: int = 0
    updated_cnt: int = 0
    error_cnt: int = 0
    orders_cnt: int = 0
    status_changes: dict[str, int] = field(default_factory=dict)  # 예: {"입금대기→미배차": 3}
    review_needed: list[dict] = field(default_factory=list)        # "확인 필요" 제품/차량
    errors: list[dict] = field(default_factory=list)
    order_match_pending: list[dict] = field(default_factory=list)  # 주문 매칭 확인 필요
    stale_groups: list[dict] = field(default_factory=list)         # 예전 데이터로 보여 주문 상태를 바꾸지 않은 그룹 (R-10)
    embedded: int = 0
    embedding_error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def content_hash(p: dict) -> str:
    payload = json.dumps([p.get(f) for f in CONTENT_FIELDS], ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _log(conn, pid: int, frm: str | None, to: str, reason: str, source: str | None) -> None:
    conn.execute(
        "INSERT INTO status_history(product_id, from_status, to_status, reason, source_file) VALUES (?,?,?,?,?)",
        (pid, frm, to, reason, source),
    )


# ---------------------------------------------------------------- 업로드 반영
def apply_upload(conn: sqlite3.Connection, parsed: ParseResult, rules: DispatchRules) -> UploadSummary:
    s = UploadSummary(file_name=parsed.file_name)
    s.sheets = [{"name": sh.name, "kind": sh.kind, "rows": sh.rows} for sh in parsed.sheets]
    s.duplicate_file = conn.execute(
        "SELECT 1 FROM uploads WHERE file_hash=? LIMIT 1", (parsed.file_hash,)
    ).fetchone() is not None
    s.errors = parsed.errors[:500]
    s.error_cnt = len(parsed.errors)

    touched_groups: set[tuple[str, str]] = set()
    stale_groups: set[tuple[str, str]] = set()

    # 1) 경매 시트: 파일에 들어 있는 (경매번호, 고객) 그룹은 파일 내용으로 통째로 교체
    #    (낙찰 → 낙찰확정이 되면 주문번호가 생겨 키가 바뀌므로, 행 단위 upsert 로는 옛 행이 남는다)
    by_group: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for o in parsed.orders:
        by_group[(o["auction_no"], o["customer_code"])].append(o)
    for (auc, cust), rows in by_group.items():
        old = conn.execute(
            "SELECT order_key, content_hash, status, qty FROM orders WHERE auction_no=? AND customer_code=?", (auc, cust)
        ).fetchall()
        # R-10: 예전 파일이면 주문 상태를 되돌리지 않는다
        if old and _is_regression([dict(r) for r in old], rows):
            stale_groups.add((auc, cust))
            s.stale_groups.append({"auction_no": auc, "customer_code": cust, "customer": rows[0].get("customer"),
                                   "db": _progress_label([dict(r) for r in old]), "file": _progress_label(rows)})
            continue
        new_hashes = {}
        for o in rows:
            new_hashes[o["order_key"]] = hashlib.sha256(
                json.dumps(o, ensure_ascii=False, sort_keys=True, default=str).encode()
            ).hexdigest()
        if {r["order_key"]: r["content_hash"] for r in old} != new_hashes:
            touched_groups.add((auc, cust))
        conn.execute("DELETE FROM orders WHERE auction_no=? AND customer_code=?", (auc, cust))
        for o in rows:
            conn.execute(
                "INSERT OR REPLACE INTO orders(order_key, auction_no, auction_date, customer_code, customer, status,"
                " order_no, qty, weight_kg, route, dlv_code, dlv_name, ship_plan_date, content_hash)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (o["order_key"], auc, o["auction_date"], cust, o["customer"], o["status"], o["order_no"], o["qty"],
                 o["weight_kg"], o["route"], o["dlv_code"], o["dlv_name"], o["ship_plan_date"],
                 new_hashes[o["order_key"]]),
            )
            if o["dlv_code"]:
                _upsert_place(conn, o["dlv_code"], o["dlv_name"], route=o["route"])
    s.orders_cnt = len(parsed.orders)

    # 2) 제품 시트
    shipped_now: set[str] = set()
    for p in parsed.products:
        h = content_hash(p)
        if p.get("shipped"):
            shipped_now.add(p["product_no"])
        if p["dlv_code"]:
            _upsert_place(conn, p["dlv_code"], p["dlv_name"])
        row = conn.execute("SELECT id, content_hash, status FROM products WHERE product_no=?", (p["product_no"],)).fetchone()
        extra = json.dumps(p["extra"], ensure_ascii=False) if p["extra"] else None
        values = (p["auction_no"], p["auction_date"], p["customer_code"], p["customer"], p["dlv_code"], p["dlv_name"],
                  p["thickness_mm"], p["width_mm"], p["length_mm"], p["length_source"], p["weight_kg"], p["spec"],
                  extra, h)
        if row is None:
            conn.execute(
                "INSERT INTO products(auction_no, auction_date, customer_code, customer, dlv_code, dlv_name,"
                " thickness_mm, width_mm, length_mm, length_source, weight_kg, spec, extra, content_hash,"
                " product_no, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, '입금대기')",
                values + (p["product_no"],),
            )
            s.new_cnt += 1
        elif row["content_hash"] == h:
            s.dup_cnt += 1
            if p["product_no"] not in shipped_now or row["status"] == "출하완료":
                continue  # 완전 중복: 아무것도 바꾸지 않음
        else:
            if row["status"] == "출하완료":
                s.dup_cnt += 1
                continue
            conn.execute(
                "UPDATE products SET auction_no=?, auction_date=?, customer_code=?, customer=?, dlv_code=?, dlv_name=?,"
                " thickness_mm=?, width_mm=?, length_mm=?, length_source=?, weight_kg=?, spec=?, extra=?, content_hash=?,"
                " updated_at=datetime('now','localtime') WHERE product_no=?",
                values + (p["product_no"],),
            )
            s.updated_cnt += 1
        if p["auction_no"] and p["customer_code"]:
            touched_groups.add((p["auction_no"], p["customer_code"]))
        else:
            _resolve_orphan(conn, p, rules, parsed.file_name, s)

    # 3) 영향받은 그룹마다 주문 매칭 + 상태 전이
    errors_by_pno = {p["product_no"]: p["error"] for p in parsed.products if p.get("error")}
    # 이번 파일 제품 시트에 그룹이 있는데 DB의 그 그룹 제품 중 빠진 것 (취소 판단 보조 근거, R-01)
    file_pnos: dict[tuple[str, str], set[str]] = defaultdict(set)
    for p in parsed.products:
        if p["auction_no"] and p["customer_code"]:
            file_pnos[(p["auction_no"], p["customer_code"])].add(p["product_no"])
    absent_by_group: dict[tuple[str, str], set[str]] = {}
    for g, pnos in file_pnos.items():
        if g in stale_groups:
            continue  # 예전 파일에는 나중에 추가된 제품이 없을 수 있으므로 '빠진 제품' 근거로 쓰지 않는다
        in_db = {r[0] for r in conn.execute(
            "SELECT product_no FROM products WHERE auction_no=? AND customer_code=?", g)}
        if in_db - pnos:
            absent_by_group[g] = in_db - pnos
            touched_groups.add(g)  # 남은 제품이 완전 중복이어도, 빠진 제품이 있으면 다시 판단해야 한다
    for auc, cust in sorted(touched_groups):
        resolve_group(conn, auc, cust, rules, s, parsed.file_name, shipped_now, errors_by_pno,
                      absent_by_group.get((auc, cust), set()))

    conn.execute(
        "INSERT INTO uploads(file_name, file_hash, new_cnt, dup_cnt, updated_cnt, error_cnt) VALUES (?,?,?,?,?,?)",
        (parsed.file_name, parsed.file_hash, s.new_cnt, s.dup_cnt, s.updated_cnt, s.error_cnt),
    )
    conn.commit()
    return s


def _progress(rows: list[dict]) -> tuple[int, int]:
    """(취소 수량, 확정 수량). 주문 상태는 낙찰 → 낙찰확정 → (어느 단계든) 낙찰취소 로만 진행한다."""
    cancelled = sum(int(r.get("qty") or 0) for r in rows if r["status"] == "낙찰취소")
    confirmed = sum(int(r.get("qty") or 0) for r in rows if r["status"] == "낙찰확정")
    return cancelled, confirmed


def _is_regression(db_rows: list[dict], file_rows: list[dict]) -> bool:
    """파일 쪽이 DB보다 '뒤 단계'면 예전 파일로 본다: 취소가 줄었거나, 취소는 같은데 확정이 줄었을 때."""
    (db_c, db_f), (f_c, f_f) = _progress(db_rows), _progress(file_rows)
    return f_c < db_c or (f_c == db_c and f_f < db_f)


def _progress_label(rows: list[dict]) -> str:
    c, f = _progress(rows)
    w = sum(int(r.get("qty") or 0) for r in rows if r["status"] == "낙찰")
    return f"확정 {f} · 낙찰 {w} · 취소 {c}"


def _upsert_place(conn, code: str, name: str | None, route: str | None = None) -> None:
    conn.execute(
        "INSERT INTO delivery_places(dlv_code, dlv_name, route) VALUES (?,?,?)"
        " ON CONFLICT(dlv_code) DO UPDATE SET dlv_name=COALESCE(excluded.dlv_name, dlv_name),"
        " route=COALESCE(excluded.route, route), updated_at=datetime('now','localtime')",
        (code, name, route),
    )


def _resolve_orphan(conn, p: dict, rules: DispatchRules, source: str, s: UploadSummary) -> None:
    """경매번호/업체코드가 없는 제품 → 데이터오류."""
    row = conn.execute("SELECT id, status FROM products WHERE product_no=?", (p["product_no"],)).fetchone()
    if row["status"] != "데이터오류":
        conn.execute(
            "UPDATE products SET status='데이터오류', unassigned_reason='DATA_ERROR', refund_notice=0, review_note=?"
            " WHERE id=?", (p.get("error"), row["id"]),
        )
        _log(conn, row["id"], row["status"], "데이터오류", p.get("error") or "데이터 오류", source)


def resolve_group(conn: sqlite3.Connection, auction_no: str, customer_code: str, rules: DispatchRules,
                  s: UploadSummary | None = None, source: str | None = None,
                  shipped_now: set[str] | None = None, errors_by_pno: dict[str, str] | None = None,
                  absent: set[str] | None = None) -> None:
    """(경매번호, 고객) 그룹의 제품들: 주문 매칭, 지역·분류 계산, 상태 전이."""
    shipped_now = shipped_now or set()
    errors_by_pno = errors_by_pno or {}
    products = [dict(r) for r in conn.execute(
        "SELECT * FROM products WHERE auction_no=? AND customer_code=?", (auction_no, customer_code))]
    if not products:
        return
    orders = [dict(r) for r in conn.execute(
        "SELECT * FROM orders WHERE auction_no=? AND customer_code=?", (auction_no, customer_code))]
    matches = match_products_to_orders(products, orders, absent or set())
    by_key = {o["order_key"]: o for o in orders}
    live = [o for o in orders if o["status"] != "낙찰취소"]

    for p in products:
        m = matches[p["product_no"]]
        o = by_key.get(m.order_key) if m.order_key else (live[0] if live else None)
        ship_plan = o["ship_plan_date"] if o else None
        place = conn.execute("SELECT address, route FROM delivery_places WHERE dlv_code=?", (p["dlv_code"],)).fetchone()
        route = (o or {}).get("route") or (place["route"] if place else None)
        region, region_source = resolve_region(place["address"] if place else None, route)

        # 오류 판단은 DB 값 기준 (이번 파일에 이 제품이 없어도 같은 결과가 나오도록)
        error = errors_by_pno.get(p["product_no"])
        category = is_long = None
        if p["width_mm"] and p["weight_kg"] and p["thickness_mm"]:
            category, is_long = classify(p["width_mm"], p["length_mm"], rules)
        elif not error:
            error = "폭·두께·중량 값 오류"
        if not error and not p["dlv_code"]:
            error = "인도처코드 없음"
        if not error and region is None:
            error = "지역 판별 불가 (인도처 주소·운송경로 없음)"

        new_status = _next_status(p["status"], p["product_no"] in shipped_now, error, m.order_status)
        conn.execute(
            "UPDATE products SET order_no=?, order_key=?, order_status=?, order_candidates=?, ship_plan_date=?,"
            " region=?, region_source=?, category=?, is_long=? WHERE id=?",
            (m.order_no, m.order_key, m.order_status, json.dumps(m.candidates, ensure_ascii=False) if m.candidates else None,
             ship_plan, region, region_source, category, int(bool(is_long)), p["id"]),
        )
        if s is not None and m.candidates:
            s.order_match_pending.append({"product_no": p["product_no"], "candidates": m.candidates})

        _apply_status(conn, p, new_status, error, source, s, rules)

        # 취소된 주문이 섞였는데 어느 제품이 취소분인지 못 정함 → 배차 보류 + 확인 필요 (R-01)
        hold_note = "주문 취소 여부 확인 필요 — 같은 경매의 주문 중 일부가 취소되었는데 어느 제품인지 판단할 수 없습니다"
        if new_status in ("미배차", "입금대기"):
            if m.cancel_uncertain:
                conn.execute("UPDATE products SET needs_review=1, review_note=? WHERE id=?", (hold_note, p["id"]))
                if s is not None:
                    s.review_needed.append({"truck_id": None, "product_no": p["product_no"], "note": hold_note})
            elif p["needs_review"]:
                conn.execute("UPDATE products SET needs_review=0, review_note=NULL WHERE id=?", (p["id"],))
        elif new_status == "편성" and m.cancel_uncertain and p["truck_id"]:
            _flag_review(conn, p["truck_id"], p, hold_note, s)

        # 편성 차량에 속한 제품의 값이 바뀌어 규칙을 벗어나면 "확인 필요"
        if new_status == "편성" and p["truck_id"]:
            t = load_truck(conn, p["truck_id"])
            if t:
                refresh_truck(conn, p["truck_id"], rules)
                errs = validate_truck(t, rules)
                if errs:
                    _flag_review(conn, p["truck_id"], p, "배차 후 정보 변경: " + " / ".join(errs), s)


def _next_status(cur: str, shipped: bool, error: str | None, order_status: str | None) -> str:
    """PRD F2 표의 상태 전이."""
    if cur == "출하완료":
        return cur
    if shipped:
        return "출하완료"
    if order_status == "낙찰취소":
        return "낙찰취소"
    if cur == "편성":
        return cur                       # 편성 제품은 재배차하지 않는다
    if error:
        return "데이터오류"
    if order_status == "낙찰확정":
        return "미배차" if cur in ("입금대기", "데이터오류", "낙찰취소", "미배차") else cur
    return "입금대기"                    # 낙찰(미입금) 또는 주문 정보 없음


def _apply_status(conn, p: dict, new: str, error: str | None, source: str | None, s: UploadSummary | None,
                  rules: DispatchRules) -> None:
    cur = p["status"]
    pid = p["id"]
    # 아직 이력이 없으면 이번 업로드로 처음 들어온 제품 (임시 상태 '입금대기'에서 첫 상태로)
    is_new = not conn.execute("SELECT 1 FROM status_history WHERE product_id=? LIMIT 1", (pid,)).fetchone()

    if new == "데이터오류":
        conn.execute("UPDATE products SET unassigned_reason='DATA_ERROR', refund_notice=0, review_note=? WHERE id=?",
                     (error, pid))
    elif cur == "데이터오류" and new != cur:
        conn.execute("UPDATE products SET unassigned_reason=NULL, review_note=NULL WHERE id=?", (pid,))

    if new == cur and not is_new:
        return

    sets = "status=?, updated_at=datetime('now','localtime')"
    args: list[Any] = [new]
    if new == "출하완료":
        sets += ", shipped_at=datetime('now','localtime'), unassigned_reason=NULL, refund_notice=0"
    if new in ("입금대기", "미배차") and cur != "편성":
        sets += ", unassigned_reason=NULL, refund_notice=0"
    conn.execute(f"UPDATE products SET {sets} WHERE id=?", (*args, pid))
    _log(conn, pid, None if is_new else cur, new, "업로드 반영", source)
    if s is not None and not is_new:
        key = f"{cur}→{new}"
        s.status_changes[key] = s.status_changes.get(key, 0) + 1

    if p["truck_id"]:
        if new == "낙찰취소":
            # R-03: 취소된 제품은 차량에서 뺀다 (차량번호를 남기면 재확정 때 미배차인데 차량 카드에 남음).
            #       어느 제품이 빠졌는지는 차량의 review_note 와 status_history 에 남긴다.
            _flag_review(conn, p["truck_id"], p, f"편성 후 낙찰취소 ({p['product_no']} 제외됨)", s)
            conn.execute("UPDATE products SET truck_id=NULL WHERE id=?", (pid,))
            refresh_truck(conn, p["truck_id"], rules)
        elif new == "출하완료":
            remaining = conn.execute(
                "SELECT COUNT(*) FROM products WHERE truck_id=? AND status='편성' AND id<>?", (p["truck_id"], pid)
            ).fetchone()[0]
            if remaining == 0:
                conn.execute("UPDATE trucks SET shipped=1 WHERE truck_id=?", (p["truck_id"],))


def _flag_review(conn, truck_id: str, p: dict, note: str, s: UploadSummary | None) -> None:
    row = conn.execute("SELECT review_note FROM trucks WHERE truck_id=?", (truck_id,)).fetchone()
    notes = [n for n in ((row["review_note"] or "").split(" / ") if row else []) if n]
    if note not in notes:
        notes.append(note)
    conn.execute("UPDATE trucks SET needs_review=1, review_note=? WHERE truck_id=?", (" / ".join(notes[-10:]), truck_id))
    conn.execute("UPDATE products SET needs_review=1, review_note=? WHERE id=?", (note, p["id"]))
    if s is not None:
        s.review_needed.append({"truck_id": truck_id, "product_no": p["product_no"], "note": note})


# ---------------------------------------------------------------- 인도처 주소표
def apply_delivery_places(conn: sqlite3.Connection, rows: Iterable[dict], rules: DispatchRules) -> dict:
    """주소표 반영 후, 해당 인도처의 (편성 아닌) 제품 지역을 다시 계산."""
    from .regions import region_from_address

    updated, unresolved = 0, []
    codes = []
    for r in rows:
        region = region_from_address(r.get("address"))
        if r.get("address") and region is None:
            unresolved.append({"dlv_code": r["dlv_code"], "address": r["address"]})
        conn.execute(
            "INSERT INTO delivery_places(dlv_code, dlv_name, address, region) VALUES (?,?,?,?)"
            " ON CONFLICT(dlv_code) DO UPDATE SET dlv_name=COALESCE(excluded.dlv_name, dlv_name),"
            " address=excluded.address, region=excluded.region, updated_at=datetime('now','localtime')",
            (r["dlv_code"], r.get("dlv_name"), r.get("address"), region),
        )
        codes.append(r["dlv_code"])
        updated += 1
    groups = set()
    for code in codes:
        for g in conn.execute(
            "SELECT DISTINCT auction_no, customer_code FROM products WHERE dlv_code=? AND status<>'출하완료'", (code,)
        ):
            groups.add((g[0], g[1]))
    for auc, cust in groups:
        if auc and cust:
            resolve_group(conn, auc, cust, rules, source="인도처 주소표")
    conn.commit()
    return {"updated": updated, "unresolved_addresses": unresolved}


# ---------------------------------------------------------------- 임베딩
def embedding_text(p: dict) -> str:
    """임베딩용 텍스트. 상태·차량은 넣지 않는다 (상태가 바뀔 때마다 다시 임베딩하지 않기 위해,
    검색 결과의 상태는 항상 SQLite 최신값으로 보여준다)."""
    parts = [
        f"제품번호 {p['product_no']}",
        f"주문번호 {p['order_no']}" if p.get("order_no") else None,
        f"경매번호 {p['auction_no']}" if p.get("auction_no") else None,
        f"고객사 {p['customer']}" if p.get("customer") else None,
        f"인도처 {p['dlv_name']}" if p.get("dlv_name") else None,
        f"주소 {p['address']}" if p.get("address") else None,
        f"지역 {p['region']}" if p.get("region") else None,
        f"규격 {p['spec']}" if p.get("spec") else None,
        f"두께 {p['thickness_mm']:g}mm 폭 {p['width_mm']:g}mm" if p.get("thickness_mm") and p.get("width_mm") else None,
        f"중량 {p['weight_kg'] / 1000:.3f}톤" if p.get("weight_kg") else None,
        {"WIDE": "분류 광폭재", "NORMAL": "분류 일반재"}.get(p.get("category") or ""),
    ]
    return " | ".join(x for x in parts if x)


def _embed_hash(p: dict) -> str:
    return hashlib.sha256(embedding_text(p).encode("utf-8")).hexdigest()


def sync_embeddings(conn: sqlite3.Connection, vs: VectorStore, embedder: Embedder, batch: int = 256) -> dict:
    """임베딩 텍스트가 바뀐 제품만 임베딩해 FAISS에 upsert. 실패 건은 embedding_queue 로."""
    rows = [dict(r) for r in conn.execute(
        "SELECT p.*, d.address FROM products p LEFT JOIN delivery_places d ON d.dlv_code = p.dlv_code")]
    todo = [r for r in rows if r["embedded_hash"] != _embed_hash(r)]
    done = 0
    error = None
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        try:
            vecs = embedder.embed([embedding_text(r) for r in chunk])
        except Exception as e:
            error = str(e)
            log.warning("임베딩 실패: %s", type(e).__name__)
            for r in chunk:
                conn.execute(
                    "INSERT INTO embedding_queue(product_id, attempts, last_error) VALUES (?,1,?)"
                    " ON CONFLICT(product_id) DO UPDATE SET attempts=attempts+1, last_error=excluded.last_error",
                    (r["id"], error[:500]),
                )
            continue
        vs.upsert([r["id"] for r in chunk], vecs)
        for r in chunk:
            conn.execute("UPDATE products SET embedded_hash=? WHERE id=?", (_embed_hash(r), r["id"]))
            conn.execute("DELETE FROM embedding_queue WHERE product_id=?", (r["id"],))
        done += len(chunk)
    conn.commit()
    if done:
        vs.save()
    pending = conn.execute("SELECT COUNT(*) FROM embedding_queue").fetchone()[0]
    return {"embedded": done, "pending": pending, "error": error}
