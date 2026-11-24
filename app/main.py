"""FastAPI 앱.

- /api/*  : REST API (UI는 이것만 호출한다 — 2단계 다수 사용자 전환 대비)
- /       : static/ 의 단일 페이지 UI
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import PROJECT_ROOT, Settings, get_settings
from .db import init_db
from .routers import admin, chat, dispatch, health, products, upload
from .services.backup import ensure_daily_backup
from .vector_store import VectorStore


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        init_db(settings.db_path)
        app.state.vector_store = VectorStore(settings.faiss_path, settings.embedding_dim).load()
        app.state.embedder = None        # 처음 쓸 때 생성 (deps.get_embedder_or_none)
        app.state.embedder_error = None
        ensure_daily_backup(settings)
        yield
        app.state.vector_store.save()

    app = FastAPI(title="후판 경매 자동배차", lifespan=lifespan)
    app.state.settings = settings

    for r in (health, upload, dispatch, chat, products, admin):
        app.include_router(r.router)

    # API 라우터 뒤에 마운트해야 /api 경로가 가려지지 않는다.
    app.mount("/", StaticFiles(directory=PROJECT_ROOT / "static", html=True), name="static")
    return app


app = create_app()
