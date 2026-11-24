import sqlite3

import pytest

from app.db import SCHEMA_VERSION, closing_conn, init_db

TABLES = {"products", "orders", "trucks", "delivery_places", "uploads", "status_history", "embedding_queue"}


def test_schema_created(settings):
    init_db(settings.db_path)
    with closing_conn(settings.db_path) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert TABLES <= names
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_init_is_idempotent_and_keeps_data(settings):
    init_db(settings.db_path)
    with closing_conn(settings.db_path) as conn:
        conn.execute("INSERT INTO products(product_no, status) VALUES ('FE02294303', '미배차')")
        conn.commit()
    init_db(settings.db_path)
    with closing_conn(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1


def test_status_constraint(settings):
    init_db(settings.db_path)
    with closing_conn(settings.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO products(product_no, status) VALUES ('X', '없는상태')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO products(product_no, unassigned_reason) VALUES ('Y', 'WHATEVER')")


def test_product_no_unique(settings):
    init_db(settings.db_path)
    with closing_conn(settings.db_path) as conn:
        conn.execute("INSERT INTO products(product_no) VALUES ('FE1')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO products(product_no) VALUES ('FE1')")
