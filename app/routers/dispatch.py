"""배차 API (PRD F3)."""
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..config import Settings
from ..deps import get_db, get_settings_dep
from ..locks import exclusive
from ..services.dispatch import MoveError, dismantle_truck, move_product, run_dispatch
from ..services.search import BASE_SELECT, row_summary

router = APIRouter(prefix="/api/dispatch", tags=["dispatch"])


class RunRequest(BaseModel):
    date: str | None = None  # YYYY-MM-DD, 없으면 오늘


class MoveRequest(BaseModel):
    product_no: str
    to_truck_id: str | None = None  # None 이면 미배차로 되돌림


@router.post("/run")
def run(body: RunRequest | None = None, settings: Settings = Depends(get_settings_dep),
        conn: sqlite3.Connection = Depends(get_db)):
    """미배차 제품으로 자동배차 실행 (편성 제품은 그대로)."""
    with exclusive("배차"):
        result = run_dispatch(conn, settings.rules, (body.date if body else None))
    return result.summary()


@router.get("")
def get_dispatch(date: str | None = None, include_shipped: bool = False,
                 conn: sqlite3.Connection = Depends(get_db)):
    """배차 결과: 차량(+제품), 미배차, 입금대기. date 를 주면 그 날짜 차량만."""
    where, args = [], []
    if date:
        where.append("dispatch_date=?")
        args.append(date)
    if not include_shipped:
        where.append("shipped=0")
    sql = "SELECT * FROM trucks" + (" WHERE " + " AND ".join(where) if where else "") + \
          " ORDER BY needs_review DESC, dispatch_date DESC, truck_id"
    trucks = [dict(t) for t in conn.execute(sql, args)]
    for t in trucks:
        rows = [dict(r) for r in conn.execute(f"{BASE_SELECT} WHERE p.truck_id=? ORDER BY p.weight_kg DESC",
                                              (t["truck_id"],))]
        t["products"] = [row_summary(r) for r in rows]
        t["total_ton"] = round(t["total_kg"] / 1000, 3)
        t["customers"] = sorted({r["customer"] for r in rows if r["customer"]})
        t["dlv_names"] = sorted({r["dlv_name"] for r in rows if r["dlv_name"]})

    unassigned = [row_summary(dict(r)) for r in conn.execute(
        f"{BASE_SELECT} WHERE p.status IN ('미배차','데이터오류') ORDER BY p.carryover_days DESC, p.region, p.weight_kg DESC")]
    waiting = [row_summary(dict(r)) for r in conn.execute(
        f"{BASE_SELECT} WHERE p.status='입금대기' ORDER BY p.auction_date, p.customer")]
    return {"trucks": trucks, "unassigned": unassigned, "waiting_payment": waiting}


@router.post("/move")
def move(req: MoveRequest, settings: Settings = Depends(get_settings_dep),
         conn: sqlite3.Connection = Depends(get_db)):
    """제품 수동 이동 (R1·R2·R3 검증)."""
    with exclusive("수동 이동"):
        try:
            return move_product(conn, settings.rules, req.product_no, req.to_truck_id)
        except MoveError as e:
            raise HTTPException(status_code=422, detail=str(e))


class DismantleRequest(BaseModel):
    truck_id: str


@router.post("/dismantle")
def dismantle(req: DismantleRequest, settings: Settings = Depends(get_settings_dep),
              conn: sqlite3.Connection = Depends(get_db)):
    """차량 해체: 편성 제품을 모두 미배차로 되돌린다."""
    with exclusive("차량 해체"):
        try:
            return dismantle_truck(conn, settings.rules, req.truck_id)
        except MoveError as e:
            raise HTTPException(status_code=422, detail=str(e))
