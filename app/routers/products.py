"""제품 검색·대시보드 API (PRD F8)."""
import sqlite3

from fastapi import APIRouter, Depends, Request

from ..deps import get_db, get_embedder_or_none, get_vector_store
from ..services.search import row_summary, search
from ..vector_store import VectorStore

router = APIRouter(prefix="/api", tags=["products"])


@router.get("/products")
def search_products(request: Request, q: str = "", conn: sqlite3.Connection = Depends(get_db),
                    vs: VectorStore = Depends(get_vector_store)):
    result = search(conn, vs, get_embedder_or_none(request), q)
    return {"match_type": result.match_type, "rows": [row_summary(r) for r in result.rows]}


@router.get("/dashboard")
def dashboard(conn: sqlite3.Connection = Depends(get_db)):
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    status = {r[0]: {"count": r[1], "ton": round((r[2] or 0) / 1000, 3)}
              for r in conn.execute("SELECT status, COUNT(*), SUM(weight_kg) FROM products GROUP BY status")}
    by_cat = [dict(r) for r in conn.execute(
        "SELECT CASE WHEN category='WIDE' THEN '광폭재' WHEN is_long=1 THEN '일반재(장척)' ELSE '일반재' END AS kind,"
        " status, COUNT(*) AS count, ROUND(SUM(weight_kg)/1000.0, 3) AS ton"
        " FROM products WHERE category IS NOT NULL AND status IN ('미배차','편성','출하완료')"
        " GROUP BY kind, status ORDER BY kind, status")]
    carry = [row_summary(dict(r)) for r in conn.execute(
        "SELECT * FROM products WHERE status='미배차' AND carryover_days > 0"
        " ORDER BY carryover_days DESC, weight_kg DESC LIMIT 20")]
    uploads = [dict(r) for r in conn.execute("SELECT * FROM uploads ORDER BY id DESC LIMIT 10")]
    return {
        "status": status,
        "total_products": one("SELECT COUNT(*) FROM products"),
        "active_trucks": one("SELECT COUNT(*) FROM trucks WHERE shipped=0"),
        "review_trucks": one("SELECT COUNT(*) FROM trucks WHERE needs_review=1 AND shipped=0"),
        "assigned_ton": round((one("SELECT SUM(weight_kg) FROM products WHERE status='편성'") or 0) / 1000, 3),
        "by_category": by_cat,
        "carryover_top": carry,
        "recent_uploads": uploads,
        "embedding_pending": one("SELECT COUNT(*) FROM embedding_queue"),
    }
