"""배차 엔진 (PRD F3).

규칙 (수치는 모두 Settings.rules 에서 읽는다):
- R1 차량 중량: 광폭(폭 > 3400) 25~26t / 일반 25~27t / 일반+장척(길이 > 17000) 포함 25~26t
- R2 광폭재와 일반재 합짐 금지
- R3 같은 지역만 (region + region_source 가 모두 같아야 함)
- 같은 지역 안 우선순위: 같은 고객사(업체코드) → 같은 인도처(인도처코드) → 같은 지역
- 대상은 상태 '미배차'만. 편성 제품은 절대 건드리지 않는다.

알고리즘: (분류, 지역)별로 나눈 뒤, 고객사 → 인도처 → 지역 순으로 범위 제약 빈패킹.
같은 그룹을 우선순위 없이 한 번에 묶은 결과(flat)가 차량 수가 더 많거나 미배차 중량이 더 적으면 그쪽을 쓴다.
"""
from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as date_cls

from ..config import DispatchRules

EPS = 1e-6
TIME_LIMIT_SEC = 3.0


# ---------------------------------------------------------------- 모델
@dataclass
class Item:
    id: int
    product_no: str
    kg: float
    category: str          # 'WIDE' | 'NORMAL'
    is_long: bool
    region: str
    region_source: str
    customer_code: str
    dlv_code: str


@dataclass
class Truck:
    truck_id: str
    category: str
    region: str
    region_source: str
    items: list[Item] = field(default_factory=list)

    @property
    def total_kg(self) -> float:
        return sum(i.kg for i in self.items)

    @property
    def has_long(self) -> bool:
        return any(i.is_long for i in self.items)

    @property
    def product_ids(self) -> list[int]:
        return [i.id for i in self.items]


@dataclass
class DispatchResult:
    dispatch_date: str
    trucks: list[Truck]
    unassigned: dict[int, str]  # product_id → OVER_MAX | UNDER_MIN
    elapsed_sec: float

    def summary(self) -> dict:
        reasons: dict[str, int] = defaultdict(int)
        for r in self.unassigned.values():
            reasons[r] += 1
        return {
            "dispatch_date": self.dispatch_date,
            "truck_count": len(self.trucks),
            "wide_trucks": sum(t.category == "WIDE" for t in self.trucks),
            "normal_trucks": sum(t.category == "NORMAL" for t in self.trucks),
            "assigned_products": sum(len(t.items) for t in self.trucks),
            "assigned_ton": round(sum(t.total_kg for t in self.trucks) / 1000, 3),
            "unassigned_products": len(self.unassigned),
            "unassigned_by_reason": dict(reasons),
            "elapsed_sec": round(self.elapsed_sec, 3),
            "truck_ids": [t.truck_id for t in self.trucks],
        }


# ---------------------------------------------------------------- 규칙
def classify(width_mm: float, length_mm: float | None, rules: DispatchRules) -> tuple[str, bool]:
    """(category, is_long). 경계값: 폭 3400 = 일반, 길이 17000 = 장척 아님."""
    category = "WIDE" if width_mm > rules.wide_width_mm else "NORMAL"
    is_long = bool(length_mm and length_mm > rules.long_length_mm)
    return category, is_long


def max_ton_for(category: str, has_long: bool, rules: DispatchRules) -> float:
    if category == "WIDE":
        return rules.wide_max_ton
    return rules.long_max_ton if has_long else rules.normal_max_ton


def _cap_kg(category: str, has_long: bool, rules: DispatchRules) -> float:
    return max_ton_for(category, has_long, rules) * 1000


def validate_truck(truck: Truck, rules: DispatchRules) -> list[str]:
    """R1·R2·R3 위반 사유 목록 (비어 있으면 정상)."""
    errs = []
    if not truck.items:
        return ["빈 차량"]
    cats = {i.category for i in truck.items}
    if len(cats) > 1:
        errs.append("R2 위반: 광폭재와 일반재 혼적")
    elif truck.category not in cats:
        errs.append("차량 분류와 제품 분류 불일치")
    regions = {(i.region_source, i.region) for i in truck.items}
    if len(regions) > 1:
        errs.append("R3 위반: 다른 지역 혼적 (" + ", ".join(sorted(r for _, r in regions)) + ")")
    cap = _cap_kg(truck.category, truck.has_long, rules)
    total = truck.total_kg
    if total < rules.truck_min_ton * 1000 - EPS:
        errs.append(f"R1 위반: {total / 1000:.3f}톤 < 하한 {rules.truck_min_ton}톤")
    if total > cap + EPS:
        long_note = " (장척 포함)" if truck.has_long and truck.category == "NORMAL" else ""
        errs.append(f"R1 위반: {total / 1000:.3f}톤 > 상한 {cap / 1000:g}톤{long_note}")
    return errs


def item_cap_kg(item: Item, rules: DispatchRules) -> float:
    return _cap_kg(item.category, item.is_long, rules)


# ---------------------------------------------------------------- 패킹
class _Packer:
    def __init__(self, rules: DispatchRules, deadline: float):
        self.rules = rules
        self.min_kg = rules.truck_min_ton * 1000
        self.deadline = deadline

    def cap(self, category: str, items: list[Item]) -> float:
        return _cap_kg(category, any(i.is_long for i in items), self.rules)

    def fits(self, category: str, load: list[Item], total: float, item: Item) -> bool:
        new_cap = _cap_kg(category, item.is_long or any(i.is_long for i in load), self.rules)
        return total + item.kg <= new_cap + EPS

    def pack(self, items: list[Item]) -> tuple[list[list[Item]], list[Item]]:
        """범위 [min, cap] 를 만족하는 차량 묶음들과 남은 제품."""
        if not items:
            return [], []
        category = items[0].category
        pool = sorted(items, key=lambda i: -i.kg)
        loads: list[list[Item]] = []
        leftovers: list[Item] = []

        while pool:
            if sum(i.kg for i in pool) < self.min_kg - EPS:
                leftovers.extend(pool)
                break
            seed = pool.pop(0)
            load = self._fill(category, seed, pool) or self._search(category, seed, pool)
            if load is None:
                leftovers.append(seed)
                continue
            ids = {i.id for i in load}
            pool = [i for i in pool if i.id not in ids]
            loads.append(load)

        self._repair(category, loads, leftovers)
        return loads, leftovers

    def _fill(self, category: str, seed: Item, pool: list[Item]) -> list[Item] | None:
        """그리디: 하한까지 남은 양을 한 번에 채울 수 있으면 가장 작은 것, 아니면 가장 큰 것을 넣는다."""
        load, total = [seed], seed.kg
        used: set[int] = set()
        while total < self.min_kg - EPS:
            need = self.min_kg - total
            fitting = [i for i in pool if i.id not in used and self.fits(category, load, total, i)]
            if not fitting:
                return None
            closers = [i for i in fitting if i.kg >= need - EPS]
            pick = min(closers, key=lambda i: i.kg) if closers else max(fitting, key=lambda i: i.kg)
            load.append(pick)
            used.add(pick.id)
            total += pick.kg
        return load

    def _search(self, category: str, seed: Item, pool: list[Item]) -> list[Item] | None:
        """그리디 실패 시 백트래킹으로 seed 를 포함한 범위 내 조합 탐색 (시간 제한)."""
        cand = [i for i in pool]
        best: list[Item] | None = None

        def rec(start: int, load: list[Item], total: float) -> bool:
            nonlocal best
            if time.perf_counter() > self.deadline:
                return True
            if total >= self.min_kg - EPS:
                best = list(load)
                return True
            for j in range(start, len(cand)):
                it = cand[j]
                if self.fits(category, load, total, it):
                    load.append(it)
                    if rec(j + 1, load, total + it.kg):
                        return True
                    load.pop()
            return False

        rec(0, [seed], seed.kg)
        return best

    def _repair(self, category: str, loads: list[list[Item]], leftovers: list[Item]) -> None:
        """남은 제품 + 여유 있는 차량에서 빌려온 제품으로 차량을 하나 더 만들 수 있으면 만든다."""
        while leftovers and time.perf_counter() < self.deadline:
            left_kg = sum(i.kg for i in leftovers)
            deficit = self.min_kg - left_kg
            if left_kg <= 0:
                return
            borrowed: list[tuple[int, Item]] = []  # (load index, item)
            if deficit > EPS:
                # 각 차량에서 하한을 지키는 범위의 제품 하나씩 빌려온다 (부족분에 가까운 것부터)
                for li, load in sorted(enumerate(loads), key=lambda x: -sum(i.kg for i in x[1])):
                    slack = sum(i.kg for i in load) - self.min_kg
                    options = [i for i in load if i.kg <= slack + EPS]
                    if not options:
                        continue
                    pick = min(options, key=lambda i: abs(i.kg - deficit))
                    borrowed.append((li, pick))
                    deficit -= pick.kg
                    if deficit <= EPS:
                        break
                if deficit > EPS:
                    return
            pool = leftovers + [it for _, it in borrowed]
            sub = _Packer(self.rules, self.deadline)
            sub_loads: list[list[Item]] = []
            sub_left: list[Item] = []
            p = sorted(pool, key=lambda i: -i.kg)
            while p:
                seed = p.pop(0)
                load = sub._fill(category, seed, p)
                if load is None:
                    sub_left.append(seed)
                    continue
                ids = {i.id for i in load}
                p = [i for i in p if i.id not in ids]
                sub_loads.append(load)
            used_borrowed = {it.id for _, it in borrowed} - {i.id for i in sub_left}
            if not sub_loads or {i.id for i in sub_left} & {it.id for _, it in borrowed}:
                return  # 빌려온 제품을 다 쓰지 못하면 원상태 유지
            for li, it in borrowed:
                if it.id in used_borrowed:
                    loads[li] = [i for i in loads[li] if i.id != it.id]
            loads.extend(sub_loads)
            leftovers[:] = sub_left
            if not sub_left:
                return


def _score(loads: list[list[Item]], leftovers: list[Item]) -> tuple:
    """목표: 차량 수 최대 → 미배차 중량 최소 → 차량당 도착지 수 최소."""
    dests = sum(len({i.dlv_code for i in l}) for l in loads)
    return (len(loads), -sum(i.kg for i in leftovers), -dests)


def pack_group(items: list[Item], rules: DispatchRules, deadline: float) -> tuple[list[list[Item]], list[Item]]:
    """같은 (분류, 지역) 그룹: 고객사 → 인도처 → 지역 우선순위로 묶는다."""
    packer = _Packer(rules, deadline)

    # 우선순위 방식
    loads: list[list[Item]] = []
    remaining = list(items)
    for key in (lambda i: i.customer_code, lambda i: i.dlv_code):
        groups: dict[str, list[Item]] = defaultdict(list)
        for it in remaining:
            groups[key(it)].append(it)
        remaining = []
        for g in groups.values():
            l, left = packer.pack(g)
            loads += l
            remaining += left
    l, left = packer.pack(remaining)
    loads += l
    hierarchical = (loads, left)

    # 비교용: 우선순위 없이 한 번에
    flat = packer.pack(list(items))
    return flat if _score(*flat) > _score(*hierarchical) else hierarchical


def plan(items: list[Item], rules: DispatchRules, dispatch_date: str, existing_ids: set[str] | None = None) -> DispatchResult:
    """DB와 무관한 순수 배차 계산."""
    t0 = time.perf_counter()
    deadline = t0 + TIME_LIMIT_SEC
    unassigned: dict[int, str] = {}
    groups: dict[tuple, list[Item]] = defaultdict(list)

    for it in items:
        if it.kg > item_cap_kg(it, rules) + EPS:
            unassigned[it.id] = "OVER_MAX"
        else:
            groups[(it.category, it.region_source, it.region)].append(it)

    trucks: list[Truck] = []
    counters = {"WIDE": 0, "NORMAL": 0}
    taken = set(existing_ids or ())

    def next_id(category: str) -> str:
        prefix = "W" if category == "WIDE" else "N"
        while True:
            counters[category] += 1
            tid = f"{dispatch_date}-{prefix}{counters[category]:02d}"
            if tid not in taken:
                taken.add(tid)
                return tid

    for (category, source, region), g in sorted(groups.items(), key=lambda kv: (kv[0][0] != "WIDE", kv[0][2])):
        loads, left = pack_group(g, rules, deadline)
        for load in loads:
            trucks.append(Truck(next_id(category), category, region, source, sorted(load, key=lambda i: -i.kg)))
        for it in left:
            unassigned[it.id] = "UNDER_MIN"

    return DispatchResult(dispatch_date, trucks, unassigned, time.perf_counter() - t0)


# ---------------------------------------------------------------- DB 연동
def _item_from_row(r: sqlite3.Row) -> Item:
    return Item(
        id=r["id"], product_no=r["product_no"], kg=r["weight_kg"], category=r["category"],
        is_long=bool(r["is_long"]), region=r["region"], region_source=r["region_source"],
        customer_code=r["customer_code"] or "", dlv_code=r["dlv_code"] or "",
    )


def _log(conn: sqlite3.Connection, pid: int, frm: str | None, to: str, reason: str) -> None:
    conn.execute(
        "INSERT INTO status_history(product_id, from_status, to_status, reason) VALUES (?,?,?,?)",
        (pid, frm, to, reason),
    )


def run_dispatch(conn: sqlite3.Connection, rules: DispatchRules, dispatch_date: str | None = None) -> DispatchResult:
    """미배차 제품으로 배차하고 DB에 반영. 편성 제품은 건드리지 않는다."""
    dispatch_date = dispatch_date or date_cls.today().isoformat()
    rows = conn.execute(
        "SELECT * FROM products WHERE status='미배차' AND needs_review=0 AND region IS NOT NULL AND category IS NOT NULL"
    ).fetchall()  # needs_review=1 인 미배차 = 주문 취소 여부 확인 전 보류 (R-01)
    items = [_item_from_row(r) for r in rows]
    existing = {r[0] for r in conn.execute("SELECT truck_id FROM trucks WHERE dispatch_date=?", (dispatch_date,))}
    result = plan(items, rules, dispatch_date, existing)

    for t in result.trucks:
        conn.execute(
            "INSERT INTO trucks(truck_id, dispatch_date, category, region, region_source, has_long, total_kg, max_ton)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (t.truck_id, dispatch_date, t.category, t.region, t.region_source, int(t.has_long), t.total_kg,
             max_ton_for(t.category, t.has_long, rules)),
        )
        for it in t.items:
            conn.execute(
                "UPDATE products SET status='편성', truck_id=?, unassigned_reason=NULL, refund_notice=0,"
                " last_dispatch_date=?, updated_at=datetime('now','localtime') WHERE id=?",
                (t.truck_id, dispatch_date, it.id),
            )
            _log(conn, it.id, "미배차", "편성", f"자동배차 {t.truck_id}")

    for pid, reason in result.unassigned.items():
        # 이월 일수: 이전에 다른 날짜에 배차를 시도했는데 또 남으면 +1
        conn.execute(
            "UPDATE products SET unassigned_reason=?, refund_notice=1,"
            " carryover_days = carryover_days + CASE WHEN last_dispatch_date IS NOT NULL AND last_dispatch_date <> ? THEN 1 ELSE 0 END,"
            " last_dispatch_date=? WHERE id=?",
            (reason, dispatch_date, dispatch_date, pid),
        )
    conn.commit()
    return result


def load_truck(conn: sqlite3.Connection, truck_id: str) -> Truck | None:
    t = conn.execute("SELECT * FROM trucks WHERE truck_id=?", (truck_id,)).fetchone()
    if not t:
        return None
    rows = conn.execute("SELECT * FROM products WHERE truck_id=? AND status='편성'", (truck_id,)).fetchall()
    return Truck(t["truck_id"], t["category"], t["region"], t["region_source"], [_item_from_row(r) for r in rows])


def refresh_truck(conn: sqlite3.Connection, truck_id: str, rules: DispatchRules) -> None:
    """차량 합계 재계산. 편성·출하 제품이 하나도 없으면 삭제."""
    t = load_truck(conn, truck_id)
    if t is None:
        return
    if not t.items:
        shipped = conn.execute("SELECT COUNT(*) FROM products WHERE truck_id=? AND status='출하완료'",
                               (truck_id,)).fetchone()[0]
        if not shipped:
            conn.execute("DELETE FROM trucks WHERE truck_id=?", (truck_id,))
            return
    conn.execute(
        "UPDATE trucks SET total_kg=?, has_long=?, max_ton=? WHERE truck_id=?",
        (t.total_kg, int(t.has_long), max_ton_for(t.category, t.has_long, rules), truck_id),
    )


def clear_review_if_valid(conn: sqlite3.Connection, truck_id: str, rules: DispatchRules) -> bool:
    """수동 조정으로 차량이 다시 규칙을 지키면 '확인 필요' 표시를 지운다."""
    t = load_truck(conn, truck_id)
    if t is None or not t.items or validate_truck(t, rules):
        return False
    conn.execute("UPDATE trucks SET needs_review=0, review_note=NULL WHERE truck_id=?", (truck_id,))
    conn.execute("UPDATE products SET needs_review=0, review_note=NULL WHERE truck_id=? AND status='편성'", (truck_id,))
    return True


def _issues(truck: Truck, rules: DispatchRules) -> set[str]:
    """규칙 위반 종류: MIX(R2), REGION(R3), UNDER/OVER(R1)."""
    if not truck.items:
        return set()
    out = set()
    if len({i.category for i in truck.items}) > 1 or truck.category not in {i.category for i in truck.items}:
        out.add("MIX")
    if len({(i.region_source, i.region) for i in truck.items}) > 1:
        out.add("REGION")
    if truck.total_kg < rules.truck_min_ton * 1000 - EPS:
        out.add("UNDER")
    if truck.total_kg > _cap_kg(truck.category, truck.has_long, rules) + EPS:
        out.add("OVER")
    return out


class MoveError(ValueError):
    pass


def move_product(conn: sqlite3.Connection, rules: DispatchRules, product_no: str, to_truck_id: str | None) -> dict:
    """수동 이동 (R-02 반영).

    - 도착 차량: 이동 후 규칙을 지켜야 한다. 단, 이미 25톤 미만이던 차량은 혼적·지역·상한 위반 없이
      무게가 늘어나는 이동(채워 넣기)을 허용한다.
    - 출발 차량: 이동 후에도 규칙을 지켜야 한다. 단, 이미 규칙을 벗어났거나 '확인 필요'인 차량에서는
      빼는 이동을 허용한다 (고칠 수 있게).
    """
    p = conn.execute("SELECT * FROM products WHERE product_no=?", (product_no,)).fetchone()
    if not p:
        raise MoveError(f"제품 {product_no}을(를) 찾을 수 없습니다.")
    if p["status"] not in ("미배차", "편성"):
        raise MoveError(f"'{p['status']}' 상태의 제품은 이동할 수 없습니다.")
    if p["region"] is None or p["category"] is None:
        raise MoveError("지역 또는 분류가 정해지지 않은 제품입니다.")
    if p["status"] == "미배차" and p["needs_review"] and to_truck_id:
        raise MoveError(p["review_note"] or "확인이 필요한 제품이라 차량에 넣을 수 없습니다.")
    from_id = p["truck_id"] if p["status"] == "편성" else None
    if from_id == to_truck_id:
        raise MoveError("같은 차량으로는 이동할 수 없습니다.")

    item = _item_from_row(p)
    errors: list[str] = []

    if from_id:
        src = load_truck(conn, from_id)
        src_flagged = conn.execute("SELECT needs_review FROM trucks WHERE truck_id=?", (from_id,)).fetchone()
        src_broken = bool(_issues(src, rules)) or bool(src_flagged and src_flagged["needs_review"])
        src.items = [i for i in src.items if i.id != item.id]
        if src.items and not src_broken:
            errors += [f"{src.truck_id}: {e}" for e in validate_truck(src, rules)]

    if to_truck_id:
        row = conn.execute("SELECT shipped FROM trucks WHERE truck_id=?", (to_truck_id,)).fetchone()
        if not row:
            raise MoveError(f"차량 {to_truck_id}을(를) 찾을 수 없습니다.")
        if row["shipped"]:
            raise MoveError("이미 출하된 차량입니다.")
        dst = load_truck(conn, to_truck_id)
        before = _issues(dst, rules)
        before_kg = dst.total_kg
        dst.items.append(item)
        after = _issues(dst, rules)
        filling_underweight = after == {"UNDER"} and "UNDER" in before and dst.total_kg > before_kg
        if after and not filling_underweight:
            errors += [f"{dst.truck_id}: {e}" for e in validate_truck(dst, rules) if "< 하한" not in e or "UNDER" not in before]

    if errors:
        raise MoveError(" / ".join(errors))

    if to_truck_id:
        conn.execute(
            "UPDATE products SET status='편성', truck_id=?, unassigned_reason=NULL, refund_notice=0,"
            " needs_review=0, review_note=NULL, updated_at=datetime('now','localtime') WHERE id=?",
            (to_truck_id, item.id),
        )
        _log(conn, item.id, p["status"], "편성", f"수동 이동 → {to_truck_id}")
    else:
        conn.execute(
            "UPDATE products SET status='미배차', truck_id=NULL, unassigned_reason=NULL, refund_notice=0,"
            " needs_review=0, review_note=NULL, updated_at=datetime('now','localtime') WHERE id=?",
            (item.id,),
        )
        _log(conn, item.id, "편성", "미배차", f"수동 이동 ← {from_id}")
    for tid in {from_id, to_truck_id} - {None}:
        refresh_truck(conn, tid, rules)
        clear_review_if_valid(conn, tid, rules)
    conn.commit()
    return {"product_no": product_no, "from": from_id, "to": to_truck_id}


def dismantle_truck(conn: sqlite3.Connection, rules: DispatchRules, truck_id: str) -> dict:
    """차량 해체: 편성 제품을 모두 미배차로 되돌린다 (R-02). 출하된 제품이 있으면 그 기록은 남긴다."""
    t = conn.execute("SELECT * FROM trucks WHERE truck_id=?", (truck_id,)).fetchone()
    if not t:
        raise MoveError(f"차량 {truck_id}을(를) 찾을 수 없습니다.")
    if t["shipped"]:
        raise MoveError("이미 출하된 차량은 해체할 수 없습니다.")
    rows = conn.execute("SELECT id FROM products WHERE truck_id=? AND status='편성'", (truck_id,)).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE products SET status='미배차', truck_id=NULL, unassigned_reason=NULL, refund_notice=0,"
            " needs_review=0, review_note=NULL, updated_at=datetime('now','localtime') WHERE id=?",
            (r["id"],),
        )
        _log(conn, r["id"], "편성", "미배차", f"차량 해체 {truck_id}")
    refresh_truck(conn, truck_id, rules)  # 남은 제품이 없으면 차량 삭제
    conn.commit()
    return {"truck_id": truck_id, "released": len(rows)}
