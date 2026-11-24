"""데이터 초기화: 업로드·배차·이력 데이터를 모두 지우고 처음 상태로 돌린다.

- 지우기 전에 반드시 'prereset' 백업을 만든다 (복원으로 되돌릴 수 있게).
- 백업 폴더(data/backup)는 지우지 않는다.
- keep_places=True 면 인도처 주소표(직접 입력한 주소)는 남기고, 운송경로 정보만 비운다.
"""
from __future__ import annotations

import sqlite3

from ..config import Settings
from ..vector_store import VectorStore
from .backup import create_backup

CONFIRM_WORD = "초기화"
# 외래키 순서대로 (자식 → 부모)
TABLES = ["status_history", "embedding_queue", "products", "trucks", "orders", "uploads"]


def reset_data(conn: sqlite3.Connection, settings: Settings, vs: VectorStore, keep_places: bool = True) -> dict:
    backup = create_backup(settings, "prereset")["name"]
    counts = {}
    for t in TABLES + ([] if keep_places else ["delivery_places"]):
        counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        conn.execute(f"DELETE FROM {t}")
    if keep_places:
        # 주소는 남기되, 다음 업로드에서 다시 채워질 운송경로·제품 정보는 비운다. 주소 없는 행은 의미가 없으므로 삭제
        conn.execute("DELETE FROM delivery_places WHERE address IS NULL OR TRIM(address) = ''")
        conn.execute("UPDATE delivery_places SET route = NULL")
        counts["delivery_places_kept"] = conn.execute("SELECT COUNT(*) FROM delivery_places").fetchone()[0]
    # 제품 ID(=FAISS ID)를 1부터 다시 시작
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('products', 'uploads', 'status_history')")
    conn.commit()
    with vs._lock:
        vs.index = vs._new_index()
        vs.save()
    return {"backup": backup, "deleted": counts, "kept_places": keep_places}
