"""SQLite 저장소 (PRD 6장).

SQLite가 모든 제품 상태의 기준(source of truth)이다. FAISS는 후보를 찾는 데만 쓴다.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 3

# 기존 DB에 없으면 ALTER TABLE 로 추가하는 컬럼 (v1 → v2 → v3)
MIGRATION_COLUMNS = {
    "products": {
        "order_key": "TEXT",
        "order_status": "TEXT",
        "last_dispatch_date": "TEXT",
    },
    "trucks": {
        "review_note": "TEXT",
    },
}

# 시작할 때마다 실행하는 데이터 정리 (멱등). R-03 이전 버전이 남긴 잘못된 차량번호를 정리한다.
REPAIRS = [
    "UPDATE products SET truck_id=NULL WHERE truck_id IS NOT NULL AND status NOT IN ('편성','출하완료')",
    "DELETE FROM trucks WHERE truck_id NOT IN (SELECT truck_id FROM products WHERE truck_id IS NOT NULL)",
]

STATUSES = ("입금대기", "미배차", "편성", "출하완료", "낙찰취소", "데이터오류")
UNASSIGNED_REASONS = ("OVER_MAX", "UNDER_MIN", "DATA_ERROR")
ORDER_STATUSES = ("낙찰확정", "낙찰", "낙찰취소")

_status_list = ", ".join(f"'{s}'" for s in STATUSES)
_reason_list = ", ".join(f"'{s}'" for s in UNASSIGNED_REASONS)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS trucks (
    truck_id        TEXT PRIMARY KEY,              -- 예: 2026-11-24-W01 / 2026-11-24-N01
    dispatch_date   TEXT NOT NULL,
    category        TEXT NOT NULL CHECK (category IN ('WIDE', 'NORMAL')),
    region          TEXT NOT NULL,
    region_source   TEXT NOT NULL CHECK (region_source IN ('address', 'route')),
    has_long        INTEGER NOT NULL DEFAULT 0,
    total_kg        REAL NOT NULL DEFAULT 0,
    max_ton         REAL NOT NULL,
    needs_review    INTEGER NOT NULL DEFAULT 0,
    review_note     TEXT,                          -- 확인 필요 사유 (취소로 빠진 제품 등)
    shipped         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS products (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,   -- FAISS ID와 동일
    product_no        TEXT NOT NULL UNIQUE,
    auction_no        TEXT,
    auction_date      TEXT,
    order_no          TEXT,                                -- 매칭 미확정이면 NULL
    order_key         TEXT,                                -- orders.order_key (매칭 확정 시)
    order_status      TEXT,                                -- 낙찰확정 / 낙찰 / 낙찰취소 / NULL(주문 정보 없음)
    order_candidates  TEXT,                                -- JSON 배열
    customer_code     TEXT,
    customer          TEXT,
    dlv_code          TEXT,
    dlv_name          TEXT,
    region            TEXT,
    region_source     TEXT CHECK (region_source IN ('address', 'route') OR region_source IS NULL),
    thickness_mm      REAL,
    width_mm          REAL,
    length_mm         REAL,
    length_source     TEXT CHECK (length_source IN ('column', 'calc') OR length_source IS NULL),
    weight_kg         REAL,
    spec              TEXT,
    category          TEXT CHECK (category IN ('WIDE', 'NORMAL') OR category IS NULL),
    is_long           INTEGER NOT NULL DEFAULT 0,
    ship_plan_date    TEXT,
    status            TEXT NOT NULL DEFAULT '미배차' CHECK (status IN ({_status_list})),
    unassigned_reason TEXT CHECK (unassigned_reason IN ({_reason_list}) OR unassigned_reason IS NULL),
    refund_notice     INTEGER NOT NULL DEFAULT 0,
    needs_review      INTEGER NOT NULL DEFAULT 0,
    review_note       TEXT,
    truck_id          TEXT REFERENCES trucks(truck_id) ON DELETE SET NULL,
    carryover_days    INTEGER NOT NULL DEFAULT 0,
    last_dispatch_date TEXT,                               -- 마지막으로 배차를 시도한 날짜 (이월 일수 계산)
    content_hash      TEXT,                                -- 중복/변경 판별
    embedded_hash     TEXT,                                -- 마지막으로 임베딩한 content_hash
    extra             TEXT,                                -- 원본 기타 컬럼 JSON
    first_seen        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    shipped_at        TEXT
);
CREATE INDEX IF NOT EXISTS ix_products_order    ON products(order_no);
CREATE INDEX IF NOT EXISTS ix_products_customer ON products(customer);
CREATE INDEX IF NOT EXISTS ix_products_dlv      ON products(dlv_code);
CREATE INDEX IF NOT EXISTS ix_products_status   ON products(status);
CREATE INDEX IF NOT EXISTS ix_products_truck    ON products(truck_id);
CREATE INDEX IF NOT EXISTS ix_products_join     ON products(auction_no, customer_code);

CREATE TABLE IF NOT EXISTS orders (               -- 경매 시트 행
    order_key       TEXT PRIMARY KEY,             -- 주문번호, 없으면 경매번호|고객코드|진행상태|중량
    auction_no      TEXT NOT NULL,
    auction_date    TEXT,
    customer_code   TEXT NOT NULL,
    customer        TEXT,
    status          TEXT NOT NULL,
    order_no        TEXT,
    qty             INTEGER,
    weight_kg       REAL,
    route           TEXT,
    dlv_code        TEXT,
    dlv_name        TEXT,
    ship_plan_date  TEXT,
    content_hash    TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS ix_orders_join ON orders(auction_no, customer_code);
CREATE INDEX IF NOT EXISTS ix_orders_no   ON orders(order_no);

CREATE TABLE IF NOT EXISTS delivery_places (      -- 인도처 주소표
    dlv_code    TEXT PRIMARY KEY,
    dlv_name    TEXT,
    address     TEXT,
    region      TEXT,                             -- 주소에서 뽑은 시 단위 지역 키
    route       TEXT,                             -- 경매 시트의 운송경로
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS uploads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name    TEXT NOT NULL,
    file_hash    TEXT NOT NULL,
    uploaded_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    new_cnt      INTEGER NOT NULL DEFAULT 0,
    dup_cnt      INTEGER NOT NULL DEFAULT 0,
    updated_cnt  INTEGER NOT NULL DEFAULT 0,
    error_cnt    INTEGER NOT NULL DEFAULT 0,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS ix_uploads_hash ON uploads(file_hash);

CREATE TABLE IF NOT EXISTS status_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    from_status  TEXT,
    to_status    TEXT NOT NULL,
    reason       TEXT,
    source_file  TEXT,
    changed_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS ix_history_product ON status_history(product_id);

CREATE TABLE IF NOT EXISTS embedding_queue (      -- 임베딩 실패 건 재시도 큐 (PRD F5)
    product_id  INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    queued_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI가 의존성(연결 생성)과 엔드포인트를 서로 다른 스레드에서 실행할 수 있다.
    # 연결은 요청 하나에서만 쓰고, 쓰기 작업은 locks.exclusive 로 직렬화한다.
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db(db_path: Path) -> None:
    # 테이블 → 컬럼 추가(마이그레이션) → 인덱스 순서. 인덱스가 새 컬럼을 참조할 수 있으므로.
    stmts = [s.strip() for s in SCHEMA.split(";") if s.strip()]
    tables = [s for s in stmts if not s.lstrip().upper().startswith("CREATE INDEX")]
    indexes = [s for s in stmts if s.lstrip().upper().startswith("CREATE INDEX")]
    with closing_conn(db_path) as conn:
        for s in tables:
            conn.execute(s)
        for table, cols in MIGRATION_COLUMNS.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        for s in indexes:
            conn.execute(s)
        for s in REPAIRS:
            conn.execute(s)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()


@contextmanager
def closing_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
