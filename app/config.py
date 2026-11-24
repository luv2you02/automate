"""설정 로드 (.env).

- 프로젝트 루트의 .env를 읽는다. 이미 설정된 환경변수가 우선한다(테스트에서 덮어쓰기 위함).
- 배차 기준 수치는 모두 여기서 읽고, 코드에 하드코딩하지 않는다 (PRD 5.2).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

EMBEDDING_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


def _int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class DispatchRules:
    wide_width_mm: float = 3400      # 폭 > 이 값이면 광폭재 (같으면 일반재)
    long_length_mm: float = 17000    # 길이 > 이 값이면 장척재 (같으면 장척 아님)
    truck_min_ton: float = 25
    wide_max_ton: float = 26
    normal_max_ton: float = 27
    long_max_ton: float = 26


@dataclass(frozen=True)
class Settings:
    openai_api_key: str = field(default="", repr=False)  # repr에서 제외: 로그 노출 방지
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    embedding_fake: bool = False
    chat_model: str | None = None
    data_dir: Path = PROJECT_ROOT / "data"
    host: str = "127.0.0.1"
    port: int = 8000
    open_browser: bool = True
    rules: DispatchRules = field(default_factory=DispatchRules)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def faiss_path(self) -> Path:
        return self.data_dir / "faiss.index"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backup"

    def public_dict(self) -> dict:
        """화면/health에 내보내도 되는 값만 (API 키 제외)."""
        return {
            "embedding_model": self.embedding_model,
            "embedding_dim": self.embedding_dim,
            "embedding_fake": self.embedding_fake,
            "chat_model": self.chat_model,
            "api_key_configured": bool(self.openai_api_key),
            "host": self.host,
            "port": self.port,
            "rules": self.rules.__dict__,
        }


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env", override=False)

    data_dir = Path(_env("DATA_DIR", "./data"))
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir

    model = _env("EMBEDDING_MODEL", "text-embedding-3-small")
    dim = _int("EMBEDDING_DIM", EMBEDDING_DIMS.get(model, 1536))

    return Settings(
        openai_api_key=_env("OPENAI_API_KEY"),
        embedding_model=model,
        embedding_dim=dim,
        embedding_fake=_bool("EMBEDDING_FAKE"),
        chat_model=_env("CHAT_MODEL") or None,
        data_dir=data_dir,
        host=_env("HOST", "127.0.0.1"),
        port=_int("PORT", 8000),
        open_browser=_bool("OPEN_BROWSER", True),
        rules=DispatchRules(
            wide_width_mm=_float("WIDE_WIDTH_MM", 3400),
            long_length_mm=_float("LONG_LENGTH_MM", 17000),
            truck_min_ton=_float("TRUCK_MIN_TON", 25),
            wide_max_ton=_float("WIDE_MAX_TON", 26),
            normal_max_ton=_float("NORMAL_MAX_TON", 27),
            long_max_ton=_float("LONG_MAX_TON", 26),
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
