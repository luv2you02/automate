"""백업/복원 (PRD 4장).

data/backup/<YYYYMMDD-HHMMSS>-<종류>[-n]/ 에 app.db + faiss.index
- 종류: auto(하루 1회 자동) · manual(수동) · prerestore(복원 직전) · prereset(데이터 초기화 직전)
- 보관: 최근 7일 (날짜 기준, R-07). 디스크 보호를 위해 전체 60개를 넘으면 오래된 것부터 정리.
- 같은 초에 여러 번 만들어도 이름 뒤에 번호를 붙여 덮어쓰지 않는다.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from ..config import Settings
from ..db import connect

KEEP_DAYS = 7
MAX_BACKUPS = 60
KINDS = ("auto", "manual", "prerestore", "prereset")
NAME_RE = re.compile(r"^(\d{8})-(\d{6})(?:-(auto|manual|prerestore|prereset))?(?:-(\d+))?$")


def _parse(name: str) -> dict | None:
    m = NAME_RE.match(name)
    if not m:
        return None
    return {"date": m.group(1), "kind": m.group(3) or "manual"}


def list_backups(settings: Settings) -> list[dict]:
    """최신순."""
    d = settings.backup_dir
    if not d.exists():
        return []
    out = []
    for p in sorted(d.iterdir(), reverse=True):
        info = _parse(p.name)
        if p.is_dir() and info and (p / "app.db").exists():
            out.append({"name": p.name, "kind": info["kind"], "date": info["date"],
                        "size": sum(f.stat().st_size for f in p.iterdir())})
    return out


def _new_dir(settings: Settings, kind: str) -> Path:
    base = f"{datetime.now():%Y%m%d-%H%M%S}-{kind}"
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    for n in range(1, 1000):
        path = settings.backup_dir / (base if n == 1 else f"{base}-{n}")
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise RuntimeError("백업 폴더 이름을 만들 수 없습니다.")


def prune(settings: Settings, today: date | None = None) -> list[str]:
    """최근 KEEP_DAYS 일보다 오래된 백업 삭제 (가장 최근 1개는 항상 남김). 전체 MAX_BACKUPS 초과분도 삭제."""
    today = today or date.today()
    cutoff = (today - timedelta(days=KEEP_DAYS - 1)).strftime("%Y%m%d")
    backups = list_backups(settings)
    removed = []
    for i, b in enumerate(backups):
        if i == 0:
            continue
        if b["date"] < cutoff or i >= MAX_BACKUPS:
            shutil.rmtree(settings.backup_dir / b["name"], ignore_errors=True)
            removed.append(b["name"])
    return removed


def create_backup(settings: Settings, kind: str = "manual") -> dict:
    if kind not in KINDS:
        raise ValueError(f"알 수 없는 백업 종류: {kind}")
    dest = _new_dir(settings, kind)
    src = connect(settings.db_path)
    dst = sqlite3.connect(dest / "app.db")
    try:
        src.backup(dst)  # WAL 사용 중에도 일관된 스냅샷
    finally:
        dst.close()
        src.close()
    if settings.faiss_path.exists():
        shutil.copy2(settings.faiss_path, dest / "faiss.index")
    prune(settings)
    return {"name": dest.name, "kind": kind}


def ensure_daily_backup(settings: Settings) -> dict | None:
    """오늘 자동 백업이 없으면 만든다 (서버 시작·업로드 때 호출)."""
    if not settings.db_path.exists():
        return None
    today = datetime.now().strftime("%Y%m%d")
    if any(b["date"] == today and b["kind"] == "auto" for b in list_backups(settings)):
        return None
    return create_backup(settings, "auto")


def restore_backup(settings: Settings, name: str) -> str:
    """복원. 복원 직전 상태를 'prerestore' 백업으로 남기고 그 이름을 돌려준다 (잘못 복원해도 되돌릴 수 있게)."""
    if not _parse(name):
        raise ValueError("백업 이름 형식이 올바르지 않습니다.")
    src_dir: Path = settings.backup_dir / name
    if not (src_dir / "app.db").exists():
        raise FileNotFoundError(f"백업 {name}을(를) 찾을 수 없습니다.")
    safety = create_backup(settings, "prerestore")["name"]
    src = sqlite3.connect(src_dir / "app.db")
    dst = connect(settings.db_path)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    if (src_dir / "faiss.index").exists():
        shutil.copy2(src_dir / "faiss.index", settings.faiss_path)
    return safety
