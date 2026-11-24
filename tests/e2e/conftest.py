"""E2E (Playwright) 공통 픽스처.

실행:  .venv\\Scripts\\python -m pytest -m e2e tests/e2e
- 실제 서버를 별도 프로세스로 띄운다 (임시 DATA_DIR, EMBEDDING_FAKE=1 → 실제 data/·OpenAI 사용 안 함)
- 스크린샷: review/screenshots/
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pw = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "review" / "screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None

    def start(self) -> "Server":
        env = os.environ | {
            "DATA_DIR": str(self.data_dir), "EMBEDDING_FAKE": "1", "OPEN_BROWSER": "0",
            "HOST": "127.0.0.1", "PORT": str(self.port), "PYTHONIOENCODING": "utf-8",
        }
        self.proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=ROOT, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                if httpx.get(f"{self.url}/api/health", timeout=1).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.15)
        raise RuntimeError("서버가 시작되지 않았습니다")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(10)

    def restart(self) -> None:
        self.stop()
        self.start()

    def api(self, method: str, path: str, **kw) -> httpx.Response:
        return httpx.request(method, f"{self.url}{path}", timeout=60, **kw)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    s = Server(tmp_path_factory.mktemp("e2e") / "데이터").start()
    yield s
    s.stop()


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser, server):
    ctx = browser.new_context(viewport={"width": 1366, "height": 768}, locale="ko-KR")
    ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=server.url)
    pg = ctx.new_page()
    pg.console_errors = []  # type: ignore[attr-defined]
    pg.on("console", lambda m: m.type == "error" and pg.console_errors.append(m.text))
    pg.on("pageerror", lambda e: pg.console_errors.append(f"pageerror: {e}"))
    pg.goto(server.url)
    pg.wait_for_selector("#health.ok")
    yield pg
    ctx.close()


def shot(page, name: str, full: bool = False) -> None:
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=full)


def tab(page, name: str) -> None:
    page.click(f"#tabbtn-{name}")
    page.wait_for_timeout(250)


def upload_via_ui(page, path_or_bytes, name="upload.xlsx", expect_ok=True):
    tab(page, "upload")
    if isinstance(path_or_bytes, (bytes, bytearray)):
        page.set_input_files("#upload-files", files=[{"name": name, "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "buffer": bytes(path_or_bytes)}])
    else:
        page.set_input_files("#upload-files", str(path_or_bytes))
    page.wait_for_selector("#preview .file-card")
    if not expect_ok:
        return
    page.click("#btn-upload")
    page.wait_for_selector("#upload-result .file-card", timeout=60000)


def kpis(page) -> dict[str, int]:
    out = {}
    for k in page.locator("#upload-result .kpi").all():
        label = k.locator("span").inner_text()
        out[label] = int(k.locator("b").inner_text().replace(",", ""))
    return out
