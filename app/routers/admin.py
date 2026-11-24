"""내보내기·백업·복원 API (PRD 4장)."""
import io
import sqlite3
from datetime import date as date_cls
from urllib.parse import quote

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import Settings
from ..deps import get_db, get_settings_dep
from ..locks import exclusive
from ..services.backup import create_backup, list_backups, restore_backup
from ..services.reset import CONFIRM_WORD, reset_data
from ..services.search import BASE_SELECT
from ..vector_store import VectorStore

router = APIRouter(prefix="/api", tags=["admin"])

STATUS_COLS = {
    "truck_id": "차량번호", "product_no": "제품번호", "order_no": "주문번호", "auction_no": "경매번호",
    "customer": "고객사", "dlv_name": "인도처", "dlv_address": "주소", "region": "지역", "status": "상태",
    "unassigned_reason": "미배차사유", "carryover_days": "이월일수", "thickness_mm": "두께(mm)",
    "width_mm": "폭(mm)", "length_mm": "길이(mm)", "weight_kg": "중량(kg)", "ship_plan_date": "출하예정일",
}


@router.get("/export")
def export(date: str | None = None, conn: sqlite3.Connection = Depends(get_db)):
    """배차 결과 엑셀 다운로드 (시트: 차량요약 / 편성제품 / 미배차)."""
    tw, ta = ("WHERE dispatch_date=?", [date]) if date else ("WHERE shipped=0", [])
    trucks = pd.read_sql_query(
        f"SELECT truck_id AS 차량번호, dispatch_date AS 배차일, CASE category WHEN 'WIDE' THEN '광폭재' ELSE '일반재' END AS 분류,"
        f" region AS 지역, CASE region_source WHEN 'address' THEN '주소' ELSE '운송경로' END AS 지역근거,"
        f" CASE has_long WHEN 1 THEN 'Y' ELSE '' END AS 장척포함, ROUND(total_kg/1000.0, 3) AS 합계톤, max_ton AS 상한톤,"
        f" CASE needs_review WHEN 1 THEN '확인 필요' ELSE '' END AS 비고 FROM trucks {tw} ORDER BY truck_id",
        conn, params=ta)
    ids = list(trucks["차량번호"])
    cols = ", ".join(f"{('d.address' if k == 'dlv_address' else 'p.' + k)} AS \"{v}\"" for k, v in STATUS_COLS.items())
    base = BASE_SELECT.replace("SELECT p.*, d.address AS dlv_address, o.ship_plan_date AS order_ship_plan_date", f"SELECT {cols}")
    assigned = pd.read_sql_query(
        f"{base} WHERE p.truck_id IN ({','.join('?' * len(ids))}) ORDER BY p.truck_id, p.weight_kg DESC", conn,
        params=ids) if ids else pd.DataFrame(columns=list(STATUS_COLS.values()))
    unassigned = pd.read_sql_query(
        f"{base} WHERE p.status IN ('미배차','데이터오류') ORDER BY p.region, p.weight_kg DESC", conn)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        trucks.to_excel(xw, sheet_name="차량요약", index=False)
        assigned.to_excel(xw, sheet_name="편성제품", index=False)
        unassigned.to_excel(xw, sheet_name="미배차", index=False)
    buf.seek(0)
    fname = f"배차결과_{date or date_cls.today().isoformat()}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )


@router.get("/backups")
def backups(settings: Settings = Depends(get_settings_dep)):
    return {"backups": list_backups(settings)}


@router.post("/backup")
def backup(settings: Settings = Depends(get_settings_dep)):
    with exclusive("백업"):
        return create_backup(settings)


class ResetRequest(BaseModel):
    confirm: str               # 화면에서 사용자가 직접 입력한 확인 문구 ('초기화')
    keep_places: bool = True   # 인도처 주소표 남기기


@router.post("/reset")
def reset(body: ResetRequest, request: Request, settings: Settings = Depends(get_settings_dep),
          conn: sqlite3.Connection = Depends(get_db)):
    """데이터 초기화 (지우기 전에 자동 백업)."""
    if body.confirm.strip() != CONFIRM_WORD:
        raise HTTPException(400, f"확인 문구가 맞지 않습니다. '{CONFIRM_WORD}'를 입력해 주세요.")
    with exclusive("데이터 초기화"):
        return reset_data(conn, settings, request.app.state.vector_store, body.keep_places)


class RestoreRequest(BaseModel):
    name: str


@router.post("/restore")
def restore(body: RestoreRequest, request: Request, settings: Settings = Depends(get_settings_dep)):
    with exclusive("복원"):
        try:
            safety = restore_backup(settings, body.name)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e))
        request.app.state.vector_store = VectorStore(settings.faiss_path, settings.embedding_dim).load()
    return {"restored": body.name, "safety_backup": safety}
