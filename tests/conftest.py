"""공통 픽스처.

- 매 테스트마다 임시 DATA_DIR 사용 (실제 data/ 는 건드리지 않음). 경로에 한글을 넣어
  Windows 한글 경로 문제(FAISS)도 함께 검증한다.
- EMBEDDING_FAKE: 실제 OpenAI 호출 금지.
"""
import os

os.environ["EMBEDDING_FAKE"] = "1"
os.environ["OPEN_BROWSER"] = "0"

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

SAMPLE_XLSX = Path(__file__).resolve().parent.parent / "sample" / "후판_업체별_제품단위_낙찰현황_인도처추가.xlsx"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        openai_api_key="",
        embedding_fake=True,
        embedding_dim=1536,
        data_dir=tmp_path / "데이터",
        open_browser=False,
    )


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as c:
        yield c
