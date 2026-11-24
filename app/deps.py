"""FastAPI 의존성: 요청마다 DB 연결, 앱 공용 객체 접근."""
from __future__ import annotations

import sqlite3
from typing import Iterator

from fastapi import HTTPException, Request

from .config import Settings
from .db import connect
from .embeddings import Embedder, get_embedder
from .vector_store import VectorStore


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_vector_store(request: Request) -> VectorStore:
    return request.app.state.vector_store


def get_embedder_or_none(request: Request) -> Embedder | None:
    """임베딩 클라이언트 (처음 쓸 때 생성). API 키가 없으면 None — 업로드·배차는 계속 동작한다."""
    state = request.app.state
    if getattr(state, "embedder", None) is None:
        try:
            state.embedder = get_embedder(state.settings)
            state.embedder_error = None
        except Exception as e:
            state.embedder_error = str(e)
            return None
    return state.embedder


def not_implemented(feature: str, stage: int = 2) -> HTTPException:
    return HTTPException(status_code=501, detail=f"{feature}: {stage}단계에서 구현 예정입니다.")
