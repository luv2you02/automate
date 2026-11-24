"""업로드 API (PRD F1, F2, R3 주소표)."""
import sqlite3

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from ..config import Settings
from ..deps import get_db, get_embedder_or_none, get_settings_dep, get_vector_store
from ..locks import exclusive
from ..services.backup import ensure_daily_backup
from ..services.dedup import apply_delivery_places, apply_upload, sync_embeddings
from ..services.parser import ParseError, parse_delivery_places, parse_workbook
from ..services.regions import region_from_address
from ..vector_store import VectorStore

router = APIRouter(prefix="/api", tags=["upload"])

MAX_FILE_BYTES = 30 * 1024 * 1024


async def _read(f: UploadFile) -> bytes:
    content = await f.read()
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(413, f"{f.filename}: 파일이 너무 큽니다 (최대 30MB).")
    if not content:
        raise HTTPException(400, f"{f.filename}: 빈 파일입니다.")
    return content


@router.post("/upload")
async def upload(
    request: Request,
    files: list[UploadFile] = File(...),
    preview: bool = Query(False, description="true 면 반영하지 않고 시트 판별 결과만 반환"),
    settings: Settings = Depends(get_settings_dep),
    conn: sqlite3.Connection = Depends(get_db),
    vs: VectorStore = Depends(get_vector_store),
):
    """낙찰현황 엑셀 업로드 → 시트 판별·연결·중복제거·임베딩, 요약 반환."""
    contents = [(f.filename or "upload.xlsx", await _read(f)) for f in files]

    if preview:
        out = []
        for name, content in contents:
            try:
                parsed = parse_workbook(content, name)
            except ParseError as e:
                out.append({"file_name": name, "error": str(e)})
                continue
            dup = conn.execute("SELECT uploaded_at FROM uploads WHERE file_hash=? ORDER BY id DESC LIMIT 1",
                               (parsed.file_hash,)).fetchone()
            out.append({
                "file_name": name,
                "sheets": [{"name": s.name, "kind": s.kind, "rows": s.rows} for s in parsed.sheets],
                "products": len(parsed.products), "orders": len(parsed.orders), "errors": len(parsed.errors),
                "has_length_column": parsed.has_length_column, "has_shipped_column": parsed.has_shipped_column,
                "duplicate_file": dup is not None, "previous_upload_at": dup[0] if dup else None,
            })
        return {"preview": True, "files": out}

    def process() -> dict:
        with exclusive("업로드"):
            ensure_daily_backup(settings)
            results = []
            for name, content in contents:
                try:
                    parsed = parse_workbook(content, name)
                except ParseError as e:
                    results.append({"file_name": name, "error": str(e)})
                    continue
                results.append(apply_upload(conn, parsed, settings.rules).to_dict())

            embedder = get_embedder_or_none(request)
            if embedder is None:
                emb = {"embedded": 0, "pending": None, "error": request.app.state.embedder_error}
            else:
                emb = sync_embeddings(conn, vs, embedder)
        return {"files": results, "embedding": emb}

    # 파싱·배차 판단·임베딩은 오래 걸릴 수 있으므로 이벤트 루프를 막지 않게 스레드에서 실행
    return await run_in_threadpool(process)


@router.post("/delivery-places")
async def upload_delivery_places(
    request: Request,
    file: UploadFile = File(...),
    settings: Settings = Depends(get_settings_dep),
    conn: sqlite3.Connection = Depends(get_db),
    vs: VectorStore = Depends(get_vector_store),
):
    """인도처 주소표(인도처코드, 주소) 업로드/수정."""
    content = await _read(file)
    try:
        rows = parse_delivery_places(content)
    except ParseError as e:
        raise HTTPException(400, str(e))
    def process() -> dict:
        with exclusive("주소표 업로드"):
            result = apply_delivery_places(conn, rows, settings.rules)
            embedder = get_embedder_or_none(request)
            if embedder is not None:
                sync_embeddings(conn, vs, embedder)
        return result

    return await run_in_threadpool(process)


class PlaceUpdate(BaseModel):
    address: str | None = None


@router.put("/delivery-places/{dlv_code}")
def update_delivery_place(
    dlv_code: str,
    body: PlaceUpdate,
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    conn: sqlite3.Connection = Depends(get_db),
    vs: VectorStore = Depends(get_vector_store),
):
    """화면에서 인도처 주소 한 건 입력/수정."""
    addr = (body.address or "").strip() or None
    if addr and region_from_address(addr) is None:
        raise HTTPException(400, f"주소에서 시·군을 찾지 못했습니다: {addr} (예: 경남 창원시 성산구 …)")
    row = conn.execute("SELECT dlv_name FROM delivery_places WHERE dlv_code=?", (dlv_code,)).fetchone()
    if row is None:
        raise HTTPException(404, f"인도처 {dlv_code}을(를) 찾을 수 없습니다.")
    with exclusive("주소 수정"):
        result = apply_delivery_places(conn, [{"dlv_code": dlv_code, "dlv_name": row["dlv_name"], "address": addr}],
                                       settings.rules)
        embedder = get_embedder_or_none(request)
        if embedder is not None:
            sync_embeddings(conn, vs, embedder)
    return result


@router.get("/delivery-places")
def list_delivery_places(conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(
        "SELECT d.*, (SELECT COUNT(*) FROM products p WHERE p.dlv_code=d.dlv_code AND p.status<>'출하완료') AS active"
        " FROM delivery_places d ORDER BY (d.address IS NULL) DESC, d.dlv_name"
    ).fetchall()
    return {"places": [dict(r) | {"region_source": "address" if r["region"] else ("route" if r["route"] else None)}
                       for r in rows]}


@router.get("/uploads")
def list_uploads(conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute("SELECT * FROM uploads ORDER BY id DESC LIMIT 100").fetchall()
    return {"uploads": [dict(r) for r in rows]}
