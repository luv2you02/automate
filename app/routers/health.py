import sqlite3

from fastapi import APIRouter, Depends

from ..config import Settings
from ..db import SCHEMA_VERSION
from ..deps import get_db, get_settings_dep, get_vector_store
from ..locks import current_job
from ..vector_store import VectorStore

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health(
    settings: Settings = Depends(get_settings_dep),
    conn: sqlite3.Connection = Depends(get_db),
    vs: VectorStore = Depends(get_vector_store),
):
    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    product_cnt = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    return {
        "status": "ok",
        "db": {
            "ok": user_version == SCHEMA_VERSION,
            "schema_version": user_version,
            "products": product_cnt,
        },
        "faiss": {"ok": settings.faiss_path.exists(), "vectors": vs.size, "dim": vs.dim},
        "current_job": current_job(),
        "settings": settings.public_dict(),
    }
