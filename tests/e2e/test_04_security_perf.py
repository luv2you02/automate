"""보안·성능 (PRD 4장): API 키 노출, 바인딩 주소, 처리 시간, UI 폭."""
from __future__ import annotations

import json
import socket
import time

import httpx
import pytest

from tests.e2e.conftest import ROOT, SHOTS, shot, tab
from tests.fixtures.builders import big_file

pytestmark = pytest.mark.e2e
PERF: dict = {}


def _api_key() -> str | None:
    env = ROOT / ".env"
    if not env.exists():
        return None
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip() or None
    return None


def test_api_key_never_exposed(page, server):
    key = _api_key()
    if not key:
        pytest.skip(".env 에 API 키 없음")
    bodies = []
    for path in ["/", "/app.js", "/style.css", "/api/health", "/api/dashboard", "/api/dispatch", "/api/uploads",
                 "/api/delivery-places", "/api/backups", "/api/products?q=FD", "/openapi.json", "/docs"]:
        bodies.append(server.api("GET", path).text)
    bodies.append(server.api("POST", "/api/chat", json={"question": "명강스틸"}).text)
    leaked = [i for i, b in enumerate(bodies) if key in b or key[-12:] in b]
    assert leaked == []  # 키 값 자체는 출력하지 않는다
    seen = []
    page.on("response", lambda r: seen.append(r))
    for t in ("upload", "dispatch", "unassigned", "chat", "dashboard"):
        tab(page, t)
    for r in seen:
        try:
            assert key not in r.text()
        except UnicodeDecodeError:
            pass


def test_bound_to_localhost_only(server):
    lan_ips = {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)} - {"127.0.0.1"}
    reachable = []
    for ip in lan_ips:
        try:
            httpx.get(f"http://{ip}:{server.port}/api/health", timeout=1.5)
            reachable.append(ip)
        except httpx.HTTPError:
            pass
    assert reachable == []


def test_performance_3000_products(server):
    big = big_file(3000)
    t0 = time.perf_counter()
    r = server.api("POST", "/api/upload", files={"files": ("big.xlsx", big)})
    PERF["upload_3000_sec"] = round(time.perf_counter() - t0, 2)
    assert r.status_code == 200
    t0 = time.perf_counter()
    d = server.api("POST", "/api/dispatch/run", json={}).json()
    PERF["dispatch_http_sec"] = round(time.perf_counter() - t0, 2)
    PERF["dispatch_engine_sec"] = d["elapsed_sec"]
    PERF["dispatch_trucks"] = d["truck_count"]
    total = server.api("GET", "/api/health").json()["db"]["products"]
    PERF["products_in_db"] = total
    times = {}
    for name, q in {"exact": "FP00000001", "order": "D900000001", "customer": "명강스틸", "dlv": "명강스틸 2공장으로 가는 거",
                    "not_found": "ZZ99999999"}.items():
        t0 = time.perf_counter()
        server.api("POST", "/api/chat", json={"question": q})
        times[name] = round(time.perf_counter() - t0, 3)
    PERF["chat_sec"] = times
    t0 = time.perf_counter()
    server.api("GET", "/api/dispatch")
    PERF["dispatch_list_sec"] = round(time.perf_counter() - t0, 3)
    (SHOTS.parent / "e2e_perf.json").write_text(json.dumps(PERF, ensure_ascii=False, indent=2), encoding="utf-8")
    assert d["elapsed_sec"] < 3.0
    assert times["exact"] < 0.5
    assert max(times.values()) < 2.0


@pytest.mark.parametrize("width,height", [(1366, 768), (1920, 1080)])
def test_layout_no_horizontal_scroll(page, width, height):
    page.set_viewport_size({"width": width, "height": height})
    for t in ("upload", "dispatch", "unassigned", "chat", "dashboard"):
        tab(page, t)
        page.wait_for_timeout(400)
        over = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert over <= 0, (t, over)
        shot(page, f"layout-{t}-{width}")
    assert page.console_errors == []
