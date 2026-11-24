"""쓰기 작업 잠금.

업로드·배차·복원은 DB와 FAISS를 함께 바꾸므로 서버 전체에서 한 번에 하나만 실행한다.
이미 실행 중이면 기다리지 않고 409를 돌려준다 (배차 버튼 연타 방지).
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from fastapi import HTTPException

_write_lock = threading.Lock()
_current: dict[str, str | None] = {"job": None}


@contextmanager
def exclusive(job: str) -> Iterator[None]:
    if not _write_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=f"다른 작업({_current['job']})이 진행 중입니다. 끝난 뒤 다시 시도해 주세요.",
        )
    _current["job"] = job
    try:
        yield
    finally:
        _current["job"] = None
        _write_lock.release()


def current_job() -> str | None:
    return _current["job"]
