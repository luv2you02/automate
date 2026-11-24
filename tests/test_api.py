from fastapi.testclient import TestClient

from app import locks
from app.main import create_app


def test_health(client, settings):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db"]["ok"] is True
    assert body["faiss"]["ok"] is True
    assert body["faiss"]["dim"] == 1536
    assert body["settings"]["embedding_fake"] is True
    assert "openai_api_key" not in body["settings"]
    assert settings.db_path.exists() and settings.faiss_path.exists()


def test_index_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "후판 경매 자동배차" in r.text
    for tab in ("업로드", "배차결과", "미배차", "챗봇", "대시보드"):
        assert tab in r.text
    assert client.get("/app.js").status_code == 200


def test_empty_state_endpoints(client):
    assert client.get("/api/dispatch").json() == {"trucks": [], "unassigned": [], "waiting_payment": []}
    assert client.post("/api/dispatch/run", json={"date": "2026-11-24"}).json()["truck_count"] == 0
    assert client.get("/api/dashboard").json()["total_products"] == 0
    assert client.post("/api/chat", json={"question": "FA00000001"}).json()["found"] is False
    assert client.post("/api/chat", json={"question": "  "}).json()["found"] is False
    assert client.get("/api/export").status_code == 200


def test_bad_upload_reports_error(client):
    r = client.post("/api/upload", files={"files": ("a.xlsx", b"not an excel file")})
    assert r.status_code == 200 and "읽을 수 없습니다" in r.json()["files"][0]["error"]
    r = client.post("/api/upload", files={"files": ("a.xlsx", b"")})
    assert r.status_code == 400
    r = client.post("/api/delivery-places", files={"file": ("a.xlsx", b"junk")})
    assert r.status_code == 400


def test_write_lock_rejects_concurrent_job(client):
    with locks.exclusive("배차"):
        r = client.post("/api/dispatch/run", json={})
    assert r.status_code == 409
    assert "배차" in r.json()["detail"]
    assert client.post("/api/dispatch/run", json={}).status_code == 200  # 잠금이 풀리면 다시 가능


def test_data_survives_restart(settings):
    with TestClient(create_app(settings)) as c:
        c.get("/api/health")
    from app.db import closing_conn

    with closing_conn(settings.db_path) as conn:
        conn.execute("INSERT INTO products(product_no) VALUES ('FE02294303')")
        conn.commit()
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/health").json()["db"]["products"] == 1


def test_schema_migration_from_v1(settings):
    """1단계 DB(컬럼 없음)에 2단계 컬럼이 자동 추가된다."""
    import sqlite3

    from app.db import init_db

    init_db(settings.db_path)
    conn = sqlite3.connect(settings.db_path)
    for col in ("order_key", "order_status", "last_dispatch_date"):  # v1 상태로 되돌림
        conn.execute(f"ALTER TABLE products DROP COLUMN {col}")
    conn.execute("INSERT INTO products(product_no) VALUES ('FE1')")
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()
    init_db(settings.db_path)
    conn = sqlite3.connect(settings.db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
    kept = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    conn.close()
    assert {"order_key", "order_status", "last_dispatch_date"} <= cols
    assert kept == 1
